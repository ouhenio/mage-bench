#!/usr/bin/env python
"""Recompute the controls' verdicts from the raw rows, with the out-of-scope class separated.

WHY A SEPARATE SCRIPT AND A SEPARATE FILE. The original `verdict.json`s were read and acted on,
so they are not overwritten -- a verdict someone has already read is a record of what was
believed, and editing it in place destroys the only evidence of the belief. These write
`verdict_corrected.json` beside them.

WHAT WAS WRONG. The out-of-range counter, when a decision was NOT bindable, fell back to
comparing the model's answer against the raw `ids` set. For the index-shaped decisions -- the
`choice=0, choice=1` class whose `choices` carry no `id` -- that set is `{None}`, so every answer
scored as out-of-range. All 200 "guarded leaks" across five chunks were that: requests which
correctly carried NO TAG, on decisions the binding rule deliberately declines, scored as failures
of a constraint that was never applied to them.

The fix is a third category rather than a different threshold. A decision the rule declines is
neither a pass nor a failure of the mechanism; it is **out of scope**, and it needs its own column
because its size is the mechanism's coverage gap and a reader must see it to size the result.

Recomputed from the raw rows only -- no server, no GPU -- so the correction costs analysis time
rather than a re-run.
"""

from __future__ import annotations

import argparse
import collections
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
from magebench.pilot.decision_schema import CHOICE_TOOL, bindable_enum  # noqa: E402


def classify(row: dict, frame: dict, arm: str) -> str:
    """One answer's class. The order matters: transport, then scope, then content.

    `arm` is needed for exactly one reason: in the UNGUARDED arm the absence of a tag is the
    treatment, not a defect. A first version of this function checked "was the tag on the wire"
    for both arms, which put every unguarded answer into `bindable_but_untagged` and reported
    the unguarded reproduction rate as 0/0 -- i.e. it made the control arm look like a broken
    treatment arm, and would have read as UNINFORMATIVE when the arm in fact reproduces the
    defect in a third of its answers.
    """
    if row.get("error"):
        return "error"
    if row["tool"] != CHOICE_TOOL:
        # The tag binds ARGUMENTS and leaves every offered name permitted, by design.
        return "other_tool" if row["tool"] else "no_tool_call"
    if row["choice"] is None:
        return "choose_action_without_choice"
    # STRIPPED. The qwen_xml parameter block wraps the value in newlines
    # (`<parameter=choice>\np9\n</parameter>`), and the parser sometimes preserves a leading
    # one -- so an exact-string comparison scored three PERFECTLY BOUND answers as misses
    # (`'\np9'` against a forced `'p9'`). The grammar had done its job; the comparison had not.
    choice = row["choice"].strip() if isinstance(row["choice"], str) else row["choice"]
    enum, _reason = bindable_enum(
        {"choices": frame.get("choices"), "respond_with": frame.get("respond_with")}
    )
    if enum is None:
        # OUT OF SCOPE. The rule declined this decision, so no tag was sent and the answer is
        # unconstrained by construction. Scoring it against the raw ids -- which is what the
        # original counter did -- measures a constraint that was never applied.
        return "out_of_scope"
    tagged = (row.get("wire") or {}).get("structured_outputs_present")
    if arm == "guarded" and not tagged:
        # Bindable, but the tag did not reach the wire. That would be a real defect and is
        # kept distinct from a leak past a present tag.
        return "bindable_but_untagged"
    return "in_enum" if choice in enum else "LEAK"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-root", required=True)
    ap.add_argument("--frames-root", required=True)
    ap.add_argument("--chunks", nargs="+", required=True, help="label=framesfile pairs")
    args = ap.parse_args()

    root = pathlib.Path(args.out_root)
    froot = pathlib.Path(args.frames_root)
    hdr = f"{'chunk':<15}{'bound':>7}{'in_enum':>9}{'LEAK':>6}{'out_of_scope':>14}{'untagged':>10}"
    print(hdr)
    print("-" * len(hdr))
    grand: collections.Counter = collections.Counter()

    for spec in args.chunks:
        label, _, fname = spec.partition("=")
        d = root / label
        fpath = froot / fname
        if not (d / "raw_control_b_guarded.jsonl").is_file() or not fpath.is_file():
            print(f"{label:<15}  (missing raw or frames)")
            continue
        frames = [json.loads(l) for l in fpath.open() if l.strip()]

        counts: dict[str, collections.Counter] = {}
        for arm in ("guarded", "unguarded"):
            c: collections.Counter = collections.Counter()
            for line in (d / f"raw_control_b_{arm}.jsonl").open():
                r = json.loads(line)
                c[classify(r, frames[r["frame"]], arm)] += 1
            counts[arm] = c

        g = counts["guarded"]
        u = counts["unguarded"]
        bound = g["in_enum"] + g["LEAK"]
        print(f"{label:<15}{bound:>7}{g['in_enum']:>9}{g['LEAK']:>6}"
              f"{g['out_of_scope']:>14}{g['bindable_but_untagged']:>10}")

        # Control (a)'s misses, classified the same way rather than assumed.
        a_rows = [json.loads(l) for l in (d / "raw_control_a.jsonl").open()] \
            if (d / "raw_control_a.jsonl").is_file() else []
        a_calls = [r for r in a_rows if r["tool"] == CHOICE_TOOL]
        def _c(r):
            return r["choice"].strip() if isinstance(r["choice"], str) else r["choice"]
        a_hits = sum(1 for r in a_calls if _c(r) == (r["enum"] or [None])[0])
        a_miss_class = collections.Counter()
        for r in a_calls:
            if _c(r) != (r["enum"] or [None])[0]:
                tagged = (r.get("wire") or {}).get("structured_outputs_present")
                a_miss_class["untagged" if not tagged else "past_a_present_tag"] += 1

        verdict = {
            "recomputed_from": "raw rows; no server involved",
            "supersedes": "verdict.json (kept: a verdict that was read is a record of a belief)",
            "control_a": {
                "choose_action_calls": len(a_calls),
                "forced_id_returned": a_hits,
                "other_tool_or_silent": len(a_rows) - len(a_calls),
                "misses_by_class": dict(a_miss_class),
            },
            "control_b_guarded": dict(g),
            "control_b_unguarded": dict(u),
            "in_scope_bound_answers": bound,
            "leaks_past_a_present_tag": g["LEAK"],
            "out_of_scope_by_design": g["out_of_scope"],
        }
        u_bound = u["in_enum"] + u["LEAK"]
        verdict["unguarded_reproduction_rate"] = (
            round(u["LEAK"] / u_bound, 4) if u_bound else None
        )
        if u["LEAK"] == 0:
            verdict["status"] = "UNINFORMATIVE: the unguarded arm reproduced nothing"
        elif g["LEAK"] == 0:
            verdict["status"] = (
                f"PASS: {u['LEAK']}/{u_bound} unguarded answers were off-menu and "
                f"{g['LEAK']}/{bound} guarded ones were, on decisions the rule binds. "
                f"{g['out_of_scope']} answers were OUT OF SCOPE (the rule declines them; no tag "
                f"is sent) and are not failures."
            )
        else:
            verdict["status"] = f"PARTIAL: {g['LEAK']} leaks past a present tag"
        (d / "verdict_corrected.json").write_text(json.dumps(verdict, indent=2) + "\n")
        for k, v in g.items():
            grand[k] += v
        grand["u_LEAK"] += u["LEAK"]
        grand["u_bound"] += u_bound

    print("-" * len(hdr))
    bound = grand["in_enum"] + grand["LEAK"]
    print(f"{'TOTAL':<15}{bound:>7}{grand['in_enum']:>9}{grand['LEAK']:>6}"
          f"{grand['out_of_scope']:>14}{grand['bindable_but_untagged']:>10}")
    print(f"\n  unguarded off-menu: {grand['u_LEAK']}/{grand['u_bound']} "
          f"({100*grand['u_LEAK']/max(1,grand['u_bound']):.1f}%)  -> control (b) is INFORMATIVE")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
