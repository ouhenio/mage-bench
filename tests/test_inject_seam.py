"""The seam must be INERT unconfigured, and must select rather than rewrite.

Inertness is a REGISTERED CONDITION, not a courtesy: docs/eval/prompt-experiment.md
makes the cheap control (arm C) reusable only if this build changes nothing on the
game path with no configuration. Arm C runs on the seam build with the seam empty,
so a seam that perturbs anything unconfigured turns the control into a third
treatment and the cheapest arm becomes the least comparable one.
"""
import pathlib

import pytest

from magebench.pilot.inject import (
    INERT,
    content_path,
    inject_near_decision,
    parse_sections,
    select_section,
)

CONTENT = pathlib.Path(__file__).resolve().parents[2] / "harness/prompts/meta-strategy-v1.md"


class _Choice:
    def __init__(self, action):
        self.action = action


class TestInertWhenUnconfigured:
    def test_returns_the_inert_singleton_by_identity(self, monkeypatch):
        monkeypatch.delenv("MAGEBENCH_INJECT_CONTENT", raising=False)
        for at, choices, phase in [
            ("select_attackers", [], "Declare Attackers"),
            ("declare_blockers", [], "Declare Blockers"),
            ("priority_action", [_Choice("cast")], "Precombat Main"),
            (None, [_Choice("land")], "Precombat Main"),
            ("mulligan", [], None),
        ]:
            got = inject_near_decision(at, choices, phase)
            # IDENTITY, not equality: a different empty list would compare equal and
            # would not prove the unconfigured path never built anything.
            assert got is INERT, f"{at} returned a non-singleton empty result"

    def test_extending_by_it_cannot_change_the_rendered_lines(self, monkeypatch):
        monkeypatch.delenv("MAGEBENCH_INJECT_CONTENT", raising=False)
        lines = ["a", "b"]
        before = list(lines)
        lines.extend(inject_near_decision("select_attackers", [], "Declare Attackers"))
        assert lines == before

    def test_empty_string_is_treated_as_unconfigured(self, monkeypatch):
        monkeypatch.setenv("MAGEBENCH_INJECT_CONTENT", "   ")
        assert content_path() is None

    def test_the_singleton_is_still_empty(self):
        """Guards the one way this file could poison every game at once."""
        assert INERT == [], "INERT was mutated; it is shared by every decision"


class TestConfiguredButWrong:
    def test_a_missing_content_file_raises_rather_than_disabling(self, monkeypatch):
        """A typo'd path must not run arm D as a second copy of arm C.

        Silently returning nothing would give the arm label D and the transcript C,
        and the two would be indistinguishable in every artifact.
        """
        monkeypatch.setenv("MAGEBENCH_INJECT_CONTENT", "/nonexistent/content.md")
        with pytest.raises(FileNotFoundError):
            inject_near_decision("select_attackers", [], "Declare Attackers")

    def test_a_trigger_naming_an_absent_section_raises(self, monkeypatch, tmp_path):
        doc = tmp_path / "c.md"
        doc.write_text("## race — only this one\nbody\n")
        monkeypatch.setenv("MAGEBENCH_INJECT_CONTENT", str(doc))
        with pytest.raises(KeyError):
            inject_near_decision("select_attackers", [], "Declare Attackers")   # wants 'role'


class TestTriggerMap:
    """The map registered in meta-strategy-v1.md before any data."""

    def test_attackers_trigger_role(self):
        assert select_section("select_attackers", [], "Declare Attackers") == "role"

    def test_blockers_trigger_race(self):
        assert select_section("declare_blockers", [], "Declare Blockers") == "race"

    def test_priority_in_a_main_phase_with_a_cast_triggers_sequencing(self):
        assert select_section("priority_action", [_Choice("cast")], "Precombat Main") == "sequencing"

    def test_priority_outside_a_main_phase_triggers_nothing(self):
        """An unclaimed decision gets NOTHING, never a nearest match.

        The specificity control asks which section moved which class; a fallback
        would make that unanswerable.
        """
        assert select_section("priority_action", [_Choice("cast")], "Upkeep") is None

    def test_mulligan_triggers_nothing(self):
        assert select_section("mulligan", [], None) is None


@pytest.mark.skipif(not CONTENT.exists(), reason="content document not present")
class TestAgainstTheRealDocument:
    def test_every_triggerable_section_exists_in_the_document(self):
        sections = parse_sections(CONTENT.read_text())
        for name in ("role", "race", "sequencing", "harness"):
            assert name in sections, f"content document has no '## {name}'"

    def test_sections_are_returned_verbatim(self):
        """Triggers SELECT, they never rewrite -- the prose must be byte-identical
        between arms, so a split that reflowed or stripped would break the design."""
        text = CONTENT.read_text()
        for name, body in parse_sections(text).items():
            assert body in text, f"section {name} was altered by parsing"

    def test_one_section_at_most(self, monkeypatch):
        monkeypatch.setenv("MAGEBENCH_INJECT_CONTENT", str(CONTENT))
        lines = inject_near_decision("select_attackers", [], "Declare Attackers")
        assert lines, "expected the role section"
        headings = [l for l in lines if l.startswith("## ")]
        assert len(headings) == 1, f"expected exactly one section, got {headings}"
