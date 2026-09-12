"""The opponent's play since this seat last acted, and the four ways to get it wrong.

The engine writes the play-by-play and exposes it as get_game_log with a CURSOR for
incremental updates. Nothing pushed it and the model never pulled it -- 113 calls across 929
corpus games, 112 of them after the bridge closed at game end. So every checkpoint so far
inferred the opponent's play from snapshot deltas, where a Bolt to the face and a chump-blocked
Bolt are indistinguishable two snapshots later.
"""
import json

import pytest

from magebench.pilot import log_delta as ld
from magebench.pilot.pilot_rendering import render_for_pilot
from magebench.pilot.pilot_state import PilotLoopState

FRAME = {
    "action_pending": True,
    "response_type": "select",
    "message": "Play spells and abilities",
    "board": [{"name": "Eval00", "life": 20, "hand": []},
              {"name": "XMage AI", "life": 20, "hand": []}],
    "choices": [{"index": 0, "text": "Play a land", "id": "o0"}],
}


class _Session:
    """A session whose get_game_log returns scripted chunks and records the cursors asked for."""

    def __init__(self, chunks):
        self.chunks = list(chunks)
        self.cursors = []

    async def call_tool(self, name, args):
        raise AssertionError("unused")


async def _inject(session_chunks, state, frame=None, cursors_seen=None):
    """Drive fetch_and_inject with get_game_log stubbed at the execute_tool seam."""
    import magebench.pilot.pilot_bridge as pb
    calls = cursors_seen if cursors_seen is not None else []
    chunks = list(session_chunks)

    async def fake_execute_tool(session, name, args):
        assert name == "get_game_log", name
        calls.append(args.get("cursor"))
        return json.dumps(chunks.pop(0))

    orig = pb.execute_tool
    pb.execute_tool = fake_execute_tool
    try:
        return await ld.fetch_and_inject(None, state, json.dumps(frame or FRAME))
    finally:
        pb.execute_tool = orig


def _state(on=True, cursor=None):
    s = PilotLoopState(history=[])
    s.log_delta_on = on
    s.log_cursor = cursor
    return s


# --- the majority case, first, because it is 47.3% of real intervals ------------------------

@pytest.mark.asyncio
async def test_an_EMPTY_interval_carries_NO_BLOCK(monkeypatch):
    """30,325 of 64,121 measured intervals are empty. A heading on all of them would be the
    auto-resolve defect inverted: noise the model learns to skip, and then skips when real."""
    st = _state()
    out = await _inject([{"log": "", "cursor": 12}], st)
    data = json.loads(out)
    assert ld.DELTA_FIELD not in data, "an empty interval must be ABSENT, not an empty heading"
    rendered, _ = render_for_pilot(out, None, set(), 1)
    assert ld.DELTA_HEADING not in rendered


@pytest.mark.asyncio
async def test_the_cursor_ADVANCES_even_on_an_empty_interval():
    """Otherwise the next frame re-shows everything since the last non-empty one."""
    st = _state(cursor=5)
    await _inject([{"log": "", "cursor": 12}], st)
    assert st.log_cursor == 12


# --- the opponent's line, verbatim ----------------------------------------------------------

@pytest.mark.asyncio
async def test_a_frame_after_an_opponent_cast_carries_THAT_LINE_verbatim():
    st = _state()
    line = "XMage AI cast Bloodghast"
    out = await _inject([{"log": line, "cursor": 20}], st)
    rendered, _ = render_for_pilot(out, None, set(), 1)
    assert line in rendered, "the engine's own line, not a summary"
    assert rendered.index(ld.DELTA_HEADING) < rendered.index("## Decision"), (
        "what happened comes before what to decide")


@pytest.mark.asyncio
async def test_the_source_is_the_engine_log_and_nothing_is_invented():
    st = _state()
    lines = ["XMage AI cast Bloodghast", "Eval00 lost 3 life"]
    out = await _inject([{"log": "\n".join(lines), "cursor": 9}], st)
    block = json.loads(out)[ld.DELTA_FIELD]
    body = block.split("\n", 1)[1]
    assert body.splitlines() == lines, "lines pass through in order, unsummarised"


# --- bounded, and it says so -----------------------------------------------------------------

@pytest.mark.asyncio
async def test_truncation_is_ANNOUNCED_not_silent(monkeypatch):
    monkeypatch.setenv("MAGEBENCH_OPPONENT_LOG_DELTA_CHARS", "60")
    st = _state()
    lines = [f"XMage AI did thing number {i}" for i in range(10)]
    out = await _inject([{"log": "\n".join(lines), "cursor": 3}], st)
    block = json.loads(out)[ld.DELTA_FIELD]
    assert "not shown" in block, "a silently shortened recap cannot be calibrated against"
    assert lines[-1] in block, "the most recent lines are the ones kept"
    assert lines[0] not in block


def test_the_cap_keeps_WHOLE_lines():
    """Half an engine line reads as a different event."""
    lines = ["aaaa", "bbbb", "cccc"]
    kept, dropped = ld.clip(lines, 6)
    assert kept == ["cccc"] and dropped == 2
    assert all(k in lines for k in kept)


def test_the_default_cap_is_the_measured_one():
    """2,000 -- ~5x p99 (380 chars), truncating 3 of 64,121 intervals. Pinned so a change is
    deliberate and has to argue with the table in the module docstring."""
    assert ld.DEFAULT_MAX_CHARS == 2000


# --- the setting ----------------------------------------------------------------------------

def test_default_OFF_explicit_ON(monkeypatch):
    """The corpus and the three evals ran without this and must stay reproducible."""
    monkeypatch.delenv("MAGEBENCH_OPPONENT_LOG_DELTA", raising=False)
    assert ld.enabled() is False
    monkeypatch.setenv("MAGEBENCH_OPPONENT_LOG_DELTA", "1")
    assert ld.enabled() is True
    monkeypatch.setenv("MAGEBENCH_OPPONENT_LOG_DELTA", "0")
    assert ld.enabled() is False


def test_a_malformed_setting_RAISES(monkeypatch):
    monkeypatch.setenv("MAGEBENCH_OPPONENT_LOG_DELTA", "true")
    with pytest.raises(ValueError, match="neither '0' nor '1'"):
        ld.enabled()


@pytest.mark.asyncio
async def test_off_means_the_frame_is_UNTOUCHED():
    st = _state(on=False)
    frame_json = json.dumps(FRAME)
    out = await ld.fetch_and_inject(None, st, frame_json)
    assert out == frame_json, "off must not even reshape the json"


# --- the control mtg-0f would have missed, and the one that matters most ---------------------

@pytest.mark.asyncio
async def test_the_interval_is_correct_ACROSS_an_auto_resolved_decision():
    """With the auto-resolve flag on, 46.6% of decisions are priority windows the harness
    answers itself. If the cursor does not advance across those, every later shown frame
    repeats lines already passed or skips them -- the majority of intervals, not a corner.

    Shown frame -> auto-resolved frame -> shown frame, with the engine emitting one line in
    each interval. The second shown frame must carry ONLY its own line.
    """
    st = _state(cursor=None)
    cursors = []
    await _inject([{"log": "line A", "cursor": 1}], st, cursors_seen=cursors)
    await _inject([{"log": "line B", "cursor": 2}], st, cursors_seen=cursors)   # auto-resolved
    out = await _inject([{"log": "line C", "cursor": 3}], st, cursors_seen=cursors)
    assert cursors == [None, 1, 2], f"the cursor must advance each time, got {cursors}"
    block = json.loads(out)[ld.DELTA_FIELD]
    assert "line C" in block
    assert "line A" not in block and "line B" not in block, (
        "a line already passed must not be shown twice")


def test_every_record_decision_seq_SITE_also_injects():
    """The invariant, asserted against the shipped source rather than remembered.

    The cursor is correct only if every decision-bearing path injects. There are five such
    paths across two modules -- including the two harness recovery passes -- and missing one
    is invisible until a transcript repeats a line. So this scans the source: every
    `record_decision_seq(state, result_text)` must be immediately preceded by a
    fetch_and_inject of the same result_text.
    """
    import pathlib
    root = pathlib.Path(ld.__file__).parent
    sites = 0
    for f in ("pilot.py", "pilot_recovery.py"):
        lines = (root / f).read_text().splitlines()
        for i, line in enumerate(lines):
            if line.strip() != "record_decision_seq(state, result_text)":
                continue
            sites += 1
            window = "\n".join(lines[max(0, i - 12):i])
            assert "fetch_and_inject(session, state, result_text)" in window, (
                f"{f}:{i + 1} stamps the decision seq without advancing the log cursor")
    assert sites == 5, f"expected 5 decision-bearing sites, found {sites}"
