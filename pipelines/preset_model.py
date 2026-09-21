#!/usr/bin/env python
"""Resolve a preset THROUGH THE HARNESS'S OWN CODE and print the model it yields.

WHAT THIS GUARDS. A job serves one model under one name and the pilot asks for a model by
another name; nothing in the runner joins them, so a disagreement surfaces only once games are
launching -- as a 404 per call, or as an `AssertionError: Unknown model` at config load, 36
games at a time. Printing the model lets the caller assert `preset.model == served name` before
a GPU is spent.

WHY IT CALLS `resolve_preset` INSTEAD OF READING JSON. The first version of this script read
`presets.json` and printed `entry["model"]`. It passed, and the eval then died anyway at
`config.py:389` on `Unknown model: 'mtg-ckpt-265'` -- because a preset naming a model is not a
model that RESOLVES, and `models.json` needs its own entry (`_resolve_randoms` reads
`ignore_providers`, `provider_order` and `cache_control` from it). A check that re-implements
one step of a four-step resolution certifies that step and nothing else. So this runs the real
`resolve_preset` and then the real models lookup, in the tree's own interpreter, and inherits
every error message the production path raises: unknown preset, unknown prompt, unknown
toolset, unknown model. wt-eval's own models.json entry says the same thing in its `_notes` --
"a preset alone is not enough" -- which is where this would have been caught by reading.

Usage:  preset_model.py <tree root> <preset name>          [prints the model id]
"""

from __future__ import annotations

import pathlib
import sys


def main() -> int:
    if len(sys.argv) != 3:
        print(f"usage: {sys.argv[0]} <tree root> <preset name>", file=sys.stderr)
        return 2
    tree, name = pathlib.Path(sys.argv[1]), sys.argv[2]

    sys.path.insert(0, str(tree / "src"))
    try:
        from magebench.orchestration.config import (  # noqa: E402
            PilotPlayer,
            load_models,
            load_presets,
            load_prompts,
            load_toolsets,
            resolve_preset,
        )
    except ImportError as exc:
        print(f"cannot import the harness from {tree}: {exc}", file=sys.stderr)
        return 2

    # `_load_json_file` searches `config_file.parent` first, so any path inside puppeteer/
    # names the directory. Without it the loaders fall back to a CWD-relative `puppeteer/`,
    # which would silently read a DIFFERENT tree's registries -- the exact confusion this
    # script exists to catch.
    cfg = tree / "puppeteer" / "config.json"
    if not cfg.parent.is_dir():
        print(f"no puppeteer/ directory under {tree}", file=sys.stderr)
        return 2

    player = PilotPlayer(name="preflight", preset=name)
    try:
        resolve_preset(player, load_presets(cfg), load_prompts(cfg), load_toolsets(cfg))
    except (ValueError, KeyError, AssertionError) as exc:
        print(f"preset {name!r} does not resolve in {tree}: {exc}", file=sys.stderr)
        return 1

    if not player.model:
        print(f"preset {name!r} resolves but names no model", file=sys.stderr)
        return 1

    # The same lookup `_resolve_randoms` makes, and the reason this script is not a JSON read.
    models = load_models(cfg)
    known = {m["id"] for m in models.get("models", [])}
    if player.model not in known:
        print(
            f"preset {name!r} asks for model {player.model!r}, which is NOT registered in "
            f"{tree}/puppeteer/models.json. This is what config.py:389 asserts on, at config "
            f"load, before any server is contacted -- jobs 9524/9525 lost 36 games each to it. "
            f"Copy the entry from a tree that has it.",
            file=sys.stderr,
        )
        return 1

    print(player.model)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
