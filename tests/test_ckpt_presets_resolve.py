"""The two checkpoint presets must survive the orchestrator's own resolution.

They were verified to differ from q35-4b-local-strategy in `model` only -- true, and not
the property that mattered. `_resolve_randoms` looks the resolved model id up in
models.json and asserts it is known, so a preset naming an id with no entry dies at
`AssertionError: Unknown model: 'mtg-ckpt-265'` BEFORE any server is contacted. A one-deal
smoke found that; a check that the preset entries exist would not have.

SO THESE TESTS RUN THE PATH THAT FAILED. Not "is the entry in models.json" -- that passes
against the tree that died, because the presets were there and the models were not, and it
would pass again against any future gap on the other side of the same join.
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

CKPT_PRESETS = ("q35-4b-ckpt265-strategy", "q35-4b-ckpt132-strategy")


def _resolve(presets):
    players = [(PilotPlayer(name=f"Eval{i:02d}", preset=p), True)
               for i, p in enumerate(presets)]
    _resolve_randoms(
        players,
        load_personalities(None),
        load_presets(None),
        load_prompts(None),
        load_models(None),
        load_toolsets(None),
    )
    return [p for p, _ in players]


def test_two_checkpoint_seats_resolve_to_DISTINCT_models():
    """The acceptance, and the reason the head-to-head exists: two seats, two model ids.

    Both resolving to the same id would be a checkpoint playing itself, which is the
    outcome this whole eval is built to avoid and which no config or result would reveal.
    """
    a, b = _resolve(CKPT_PRESETS)
    assert a.model == "mtg-ckpt-265"
    assert b.model == "mtg-ckpt-132"
    assert a.model != b.model


@pytest.mark.parametrize("preset", CKPT_PRESETS)
def test_each_checkpoint_preset_resolves_alone(preset):
    """One seat at a time, so a failure names which preset rather than which pair."""
    (player,) = _resolve([preset])
    assert player.model in ("mtg-ckpt-265", "mtg-ckpt-132")


def test_the_base_preset_still_resolves():
    """The non-regression. Adding entries to models.json must not disturb the preset the
    2,429-game corpus was generated with."""
    (player,) = _resolve(["q35-4b-local-strategy"])
    assert player.model == "Qwen/Qwen3.5-4B"


def test_an_unknown_model_STILL_refuses():
    """The guard that caught this must keep catching. Without this, a change that made
    _resolve_randoms tolerant would turn every future missing entry into a silent
    fallback -- and the smoke that found this one would pass while resolving nothing."""
    players = [(PilotPlayer(name="Eval00", model="mtg-ckpt-does-not-exist"), True)]
    with pytest.raises(AssertionError, match="Unknown model"):
        _resolve_randoms(
            players,
            load_personalities(None),
            load_presets(None),
            load_prompts(None),
            load_models(None),
            load_toolsets(None),
        )


def test_the_checkpoint_entries_are_priced_like_the_base():
    """They ARE Qwen3.5-4B with different weights, so a divergence in price, context or
    tier would be a mistake rather than a decision."""
    models = {m["id"]: m for m in load_models(None)["models"]}
    base = models["Qwen/Qwen3.5-4B"]
    for mid in ("mtg-ckpt-265", "mtg-ckpt-132"):
        entry = models[mid]
        for field in ("_input_per_m", "_output_per_m", "_context_k", "_thinking", "_tier"):
            assert entry[field] == base[field], f"{mid}.{field} diverges from the base"
