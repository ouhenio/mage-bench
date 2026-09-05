"""Pilot: LLM-powered game player that makes strategic decisions via MCP tools."""

import argparse
import asyncio
import json
import os
import sys
import time
from contextlib import ExitStack
from pathlib import Path
from typing import Protocol

from mcp import ClientSession
from openai import AsyncOpenAI, OpenAIError

from magebench.common.llm_cost import (
    DEFAULT_LLM_PROVIDER,
    SUPPORTED_LLM_PROVIDERS,
    get_model_price,
    llm_base_url,
    load_prices,
    required_api_key_env,
    write_cost_file,
)
from magebench.common.log import get_logger, log_error, setup_logging
from magebench.game.game_log import GameLogWriter
from magebench.pilot.deck_text import build_deck_block
from magebench.pilot.auto_pass import auto_pass_loop
from magebench.pilot.bridge_transport import build_bridge_launch_args, spawn_bridge_http
from magebench.pilot.pilot_bridge import (
    _record_tool_execution_failure as _record_tool_execution_failure_impl,
)
from magebench.pilot.pilot_bridge import (
    _tool_execution_error_result,
    execute_tool,
    mcp_tools_to_openai,
)
from magebench.pilot.pilot_recovery import (
    _classify_permanent_llm_failure,
    is_context_overflow,
    recover_unwrapped_tool_call,
)
from magebench.pilot.pilot_recovery import (
    _handle_timeout as _handle_timeout_impl,
)
from magebench.pilot.pilot_recovery import (
    _handle_truncated_response as _handle_truncated_response_impl,
)
from magebench.pilot.pilot_recovery import (
    _recover_from_stall as _recover_from_stall_impl,
)
from magebench.pilot.pilot_rendering import (
    CONTEXT_RECENT_COUNT,
    MAX_TOKENS,
    RENDER_INTERVAL,
    prompt_char_count,
    _fetch_state_summary,
    _find_cache_breakpoint_idx,
    _with_cache_control,
    render_context,
    render_for_pilot,
)
from magebench.pilot.context_segments import (
    PENDING_ANSWER_RESERVE_CHARS,
    SEGMENT_MAX_DECISIONS,
    context_window_mode,
    segment_budget_chars,
    should_close_segment,
)
from magebench.pilot.auto_resolve import (
    FORCED_ANSWER,
    auto_resolve_enabled,
    chose_unoffered,
    is_forced_decision,
)
from magebench.pilot.mulligan import (
    count_lands,
    is_mulligan_decision,
    mulligan_choice,
    mulligan_mode,
)
from magebench.pilot.pilot_state import (
    record_decision_seq,
    PilotLoopState,
    PilotTurnState,
    reset_render_cache,
    reset_context,
)
from magebench.pilot.prompts import load_prompts
from magebench.pilot.tool_error import ToolExecutionError

logger = get_logger(__name__)

DEFAULT_MODEL = "google/gemini-2.0-flash-001"

# Exit code returned when the LLM permanently fails (404 model not found,
# 402/403 credits exhausted). The orchestrator checks for this to abort the
# game early instead of wasting API tokens on the other player.
PERMANENT_FAILURE_EXIT_CODE = 3


# Ask the serving engine for exact prompt/completion token ids, for RL rollouts.
# vLLM-only: `return_token_ids` / `return_prompt_text` are vLLM extensions and other
# providers reject unknown request fields. Gated on an env var rather than on
# `provider == "local"` because run_pilot_loop is not passed the provider; if that
# changes, prefer the provider check.
CAPTURE_TOKEN_IDS = os.environ.get("MAGEBENCH_CAPTURE_TOKEN_IDS") == "1"

# Send the decision's identity alongside the request, so a serving-side brain can answer
# "which prompt option is the engine's hint pointing at" by OBJECT IDENTITY rather than
# by matching rules text against card names -- the text match resolves only 31.9% of
# hints, which is why a text-keyed generator acted on 3.79% of decisions where the engine
# acted on 11.74%.
#
# The consumer is NOT vLLM. vLLM 0.26 (the pinned engine) ignores unknown request fields
# outright: OpenAIBaseModel sets `model_config = ConfigDict(extra="allow")`
# (entrypoints/openai/engine/protocol.py:31-33) and only logs "the following fields were
# present in the request but ignored". The real consumer is the synth shim's
# `brain.handle(payload)` (pipelines/synth/engine_shim.py), which receives the whole
# request body because the OpenAI client merges extra_body into the top-level JSON --
# the same mechanism that makes chat_template_kwargs and return_token_ids work. Do not
# delete this as dead weight because vLLM ignores it.
#
# The gate exists for OpenRouter/Anthropic/OpenAI, which DO reject unknown fields, and it
# is enforced at startup in main() rather than per-request: a 400 discovered mid-batch
# costs the whole batch. Read from the environment rather than from `provider` for the
# same reason CAPTURE_TOKEN_IDS is -- run_pilot_loop is not passed the provider -- but
# main() does have it and checks there.
#
# Carries only the seq. The alias -> UUID map is deliberately NOT here: putting it on an
# MCP tool result would move prompt text, because a result whose `action_pending` is
# falsy is forwarded to the model VERBATIM (pilot_rendering.py:131-132) and 13 of the 15
# prompt goldens contain exactly such a raw-JSON tool message. The alias channel is
# `source_aliases` on the hint row plus the per-game alias side file instead, both
# written by AiHintProvider, both outside the prompt.
DECISION_IDENTITY = os.environ.get("MAGEBENCH_DECISION_IDENTITY") == "1"
LLM_REQUEST_TIMEOUT_SECS = 120
MAX_CONSECUTIVE_TIMEOUTS = 3
MAX_CONSECUTIVE_EMPTY_CHOICES = 5
MAX_GAME_DURATION_SECS = 3 * 3600  # 3 hours absolute maximum
MAX_TURNS_WITHOUT_PROGRESS = 20
MAX_CONSECUTIVE_PASS_ERRORS = 3
MAX_CONSECUTIVE_TRUNCATIONS = 3
MAX_CONSECUTIVE_EMPTY_ERRORS = 10  # bridge is dead if every tool returns empty error
MAX_EMPTY_RESPONSES = 10
MAX_CHAT_MESSAGES_PER_TURN = 2  # max send_chat_message calls per LLM iteration


# Two is enough to distinguish 'the prompt was long' from 'even a fresh prompt
# overflows'. The second case means the reset cannot help and the game should end
# loudly rather than spin until the batch deadline.
MAX_CONTEXT_OVERFLOW_RESETS = 2


class PermanentLLMError(Exception):
    """Raised when the LLM is permanently unreachable (model not found, credits exhausted)."""


class _ToolFunctionLike(Protocol):
    name: str
    arguments: str


class _ToolCallLike(Protocol):
    id: str
    function: _ToolFunctionLike


class _AssistantMessageLike(Protocol):
    content: str | None
    tool_calls: list[_ToolCallLike] | None


class _ChoiceLike(Protocol):
    finish_reason: str | None
    message: _AssistantMessageLike


class _UsageLike(Protocol):
    completion_tokens: int | None
    prompt_tokens: int | None


class _ResponseLike(Protocol):
    usage: _UsageLike | None


def _record_tool_execution_failure(
    error: ToolExecutionError,
    username: str,
    game_dir: Path | None,
    game_log: GameLogWriter | None,
) -> None:
    """Persist fatal MCP tool failures so exports don't look falsely clean."""
    _record_tool_execution_failure_impl(
        error,
        username,
        game_dir,
        game_log,
        logger=logger,
        log_error_fn=log_error,
    )


def _mark_game_aborted(game_dir: Path | None, username: str, reason: str, error_type: str) -> None:
    """Drop a marker file so a consumer cannot mistake an aborted game for a completed one.

    The game log and the exit code both already say the game died, but a downstream reader
    scanning game directories sees neither. A file whose presence is the signal survives being
    read by something that only knows how to glob.
    """
    if game_dir is None:
        return
    marker = game_dir / "ABORTED.json"
    payload = {
        "aborted": True,
        "player": username,
        "reason": reason,
        "error_type": error_type,
        "note": "Trajectory is incomplete. Do not use for training or scoring.",
    }
    marker.write_text(json.dumps(payload, indent=2) + "\n")
    log_error(logger, game_dir, username, f"game aborted: {reason}")


def _handle_truncated_response(
    state: PilotLoopState,
    choice: _ChoiceLike,
    response: _ResponseLike,
    game_log: GameLogWriter | None,
) -> bool:
    """Handle max-token truncation and reset context after repeated failures."""
    return _handle_truncated_response_impl(
        state,
        choice,
        response,
        game_log,
        logger=logger,
        max_tokens=MAX_TOKENS,
        max_consecutive_truncations=MAX_CONSECUTIVE_TRUNCATIONS,
    )


async def _recover_from_stall(
    session: ClientSession,
    state: PilotLoopState,
    game_log: GameLogWriter | None,
    turn_tools_called: set[str],
) -> bool:
    """Auto-pass once, then reset conversation after a stalled turn sequence."""
    return await _recover_from_stall_impl(
        session,
        state,
        game_log,
        turn_tools_called,
        logger=logger,
    )


async def _handle_timeout(
    session: ClientSession,
    state: PilotLoopState,
    game_log: GameLogWriter | None,
) -> bool:
    """Keep the game moving across request timeouts and reset repeated failures."""
    return await _handle_timeout_impl(
        session,
        state,
        game_log,
        logger=logger,
        llm_request_timeout_secs=LLM_REQUEST_TIMEOUT_SECS,
        max_consecutive_timeouts=MAX_CONSECUTIVE_TIMEOUTS,
    )


# Tools that are purely informational (don't advance game state).
# Used by stall detection to classify LLM turns.
INFO_ONLY_TOOLS = {"get_game_state", "get_oracle_text", "send_chat_message"}


def assemble_system_prompt(base: str, deck_path) -> str:
    """Join the base prompt to the own-deck card text THE WAY TRAINING JOINS THEM.

    render_conversations.py:369-371 builds `f"{system}\n\n{block}"` and leaves the
    system prompt alone when the block is empty. Both halves matter: a different
    separator, or emitting an empty block as a trailing blank line, makes every
    served prompt differ from every rendered one by characters no reader would
    notice and no comparison would forgive.

    Extracted from main() so the join can be tested without starting a game -- the
    byte-identity gate is on this function.
    """
    block, _covered = build_deck_block(deck_path)
    return f"{base}\n\n{block}" if block else base


def _load_default_system_prompt() -> str:
    """Load the default system prompt from prompts.json."""
    prompts = load_prompts(None)
    assert "default" in prompts, "prompts.json must contain a 'default' key"
    return prompts["default"]


def close_segment_if_needed(
    state: PilotLoopState,
    system_prompt: str,
    game_log: GameLogWriter | None = None,
) -> bool:
    """Cut the conversation at a decision boundary, the way training cuts it.

    A game long enough to outgrow the context gets SPLIT at training, never
    dropped: each segment repeats the system prompt and the deck block, resets
    `seen` so a card first revealed before the boundary is revealed again after
    it, and re-renders the crossing decision as the first of the next segment.
    Inference does the same thing here, by the same rule, from the same module.

    The alternative was to keep letting the serving engine refuse the request
    and reset reactively. That reset is a DIFFERENT boundary: it lands wherever
    the token ceiling happens to fall, restarts with a "the conversation was
    reset" preamble that appears nowhere in training, and keeps the header
    index counting. Every one of those is a conditioning the model never saw.
    The reactive path stays as the backstop -- the character budget is an
    estimate and the engine's refusal is not -- but it should now be unreachable
    in a game the budget bounds.

    Returns True if a cut happened.
    """
    blob = state.pending_decision_blob
    if blob is None:
        return False
    # Consumed whether or not it cuts: one decision, one check. Leaving it set
    # would re-price the same decision on every LLM call of a stalled turn,
    # where several calls answer one decision.
    state.pending_decision_blob = None
    pending_chars = state.pending_decision_chars

    if context_window_mode() != "full":
        # The windowed arm bounds the prompt by construction, and cutting it
        # here would put a boundary in the reference arm that the arm it is
        # being compared against does not have.
        return False

    # The characters that are actually in the prompt, not a model of them: the
    # system prompt (which carries the deck block when there is one, so a cut
    # repeats it automatically) plus every message in history. Training's `used`
    # is the same quantity computed over what IT emits. Where the two
    # conversations coincide -- one call per decision, no tool errors -- the two
    # numbers coincide and the boundary lands in the same place. Where inference
    # spent extra turns on a decision its prompt really is larger, and cutting
    # earlier for it is right rather than a discrepancy.
    used_after = len(system_prompt) + prompt_char_count(state.history)
    used_before = used_after - pending_chars

    if not should_close_segment(
        used_chars=used_before,
        pending_cost_chars=pending_chars + PENDING_ANSWER_RESERVE_CHARS,
        decisions_in_segment=state.segment_decisions,
        budget_chars=segment_budget_chars(),
        max_decisions=SEGMENT_MAX_DECISIONS,
    ):
        state.segment_decisions += 1
        return False

    state.seen_oracle_cards.clear()
    state.last_board = None
    text, state.last_board = render_for_pilot(blob, None, state.seen_oracle_cards, 0)
    # A `user` message, not a `tool` message. The tool message it replaces was
    # the answer to an assistant tool call that is being dropped with the rest
    # of history, and a tool result whose call is gone is a 400 from the server.
    # Training writes this decision as a user message too, so the replacement is
    # also what makes the two renderings match.
    state.history = [{"role": "user", "content": text}]
    # The header restarts at 0 exactly as the assembler's decision_index does.
    # It was rendered above with index 0; the decision it names is this one, so
    # the count standing at 1 afterwards is the same off-by-one the append site
    # keeps.
    state.decisions_seen = 1
    state.segment_decisions = 1
    state.segment_index += 1
    reset_render_cache(state)
    logger.info(
        "[pilot] context segment %d closed at the training budget; "
        "the crossing decision reopens as decision 0",
        state.segment_index,
    )
    if game_log:
        game_log.emit(
            "context_reset",
            cause="segment_boundary",
            harness_action=True,
            reset_index=state.segment_index,
        )
    return True


async def _auto_resolve_forced_decision(
    session: ClientSession,
    state: PilotLoopState,
    game_log: GameLogWriter | None,
) -> bool:
    """Answer a decision that offers nothing, without calling the policy.

    Returns True if it answered, so the caller skips this turn's LLM call.

    THE DECISION INDEX STILL COUNTS IT, deliberately. `decisions_seen` advances
    here exactly as it does for a decision the policy answers, so "[Decision N]"
    keeps meaning "the Nth decision in this game" rather than "the Nth decision
    the policy was asked about". Two reasons: the index is what the recorder's
    stream and the rendered transcript are joined on, and a renumbering would
    make every corpus collected before this epoch disagree with every one after
    it on which decision is which. The share of decisions the policy sees is a
    thing to report, not a thing to hide by renumbering.
    """
    blob = state.pending_decision_blob
    if blob is None or not auto_resolve_enabled():
        return False
    try:
        data = json.loads(blob)
    except (json.JSONDecodeError, TypeError):
        return False
    if not is_forced_decision(data):
        return False

    if game_log:
        game_log.emit(
            "auto_resolved",
            harness_action=True,
            decision_index=state.decisions_seen,
            game_seq=state.last_decision_seq,
            # 26 of 310,980 zero-choice decisions in block 2 answer with a play
            # despite offering none. Passing them is accepted at that rate; this
            # flag is how the audit counts what it cost instead of inferring it.
            chose_unoffered_risk=chose_unoffered(data, FORCED_ANSWER),
            # WHAT WAS SKIPPED, so a later audit can classify it. Without these the
            # decision is answered and its frame is never written anywhere -- not the
            # transcript, not game_events, not the bridge log -- so "every skipped
            # decision was a priority window" becomes unanswerable from the artifacts.
            # Validation job 1979 could not check it: 1,531 skips, 1,172 unclassifiable.
            # Three small fields rather than the whole frame: the frame is what we are
            # deliberately not paying for, and these are what a check needs.
            skipped_action_type=data.get("action_type"),
            skipped_response_type=data.get("response_type"),
            skipped_message=data.get("message"),
        )

    # A BRIDGE THAT HAS ALREADY TORN DOWN IS NOT A FATAL ERROR HERE, and letting it
    # propagate is what made this flag look like a 2.3x net LOSS in usable games.
    #
    # Measured on job 1979, same seeds, only this knob differing: "Bridge processor is shut
    # down" reaches errors.log in 14 of 20 games with the flag ON and 0 of 20 with it OFF --
    # while the underlying event occurs in 18 of 20 OFF games too. So the flag never caused
    # the race; it changed where the event was ROUTED. Unhandled here it becomes
    # "[pilot] Fatal tool error" in errors.log, and collect.py:315 drops any game with a
    # non-empty errors.log, so 70% of a flag-ON corpus would be discarded AFTER being played.
    #
    # `auto_pass.py:62` already treats exactly this as an infrastructure event and exits its
    # loop on a warning. This path is the one place that did not, which is the whole delta
    # between the arms. The event is still RECORDED -- game_log gets it, as it does in the
    # OFF arm's transcripts -- it is simply not classified as fatal.
    try:
        result_text = await execute_tool(session, "choose_action", dict(FORCED_ANSWER))
    except ToolExecutionError as exc:
        if game_log:
            game_log.emit(
                "auto_resolve_teardown",
                harness_action=True,
                decision_index=state.decisions_seen,
                game_seq=state.last_decision_seq,
                error_message=str(exc)[:500],
            )
        # No `username` in this function's scope -- a NameError here would fire only in the
        # error path, i.e. only once something else had already gone wrong.
        logger.warning("bridge gone while auto-answering a forced decision, "
                       "ending the game loop: %s", exc)
        return True
    record_decision_seq(state, result_text)
    # THE FORCED DECISION'S OWN LINE IS ALREADY IN HISTORY. _process_tool_calls
    # rendered and appended it before stashing the blob, and render_for_pilot
    # reduced it to one line there -- so appending here would duplicate it.
    # What this adds is the NEXT decision, the one the tool call returned.
    display_text, state.last_board = render_for_pilot(
        result_text, state.last_board, state.seen_oracle_cards, state.decisions_seen
    )
    if _carries_a_decision(result_text):
        state.decisions_seen += 1
    state.history.append({"role": "user", "content": display_text})
    state.pending_decision_blob = result_text if _carries_a_decision(result_text) else None
    state.pending_decision_chars = len(display_text)
    return True


async def _answer_mulligan_from_the_engine_rule(
    session: ClientSession,
    state: PilotLoopState,
    game_log: GameLogWriter | None,
) -> bool:
    """Answer a pending mulligan with the engine's rule, without calling the policy.

    Returns True if it answered, so the caller skips this turn's LLM call.

    WHY THE HARNESS ANSWERS. The corpus records mulligans and excludes them from
    training (build_dataset.TRAIN_EXCLUDED_KINDS), so a policy asked to answer one
    at inference is being asked something it was never shown. Answering it here
    with ComputerPlayer.chooseMulligan's own rule makes inference match training
    by removing the question rather than by supervising it.

    VALIDATED, not asserted: the rule agrees with the engine on 745 of 745
    recorded mulligan decisions across 290 games, over all five reachable
    (hand size, answer) cells. Three deliberately wrong rules were run against
    the same set as controls; two were caught. The third -- the threshold written
    as a literal 5 rather than size-2 -- agrees on all 745, because a flooded
    mulligan never occurred below a seven-card hand, so that distinction is
    covered by a constructed case in test_mulligan_rule.py instead.
    """
    blob = state.pending_decision_blob
    if blob is None or mulligan_mode() != "engine-rule":
        return False
    try:
        data = json.loads(blob)
    except (json.JSONDecodeError, TypeError):
        return False
    if not is_mulligan_decision(data):
        return False

    hand = data["your_hand"]
    choice = mulligan_choice(hand)
    lands = count_lands(hand)
    logger.info(
        "[pilot] mulligan answered from the engine rule: hand=%d lands=%d -> %s",
        len(hand), lands, "mulligan" if choice == "yes" else "keep",
    )
    if game_log:
        # AUDITABLE, and it has to carry the inputs rather than just the answer:
        # "keep" alone cannot be checked against the rule afterwards, and this is
        # a decision no LLM call will appear for in the trace.
        game_log.emit(
            "mulligan_auto",
            harness_action=True,
            hand=[c.get("name") for c in hand],
            hand_size=len(hand),
            lands=lands,
            choice=choice,
            decision="mulligan" if choice == "yes" else "keep",
        )

    result_text = await execute_tool(session, "choose_action", {"choice": choice})
    record_decision_seq(state, result_text)
    # A USER message, for the same reason the segment cut writes one: there is no
    # assistant tool call to answer, because the policy was never asked.
    display_text, state.last_board = render_for_pilot(
        result_text, state.last_board, state.seen_oracle_cards, state.decisions_seen
    )
    state.decisions_seen += 1
    state.history.append({"role": "user", "content": display_text})
    state.pending_decision_blob = result_text if _carries_a_decision(result_text) else None
    state.pending_decision_chars = len(display_text)
    return True


async def _build_loop_messages(
    state: PilotLoopState,
    session: ClientSession,
    system_prompt: str,
    cache_control: dict | None,
    game_log: GameLogWriter | None = None,
) -> list[dict]:
    """Render the next LLM request from the current history."""
    close_segment_if_needed(state, system_prompt, game_log)
    if len(state.history) > CONTEXT_RECENT_COUNT:
        state.render_counter += 1
        if not state.state_summary or state.render_counter % RENDER_INTERVAL == 0:
            state.state_summary = await _fetch_state_summary(session)
            state.render_counter = 0
        messages = render_context(
            state.history, system_prompt, state.state_summary, cache_control,
            last_prompt_tokens=state.last_prompt_tokens,
            last_prompt_chars=state.last_prompt_chars,
        )
        state.cache_breakpoint_idx = _find_cache_breakpoint_idx(messages)
        return messages

    messages = render_context(
        state.history, system_prompt, state.state_summary, cache_control,
        last_prompt_tokens=state.last_prompt_tokens,
        last_prompt_chars=state.last_prompt_chars,
    )
    state.cache_breakpoint_idx = len(messages) - 1 if messages else None
    state.render_counter = 0
    return messages


def _mark_tail_cache_breakpoint(
    messages: list[dict],
    state: PilotLoopState,
    cache_control: dict | None,
) -> None:
    """Mark the end of the stable prompt prefix for providers that cache it."""
    if not cache_control or len(messages) <= 1:
        return

    tail_idx = state.cache_breakpoint_idx if state.cache_breakpoint_idx is not None else len(messages) - 1
    marked = _with_cache_control(messages[tail_idx], cache_control)
    if marked is not messages[tail_idx]:
        messages[tail_idx] = marked


def _build_assistant_tool_message(message: _AssistantMessageLike) -> dict:
    """Build a provider-safe assistant message from an SDK tool response."""
    assistant_msg: dict = {"role": "assistant", "content": message.content}
    if message.tool_calls:
        assistant_msg["tool_calls"] = [
            {
                "id": tool_call.id,
                "type": "function",
                "function": {
                    "name": tool_call.function.name,
                    "arguments": tool_call.function.arguments,
                },
            }
            for tool_call in message.tool_calls
        ]
    return assistant_msg


def _maybe_extract_result_dict(result_text: str) -> dict | None:
    """Parse a JSON tool result when it is a dict."""
    try:
        data = json.loads(result_text)
    except (json.JSONDecodeError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def _carries_a_decision(result_text: str) -> bool:
    """True if this tool result is a decision put to the policy.

    `action_pending` is the same field render_for_pilot gates its header on, so
    the counter advances exactly when a "[Decision N]" line is emitted.
    """
    try:
        data = json.loads(result_text)
    except (json.JSONDecodeError, TypeError):
        return False
    return isinstance(data, dict) and bool(data.get("action_pending"))


def _chat_prompts_enabled() -> bool:
    """Whether to nag the pilot to chat with its opponent.

    Chatting is a MageBench benchmark feature -- models play Magic *and* talk to their
    opponent -- and it is dead weight for RL data collection in two ways. Measured over
    one 215-decision game it was 7,084 characters, 7.5% of all tool output, and because
    the nag is appended to display_text it lands in state.history and is then re-sent on
    every subsequent decision under append-only rendering.

    It also tells the model, in the middle of a game decision, to do something that is
    not a game action -- a candidate explanation for the "N consecutive responses emitted
    no tool call" stalls that collect.py rejects whole games for.

    Set MAGEBENCH_CHAT_PROMPTS=0 to suppress. Default on, so the benchmark keeps its
    behaviour and only training runs opt out.
    """
    return os.environ.get("MAGEBENCH_CHAT_PROMPTS") != "0"

async def _process_tool_calls(
    session: ClientSession,
    choice: _ChoiceLike,
    state: PilotLoopState,
    username: str,
    game_dir: Path | None,
    game_log: GameLogWriter | None,
    tool_calls: list | None = None,
) -> tuple[bool, set[str]]:
    """Execute a single LLM tool-calling turn.

    `tool_calls` overrides the response's own list, for a call the model emitted
    without its <tool_call> envelope. Same execution path deliberately: a
    recovered call must be indistinguishable downstream from a parsed one.
    """
    turn_state = PilotTurnState()

    if choice.message.content:
        logger.info("[pilot] Thinking: %s", choice.message.content)
    state.empty_responses = 0
    state.last_was_empty = False
    state.history.append(_build_assistant_tool_message(choice.message))

    calls = tool_calls if tool_calls is not None else choice.message.tool_calls
    assert calls is not None, "expected tool_calls in LLM response"
    for tool_call in calls:
        fn = tool_call.function
        args = json.loads(fn.arguments) if fn.arguments else {}

        state.board_tracker.inject(fn.name, args)
        logger.info("[pilot] Tool: %s(%s)", fn.name, json.dumps(args, separators=(",", ":")))

        if fn.name == "send_chat_message" and turn_state.chat_messages_this_turn >= MAX_CHAT_MESSAGES_PER_TURN:
            result_text = json.dumps({"success": False, "error": "Chat limit reached — focus on gameplay."})
            tool_latency_ms = 0
        else:
            if fn.name == "send_chat_message":
                turn_state.chat_messages_this_turn += 1
            tool_start = time.monotonic()
            try:
                result_text = await execute_tool(session, fn.name, args)
            except ToolExecutionError as exc:
                tool_latency_ms = int((time.monotonic() - tool_start) * 1000)
                if game_log:
                    game_log.emit(
                        "tool_call",
                        call_id=tool_call.id,
                        tool=fn.name,
                        arguments=args,
                        result=_tool_execution_error_result(exc, state.last_game_seq),
                        latency_ms=tool_latency_ms,
                        game_seq=state.last_game_seq,
                    )
                raise
            tool_latency_ms = int((time.monotonic() - tool_start) * 1000)

        result_data = _maybe_extract_result_dict(result_text)
        if result_data and "game_seq" in result_data:
            state.last_game_seq = result_data["game_seq"]
        state.board_tracker.extract(result_text)

        if game_log:
            game_log.emit(
                "tool_call",
                call_id=tool_call.id,
                tool=fn.name,
                arguments=args,
                result=result_text,
                latency_ms=tool_latency_ms,
                game_seq=state.last_game_seq,
            )

        if result_text == '{"error": ""}':
            state.consecutive_empty_errors += 1
            if state.consecutive_empty_errors >= MAX_CONSECUTIVE_EMPTY_ERRORS:
                log_error(
                    logger,
                    game_dir,
                    username,
                    f"[pilot] {state.consecutive_empty_errors} consecutive empty errors — bridge is dead, exiting",
                )
                if game_log:
                    game_log.emit(
                        "auto_pilot_mode",
                        reason="bridge_dead",
                        consecutive_empty_errors=state.consecutive_empty_errors,
                    )
                return True, turn_state.tools_called
        else:
            state.consecutive_empty_errors = 0

        turn_state.tools_called.add(fn.name)
        if fn.name == "choose_action":
            choice_result = json.loads(result_text)
            action_taken = choice_result.get("action_taken")
            success = choice_result.get("success", False)
            if success:
                logger.info("[pilot] Action: %s", action_taken)
                turn_state.had_successful_action = True
                state.turns_without_progress = 0
            else:
                logger.warning("[pilot] Action failed: %s", choice_result.get("error"))
                turn_state.had_actionable_opportunity = True
        elif fn.name == "get_action_choices":
            choice_result = json.loads(result_text)
            action_type = choice_result.get("action_type")
            message = choice_result.get("message")
            choices = choice_result.get("choices")
            if choice_result.get("error"):
                turn_state.had_actionable_opportunity = True
            elif choices:
                logger.info("[pilot] Choices for %s: %d options", action_type, len(choices))
                turn_state.had_actionable_opportunity = True
            else:
                logger.info(
                    "[pilot] Action: %s - %s",
                    action_type,
                    message[:100] if message else "",
                )
        elif fn.name == "pass_priority":
            try:
                pass_result = json.loads(result_text)
                context = pass_result.get("context")
                if context and context.startswith("T"):
                    try:
                        state.current_game_turn = int(context[1:].split()[0])
                    except (ValueError, IndexError):
                        pass
                if pass_result.get("action_pending"):
                    turn_state.had_actionable_opportunity = True
                    state.consecutive_pass_errors = 0
                    state.last_pass_error_msg = ""
                if pass_result.get("error"):
                    turn_state.had_actionable_opportunity = True
                    err_msg = pass_result["error"]
                    if err_msg == state.last_pass_error_msg:
                        state.consecutive_pass_errors += 1
                    else:
                        state.consecutive_pass_errors = 1
                        state.last_pass_error_msg = err_msg
                    if state.consecutive_pass_errors >= MAX_CONSECUTIVE_PASS_ERRORS:
                        logger.warning(
                            "[pilot] %d consecutive identical pass_priority errors, forcing plain pass",
                            state.consecutive_pass_errors,
                        )
                        if game_log:
                            game_log.emit(
                                "forced_pass",
                                reason="repeated_pass_error",
                                error=err_msg,
                                count=state.consecutive_pass_errors,
                            )
                        result_text = await execute_tool(session, "pass_priority", {})
                        state.consecutive_pass_errors = 0
                        state.last_pass_error_msg = ""
                else:
                    state.consecutive_pass_errors = 0
                    state.last_pass_error_msg = ""
            except (json.JSONDecodeError, TypeError):
                pass

        if fn.name == "send_chat_message":
            state.last_chat_turn = state.current_game_turn

        result_data = _maybe_extract_result_dict(result_text)
        if result_data:
            if result_data.get("game_over"):
                logger.info(
                    "[pilot] Game over detected from %s, switching to auto-pass",
                    fn.name,
                )
                if game_log:
                    game_log.emit("auto_pilot_mode", reason="game_over")
                await auto_pass_loop(session, "pilot")
                return True, turn_state.tools_called
            if result_data.get("player_dead"):
                logger.info(
                    "[pilot] Player dead detected from %s, switching to auto-pass",
                    fn.name,
                )
                if game_log:
                    game_log.emit("auto_pilot_mode", reason="player_dead")
                await auto_pass_loop(session, "pilot")
                return True, turn_state.tools_called

        display_text = result_text
        decision_blob: str | None = None
        if fn.name in ("pass_priority", "get_action_choices", "choose_action"):
            # Stamp the decision seq HERE, from the same result_text that is about to be
            # rendered into history -- not from the capture at :378, which happens BEFORE
            # the forced-pass path at :474 can rebind result_text. Reading it there would
            # label the prompt with decision N while the prompt shows decision N+1.
            # (The forced-pass path has never fired: 0 forced_pass events across 688
            # recorded game dirs. The same counter finds stall=79 and context_reset=4, so
            # the zero is a real negative, not a broken check. Closing it by construction
            # costs nothing.)
            record_decision_seq(state, result_text)
            display_text, state.last_board = render_for_pilot(
                result_text, state.last_board, state.seen_oracle_cards, state.decisions_seen
            )
            # Advance only when this result WAS a decision. Asked of the DATA, not
            # inferred from "the rendered text differs from the input" -- that proxy
            # is true today only because the renderer happens to return its input
            # unchanged in exactly the two non-decision cases, and it would go quietly
            # wrong the day the renderer decorates anything else.
            if _carries_a_decision(result_text):
                state.decisions_seen += 1
                # Only a decision that was actually RENDERED delimits a segment,
                # which is why this is set inside the branch that renders rather
                # than beside the history append below. A result carrying
                # action_pending from some other tool would be stashed there and
                # re-rendered at a cut into text the game never showed.
                decision_blob = result_text
            turns_since_chat = state.current_game_turn - state.last_chat_turn
            chat_budget_left = turn_state.chat_messages_this_turn < MAX_CHAT_MESSAGES_PER_TURN
            if _chat_prompts_enabled() and turns_since_chat >= 2 and display_text != result_text and chat_budget_left:
                display_text += (
                    f"\n\n[It's been {turns_since_chat} turns since you last "
                    f"chatted — send a message to your opponent!]"
                )

        state.history.append(
            {
                "role": "tool",
                "tool_call_id": tool_call.id,
                "content": display_text,
            }
        )
        if decision_blob is not None:
            # Hand the segment check the raw blob and what it cost, so the cut
            # can re-render this decision from a clean board if it turns out to
            # start the next segment. Priced HERE, after the append, because
            # display_text may have picked up the chat nag since it was
            # rendered, and the budget has to count the characters that were
            # actually sent. Recorded here because this is the only place that
            # holds the blob; ACTED ON in _build_loop_messages, where history is
            # between turns and consistent -- cutting from inside this loop
            # would drop the assistant message whose tool_call_id this result
            # still names.
            state.pending_decision_blob = decision_blob
            state.pending_decision_chars = len(display_text)

    if not turn_state.had_successful_action and (
        turn_state.had_actionable_opportunity
        or not turn_state.tools_called
        or turn_state.tools_called <= INFO_ONLY_TOOLS
    ):
        state.turns_without_progress += 1
    return False, turn_state.tools_called


def build_initial_message(pass_priority_result: dict) -> str:
    """Build the initial user message from a pass_priority result."""
    if pass_priority_result.get("game_over"):
        return "The game is over."
    if not pass_priority_result.get("action_pending"):
        return "The game is starting. Call pass_priority to get your first decision."

    action_type = pass_priority_result.get("action_type")
    message = pass_priority_result.get("message")

    if message and ("Mulligan" in message or "mulligan" in message.lower()):
        return (
            f"The game is starting. Your first decision: {message}\n"
            "Call get_action_choices to see your hand, then choose_action to decide."
        )
    if action_type:
        return (
            f"The game is starting. Your first decision ({action_type}): {message if message else ''}\n"
            "Call get_action_choices to see your options, then choose_action to decide."
        )
    return "The game is starting. Call pass_priority to get your first decision."


async def _prefetch_first_action(session: ClientSession) -> tuple[str, int | None, str]:
    """Wait for the first game decision; return its message, the decision's seq and its raw blob.

    The seq is returned rather than discarded because without it the FIRST trace row of
    every game carries no decision identity -- measured, exactly one unstamped row per
    game in 6 of 6 games. The initial message describes a decision that is already
    pending, and the seq is already in this result, so the gap is pure loss.

    The RAW BLOB is returned for the same reason one layer on: the mulligan is the
    first decision of every game, and answering it from the engine's rule needs the
    hand, which only the blob carries. build_initial_message reduces it to prose.
    """
    result_text = await execute_tool(session, "pass_priority", {})
    try:
        result = json.loads(result_text)
    except (json.JSONDecodeError, TypeError):
        return ("The game is starting. Call pass_priority to get your first decision.",
                None, result_text)
    seq = result.get("game_seq") if isinstance(result, dict) else None
    return build_initial_message(result), seq if isinstance(seq, int) else None, result_text


async def run_pilot_loop(
    session: ClientSession,
    client: AsyncOpenAI,
    model: str,
    system_prompt: str,
    tools: list[dict],
    prices: dict[str, tuple[float, float]],
    username: str = "",
    game_dir: Path | None = None,
    game_log: GameLogWriter | None = None,
    trace_log: GameLogWriter | None = None,
    reasoning_effort: str = "",
    ignore_providers: list[str] | None = None,
    provider_order: list[str] | None = None,
    cache_control: dict | None = None,
    *,
    capture_token_ids: bool = False,
    decision_identity: bool = False,
) -> None:
    """Run the LLM-driven game-playing loop.

    `capture_token_ids` asks the serving engine to return the exact prompt and
    completion token ids for every call, for RL rollouts. It is vLLM-specific
    (`return_token_ids` / `return_prompt_text`) and must stay off for providers that
    reject unknown request fields.

    `decision_identity` adds the decision's seq to the request body so a serving-side
    brain can join it to the engine hint stream. Same shape and same reason as
    `capture_token_ids`; see DECISION_IDENTITY. Off means the request is unchanged, not
    changed-but-empty -- the field is never added.
    """
    # Names of the tools this game actually offers. The unwrapped-tool-call recovery
    # below requires a match here, so prose that merely mentions a tool name cannot
    # be mistaken for a call.
    _toolset_names = {
        tool["function"]["name"] for tool in tools
        if isinstance(tool, dict) and isinstance(tool.get("function"), dict)
    }
    try:
        initial_message, first_decision_seq, first_blob = await _prefetch_first_action(session)
    except ToolExecutionError as exc:
        _record_tool_execution_failure(exc, username, game_dir, game_log)
        raise
    state = PilotLoopState(history=[{"role": "user", "content": initial_message}])
    state.last_decision_seq = first_decision_seq
    # The first decision is pending before the loop starts, so stash it the way
    # _process_tool_calls stashes every later one. Safe for the segment check as
    # well: a cut cannot fire with zero decisions in the segment.
    state.pending_decision_blob = first_blob
    state.pending_decision_chars = len(initial_message)
    model_price = get_model_price(model, prices)
    game_start = time.monotonic()

    while True:
        if time.monotonic() - game_start > MAX_GAME_DURATION_SECS:
            logger.warning("[pilot] Maximum game duration exceeded, switching to auto-pass")
            if game_log:
                game_log.emit("auto_pilot_mode", reason="max_duration_exceeded")
            await auto_pass_loop(session, "pilot")
            return
        try:
            # Before anything is rendered or sent: a mulligan is answered by the
            # engine's own rule and never reaches the policy. Costs one tool call
            # instead of an LLM round trip, and keeps the decision out of a
            # context the model is not trained to answer.
            if await _answer_mulligan_from_the_engine_rule(session, state, game_log):
                continue
            # After the mulligan check, never before it: a mulligan ask carries no
            # `choices` either, and is a real decision.
            if await _auto_resolve_forced_decision(session, state, game_log):
                continue
            messages = await _build_loop_messages(
                state, session, system_prompt, cache_control, game_log
            )
            _mark_tail_cache_breakpoint(messages, state, cache_control)

            create_kwargs: dict = {
                "model": model,
                "messages": messages,
                "tools": tools,
                "tool_choice": "auto",
                "max_tokens": MAX_TOKENS,
            }
            # Sending no temperature means the SERVER decides, and vLLM falls back to
            # the model's generation_config.json. For Qwen3-4B that is temperature 0.6
            # with top_k 20 -- measured effectively deterministic: 16 of 16 identical
            # completions on the same prompt. A policy-gradient method needs the policy
            # to explore; with a deterministic policy the advantage can only reweight
            # actions it already takes, and every rollout in a group differs only by
            # what it was dealt. Set it explicitly, so it is recorded in the trace and
            # cannot change under us when a model's config file does.
            if os.environ.get("MAGEBENCH_TEMPERATURE"):
                create_kwargs["temperature"] = float(os.environ["MAGEBENCH_TEMPERATURE"])
            extra_body: dict = {}
            # Qwen3 and friends default to thinking mode in their chat template, which spends
            # ~800-1800 completion tokens per decision — most of them to decide to pass. A
            # served reasoning-parser strips the trace from the response but does not stop it
            # being generated, so the cost is invisible in the logs and real on the clock.
            if os.environ.get("MAGEBENCH_DISABLE_THINKING") == "1":
                extra_body["chat_template_kwargs"] = {"enable_thinking": False}
            if capture_token_ids:
                # RL rollouts train on these. Ask the serving engine for the exact token
                # sequence it prompted with and sampled, so the training path never has to
                # re-tokenize a re-rendered transcript. Re-rendering is not an identity here:
                # the chat template rebuilds history each turn, vLLM's hermes parser rewrites
                # tool-call arguments through json.dumps, and tool_call ids are regenerated
                # per response. Re-tokenizing any of that yields a sequence the policy never
                # produced, and the resulting GRPO importance ratio is silently wrong.
                #
                # WARNING: both of these are silently nulled if `include_reasoning` is False.
                # vLLM computes `suppress_metadata = not include_reasoning and parser is not
                # None` and uses it to gate token_ids and logprobs off the response
                # (entrypoints/openai/chat_completion/serving.py:899,1003). A parser IS active
                # here (--tool-call-parser hermes, --reasoning-parser qwen3), so leaving
                # include_reasoning at its default True is load-bearing. Thinking is disabled
                # via chat_template_kwargs above, NOT via include_reasoning -- do not "simplify"
                # those into one another or the training signal disappears with no error.
                extra_body["return_token_ids"] = True
                # AUDIT ONLY. Never feed prompt_text to the trainer: it is the rendered string,
                # and tokenizing it is exactly the round trip return_token_ids exists to avoid.
                extra_body["return_prompt_text"] = True
            if reasoning_effort:
                extra_body["reasoning"] = {"effort": reasoning_effort}
            if ignore_providers or provider_order:
                provider_cfg: dict = {}
                if ignore_providers:
                    provider_cfg["ignore"] = ignore_providers
                if provider_order:
                    provider_cfg["order"] = provider_order
                extra_body["provider"] = provider_cfg
            if decision_identity and state.last_decision_seq is not None:
                # Under a namespaced key so it cannot collide with a provider extension.
                # Note the byte-identity argument is structural, not a promise about this
                # block: when the flag is off nothing is added, so on a run with no other
                # extras `extra_body` stays empty and the `if extra_body:` below leaves
                # the key off create_kwargs entirely.
                extra_body["magebench_decision"] = {"game_seq": state.last_decision_seq}
            if extra_body:
                create_kwargs["extra_body"] = extra_body
            response = await asyncio.wait_for(
                client.chat.completions.create(**create_kwargs),
                timeout=LLM_REQUEST_TIMEOUT_SECS,
            )
            state.consecutive_timeouts = 0
            if capture_token_ids:
                # Fires the moment a tokenizer or chat template shifts under us, which would
                # otherwise show up only as a quietly wrong policy six weeks into training.
                prompt_token_ids = response.prompt_token_ids
                assert prompt_token_ids is not None, (
                    "return_token_ids was requested but the server returned no prompt_token_ids; "
                    "check that include_reasoning is not set to False"
                )
                assert response.usage is not None, "expected usage alongside prompt_token_ids"
                assert len(prompt_token_ids) == response.usage.prompt_tokens, (
                    f"prompt_token_ids length {len(prompt_token_ids)} != "
                    f"usage.prompt_tokens {response.usage.prompt_tokens}"
                )
            if not response.choices:
                state.consecutive_empty_choices += 1
                logger.warning(
                    "[pilot] LLM returned empty/null choices, retrying... [%d]",
                    state.consecutive_empty_choices,
                )
                if state.consecutive_empty_choices >= MAX_CONSECUTIVE_EMPTY_CHOICES:
                    logger.warning("[pilot] LLM returning empty choices repeatedly, switching to auto-pass mode")
                    if game_log:
                        game_log.emit(
                            "auto_pilot_mode",
                            reason=f"LLM degraded ({state.consecutive_empty_choices} consecutive empty choices)",
                        )
                    try:
                        await execute_tool(
                            session,
                            "send_chat_message",
                            {"message": "My brain is fried... going on autopilot for the rest of this game. GG!"},
                        )
                    except ToolExecutionError:
                        pass
                    await auto_pass_loop(session, "pilot")
                    return
                continue
            state.consecutive_empty_choices = 0
            choice = response.choices[0]
            if _handle_truncated_response(state, choice, response, game_log):
                continue

            if trace_log:
                # Which DECISION this call is about. The writer's own `seq` is a dense
                # per-file counter over policy calls; the server's decision stream is
                # sparse and global, and the two differ in both directions (measured
                # decisions-published vs trace rows on six games: 83/67, 153/116, 138/154,
                # 103/75, 106/85, 158/127). Without this the trace cannot be joined to the
                # decision it labels at all.
                #
                # UNCONDITIONAL, and separate from the generation flag below: this is an
                # observability field on a file nothing forwards to the server. It is safe
                # on `llm_trace` specifically -- merge_game_log sorts game.jsonl by
                # `game_seq` and its glob is `*_llm.jsonl`, which does not match
                # `*_llm_trace.jsonl`, so no merge ordering moves.
                trace_log.emit(
                    "llm_call",
                    request=create_kwargs,
                    response=response.model_dump(),
                    game_seq=state.last_decision_seq,
                )

            # Anchor for the append-only context guard: the engine's OWN token count
            # for the prompt we just sent, paired with that prompt's character count.
            # Without this the guard falls back to estimating the whole prompt from
            # characters, which is deck-dependent and was wrong by 18% on Azorius.
            if response.usage and response.usage.prompt_tokens:
                state.last_prompt_tokens = response.usage.prompt_tokens
                state.last_prompt_chars = prompt_char_count(messages)

            call_cost = 0.0
            if response.usage and model_price is not None:
                input_cost = (response.usage.prompt_tokens or 0) * model_price[0] / 1_000_000
                output_cost = (response.usage.completion_tokens or 0) * model_price[1] / 1_000_000
                call_cost = input_cost + output_cost
                state.cumulative_cost += call_cost
                if game_dir:
                    write_cost_file(game_dir, username, state.cumulative_cost)

            if game_log:
                llm_event = {"reasoning": choice.message.content}
                thinking = getattr(choice.message, "reasoning_content", None)
                if thinking:
                    llm_event["thinking"] = thinking
                if choice.message.tool_calls:
                    llm_event["tool_calls"] = [
                        {
                            "name": tool_call.function.name,
                            "arguments": tool_call.function.arguments,
                        }
                        for tool_call in choice.message.tool_calls
                    ]
                if response.usage:
                    usage_dict: dict = {
                        "prompt_tokens": response.usage.prompt_tokens or 0,
                        "completion_tokens": response.usage.completion_tokens or 0,
                    }
                    prompt_details = response.usage.prompt_tokens_details
                    if prompt_details and getattr(prompt_details, "cached_tokens", None):
                        usage_dict["cached_tokens"] = prompt_details.cached_tokens
                        total_prompt = response.usage.prompt_tokens or 0
                        if prompt_details.cached_tokens > total_prompt > 0:
                            logger.warning(
                                "[pilot] cached_tokens (%d) > prompt_tokens (%d) — upstream API bug",
                                prompt_details.cached_tokens,
                                total_prompt,
                            )
                        elif total_prompt > 0:
                            hit_pct = prompt_details.cached_tokens / total_prompt * 100
                            logger.debug(
                                "[pilot] Cache: %d/%d (%.0f%%)",
                                prompt_details.cached_tokens,
                                total_prompt,
                                hit_pct,
                            )
                    completion_details = response.usage.completion_tokens_details
                    if completion_details and getattr(completion_details, "reasoning_tokens", None):
                        usage_dict["reasoning_tokens"] = completion_details.reasoning_tokens
                    llm_event["usage"] = usage_dict
                llm_event["cost_usd"] = round(call_cost, 6)
                llm_event["cumulative_cost_usd"] = round(state.cumulative_cost, 6)
                if state.last_game_seq is not None:
                    llm_event["game_seq"] = state.last_game_seq
                game_log.emit("llm_response", **llm_event)

            turn_tools_called: set[str] = set()
            # A tool call the model wrote correctly but emitted without its
            # <tool_call> tags parses to nothing, and the branch below then tells it
            # to pass -- turning a correct decision into a pass on a main phase with
            # a land available. Measured at 1,142 of 41,970 decisions (2.7%) over 449 games.
            recovered = None
            if not choice.message.tool_calls:
                recovered = recover_unwrapped_tool_call(choice.message.content, _toolset_names)
                if recovered:
                    logger.warning(
                        "[pilot] Recovered an unwrapped tool call: %s. The model answered correctly "
                        "and the envelope was missing.",
                        recovered[0].function.name,
                    )
                    if game_log:
                        game_log.emit("unwrapped_tool_call", tool=recovered[0].function.name)

            if choice.message.tool_calls or recovered:
                finished, turn_tools_called = await _process_tool_calls(
                    session,
                    choice,
                    state,
                    username,
                    game_dir,
                    game_log,
                    tool_calls=recovered,
                )
                if finished:
                    return
            else:
                state.turns_without_progress += 1
                content = choice.message.content
                if content:
                    content = content.strip()
                if content:
                    logger.info("[pilot] Thinking: %s", content[:500])
                    state.history.append({"role": "assistant", "content": content})
                    state.empty_responses = 0
                    state.last_was_empty = False
                elif not state.last_was_empty:
                    logger.warning("[pilot] Empty response from LLM, retrying...")
                    state.last_was_empty = True
                    continue
                else:
                    state.last_was_empty = False
                    state.empty_responses += 1
                    logger.warning(
                        "[pilot] Empty response from LLM (no tools, no text) [%d]",
                        state.empty_responses,
                    )
                    if state.empty_responses >= MAX_EMPTY_RESPONSES:
                        logger.warning("[pilot] LLM appears degraded (no tools or text), switching to auto-pass mode")
                        if game_log:
                            game_log.emit(
                                "auto_pilot_mode",
                                reason="LLM degraded (10+ empty responses)",
                            )
                        try:
                            await execute_tool(
                                session,
                                "send_chat_message",
                                {"message": "My brain is fried... going on autopilot for the rest of this game. GG!"},
                            )
                        except ToolExecutionError:
                            pass
                        await auto_pass_loop(session, "pilot")
                        return
                state.history.append(
                    {
                        "role": "user",
                        # NOT "call pass_priority". This nudge fires when a turn produced
                        # no tool call, and naming a specific action makes the model take
                        # THAT action -- so a formatting slip on a main phase with a land
                        # available became a pass, in every one of the 319 measured cases.
                        # The recovery was worse than the failure it recovered from.
                        "content": "Respond with a tool call.",
                    }
                )

            if state.turns_without_progress >= MAX_TURNS_WITHOUT_PROGRESS:
                if await _recover_from_stall(
                    session,
                    state,
                    game_log,
                    turn_tools_called,
                ):
                    return
                continue

        except TimeoutError:
            if await _handle_timeout(session, state, game_log):
                return

        except ToolExecutionError as exc:
            _record_tool_execution_failure(exc, username, game_dir, game_log)
            raise

        except OpenAIError as exc:
            state.consecutive_timeouts = 0
            error_str = str(exc)
            logger.warning("[pilot] LLM error: %s", exc)
            if game_log:
                game_log.emit(
                    "llm_error",
                    error_type=type(exc).__name__,
                    error_message=error_str[:500],
                )

            # ONE exception to the no-recovery rule below, and it is not the old one.
            #
            # A context overflow is not the policy failing -- it is the prompt having
            # outgrown the server, which append-only rendering makes inevitable in a long
            # enough game. Resetting shortens the prompt and the SAME decision is then put
            # back through the policy. No action is fabricated: that is the whole
            # difference from the path the comment below describes, which blind-fired
            # pass_priority and recorded a move nobody chose.
            #
            # This is only safe because a mid-game reset is now a first-class outcome:
            # the training layout segments on it and each segment reproduces the
            # conditioning its tokens were sampled under. Before 2026-08-17 this same fix
            # would have traded a loud failure for a silent one.
            #
            # Bounded, because if the post-reset prompt still overflows nothing has been
            # gained and retrying forever would hang the batch on the deadline.
            if is_context_overflow(error_str) and state.context_overflow_resets < MAX_CONTEXT_OVERFLOW_RESETS:
                state.context_overflow_resets += 1
                logger.warning(
                    "[pilot] context overflow, resetting conversation and retrying (%d/%d)",
                    state.context_overflow_resets,
                    MAX_CONTEXT_OVERFLOW_RESETS,
                )
                if game_log:
                    # The marker the stall path never had. Without it the trainer cannot
                    # tell a ceiling reset from a stall reset from ordinary history, and
                    # they do not mean the same thing about the policy.
                    game_log.emit(
                        "context_reset",
                        cause="context_overflow",
                        harness_action=True,
                        reset_index=state.context_overflow_resets,
                        error_message=error_str[:500],
                    )
                reset_context(
                    state,
                    "The conversation was reset because it grew past the context limit. "
                    "The game is unchanged. Call pass_priority to see the current state.",
                    reset_board_context=False,
                )
                continue

            # Any LLM error aborts the game. There is deliberately no recovery path.
            #
            # This used to blind-fire pass_priority and reset_context for every error that was
            # not a 401/402/403/404, then keep playing. That records an action the policy never
            # chose, as though it had, with no marker at any level: the game finishes, the batch
            # summary is clean, and the trajectory is silently mislabelled. It fired in practice
            # on context-overflow 400s and, worse, on APIConnectionError -- one game reached
            # GAME_OVER having made zero LLM calls, played entirely by this path.
            #
            # For RL data a fabricated action is worse than a missing one: losing a rollout costs
            # one episode, poisoning one corrupts the gradient and is invisible downstream.
            # Fail-fast is also what AGENTS.md requires; the old branch was a graceful fallback
            # that continued with degraded behaviour.
            reason = _classify_permanent_llm_failure(error_str) or f"LLM error ({type(exc).__name__})"
            logger.error("[pilot] %s, aborting game", reason)
            if game_log:
                game_log.emit(
                    "permanent_llm_failure",
                    reason=reason,
                    error_type=type(exc).__name__,
                    error_message=error_str[:500],
                )
            _mark_game_aborted(game_dir, username, reason, type(exc).__name__)
            try:
                await execute_tool(
                    session,
                    "send_chat_message",
                    {"message": f"{reason}... aborting game. GG!"},
                )
            except ToolExecutionError:
                pass
            raise PermanentLLMError(reason) from None


async def run_pilot(
    server: str,
    port: int,
    username: str,
    project_root: Path,
    prices: dict[str, tuple[float, float]],
    deck_path: Path | None = None,
    api_key: str = "",
    model: str = DEFAULT_MODEL,
    provider: str = DEFAULT_LLM_PROVIDER,
    system_prompt: str = "",
    game_dir: Path | None = None,
    table_id: str = "",
    max_interactions_per_turn: int | None = None,
    reasoning_effort: str = "",
    tools: set[str] | None = None,
    ignore_providers: list[str] | None = None,
    provider_order: list[str] | None = None,
    cache_control: dict | None = None,
) -> None:
    """Run the pilot client."""
    base_url = llm_base_url(provider)
    logger.info("[pilot] Starting for %s@%s:%s", username, server, port)
    logger.info("[pilot] Model: %s", model)
    logger.info("[pilot] Provider: %s", provider)
    if reasoning_effort:
        logger.info("[pilot] Reasoning effort: %s", reasoning_effort)
    if tools is not None:
        logger.info("[pilot] Custom toolset: %s", sorted(tools))
    if ignore_providers:
        logger.info("[pilot] Ignoring providers: %s", ignore_providers)
    if provider_order:
        logger.info("[pilot] Provider order: %s", provider_order)
    if cache_control:
        logger.debug("[pilot] Prompt cache_control: %s", cache_control)
    if provider != DEFAULT_LLM_PROVIDER:
        assert ignore_providers is None, (
            f"ignore_providers requires provider={DEFAULT_LLM_PROVIDER!r}, got {provider!r}"
        )
        assert provider_order is None, f"provider_order requires provider={DEFAULT_LLM_PROVIDER!r}, got {provider!r}"

    llm_client = AsyncOpenAI(
        api_key=api_key,
        base_url=base_url,
        timeout=LLM_REQUEST_TIMEOUT_SECS + 5,
        max_retries=1,
    )

    launch_args = build_bridge_launch_args(
        server=server,
        port=port,
        username=username,
        deck_path=deck_path,
        heap_size_mb=512,
        error_log_path=game_dir / f"{username}_errors.log" if game_dir else None,
        bridge_log_path=game_dir / f"{username}_bridge.jsonl" if game_dir else None,
        max_interactions_per_turn=max_interactions_per_turn,
        table_id=table_id or None,
    )

    logger.info("[pilot] Spawning bridge client...")

    game_log = None
    trace_log = None
    with ExitStack() as log_stack:
        if game_dir:
            game_log = log_stack.enter_context(GameLogWriter(game_dir, username))
            trace_log = log_stack.enter_context(GameLogWriter(game_dir, username, suffix="llm_trace"))

        try:
            async with spawn_bridge_http(
                mvn_args=launch_args.mvn_args,
                project_root=project_root,
                jvm_args=launch_args.jvm_args,
                log_file=game_dir / f"{username}_mcp.log" if game_dir else None,
            ) as session:
                result = await session.initialize()
                logger.debug("[pilot] MCP initialized: %s", result.serverInfo)

                tools_result = await session.list_tools()
                if tools is not None:
                    available_mcp_names = {tool.name for tool in tools_result.tools}
                    unknown = tools - available_mcp_names
                    if unknown:
                        raise ValueError(
                            f"Toolset references unknown MCP tools: {sorted(unknown)}. "
                            f"Available: {sorted(available_mcp_names)}"
                        )
                openai_tools = mcp_tools_to_openai(tools_result.tools, tools)
                tool_names = [tool["function"]["name"] for tool in openai_tools]
                logger.debug("[pilot] Available tools: %s", tool_names)

                if game_log:
                    game_log.emit(
                        "game_start",
                        model=model,
                        system_prompt=system_prompt,
                        available_tools=tool_names,
                        deck_path=str(deck_path) if deck_path else None,
                    )

                logger.info("[pilot] Starting game-playing loop...")
                await run_pilot_loop(
                    session,
                    llm_client,
                    model,
                    system_prompt,
                    openai_tools,
                    username=username,
                    game_dir=game_dir,
                    prices=prices,
                    game_log=game_log,
                    trace_log=trace_log,
                    reasoning_effort=reasoning_effort,
                    ignore_providers=ignore_providers,
                    provider_order=provider_order,
                    cache_control=cache_control,
                    capture_token_ids=CAPTURE_TOKEN_IDS,
                    decision_identity=DECISION_IDENTITY,
                )
        finally:
            if game_log:
                game_log.emit(
                    "game_end",
                    total_cost_usd=round(game_log.last_cumulative_cost_usd(), 6),
                )


def main() -> int:
    """Main entry point."""
    setup_logging()
    parser = argparse.ArgumentParser(description="Pilot LLM game player for XMage")
    parser.add_argument("--server", default="localhost", help="XMage server address")
    parser.add_argument("--port", type=int, default=17171, help="XMage server port")
    parser.add_argument("--username", default="Pilot", help="Player username")
    parser.add_argument("--project-root", type=Path, help="Project root directory")
    parser.add_argument("--deck", type=Path, help="Path to deck file (.dck)")
    parser.add_argument("--api-key", default="", help="API key (prefer provider-specific env vars)")
    parser.add_argument("--model", default=DEFAULT_MODEL, help=f"LLM model (default: {DEFAULT_MODEL})")
    parser.add_argument("--provider", choices=SUPPORTED_LLM_PROVIDERS, default=DEFAULT_LLM_PROVIDER)
    parser.add_argument("--system-prompt", default="", help="Custom system prompt")
    parser.add_argument(
        "--no-deck-block",
        action="store_true",
        help="Run without the own-deck card text. Diverges from the training prompt on purpose.",
    )
    parser.add_argument("--game-dir", type=Path, help="Game directory for cost file output")
    parser.add_argument(
        "--table-id",
        default="",
        help=(
            "Pin the bridge to this table. Without it the bridge joins the first WAITING "
            "table with an open seat, which is only correct while one table is open at a "
            "time -- i.e. while batch setup is serialised."
        ),
    )
    parser.add_argument(
        "--max-interactions-per-turn",
        type=int,
        help="Loop detection threshold (default 25)",
    )
    parser.add_argument(
        "--reasoning-effort",
        default="",
        help="OpenRouter reasoning effort: low, medium, high",
    )
    parser.add_argument("--tools", default="", help="Comma-separated MCP tool names (default: all)")
    parser.add_argument(
        "--ignore-providers",
        default="",
        help="Comma-separated OpenRouter providers to exclude",
    )
    parser.add_argument(
        "--provider-order",
        default="",
        help="Comma-separated OpenRouter providers to prefer, in order",
    )
    parser.add_argument(
        "--cache-control",
        default="",
        help="JSON cache_control config for prompt caching",
    )
    args = parser.parse_args()

    if args.project_root:
        project_root = args.project_root.resolve()
    else:
        project_root = Path.cwd().resolve()
        if project_root.name == "puppeteer" and project_root.parent.name == "src":
            project_root = project_root.parent.parent.parent
        elif project_root.name == "puppeteer":
            project_root = project_root.parent

    provider = args.provider or DEFAULT_LLM_PROVIDER

    api_key = args.api_key
    if not api_key.strip():
        required_key_env = required_api_key_env(provider)
        api_key = os.environ.get(required_key_env)
    if not api_key or not api_key.strip():
        logger.error("[pilot] Missing API key for provider %s", provider)
        logger.error("[pilot] Pass --api-key or export the provider's configured API key env var.")
        return 2

    prices = load_prices()
    logger.debug("[pilot] Project root: %s", project_root)

    system_prompt = args.system_prompt or _load_default_system_prompt()
    pilot_tools = set(args.tools.split(",")) if args.tools else None
    ignore_providers = args.ignore_providers.split(",") if args.ignore_providers else None
    provider_order = args.provider_order.split(",") if args.provider_order else None
    cache_control = json.loads(args.cache_control) if args.cache_control else None
    if DECISION_IDENTITY and provider != "local":
        # Fail HERE, before asyncio.run and before a single game starts. The field is a
        # vLLM/shim extension; OpenRouter, Anthropic and OpenAI reject unknown request
        # fields with a 400, and a 400 discovered on the first LLM call of a batch means
        # every game in that batch is played by the error handler.
        logger.error(
            "[pilot] MAGEBENCH_DECISION_IDENTITY=1 requires --provider=local "
            "(got %s). The magebench_decision passthrough is a vLLM/shim extension "
            "and hosted providers 400 on unknown request fields.",
            provider,
        )
        return 2
    if provider != DEFAULT_LLM_PROVIDER:
        if ignore_providers:
            logger.error(
                "[pilot] --ignore-providers requires --provider=%s",
                DEFAULT_LLM_PROVIDER,
            )
            return 2
        if provider_order:
            logger.error("[pilot] --provider-order requires --provider=%s", DEFAULT_LLM_PROVIDER)
            return 2

    # OWN-DECK CARD TEXT, assembled exactly as training assembles it.
    #
    # LAST of the startup checks on purpose. Placed earlier it pre-empted the
    # provider and decision-identity guards, so a run misconfigured in two ways
    # reported the deck first and the operator fixed the wrong thing -- and a test
    # asserting the decision-identity message got this one instead.
    #
    # THE SEAT IS ALREADY RESOLVED BY THE LAUNCHER, and the comment that used to sit
    # here said otherwise. It claimed the block could not be wired because every
    # production invocation passes --config and none passes --deck. That conflated
    # two layers: the shell and mtg_agent_loop pass --config to the ORCHESTRATOR,
    # which reads the seat out of it and passes --deck <root>/<player.deck> to THIS
    # process (game_processes.py:482). All 56 pilot seats in configs/ carry a deck,
    # so --deck is what a pilot receives and it IS the seat's own deck.
    if args.deck and not args.no_deck_block:
        system_prompt = assemble_system_prompt(system_prompt, args.deck)
    elif not args.no_deck_block:
        # LOUD. A pilot without its deck block is served a strictly different prompt
        # from the one training rendered -- the exact train/serve gap this closes --
        # and it would run perfectly, just measuring something else. The opt-out
        # exists for ad-hoc play and has to be typed on purpose.
        logger.error(
            "[pilot] no --deck, so no own-deck card text, but training always "
            "renders it: the served prompt would differ from the trained one. "
            "Pass --deck, or --no-deck-block to say you meant it."
        )
        return 2

    try:
        asyncio.run(
            run_pilot(
                server=args.server,
                port=args.port,
                username=args.username,
                project_root=project_root,
                deck_path=args.deck,
                api_key=api_key,
                model=args.model,
                provider=args.provider,
                system_prompt=system_prompt,
                game_dir=args.game_dir,
                table_id=args.table_id,
                prices=prices,
                max_interactions_per_turn=args.max_interactions_per_turn,
                reasoning_effort=args.reasoning_effort,
                tools=pilot_tools,
                ignore_providers=ignore_providers,
                provider_order=provider_order,
                cache_control=cache_control,
            )
        )
    except KeyboardInterrupt:
        pass
    except PermanentLLMError as exc:
        logger.error("[pilot] Permanent LLM failure: %s", exc)
        return PERMANENT_FAILURE_EXIT_CODE

    return 0


if __name__ == "__main__":
    sys.exit(main())
