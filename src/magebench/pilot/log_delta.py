"""Push the engine's play-by-play since this seat's last decision into the decision frame.

WHY. The engine writes a play-by-play -- "XMage AI cast Bloodghast", "XMage AI blocked Wurm
Token with Inti", "Eval1775 lost 3 life" -- and exposes it as `get_game_log`. The model never
fetched it: 113 calls across 929 corpus games, 112 of them after the bridge had closed at game
end, one successful. So every checkpoint so far inferred the opponent's play from snapshot
deltas, and a Bolt to the face and a Bolt to a chump-blocked creature look identical two
snapshots later.

THE MECHANISM ALREADY EXISTED. `get_game_log` takes a `cursor` for incremental updates and
`BridgeCallbackHandler.getGameLogChunk(maxChars, cursor)` implements it. Nothing pushed it and
nobody pulled it -- the instrument was there and unread, which is this project's recurring
shape rather than a new one.

WHERE IT IS INJECTED, AND WHY NOT ANYWHERE ELSE. Into `result_text` BEFORE
`game_log.emit("tool_call", ...)`. `render_for_pilot` is the one renderer both the pilot and
`render_conversations` call, and its docstring says that is what makes the training transcript
and the inference transcript agree by construction rather than by two matching edits. A delta
the pilot fetched and passed as an ARGUMENT would be invisible to the training renderer, which
reads a corpus row. Injecting before the emit puts it in the artifact, the frame and the row
from one write.

DO NOT "improve" this into a field set by the Java query builder. That is the same boundary
error: a change on one side with the consumer on the other.

THE CAP IS MEASURED, not chosen. Over 402 banked games and 64,121 decision intervals from the
corpus's own server_game_events.jsonl:

    lines   p50  1   p90   2   p99   5   max    296
    chars   p50 21   p90 174   p99 380   max 14208

    cap  500 chars -> truncates   229 of 64,121 intervals (0.36%)
    cap 1000 chars -> truncates    25            (0.04%)
    cap 2000 chars -> truncates     3            (0.005%)

2,000 is ~5x p99, truncates three intervals in sixty-four thousand, and is under half a
percent of a 131,072-token window. Anyone raising or lowering it should argue with that table.

AND 47.3% OF INTERVALS ARE EMPTY -- 30,325 of 64,121. That is the MAJORITY case, not an edge:
an empty heading on thirty thousand frames would be the auto-resolve defect inverted, noise
the model learns to ignore and then ignores when it is real. Nothing happened means NO BLOCK,
not an empty one.
"""
from __future__ import annotations

import json
import logging
import os

logger = logging.getLogger(__name__)

# The key the delta rides on inside the decision frame, and the heading the renderer emits.
DELTA_FIELD = "since_last_decision"
DELTA_HEADING = "## Since your last decision"
# Measured: see the module docstring's table. Per FRAME, not per game.
DEFAULT_MAX_CHARS = 2000
# The settings-manifest field name, so a census can key on it.
SETTING_NAME = "opponent_log_delta"


_announced = False


def enabled() -> bool:
    """Whether to push the delta. DEFAULT OFF, explicit ON, and it says which.

    Off by default because the 2,429-game corpus and the three evals ran without it and must
    stay reproducible. A malformed value raises rather than falling back -- the deck-block
    defect was a script default silently beating an explicit setting.
    """
    global _announced
    # The provenance line is printed ONCE per process, not once per caller. Two call sites
    # legitimately ask -- the loop, to set state, and the game_start emit, to record it -- and
    # a line per ask would make a log look like the setting had changed.
    announce, _announced = not _announced, True
    raw = os.environ.get("MAGEBENCH_OPPONENT_LOG_DELTA")
    if raw is None:
        if announce:
            logger.info("[log_delta] opponent log delta: OFF (DEFAULT; nothing explicit in the environment)")
        return False
    if raw not in ("0", "1"):
        raise ValueError(
            f"MAGEBENCH_OPPONENT_LOG_DELTA={raw!r} is neither '0' nor '1'. "
            "This decides what the model sees at every decision; it is not guessable.")
    if announce:
        logger.info("[log_delta] opponent log delta: %s (EXPLICIT, from the environment)",
                    "ON" if raw == "1" else "OFF")
    return raw == "1"


def max_chars() -> int:
    raw = os.environ.get("MAGEBENCH_OPPONENT_LOG_DELTA_CHARS")
    if not raw:
        return DEFAULT_MAX_CHARS
    value = int(raw)          # malformed raises; never falls back
    if value < 1:
        raise ValueError(f"MAGEBENCH_OPPONENT_LOG_DELTA_CHARS={value} must be >= 1")
    return value


def clip(lines: list[str], cap: int) -> tuple[list[str], int]:
    """Keep whole lines within `cap` chars. Returns (kept, dropped_count).

    The MOST RECENT lines are kept, because the decision is about what just happened. Whole
    lines only: half an engine line reads as a different event.
    """
    kept: list[str] = []
    used = 0
    for line in reversed(lines):
        cost = len(line) + 1
        if used + cost > cap and kept:
            break
        kept.append(line)
        used += cost
    kept.reverse()
    return kept, len(lines) - len(kept)


def render_block(lines: list[str], dropped: int) -> str | None:
    """The frame text, or None when nothing happened.

    None -- not "" and not a bare heading. 47.3% of real intervals are empty; a heading on
    all of them trains the model to skip the section it is supposed to read.
    """
    if not lines:
        return None
    body = "\n".join(lines)
    if dropped:
        # SAY SO. A silently shortened recap is a recap the model cannot calibrate against.
        body = f"… and {dropped} earlier line{'s' if dropped != 1 else ''} not shown\n" + body
    return f"{DELTA_HEADING}\n{body}"


async def fetch_and_inject(session, state, result_text: str) -> str:
    """Fetch the log since this seat's last decision and put it in the frame.

    CALLED ON EVERY DECISION-BEARING PATH, including the auto-resolved ones. With the
    auto-resolve flag on, 46.6% of decisions are priority windows the harness answers itself;
    if the cursor does not advance across those, every later shown frame repeats lines already
    passed or skips them. That is the majority of intervals, not a corner case, and
    test_log_delta.py scans this module's call sites in pilot.py to assert none was missed.

    Returns result_text unchanged when the feature is off, when the result is not a decision,
    or when the log call fails -- a recap is not worth losing a game over, and a failure here
    is logged rather than swallowed.
    """
    if not state.log_delta_on:
        return result_text
    try:
        data = json.loads(result_text)
    except (json.JSONDecodeError, TypeError, ValueError):
        return result_text
    if not isinstance(data, dict) or not data.get("action_pending"):
        return result_text
    try:
        from magebench.pilot.pilot_bridge import execute_tool
        args = {"max_chars": 0}
        if state.log_cursor is not None:
            args["cursor"] = state.log_cursor
        raw = await execute_tool(session, "get_game_log", args)
        log = json.loads(raw)
    except Exception as exc:  # noqa: BLE001 -- see the docstring: logged, not swallowed
        logger.warning("[log_delta] get_game_log failed (%s); frame carries no delta", exc)
        return result_text
    # ADVANCE THE CURSOR EVEN WHEN THE DELTA IS EMPTY. Otherwise an empty interval leaves the
    # cursor behind and the next frame re-shows everything since the last non-empty one.
    if isinstance(log.get("cursor"), int):
        state.log_cursor = log["cursor"]
    # NOT `log.get("log") or ""`. The repo's no-fallback lint caught that, and it was right --
    # and right in the family this whole file is careful about. `or ""` collapses two different
    # facts: "the tool returned no `log` key", which means the result is not the shape this
    # code expects and should be loud, and "the log is empty", which is the 47.3% majority
    # case and is normal. Conflating them would have made a changed tool contract look like a
    # quiet game.
    text = log.get("log")
    if text is None:
        logger.warning("[log_delta] get_game_log returned no 'log' key (keys: %s); frame "
                       "carries no delta", sorted(log))
        return result_text
    if not isinstance(text, str):
        logger.warning("[log_delta] get_game_log's 'log' is %s, not a string; frame carries "
                       "no delta", type(text).__name__)
        return result_text
    lines = [ln for ln in text.splitlines() if ln.strip()]
    kept, dropped = clip(lines, max_chars())
    block = render_block(kept, dropped)
    if block is None:
        return result_text
    data[DELTA_FIELD] = block
    return json.dumps(data)
