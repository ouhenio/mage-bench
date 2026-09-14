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


def _one_call(name, arguments):
    tool_call = MagicMock()
    tool_call.id = f"call_{name}"
    tool_call.function.name = name
    tool_call.function.arguments = arguments
    return tool_call


def _response(*, finish, completion, name, arguments, repeats_before=0):
    """`repeats_before` prepends N complete pass_priority calls, which is the
    degenerate-loop shape run A found: many identical calls and then a stump."""
    calls = [_one_call("pass_priority", "{}") for _ in range(repeats_before)]
    if name:
        calls.append(_one_call(name, arguments))
    choice = MagicMock()
    choice.finish_reason = finish
    choice.message.tool_calls = calls
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


def test_the_live_path_and_a_census_agree_on_the_same_call():
    """One predicate, two wrappers. If these ever disagree, two counts of this
    population are two different objects and the reconciliation is void."""
    from magebench.pilot.cap_retry import cap_hit_from_trace_row

    choice, response = _response(finish="tool_calls", completion=MAX_TOKENS,
                                 name="choose", arguments="{}")
    live_hit, live_detail = cap_hit_with_call_open(
        choice, response, max_tokens=MAX_TOKENS, offered=OFFERED)

    row = {
        "type": "llm_call",
        "request": {
            "model": "m",
            "max_tokens": MAX_TOKENS,
            "tools": [{"type": "function", "function": {"name": n, "parameters": {}}}
                      for n in sorted(OFFERED)],
        },
        "response": {
            "usage": {"completion_tokens": MAX_TOKENS},
            "choices": [{"finish_reason": "tool_calls",
                         "message": {"tool_calls": [
                             {"function": {"name": "choose", "arguments": "{}"}}]}}],
        },
    }
    census_hit, census_detail = cap_hit_from_trace_row(row)

    assert live_hit is census_hit is True
    for key in ("tool", "arguments_empty", "name_offered", "name_is_prefix_of_offered",
                "completion_tokens", "max_tokens", "finish_reason"):
        assert live_detail[key] == census_detail[key], key


def test_a_row_with_no_max_tokens_is_undecidable_not_false():
    """A census must not report zero on rows it could not judge."""
    from magebench.pilot.cap_retry import cap_hit_from_trace_row

    hit, detail = cap_hit_from_trace_row({
        "type": "llm_call",
        "request": {"model": "m", "tools": []},
        "response": {"usage": {"completion_tokens": 10},
                     "choices": [{"finish_reason": "tool_calls", "message": {}}]},
    })
    assert hit is False
    assert detail["undecidable"] == "request carries no max_tokens"


def test_the_census_reads_the_cap_from_the_row_not_from_a_constant():
    """A corpus can hold more than one max_tokens; keying on today's constant
    would silently misclassify yesterday's games."""
    from magebench.pilot.cap_retry import cap_hit_from_trace_row

    row = {
        "type": "llm_call",
        "request": {"model": "m", "max_tokens": 2048,
                    "tools": [{"type": "function", "function": {"name": "choose_action"}}]},
        "response": {"usage": {"completion_tokens": 2048},
                     "choices": [{"finish_reason": "tool_calls",
                                  "message": {"tool_calls": [
                                      {"function": {"name": "choose", "arguments": "{}"}}]}}]},
    }
    hit, detail = cap_hit_from_trace_row(row)
    assert hit and detail["max_tokens"] == 2048 and detail["completion_tokens"] == 2048


def test_a_stump_after_a_repeat_loop_is_counted_and_not_retried():
    """Run A: 5 responses of 14,442 are 68 x pass_priority with empty args filling
    the budget, and the GAME SURVIVES them (g175 seq 120/122/124). Empty args plus
    a cap hit is the three-term population, so it must still be counted -- but
    redrawing a loop is a round trip into the same loop."""
    hit, detail = cap_hit_with_call_open(
        *_response(finish="tool_calls", completion=MAX_TOKENS, name="pass",
                   arguments="{}", repeats_before=65),
        max_tokens=MAX_TOKENS, offered=OFFERED)
    assert hit is True, "a multi-call stump is still in the population"
    assert detail["n_tool_calls"] == 66
    assert detail["retry_eligible"] is False


def test_the_single_stump_still_retries():
    """The positive control for the test above: the fourth term must not swallow
    the case the retry exists for."""
    hit, detail = cap_hit_with_call_open(
        *_response(finish="tool_calls", completion=MAX_TOKENS, name="choose",
                   arguments="{}"),
        max_tokens=MAX_TOKENS, offered=OFFERED)
    assert hit is True
    assert detail["n_tool_calls"] == 1
    assert detail["retry_eligible"] is True


def test_the_population_is_unchanged_by_the_fourth_term():
    """cap-hit/2 must not move a number reconciled against cap-hit/1. `matched`
    is still the three terms; the fourth lives in detail only."""
    from magebench.pilot.cap_retry import PREDICATE_VERSION
    assert PREDICATE_VERSION == "cap-hit/2"
    for repeats in (0, 1, 65):
        hit, _ = cap_hit_with_call_open(
            *_response(finish="tool_calls", completion=MAX_TOKENS, name="choose",
                       arguments="{}", repeats_before=repeats),
            max_tokens=MAX_TOKENS, offered=OFFERED)
        assert hit is True, repeats


@pytest.mark.asyncio
@pytest.mark.usefixtures("_no_prefetch")
async def test_the_loop_counts_a_multicall_stump_without_redrawing_it():
    calls: list = []
    session = _session(calls)
    client = MagicMock()

    async def fake_create(**_kw):
        fake_create.n += 1
        if fake_create.n == 1:
            return _response(finish="tool_calls", completion=MAX_TOKENS,
                             name="pass", arguments="{}", repeats_before=65)[1]
        return _response(finish="tool_calls", completion=20,
                         name="pass_priority", arguments="{}")[1]

    fake_create.n = 0
    client.chat.completions.create = AsyncMock(side_effect=fake_create)
    game_log = MagicMock()

    with patch("magebench.pilot.pilot.auto_pass_loop", new_callable=AsyncMock):
        await asyncio.wait_for(
            run_pilot_loop(session=session, client=client, model="m",
                           system_prompt="s", tools=_TOOLS, prices={},
                           username="p", game_log=game_log),
            timeout=5)

    rows = [c for c in game_log.emit.call_args_list
            if c.args and c.args[0] == "completion_truncated"]
    assert rows, "the occurrence was not counted"
    assert rows[0].kwargs["outcome"] == "not_retried_multicall"
    assert rows[0].kwargs["n_tool_calls"] == 66
    assert rows[0].kwargs["call_open"] is True
