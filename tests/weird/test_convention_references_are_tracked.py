"""Every file a committed config or preset names must itself be committed.

This class of defect landed THREE times in one day, each time invisible to
`git status` and to every existing test:

  1. tracked configs referenced a preset that lived only in a working tree
  2. the commit fixing that referenced a PROMPT that was itself untracked
  3. twelve tracked configs named decks excluded by .gitignore, so a fresh clone
     resolved none of them and could not run a game

Each was caught by accident, one level at a time, after the fact. The shape is
always the same: the repository is internally consistent on the machine that
wrote it and broken on every other machine, and nothing in the working tree says
so. `ls` cannot see it -- only `git ls-files` can, which is why these tests use
trackedness rather than existence as the predicate.

If you are here because this test failed: do not delete the reference and do not
relax the check. Commit the file it names. If the file genuinely should not be
in the repository, the config naming it should not be either.
"""

import json
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]


def _tracked() -> set[str]:
    out = subprocess.run(
        ["git", "ls-files"], cwd=REPO, capture_output=True, text=True, check=True
    ).stdout
    return set(out.splitlines())


def _configs() -> list[Path]:
    return sorted((REPO / "configs").glob("*.json"))


def _load(p: Path) -> dict:
    return json.loads(p.read_text())


# Sentinels, not references. The round-robin runner resolves these from a pool at
# launch rather than naming a file. Listed explicitly so the exemption is visible:
# a silent "skip anything that does not look like a path" would also skip a typo.
_DECK_SENTINELS = {"random"}
_PRESET_SENTINELS = {"round-robin"}


def test_every_config_deck_is_tracked():
    """A deck under .gitignore'd tmp/ resolves locally and nowhere else."""
    tracked = _tracked()
    missing = []
    for cfg in _configs():
        for player in _load(cfg).get("players", []):
            deck = player.get("deck")
            if deck in _DECK_SENTINELS:
                continue
            if deck and deck not in tracked:
                missing.append(f"{cfg.name} -> {deck}")
    assert not missing, (
        "Committed configs name decks that are not committed. A clone of this "
        "repository cannot run them:\n  " + "\n  ".join(missing) +
        "\n\nDecks under tmp/ need `git add -f`; tmp/ is gitignored for build scratch."
    )


def test_every_config_preset_exists():
    presets = _load(REPO / "puppeteer" / "presets.json")["presets"]
    missing = [
        f"{cfg.name} -> {p['preset']}"
        for cfg in _configs()
        for p in _load(cfg).get("players", [])
        if p.get("preset") and p["preset"] not in presets and p["preset"] not in _PRESET_SENTINELS
    ]
    assert not missing, "Configs reference unknown presets:\n  " + "\n  ".join(missing)


def test_every_preset_prompt_is_tracked():
    """Existence is not enough: the prompt must be IN THE REPO, not just on disk."""
    tracked = _tracked()
    presets = _load(REPO / "puppeteer" / "presets.json")["presets"]
    missing = []
    for name, preset in presets.items():
        prompt = preset.get("system_prompt")
        if not prompt or prompt == "default":
            continue
        rel = f"puppeteer/prompts/{prompt}.md"
        if rel not in tracked:
            missing.append(f"{name} -> {rel}")
    assert not missing, (
        "Presets reference prompts that are not committed:\n  " + "\n  ".join(missing)
    )


@pytest.mark.parametrize("path", ["puppeteer/presets.json", "puppeteer/models.json"])
def test_catalog_files_are_tracked(path):
    """The catalogs themselves. Both were uncommitted while every game resolved
    through them, which is how 711 games ran under a preset in no commit."""
    assert path in _tracked(), f"{path} is not committed"
