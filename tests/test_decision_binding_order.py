"""The guarded arm's blob must be captured BEFORE the message builder consumes it.

WHY THIS FILE EXISTS. Job 9960 ran with MAGEBENCH_DECISION_SCHEMA=bound_choice and bound
NOTHING: 487 of 487 decisions recorded `reason: "no decision blob"`, which made the "guarded"
arm a second copy of the control. The grammar was fine and the controls were green -- the
controls replay recorded frames and hand the decision straight to the request builder, so they
never traverse the pilot loop, and the loop is where the defect lived.

`close_segment_if_needed` reads `state.pending_decision_blob` and sets it to None *whether or
not it cuts* -- deliberately, so a stalled turn prices one decision once rather than once per
LLM call. `_build_loop_messages` calls it. The binding site read the same field afterwards.
Two consumers, one mutable field, and the one that runs first nulls it.

The first test is the collision itself, with the real function and real state. The second is a
SOURCE-ORDER check, and that is a deliberate choice rather than laziness: the invariant is
"the read happens before the consume", which is a property of the loop's order and not of any
value a unit test can inspect after the fact. Driving the whole loop would need a server, an
engine and a display. So the order is asserted where it is written down.
"""

from __future__ import annotations

import importlib.util
import pathlib
import re

import pytest

from magebench.pilot import pilot

# The fixtures live in the sibling test module. Imported by path rather than by name because
# `tests/` is not a package and pytest's rootdir insertion is not guaranteed for a direct run.
_sib = pathlib.Path(__file__).with_name("test_context_segments.py")
_spec = importlib.util.spec_from_file_location("_seg_fixtures", _sib)
_seg = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_seg)
_decision_blob, _state_with_one_decision = _seg._decision_blob, _seg._state_with_one_decision

PILOT_SRC = pathlib.Path(pilot.__file__).read_text()


def test_the_message_builder_consumes_the_blob_so_a_later_read_sees_nothing(monkeypatch):
    """The collision, demonstrated: this is what the binding site used to observe."""
    state = _state_with_one_decision(monkeypatch, _decision_blob())
    assert state.pending_decision_blob is not None, "fixture must start with a decision in hand"
    captured_before = state.pending_decision_blob          # what the fix reads
    pilot.close_segment_if_needed(state, "system prompt")   # what _build_loop_messages calls
    assert state.pending_decision_blob is None, (
        "if this ever fails, close_segment_if_needed stopped consuming the blob and the capture "
        "below is merely harmless rather than necessary -- read this file's header before "
        "removing it")
    assert captured_before is not None, (
        "the pre-builder capture is the only value that survives the builder; a binding site "
        "reading state.pending_decision_blob after the builder sees None on EVERY call, which "
        "is job 9960: 487 of 487 decisions unbound")


def _line_of(pattern: str) -> int:
    m = re.search(pattern, PILOT_SRC, re.MULTILINE)   # ^ must mean line-start, not file-start
    assert m is not None, f"pattern not found in pilot.py: {pattern}"
    return PILOT_SRC[: m.start()].count("\n") + 1


def test_the_capture_precedes_the_builder_and_the_binding_site_reads_the_capture():
    capture = _line_of(r"^\s*decision_blob_for_binding = state\.pending_decision_blob")
    builder = _line_of(r"^\s*messages = await _build_loop_messages\(")
    read = _line_of(r"^\s*blob = decision_blob_for_binding")
    assert capture < builder, (
        f"the capture is at line {capture} and the message builder at {builder}: the builder "
        f"consumes the blob, so capturing after it captures None")
    assert builder < read, "sanity: the binding site should sit after the request is built"
    # And the old read must be gone, not merely shadowed.
    assert "blob = state.pending_decision_blob\n                parsed_blob" not in PILOT_SRC, (
        "the binding site still reads the consumed field directly")


def test_the_coverage_reason_for_a_missing_blob_is_still_reachable():
    """The `no decision blob` branch is legitimate for calls with no decision pending, so it
    must stay -- what was wrong was that it fired on 100% of calls, not that it exists."""
    assert '"reason": "no decision blob"' in PILOT_SRC


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
