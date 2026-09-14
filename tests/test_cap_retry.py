"""The cap-hit-with-a-call-open retry, and the state it must be able to reach.

The condition is a call cut off AT the completion cap: the parser finds a call, so
`finish_reason` is "tool_calls" and not "length", and what reaches the bridge is a
half-written name with empty arguments -- `choose` for `choose_action`. Measured at
19 / 12 / 47 / 53 such calls in the four corpora, of which 6 carried an
out-of-schema name, every one a prefix of a real one.

A check that only ever returns "the retry fired" is not a check. The second
scenario here is the one that proves the fatal path is still reachable: when the
redraw also hits the cap, the game must die exactly as it did before.
"""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from magebench.pilot.cap_retry import cap_hit_with_call_open, should_retry
from magebench.pilot.pilot import run_pilot_loop
from magebench.pilot.pilot_rendering import MAX_TOKENS
from magebench.pilot.pilot_state import PilotLoopState
from magebench.pilot.tool_error import ToolExecutionError

OFFERED = {"choose_action", "pass_priority"}


@pytest.fixture
def _no_prefetch():
    """Local copy of test_pilot.py's fixture: run_pilot_loop must not block on the
    bridge. Duplicated rather than moved to conftest, because moving it would touch
    a file three other branches are also editing this week."""
    with patch(
        "magebench.pilot.pilot._prefetch_first_action",
        new_callable=AsyncMock,
        return_value=("Game starting.", None, "{}"),
    ):
        yield


def _response(*, finish, completion, name, arguments):
    tool_call = MagicMock()
    tool_call.id = "call_x"
    tool_call.function.name = name
    tool_call.function.arguments = arguments
    choice = MagicMock()
    choice.finish_reason = finish
    choice.message.tool_calls = [tool_call] if name else []
    choice.message.content = None
    response = MagicMock()
    response.choices = [choice]
    response.usage = MagicMock(prompt_tokens=10, completion_tokens=completion)
    response.usage.prompt_tokens_details = None
    response.usage.completion_tokens_details = None
    return choice, response


def test_the_three_terms_are_each_necessary():
    """Drop any one and the condition must not match -- otherwise the trigger is
    broader than the population it was measured on."""
    hit, detail = cap_hit_with_call_open(
        *_response(finish="tool_calls", completion=MAX_TOKENS, name="choose", arguments="{}"),
        max_tokens=MAX_TOKENS, offered=OFFERED)
    assert hit and detail["tool"] == "choose"
    assert detail["arguments_empty"] and detail["name_is_prefix_of_offered"]

    # not at the cap
    hit, _ = cap_hit_with_call_open(
        *_response(finish="tool_calls", completion=MAX_TOKENS - 1, name="choose", arguments="{}"),
        max_tokens=MAX_TOKENS, offered=OFFERED)
    assert not hit

    # no call open (this is the finish_reason=length case the other handler owns)
    hit, _ = cap_hit_with_call_open(
        *_response(finish="length", completion=MAX_TOKENS, name="", arguments=None),
        max_tokens=MAX_TOKENS, offered=OFFERED)
    assert not hit

    # a real name with real arguments, at the cap: a long but COMPLETE call
    hit, _ = cap_hit_with_call_open(
        *_response(finish="tool_calls", completion=MAX_TOKENS, name="choose_action",
                   arguments='{"choice": "p1"}'),
        max_tokens=MAX_TOKENS, offered=OFFERED)
    assert not hit


def test_a_real_name_with_empty_arguments_at_the_cap_still_matches():
    """The arguments can be the half that was cut. Name offered, args empty."""
    hit, detail = cap_hit_with_call_open(
        *_response(finish="tool_calls", completion=MAX_TOKENS, name="choose_action", arguments="{}"),
        max_tokens=MAX_TOKENS, offered=OFFERED)
    assert hit and detail["name_offered"] and detail["arguments_empty"]


def test_unparseable_arguments_count_as_cut_off():
    hit, detail = cap_hit_with_call_open(
        *_response(finish="tool_calls", completion=MAX_TOKENS, name="choose_action",
                   arguments='{"choice": "p'),
        max_tokens=MAX_TOKENS, offered=OFFERED)
    assert hit and detail["arguments_empty"]


def test_one_redraw_per_decision_then_the_fatal_path():
    state = PilotLoopState(history=[])
    state.last_decision_seq = 7
    logger = MagicMock()
    assert should_retry(state, {"tool": "choose"}, logger=logger, temperature=0.6) is True
    assert should_retry(state, {"tool": "choose"}, logger=logger, temperature=0.6) is False
    # a NEW decision gets its own redraw
    state.last_decision_seq = 8
    assert should_retry(state, {"tool": "choose"}, logger=logger, temperature=0.6) is True


def test_temperature_zero_is_recorded_as_unable_to_differ():
    state = PilotLoopState(history=[])
    state.last_decision_seq = 1
    logger = MagicMock()
    should_retry(state, {"tool": "choose"}, logger=logger, temperature=0)
    assert any("temperature 0" in str(c) for c in logger.warning.call_args_list)


_TOOLS = [{"type": "function", "function": {"name": "pass_priority", "parameters": {}}},
          {"type": "function", "function": {"name": "choose_action", "parameters": {}}}]
_PENDING = json.dumps({"action_pending": True, "action_type": "GAME_SELECT",
                       "board": [{"name": "You", "life": 20}], "board_cursor": 1})


def _session(tool_calls):
    session = MagicMock()

    async def fake_call_tool(name, args):
        tool_calls.append((name, dict(args)))
        if name == "choose":
            raise ToolExecutionError("MCP tool choose failed: Unknown tool: choose")
        result = MagicMock()
        result.content = [MagicMock(text='{"game_over": true}')]
        result.isError = False
        return result

    session.call_tool = AsyncMock(side_effect=fake_call_tool)
    return session


@pytest.mark.asyncio
@pytest.mark.usefixtures("_no_prefetch")
async def test_the_redraw_fires_and_the_truncated_call_is_never_executed():
    """The retry's whole point: `choose` must not reach the bridge."""
    calls: list = []
    session = _session(calls)
    client = MagicMock()

    async def fake_create(**_kw):
        fake_create.n += 1
        if fake_create.n == 1:
            _c, r = _response(finish="tool_calls", completion=MAX_TOKENS,
                              name="choose", arguments="{}")
            return r
        _c, r = _response(finish="tool_calls", completion=20,
                          name="pass_priority", arguments="{}")
        return r

    fake_create.n = 0
    client.chat.completions.create = AsyncMock(side_effect=fake_create)
    game_log = MagicMock()

    with patch("magebench.pilot.pilot.auto_pass_loop", new_callable=AsyncMock):
        await asyncio.wait_for(
            run_pilot_loop(session=session, client=client, model="m",
                           system_prompt="s", tools=_TOOLS, prices={},
                           username="p", game_log=game_log),
            timeout=5)

    assert fake_create.n == 2, "the redraw did not happen"
    assert not any(name == "choose" for name, _ in calls), "the cut-off call was executed"
    rows = [c for c in game_log.emit.call_args_list if c.args and c.args[0] == "completion_truncated"]
    assert rows, "the occurrence was not recorded"
    assert rows[0].kwargs["outcome"] == "retry_after_cap"
    assert rows[0].kwargs["call_open"] is True


@pytest.mark.asyncio
@pytest.mark.usefixtures("_no_prefetch")
async def test_a_second_cap_hit_falls_through_to_the_fatal_path():
    """The control for the test above: the check must be able to return both states."""
    calls: list = []
    session = _session(calls)
    client = MagicMock()

    async def fake_create(**_kw):
        fake_create.n += 1
        _c, r = _response(finish="tool_calls", completion=MAX_TOKENS,
                          name="choose", arguments="{}")
        return r

    fake_create.n = 0
    client.chat.completions.create = AsyncMock(side_effect=fake_create)
    game_log = MagicMock()

    with (
        patch("magebench.pilot.pilot.auto_pass_loop", new_callable=AsyncMock),
        pytest.raises(ToolExecutionError, match="Unknown tool: choose"),
    ):
        await asyncio.wait_for(
            run_pilot_loop(session=session, client=client, model="m",
                           system_prompt="s", tools=_TOOLS, prices={},
                           username="p", game_log=game_log),
            timeout=5)

    assert fake_create.n == 2, "expected exactly one redraw before the fatal path"
    outcomes = [c.kwargs["outcome"] for c in game_log.emit.call_args_list
                if c.args and c.args[0] == "completion_truncated"]
    assert outcomes == ["retry_after_cap", "fatal_path"], outcomes
