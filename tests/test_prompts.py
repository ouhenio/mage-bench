"""Tests for prompt-loading helpers."""

from pathlib import Path

import pytest

from magebench.pilot.prompts import load_prompts


def test_load_prompts_without_config_uses_repo_prompts_json(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """The repo's own prompts load, and the working directory does not decide which.

    THIS TEST USED TO ASSERT THE DEFECT. It chdir'd into a tmp directory, planted a
    fake `puppeteer/` there, and required load_prompts(None) to read it -- which
    passed only because the lookup was `Path("puppeteer")`, relative to the CWD.
    That is the exact behaviour that made the loader unusable from anywhere but the
    harness directory, and the test encoded it as the contract.

    The contract is now: the repo's own puppeteer/ is found from the MODULE, so the
    defaults are the same wherever you run from. Config-relative overriding is what
    `config_file` is for, and is covered below.
    """
    monkeypatch.chdir(tmp_path)

    prompts = load_prompts(None)

    assert "default" in prompts
    # A tmp dir with no puppeteer/ in it must not change the answer.
    assert prompts["default"].strip(), "the repo default must be non-empty"


def test_a_config_dir_still_overrides_the_repo_default(tmp_path: Path) -> None:
    """config_file is the supported way to supply your own prompts."""
    config_dir = tmp_path / "cfg"
    (config_dir / "prompts").mkdir(parents=True)
    (config_dir / "prompts" / "default.md").write_text("config default")
    config_file = config_dir / "game.json"
    config_file.write_text("{}")

    prompts = load_prompts(config_file)

    assert prompts["default"] == "config default"


def test_load_prompts_rejects_non_string_json_values(tmp_path: Path) -> None:
    """A prompts.json whose values are not strings is an error, not a coercion.

    Also formerly CWD-dependent: it planted the bad file in a tmp dir and chdir'd
    there. Supplied through config_file now, which is the route a caller actually
    has.
    """
    config_dir = tmp_path / "cfg"
    config_dir.mkdir(parents=True)
    (config_dir / "prompts.json").write_text('{"default": 17}')
    config_file = config_dir / "game.json"
    config_file.write_text("{}")

    with pytest.raises(AssertionError, match="must be a string"):
        load_prompts(config_file)
