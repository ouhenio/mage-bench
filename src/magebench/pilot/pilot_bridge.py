"""Bridge-facing helpers used by the pilot loop."""

import json
from collections.abc import Callable, Sequence
from logging import Logger
from pathlib import Path

from mcp import ClientSession
from mcp.types import Tool

from magebench.game.game_export_types import (
    Choice,
    Decision,
    MultiAmountItem,
    PilotContext,
    Snapshot,
    require_snapshot,
)
from magebench.game.game_log import GameLogWriter
from magebench.pilot.pilot_game_state import parse_context_metadata
from magebench.pilot.tool_error import ToolExecutionError, extract_text_content


def build_pilot_snapshot(data: dict, board: list[dict] | None, decision: Decision) -> Snapshot:
    """Build a typed snapshot from a pass_priority/get_action_choices result."""
    players: list[dict] = []
    active_player: str | None = None
    if board:
        for p in board:
            name = p.get("name")
            life = p.get("life")
            library_size = p.get("library_size", 0)
            assert isinstance(name, str) and name, f"pilot board player missing name: {p!r}"
            assert isinstance(life, int), f"pilot board player life must be an int, got {life!r}"
            assert isinstance(library_size, int), (
                f"pilot board player library_size must be an int, got {library_size!r}"
            )
            if p.get("is_active"):
                active_player = name
            hand = p.get("hand")
            battlefield = p.get("battlefield")
            graveyard = p.get("graveyard")
            player: dict = {
                "name": name,
                "life": life,
                "library_size": library_size,
                "battlefield": [] if battlefield is None else battlefield,
                "graveyard": [] if graveyard is None else graveyard,
                "hand": [] if hand is None else hand,
            }
            player["hand_count"] = p.get("hand_size", len(hand) if hand is not None else 0)
            for zone in ("battlefield", "graveyard", "exile", "commanders"):
                if p.get(zone):
                    player[zone] = p[zone]
            if p.get("counters"):
                player["counters"] = p["counters"]
            players.append(player)

    _, _, step, context_active_player = parse_context_metadata(data.get("context"))
    raw_seq = data.get("game_seq", data.get("board_cursor", 0))
    assert isinstance(raw_seq, int), f"pilot snapshot missing integer game_seq/board_cursor: {data!r}"
    stack = data.get("stack")
    snapshot_payload: dict[str, object] = {
        "seq": raw_seq,
        "turn": decision.turn,
        "phase": decision.phase,
        "step": step,
        "active_player": active_player or context_active_player,
        "priority_player": decision.player,
        "players": players,
        "stack": [] if stack is None else stack,
    }
    if data.get("combat") is not None:
        snapshot_payload["combat"] = data["combat"]
    return require_snapshot(snapshot_payload, source="pilot snapshot")


def build_pilot_decision(
    data: dict, fallback_board: list[dict] | None = None, decision_index: int = 0
) -> Decision:
    """Build a decision-like dict from a pass_priority/get_action_choices result.

    `fallback_board` is the last known board, used when this result carries none
    (the board_unchanged path). Without it `player` stays the literal "You", and
    since no player is named "You" the renderer redacts the pilot's own hand as
    if it belonged to an opponent -- the policy is then asked to act without
    seeing the cards it holds.
    """
    raw_choices = data.get("choices")
    if raw_choices is None:
        raw_choices = []
    choices = Choice.coerce_list(raw_choices)
    action_type = data.get("action_type")
    response_type = data.get("response_type")
    message = data.get("message")
    assert action_type is None or isinstance(action_type, str), (
        f"action_type must be a string when present, got {action_type!r}"
    )
    assert response_type is None or isinstance(response_type, str), (
        f"response_type must be a string when present, got {response_type!r}"
    )
    assert message is None or isinstance(message, str), f"message must be a string when present, got {message!r}"
    decision = Decision(
        # WAS HARDCODED 0, on every decision of every live game. The header this
        # feeds reads "[Decision N, snapshot=M] Turn T ...", and N never moved --
        # 29,906 of 29,906 decisions across 400 rollouts rendered as Decision 0.
        # The model could not tell its first decision from its fortieth, and any
        # analysis asking "how long ago was this card revealed" was reading a
        # position field that had no position in it.
        #
        # snapshot_index stays 0 deliberately: the pilot renders one snapshot per
        # decision, so 0 is the true value here rather than a missing one.
        index=decision_index,
        snapshot_index=0,
        player="You",
        turn=0,
        phase="",
        action_type="" if action_type is None else action_type,
        response_type="" if response_type is None else response_type,
        message="" if message is None else message,
        choices=choices,
        choice_count=len(choices),
        is_forced=len(choices) <= 1,
        llm_event_indices=[],
        subsequent_actions=[],
    )

    context_turn, context_phase, _, _ = parse_context_metadata(data.get("context"))
    if context_turn is not None:
        decision.turn = context_turn
    if context_phase is not None:
        decision.phase = context_phase

    board = data.get("board")
    if not isinstance(board, list):
        board = fallback_board
    if isinstance(board, list):
        for p in board:
            if isinstance(p, dict) and p.get("is_you"):
                decision.player = p["name"]
                break

    pilot_ctx: dict = {}
    if "untapped_lands" in data:
        pilot_ctx["untapped_lands"] = data["untapped_lands"]
    if "land_drops_used" in data:
        pilot_ctx["land_drops_used"] = data["land_drops_used"]
    if "combat_phase" in data:
        pilot_ctx["combat_phase"] = data["combat_phase"]
    if "already_attacking" in data:
        pilot_ctx["already_attacking"] = data["already_attacking"]
    if "incoming_attackers" in data:
        pilot_ctx["incoming_attackers"] = data["incoming_attackers"]
    if pilot_ctx:
        decision.pilot_context = PilotContext.from_mapping(pilot_ctx)

    raw_items = data.get("items")
    if raw_items:
        decision.items = MultiAmountItem.coerce_list(raw_items)
        if "total_min" in data:
            decision.total_min = data["total_min"]
        if "total_max" in data:
            decision.total_max = data["total_max"]

    return decision


def mcp_tools_to_openai(mcp_tools: Sequence[Tool], allowed_tools: set[str] | None = None) -> list[dict]:
    """Convert MCP tool definitions to OpenAI function calling format."""
    return [
        {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.inputSchema or {"type": "object", "properties": {}},
            },
        }
        for tool in mcp_tools
        if allowed_tools is None or tool.name in allowed_tools
    ]


async def execute_tool(session: ClientSession, name: str, arguments: dict) -> str:
    """Route a tool call through the MCP session and return the result text."""
    try:
        result = await session.call_tool(name, arguments)
    except Exception as exc:
        raise ToolExecutionError(f"MCP tool {name} failed: {exc}") from exc
    return extract_text_content(name, result)


def _tool_execution_error_result(error: ToolExecutionError, game_seq: int | None) -> str:
    """Build a structured tool_call payload for fatal MCP execution failures."""
    result: dict[str, object] = {
        "success": False,
        "error": str(error),
        "error_code": "tool_execution_error",
        "retryable": False,
    }
    if game_seq is not None:
        result["game_seq"] = game_seq
    return json.dumps(result, separators=(",", ":"))


# The engine's own wording for a bridge that has already shut down. A constant rather than a
# literal at the use site so the string that decides "recorded vs fatal" is greppable, and so
# a test can reference the same one the code matches on.
BRIDGE_TEARDOWN_MARKER = "Bridge processor is shut down"


def _record_tool_execution_failure(
    error: ToolExecutionError,
    username: str,
    game_dir: Path | None,
    game_log: GameLogWriter | None,
    *,
    logger: Logger,
    log_error_fn: Callable[[Logger, Path | None, str, str], None],
) -> None:
    """Persist fatal MCP tool failures so exports don't look falsely clean.

    A TORN-DOWN BRIDGE IS RECORDED BUT NOT WRITTEN TO errors.log, because it is what the
    end of a game looks like rather than a failure. Measured across the 1,010 v1 production
    games: "Bridge processor is shut down" occurs in 96.2% of them and reaches errors.log in
    2.2%. It is present in 39 of 40 games in BOTH arms of the paired flag validation. An
    event that occurs in ~96% of SUCCESSFUL games is not a failure mode.

    Why it has to be handled HERE and not at a call site. `collect.py` drops any game with a
    non-empty errors.log, so with the empty-priority flag ON this routing discarded 26 of 40
    played games -- turning a 2.39x speedup into a 20% net LOSS. A previous fix wrapped the
    auto-resolve path's own `choose_action`, and that call never fails: it fired ZERO times
    across 2,012 auto-resolved decisions while the rate stayed at 26/40. The failures are at
    `get_game_state` (8), `pass_priority` (3) and `get_game_log` (3). This function is the
    single chokepoint every one of them passes through, which is why the fix belongs here.

    The event is STILL RECORDED: `llm_error` goes to the game log unconditionally, as it
    already did in the OFF arm's transcripts. Only the errors.log line is withheld, and
    only for this one condition. Everything else -- a timeout, a protocol failure, an unknown
    tool name -- is still fatal, because the parity argument covers this condition alone.

    NOT via `_has_errors`: reclassifying post-game errors there may well be right, but it
    revalidates games every past corpus excluded, which is a decision about existing data
    rather than a fix to new behaviour. Kept separable deliberately.

    A game that dies BEFORE reaching game_end is still excluded, by the artifact gate rather
    than by this one -- karn-sft measured zero overlap between the 22 v1 games with a
    non-empty errors.log and the 977 its renderer accepted, so the two gates already agree.

    If the engine's wording changes this stops matching and these become fatal again. That
    is over-recording, which is the safe direction to fail in.
    """
    error_str = str(error)
    is_teardown = BRIDGE_TEARDOWN_MARKER in error_str
    if game_log:
        # `bridge_teardown` is the PRODUCTION COUNTER for this branch, and it exists because
        # the previous fix for this defect had none. That fix was correct code with a
        # behavioural test in both directions and a mutation check that failed as designed --
        # and it fired ZERO times in 2,012 decisions, because it guarded a call that never
        # fails. Every check verified behaviour CONDITIONAL ON REACHING THE CODE; none asked
        # whether the code is reached. After deploying this, the first question is not "do the
        # tests pass" but "how many times did it fire": count bridge_teardown=true against the
        # errors.log rate, and if it is zero the fix is inert again whatever the suite says.
        game_log.emit(
            "llm_error",
            error_type=type(error).__name__,
            error_message=error_str[:500],
            bridge_teardown=is_teardown,
        )
    if is_teardown:
        logger.warning(
            "[%s] bridge already torn down (%s) -- recorded, not fatal", username, error_str[:200]
        )
        return
    log_error_fn(logger, game_dir, username, f"[pilot] Fatal tool error: {error_str}")
