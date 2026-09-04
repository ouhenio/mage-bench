"""Forced decisions are answered by the harness and cost one line.

The property under test is not "the rule is right" -- a decision with no options
has one legal answer -- but that the model is never asked, and that a decision
which DOES offer a choice always reaches it.
"""

import json

import pytest

from magebench.pilot.auto_resolve import (
    auto_resolve_enabled,
    card_text_mode,
    chose_unoffered,
    is_forced_decision,
    render_auto_resolved,
)
from magebench.pilot.pilot_rendering import render_for_pilot


def _decision(n_choices: int, response_type: str = "select", **over) -> dict:
    data = {
        "action_pending": True,
        "response_type": response_type,
        "message": "Choose an action",
        "board": [{"name": "Skill1", "life": 20, "library_size": 40}],
    }
    if n_choices:
        data["choices"] = [{"index": i, "text": f"Play card {i}", "id": f"c{i}"}
                           for i in range(n_choices)]
    data.update(over)
    return data


# ------------------------------------------------------------- the predicate


def test_no_options_is_forced():
    assert is_forced_decision(_decision(0)) is True


def test_one_option_is_NOT_forced():
    """THE LINE THE TOKEN-BUDGET REPORT DREW IN THE WRONG PLACE.

    `len(choices) < 2` would call this forced. Measured on 124,007 such
    decisions, the teacher takes a real action in 17,229 of them -- 7,269 a pN
    and 9,960 an attackers/blockers answer. One available creature is
    attack-with-it-or-not, and auto-passing would suppress every single-attacker
    attack in the corpus.
    """
    assert is_forced_decision(_decision(1)) is False


def test_two_options_is_not_forced():
    assert is_forced_decision(_decision(2)) is False


@pytest.mark.parametrize("response_type", ["boolean"])
def test_a_boolean_ask_is_never_forced(response_type):
    """The guard that keeps mulligans and choose_use out.

    Both reach the pilot as response_type "boolean" carrying NO `choices` key, so
    a predicate written only as len(choices)==0 would auto-pass them -- declining
    an ability, or keeping a hand the mulligan rule wanted to throw. They are
    absent from the corpus's zero-choice class (310,738 select + 242 index, zero
    boolean) because the recorder builds options for them; the bridge projection
    the pilot sees is a different object.
    """
    assert is_forced_decision(_decision(0, response_type=response_type)) is False


def test_a_non_decision_is_not_forced():
    assert is_forced_decision(_decision(0, action_pending=False)) is False


# ------------------------------------------------------------- the rendering


def test_a_forced_decision_renders_as_one_line(monkeypatch):
    monkeypatch.delenv("MAGEBENCH_AUTO_RESOLVE_FORCED", raising=False)
    text, board = render_for_pilot(json.dumps(_decision(0)), None, set(), 7)
    assert text == render_auto_resolved(7)
    assert "\n" not in text
    # The board must NOT be shown -- that is the whole saving.
    assert "life" not in text and "Skill1" not in text


def test_the_board_cursor_does_not_advance_past_an_unshown_board(monkeypatch):
    """LOAD-BEARING. The next decision that IS shown deltas against the last
    board the model actually saw. Advancing here would diff against a board that
    appears nowhere in the transcript."""
    monkeypatch.delenv("MAGEBENCH_AUTO_RESOLVE_FORCED", raising=False)
    previous = [{"name": "Skill1", "life": 20, "library_size": 40}]
    _text, board = render_for_pilot(json.dumps(_decision(0)), previous, set(), 3)
    assert board is previous


def test_seen_cards_are_untouched_by_a_forced_decision(monkeypatch):
    # No card text was emitted, so nothing may be marked as already seen -- or a
    # card would be suppressed later on the grounds of a line nobody read.
    monkeypatch.delenv("MAGEBENCH_AUTO_RESOLVE_FORCED", raising=False)
    seen: set = set()
    render_for_pilot(json.dumps(_decision(0)), None, seen, 0)
    assert seen == set()


def test_a_real_decision_still_renders_its_board(monkeypatch):
    """The control. If this shrank too, the saving would be indiscriminate."""
    monkeypatch.delenv("MAGEBENCH_AUTO_RESOLVE_FORCED", raising=False)
    text, _ = render_for_pilot(json.dumps(_decision(3)), None, set(), 0)
    assert "[Decision 0" in text
    # The PROPERTY, not a length proxy: the board and the options are present.
    assert "Board:" in text
    assert "Choices (3)" in text


def test_the_reference_arm_renders_forced_decisions_in_full(monkeypatch):
    # MAGEBENCH_AUTO_RESOLVE_FORCED=0 must reproduce the old transcript exactly,
    # or the A/B compares an arm against a different renderer as well.
    monkeypatch.setenv("MAGEBENCH_AUTO_RESOLVE_FORCED", "0")
    text, _ = render_for_pilot(json.dumps(_decision(0)), None, set(), 0)
    assert text != render_auto_resolved(0)
    assert "[Decision 0" in text


# ------------------------------------------------------------------ the knob


def test_the_default_is_on(monkeypatch):
    monkeypatch.delenv("MAGEBENCH_AUTO_RESOLVE_FORCED", raising=False)
    assert auto_resolve_enabled() is True


def test_an_unknown_value_is_refused(monkeypatch):
    monkeypatch.setenv("MAGEBENCH_AUTO_RESOLVE_FORCED", "true")
    with pytest.raises(ValueError, match="not 0 or 1"):
        auto_resolve_enabled()


# ------------------------------------------------------- the accepted loss


def test_a_play_offered_no_options_is_flagged_not_hidden():
    # 26 of 310,980 zero-choice decisions answer with a pN. Auto-resolving passes
    # them; this is how the audit counts what that cost.
    assert chose_unoffered(_decision(0), {"choice": "p3"}) is True
    assert chose_unoffered(_decision(0), {"choice": "no"}) is False
    assert chose_unoffered(_decision(3), {"choice": "p3"}) is False


# ------------------------------------------------------ card text: the arms


def _board_decision() -> str:
    return json.dumps({
        "action_pending": True,
        "response_type": "select",
        "message": "Choose an action",
        "choices": [{"index": 0, "text": "Play Llanowar Elves", "id": "c0"}],
        "board": [{
            "name": "Skill1", "life": 20, "library_size": 40,
            "battlefield": [{"name": "Llanowar Elves", "rules": ["{T}: Add {G}."]}],
        }],
    })


def _render_twice(monkeypatch, mode):
    """Render the SAME decision twice with one `seen` set, as a game would."""
    if mode is None:
        monkeypatch.delenv("MAGEBENCH_CARD_TEXT", raising=False)
    else:
        monkeypatch.setenv("MAGEBENCH_CARD_TEXT", mode)
    seen: set = set()
    blob = _board_decision()
    first, board = render_for_pilot(blob, None, seen, 0)
    second, _ = render_for_pilot(blob, board, seen, 1)
    return first, second


def test_first_reveal_shows_the_text_once(monkeypatch):
    first, second = _render_twice(monkeypatch, None)
    assert "{T}: Add {G}." in first
    # THE STATUS QUO, and the reason the reading test needs a third arm: by the
    # second decision the policy must have remembered, not read.
    assert "{T}: Add {G}." not in second


def test_none_shows_the_text_never(monkeypatch):
    first, second = _render_twice(monkeypatch, "none")
    assert "{T}: Add {G}." not in first
    assert "{T}: Add {G}." not in second
    # ABSENT, not suppressed-because-seen: the card is still on the board.
    assert "Llanowar Elves" in first


def test_always_shows_the_text_every_time(monkeypatch):
    first, second = _render_twice(monkeypatch, "always")
    assert "{T}: Add {G}." in first
    assert "{T}: Add {G}." in second, (
        "arm C must re-emit on every decision, or it cannot separate a policy "
        "that cannot read from one that cannot remember"
    )


def test_always_and_first_reveal_agree_on_the_FIRST_decision(monkeypatch):
    # The arms must differ only in repetition. If they disagreed at first
    # reveal, a gap between them would confound repetition with formatting.
    a, _ = _render_twice(monkeypatch, None)
    c, _ = _render_twice(monkeypatch, "always")
    assert a == c


def test_an_unknown_card_text_mode_is_refused(monkeypatch):
    monkeypatch.setenv("MAGEBENCH_CARD_TEXT", "repeat")
    with pytest.raises(ValueError, match="not one of"):
        card_text_mode()


# ---------------------------------------------------------- empty priority windows

_PRIORITY = {"action_pending": True, "action_type": "GAME_SELECT", "response_type": "boolean"}


def _pw(**kw):
    return {**_PRIORITY, "message": "Play instants and activated abilities", **kw}


def test_empty_priority_window_is_forced():
    """46.6% of every decision the policy answers, each ~2.3s and ~180 completion tokens
    spent concluding "I have no mana"."""
    from magebench.pilot.auto_resolve import is_forced_decision

    assert is_forced_decision(_pw(has_playable_cards=False)) is True


def test_a_priority_window_with_something_playable_is_NOT_forced():
    from magebench.pilot.auto_resolve import is_forced_decision

    assert is_forced_decision(_pw(has_playable_cards=True)) is False


def test_a_missing_flag_is_not_treated_as_empty():
    """Absent means a bridge that never emitted it. Treating absent as False would
    auto-pass every priority window in every corpus recorded before 2026-09-04 -- the
    absent-vs-false collapse that `x.get(k) or False` performs silently."""
    from magebench.pilot.auto_resolve import is_forced_decision

    assert is_forced_decision(_pw()) is False


def test_select_attackers_is_never_auto_passed():
    """THE ONE THAT MATTERS. 'Select attackers' is GAME_SELECT/boolean with the IDENTICAL
    respond_with string, and a player holding creatures but no castable cards has
    has_playable_cards=false -- so a rule keyed on the flag alone suppresses every attack
    in the corpus. combat_phase does not separate them: 19 of 44 such frames in
    leg1-ckpt132 carry combat_phase=None. Only the message does."""
    from magebench.pilot.auto_resolve import is_forced_decision

    assert is_forced_decision({**_PRIORITY, "message": "Select attackers",
                               "has_playable_cards": False}) is False
    assert is_forced_decision({**_PRIORITY, "message": "Select blockers",
                               "has_playable_cards": False}) is False


def test_a_mulligan_is_never_auto_passed():
    """GAME_ASK, not GAME_SELECT. Auto-passing it keeps a hand the mulligan rule wanted
    thrown -- the failure the response-type guard was written against."""
    from magebench.pilot.auto_resolve import is_forced_decision

    assert is_forced_decision({"action_pending": True, "action_type": "GAME_ASK",
                               "response_type": "boolean",
                               "message": "Mulligan down to 6 cards?",
                               "has_playable_cards": False}) is False


def test_the_knob_turns_it_off_for_comparability(monkeypatch):
    """This class is 46.6% of decisions, so a corpus generated with it on is not
    comparable to one generated without. The knob is how that stays measurable."""
    from magebench.pilot import auto_resolve

    monkeypatch.setenv("MAGEBENCH_AUTO_RESOLVE_EMPTY_PRIORITY", "0")
    assert auto_resolve.is_forced_decision(_pw(has_playable_cards=False)) is False
    monkeypatch.setenv("MAGEBENCH_AUTO_RESOLVE_EMPTY_PRIORITY", "1")
    assert auto_resolve.is_forced_decision(_pw(has_playable_cards=False)) is True


def test_the_knob_refuses_a_value_that_is_not_0_or_1(monkeypatch):
    import pytest
    from magebench.pilot import auto_resolve

    monkeypatch.setenv("MAGEBENCH_AUTO_RESOLVE_EMPTY_PRIORITY", "yes")
    with pytest.raises(ValueError, match="is not 0 or 1"):
        auto_resolve.auto_resolve_empty_priority_enabled()
