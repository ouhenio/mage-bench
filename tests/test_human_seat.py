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


def _leak_frame():
    """karn-interface's leak lens: exactly one hand, on the WRONG player."""
    return _frame([
        {"name": "Human", "is_you": False, "hand_size": 5},
        {"name": "EngineAI", "is_you": True, "hand": [{"name": "Mountain"}], "hand_size": 1},
    ])


def test_refuses_a_hand_on_the_wrong_player():
    """The count check passes here -- one hand array -- and the frame is still a
    leak. Only an expectation held OUTSIDE the payload catches it."""
    check_frame_invariants(_leak_frame())          # count check alone: passes
    with pytest.raises(AdapterInvariantError, match="this adapter serves 'Human'"):
        check_frame_invariants(_leak_frame(), "Human")


def test_refuses_is_you_on_the_wrong_player():
    frame = _frame([
        {"name": "Human", "is_you": False, "hand": [{"name": "Plains"}]},
        {"name": "EngineAI", "is_you": True, "hand_size": 7},
    ])
    with pytest.raises(AdapterInvariantError, match="is_you names"):
        check_frame_invariants(frame, "Human")


def test_accepts_the_right_player_with_the_seat_name_given():
    """Positive control for the seat check: the good frame still passes."""
    check_frame_invariants(_frame([
        {"name": "Human", "is_you": True, "hand": [{"name": "Plains"}], "hand_size": 1},
        {"name": "EngineAI", "is_you": False, "hand_size": 7},
    ]), "Human")


class _StubSession:
    """Records tool calls so the concede path can be exercised without a JVM."""

    def __init__(self, result=None):
        self.calls = []
        self._result = result if result is not None else {"game_seq": 42}

    def call_tool_json(self, name, arguments=None, timeout=None):
        self.calls.append((name, arguments))
        return dict(self._result)


def test_concede_calls_the_bridge_and_emits():
    """A seat must be able to END its game, not only stop answering.

    Without this the engine sits on a pending prompt until the job's wall clock
    kills it: no game_end, no winner, and a record that says the job expired.
    """
    from magebench.play.human_seat import SeatDriver

    events = EventStream()
    session = _StubSession()
    driver = SeatDriver(session, events, seat_player="Human")
    result = driver.concede()

    assert session.calls == [("concede", {})]
    assert result == {"game_seq": 42}
    _, backlog = events.subscribe(0)
    # No frame has been seen, so there is no seq to stamp and the field is
    # honestly None -- but it is LABELLED, so a client can tell "not seen yet"
    # from "the tool did not say".
    assert [(e[1], e[2]) for e in backlog] == [
        ("conceded", {"game_seq": None, "game_seq_source": "adapter_last_seen"})
    ]


def test_concede_stamps_the_last_seq_the_adapter_saw():
    """ConcedeTool returns no game_seq, so passing its result through emitted a
    null on an event the client had bound to. The adapter stamps what it saw."""
    from magebench.play.human_seat import SeatDriver

    events = EventStream()
    driver = SeatDriver(_StubSession(), events, seat_player="Human")
    driver.emit_frame({
        "action_pending": True,
        "game_seq": 117,
        "board": [
            {"name": "Human", "is_you": True, "hand": [{"name": "Plains"}]},
            {"name": "EngineAI", "is_you": False, "hand_size": 7},
        ],
    })
    driver.concede()
    _, backlog = events.subscribe(0)
    conceded = [e[2] for e in backlog if e[1] == "conceded"]
    assert conceded == [{"game_seq": 117, "game_seq_source": "adapter_last_seen"}]


def test_concede_wakes_a_driver_parked_on_a_decision():
    """The loop blocks in _actions.get(); conceding must release it, or the seat
    holds its slot until the clock runs out anyway."""
    from magebench.play.human_seat import SeatDriver

    driver = SeatDriver(_StubSession(), EventStream(), seat_player="Human")
    driver.concede()
    assert driver._actions.get_nowait() is None
