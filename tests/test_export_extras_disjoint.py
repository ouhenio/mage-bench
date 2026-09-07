"""_extras must never shadow a known field.

`to_mapping()` writes the known fields and then `obj.update(self._extras)`, so an extras
key with a known field's name wins silently. Nothing reachable produces that today --
`_extras_from_mapping` partitions by known-field membership -- which is exactly why it
would go unnoticed: the first record built by hand with both would export a value the
reader can see and the writer cannot, with no error anywhere.

The tests are ordered deliberately: the FIRST one demonstrates the precedence that makes
this worth guarding, by reproducing it on a minimal record. Without it, the guard reads
as defensive noise.
"""
from dataclasses import dataclass, field
from typing import ClassVar

import pytest

from magebench.game.game_export_types import _DecisionSupportRecord


@dataclass(frozen=True, slots=True)
class _Sample(_DecisionSupportRecord):
    name: str | None = None
    _KNOWN_FIELDS: ClassVar[tuple[str, ...]] = ("name",)


def test_the_precedence_this_guards_is_real():
    """Bypass the guard and show extras would win. This is the motivation, not a wish."""
    rec = _Sample(name="from_the_field")
    object.__setattr__(rec, "_extras", {"name": "from_extras"})
    assert rec.to_mapping()["name"] == "from_extras", (
        "if this ever fails, the ordering in to_mapping changed and this guard's "
        "rationale needs rewriting rather than deleting"
    )


def test_a_collision_is_refused_at_construction():
    with pytest.raises(ValueError, match="appear in both _extras and the known fields"):
        _Sample(name="x", _extras={"name": "y"})


def test_the_refusal_names_the_colliding_keys():
    with pytest.raises(ValueError) as e:
        _Sample(name="x", _extras={"name": "y"})
    assert "['name']" in str(e.value)


def test_disjoint_extras_are_untouched():
    """The good state, not merely the absence of the bad one: an unknown key still
    round-trips, which is the whole point of _extras."""
    rec = _Sample(name="x", _extras={"settings": {"a": 1}})
    out = rec.to_mapping()
    assert out["name"] == "x"
    assert out["settings"] == {"a": 1}


def test_an_absent_known_field_stays_absent():
    """Omit-when-absent has to survive the guard: 'predates the instrument' and 'the
    instrument ran and resolved nothing' must not collapse into the same encoding."""
    rec = _Sample(_extras={"settings": {"a": 1}})
    out = rec.to_mapping()
    assert "name" not in out
    assert out["settings"] == {"a": 1}
