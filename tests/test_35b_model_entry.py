"""The 35B teacher's model entry must exist, and the test runs the resolution.

The entry was on the as-run corpus harness (03211038) and on the ladder branch, and NOT on
integration -- so corpus v3's provenance is intact but every tree built from integration
from now on would resolve a 35B preset straight into `AssertionError: Unknown model` at
config.py:371, exactly as the two checkpoint presets did an hour earlier.

A JSON-presence check would pass against a tree whose preset and entry disagree. So this
resolves through the real config files, the way the failure actually arrives.
"""
import pytest

from magebench.orchestration.config import (
    PilotPlayer,
    _resolve_randoms,
    load_models,
    load_personalities,
    load_presets,
    load_prompts,
    load_toolsets,
)

TEACHER = "Qwen/Qwen3.5-35B-A3B"


def _resolve(**kwargs):
    players = [(PilotPlayer(name="Eval00", **kwargs), True)]
    _resolve_randoms(
        players,
        load_personalities(None),
        load_presets(None),
        load_prompts(None),
        load_models(None),
        load_toolsets(None),
    )
    return players[0][0]


def test_the_teacher_id_resolves():
    """The failure this closes: the id is asked for and not known."""
    assert _resolve(model=TEACHER).model == TEACHER


@pytest.mark.parametrize(
    "preset",
    [name for name, data in
     __import__("magebench.orchestration.config", fromlist=["x"]).load_presets(None)["presets"].items()
     if data.get("model") == TEACHER],
)
def test_every_preset_asking_for_the_teacher_resolves(preset):
    """Parametrised over the presets that actually name it, so a preset added later is
    covered without anyone remembering to extend this list -- and if none exist, the
    parametrisation is empty and pytest says so rather than the file passing silently."""
    assert _resolve(preset=preset).model == TEACHER


def test_the_username_budget_is_exactly_full():
    """name_part is 6 and the longest personality label is 7, so the username is exactly
    at the 14-char limit with no headroom. Documented in the entry's own notes, and worth a
    test because I overflowed that same budget by one character with CKPT265 an hour ago --
    the rule is invisible until it refuses you."""
    models = {m["id"]: m for m in load_models(None)["models"]}
    assert len(models[TEACHER]["name_part"]) == 6


def test_the_entry_matches_the_tree_that_generated_corpus_v3():
    """Provenance: these are the as-run bytes from 03211038, not a fresh transcription.
    Everything except the notes must be identical, and the notes changed deliberately --
    the old ones inferred the tool-call parser from the 4B's note rather than measuring
    this model."""
    entry = {m["id"]: m for m in load_models(None)["models"]}[TEACHER]
    assert entry["name"] == "Qwen3.5 35B-A3B"
    assert entry["name_part"] == "Q3535B"
    assert entry["_context_k"] == 143
    assert entry["_input_per_m"] == 0.0 and entry["_output_per_m"] == 0.0
    assert entry["_thinking"] is False
    assert entry["_tier"] == "cheap"


def test_the_parser_note_names_what_actually_ran():
    """The note claimed qwen3_xml by inference from the 4B. Production served qwen3_coder
    for all 2,429 games. A note that argues for a parser nobody ran is worse than none:
    it is the first thing read when tool calls come back malformed."""
    entry = {m["id"]: m for m in load_models(None)["models"]}[TEACHER]
    notes = entry["_notes"]
    assert "qwen3_coder" in notes
    assert "127,571 responses" in notes, "the claim must carry its measurement"
    assert "does NOT bound calls the parser may have missed" in notes, (
        "the measurement's limit must travel with it"
    )
