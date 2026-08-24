"""Prompt-loading helpers for pilot-facing configuration."""

from pathlib import Path

from magebench.common.json5_utils import loads_json5

# The repo's own puppeteer/ directory, located from THIS FILE rather than from the
# process's working directory.
#
# `Path("puppeteer") / "prompts"` is relative to the CWD, so these helpers only
# found the defaults when something happened to be running from inside the harness
# checkout. From anywhere else -- the mtg trunk, a pipeline, a notebook -- the
# directory silently did not exist, load_prompts returned {} for the defaults, and
# the caller died later on `KeyError: 'default'` with nothing naming a path.
#
# src/magebench/pilot/prompts.py -> parents[3] is the repo root.
_REPO_PUPPETEER = Path(__file__).resolve().parents[3] / "puppeteer"


def _load_json_file(name: str, config_file: Path | None) -> dict[str, str]:
    """Load a JSON/JSON5 object by name from the config dir or repo defaults."""
    candidates: list[Path] = []
    if config_file is not None:
        candidates.append(config_file.parent / name)
    candidates.append(_REPO_PUPPETEER / name)

    for candidate in candidates:
        if candidate.exists():
            data = loads_json5(candidate.read_text())
            assert isinstance(data, dict), f"{candidate}: expected JSON object"
            typed_prompts: dict[str, str] = {}
            for key, value in data.items():
                assert isinstance(key, str), f"{candidate}: prompt key must be a string, got {key!r}"
                assert isinstance(value, str), f"{candidate}: prompt {key!r} must be a string, got {value!r}"
                typed_prompts[key] = value
            return typed_prompts
    return {}


def load_prompts(config_file: Path | None) -> dict[str, str]:
    """Load prompt definitions from prompts/ directories plus prompts.json."""
    result: dict[str, str] = {}

    prompt_dirs: list[Path] = []
    if config_file is not None:
        prompt_dirs.append(config_file.parent / "prompts")
    prompt_dirs.append(_REPO_PUPPETEER / "prompts")

    for prompt_dir in prompt_dirs:
        if prompt_dir.is_dir():
            for md_file in sorted(prompt_dir.glob("*.md")):
                key = md_file.stem
                if key not in result:
                    result[key] = md_file.read_text().strip()

    result.update(_load_json_file("prompts.json", config_file))
    return result
