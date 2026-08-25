"""The engine's mulligan rule, as the harness answers it.

Validated against 745 recorded engine answers (290 games): 745 of 745 agree.
That validation has ONE BLIND SPOT and these tests exist to cover it -- see
test_the_threshold_moves_with_the_hand.
"""

import pytest

from magebench.pilot.mulligan import (
    count_lands,
    is_mulligan_decision,
    mulligan_choice,
    mulligan_mode,
    should_mulligan,
)


def _hand(lands: int, spells: int) -> list[dict]:
    return ([{"name": f"Land{i}", "is_land": True} for i in range(lands)]
            + [{"name": f"Spell{i}", "is_land": False} for i in range(spells)])


# ------------------------------------------------------------------ the rule


@pytest.mark.parametrize("lands,expected", [(0, True), (1, True), (2, False),
                                            (3, False), (5, False), (6, True), (7, True)])
def test_the_seven_card_boundaries(lands, expected):
    # keep iff 2 <= lands <= 5 on a seven-card hand.
    assert should_mulligan(_hand(lands, 7 - lands)) is expected


def test_the_threshold_moves_with_the_hand():
    """THE CASE THE 745 RECORDED ANSWERS CANNOT DECIDE.

    Every flooded mulligan in the corpus occurred at hand=7 (6 lands > 5), so a
    rule with the threshold written as a literal 5 agrees with the engine on all
    745 -- measured, 0 disagreements. The `size - 2` form is right because it is
    lifted from ComputerPlayer.chooseMulligan, not because the data shows it.

    A six-card hand with five lands is where the two forms part: 5 > 6-2 is a
    mulligan, 5 > 5 is not. The condition occurred zero times in 290 games, so
    it is constructed here rather than waited for.
    """
    assert should_mulligan(_hand(5, 1)) is True      # size-2 = 4, so 5 mulligans
    assert should_mulligan(_hand(4, 2)) is False     # 4 is not > 4, so keep

    def hardcoded_five(hand):
        if len(hand) < 6:
            return False
        lands = count_lands(hand)
        return lands < 2 or lands > 5

    # The control: the wrong form disagrees HERE and nowhere in the corpus.
    assert hardcoded_five(_hand(5, 1)) is False
    assert hardcoded_five(_hand(5, 1)) != should_mulligan(_hand(5, 1))


def test_a_hand_under_six_is_always_kept():
    # Live, not dead code: XMage's London bottoms inside mulligan(), so later
    # decisions see smaller hands. Fired on all 51 five-card hands in 290 games.
    for lands in range(6):
        assert should_mulligan(_hand(lands, 5 - lands)) is False
    # ... and the branch is load-bearing: without it a landless five would mull.
    assert count_lands(_hand(0, 5)) == 0


def test_the_answer_is_translated_to_the_tools_vocabulary():
    # ChooseActionTool: "yes=mulligan/confirm, no=keep/pass".
    assert mulligan_choice(_hand(0, 7)) == "yes"
    assert mulligan_choice(_hand(3, 4)) == "no"


# --------------------------------------------------------------- the detector


def _ask(message="Mulligan down to 6 cards?", **over):
    data = {"action_pending": True, "response_type": "boolean",
            "message": message, "your_hand": _hand(3, 4)}
    data.update(over)
    return data


def test_the_mulligan_ask_is_recognised():
    assert is_mulligan_decision(_ask()) is True


@pytest.mark.parametrize("over", [
    {"action_pending": False},                     # not a decision
    {"response_type": "select"},                   # not an ask
    {"message": "Pay {2} to keep this?"},          # an ask, but not a mulligan
    {"your_hand": None},                           # a mulligan-worded ask with no hand
])
def test_what_is_not_a_mulligan_ask(over):
    # The controls for the detector. Both gates matter: a non-mulligan ask has
    # no hand, and any other decision carrying a hand is not a boolean.
    assert is_mulligan_decision(_ask(**over)) is False


# ------------------------------------------------------------------ the knob


def test_the_default_is_the_engine_rule(monkeypatch):
    monkeypatch.delenv("MAGEBENCH_MULLIGAN", raising=False)
    assert mulligan_mode() == "engine-rule"


def test_the_model_arm_is_selectable(monkeypatch):
    monkeypatch.setenv("MAGEBENCH_MULLIGAN", "model")
    assert mulligan_mode() == "model"


def test_an_unknown_mode_is_refused_rather_than_defaulted(monkeypatch):
    # Defaulting would silently run the engine-rule arm while someone believed
    # they had selected the reference arm.
    monkeypatch.setenv("MAGEBENCH_MULLIGAN", "engine")
    with pytest.raises(ValueError, match="not one of"):
        mulligan_mode()


# ------------------------------------------------- the pilot answering it


@pytest.mark.asyncio
async def test_the_pilot_answers_a_mulligan_without_calling_the_policy(monkeypatch):
    """THE PROPERTY THAT MATTERS: no LLM round trip for a mulligan.

    Everything else here is arithmetic; this is the behaviour the change exists
    for. The rule agreeing with the engine is worth nothing if the decision still
    goes to the model.
    """
    import json
    from unittest.mock import AsyncMock, MagicMock

    from mcp.types import CallToolResult, TextContent

    from magebench.pilot import pilot

    monkeypatch.delenv("MAGEBENCH_MULLIGAN", raising=False)
    blob = json.dumps({
        "action_pending": True,
        "response_type": "boolean",
        "message": "Mulligan down to 6 cards?",
        # One land in seven: the screwed clause, so the answer must be "yes".
        "your_hand": ([{"name": "Island", "is_land": True}]
                      + [{"name": f"Spell{i}", "is_land": False} for i in range(6)]),
    })

    session = MagicMock()
    session.call_tool = AsyncMock(return_value=CallToolResult(
        content=[TextContent(type="text", text='{"ok": true}')]))
    events = []

    class _Log:
        def emit(self, event, **fields):
            events.append((event, fields))

    state = pilot.PilotLoopState(history=[])
    state.pending_decision_blob = blob

    answered = await pilot._answer_mulligan_from_the_engine_rule(session, state, _Log())

    assert answered is True
    name, args = session.call_tool.await_args.args[:2]
    assert name == "choose_action"
    assert args == {"choice": "yes"}, "one land in seven must mulligan"
    # A user message, not a tool message: no assistant tool call was made, and a
    # tool result whose call is missing is a 400 from the server.
    assert [m["role"] for m in state.history] == ["user"]

    kind, fields = events[0]
    assert kind == "mulligan_auto"
    # The INPUTS, not just the answer -- "keep" alone cannot be checked against
    # the rule afterwards, and no LLM call appears in the trace for this decision.
    assert fields["hand_size"] == 7
    assert fields["lands"] == 1
    assert fields["decision"] == "mulligan"
    assert fields["hand"].count("Island") == 1


@pytest.mark.asyncio
async def test_the_model_arm_leaves_the_mulligan_to_the_policy(monkeypatch):
    """The control. If this returned True the reference arm would not exist."""
    import json
    from unittest.mock import AsyncMock, MagicMock

    from magebench.pilot import pilot

    monkeypatch.setenv("MAGEBENCH_MULLIGAN", "model")
    session = MagicMock()
    session.call_tool = AsyncMock()
    state = pilot.PilotLoopState(history=[])
    state.pending_decision_blob = json.dumps({
        "action_pending": True, "response_type": "boolean",
        "message": "Mulligan down to 6 cards?",
        "your_hand": [{"name": "Island", "is_land": True}],
    })

    assert await pilot._answer_mulligan_from_the_engine_rule(session, state, None) is False
    session.call_tool.assert_not_awaited()


@pytest.mark.asyncio
async def test_an_ordinary_decision_is_left_alone(monkeypatch):
    """The other control: this must not swallow decisions that are not mulligans."""
    import json
    from unittest.mock import AsyncMock, MagicMock

    from magebench.pilot import pilot

    monkeypatch.delenv("MAGEBENCH_MULLIGAN", raising=False)
    session = MagicMock()
    session.call_tool = AsyncMock()
    state = pilot.PilotLoopState(history=[])
    state.pending_decision_blob = json.dumps({
        "action_pending": True, "response_type": "select",
        "message": "Choose an action", "options": [{"index": 0, "text": "Pass"}],
    })

    assert await pilot._answer_mulligan_from_the_engine_rule(session, state, None) is False
    session.call_tool.assert_not_awaited()
