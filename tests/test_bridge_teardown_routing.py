"""A torn-down bridge is recorded but not written to errors.log; everything else still is.

Behavioural, not source-inspecting: the function is CALLED with a constructed error and the
two sinks are captured. A previous fix for this defect was tested with
`assert "..." in inspect.getsource(...)`, which passes whether or not the code works.

The pair matters more than either test. Remove the narrowing so every error is swallowed and
`test_other_failures_still_reach_errors_log` FAILS while the teardown test still passes --
so the suite distinguishes the two cases rather than merely noticing that something changed.
"""

import logging

from magebench.pilot.pilot_bridge import (
    BRIDGE_TEARDOWN_MARKER,
    _record_tool_execution_failure,
)
from magebench.pilot.tool_error import ToolExecutionError


class _Log:
    def __init__(self):
        self.events = []

    def emit(self, kind, **kw):
        self.events.append((kind, kw))


def _run(error):
    """Call the real function; return (game-log events, errors.log lines)."""
    game_log, written = _Log(), []
    _record_tool_execution_failure(
        error,
        "Eval00",
        None,
        game_log,
        logger=logging.getLogger("t"),
        log_error_fn=lambda _lg, _d, _u, msg: written.append(msg),
    )
    return game_log.events, written


def test_teardown_is_recorded_but_not_written_to_errors_log():
    events, written = _run(ToolExecutionError(f"MCP tool get_game_state failed: {BRIDGE_TEARDOWN_MARKER}"))
    assert written == [], f"a torn-down bridge must not reach errors.log, got {written!r}"
    assert [k for k, _ in events] == ["llm_error"], "the event must still be recorded in the game log"


def test_other_failures_still_reach_errors_log():
    """The narrowing's other half. This is the test the over-broad version fails."""
    events, written = _run(ToolExecutionError("MCP tool choose_action failed: connection reset by peer"))
    assert len(written) == 1, f"a real failure must still reach errors.log, got {written!r}"
    assert "Fatal tool error" in written[0]
    assert [k for k, _ in events] == ["llm_error"]


def test_unknown_tool_name_is_still_fatal():
    """The policy failure that killed games in the OPD arms must not be swallowed here."""
    _, written = _run(ToolExecutionError("MCP tool get_active_choices failed: Unknown tool: get_active_choices"))
    assert len(written) == 1, "an invented tool name is a POLICY failure and stays fatal"


def test_the_marker_is_the_string_the_engine_emits():
    """Guards the failure direction: if the wording drifts, teardowns become fatal again
    (over-recording, the safe direction) rather than silently matching something else."""
    assert BRIDGE_TEARDOWN_MARKER == "Bridge processor is shut down"
