"""Atomic file writes, for paths that a concurrent process may be reading."""

from __future__ import annotations

import os
from pathlib import Path


def atomic_write_text(path: Path, content: str) -> None:
    """Write via a temp file and rename, so a concurrent reader never sees a partial file.

    Deck paths are derived from the deck's NAME, not the game, so two games in one batch can
    resolve to the same file. That used to be safe by accident: setup ran one game at a time, so
    each deck was written, read by that game's spectator and bridge, and only then possibly
    rewritten. Batch setup now launches every spectator before waiting on any of them, so one
    game's JVM can be reading a .dck while another game's setup rewrites it.

    rename(2) is atomic within a filesystem, so a reader sees either the whole old file or the
    whole new one, never a half-written one. The temp name carries the pid so two writers cannot
    collide on the temp file either.
    """
    tmp_path = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp_path.write_text(content)
    tmp_path.replace(path)
