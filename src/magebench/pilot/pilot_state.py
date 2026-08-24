"""Mutable pilot loop state and board-cursor helpers."""

import json
from dataclasses import dataclass, field

from magebench.pilot.pilot_rendering import build_reset_message, extract_last_reasoning

_BOARD_CURSOR_TOOLS = frozenset({"pass_priority", "get_action_choices"})


class BoardCursorTracker:
    """Tracks board_cursor across tool calls for board state dedup."""

    def __init__(self) -> None:
        self.cursor: int | None = None

    def inject(self, tool_name: str, args: dict) -> None:
        """Inject board_cursor into args if applicable."""
        if tool_name in _BOARD_CURSOR_TOOLS and self.cursor is not None:
            args["board_cursor"] = self.cursor

    def extract(self, result_text: str) -> None:
        """Extract board_cursor from a tool result string."""
        try:
            data = json.loads(result_text)
            if isinstance(data, dict) and "board_cursor" in data:
                self.cursor = data["board_cursor"]
        except (json.JSONDecodeError, TypeError):
            pass

    def reset(self) -> None:
        """Force full board on the next call (e.g. after context reset)."""
        self.cursor = None


@dataclass
class PilotLoopState:
    """Mutable state for the pilot loop."""

    history: list[dict]
    state_summary: str = ""
    cumulative_cost: float = 0.0
    empty_responses: int = 0
    last_was_empty: bool = False
    consecutive_timeouts: int = 0
    consecutive_empty_choices: int = 0
    turns_without_progress: int = 0
    consecutive_pass_errors: int = 0
    last_pass_error_msg: str = ""
    consecutive_truncations: int = 0
    consecutive_empty_errors: int = 0
    last_game_seq: int | None = None
    # The seq of the decision the CURRENT prompt is about, captured from the same
    # result_text that gets rendered into history. Deliberately a second field rather
    # than a reuse of last_game_seq above:
    #   - last_game_seq is stamped from EVERY tool result (pilot.py:378-380) and
    #     tool_call rows are joined on it, so its meaning is "the last seq any tool
    #     mentioned" and it must not change;
    #   - get_game_state's result also carries game_seq -- the current VIEW's seq, not
    #     the pending decision's -- and would clobber it. (Unexercised: 0 get_game_state
    #     calls across the recorded corpus, but 8 games were offered the tool.)
    # This one is written only at the three publish-site tools, so it always names a
    # decision that was actually put to the policy. It repeats across calls on purpose:
    # one decision can take several LLM turns (measured: 153 rows over 115 distinct
    # seqs in one game, seq=19 repeated 20 times).
    last_decision_seq: int | None = None

    # How many DECISIONS this pilot has been shown, which is what the rendered
    # header's "[Decision N]" is supposed to say. Counted here rather than derived
    # in the renderer because the renderer sees one tool result at a time and has
    # no memory; it was passing a literal 0 for want of anywhere to keep this.
    #
    # Incremented only for results that actually carry a decision (action_pending),
    # so N indexes decisions rather than tool calls -- a state query between two
    # decisions must not advance it, or the number stops meaning position.
    decisions_seen: int = 0
    board_tracker: BoardCursorTracker = field(default_factory=BoardCursorTracker)
    last_board: list[dict] | None = None
    current_game_turn: int = 0
    last_chat_turn: int = 0
    seen_oracle_cards: set[str] = field(default_factory=set)
    cache_breakpoint_idx: int | None = None
    render_counter: int = 0
    context_overflow_resets: int = 0
    # Actual prompt_tokens from the serving engine's last response. The
    # append-only context guard measures the DELTA against this instead of
    # estimating the whole prompt from characters -- see render_context.
    last_prompt_tokens: int | None = None
    # Character count of the messages list that produced last_prompt_tokens.
    # The pair is the anchor: tokens from the engine, chars from what we sent.
    last_prompt_chars: int | None = None


@dataclass
class PilotTurnState:
    """Per-response tool execution state used for stall detection."""

    had_successful_action: bool = False
    had_actionable_opportunity: bool = False
    tools_called: set[str] = field(default_factory=set)
    chat_messages_this_turn: int = 0


def _reset_render_cache(state: PilotLoopState) -> None:
    """Drop cached prompt metadata after a context reset."""
    state.state_summary = ""
    state.cache_breakpoint_idx = None
    state.render_counter = 0



def record_decision_seq(state: "PilotLoopState", result_text: str) -> None:
    """Stamp the server's decision seq from a tool result, wherever that result came from.

    THIS MUST BE CALLED FROM EVERY PATH THAT EXECUTES A TOOL, not just the model's.

    It used to live inline in _process_tool_calls, which is the ONLY place the policy's
    own tool calls are handled -- so the harness's own recovery passes never reached it.
    _recover_from_stall and _handle_timeout call execute_tool directly, and their
    pass_priority can answer several decisions at once. Measured on game_20260818_025636:
    the stall auto-pass answered server decisions 23, 26, 30, 36 and 39, and the next
    policy call was still stamped 23 while the engine was on 44. Every row after that
    named a decision the harness had already answered, and a join keyed on it would look
    exact and be silently wrong -- in exactly the recovery paths, which is where nobody
    looks.

    A positive control makes that a negative result rather than an absence: the identical
    game_seq=44 result DOES advance the stamp when it arrives through the model's own tool
    call, so the check can see an advance when there is one.

    Absent stays absent. A result with no game_seq leaves the previous value alone rather
    than writing a sentinel; 0 is a legal seq, so a consumer cannot tell "no decision
    behind this call" from "decision zero".
    """
    if not result_text:
        return
    try:
        parsed = json.loads(result_text)
    except (json.JSONDecodeError, TypeError, ValueError):
        return
    if isinstance(parsed, dict) and isinstance(parsed.get("game_seq"), int):
        state.last_decision_seq = parsed["game_seq"]


def reset_context(
    state: PilotLoopState,
    base_text: str,
    *,
    reset_board_context: bool,
) -> None:
    """Reset the conversation while preserving the last assistant reasoning."""
    last_reasoning = extract_last_reasoning(state.history)
    state.history = [
        {
            "role": "user",
            "content": build_reset_message(base_text, last_reasoning),
        },
    ]
    _reset_render_cache(state)
    state.seen_oracle_cards.clear()
    if reset_board_context:
        state.board_tracker.reset()
        state.last_board = None
