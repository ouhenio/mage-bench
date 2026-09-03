"""Unit tests for the human-seat adapter's frame invariants and event stream."""

import pytest

from magebench.play.human_seat import AdapterInvariantError, EventStream, check_frame_invariants


def _frame(players):
    return {"action_pending": True, "board": players}


def test_accepts_a_frame_with_exactly_one_hand():
    """The positive control: the check must be able to return the GOOD state.

    A refusal-only test proves nothing -- a predicate that always raises passes it.
    """
    check_frame_invariants(_frame([
        {"name": "Human", "is_you": True, "hand": [{"name": "Plains"}], "hand_size": 1},
        {"name": "EngineAI", "is_you": False, "hand_size": 7},
    ]))


def test_refuses_a_second_hand_array():
    with pytest.raises(AdapterInvariantError, match="hidden-information leak"):
        check_frame_invariants(_frame([
            {"name": "Human", "hand": []},
            {"name": "EngineAI", "hand": []},
        ]))


def test_refuses_zero_hand_arrays():
    """Zero is a bug too: it is what a seat sees when its own player id does not
    resolve, and it renders as a legal-looking board with no hand."""
    with pytest.raises(AdapterInvariantError, match="did not resolve"):
        check_frame_invariants(_frame([{"name": "Human"}, {"name": "EngineAI"}]))


def test_refuses_a_frame_with_no_board():
    """board_unchanged is the shape this guards: the adapter never sends
    board_cursor, so a board-less frame means that promise broke upstream."""
    with pytest.raises(AdapterInvariantError, match="no board"):
        check_frame_invariants({"action_pending": True, "board_unchanged": True})


def test_your_hand_is_not_counted_as_a_board_hand():
    """`your_hand` is a separate mulligan-time field; only board[] is in scope."""
    check_frame_invariants({
        "action_pending": True,
        "your_hand": [{"name": "Plains"}],
        "board": [
            {"name": "Human", "hand": [{"name": "Plains"}]},
            {"name": "EngineAI", "hand_size": 7},
        ],
    })


def test_event_stream_replays_only_events_after_last_id():
    events = EventStream()
    first = events.emit("frame", {"n": 1})
    events.emit("phase", {"n": 2})
    _, backlog = events.subscribe(first)
    assert [e[2]["n"] for e in backlog] == [2]


def test_event_stream_gives_a_fresh_client_no_backlog():
    """A fresh connect is answered with authoritative state, not a replay of the
    whole game -- so subscribe(None) must hand back nothing."""
    events = EventStream()
    events.emit("frame", {"n": 1})
    _, backlog = events.subscribe(None)
    assert backlog == []
