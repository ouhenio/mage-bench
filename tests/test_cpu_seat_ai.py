"""A cpu seat can name its XMage AI, and a seat that names none is serialized exactly as before.

The observer already reads an ``ai`` field on a bot seat (AiPuppeteerConfig.PlayerConfig) and maps
it through ``PlayerType.valueOf``; the harness simply never sent one, so every cpu seat was
COMPUTER_MAD and Mage.Player.AIMCTS could not be seated at all. The rollout-cost measurement needs
an MCTS seat, and every existing run must keep ``mad`` without a byte of its config changing.
"""

import json
import re
import tempfile
from pathlib import Path

import pytest

from magebench.orchestration.config import CPU_AI_TYPES, Config, CpuPlayer

REPO = Path(__file__).resolve().parents[1]


def _load(players: list[dict]) -> Config:
    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "config.json"
        path.write_text(json.dumps({"players": players}))
        config = Config(config_file=path)
        config.load_config()
        return config


def test_cpu_seat_parses_monte_carlo():
    config = _load([{"type": "cpu", "name": "mc", "ai": "COMPUTER_MONTE_CARLO"}])
    assert config.cpu_players[0].ai == "COMPUTER_MONTE_CARLO"


def test_cpu_seat_without_ai_leaves_it_unset():
    config = _load([{"type": "cpu", "name": "mad"}])
    assert config.cpu_players[0].ai is None


def test_monte_carlo_reaches_the_observer_json():
    config = Config()
    config.cpu_players = [CpuPlayer(name="mc", ai="COMPUTER_MONTE_CARLO", skill=1)]
    seat = json.loads(config.get_players_config_json())["players"][0]
    assert seat == {"type": "cpu", "name": "mc", "ai": "COMPUTER_MONTE_CARLO", "skill": 1}


def test_seat_without_ai_serializes_as_before():
    # No "ai" key at all: the observer's null branch is what makes it COMPUTER_MAD, so existing
    # runs send the identical JSON they sent before this field existed.
    config = Config()
    config.cpu_players = [CpuPlayer(name="mad", skill=1)]
    seat = json.loads(config.get_players_config_json())["players"][0]
    assert seat == {"type": "cpu", "name": "mad", "skill": 1}


@pytest.mark.parametrize("bad", ["monte carlo", "COMPUTER_MCTS", "computer_mad", ""])
def test_unknown_ai_refuses_at_load(bad):
    # Refused here, not in the observer: the observer's IllegalArgumentException surfaces as a
    # table that never fills, minutes into a job.
    with pytest.raises(AssertionError, match="COMPUTER_MONTE_CARLO"):
        _load([{"type": "cpu", "name": "x", "ai": bad}])


def test_draft_bot_is_not_a_game_seat():
    with pytest.raises(AssertionError):
        _load([{"type": "cpu", "name": "x", "ai": "COMPUTER_DRAFT_BOT"}])


def test_allowed_types_exist_in_the_java_enum():
    # The Python list is a copy of Java names; a rename there must fail here, not at seat time.
    enum_src = (REPO / "Mage/src/main/java/mage/players/PlayerType.java").read_text()
    java_names = set(re.findall(r"^\s*(COMPUTER_\w+)\(", enum_src, re.MULTILINE))
    assert set(CPU_AI_TYPES) <= java_names, (set(CPU_AI_TYPES), java_names)
