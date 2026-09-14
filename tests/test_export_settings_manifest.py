"""The settings manifest has to reach the EXPORT, not only game.jsonl.

A manifest a person can audit and a consumer cannot read is half a fix: every
downstream reader works from the exported game, so a provenance field that stops
at the seat log answers no question anyone actually asks of the corpus.

No export version bump. `version` stays 9 because this is an ADDITIVE OPTIONAL
field on an object the schema already declares `additionalProperties: true` --
a v9 reader that has never heard of `settings` ignores it, and one that wants it
finds it. Bumping would force every reader to migrate for a field none of them
are required to read, which is a cost with no corresponding failure.
"""

import json
from pathlib import Path

from magebench.game.export_llm_events import read_llm_events
from magebench.game.game_export_types import GameStartEvent

MANIFEST = {
    "resolved": {"auto_resolve_forced": True, "mulligan": "engine-rule"},
    "constants": {"max_tokens": 2048},
    "requested_but_unread": {"MAGEBENCH_AUTO_RESOLVE_EMPTY_PRIORITY": "1"},
}


def _seat_log(tmp_path: Path, game_start: dict) -> Path:
    game_dir = tmp_path / "game_20260907_000000"
    game_dir.mkdir()
    (game_dir / "Eval00_llm.jsonl").write_text(json.dumps(game_start) + "\n")
    return game_dir


def _start(**extra) -> dict:
    return {"ts": "2026-09-07T00:00:00", "seq": 1, "type": "game_start",
            "player": "Eval00", "model": "m", "available_tools": ["pass_priority"], **extra}


def test_the_manifest_reaches_the_exported_game(tmp_path):
    events, *_ = read_llm_events(_seat_log(tmp_path, _start(settings=MANIFEST)))
    start = [e for e in events if e["type"] == "game_start"]
    assert len(start) == 1
    assert start[0]["settings"] == MANIFEST
    assert start[0]["settings"]["requested_but_unread"] == {
        "MAGEBENCH_AUTO_RESOLVE_EMPTY_PRIORITY": "1"
    }, "the incident field must survive the export, or it answers nothing downstream"


def test_a_game_without_a_manifest_exports_no_key_rather_than_null(tmp_path):
    """Positive control, and the distinction that matters for pooling.

    Every game generated before the manifest existed has no settings. Exporting
    `null` would make "predates the instrument" look like "the instrument ran and
    resolved nothing" -- and those two must stay distinguishable, because one is a
    corpus whose settings are unrecoverable and the other is a corpus that
    described itself.
    """
    events, *_ = read_llm_events(_seat_log(tmp_path, _start()))
    start = [e for e in events if e["type"] == "game_start"][0]
    assert "settings" not in start


def test_the_typed_reader_carries_it(tmp_path):
    """A reader using the dataclass sees the field rather than losing it to _extras."""
    event = GameStartEvent(type="game_start", player="Eval00", ts="t", seq=1,
                           model="m", settings=MANIFEST)
    assert event.settings["constants"]["max_tokens"] == 2048
    assert GameStartEvent(type="game_start", player="Eval00", ts="t", seq=1).settings is None
