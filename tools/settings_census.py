#!/usr/bin/env python3
"""What was in force across a corpus, and where it disagrees with itself.

    tools/settings_census.py <dir> [<dir> ...]

Reads the `settings` object on each game's `game_start` row and groups games by
the exact settings they ran under. Three things it is for:

  * **Pooling.** Two blocks of games are one corpus only if they ran under the
    same settings. This prints the groups, so that is a fact rather than an
    assumption.
  * **The unread flag.** Any game whose `requested_but_unread` is non-empty asked
    for something the build could not honour. That is the incident of 2026-09-07,
    where one node's `MAGEBENCH_AUTO_RESOLVE_EMPTY_PRIORITY=1` was read by nothing
    and the two nodes' corpora differed by 63% of their decisions.
  * **Games with NO manifest at all**, counted separately and loudly. Every game
    generated before this instrumentation landed is in that bucket, and its
    settings are NOT recoverable from the corpus -- "unknown" is the answer, and
    printing it as its own line is the only way it does not read as "same as the
    others".

A game is located by its `game_start` row, in `game.jsonl` or a `*_llm.jsonl`
seat log. Games are keyed by directory so a merged log is not counted twice.
"""

from __future__ import annotations

import collections
import json
import sys
from pathlib import Path


def _game_start_settings(path: Path):
    """(found_game_start, settings-or-None) for one log file."""
    found = False
    try:
        with path.open() as fh:
            for line in fh:
                if '"game_start"' not in line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if row.get("type") != "game_start":
                    continue
                found = True
                settings = row.get("settings")
                if settings is not None:
                    return True, settings
    except (OSError, UnicodeDecodeError):
        return False, None
    return found, None


def scan(root: Path) -> None:
    by_settings: collections.Counter = collections.Counter()
    unread_games: list[tuple[str, dict]] = []
    no_manifest = 0
    no_game_start = 0
    seen: set[Path] = set()

    logs = sorted(root.rglob("game.jsonl")) + sorted(root.rglob("*_llm.jsonl"))
    for f in logs:
        if f.parent in seen:
            continue
        found, settings = _game_start_settings(f)
        if not found:
            no_game_start += 1
            continue
        seen.add(f.parent)
        if settings is None:
            no_manifest += 1
            continue
        by_settings[json.dumps(settings.get("resolved"), sort_keys=True)] += 1
        # A manifest written by a build that predates a later field would have no
        # such key; that is data, not a fallback, so it is spelled out rather than
        # collapsed into `or {}`.
        unread = settings["requested_but_unread"] if "requested_but_unread" in settings else {}
        if unread:
            unread_games.append((str(f.parent), unread))

    print(f"\n### {root}")
    print(f"  games with a settings manifest: {sum(by_settings.values())}")
    print(f"  games with a game_start but NO manifest: {no_manifest}"
          + ("   <- settings NOT recoverable for these" if no_manifest else ""))
    if no_game_start:
        print(f"  files with no game_start row (in flight or aborted): {no_game_start}")
    if len(by_settings) > 1:
        print(f"  ** {len(by_settings)} DISTINCT settings groups in one directory. These games")
        print("     are not one corpus; pooling them is a decision, not a default.")
    for i, (blob, n) in enumerate(by_settings.most_common(), 1):
        print(f"  --- group {i}: {n} games")
        for k, v in json.loads(blob).items():
            print(f"        {k} = {v}")
    if unread_games:
        print(f"  ** {len(unread_games)} games REQUESTED a setting this build does not read:")
        for d, unread in unread_games[:5]:
            print(f"        {d}: {unread}")
        if len(unread_games) > 5:
            print(f"        ... and {len(unread_games) - 5} more")


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    for arg in argv:
        root = Path(arg)
        if not root.exists():
            print(f"FATAL: {root} does not exist", file=sys.stderr)
            return 1
        scan(root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
