"""The rollout probe's settings reach the server JVM all together or not at all."""

import pytest

from magebench.orchestration.game_processes import ROLLOUT_PROBE_SETTINGS, rollout_probe_props

FULL = {
    "MAGEBENCH_ROLLOUT_SEATS": "Sleepy",
    "MAGEBENCH_ROLLOUT_OUT": "/x/rollouts.jsonl",
    "MAGEBENCH_ROLLOUT_POSITIONS": "20",
    "MAGEBENCH_ROLLOUT_NS": "8,16,32",
    "MAGEBENCH_ROLLOUT_REPEATS": "2",
    "MAGEBENCH_ROLLOUT_BUDGET_MS": "2000",
    "MAGEBENCH_ROLLOUT_THREADS": "8",
}


def test_off_without_seats():
    assert rollout_probe_props({"MAGEBENCH_ROLLOUT_NS": "8"}) == []


def test_all_settings_pass_through():
    props = rollout_probe_props(FULL)
    assert props == [
        "-Dxmage.rollout.seats=Sleepy",
        "-Dxmage.rollout.out=/x/rollouts.jsonl",
        "-Dxmage.rollout.positions=20",
        "-Dxmage.rollout.ns=8,16,32",
        "-Dxmage.rollout.repeats=2",
        "-Dxmage.rollout.budgetMs=2000",
        "-Dxmage.rollout.threads=8",
    ]


@pytest.mark.parametrize("env", [e for e, _ in ROLLOUT_PROBE_SETTINGS])
def test_any_missing_setting_refuses(env):
    partial = {k: v for k, v in FULL.items() if k != env}
    with pytest.raises(AssertionError, match=env):
        rollout_probe_props(partial)


def test_property_names_match_the_java_probe():
    # RolloutProbe reads these names; a rename on either side must fail here, not in a game
    from pathlib import Path

    java = (
        Path(__file__).resolve().parents[1]
        / "Mage.Server.Plugins/Mage.Player.AIMCTS/src/mage/player/ai/RolloutProbe.java"
    ).read_text()
    for _, prop in ROLLOUT_PROBE_SETTINGS:
        assert f'"{prop}"' in java, prop
    assert '"xmage.rollout.seats"' in java
