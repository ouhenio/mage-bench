"""The segment rule, and the pilot cutting by it.

The claim under test is not "the pilot cuts somewhere". It is that the pilot
cuts where `render_conversations` cuts, from the same function, so a boundary
the model meets in play is a boundary it met in training.
"""

import pathlib

import pytest

from magebench.pilot import pilot
from magebench.pilot.context_segments import (
    PENDING_ANSWER_RESERVE_CHARS,
    SEGMENT_MAX_DECISIONS,
    SEGMENT_MAX_TOKENS,
    SERVE_MIN_MODEL_LEN,
    context_window_mode,
    require_servable_context,
    segment_budget_chars,
    should_close_segment,
)
from magebench.pilot.pilot_rendering import CHARS_PER_TOKEN_WORST, MAX_TOKENS
from magebench.pilot.pilot_state import PilotLoopState


def _decision_blob() -> str:
    """A REAL tool result, lifted out of the teacher corpus.

    Hand-written blobs kept passing the renderer's asserts by accident and then
    rendering into something no game ever produced. This one is a `choose_action`
    from lascar.final.decisions.jsonl, so a cut that renders it wrong fails here
    rather than in a rollout.
    """
    return (pathlib.Path(__file__).parent / "fixtures_decision_blob.json").read_text().strip()


# ---------------------------------------------------------------- the rule


def test_the_budget_is_the_assemblers_arithmetic():
    # render_conversations computed `args.max_tokens * 3 * CHARS_PER_TOKEN_WORST`
    # inline. The shared function has to be that same number, or moving the rule
    # moved the boundary with it.
    assert segment_budget_chars(131072) == 131072 * 3 * CHARS_PER_TOKEN_WORST
    assert segment_budget_chars() == segment_budget_chars(SEGMENT_MAX_TOKENS)


def test_a_segment_is_never_closed_empty():
    # `if cur and ...` in the assembler. Without it a single decision larger
    # than the whole budget cuts forever and the render makes no progress.
    assert not should_close_segment(
        used_chars=0,
        pending_cost_chars=10**9,
        decisions_in_segment=0,
        budget_chars=100.0,
        max_decisions=SEGMENT_MAX_DECISIONS,
    )


def test_the_decision_ceiling_binds_independently_of_characters():
    # Two ceilings on one segment; whichever binds first wins. A tiny
    # conversation at the decision cap still cuts.
    assert should_close_segment(
        used_chars=1,
        pending_cost_chars=1,
        decisions_in_segment=SEGMENT_MAX_DECISIONS,
        budget_chars=10**9,
        max_decisions=SEGMENT_MAX_DECISIONS,
    )


def test_the_character_ceiling_is_strict_not_inclusive():
    # `used + cost > budget`, so landing exactly on the budget is accepted.
    # A `>=` here would cut one decision early on every boundary.
    assert not should_close_segment(
        used_chars=60,
        pending_cost_chars=40,
        decisions_in_segment=1,
        budget_chars=100.0,
        max_decisions=SEGMENT_MAX_DECISIONS,
    )
    assert should_close_segment(
        used_chars=61,
        pending_cost_chars=40,
        decisions_in_segment=1,
        budget_chars=100.0,
        max_decisions=SEGMENT_MAX_DECISIONS,
    )


# ---------------------------------------------------------------- the knob


def test_the_default_is_full_history(monkeypatch):
    monkeypatch.delenv("MAGEBENCH_CONTEXT_WINDOW", raising=False)
    monkeypatch.delenv("MAGEBENCH_APPEND_ONLY", raising=False)
    assert context_window_mode() == "full"


def test_the_old_spelling_still_selects_the_windowed_arm(monkeypatch):
    # The windowed arm is the reference for the paired A/B and every baseline
    # taken before 2026-08-17. A rename that ignored this would have turned the
    # reference arm into a second copy of the treatment arm, silently.
    monkeypatch.delenv("MAGEBENCH_CONTEXT_WINDOW", raising=False)
    monkeypatch.setenv("MAGEBENCH_APPEND_ONLY", "0")
    assert context_window_mode() == "windowed"


def test_the_two_spellings_disagreeing_is_refused(monkeypatch):
    monkeypatch.setenv("MAGEBENCH_CONTEXT_WINDOW", "full")
    monkeypatch.setenv("MAGEBENCH_APPEND_ONLY", "0")
    with pytest.raises(ValueError, match="different things"):
        context_window_mode()


def test_the_two_spellings_agreeing_is_accepted(monkeypatch):
    # The control for the test above: a preset that sets both to the same thing
    # during a migration must not be refused, or the refusal is just a ban.
    monkeypatch.setenv("MAGEBENCH_CONTEXT_WINDOW", "windowed")
    monkeypatch.setenv("MAGEBENCH_APPEND_ONLY", "0")
    assert context_window_mode() == "windowed"


def test_an_unknown_mode_is_refused_rather_than_defaulted(monkeypatch):
    monkeypatch.setenv("MAGEBENCH_CONTEXT_WINDOW", "whole")
    monkeypatch.delenv("MAGEBENCH_APPEND_ONLY", raising=False)
    with pytest.raises(ValueError, match="not one of"):
        context_window_mode()


# ------------------------------------------------------------- the serving guard


def test_a_server_below_the_serving_floor_is_refused():
    with pytest.raises(ValueError, match="max_model_len=40960"):
        require_servable_context(40960, source="http://localhost:8000/v1")


def test_the_segment_budget_alone_is_not_enough_to_serve():
    # THE POINT OF THE SEPARATE CONSTANT. 131,072 is what the budget names; it
    # is not what the budget can emit, because CHARS_PER_TOKEN_WORST (0.805)
    # sits above the worst ratio actually measured (0.771). A server at exactly
    # the budget still has a band where the characters fit and the tokens do not.
    with pytest.raises(ValueError, match="max_model_len=131072"):
        require_servable_context(SEGMENT_MAX_TOKENS, source="test")


def test_a_server_at_the_serving_floor_is_accepted():
    require_servable_context(SERVE_MIN_MODEL_LEN, source="test")
    require_servable_context(SERVE_MIN_MODEL_LEN + 1, source="test")


def test_the_serving_floor_covers_the_budget_at_the_worst_measured_ratio():
    # The derivation, not the number: 0.771 is the minimum (chars/3)/tokens over
    # 10,875 recorded prompts, and MAX_TOKENS is reserved for the completion
    # against the same limit. If someone lowers the floor to a rounder figure
    # this fails rather than quietly reopening the band.
    worst_case = segment_budget_chars() / 3 / 0.771 + MAX_TOKENS
    assert SERVE_MIN_MODEL_LEN >= worst_case


# --------------------------------------------------------------- the pilot cut


# The fixture history below is 5,000 chars of prior context plus a 400-char
# pending decision, so the cut fires exactly when the budget drops to that plus
# the reserve.
_FIXTURE_USED_BEFORE = 5000
_FIXTURE_PENDING = 400


def _cut_threshold() -> float:
    """The largest budget at which this fixture still does NOT cut."""
    return float(_FIXTURE_USED_BEFORE + _FIXTURE_PENDING + PENDING_ANSWER_RESERVE_CHARS)


def _state_with_one_decision(monkeypatch, blob: str) -> PilotLoopState:
    monkeypatch.delenv("MAGEBENCH_CONTEXT_WINDOW", raising=False)
    monkeypatch.delenv("MAGEBENCH_APPEND_ONLY", raising=False)
    state = PilotLoopState(
        history=[
            {"role": "user", "content": "x" * 5000},
            {"role": "assistant", "content": None},
            {"role": "tool", "tool_call_id": "call_1", "content": "y" * 400},
        ]
    )
    state.pending_decision_blob = blob
    state.pending_decision_chars = 400
    state.decisions_seen = 7
    state.segment_decisions = 7
    # A card the fixture reveals, marked as already seen. Under append-only its
    # text is suppressed because the model was shown it earlier in the SAME
    # conversation; across a cut that conversation is gone, so suppressing it
    # would leave a reference to text the model never got.
    state.seen_oracle_cards.add("Ghalma's Warden")
    state.last_board = [{"player": "Pilot"}]
    return state


def test_below_the_budget_nothing_is_cut(monkeypatch):
    state = _state_with_one_decision(monkeypatch, _decision_blob())
    cut = pilot.close_segment_if_needed(state, "system prompt")
    assert cut is False
    assert len(state.history) == 3
    assert state.segment_decisions == 8
    assert state.decisions_seen == 7
    # Consumed, so a stalled turn that makes several calls for one decision
    # prices that decision once rather than once per call.
    assert state.pending_decision_blob is None


def test_over_the_budget_the_crossing_decision_opens_a_new_segment(monkeypatch):
    state = _state_with_one_decision(monkeypatch, _decision_blob())
    # DERIVED, NOT WRITTEN DOWN. The exact threshold moves whenever the reserve
    # moves -- it did, from 256 to 4096 -- and a hardcoded 5655 turns a
    # deliberate change to the constant into two red tests that say nothing
    # about the property under test.
    monkeypatch.setattr(pilot, "segment_budget_chars", lambda *a: _cut_threshold() - 1)

    cut = pilot.close_segment_if_needed(state, "")

    assert cut is True
    assert [m["role"] for m in state.history] == ["user"]
    # A user message, not a tool message: the assistant call it answered is gone
    # with the rest of history, and a tool result whose call is missing is a 400.
    assert "tool_call_id" not in state.history[0]
    # The decision is re-rendered, not carried over verbatim.
    assert "[Decision 0" in state.history[0]["content"]
    assert state.decisions_seen == 1
    assert state.segment_decisions == 1
    assert state.segment_index == 1
    # `seen` reset, so a card first revealed before the boundary is revealed
    # again after it rather than referenced in a context that never had it.
    assert "Ghalma's Warden {3}{W}" in state.history[0]["content"]


def test_without_a_cut_an_already_seen_card_stays_suppressed(monkeypatch):
    # The control for the reset assertion above. If the renderer emitted oracle
    # text for a seen card anyway, that assertion would pass on a cut function
    # that reset nothing.
    state = _state_with_one_decision(monkeypatch, _decision_blob())
    seen = set(state.seen_oracle_cards)
    text, _ = pilot.render_for_pilot(_decision_blob(), None, seen, 0)
    assert "Ghalma's Warden {3}{W}" not in text


def test_one_character_under_the_budget_does_not_cut(monkeypatch):
    # The control for the test above. Without it, a cut function that fired
    # unconditionally would pass every assertion there.
    state = _state_with_one_decision(monkeypatch, _decision_blob())
    monkeypatch.setattr(pilot, "segment_budget_chars", lambda *a: _cut_threshold())
    assert pilot.close_segment_if_needed(state, "") is False
    assert len(state.history) == 3


def test_the_system_prompt_counts_toward_the_budget(monkeypatch):
    # The deck block rides inside the system prompt at inference, and a segment
    # repeats it. A budget that ignored it would overshoot by the size of the
    # block on every segment.
    state = _state_with_one_decision(monkeypatch, _decision_blob())
    monkeypatch.setattr(pilot, "segment_budget_chars", lambda *a: _cut_threshold())
    assert pilot.close_segment_if_needed(state, "") is False

    state = _state_with_one_decision(monkeypatch, _decision_blob())
    monkeypatch.setattr(pilot, "segment_budget_chars", lambda *a: _cut_threshold())
    assert pilot.close_segment_if_needed(state, "z") is True


def test_the_windowed_arm_is_never_cut(monkeypatch):
    # The windowed arm bounds its own prompt. Cutting it here would put a
    # boundary in the reference arm that the treatment arm is measured against.
    state = _state_with_one_decision(monkeypatch, _decision_blob())
    monkeypatch.setenv("MAGEBENCH_APPEND_ONLY", "0")
    monkeypatch.setattr(pilot, "segment_budget_chars", lambda *a: 1.0)
    assert pilot.close_segment_if_needed(state, "system") is False
    assert len(state.history) == 3


def test_no_pending_decision_means_no_check(monkeypatch):
    state = _state_with_one_decision(monkeypatch, _decision_blob())
    state.pending_decision_blob = None
    monkeypatch.setattr(pilot, "segment_budget_chars", lambda *a: 1.0)
    assert pilot.close_segment_if_needed(state, "system") is False
    assert state.segment_decisions == 7


def test_a_cut_is_recorded_in_the_game_log(monkeypatch):
    # A boundary the trainer cannot see is a boundary it cannot reproduce.
    state = _state_with_one_decision(monkeypatch, _decision_blob())
    monkeypatch.setattr(pilot, "segment_budget_chars", lambda *a: 1.0)
    events = []

    class _Log:
        def emit(self, event, **fields):
            events.append((event, fields))

    assert pilot.close_segment_if_needed(state, "", _Log()) is True
    assert events[0][0] == "context_reset"
    assert events[0][1]["cause"] == "segment_boundary"
    assert events[0][1]["reset_index"] == 1


def test_the_reserve_covers_the_measured_label_census_with_headroom():
    # Census of the SERIALISED label, two sessions independently: 2,875,957
    # decisions max 1436 (karn-research, both blocks) and 956,320 max 1034
    # (karn-engine, block 1). The previous value was 256, set from a max of 161
    # over 300,000 decisions -- a sample that expected 0.63 outliers at the
    # measured 2.09-per-million rate, so P(it saw none) was 0.53.
    #
    # ASSERTED WITH HEADROOM, not at the maximum. Nothing bounds an `attackers`
    # list except board width, so the observed max is not a bound and setting
    # the reserve to it would repeat the original error with a bigger number.
    assert PENDING_ANSWER_RESERVE_CHARS >= 2 * 1436
