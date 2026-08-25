"""The corpus-integrity guards must still fire under `python -O`.

`assert` is removed entirely at optimisation level 1. A guard written as a bare
assert is therefore absent in exactly the configuration where nobody is watching,
and these guards exist to make a SILENT corpus defect loud: a manifest that does
not match the game count, a game with no seed, two games in one directory.

Measured before this change, running the seeds guard under -O:

    returned 2 seeds for a 5-game session, silently

so a run would have proceeded with three games unaccounted for.

The test compiles the real module source at optimize=2 -- the same transformation
-O applies -- and exercises the guard, so it cannot pass by inspecting the source
and being satisfied by a comment.
"""

from __future__ import annotations

import pathlib

import pytest

SRC = pathlib.Path(__file__).resolve().parents[1] / "src" / "magebench" / "orchestration"


def _module_without_asserts(name: str):
    """Import a module from source compiled as if under `python -O`."""
    import sys
    import types

    path = SRC / f"{name}.py"
    code = compile(path.read_text(), str(path), "exec", optimize=2)
    mod_name = f"optimised_{name}"
    module = types.ModuleType(mod_name)
    module.__file__ = str(path)
    # Registered before exec: dataclasses resolves its own module through
    # sys.modules while the class body runs, and gets None if it is not there.
    sys.modules[mod_name] = module
    try:
        exec(code, module.__dict__)
    finally:
        sys.modules.pop(mod_name, None)
    return module


class TestTheGuardsSurviveMinusO:
    def test_the_seed_count_guard_still_fires(self, monkeypatch):
        monkeypatch.setenv("MAGEBENCH_GAME_SEEDS", "1,2")
        optimised = _module_without_asserts("orchestrator")

        with pytest.raises(ValueError, match="one seed per game"):
            optimised._sequential_seeds(5)

    def test_the_seed_guard_still_allows_a_matching_count(self, monkeypatch):
        monkeypatch.setenv("MAGEBENCH_GAME_SEEDS", "1,2,3")
        optimised = _module_without_asserts("orchestrator")

        # The control: a guard that raised unconditionally would also pass the test
        # above, and would be worse than the bug it replaced.
        assert optimised._sequential_seeds(3) == [1, 2, 3]

    def test_no_bare_assert_remains_in_the_sequential_runner(self):
        """A source check, as a second line of defence against a new one creeping in.

        The behavioural tests above cover the guards that exist today; this one
        notices a guard added tomorrow as an assert.
        """
        source = (SRC / "sequential_batch.py").read_text()
        offenders = [
            line.strip()
            for line in source.splitlines()
            if line.strip().startswith("assert ")
        ]

        assert offenders == [], (
            "corpus-integrity guards must be explicit raises, not asserts -- "
            f"`python -O` deletes these: {offenders}"
        )
