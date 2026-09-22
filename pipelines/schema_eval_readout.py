#!/usr/bin/env python3
"""The schema eval's readout, in the registered order, gated on the treatment having fired.

    schema_eval_readout.py --root <evidence/opd> [--root ...] [--registered-coverage 0.82]

WHY COVERAGE COMES FIRST AND GATES EVERYTHING. Job 9960 produced 200 games of "guarded" evidence
in which the enum was never applied to a single decision -- 652 of 652 rows read `no decision
blob` -- because the binding site read a field the message builder had already consumed. Nothing
about those games looked wrong. A win rate computed from them would have been a real number,
computed correctly, answering a question nobody asked: it would have compared the control to
itself.

So this refuses to print a win rate until it has established, PER GAME, that the treatment
reached the wire. A run where binding stops silently after game 50 is the same defect wearing a
later timestamp, and only a per-game check sees it -- a pooled fraction of 41% would look like
"82% coverage on half the games" and like "41% coverage throughout", which are not the same run.

THE REGISTERED FIGURE IS ~82% (1,331 of 1,624 decisions bindable, measured on recorded frames
before this branch ran anything live). It is an expectation, not a threshold: the live decision
mix is not the recorded one. A large gap is reported as a finding to explain, not as a failure --
but ZERO bound in any game is a failure, because it is 9960.
"""

from __future__ import annotations

import argparse
import collections
import json
import pathlib
import sys

COVERAGE = "decision_schema_coverage"


def arm_dirs(roots: list[pathlib.Path], arm: str) -> list[pathlib.Path]:
    # `schema-eval-<arm>-*` only. A dir renamed out of that shape is excluded ON PURPOSE:
    # INVALID-bound-nothing-schema-eval-guarded-9960 holds four games produced with the
    # treatment silently absent, and they were briefly counted as banked by the complement.
    return sorted((d for r in roots for d in r.glob(f"schema-eval-{arm}-*") if d.is_dir()),
                  key=lambda d: d.name)


def per_game_coverage(d: pathlib.Path) -> dict[str, dict]:
    """{game dir name: {bound, unbound, reasons}} for every game that has a seat log."""
    out: dict[str, dict] = {}
    for g in sorted(d.glob("g*")):
        if not g.is_dir():
            continue
        row = {"bound": 0, "unbound": 0, "reasons": collections.Counter(),
               "by_class": collections.Counter()}
        seen = False
        for f in g.rglob("*_llm.jsonl"):
            seen = True
            for line in f.open():
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                if r.get("type") != COVERAGE:
                    continue
                # THE CLASS IS (action_type, response_type). The registered ~82% was measured
                # on recorded frames, and the live decision MIX is the first candidate for any
                # gap: the same rule applied to a different distribution of decisions gives a
                # different coverage without anything being wrong. Splitting by class is what
                # turns "68% vs 82%" from a discrepancy into an account of one.
                cls = (r.get("action_type"), r.get("response_type"))
                row["by_class"][(cls, bool(r.get("bound")))] += 1
                if r.get("bound"):
                    row["bound"] += 1
                else:
                    row["unbound"] += 1
                    row["reasons"][r.get("reason")] += 1
        if seen:
            out[g.name] = row
    return out


def ended(d: pathlib.Path) -> set[str]:
    out = set()
    for g in sorted(d.glob("g*")):
        if g.is_dir() and any(
            "game_end" in p.read_text(errors="ignore")
            for p in g.rglob("server_game_events.jsonl")
        ):
            out.add(g.name)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True, action="append")
    ap.add_argument("--registered-coverage", type=float, default=0.82)
    args = ap.parse_args()
    roots = [pathlib.Path(r) for r in args.root]
    missing = [r for r in roots if not r.is_dir()]
    if missing:
        print(f"FATAL: unreadable root(s): {missing}", file=sys.stderr)
        return 2

    print("=" * 78)
    print("STEP 1 — DID THE TREATMENT FIRE?  (gates everything below)")
    print("=" * 78)

    gate_ok = True
    for arm in ("guarded", "unguarded"):
        dirs = arm_dirs(roots, arm)
        games = {}
        for d in dirs:
            for name, row in per_game_coverage(d).items():
                games[f"{d.name}/{name}"] = row
        finished = sum(len(ended(d)) for d in dirs)
        b = sum(g["bound"] for g in games.values())
        u = sum(g["unbound"] for g in games.values())
        n = b + u
        print(f"\n  arm={arm}: {len(dirs)} dir(s), {len(games)} game(s) with a seat log, "
              f"{finished} with an engine game_end")
        if arm == "unguarded":
            # The control emits no coverage rows by design: the branch that writes them runs
            # only under bound_choice. Rows here would mean the arms are not what they say.
            if n:
                print(f"  FAIL: the CONTROL emitted {n} coverage rows. The control must not be "
                      f"running the treatment.")
                gate_ok = False
            else:
                print("  ok: no coverage rows, as the control should have "
                      "(the emitting branch runs only under bound_choice)")
            continue

        if n == 0:
            print("  FAIL: zero coverage rows in the guarded arm. Either no game ran, or the "
                  "emitting branch never executed. This is job 9960.")
            gate_ok = False
            continue
        frac = b / n
        print(f"  decisions {n}: bound {b} ({100*frac:.1f}%), unbound {u}")
        print(f"  registered expectation ~{100*args.registered_coverage:.0f}% "
              f"(1,331/1,624 on recorded frames) — an expectation, not a threshold")
        zero = sorted(k for k, g in games.items() if g["bound"] == 0)
        if zero:
            print(f"  FAIL: {len(zero)} game(s) bound ZERO decisions — binding was absent for "
                  f"whole games, which a pooled fraction hides:")
            for k in zero[:10]:
                print(f"      {k}  (unbound {games[k]['unbound']}, "
                      f"{dict(games[k]['reasons'].most_common(2))})")
            gate_ok = False
        else:
            per = sorted(g["bound"] / max(1, g["bound"] + g["unbound"]) for g in games.values())
            print(f"  ok: every game bound at least one decision; per-game bound fraction "
                  f"min {per[0]:.2f} / median {per[len(per)//2]:.2f} / max {per[-1]:.2f}")
        reasons = collections.Counter()
        classes: collections.Counter = collections.Counter()
        for g in games.values():
            reasons.update(g["reasons"])
            classes.update(g["by_class"])
        print("  unbound by reason:")
        for why, k in reasons.most_common():
            print(f"      {k:>6}  {why}")

        # THE CLASS SPLIT, which is what the 82% has to be compared against.
        per_class: dict = {}
        for (cls, was_bound), k in classes.items():
            d = per_class.setdefault(cls, {True: 0, False: 0})
            d[was_bound] += k
        print(f"  coverage by (action_type, response_type) — {len(per_class)} class(es), "
              f"most frequent first:")
        print(f"      {'bound':>6}{'total':>7}{'  cov':>7}  class")
        for cls, d in sorted(per_class.items(), key=lambda kv: -(kv[1][True] + kv[1][False])):
            tot = d[True] + d[False]
            at, rt = cls
            print(f"      {d[True]:>6}{tot:>7}{100*d[True]/tot:>6.0f}%  "
                  f"action={at!r} respond_with={rt!r}")
        unbindable = sum(t[False] for t in per_class.values() if t[True] == 0)
        print(f"  UNBINDABLE-BY-DESIGN share: {unbindable}/{n} "
              f"({100*unbindable/n:.1f}%) sit in classes where nothing binds at all")

    print()
    if not gate_ok:
        print("=" * 78)
        print("REFUSING TO REPORT A WIN RATE.")
        print("The treatment did not demonstrably reach every game, so a guarded-vs-unguarded")
        print("comparison would be measuring the control against itself, correctly and")
        print("meaninglessly — which is exactly what job 9960 was ready to hand us.")
        print("=" * 78)
        return 1
    print("gate PASSED — coverage established per game; the rest of the readout may run.")
    rest(roots)
    return 0


MARK = "not found in current choices"


def _rows(f: pathlib.Path):
    for line in f.open():
        try:
            yield json.loads(line)
        except Exception:
            continue


def rest(roots: list[pathlib.Path]) -> None:
    """Steps 2-5, in the registered order. Win rate is last on purpose."""
    sys.path.insert(0, "/workspace1/users/ouhenio/mtg/pipelines/eval")
    from score_suite import score_game  # the engine record; never the `exit` marker

    print()
    print("=" * 78)
    print("STEP 2 — OFF-MENU IDS AND INVENTED TOOL NAMES")
    print("=" * 78)
    print("""  Read from the pilot's OWN records only. The control writes no coverage rows, and the
  decision its pilot would have bound is not recoverable from the log: `auto_resolved`
  records only what was SKIPPED, and a reconstruction from tool results disagreed with the
  guarded arm's ground truth on 21% of decisions -- every one a DIFFERENT decision, never a
  different verdict on the same one. So the control is a TOTAL and is not split by class.
  Guarded answers are attributed by ORDER (coverage seq k -> response k+1 -> call k+2),
  verified exact before use.""")
    for arm in ("unguarded", "guarded"):
        c = collections.Counter(); invented = 0
        for d in arm_dirs(roots, arm):
            for f in d.rglob("*_llm.jsonl"):
                last_cov = None
                for r in _rows(f):
                    t = r.get("type")
                    if t == "decision_schema_coverage":
                        last_cov = r
                    elif t == "tool_call":
                        res = str(r.get("result") or "")
                        if "Unknown tool" in res or "unknown tool" in res.lower():
                            invented += 1
                        if r.get("tool") != "choose_action":
                            continue
                        err = MARK in res
                        if arm == "unguarded":
                            key = "all answers"
                        elif last_cov is None:
                            # A choose_action with NO coverage row of its own: the 2nd..Nth call
                            # of a multi-call response. Its request was bound to the FIRST
                            # decision's enum; it lands on a later decision carrying ids from
                            # the old menu. Not a leak past the enum it was built for, but an
                            # off-menu id produced UNDER a present tag -- so it is its own class.
                            key = "extra call in a multi-call response"
                        else:
                            key = "bound" if last_cov.get("bound") else "unbound (declined by design)"
                        c[(key, err)] += 1
                        last_cov = None
        print(f"\n  arm={arm}")
        for key in sorted({k for k, _ in c}):
            n = c[(key, True)] + c[(key, False)]
            e = c[(key, True)]
            print(f"      {n:>7} answers  {e:>5} off-menu  {100*e/max(1,n):>6.2f}%   {key}")
        tn = sum(c.values()); te = sum(v for (k, e), v in c.items() if e)
        print(f"      {tn:>7} answers  {te:>5} off-menu  {100*te/max(1,tn):>6.2f}%   TOTAL")
        print(f"      invented tool names: {invented}")

    print()
    print("=" * 78)
    print("STEP 3 — MUTE MODE")
    print("=" * 78)
    print("""  Registered as nudge rate and `(none)` share. THERE IS NO NUDGE EVENT in these logs -- no
  event type contains 'nudge' or 'remind' -- so the nudge rate is NOT MEASURABLE from what was
  recorded and is not replaced by a proxy. `(none)` share is measured directly: responses that
  contain no tool call.""")
    for arm in ("unguarded", "guarded"):
        resp = none = errs = 0
        for d in arm_dirs(roots, arm):
            for f in d.rglob("*_llm.jsonl"):
                for r in _rows(f):
                    if r.get("type") == "llm_response":
                        resp += 1
                        none += not (r.get("tool_calls") or [])
                    elif r.get("type") == "llm_error":
                        errs += 1
        print(f"  arm={arm:<10} responses {resp:>7}   (none) {none:>5} = {100*none/max(1,resp):.2f}%"
              f"   llm_error {errs}  (message field EMPTY: counted, not classifiable)")

    print()
    print("=" * 78)
    print("STEP 4 — CASUALTIES BY CLASS")
    print("=" * 78)
    print("""  A casualty is a game with NO engine game_end. The launcher's `exit` marker is shown beside
  it, never used as the count: run B measured it wrong in BOTH directions (84 vs 86, 96 vs 94).""")
    print("""  PER DIR, because a pooled casualty count blends causes that mean opposite things. A game
  killed by a deliberate `scancel` (9959 was cancelled to put both arms on identical bytes) is
  not a harness failure, and five dirs from dead configurations were quarantined with an
  INVALID- prefix on 2026-09-22 after one of them -- renamed with a SUFFIX -- had been
  contributing 24 phantom casualties.""")
    outcomes = {}
    for arm in ("unguarded", "guarded"):
        seen: dict[int, dict] = {}
        print(f"\n  arm={arm}")
        for d in arm_dirs(roots, arm):
            started = ended = 0; cas = collections.Counter()
            for g in sorted(d.glob("g*")):
                if not g.is_dir():
                    continue
                row = score_game(g)
                if not row.get("started"):
                    continue
                started += 1
                if row.get("ended"):
                    ended += 1
                    idx = int(g.name[1:])
                    # DEDUPED BY GAME INDEX: the complement should make a double bank
                    # impossible, and it measured zero across both nodes -- but a readout that
                    # silently double-counts if that ever stops being true is not a readout.
                    if idx in seen:
                        print(f"      WARNING: g{idx:02d} banked twice; keeping the first")
                    else:
                        seen[idx] = row
                else:
                    # NO MARKER IS NOT A DEATH WHILE THE JOB RUNS. The runner writes `exit` only
                    # when a launch stops, so a started game without one is IN FLIGHT if its job
                    # is still running, and was killed hard (e.g. by scancel) if it is not. A
                    # readout run mid-flight that called these casualties would report a
                    # progress bar as a failure rate -- score_suite already warns about exactly
                    # that confusion between "not started" and "aborted".
                    ex = ((g / "exit").read_text().strip() if (g / "exit").exists()
                          else "(no marker: in flight if the job runs, else killed hard)")
                    cas[ex] += 1
            print(f"      {d.name:<44} started {started:>4}  game_end {ended:>4}  "
                  f"casualties {started-ended:>3} {dict(cas) if cas else ''}")
        outcomes[arm] = list(seen.values())
        print(f"      {'distinct banked games':<44} {len(seen)}")

    print()
    print("=" * 78)
    print("STEP 5 — WIN RATE  (last, by registration)")
    print("=" * 78)
    for arm in ("unguarded", "guarded"):
        games = outcomes[arm]
        n = len(games)
        won = sum(1 for g in games if str(g.get("winner") or "").startswith("Eval") and not g.get("draw"))
        draws = sum(1 for g in games if g.get("draw"))
        print(f"  arm={arm:<10} {won}/{n} won ({100*won/max(1,n):.1f}%), {draws} draw(s)")
    print("""
  +/-7.1 points is the registered half-width at n=200: a guarded-vs-unguarded difference
  inside it is UNRESOLVABLE BY DESIGN, not a null result. This eval was powered for the
  off-menu comparison in step 2, not for win rate, and says so before anyone reads step 5.""")


if __name__ == "__main__":
    raise SystemExit(main())
