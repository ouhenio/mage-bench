"""Tests for the reminders appended to a rendered decision.

They exist because the system prompt's own rules were measured losing to the
conversation history: the same decision replayed without its history plays the land
16/16, with its history declines 16/16. Restating the rule beside the decision moved
land offers taken from 41.5% to 100.0% over 100 real games.

These pin the TRIGGER CONDITIONS, not the wording. The wording is an experiment; the
conditions are the contract -- in particular that a land outranks a spell, and that
the cast reminder is confined to a main phase.
"""

import pytest

from magebench.game.game_export_types import Choice
from magebench.pilot.pilot_rendering import (
    _CAST_REMINDER,
    _LAND_REMINDER,
    land_drop_reminder,
)


def _choices(*specs: dict) -> list[Choice]:
    return [Choice.from_mapping(s) for s in specs]


LAND = {"name": "Sacred Foundry", "id": "p6", "action": "land"}
SPELL = {"name": "Shock", "id": "p3", "action": "cast"}
MAIN = "PRECOMBAT_MAIN"


class TestLandReminder:
    def test_fires_on_a_land_choice(self) -> None:
        assert land_drop_reminder(_choices(LAND), MAIN) == [_LAND_REMINDER]

    def test_fires_regardless_of_phase(self) -> None:
        """A land drop is never the wrong play, so rule 1 is unconditional."""
        for phase in (MAIN, "POSTCOMBAT_MAIN", "COMBAT", "UPKEEP", None):
            assert land_drop_reminder(_choices(LAND), phase) == [_LAND_REMINDER]

    def test_land_outranks_a_spell(self) -> None:
        """One choice can be made, so only one imperative may be shown."""
        out = land_drop_reminder(_choices(SPELL, LAND), MAIN)
        assert out == [_LAND_REMINDER]
        assert len(out) == 1


class TestCastReminder:
    def test_fires_on_a_castable_spell_in_a_main_phase(self) -> None:
        assert land_drop_reminder(_choices(SPELL), MAIN) == [_CAST_REMINDER]
        assert land_drop_reminder(_choices(SPELL), "POSTCOMBAT_MAIN") == [_CAST_REMINDER]

    @pytest.mark.parametrize("phase", ["COMBAT", "DECLARE_BLOCKERS", "UPKEEP", "END_TURN", None, ""])
    def test_silent_outside_a_main_phase(self, phase: str | None) -> None:
        """Rule 3 scopes itself to "your own main phase"; holding an instant is fine."""
        assert land_drop_reminder(_choices(SPELL), phase) == []

    def test_silent_when_nothing_is_castable(self) -> None:
        assert land_drop_reminder(_choices({"name": "x", "action": "activate"}), MAIN) == []


class TestShared:
    def test_silent_on_empty_choices(self) -> None:
        assert land_drop_reminder([], MAIN) == []

    def test_silent_on_choices_with_no_action_field(self) -> None:
        """Boolean prompts (mulligan, yes/no) carry no action and must not trigger."""
        assert land_drop_reminder(_choices({"name": "p1"}, {"name": "p2"}), MAIN) == []

    def test_tolerates_bare_string_choices(self) -> None:
        assert land_drop_reminder(["yes", "no"], MAIN) == []

    def test_never_returns_more_than_one_line(self) -> None:
        out = land_drop_reminder(_choices(SPELL, LAND, {"name": "y", "action": "activate"}), MAIN)
        assert len(out) <= 1

    @pytest.mark.parametrize("action", ["cast", "activate", "ability", "", "LAND", "island"])
    def test_only_the_exact_land_action_triggers_rule_1(self, action: str) -> None:
        """Guard a substring match: 'land' must be the action, not part of one."""
        assert land_drop_reminder(_choices({"name": "x", "action": action}), "COMBAT") == []

    def test_both_reminders_name_the_response_format(self) -> None:
        """A reminder that does not say HOW to answer measurably did not help."""
        assert "choice=pN" in _LAND_REMINDER
        assert "choice=no" in _LAND_REMINDER
        assert "choice=no" in _CAST_REMINDER
