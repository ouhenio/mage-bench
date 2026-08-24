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


class TestTheSeedListIsExplicit:
    """MAGEBENCH_GAME_SEEDS must name one seed per game, or none at all.

    A session is the first place in this harness where "which seed did game 4
    get" has a non-obvious answer. Deriving it from a base plus an index reads
    fine until a batch is resumed or one bad game is re-run, at which point the
    derivation quietly re-deals a game the corpus already has -- and a duplicate
    deal is indistinguishable from a real one downstream.
    """

    def test_unset_means_every_game_is_unseeded(self, monkeypatch):
        from magebench.orchestration.orchestrator import _sequential_seeds

        monkeypatch.delenv("MAGEBENCH_GAME_SEEDS", raising=False)

        assert _sequential_seeds(3) == [None, None, None]

    def test_empty_is_treated_as_unset(self, monkeypatch):
        from magebench.orchestration.orchestrator import _sequential_seeds

        monkeypatch.setenv("MAGEBENCH_GAME_SEEDS", "")

        assert _sequential_seeds(2) == [None, None]

    def test_one_seed_per_game(self, monkeypatch):
        from magebench.orchestration.orchestrator import _sequential_seeds

        monkeypatch.setenv("MAGEBENCH_GAME_SEEDS", "901001, 901002 ,901003")

        assert _sequential_seeds(3) == [901001, 901002, 901003]

    def test_a_short_list_is_an_error_not_a_cycle(self, monkeypatch):
        import pytest

        from magebench.orchestration.orchestrator import _sequential_seeds

        monkeypatch.setenv("MAGEBENCH_GAME_SEEDS", "901001,901002")

        # Recycling would deal the same hands twice and still look like a corpus.
        with pytest.raises(AssertionError, match="one seed per game"):
            _sequential_seeds(5)

    def test_a_long_list_is_an_error_too(self, monkeypatch):
        import pytest

        from magebench.orchestration.orchestrator import _sequential_seeds

        monkeypatch.setenv("MAGEBENCH_GAME_SEEDS", "1,2,3,4")

        # Silently dropping the tail loses games the caller asked for.
        with pytest.raises(AssertionError, match="one seed per game"):
            _sequential_seeds(2)
