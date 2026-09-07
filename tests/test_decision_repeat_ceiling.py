"""The repeat counter that catches an engine re-asking one decision forever.

The defect this exists for (issues/p1-select-attackers-repeats-until-the-game-is-killed)
is invisible to MAX_TURNS_WITHOUT_PROGRESS, because the policy is calling tools
successfully and getting valid results the whole time -- turns_without_progress is reset on
every pass round the loop. So the first test here is the NEGATIVE CONTROL: the old signal
must be shown blind to the case, or "the new counter fires" says nothing about whether it
was needed.
"""
import json

import pytest

from magebench.pilot.pilot_state import PilotLoopState, record_decision_seq


def result(seq: int) -> str:
    return json.dumps({"game_seq": seq, "action_pending": True})


def test_the_existing_progress_signal_is_blind_to_a_repeat_loop():
    """Negative control. A repeat loop makes successful tool calls, so any counter keyed on
    'did a tool call succeed' stays at zero while the game goes nowhere."""
    state = PilotLoopState(history=[])
    for _ in range(500):
        record_decision_seq(state, result(7))
        state.turns_without_progress = 0  # what the loop does on a successful tool result
    assert state.turns_without_progress == 0, "the old signal would have fired; the control is wrong"
    assert state.consecutive_same_decision_seq == 500, "the new counter must see what it cannot"


def test_repeats_of_one_decision_accumulate():
    state = PilotLoopState(history=[])
    for i in range(1, 11):
        record_decision_seq(state, result(42))
        assert state.consecutive_same_decision_seq == i


def test_a_new_decision_resets_the_counter():
    """The good state, not merely the absence of the bad one: a game that advances must drive
    this back to 1, or the ceiling would eventually trip on every long game."""
    state = PilotLoopState(history=[])
    for _ in range(5):
        record_decision_seq(state, result(1))
    assert state.consecutive_same_decision_seq == 5
    record_decision_seq(state, result(2))
    assert state.consecutive_same_decision_seq == 1


def test_a_normal_game_never_approaches_the_ceiling():
    """Measured over 214 finished games on two nodes: p99 5-8, max 8, and the largest repeat
    ever recorded in any configuration was 20. Replayed here at the observed worst."""
    state = PilotLoopState(history=[])
    seq = 0
    worst = 0
    for _ in range(200):
        for _ in range(8):  # the measured maximum for a legitimate decision
            record_decision_seq(state, result(seq))
            worst = max(worst, state.consecutive_same_decision_seq)
        seq += 1
    assert worst == 8
    assert worst < 60, "the default ceiling must sit clear of the measured worst case"


def test_the_recovery_flag_clears_when_the_game_moves_on():
    """Otherwise one recovered decision would poison the next one: the second time the ceiling
    was reached the game would be abandoned without the auto-pass ever being tried."""
    state = PilotLoopState(history=[])
    record_decision_seq(state, result(3))
    state.repeat_recovery_attempted = True
    record_decision_seq(state, result(4))
    assert state.repeat_recovery_attempted is False


def test_a_result_without_a_game_seq_does_not_touch_the_counter():
    """Absent stays absent -- the same rule the stamp itself follows. A state query between
    two turns of one decision must neither advance nor reset the count."""
    state = PilotLoopState(history=[])
    record_decision_seq(state, result(9))
    record_decision_seq(state, result(9))
    record_decision_seq(state, json.dumps({"board": "..."}))
    record_decision_seq(state, "")
    record_decision_seq(state, "not json at all")
    assert state.consecutive_same_decision_seq == 2


def test_seq_zero_is_a_real_seq_and_not_a_sentinel():
    state = PilotLoopState(history=[])
    record_decision_seq(state, result(0))
    record_decision_seq(state, result(0))
    assert state.consecutive_same_decision_seq == 2


def test_the_ceiling_default_and_its_override(monkeypatch):
    from magebench.pilot.pilot import _max_decision_repeats

    monkeypatch.delenv("MAGEBENCH_MAX_DECISION_REPEATS", raising=False)
    assert _max_decision_repeats() == 60
    monkeypatch.setenv("MAGEBENCH_MAX_DECISION_REPEATS", "12")
    assert _max_decision_repeats() == 12
    # No silent fallback: a malformed or nonsensical value must raise rather than quietly
    # becoming the default, which is how a deliberate setting gets replaced without a word.
    monkeypatch.setenv("MAGEBENCH_MAX_DECISION_REPEATS", "0")
    with pytest.raises(ValueError):
        _max_decision_repeats()
    monkeypatch.setenv("MAGEBENCH_MAX_DECISION_REPEATS", "banana")
    with pytest.raises(ValueError):
        _max_decision_repeats()
