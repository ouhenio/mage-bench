"""A capability the build offers and the run never uses must be visible.

`get_game_log` was offered for a whole corpus, carried a cursor for incremental
updates, and was called 113 times across 929 games -- 112 of them after the
bridge had closed. Nothing in any artifact said so, because "never called" had no
representation: no row, no zero, nothing to count. This is that zero.

Same absent-vs-empty discipline as `requested_but_unread`: a tool with no calls is
a count of zero, not a missing key.
"""

from magebench.pilot.pilot import _tally_tool_call
from magebench.pilot.pilot_state import PilotLoopState


def _state(offered):
    s = PilotLoopState(history=[])
    s.tool_usage = {n: {"calls": 0, "ok": 0, "failed": 0} for n in sorted(offered)}
    return s


def test_an_offered_tool_nobody_called_is_a_zero_not_a_missing_key():
    s = _state(["choose_action", "pass_priority", "get_game_log"])
    _tally_tool_call(s, "choose_action", ok=True)
    assert s.tool_usage["get_game_log"] == {"calls": 0, "ok": 0, "failed": 0}
    assert set(s.tool_usage) == {"choose_action", "pass_priority", "get_game_log"}


def test_successes_and_failures_are_counted_apart():
    s = _state(["choose_action"])
    _tally_tool_call(s, "choose_action", ok=True)
    _tally_tool_call(s, "choose_action", ok=False)
    _tally_tool_call(s, "choose_action", ok=True)
    assert s.tool_usage["choose_action"] == {"calls": 3, "ok": 2, "failed": 1}


def test_a_tool_that_was_never_offered_still_gets_a_row():
    """A hallucinated name is the thing most worth seeing, so it must not be
    dropped for failing to appear in the offered set. The offered list is emitted
    beside the tally, which is what keeps the two distinguishable."""
    s = _state(["choose_action"])
    _tally_tool_call(s, "ghost_pass", ok=False)
    assert s.tool_usage["ghost_pass"] == {"calls": 1, "ok": 0, "failed": 1}
    assert s.tool_usage["choose_action"]["calls"] == 0


def test_the_loop_fills_the_caller_s_dict_in_place():
    """The summary is emitted at game_end by the CALLER, so the count has to reach
    it without the loop returning anything -- and it has to survive the loop dying,
    since an aborted game is exactly the one whose capability usage is worth
    reading."""
    import inspect

    from magebench.pilot import pilot

    sig = inspect.signature(pilot.run_pilot_loop)
    assert "tool_usage_out" in sig.parameters
    assert sig.parameters["tool_usage_out"].default is None
    src = inspect.getsource(pilot.run_pilot_loop)
    assert "state.tool_usage = tool_usage_out if tool_usage_out is not None else {}" in src


def test_the_summary_does_not_depend_on_a_name_bound_inside_the_try():
    """A finally that reads a local from the try turns a bridge failure into a
    NameError that masks the real exception."""
    import inspect

    from magebench.pilot import pilot

    src = inspect.getsource(pilot.play_game if hasattr(pilot, "play_game") else pilot.main)
    # The emit must use the pre-declared list, never openai_tools.
    assert "offered=offered_tools" in inspect.getsource(pilot)
    assert "offered=sorted(t[\"function\"][\"name\"] for t in openai_tools" not in inspect.getsource(pilot)
