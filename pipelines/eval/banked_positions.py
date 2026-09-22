"""Emit the banked-positions file for a resume: one "<seat> <position>" per line, per game seed.

A resume job replays its game and measures only positions NOT listed here (xmage.rollout.banked).
Window chunking alone cannot do that: a cancelled wave leaves no complete window (2026-09-22,
63 of 360 positions banked, 0 windows complete), so resuming by window re-measures everything.
"""

import json
import sys
from collections import defaultdict
from pathlib import Path

if len(sys.argv) < 3:
    raise SystemExit("usage: banked_positions.py <out-dir> <probe.jsonl>...")
out_dir = Path(sys.argv[1])
out_dir.mkdir(parents=True, exist_ok=True)
by_seed = defaultdict(set)
for path in sys.argv[2:]:
    for line in open(path):
        r = json.loads(line)
        by_seed[r["game_seed"]].add((r["seat"], r["position"]))
for seed, entries in sorted(by_seed.items()):
    f = out_dir / f"banked-{seed}.txt"
    f.write_text("".join(f"{seat} {pos}\n" for seat, pos in sorted(entries)))
    print(f"{f}: {len(entries)} banked position(s)")
