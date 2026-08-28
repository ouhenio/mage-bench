"""The sequential path must seat policy players, and must pin every one of them.

It never called attach_game, so a pilot or sleepwalker seat was simply never
filled: nothing joined the table, the server closed the connection, and the run
died with "Remote end closed connection" -- a failure that reads as a flaky
network rather than an unimplemented feature. Corpus games are two cpu seats, so
nothing noticed until an eval arm needed a policy.
"""
from pathlib import Path

import pytest

from magebench.orchestration import batch_coordination
from magebench.orchestration.config import Config, PilotPlayer


class _Rec:
    """Captures what each client starter was pinned to."""

    def __init__(self):
        self.calls = []

    def __call__(self, *args, **kwargs):
        self.calls.append(kwargs.get("table_id", "<unset>"))

        class _P:
            pid = 4242

            def poll(self_inner):
                return None

        return _P()


@pytest.fixture
def recorders(monkeypatch):
    made = {}
    for name in ("start_sleepwalker_client", "start_pilot_client", "start_replay_client"):
        r = _Rec()
        made[name] = r
        monkeypatch.setattr(batch_coordination, name, r)
    return made


def _config_with(**kw):
    c = Config()
    for k, v in kw.items():
        setattr(c, k, v)
    return c


def test_every_client_is_pinned_to_the_given_table(recorders, tmp_path):
    cfg = _config_with(
        sleepwalker_players=[PilotPlayer(name="SW", deck="d.dck")],
        pilot_players=[PilotPlayer(name="P", deck="d.dck")],
        replay_players=[],
    )
    procs = batch_coordination.start_policy_clients(
        pm=None, project_root=Path("/root"), game_config=cfg,
        game_dir=tmp_path, table_id="TABLE-7",
    )
    assert [n for n, _ in procs] == ["SW", "P"]
    assert recorders["start_sleepwalker_client"].calls == ["TABLE-7"]
    assert recorders["start_pilot_client"].calls == ["TABLE-7"]


def test_an_unpinned_client_is_never_produced_silently(recorders, tmp_path):
    """A bridge with no table id joins the FIRST waiting table it finds.

    On a path where one server hosts many games back to back, that is how a client
    lands in somebody else's game while both runs still look healthy. If a caller
    ever passes None, the assertion below is what makes it visible -- the pin
    travelling as None is indistinguishable from correct behaviour at run time.
    """
    cfg = _config_with(
        sleepwalker_players=[PilotPlayer(name="SW", deck="d.dck")],
        pilot_players=[], replay_players=[],
    )
    batch_coordination.start_policy_clients(
        pm=None, project_root=Path("/root"), game_config=cfg,
        game_dir=tmp_path, table_id=None,
    )
    assert recorders["start_sleepwalker_client"].calls == [None], (
        "table_id must be forwarded verbatim, including None, so a caller that "
        "loses the pin fails its own assertion rather than cross-wiring quietly"
    )


def test_no_policy_players_starts_nothing(recorders, tmp_path):
    cfg = _config_with(sleepwalker_players=[], pilot_players=[], replay_players=[])
    procs = batch_coordination.start_policy_clients(
        pm=None, project_root=Path("/root"), game_config=cfg,
        game_dir=tmp_path, table_id="T",
    )
    assert procs == []
    assert not any(r.calls for r in recorders.values())


def test_the_sequential_module_starts_policy_clients():
    """The regression that mattered: this path used to import no such thing.

    Asserted on the module's own reference rather than on source text -- run_arm.py
    currently guards by grepping sequential_batch.py for the string "attach_game",
    which a comment would satisfy. A bound symbol cannot be satisfied by a comment.
    """
    from magebench.orchestration import sequential_batch

    assert sequential_batch.start_policy_clients is batch_coordination.start_policy_clients
