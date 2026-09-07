"""The completion cap, and the record that says how often it was hit.

These tests exist because the condition was real and invisible at the same time:
on corpus-v3-lascar-3135 the engine's own traces carried 62 truncated completions
in 9,196 calls, and the harness's game logs carried ZERO -- the warning went to
stderr and the game log heard nothing until three fired in a row, which at 0.7%
of calls essentially never happens.

So each test here pins the OCCURRENCE being recorded, not the recovery. The
negative test is the positive control for the others: it proves the emit is
conditional on the condition rather than unconditional noise.
"""

import logging
from unittest.mock import MagicMock

from magebench.pilot.context_segments import (
    SERVE_MIN_MODEL_LEN,
    _WORST_CASE_PROMPT_TOKENS,
)
from magebench.pilot.pilot_recovery import _handle_truncated_response
from magebench.pilot.pilot_rendering import MAX_TOKENS
from magebench.pilot.pilot_state import PilotLoopState

logger = logging.getLogger(__name__)


def _truncated(completion_tokens: int = MAX_TOKENS) -> tuple[MagicMock, MagicMock]:
    choice = MagicMock()
    choice.finish_reason = "length"
    response = MagicMock()
    response.usage = MagicMock(completion_tokens=completion_tokens)
    return choice, response


def _clean() -> tuple[MagicMock, MagicMock]:
    choice = MagicMock()
    choice.finish_reason = "tool_calls"
    response = MagicMock()
    response.usage = MagicMock(completion_tokens=18)
    return choice, response


def test_first_truncation_is_recorded_in_the_game_log():
    """One occurrence, one row -- not one row per three-in-a-row."""
    state = PilotLoopState(history=[])
    state.last_decision_seq = 207
    game_log = MagicMock()
    choice, response = _truncated()

    reset = _handle_truncated_response(
        state, choice, response, game_log,
        logger=logger, max_tokens=MAX_TOKENS, max_consecutive_truncations=3,
    )

    assert reset is False, "a single truncation must not reset the conversation"
    game_log.emit.assert_called_once_with(
        "completion_truncated",
        completion_tokens=MAX_TOKENS,
        max_tokens=MAX_TOKENS,
        consecutive=1,
        game_seq=207,
    )


def test_every_occurrence_is_recorded_not_only_the_reset():
    """Three truncations produce three rows AND the reset row, in that order."""
    state = PilotLoopState(history=[])
    game_log = MagicMock()
    for _ in range(3):
        choice, response = _truncated()
        _handle_truncated_response(
            state, choice, response, game_log,
            logger=logger, max_tokens=MAX_TOKENS, max_consecutive_truncations=3,
        )

    emitted = [call.args[0] for call in game_log.emit.call_args_list]
    assert emitted == [
        "completion_truncated",
        "completion_truncated",
        "completion_truncated",
        "context_reset",
    ]
    assert state.consecutive_truncations == 0, "the reset clears the run"


def test_a_clean_finish_records_nothing():
    """Positive control: the emit fires on the condition, not on every call."""
    state = PilotLoopState(history=[])
    state.consecutive_truncations = 2
    game_log = MagicMock()
    choice, response = _clean()

    reset = _handle_truncated_response(
        state, choice, response, game_log,
        logger=logger, max_tokens=MAX_TOKENS, max_consecutive_truncations=3,
    )

    assert reset is False
    game_log.emit.assert_not_called()
    assert state.consecutive_truncations == 0


def test_the_completion_reserve_fits_under_the_serving_floor():
    """Raising MAX_TOKENS must not silently outgrow the max_model_len we require.

    The floor was derived from a literal 1,024 written into a comment. This is the
    arithmetic itself, so the next raise fails here rather than as a mid-game 400
    on the longest games only.
    """
    assert _WORST_CASE_PROMPT_TOKENS + MAX_TOKENS <= SERVE_MIN_MODEL_LEN
