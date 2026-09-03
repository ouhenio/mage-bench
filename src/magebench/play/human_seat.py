"""The human seat: a bridge seat a browser can play, over SSE + POST.

One process, one seat. It starts its own bridge JVM, blocks on that seat's
decisions like any other client, and exposes them to a browser:

    GET  /seat/<name>/events    SSE: state, frame, phase, auto_passed, game_over
    POST /seat/<name>/action    body = choose_action arguments, verbatim
    GET  /seat/<name>/state     get_game_state, for a client that just connected

The client holds no rules. Everything it renders came from the bridge, and the
hidden-information filter is in the bridge JVM (BridgeGameStateBuilder attaches
`hand` only to the seat's own player), so a browser cannot ask past it.

See docs/play-client-contract.md in the mtg repo for the agreed wire contract.
Two of its promises are kept HERE and nowhere else:

  * BOARD IS ALWAYS PRESENT. `board` is omitted whenever a request passes a
    `board_cursor` that matches (BridgePublishedActionChoices.copyActionResult),
    so this adapter never sends one. Forwarding the omission would hand the
    client a decision with no board, which is the defect
    p1-pilot-blind-to-own-hand-on-board-unchanged records against the pilot.
  * ZERO-CHOICE DECISIONS ARE ANSWERED HERE, not shown. The predicate is
    pilot.auto_resolve's, imported rather than rewritten, so the human seat and
    the policy agree on what "no decision" means. The line is at ZERO and not
    "fewer than two": a one-choice decision is play-or-pass and goes to the human.
"""

from __future__ import annotations

import argparse
import json
import queue
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from magebench.common.bridge_session import BridgeSession
from magebench.common.log import get_logger, setup_logging
from magebench.pilot.auto_resolve import FORCED_ANSWER, is_forced_decision
from magebench.play.bridge_process import BridgeProcess

logger = get_logger(__name__)

# How long one blocking decision call may sit in the bridge. A human game has no
# opponent-thinking bound worth guessing at, and the bridge returns as soon as a
# decision exists, so this is a liveness ceiling and not a game rule.
_DECISION_BLOCK_TIMEOUT_SECS = 3600

# How often the phase poller asks the bridge where the game is. It runs
# concurrently with the blocking decision call -- McpServer uses a
# virtual-thread-per-task executor, so a second request is served while the
# first is parked.
_PHASE_POLL_SECS = 1.0

# Events kept for Last-Event-ID replay. A game is a few hundred events.
_EVENT_BUFFER = 2000


class AdapterInvariantError(RuntimeError):
    """A frame violated an invariant and was REFUSED, not logged and emitted."""


def check_frame_invariants(frame: dict, seat_player: str | None = None) -> None:
    """Refuse a frame that could mislead or leak. Raises; never returns False.

    The JVM filter is the mechanism and this is the witness -- two independent
    checks of one property, so that a future shortcut which renders from
    `state` cannot quietly drop the filter. The client runs the same two.

    EXACTLY ONE, not at most one: zero `hand` arrays is also a bug. It is what a
    seat sees when its own player id fails to resolve, and it renders as a
    legal-looking board with no hand rather than as an error.

    AND IT MUST BE THE RIGHT PLAYER. karn-interface's leak lens built a frame
    where the OPPONENT carried is_you and the hand: exactly one hand array, on
    the wrong player, and a client that trusts is_you renders the opponent's
    hand as its own. `seat_player` is the name this adapter passed to its bridge
    JVM at launch, so checking against it is independent of anything the frame
    says -- which is the whole point. A check that reads its expectation out of
    the payload it is checking is not a check.
    """
    board = frame.get("board")
    if not isinstance(board, list):
        raise AdapterInvariantError(
            f"frame has no board[] (got {type(board).__name__}). The adapter never sends "
            f"board_cursor, so the bridge must never omit the board; a missing board here "
            f"means that promise broke upstream. Frame keys: {sorted(frame)}"
        )
    with_hand = [p.get("name") for p in board if isinstance(p, dict) and "hand" in p]
    if len(with_hand) != 1:
        raise AdapterInvariantError(
            f"expected exactly one player in board[] carrying a hand array, got "
            f"{len(with_hand)}: {with_hand}. More than one is a hidden-information leak; "
            f"zero means the seat's own player id did not resolve."
        )
    if seat_player is not None and with_hand[0] != seat_player:
        raise AdapterInvariantError(
            f"the hand array is on {with_hand[0]!r}, but this adapter serves {seat_player!r}. "
            f"Exactly one hand is present, so the count check passes and the frame still "
            f"shows the wrong seat's cards."
        )
    if seat_player is not None:
        is_you = [p.get("name") for p in board if isinstance(p, dict) and p.get("is_you")]
        if is_you != [seat_player]:
            raise AdapterInvariantError(
                f"is_you names {is_you}, but this adapter serves {seat_player!r}."
            )


class EventStream:
    """Ordered, id'd events with replay for a client that reconnects."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._events: list[tuple[int, str, dict]] = []
        self._next_id = 1
        self._subscribers: list[queue.Queue] = []

    def emit(self, event_type: str, data: dict) -> int:
        with self._lock:
            event_id = self._next_id
            self._next_id += 1
            self._events.append((event_id, event_type, data))
            if len(self._events) > _EVENT_BUFFER:
                del self._events[: len(self._events) - _EVENT_BUFFER]
            subscribers = list(self._subscribers)
        for q in subscribers:
            q.put((event_id, event_type, data))
        logger.debug("[human-seat] event %s #%s", event_type, event_id)
        return event_id

    def subscribe(self, last_event_id: int | None) -> tuple[queue.Queue, list[tuple[int, str, dict]]]:
        q: queue.Queue = queue.Queue()
        with self._lock:
            self._subscribers.append(q)
            if last_event_id is None:
                backlog: list[tuple[int, str, dict]] = []
            else:
                backlog = [e for e in self._events if e[0] > last_event_id]
        return q, backlog

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            if q in self._subscribers:
                self._subscribers.remove(q)


class SeatDriver:
    """Blocks on this seat's decisions and routes them to and from the browser."""

    def __init__(
        self,
        session: BridgeSession,
        events: EventStream,
        *,
        seat_player: str,
        record_path: Path | None = None,
    ) -> None:
        self._session = session
        self._events = events
        # The name this adapter passed to its bridge JVM. Known BEFORE any frame
        # arrives, which is what makes it usable as the expectation.
        self._seat_player = seat_player
        self._actions: queue.Queue = queue.Queue()
        self._record_path = record_path
        self._stop = threading.Event()
        self._state_lock = threading.Lock()
        self._last_phase: tuple[int | None, str | None, str | None] | None = None
        self._last_turn: int | None = None
        # The decision currently in front of the human, kept so a client that
        # connects AFTER it was emitted still gets it. Without this a browser that
        # attaches even a second late sees a board and no question, and the game
        # stalls on a decision nobody was shown -- measured 2026-09-02, the adapter
        # emitted the mulligan before the client's SSE stream was up and both sides
        # waited for the other.
        self._open_frame: dict | None = None
        self.finished = threading.Event()

    # -- browser -> seat -------------------------------------------------

    def submit_action(self, arguments: dict) -> None:
        self._actions.put(arguments)

    # -- recording -------------------------------------------------------

    def _record(self, event_type: str, data: dict) -> None:
        if self._record_path is None:
            return
        with open(self._record_path, "a") as fh:
            fh.write(json.dumps({"ts": time.time(), "type": event_type, "data": data},
                                separators=(",", ":")) + "\n")

    def _emit(self, event_type: str, data: dict) -> None:
        self._record(event_type, data)
        self._events.emit(event_type, data)

    # -- seat -> browser -------------------------------------------------

    def emit_frame(self, frame: dict) -> None:
        check_frame_invariants(frame, self._seat_player)
        with self._state_lock:
            self._open_frame = frame
        self._emit("frame", frame)

    def reemit_open_frame(self) -> bool:
        """Re-send the decision the human still owes an answer to, if any."""
        with self._state_lock:
            frame = self._open_frame
        if frame is None:
            return False
        self._events.emit("frame", frame)
        return True

    @property
    def seat_player(self) -> str:
        return self._seat_player

    def emit_state(self) -> dict:
        state = self._session.call_tool_json("get_game_state", {})
        # ADAPTER-SUPPLIED, not from the bridge: the client locks its identity to
        # this before rendering anything, so its seat check and the adapter's are
        # independent instead of both reading is_you off one payload.
        state["seat_player"] = self._seat_player
        self._emit("state", state)
        return state

    def poll_phases(self) -> None:
        """Emit a `phase` event on every transition, concurrently with the block."""
        while not self._stop.is_set():
            try:
                state = self._session.call_tool_json("get_game_state", {}, timeout=10)
            except RuntimeError as exc:
                # The bridge going away is how a finished game looks from here.
                logger.debug("[human-seat] phase poll stopped: %s", exc)
                return
            key = (state.get("turn"), state.get("phase"), state.get("step"))
            with self._state_lock:
                self._last_turn = state.get("turn")
                changed = key != self._last_phase
                if changed:
                    self._last_phase = key
            if changed:
                self._emit("phase", {
                    "turn": state.get("turn"),
                    "phase": state.get("phase"),
                    "step": state.get("step"),
                    "active_player": state.get("active_player"),
                })
            self._stop.wait(_PHASE_POLL_SECS)

    def run(self) -> None:
        """Drive the seat until the game ends."""
        try:
            self.emit_state()
            while not self._stop.is_set():
                result = self._session.call_tool_json(
                    "pass_priority", {}, timeout=_DECISION_BLOCK_TIMEOUT_SECS
                )
                if self._game_over(result):
                    return
                if not result.get("action_pending"):
                    continue
                if not self._handle_pending(result):
                    return
        finally:
            self._stop.set()
            self.finished.set()

    def _game_over(self, result: dict) -> bool:
        if result.get("game_over") or result.get("player_dead"):
            self._emit("game_over", {
                "game_over": bool(result.get("game_over")),
                "player_dead": bool(result.get("player_dead")),
                "game_seq": result.get("game_seq"),
            })
            return True
        return False

    def _handle_pending(self, result: dict) -> bool:
        """Answer decisions until none is pending. False when the game ended."""
        while result is not None and result.get("action_pending"):
            if self._stop.is_set():
                return False

            if is_forced_decision(result):
                with self._state_lock:
                    turn = self._last_turn
                self._emit("auto_passed", {
                    "context": result.get("context"),
                    "game_seq": result.get("game_seq"),
                    "turn": turn,
                })
                result = self._session.call_tool_json("choose_action", dict(FORCED_ANSWER))
                if self._game_over(result):
                    return False
                continue

            self.emit_frame(result)
            arguments = self._actions.get()
            with self._state_lock:
                self._open_frame = None
            if arguments is None:
                return False
            result = self._session.call_tool_json(
                "choose_action", arguments, timeout=_DECISION_BLOCK_TIMEOUT_SECS
            )
            if self._game_over(result):
                return False
            if result.get("success") is False:
                # A rejected choice is not a failure of the game: the bridge says
                # what was wrong and whether it can be retried, and the human gets
                # the same decision back with the error attached. Emitted as an
                # `error` event rather than folded into the frame so the client
                # never has to tell an error frame from a real one.
                self._emit("error", {
                    "error": result.get("error"),
                    "error_code": result.get("error_code"),
                    "retryable": result.get("retryable"),
                })
                if not result.get("action_pending"):
                    result = self._session.call_tool_json("get_action_choices", {})
        return True

    def concede(self) -> dict:
        """Concede this seat's game so it ENDS rather than expires.

        Without this the only way out is to stop answering, and the engine then
        sits on a pending prompt until the job's wall clock kills it -- no
        game_end, no winner, and a recording that says "the job expired" where it
        should say "conceded on turn 9". Measured on the first real human game:
        it stopped at turn 9 on an unanswered kicker prompt and produced no
        server game_end at all.

        The bridge already has the tool; nothing new happens in the engine.
        """
        result = self._session.call_tool_json("concede", {})
        self._emit("conceded", {"game_seq": result.get("game_seq")})
        # The decision loop is parked in _actions.get(); wake it so it can see
        # the game is over rather than hold the seat until the clock runs out.
        self._actions.put(None)
        return result

    def stop(self) -> None:
        self._stop.set()
        self._actions.put(None)


def _make_handler(seat: str, driver: SeatDriver, events: EventStream):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt: str, *args) -> None:  # noqa: A003 - BaseHTTPRequestHandler API
            logger.debug("[human-seat http] " + fmt, *args)

        def _json(self, code: int, payload: dict) -> None:
            body = json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            if self.path == f"/seat/{seat}/events":
                self._sse()
            elif self.path == f"/seat/{seat}/state":
                self._json(200, driver.emit_state())
            elif self.path == f"/seat/{seat}":
                self._json(200, {"seat": seat, "seat_player": driver.seat_player})
            elif self.path == "/healthz":
                self._json(200, {"ok": True, "seat": seat})
            else:
                self._json(404, {"error": f"no such path: {self.path}"})

        def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            if self.path == f"/seat/{seat}/concede":
                try:
                    result = driver.concede()
                except RuntimeError as exc:
                    self._json(502, {"error": f"concede failed at the bridge: {exc}"})
                    return
                self._json(200, {"conceded": True, "result": result})
                return
            if self.path != f"/seat/{seat}/action":
                self._json(404, {"error": f"no such path: {self.path}"})
                return
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            try:
                arguments = json.loads(raw)
            except json.JSONDecodeError as exc:
                self._json(400, {"error": f"body is not JSON: {exc}"})
                return
            if not isinstance(arguments, dict):
                self._json(400, {"error": "body must be a choose_action arguments object"})
                return
            driver.submit_action(arguments)
            self._json(202, {"accepted": True})

        def _sse(self) -> None:
            raw_last = self.headers.get("Last-Event-ID")
            last_id = int(raw_last) if raw_last and raw_last.isdigit() else None
            q, backlog = events.subscribe(last_id)
            # Connection: close, NOT keep-alive, and this is load-bearing. An SSE
            # body has no Content-Length and BaseHTTPRequestHandler does not chunk,
            # so under HTTP/1.1 keep-alive the client cannot tell where the body
            # ends and waits forever -- measured 2026-09-02: the adapter emitted
            # state, phase and a frame, the browser saw none of them, and the game
            # stalled on a decision nobody could answer. Closing the connection
            # makes read-to-EOF the framing, which is what every SSE client does.
            self.close_connection = True
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            try:
                for item in backlog:
                    self._write_event(*item)
                # A reconnecting client gets authoritative state regardless of
                # replay, and then the open decision if there is one, so the
                # client never has to reason about which arrived.
                if last_id is None:
                    driver.emit_state()
                    driver.reemit_open_frame()
                while True:
                    try:
                        item = q.get(timeout=15)
                    except queue.Empty:
                        self.wfile.write(b": keepalive\n\n")
                        self.wfile.flush()
                        continue
                    self._write_event(*item)
            except (BrokenPipeError, ConnectionResetError):
                logger.info("[human-seat] SSE client disconnected")
            finally:
                events.unsubscribe(q)

        def _write_event(self, event_id: int, event_type: str, data: dict) -> None:
            payload = json.dumps(data, separators=(",", ":"))
            self.wfile.write(
                f"id: {event_id}\nevent: {event_type}\ndata: {payload}\n\n".encode()
            )
            self.wfile.flush()

    return Handler


def main() -> int:
    setup_logging()
    parser = argparse.ArgumentParser(description="Human seat adapter for a browser play client")
    parser.add_argument("--server", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=17171, help="XMage server port")
    parser.add_argument("--username", required=True, help="Seat name; also the URL segment")
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--deck", type=Path, help="Path to the seat's .dck")
    parser.add_argument("--table-id", default="", help="Pin the bridge to this table")
    parser.add_argument("--game-dir", type=Path, help="Where to record the seat's event stream")
    parser.add_argument(
        "--http-port",
        type=int,
        required=True,
        help="Adapter HTTP port. Explicit, never auto-picked: an explicitly numbered "
             "port is killable by identity and does not race under concurrency.",
    )
    parser.add_argument(
        "--bind",
        default="127.0.0.1",
        help="Adapter bind address. Loopback only; reach it over ssh -L, never a public port.",
    )
    args = parser.parse_args()

    if args.bind != "127.0.0.1":
        raise SystemExit(
            f"--bind {args.bind!r} refused. This adapter serves a seat's full view of a game "
            f"and must not be reachable off the box; use ssh -L to reach 127.0.0.1."
        )

    game_dir: Path | None = args.game_dir
    if game_dir is not None:
        game_dir.mkdir(parents=True, exist_ok=True)

    bridge = BridgeProcess(
        server=args.server,
        port=args.port,
        username=args.username,
        project_root=args.project_root.resolve(),
        deck_path=args.deck.resolve() if args.deck else None,
        table_id=args.table_id or None,
        log_file=game_dir / f"{args.username}_bridge.log" if game_dir else None,
        bridge_log_path=game_dir / f"{args.username}_bridge-events.jsonl" if game_dir else None,
        error_log_path=game_dir / f"{args.username}_errors.log" if game_dir else None,
    )
    session = bridge.start()

    events = EventStream()
    driver = SeatDriver(
        session,
        events,
        seat_player=args.username,
        record_path=game_dir / f"{args.username}_seat_events.jsonl" if game_dir else None,
    )

    httpd = ThreadingHTTPServer((args.bind, args.http_port), _make_handler(args.username, driver, events))
    httpd.daemon_threads = True
    http_thread = threading.Thread(target=httpd.serve_forever, name="human-seat-http", daemon=True)
    http_thread.start()
    logger.info("[human-seat] serving http://%s:%s/seat/%s/events",
                args.bind, args.http_port, args.username)

    phase_thread = threading.Thread(target=driver.poll_phases, name="human-seat-phase", daemon=True)
    phase_thread.start()

    exit_code = 0
    try:
        driver.run()
    except AdapterInvariantError:
        logger.exception("[human-seat] REFUSED a frame; the seat stops rather than render it")
        exit_code = 2
    except (RuntimeError, OSError):
        logger.exception("[human-seat] seat driver failed")
        exit_code = 1
    finally:
        driver.stop()
        httpd.shutdown()
        bridge.stop()
    logger.info("[human-seat] done (rc=%s)", exit_code)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
