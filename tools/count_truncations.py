#!/usr/bin/env python3
"""Count how often the completion cap was HIT, not how many games ran.

    tools/count_truncations.py <dir> [<dir> ...]

A run count grows whether or not the condition ever appeared, so it is not
evidence about the condition. This counts the OCCURRENCE -- finish_reason
"length" -- against its denominator, the calls that could have shown it, and
prints the completion-length survival curve, which is what a new cap has to be
sized from.

TWO SOURCES, DELIBERATELY, because they fail differently:

  *_llm_trace.jsonl   the engine's own response, `type: llm_call`. Complete, and
                      independent of any harness bookkeeping -- this is the
                      instrument. Written only when tracing is on.
  game.jsonl          the harness's `completion_truncated` event, one per
  <Player>_llm.jsonl  occurrence -- written to the per-seat log and merged into
                      game.jsonl. Present in every game, and it is what a
                      downstream reader that never sees a trace can count.

They should agree. A disagreement is itself the finding: the harness recording
fewer than the engine emitted means occurrences are being lost before anything
downstream can see them, which is the exact failure this event was added for.

The survival curve is CENSORED at the cap by construction -- nothing above it can
be observed -- so read the decay below the cap and extrapolate explicitly. Do not
report "0 above the cap" as though the tail had been measured.
"""

from __future__ import annotations

import collections
import json
import sys
from pathlib import Path


def _rows(path: Path):
    try:
        with path.open() as fh:
            for line in fh:
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue
    except (OSError, UnicodeDecodeError):
        return


def scan(root: Path) -> dict:
    calls = 0
    trunc_trace = 0
    trunc_event = 0
    comp: list[int] = []
    caps: collections.Counter = collections.Counter()
    trace_games: set[str] = set()
    hit_games: set[str] = set()
    event_games: set[str] = set()
    seen_dirs: set[Path] = set()

    for f in sorted(root.rglob("*_llm_trace.jsonl")):
        trace_games.add(str(f))
        for d in _rows(f):
            if d.get("type") != "llm_call":
                continue
            response = d.get("response") or {}
            choices = response.get("choices") or []
            if not choices:
                continue
            calls += 1
            caps[(d.get("request") or {}).get("max_tokens")] += 1
            usage = response.get("usage") or {}
            tokens = usage.get("completion_tokens")
            if isinstance(tokens, int):
                comp.append(tokens)
            if choices[0].get("finish_reason") == "length":
                trunc_trace += 1
                hit_games.add(str(f))

    # The writer emits into <Player>_llm.jsonl, and merge_game_log folds those
    # into game.jsonl. Both are read, and a merged game is counted once: a game
    # directory holding both would otherwise double every occurrence in it.
    for f in sorted(root.rglob("game.jsonl")):
        seen_dirs.add(f.parent)
        for d in _rows(f):
            if d.get("type") == "completion_truncated":
                trunc_event += 1
                event_games.add(str(f))
    for f in sorted(root.rglob("*_llm.jsonl")):
        if f.parent in seen_dirs:
            continue
        for d in _rows(f):
            if d.get("type") == "completion_truncated":
                trunc_event += 1
                event_games.add(str(f))

    return {
        "calls": calls,
        "trunc_trace": trunc_trace,
        "trunc_event": trunc_event,
        "comp": sorted(comp),
        "caps": dict(caps),
        "trace_games": len(trace_games),
        "hit_games": len(hit_games),
        "event_games": len(event_games),
    }


def report(root: Path, r: dict) -> None:
    calls = r["calls"]
    comp = r["comp"]
    print(f"\n### {root}")
    print(f"  llm_call rows           {calls} in {r['trace_games']} traces")
    print(f"  max_tokens sent         {r['caps'] or '(no traces)'}")
    pct = 100 * r["trunc_trace"] / calls if calls else 0.0
    print(f"  TRUNCATED (trace)       {r['trunc_trace']}  ({pct:.2f}% of calls, "
          f"{r['hit_games']} traces with >=1)")
    print(f"  TRUNCATED (game.jsonl)  {r['trunc_event']}  ({r['event_games']} game logs)")
    if r["trace_games"] and r["trunc_event"] != r["trunc_trace"]:
        print("  ** the two sources DISAGREE. If game.jsonl is short, occurrences are")
        print("     being lost before any downstream reader can count them.")
    if not comp:
        print("  no completion_tokens recorded; nothing to size a cap from")
        return
    n = len(comp)
    print(f"  completion_tokens       p50 {comp[n // 2]}  p90 {comp[int(n * .9)]}  "
          f"p99 {comp[int(n * .99)]}  max {comp[-1]}")
    print("  survival (censored at the cap -- the tail above it is unobservable):")
    for t in (128, 256, 512, 768, 896, 960, 1024, 1536, 2048):
        over = sum(1 for c in comp if c > t)
        if over or t < (comp[-1] if comp else 0):
            print(f"    > {t:>5}: {over:>7}  ({100 * over / n:.2f}%)")


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    for arg in argv:
        root = Path(arg)
        if not root.exists():
            # Fail loud: a missing path silently contributing zero is how a census
            # comes back clean because it read nothing.
            print(f"FATAL: {root} does not exist", file=sys.stderr)
            return 1
        report(root, scan(root))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
