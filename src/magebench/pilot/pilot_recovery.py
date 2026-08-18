"""Recovery and error-handling helpers for the pilot loop."""

import asyncio
import json
from dataclasses import dataclass
from logging import Logger
from typing import Protocol

from mcp import ClientSession

from magebench.game.game_log import GameLogWriter
from magebench.pilot.pilot_bridge import execute_tool
from magebench.pilot.pilot_state import PilotLoopState, reset_context
from magebench.pilot.tool_error import ToolExecutionError


class _ChoiceLike(Protocol):
    finish_reason: str | None


class _UsageLike(Protocol):
    completion_tokens: int | None


class _ResponseLike(Protocol):
    usage: _UsageLike | None


def _handle_truncated_response(
    state: PilotLoopState,
    choice: _ChoiceLike,
    response: _ResponseLike,
    game_log: GameLogWriter | None,
    *,
    logger: Logger,
    max_tokens: int,
    max_consecutive_truncations: int,
) -> bool:
    """Handle max-token truncation and reset context after repeated failures.

    UNREACHABLE UNDER THE CURRENT CONFIGURATION, and kept deliberately.
    finish_reason "length" fired 0 times in 41,970 decisions across 449 games:
    with thinking disabled a tool call is ~18 completion tokens against
    MAX_TOKENS=1024, so the budget is never approached.

    Not dead code, conditionally dead. Before MAGEBENCH_DISABLE_THINKING was set,
    ~19% of decisions were truncated mid-<think> and this path carried them. It
    becomes live again the moment a model reasons at length -- Qwen3.5-4B was
    measured at 10.5x the completion tokens of Qwen3-4B (median 89 vs 20), so
    re-measure this rate before switching rather than after.

    Its nudge still names pass_priority, unlike the no-tool-call nudge in pilot.py
    which was changed because it was coercing passes. That difference is not an
    oversight: nobody has measured this path coercing anything, because nobody can
    -- it does not currently execute. Editing it would be changing code on the
    strength of an analogy.
    """
    if choice.finish_reason != "length":
        state.consecutive_truncations = 0
        return False

    state.consecutive_truncations += 1
    tokens_used = (response.usage.completion_tokens or 0) if response.usage else "?"
    logger.warning(
        "[pilot] OUTPUT TRUNCATED: finish_reason=length, completion_tokens=%s/%s. "
        "Model hit max_tokens cap before producing a tool call. [%d]",
        tokens_used,
        max_tokens,
        state.consecutive_truncations,
    )
    if state.consecutive_truncations < max_consecutive_truncations:
        return False

    logger.warning("[pilot] Repeated truncations, resetting conversation context")
    if game_log:
        game_log.emit("context_reset", reason="repeated_truncations")
    reset_context(
        state,
        "Continue playing. Be concise. Call pass_priority.",
        reset_board_context=True,
    )
    state.consecutive_truncations = 0
    return True


def _parse_game_ended_reason(result_text: str) -> str | None:
    """Return 'game_over' or 'player_dead' if the tool result indicates the game ended."""
    try:
        data = json.loads(result_text)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    if data.get("game_over") or data.get("stop_reason") == "game_over":
        return "game_over"
    if data.get("player_dead"):
        return "player_dead"
    return None


async def _recover_from_stall(
    session: ClientSession,
    state: PilotLoopState,
    game_log: GameLogWriter | None,
    turn_tools_called: set[str],
    *,
    logger: Logger,
) -> bool:
    """Auto-pass once, then reset conversation after a stalled turn sequence.

    Returns True if the game ended during recovery (game_over or player_dead).
    """
    last_tools = sorted(turn_tools_called)
    logger.warning(
        "[pilot] Stalled: %d turns without progress, last tools: %s, auto-passing until next event",
        state.turns_without_progress,
        last_tools or "none",
    )
    if game_log:
        game_log.emit(
            "stall",
            turns_without_progress=state.turns_without_progress,
            last_tools=last_tools,
            # The auto-pass below is chosen by the harness, not the policy. Trajectories that
            # include it carry an action the model never selected, so training data must be able
            # to find and exclude it. Filter on this flag rather than on the event name.
            harness_action=True,
        )
    try:
        await execute_tool(
            session,
            "send_chat_message",
            {"message": "Brain freeze! Auto-passing until next turn..."},
        )
    except ToolExecutionError:
        pass
    game_ended = False
    try:
        result_text = await execute_tool(session, "pass_priority", {})
        logger.info("[pilot] Auto-passed stalled action")
        reason = _parse_game_ended_reason(result_text)
        if reason:
            logger.info("[pilot] %s detected during stall recovery", reason)
            if game_log:
                game_log.emit("auto_pilot_mode", reason=reason)
            game_ended = True
    except ToolExecutionError as exc:
        logger.warning("[pilot] Auto-pass failed: %s", exc)

    state.turns_without_progress = 0
    if not game_ended:
        reset_context(
            state,
            "A new turn has started. Call pass_priority to continue.",
            reset_board_context=False,
        )
    return game_ended


async def _handle_timeout(
    session: ClientSession,
    state: PilotLoopState,
    game_log: GameLogWriter | None,
    *,
    logger: Logger,
    llm_request_timeout_secs: int,
    max_consecutive_timeouts: int,
) -> bool:
    """Keep the game moving across request timeouts and reset repeated failures.

    Returns True if the game ended during recovery (game_over or player_dead).
    """
    state.consecutive_timeouts += 1
    logger.warning(
        "[pilot] LLM request timed out after %ss [%d]",
        llm_request_timeout_secs,
        state.consecutive_timeouts,
    )
    if game_log:
        game_log.emit(
            "llm_error",
            error_type="timeout",
            error_message=f"Timed out after {llm_request_timeout_secs}s [{state.consecutive_timeouts}]",
        )
    try:
        result_text = await execute_tool(session, "pass_priority", {})
        reason = _parse_game_ended_reason(result_text)
        if reason:
            logger.info("[pilot] %s detected during timeout recovery", reason)
            if game_log:
                game_log.emit("auto_pilot_mode", reason=reason)
            return True
    except ToolExecutionError:
        await asyncio.sleep(5)

    full_reset = state.consecutive_timeouts >= max_consecutive_timeouts
    if full_reset:
        logger.warning("[pilot] Repeated LLM timeouts, resetting conversation context")
        if game_log:
            game_log.emit("context_reset", reason="repeated_timeouts")
        state.consecutive_timeouts = 0
    reset_context(
        state,
        # Deliberately still names pass_priority, unlike the no-tool-call nudge in
        # pilot.py. Two differences: this follows a context WIPE, so it is the entire
        # opening message of a fresh conversation and needs to orient rather than
        # correct; and the harness has already executed pass_priority above, so this
        # tells the model how to re-read a state it can no longer see. The measured
        # 319 coerced passes were all the other path.
        "Continue playing. Call pass_priority.",
        reset_board_context=full_reset,
    )
    return False


@dataclass(frozen=True)
class _RecoveredFunction:
    name: str
    arguments: str


@dataclass(frozen=True)
class _RecoveredToolCall:
    """A tool call the model stated correctly but emitted without its envelope."""

    id: str
    function: _RecoveredFunction
    type: str = "function"


def recover_unwrapped_tool_call(content: str | None, tool_names: set[str]) -> list | None:
    """Accept a well-formed tool call the model emitted without <tool_call> tags.

    Qwen sometimes writes the call correctly into `content` and omits the wrapper.
    vLLM's hermes parser then returns no tool_calls, and the harness treats a
    correct decision as a non-answer. Measured across all tier-1 traces, 449 games
    and 41,970 decisions: 1,142 (2.7%) are a well-formed call that lost its
    envelope, every one with finish_reason "stop" -- a clean finish, not
    truncation.

    A second, smaller population shares the symptom and is NOT this bug: of the
    1,765 no-call turns, the other 623 (35%) are prose, the model answering the
    "[It's been N turns since you last chatted]" prompt instead of playing. The
    guards below refuse those, correctly -- they are not tool calls and must not
    be coerced into one. MAGEBENCH_CHAT_PROMPTS=0 is their fix.

    An earlier revision of this docstring said 1.29% over 312 games. That was the
    rate in the published corpus -- turns surviving into the final message list --
    not the rate at which the harness drops calls. Same quantity, wrong
    denominator, and it understated the fix by about half.

    The damage was not the dropped turn. The recovery nudge then said "Call
    pass_priority", the model obeyed, and a main phase with a land available
    became a pass -- manufacturing the exact defect the reminder work exists to
    remove.

    This is NOT fabrication. The model stated exactly one action, in the tool
    schema's own shape, and we accept a valid call that is missing its envelope.
    The guards below are what keep it that way: it must parse, name a tool that
    is actually in this game's toolset, and carry an arguments object. Prose that
    merely mentions a tool name does not qualify.

    Returns a one-element list shaped like the SDK's tool_calls, or None.
    """
    if not content:
        return None
    text = content.strip()
    if not text.startswith("{"):
        return None
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(parsed, dict):
        return None

    name = parsed.get("name")
    if not isinstance(name, str) or name not in tool_names:
        return None

    args = parsed.get("arguments", {})
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except (json.JSONDecodeError, ValueError):
            return None
    if not isinstance(args, dict):
        return None

    return [_RecoveredToolCall(id=f"recovered_{name}", function=_RecoveredFunction(name=name, arguments=json.dumps(args)))]


def _classify_permanent_llm_failure(error_str: str) -> str | None:
    """Return the permanent failure reason, if the error should abort the game."""
    permanent_codes = {"401", "402", "403", "404"}
    if not any(code in error_str for code in permanent_codes):
        return None
    is_not_found = "404" in error_str and "401" not in error_str
    return "Model not found" if is_not_found else "Credits exhausted"
