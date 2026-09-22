#!/usr/bin/env python3
"""The schema eval's unplayed seeds for one arm, by the ENGINE record, across every dir it has.

    schema_eval_complement.py --arm guarded --evidence-root R --score-suite-dir S --count
    schema_eval_complement.py --arm guarded ... --base full.json --out resume.json

Built on karn-research's run B pattern (`pipelines/eval/runB_complement.py`), generalised from
one hard-coded run to an arm, because this eval has TWO arms whose seeds must be pooled and
compared per seed at readout.

BANKED IS AN ENGINE JUDGEMENT, NEVER THE `exit` MARKER. Run B measured the marker wrong in BOTH
directions -- 7927 read exit 84 against engine 86, its resume read exit 96 against engine 94 --
so this reads `game_end` through `score_suite.score_game`. A seed wrongly called banked is a
seed silently dropped from a 200-deal design; a seed wrongly called unplayed is one replayed
into a second dir and double-counted at readout. Draws count as banked.

THE UNIT IS THE GAME, NOT THE SEED. This suite is MIRROR-PAIRED: 200 games over 100 distinct
seeds, two per seed with the decks swapped. A complement keyed on the seed marks the seed
banked as soon as EITHER half finishes and drops the other, which silently halves a paired
design -- measured on 4452's own evidence, where 98 banked games keyed by seed left 4 games
unplayed instead of 102. So the key is the game's index in the base suite, which is exactly
what `run_suite_slots.sh` writes as `g{game:02d}`, and the dir's own `seed` file is asserted
against the base suite's seed for that index so a drifted base cannot pass unnoticed.

EVERY ROOT, OR NONE. An arm can run on both nodes -- lascar and ranokau have SEPARATE
filesystems at the same path, so an arm's dirs are split between them and reachable from either
side only over the cross-mount. A complement computed from one root under-counts what is banked
and queues banked games for replay, which double-counts them at readout. So roots are passed
explicitly, all of them, and a root that does not exist is a REFUSAL rather than a skip: the
cross-mount being down is exactly when a silent skip does the damage.

THE ARMS DO NOT SHARE A COMPLEMENT. Each arm's dirs are globbed separately and a seed banked by
the unguarded arm says nothing about whether the guarded arm has played it. Pooling the two
would silently shrink the guarded suite by whatever the unguarded arm happened to finish first,
which is the one thing a paired design cannot survive.

Dead dirs contribute nothing and are not special-cased: `schema-eval-unguarded-9494-NO-JARS-
WRONG-PRESET` matches the glob and banks zero games, which is exactly what it should
contribute. A dir is included by what its games record, not by what its name claims.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys


_GDIR = re.compile(r"^g(\d+)$")


def banked_by_game(arm_dirs: list[pathlib.Path], score_game,
                   seed_of: dict[int, int]) -> dict[int, tuple[str, int]]:
    """{base game index: (dir name, seed)} for every game the ENGINE finished."""
    banked: dict[int, tuple[str, int]] = {}
    for d in arm_dirs:
        for g in sorted(d.glob("g*")):
            if not g.is_dir():
                continue
            m = _GDIR.match(g.name)
            if m is None:
                continue
            idx = int(m.group(1))
            row = score_game(g)
            if not (row.get("ended") and row.get("seed") is not None):
                continue
            seed = int(row["seed"])
            # THE INDEX IS ONLY A KEY IF IT STILL MEANS THE SAME GAME. A resume suite keeps the
            # base numbering, so g07 is base game 7 in every dir -- unless the base itself
            # changed, in which case this join is quietly wrong in both directions.
            if idx in seed_of and seed_of[idx] != seed:
                raise SystemExit(
                    f"{d.name}/{g.name} carries seed {seed} but the base suite says game {idx} "
                    f"is seed {seed_of[idx]}. The base suite has drifted; this complement would "
                    f"pair the wrong games. Refusing.")
            # setdefault: the FIRST dir that banked a game owns it. A game banked twice is a
            # double-count at readout, and this is where it gets noticed rather than averaged.
            # THE FULL PATH, not the basename: the same label can exist on both nodes and
            # collapsing them would hide which node banked what.
            banked.setdefault(idx, (str(d), seed))
    return banked


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--arm", required=True, choices=["guarded", "unguarded"])
    ap.add_argument("--evidence-root", required=True, action="append", metavar="DIR",
                    help="repeat for every node's evidence/opd; a missing one refuses")
    ap.add_argument("--score-suite-dir", required=True,
                    help="the directory holding score_suite.py (it lives in the mtg repo)")
    ap.add_argument("--base", help="the full suite to take the complement of")
    ap.add_argument("--out", help="where to write the resume suite")
    ap.add_argument("--limit", type=int, default=0,
                    help="keep at most N seeds; 0 = all. N=1 is the one-deal smoke.")
    ap.add_argument("--count", action="store_true", help="print the unplayed count and nothing else")
    ap.add_argument("--exclude-games", default="",
                    help="comma-separated base game indices never to queue, each with a reason on record")
    args = ap.parse_args()

    sys.path.insert(0, args.score_suite_dir)
    try:
        from score_suite import score_game  # noqa: E402
    except ImportError as exc:
        print(f"cannot import score_suite from {args.score_suite_dir}: {exc}", file=sys.stderr)
        return 2

    roots = [pathlib.Path(r) for r in args.evidence_root]
    missing = [r for r in roots if not r.is_dir()]
    if missing:
        print(f"evidence root(s) unreadable: {', '.join(map(str, missing))}. Refusing rather "
              f"than computing a complement from a subset -- a root skipped is banked games "
              f"counted as unplayed, replayed, and double-counted at readout.", file=sys.stderr)
        return 2
    if not args.base:
        print("--base is required: the complement has no denominator without it", file=sys.stderr)
        return 2
    base = json.loads(pathlib.Path(args.base).read_text())
    seed_of = {g["game"]: g["seed"] for g in base["games"]}
    assert len(seed_of) == len(base["games"]), "the base suite repeats a game index"

    arm_dirs = sorted((d for r in roots for d in r.glob(f"schema-eval-{args.arm}-*") if d.is_dir()),
                      key=lambda d: d.name)
    banked = banked_by_game(arm_dirs, score_game, seed_of)
    # EXCLUDED IS NOT BANKED, and the two are kept apart all the way through. A game excluded
    # here is one KNOWN to fail deterministically -- seeds 1200062 and 1200068 freeze the XMage
    # AI's search in both arms and both seatings (Ad Nauseam mirrors), 1200082 dies without a
    # freeze warning (KikiPod). Queueing them again burns a GPU re-failing them. But they must
    # never be counted as banked either: the readout's n has to show 195 of 200, not 200, so the
    # design's shortfall stays visible instead of being absorbed into the partition.
    excluded = {int(x) for x in args.exclude_games.split(",") if x.strip()}
    unknown = excluded - set(seed_of)
    if unknown:
        print(f"--exclude-games names indices not in the base suite: {sorted(unknown)}", file=sys.stderr)
        return 2
    already = excluded & set(banked)
    if already:
        print(f"--exclude-games names games that are ALREADY BANKED: {sorted(already)}. Excluding a "
              f"banked game would silently drop real evidence; refusing.", file=sys.stderr)
        return 2
    replay = [g for g in base["games"] if g["game"] not in banked and g["game"] not in excluded]

    if args.count:
        print(len(replay))
        return 0

    # THE PARTITION IS ASSERTED, not assumed. Every base game is either banked or queued, never
    # both and never neither -- the two ways a resume silently changes the design's n.
    # The partition is now THREE-way, and still asserted: banked + excluded + queued = the suite.
    assert len(replay) + len(banked) + len(excluded) == len(base["games"]), (
        f"partition does not cover the suite: {len(replay)} queued + {len(banked)} banked + "
        f"{len(excluded)} excluded != {len(base['games'])}")
    assert not ({g["game"] for g in replay} & set(banked)), "a banked game is queued for replay"

    if not args.out:
        for d in arm_dirs:
            n = sum(1 for v in banked.values() if v[0] == str(d))
            print(f"   {d} : {n} banked")
        print(f"   arm={args.arm}  BANKED {len(banked)} games over "
              f"{len({s for _, s in banked.values()})} seeds; UNPLAYED {len(replay)}")
        return 0

    limited = replay[: args.limit] if args.limit else replay
    out = dict(base)
    out["games"], out["n_games"] = limited, len(limited)
    # Both recorded: the games, which are what the design counts, and the seeds, which are what
    # the readout pairs on. They differ by the mirror factor and confusing them is the defect
    # this file's header describes.
    out["excluded_games"] = sorted(excluded)
    out["banked_elsewhere_games"] = sorted(banked)
    out["banked_elsewhere_seeds"] = sorted({s for _, s in banked.values()})
    out["banked_by_dir"] = {str(d): sum(1 for v in banked.values() if v[0] == str(d))
                            for d in arm_dirs}
    out["evidence_roots"] = [str(r) for r in roots]
    out["derived_from"] = (
        f"engine game_end across {len(arm_dirs)} dir(s) in {len(roots)} root(s) "
        f"for arm={args.arm}: " + ", ".join(str(d) for d in arm_dirs)
        + (f"; LIMITED to {args.limit} seed(s)" if args.limit else "")
    )
    pathlib.Path(args.out).write_text(json.dumps(out, indent=1) + "\n")
    print(f"arm={args.arm}: {len(banked)} banked games, {len(replay)} unplayed, "
          f"{len(limited)} queued -> {pathlib.Path(args.out).name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
