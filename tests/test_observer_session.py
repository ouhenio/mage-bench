"""Tests for the keepAlive observer session used by sequential batches."""

from __future__ import annotations

import json
from pathlib import Path

from magebench.orchestration.observer_session import ObserverSession


class _FakeStdin:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def write(self, data: str) -> int:
        self.lines.append(data)
        return len(data)

    def flush(self) -> None:
        pass

    def close(self) -> None:
        pass


class _FakeProc:
    stdin = object()


def _session() -> tuple[ObserverSession, _FakeStdin]:
    session = ObserverSession.__new__(ObserverSession)
    stdin = _FakeStdin()
    session._stdin = stdin
    session.health_port = 0
    return session, stdin


def _sent(stdin: _FakeStdin) -> dict:
    assert len(stdin.lines) == 1
    return json.loads(stdin.lines[0])


class TestTheSeedTravelsWithTheGame:
    """One command per game, so what varies per game must be in the command.

    The whole reason a server can host a batch is that the seed stopped being a
    JVM property. If it leaked back into the launch environment, every game in a
    session would be dealt the same hand -- and a corpus of one deal repeated
    looks exactly like a corpus, which is why this is worth a test rather than a
    comment.
    """

    def test_the_seed_is_sent_per_game(self):
        session, stdin = _session()

        session.start_game(Path("/tmp/g1"), {"players": []}, game_seed=901001)

        assert _sent(stdin)["gameSeed"] == 901001

    def test_an_unseeded_game_omits_the_field_entirely(self):
        session, stdin = _session()

        session.start_game(Path("/tmp/g1"), {"players": []})

        # Absent, not null and not 0. An unseeded game must leave the RNG stream
        # alone, and 0 is a legal seed -- collapsing the two would silently give
        # every "unseeded" game in a session the same deal.
        assert "gameSeed" not in _sent(stdin)

    def test_seed_zero_survives(self):
        session, stdin = _session()

        session.start_game(Path("/tmp/g1"), {"players": []}, game_seed=0)

        sent = _sent(stdin)
        assert "gameSeed" in sent and sent["gameSeed"] == 0

    def test_each_game_sends_its_own_directory(self):
        session, stdin = _session()

        session.start_game(Path("/tmp/g1"), {"players": []}, game_seed=1)
        session.start_game(Path("/tmp/g2"), {"players": []}, game_seed=2)

        sent = [json.loads(line) for line in stdin.lines]
        assert [s["gameDir"] for s in sent] == ["/tmp/g1", "/tmp/g2"]
        assert [s["gameSeed"] for s in sent] == [1, 2]
