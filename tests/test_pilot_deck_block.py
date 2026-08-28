"""The pilot's own-deck card text must match what training renders, byte for byte.

Training assembles `f"{prompts['default']}\n\n{block}"` in
render_conversations.py:369-371. If the pilot assembles it any other way, every
served prompt differs from every trained one -- and by an amount too small to
notice by reading, which is exactly why it needs an assertion rather than a look.
"""
import pathlib

import pytest

from magebench.pilot.deck_text import build_deck_block, deck_text_block, load_oracle, maindeck_names
from magebench.pilot.pilot import _load_default_system_prompt, assemble_system_prompt

DECKS = sorted(pathlib.Path("tmp/decks").glob("*.dck")) if pathlib.Path("tmp/decks").exists() else []


@pytest.mark.skipif(not DECKS, reason="no decks checked out at tmp/decks")
def test_assembly_matches_the_training_join_exactly():
    deck = DECKS[0]
    base = _load_default_system_prompt()
    block, _ = build_deck_block(deck)
    assert block, f"{deck} produced an empty deck block; the gate would be vacuous"
    # The literal training rule, written out rather than imported, so that a change
    # to either side has to be made twice on purpose.
    assert assemble_system_prompt(base, deck) == f"{base}\n\n{block}"


@pytest.mark.skipif(not DECKS, reason="no decks checked out at tmp/decks")
def test_every_deck_produces_a_block_that_appears_verbatim_in_the_prompt():
    base = _load_default_system_prompt()
    for deck in DECKS:
        block, covered = build_deck_block(deck)
        prompt = assemble_system_prompt(base, deck)
        assert block in prompt, f"{deck.name}: block not present verbatim"
        assert prompt.startswith(base), f"{deck.name}: base prompt was modified"
        assert covered, f"{deck.name}: no cards covered, so the block says nothing"


@pytest.mark.skipif(not DECKS, reason="no decks checked out at tmp/decks")
def test_the_block_is_a_pure_function_of_the_decklist():
    """Same names in, same bytes out -- the property the cross-repo gate relies on."""
    oracle = load_oracle()
    for deck in DECKS[:8]:
        names = maindeck_names(deck.read_text(errors="replace").splitlines())
        a, _ = deck_text_block(names, oracle)
        b, _ = deck_text_block(names, oracle)
        assert a == b
        assert a == build_deck_block(deck)[0], f"{deck.name}: path and names disagree"


def test_an_empty_block_does_not_add_a_trailing_separator(tmp_path):
    """A deck of basics only covers nothing; the prompt must come back untouched."""
    empty = tmp_path / "basics.dck"
    empty.write_text("4 [ZEN:230] Mountain\n")
    base = "BASE"
    block, _ = build_deck_block(empty)
    if block:
        pytest.skip("basic lands produced a block; this guard needs a different fixture")
    assert assemble_system_prompt(base, empty) == base
