"""Tests for the sequential batch runner's failure isolation."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from magebench.orchestration import sequential_batch
from magebench.orchestration.config import Config, CpuPlayer


class _FakeObserver:
    """Stands in for the keepAlive observer JVM, failing on chosen games."""

    def __init__(self, fail_on: set[int]) -> None:
        self.fail_on = fail_on
        self.started: list[tuple[str, int | None]] = []
        self.started_full: list[dict] = []
        self.closed = False
        self.log_path = Path("/tmp/observer.log")
        self.proc = _FakeProc()

    def start_game(self, game_dir, players_config, **kwargs):
        self.started.append((game_dir.name, kwargs.get("game_seed")))
        self.started_full.append({"players_config": players_config, **kwargs})

    def wait_for_ready(self, game_dir, timeout=0):
        return "table"

    def wait_for_watching(self, game_dir, timeout=0):
        pass

    def wait_for_game_end(self, game_dir, timeout=0):
        if len(self.started) in self.fail_on:
            raise RuntimeError("observer /wait-for-game-end returned: timeout")

    def close(self):
        self.closed = True


class _FakeProc:
    pid = -1

    def wait(self, timeout=0):
        return 0


@pytest.fixture
def batch_env(tmp_path, monkeypatch):
    observers: list[_FakeObserver] = []

    def _fake_observer(project_root, config, port, observer_log, health_port_file):
        observers.append(_FakeObserver(fail_on=getattr(_fake_observer, "fail_on", set())))
        return observers[-1]

    monkeypatch.setattr(sequential_batch, "_start_observer", _fake_observer)
    monkeypatch.setattr(sequential_batch, "_start_server", lambda *a, **k: _FakeProc())
    monkeypatch.setattr(sequential_batch, "modify_server_config", lambda **k: None)
    monkeypatch.setattr(sequential_batch, "wait_for_port", lambda *a, **k: True)
    monkeypatch.setattr(sequential_batch, "write_game_meta", lambda *a, **k: None)
    monkeypatch.setattr(sequential_batch, "kill_tree", lambda pid: None)
    monkeypatch.setattr(
        sequential_batch,
        "find_available_port",
        lambda start: type("R", (), {"port": 17171, "release": lambda self: None})(),
    )
    return _fake_observer, observers, tmp_path


def _config() -> Config:
    config = Config()
    config.cpu_players = [CpuPlayer(name="A", deck="a.dck"), CpuPlayer(name="B", deck="b.dck")]
    config.timestamp = "20260819_200000"
    return config


class TestOneBadGameDoesNotCostTheSession:
    """The card load is the expensive thing; a failed game must not discard it.

    Under one-server-per-game a failure cost one game because the server died
    with it. Under one server per session the server is a shared, expensive
    asset -- 25s of card loading that the remaining games have already paid for
    -- so aborting the session on a single bad game would throw away exactly the
    thing this runner exists to amortise.
    """

    def test_the_session_continues_past_a_failed_game(self, batch_env, tmp_path):
        fake_observer, observers, _ = batch_env
        fake_observer.fail_on = {2}

        result = sequential_batch.run_sequential_batch(
            _config(), tmp_path, tmp_path / "logs", [1, 2, 3, 4], pm=_FakePm()
        )

        assert len(result.completed) == 3
        assert len(result.failed) == 1
        assert result.attempted == 4
        # Games 3 and 4 ran AFTER the failure, on the same server.
        assert len(observers[0].started) == 4

    def test_every_game_gets_its_own_directory_and_seed(self, batch_env, tmp_path):
        fake_observer, observers, _ = batch_env
        fake_observer.fail_on = set()

        result = sequential_batch.run_sequential_batch(
            _config(), tmp_path, tmp_path / "logs", [901001, 901002, 901001], pm=_FakePm()
        )

        names = [name for name, _ in observers[0].started]
        seeds = [seed for _, seed in observers[0].started]
        assert len(set(names)) == 3, "two games sharing a directory would overwrite each other"
        assert seeds == [901001, 901002, 901001], "the repeated seed must survive verbatim"
        assert len(result.completed) == 3

    def test_the_observer_is_closed_even_when_a_game_fails(self, batch_env, tmp_path):
        fake_observer, observers, _ = batch_env
        fake_observer.fail_on = {1}

        sequential_batch.run_sequential_batch(
            _config(), tmp_path, tmp_path / "logs", [1], pm=_FakePm()
        )

        assert observers[0].closed, "a leaked observer JVM holds a port and a display"

    def test_an_unseeded_session_passes_none_through(self, batch_env, tmp_path):
        fake_observer, observers, _ = batch_env
        fake_observer.fail_on = set()

        sequential_batch.run_sequential_batch(
            _config(), tmp_path, tmp_path / "logs", [None, None], pm=_FakePm()
        )

        assert [seed for _, seed in observers[0].started] == [None, None]


class _FakePm:
    def start_jvm_process(self, **kwargs):
        return _FakeProc()

    def start_process(self, **kwargs):
        return _FakeProc()


def test_subprocess_timeout_on_close_is_survivable(batch_env, tmp_path, monkeypatch):
    """A wedged observer is killed rather than allowed to hang the session teardown."""
    fake_observer, observers, _ = batch_env
    fake_observer.fail_on = set()
    killed: list[int] = []
    monkeypatch.setattr(sequential_batch, "kill_tree", killed.append)

    def _hang(timeout=0):
        raise subprocess.TimeoutExpired(cmd="observer", timeout=timeout)

    original = sequential_batch._start_observer

    def _observer_that_hangs(*args, **kwargs):
        obs = original(*args, **kwargs)
        obs.proc.wait = _hang
        return obs

    monkeypatch.setattr(sequential_batch, "_start_observer", _observer_that_hangs)

    sequential_batch.run_sequential_batch(
        _config(), tmp_path, tmp_path / "logs", [1], pm=_FakePm()
    )

    assert killed, "a hung observer must be killed, not waited on forever"


class TestTwoSessionsNeverShareASessionDirectory:
    """Sessions launched in the same second must not share their scratch space.

    This is the game-directory defect in the one place the fix was not applied.
    The session directory holds health_port and observer.log, and health_port is
    how a session finds ITS observer. Eight sessions sharing one directory means
    eight observers writing one health_port, so a session reads a port another
    session just overwrote and drives somebody else's observer -- or a dead one.

    Measured before the fix: 8 sessions, 0 of 40 games completed, failing as
    "timed out", "Connection reset by peer" and "Connection refused". Nothing
    crashed, and the server logs inside were claimed and so looked healthy as
    server-1.log through server-8.log. The directory listing was the only place
    the collision was visible at all.
    """

    def test_sessions_with_one_timestamp_get_one_directory_each(self, batch_env, tmp_path):
        fake_observer, observers, _ = batch_env
        fake_observer.fail_on = set()
        log_dir = tmp_path / "logs"

        seen = []
        for _ in range(3):
            config = _config()          # same timestamp every time, as in a same-second launch
            sequential_batch.run_sequential_batch(config, tmp_path, log_dir, [1], pm=_FakePm())
            seen.append(sorted(p.name for p in log_dir.glob("session_*")))

        sessions = sorted(p.name for p in log_dir.glob("session_*"))
        assert len(sessions) == 3, f"three sessions produced {len(sessions)} directories: {sessions}"

    def test_the_first_session_keeps_the_plain_name(self, batch_env, tmp_path):
        fake_observer, observers, _ = batch_env
        fake_observer.fail_on = set()
        log_dir = tmp_path / "logs"

        sequential_batch.run_sequential_batch(_config(), tmp_path, log_dir, [1], pm=_FakePm())

        assert [p.name for p in log_dir.glob("session_*")] == ["session_20260819_200000"]


class TestAManifestIsConsumedPerGame:
    """Five games from a five-config manifest must play five configs, not the first one five times.

    Measured by ranokau at 100 games before the fix: 24 of 24 groups had all five
    games on the same deck pair. The corpus was the right SIZE and covered a fifth
    of the matchups its manifest named -- which is the failure mode that survives
    review, because every count is correct.
    """

    def _manifest(self, tmp_path, decks):
        files = []
        for i, deck in enumerate(decks):
            f = tmp_path / f"cfg{i}.json"
            f.write_text(json.dumps({
                "gameType": "Two Player Duel", "deckType": "Constructed - Standard",
                "players": [
                    {"type": "cpu", "name": "Skill1", "deck": deck},
                    {"type": "cpu", "name": "Skill8", "deck": deck},
                ],
            }))
            files.append(f)
        return files

    def test_three_configs_give_three_distinct_deck_pairs(self, batch_env, tmp_path):
        fake_observer, observers, _ = batch_env
        fake_observer.fail_on = set()
        config = _config()
        config.batch_config_files = self._manifest(
            tmp_path, ["a.dck", "b.dck", "c.dck"]
        )

        sequential_batch.run_sequential_batch(
            config, tmp_path, tmp_path / "logs", [11, 22, 33], pm=_FakePm()
        )

        decks = []
        for call in observers[0].started_full:
            decks.append(call["players_config"]["players"][0]["deck"])
        assert decks == ["a.dck", "b.dck", "c.dck"], (
            f"each game must use ITS manifest entry, got {decks}"
        )

    def test_three_games_carry_three_distinct_seeds(self, batch_env, tmp_path):
        fake_observer, observers, _ = batch_env
        fake_observer.fail_on = set()
        config = _config()
        config.batch_config_files = self._manifest(tmp_path, ["a.dck", "b.dck", "c.dck"])

        sequential_batch.run_sequential_batch(
            config, tmp_path, tmp_path / "logs", [11, 22, 33], pm=_FakePm()
        )

        assert [c["game_seed"] for c in observers[0].started_full] == [11, 22, 33]

    def test_a_length_mismatch_is_an_error_not_a_truncation(self, batch_env, tmp_path):
        fake_observer, observers, _ = batch_env
        fake_observer.fail_on = set()
        config = _config()
        config.batch_config_files = self._manifest(tmp_path, ["a.dck", "b.dck", "c.dck"])

        # Truncating would run 2 games and look successful. Recycling would run 3 with
        # a repeat. Both produce a corpus that disagrees with its own manifest.
        with pytest.raises(AssertionError, match="One config per game"):
            sequential_batch.run_sequential_batch(
                config, tmp_path, tmp_path / "logs", [11, 22], pm=_FakePm()
            )


class TestASessionsDirectoriesHoldOneGameEach:
    """Two games in one directory means every provenance field is right for one and wrong for the other.

    game_meta.json, the seed and the deck pair describe ONE game. A directory with
    two has nothing in it saying which, so a row built from it can carry the wrong
    deck pair -- worse than a missing row, because it lands in the archetype
    attribution looking valid.

    Cause: a game ending is not a match ending. MatchImpl.endGame credits a win only
    if a player hasWon, so a game that finishes with nobody winning leaves the match
    live and TableController starts its next game into the same gameLogDir. Measured
    at 10 of 3,907 directories on the step-1 corpus, nine of them games that ended
    with no player at or below 0 life.
    """

    def _dir(self, tmp_path, name, starts):
        d = tmp_path / name
        d.mkdir(parents=True)
        lines = []
        for i in range(starts):
            lines.append(json.dumps({"seq": 0, "type": "game_start", "players": []}))
            lines.append(json.dumps({"seq": 9, "type": "game_end", "winner": "Skill1"}))
        (d / "server_game_events.jsonl").write_text("\n".join(lines) + "\n")
        return d

    def test_a_clean_session_reports_nothing(self, tmp_path):
        dirs = [self._dir(tmp_path, f"game_T_s{i}", 1) for i in range(1, 6)]

        assert sequential_batch.dirs_holding_more_than_one_game(dirs) == []

    def test_a_directory_with_two_games_is_named(self, tmp_path):
        clean = [self._dir(tmp_path, f"game_T_s{i}", 1) for i in (1, 2)]
        doubled = self._dir(tmp_path, "game_T_s3", 2)

        found = sequential_batch.dirs_holding_more_than_one_game(clean + [doubled])

        assert found == [doubled]

    def test_a_directory_with_no_events_is_not_reported(self, tmp_path):
        # A game that never wrote events is a different failure and has its own path;
        # reporting it here would make this check fire for the wrong reason.
        empty = tmp_path / "game_T_s9"
        empty.mkdir(parents=True)

        assert sequential_batch.dirs_holding_more_than_one_game([empty]) == []
