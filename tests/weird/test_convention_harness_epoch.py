"""Convention tests for harness epoch history and golden coherence."""

import ast
import re
import subprocess

import pytest

from tests.weird.repo_convention_helpers import REPO_ROOT, changed_files_since_master


class TestHarnessEpochMonotonic:
    def test_epoch_matches_history(self) -> None:
        source = (REPO_ROOT / "src" / "magebench" / "game" / "harness_epoch.py").read_text()

        tree = ast.parse(source)
        epoch_value = None
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Assign)
                and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id == "HARNESS_EPOCH"
                and isinstance(node.value, ast.Constant)
            ):
                epoch_value = node.value.value
        assert isinstance(epoch_value, int), f"HARNESS_EPOCH must be an int, got {type(epoch_value)}"

        history_epochs = [int(match) for match in re.findall(r"#\s+(\d+)\s+-\s+", source)]
        assert history_epochs, "No history comments found in harness_epoch.py"

        assert epoch_value == max(history_epochs), (
            f"HARNESS_EPOCH={epoch_value} doesn't match max history entry {max(history_epochs)}"
        )

        expected = list(range(1, max(history_epochs) + 1))
        assert sorted(history_epochs) == expected, f"History has gaps or duplicates: {sorted(history_epochs)}"


class TestGoldenEpochCoherence:
    """Two-way invariant between golden output and harness epoch.

    1. Modified existing golden output -> harness epoch must be bumped.
    2. Bumped harness epoch -> all goldens must be regenerated.
    """

    _EXPORT_GOLDEN_PREFIX = "tests/golden/exports/"
    _EPOCH_FILE = "src/magebench/game/harness_epoch.py"

    def test_golden_changes_require_epoch_bump(self) -> None:
        """If existing export golden output changed, HARNESS_EPOCH must be bumped too."""
        changed = changed_files_since_master()
        if changed is None:
            pytest.skip("On master or git unavailable")

        merge_base = subprocess.run(
            ["git", "merge-base", "master", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        result = subprocess.run(
            [
                "git",
                "diff",
                "--diff-filter=M",
                "--name-only",
                merge_base,
                "--",
                self._EXPORT_GOLDEN_PREFIX,
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        modified_goldens = set(result.stdout.strip().splitlines()) if result.stdout.strip() else set()
        if not modified_goldens:
            return

        assert self._EPOCH_FILE in changed, (
            f"{len(modified_goldens)} export golden(s) modified without bumping HARNESS_EPOCH.\n"
            "Export golden output changes mean the harness changed — bump the epoch.\n"
            "Modified goldens:\n  " + "\n  ".join(sorted(modified_goldens))
        )

    def test_epoch_bump_requires_full_regen(self) -> None:
        """If HARNESS_EPOCH was bumped, all export goldens must be regenerated.

        Export goldens embed harnessEpoch, so they always change when the epoch
        bumps. If any export golden is untouched, ``make regen-golden`` was not
        run. (Prompt/blunder goldens may legitimately be unchanged if the epoch
        bump didn't affect prompt content.)
        """
        changed = changed_files_since_master()
        if changed is None:
            pytest.skip("On master or git unavailable")

        if self._EPOCH_FILE not in changed:
            return

        exports_dir = REPO_ROOT / "tests" / "golden" / "exports"
        all_exports = {str(path.relative_to(REPO_ROOT)) for path in exports_dir.glob("*.json5")}

        untouched = all_exports - changed
        assert not untouched, (
            f"HARNESS_EPOCH was bumped but {len(untouched)} export golden(s) not regenerated.\n"
            "Run `make regen-golden` after bumping the epoch.\n"
            "Untouched exports:\n  " + "\n  ".join(sorted(untouched))
        )


def _declared_epoch() -> int:
    """HARNESS_EPOCH as the source declares it, read without importing magebench."""
    source = (REPO_ROOT / "src" / "magebench" / "game" / "harness_epoch.py").read_text()
    for node in ast.walk(ast.parse(source)):
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id == "HARNESS_EPOCH"
            and isinstance(node.value, ast.Constant)
        ):
            return int(node.value.value)
    raise AssertionError("HARNESS_EPOCH not found in harness_epoch.py")


class TestGoldenEpochIsCurrent:
    """The epoch the goldens claim must be the epoch the source declares.

    This is the same invariant TestGoldenEpochCoherence guards, checked a way that
    does not depend on git. Both of its tests call changed_files_since_master(),
    which returns None -- and skips -- when HEAD is master. So the gate that stops
    an unbacked epoch bump reaching trunk was disabled precisely on trunk, and all
    15 exports sat at harness_epoch 60 against a constant of 61 for a whole epoch
    with a green suite. The failure was not that the check was wrong. It never ran,
    and nothing said so.

    A skip is not a pass. Read pytest -rs before believing a green run of a gate
    whose whole job is to fail.

    Unconditional on purpose: no merge-base, no upstream, no branch. It holds on
    master, on a feature branch, in a fresh clone with no remotes, and in CI.
    """

    def test_every_export_golden_carries_the_declared_epoch(self) -> None:
        epoch = _declared_epoch()
        exports = sorted((REPO_ROOT / "tests" / "golden" / "exports").glob("*.json5"))
        assert exports, "No export goldens found -- this test would pass vacuously."

        wrong: list[str] = []
        for path in exports:
            found = re.findall(r'"harness_epoch"\s*:\s*(\d+)', path.read_text())
            rel = str(path.relative_to(REPO_ROOT))
            if not found:
                wrong.append(f"{rel}: no harness_epoch field")
            elif {int(v) for v in found} != {epoch}:
                wrong.append(f"{rel}: {sorted({int(v) for v in found})}")

        assert not wrong, (
            f"HARNESS_EPOCH is {epoch} but {len(wrong)} of {len(exports)} export golden(s) "
            "disagree. Run `make regen-golden`.\n  " + "\n  ".join(wrong)
        )
