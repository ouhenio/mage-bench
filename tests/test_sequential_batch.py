"""Tests for the sequential batch runner's failure isolation."""

from __future__ import annotations

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
        self.closed = False
        self.log_path = Path("/tmp/observer.log")
        self.proc = _FakeProc()

    def start_game(self, game_dir, players_config, **kwargs):
        self.started.append((game_dir.name, kwargs.get("game_seed")))

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
