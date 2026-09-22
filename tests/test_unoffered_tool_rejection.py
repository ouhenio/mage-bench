"""An unoffered tool name is rejected WITH THE TOOL LIST, not fatal -- tested through the real loop.

The plan's recorded decision (deck-general-plan.md: "Wrong-tool-name should be a rejection with the
tool list, same as wrong-argument"). Built on the schema eval's g14: a VALID `get_action_choices`
followed by an INVENTED `(http://127.0.0.1:5000/api/v2/popular)` in one response. Until now the
invented call reached the bridge, came back "Unknown tool", and the pilot re-raised -- game lost.

These drive `_process_tool_calls` itself, with the bridge (`execute_tool`) stood in for, so they
test the loop that decides, not a helper in isolation.
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace as NS

import pytest

import magebench.pilot.pilot as pilot
from magebench.pilot.pilot_state import PilotLoopState
from magebench.pilot.tool_error import ToolExecutionError

OFFERED = {"get_game_log", "pass_priority", "get_game_state", "get_oracle_text",
           "get_action_choices", "choose_action"}
INVENTED = "(http://127.0.0.1:5000/api/v2/popular)"


def _call(cid, name, args="{}"):
    return NS(id=cid, type="function", function=NS(name=name, arguments=args))


def _choice(*calls):
    return NS(finish_reason="tool_calls",
              message=NS(role="assistant", content="", tool_calls=list(calls)))


@pytest.fixture
def bridge(monkeypatch):
    """Stands in for the MCP bridge: records every name it is asked to run."""
    sent = []
    async def fake_execute_tool(session, name, args):
        sent.append(name)
        # FAITHFUL TO THE REAL BRIDGE, which answers a name it does not know with "Unknown tool" and
        # raises. A first draft of this stand-in accepted ANY name -- under which the not-fatal test
        # passed even with the fix removed, i.e. proved nothing.
        if name not in OFFERED:
            raise ToolExecutionError(f"MCP tool {name} failed: Unknown tool: {name}")
        return json.dumps({"action_pending": False, "success": True})
    monkeypatch.setattr(pilot, "execute_tool", fake_execute_tool)
    return sent


def _run(choice, state=None, reject_unoffered=True):
    state = state if state is not None else PilotLoopState(history=[])
    asyncio.run(pilot._process_tool_calls(None, choice, state, "Eval14", None, None,
                                          offered=OFFERED, reject_unoffered=reject_unoffered))
    return state


def _tool_results(state):
    return {m.get("tool_call_id"): m.get("content") for m in state.history if m.get("role") == "tool"}


def test_g14_the_valid_call_runs_and_the_invented_one_never_reaches_the_bridge(bridge):
    state = _run(_choice(_call("c1", "get_action_choices"), _call("c2", INVENTED)))
    assert bridge == ["get_action_choices"], f"bridge was asked to run {bridge}"
    results = _tool_results(state)
    assert "Unknown tool" in results["c2"]


def test_the_rejection_carries_the_tool_list__that_is_the_correction(bridge):
    state = _run(_choice(_call("c1", INVENTED)))
    err = json.loads(_tool_results(state)["c1"])["error"]
    for name in OFFERED:
        assert name in err, f"the rejection must name {name!r} so the model can correct"


def test_it_is_not_fatal__the_turn_completes(bridge):
    _run(_choice(_call("c1", INVENTED)))          # would raise before this change


def test_an_invented_name_anywhere_in_the_response_is_rejected(bridge):
    state = _run(_choice(_call("c1", "choose_action", '{"choice":"p1"}'),
                         _call("c2", "get_api_state"), _call("c3", INVENTED)))
    assert bridge == ["choose_action"]
    r = _tool_results(state)
    assert "Unknown tool" in r["c2"] and "Unknown tool" in r["c3"]


def test_only_offered_names_all_run(bridge):
    _run(_choice(_call("c1", "get_action_choices"), _call("c2", "get_game_state")))
    assert bridge == ["get_action_choices", "get_game_state"]


def test_SCOPE__a_real_bridge_error_on_an_OFFERED_tool_still_raises(monkeypatch):
    """Only unknown NAMES become rejections. g167 -- a real tool with a malformed argument --
    raised ToolExecutionError from the bridge, and that path is deliberately unchanged here:
    turning every bridge error into a rejection is a different decision with different risks."""
    async def failing(session, name, args):
        raise ToolExecutionError(f"MCP tool {name} failed: For input string: \"5")
    monkeypatch.setattr(pilot, "execute_tool", failing)
    with pytest.raises(ToolExecutionError):
        _run(_choice(_call("c1", "get_game_log", '{"n":"5"}')))


def test_BOUNDED__a_persistent_invented_name_ends_in_the_fatal_path(bridge):
    """A rejection never reaches the engine, so the engine's interaction cap cannot see it. Without
    this bound a model that keeps inventing names loops pilot-side until the game timeout -- which
    is what this change's first draft did, caught by tests/test_cap_retry.py."""
    state = PilotLoopState(history=[])
    for _ in range(pilot.MAX_CONSECUTIVE_UNOFFERED_REJECTIONS - 1):
        _run(_choice(_call("c", INVENTED)), state)          # rejected, not fatal
    with pytest.raises(ToolExecutionError, match="Unknown tool"):
        _run(_choice(_call("c", INVENTED)), state)          # the Nth is fatal
    assert bridge == [], "a rejection must never reach the bridge"


def test_the_count_is_CONSECUTIVE__a_valid_call_resets_it(bridge):
    state = PilotLoopState(history=[])
    for _ in range(3 * pilot.MAX_CONSECUTIVE_UNOFFERED_REJECTIONS):
        _run(_choice(_call("c", INVENTED)), state)
        _run(_choice(_call("v", "get_game_state")), state)  # a real call in between
    assert state.consecutive_unoffered_rejections == 0


def test_a_CAP_HIT_fall_through_is_NOT_rejected__cap_retry_keeps_its_contract(bridge):
    """reject_unoffered=False is the call site's value on a cap-hit: the stump goes to the bridge
    and dies, exactly as cap_retry's 'redraw once, then the fatal path' specifies."""
    with pytest.raises(ToolExecutionError, match="Unknown tool"):
        _run(_choice(_call("c", "choose")), reject_unoffered=False)
    assert bridge == ["choose"]
