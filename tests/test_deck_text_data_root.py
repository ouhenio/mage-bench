"""The harness must resolve the data root or refuse -- never default to a node.

This file previously carried, three lines apart, a docstring explaining why the oracle
path must be resolved at CALL TIME and a module constant that resolved it at import;
and, twice, a default of "/workspace1/projects/posttrainlatamgpt/ouhenio/mtg" -- RANOKAU's
root. Lascar's carries a `users/` segment. Right lesson, wrong constant, same function.

This is the path the PILOT reads at inference, so the failure lands on whichever node the
harness was not written on, and it lands two files away naming a missing 200 MB download
that is present all along under another prefix.

The tests that matter are the REFUSALS. A resolver that always returns something is the bug.
"""
import pathlib

import pytest

from magebench.pilot import deck_text


def test_an_explicit_root_wins_even_if_absent(monkeypatch):
    """Pointing this at a scratch tree is deliberate; a resolver that overrules the
    operator is its own silent failure."""
    monkeypatch.setenv("MTG_DATA_ROOT", "/nowhere/at/all")
    assert deck_text.data_root() == pathlib.Path("/nowhere/at/all")


def test_it_resolves_when_exactly_one_candidate_exists(monkeypatch, tmp_path):
    monkeypatch.delenv("MTG_DATA_ROOT", raising=False)
    only = tmp_path / "one"
    only.mkdir()
    monkeypatch.setattr(deck_text, "CANDIDATE_DATA_ROOTS", (only, tmp_path / "absent"))
    assert deck_text.data_root() == only


def test_it_REFUSES_when_none_exists(monkeypatch, tmp_path):
    monkeypatch.delenv("MTG_DATA_ROOT", raising=False)
    monkeypatch.setattr(deck_text, "CANDIDATE_DATA_ROOTS", (tmp_path / "a", tmp_path / "b"))
    with pytest.raises(RuntimeError, match="cannot resolve MTG_DATA_ROOT"):
        deck_text.data_root()


def test_it_REFUSES_when_the_node_is_ambiguous(monkeypatch, tmp_path):
    """Two roots on one node means a silent pick splits one run's data across both."""
    monkeypatch.delenv("MTG_DATA_ROOT", raising=False)
    a = tmp_path / "a"; a.mkdir()
    b = tmp_path / "b"; b.mkdir()
    monkeypatch.setattr(deck_text, "CANDIDATE_DATA_ROOTS", (a, b))
    with pytest.raises(RuntimeError, match="2 of 2 candidate roots exist"):
        deck_text.data_root()


def test_neither_node_is_a_default(monkeypatch, tmp_path):
    """The regression guard, stated as the thing that went wrong: with nothing in the
    environment and no candidate present, the answer is an exception -- not ranokau."""
    monkeypatch.delenv("MTG_DATA_ROOT", raising=False)
    monkeypatch.delenv("MTG_ORACLE_CARDS", raising=False)
    monkeypatch.setattr(deck_text, "CANDIDATE_DATA_ROOTS", (tmp_path / "neither",))
    with pytest.raises(RuntimeError):
        deck_text.oracle_cards_path()


def test_the_oracle_path_is_resolved_at_call_time(monkeypatch, tmp_path):
    """The other half: a module constant is read once at import, so a later
    MTG_ORACLE_CARDS is silently ignored -- for the path that decides which card text
    a corpus is built from."""
    monkeypatch.setenv("MTG_ORACLE_CARDS", str(tmp_path / "first.jsonl"))
    assert deck_text.oracle_cards_path() == tmp_path / "first.jsonl"
    monkeypatch.setenv("MTG_ORACLE_CARDS", str(tmp_path / "second.jsonl"))
    assert deck_text.oracle_cards_path() == tmp_path / "second.jsonl"


def test_there_is_no_import_time_constant_left(monkeypatch):
    """ORACLE_CARDS was computed at import. Resolution can now raise, so a module that
    refuses to IMPORT on an unresolvable node would be a worse failure than a call that
    refuses at the point of use."""
    assert not hasattr(deck_text, "ORACLE_CARDS")
    assert not hasattr(deck_text, "MTG_DATA_ROOT")


def test_the_candidate_list_still_holds_both_nodes():
    """If this ever shrinks to one, the resolver silently becomes a default again."""
    roots = {str(p) for p in deck_text.CANDIDATE_DATA_ROOTS}
    assert "/workspace1/projects/posttrainlatamgpt/users/ouhenio/mtg" in roots, "lascar"
    assert "/workspace1/projects/posttrainlatamgpt/ouhenio/mtg" in roots, "ranokau"
