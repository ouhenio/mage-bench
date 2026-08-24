"""Tests for the two rendering economies, and for their defaults staying off.

Both exist because rendering cost is not a cost concern in this project -- it is a deck
admissibility criterion. Tool results are 76.7% of the prompt, 74.7% of each render
repeats the previous one, and growth of ~270 tok/decision against a 39,936-token usable
budget is what aborted 13/20 Azorius games and put DevotionBlack's p90 within 109 tokens
of the ceiling.

The DEFAULTS are pinned deliberately. State encoding changes agent behaviour even when
the information is identical, so neither economy may become the default by accident --
each needs an A/B against the screen seeds first. A test that only checked the compacted
output would let a flipped default ship silently.
"""


import pytest

from magebench.game.decision_renderer import _aggregate_duplicates
from magebench.pilot.pilot import _chat_prompts_enabled


@pytest.fixture
def compact(monkeypatch):
    monkeypatch.setenv("MAGEBENCH_COMPACT_BOARD", "1")


def test_aggregation_is_off_by_default(monkeypatch):
    monkeypatch.delenv("MAGEBENCH_COMPACT_BOARD", raising=False)
    items = ["Swamp", "Swamp", "Swamp"]
    assert _aggregate_duplicates(items) == items


def test_aggregation_collapses_repeats(compact):
    assert _aggregate_duplicates(["Swamp"] * 5) == ["5x Swamp"]


def test_aggregation_preserves_first_appearance_order(compact):
    got = _aggregate_duplicates(["Mutavault", "Swamp", "Swamp", "Pack Rat 1/1", "Swamp"])
    assert got == ["Mutavault", "3x Swamp", "Pack Rat 1/1"]


def test_singletons_are_not_annotated(compact):
    assert _aggregate_duplicates(["Mutavault", "Pack Rat 1/1"]) == ["Mutavault", "Pack Rat 1/1"]


def test_differing_state_is_not_collapsed(compact):
    """"Swamp" and "Swamp (tapped)" are different game states and must stay separate.

    This is the property that makes the whole change safe: it collapses only entries
    whose rendered text is byte-identical, and tapped/untapped is in that text.
    """
    got = _aggregate_duplicates(["Swamp", "Swamp (tapped)", "Swamp", "Swamp (tapped)"])
    assert got == ["2x Swamp", "2x Swamp (tapped)"]


def test_empty_and_single(compact):
    assert _aggregate_duplicates([]) == []
    assert _aggregate_duplicates(["Island"]) == ["Island"]


def test_aggregation_is_information_preserving(compact):
    """The multiset must be recoverable from the compacted form.

    The board line carries no object ids -- ids appear only in the Choices list -- so
    identical entries are interchangeable and "Nx Name" loses nothing a reader could
    have used.
    """
    items = ["Swamp"] * 4 + ["Mutavault", "Mutavault", "Temple of Deceit"]
    out = _aggregate_duplicates(items)
    recovered: list[str] = []
    for entry in out:
        head, _, rest = entry.partition("x ")
        if head.isdigit() and rest:
            recovered.extend([rest] * int(head))
        else:
            recovered.append(entry)
    assert sorted(recovered) == sorted(items)


def test_chat_prompts_default_on(monkeypatch):
    monkeypatch.delenv("MAGEBENCH_CHAT_PROMPTS", raising=False)
    assert _chat_prompts_enabled() is True


def test_chat_prompts_opt_out(monkeypatch):
    monkeypatch.setenv("MAGEBENCH_CHAT_PROMPTS", "0")
    assert _chat_prompts_enabled() is False


def test_chat_prompts_only_zero_disables(monkeypatch):
    """Anything other than "0" leaves the benchmark feature on, matching the
    MAGEBENCH_DECISION_REMINDERS convention already in the tree."""
    monkeypatch.setenv("MAGEBENCH_CHAT_PROMPTS", "1")
    assert _chat_prompts_enabled() is True
