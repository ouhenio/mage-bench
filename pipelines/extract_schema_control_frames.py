#!/usr/bin/env python
"""Build control (b)'s replay set: frames where the model chose an id that was not on the menu.

THE POPULATION, and why it is this one rather than the 18 invention frames. The 18 invented TOOL
NAMES are the name guard's defect and it was already proven against them. This branch binds
`choose_action`'s `choice` argument, and its defect is a *valid tool with an out-of-range id* --
the engine's own `Object pN not found in current choices`. There are 976 of those across 4452 and
both 4056 arms, which is 54x the invention population and, unlike it, large enough to be
informative.

JOINED ON `game_seq`, NOT `seq`. The two files use different counters: a tool_call's `seq` in
`<seat>_llm.jsonl` is a tool-call index, an llm_call's `seq` in `<seat>_llm_trace.jsonl` is an
LLM-call index. Joining on `seq` recovers 273 of 976 and looks like missing data; joining on
`game_seq`, the engine's own position counter, recovers all 976. An implausibly small yield from
a join is a hypothesis about the key.

Each emitted record carries what a paired replay needs and nothing else:
    request       the exact messages/tools the model saw
    choices       the enumerated options at that position, for building the enum
    respond_with  the engine's declared answer shape, so bindability is decided the same way
                  the pilot decides it rather than re-derived here
    chosen        the out-of-range id the model actually emitted
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

MARKER = "not found in current choices"


def frames_for(root: pathlib.Path) -> list[dict]:
    out: list[dict] = []
    for seat in sorted(root.rglob("*_llm.jsonl")):
        trace = seat.with_name(seat.name.replace("_llm.jsonl", "_llm_trace.jsonl"))
        if not trace.is_file():
            continue

        # Pass 1: the failures, and the decision that was pending at each position.
        failures: dict[int, str] = {}
        decisions: dict[int, dict] = {}
        for line in seat.open():
            if '"tool_call"' not in line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("type") != "tool_call":
                continue
            result = row.get("result")
            if isinstance(result, str):
                try:
                    result = json.loads(result)
                except json.JSONDecodeError:
                    continue
            if not isinstance(result, dict):
                continue
            gseq = row.get("game_seq")
            if gseq is None:
                continue
            # A tool result that carries the pending decision -- ANY tool, because every
            # result carries the next decision. Filtering to get_action_choices here would
            # sample the model's asking policy, which is the defect that produced a 2.4%
            # census elsewhere in this repo.
            if result.get("action_pending") and result.get("choices"):
                decisions[gseq] = result
            if row.get("tool") == "choose_action" and MARKER in str(result.get("error") or ""):
                args = row.get("arguments")
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except json.JSONDecodeError:
                        args = {}
                failures[gseq] = (args or {}).get("choice")

        if not failures:
            continue

        # Pass 2: the request the model was answering, keyed on game_seq.
        #
        # THE UNIT IS EXPLICIT, because the three plausible units differ by 3x and the wrong
        # one silently reweights the control. Measured across 4452 + both 4056 arms:
        #
        #     976   failing choose_action CALLS
        #   2,006   llm_call FRAMES at the positions where those failures happened
        #     684   distinct POSITIONS
        #
        # The gap between 976 and 2,006 is retries: the model answered, was told
        # "Object pN not found in current choices", and answered again at the same
        # `game_seq` with that rejection now in its history -- up to 13 times at one position.
        # A retry frame is a DIFFERENT prompt answering the same position, and it already
        # contains the information that the id was wrong, so replaying retries mixes "does
        # the enum prevent the first mistake" with "does it prevent the repeat".
        #
        # So every frame is emitted, tagged with its attempt index at that position, and the
        # runner selects. `attempt: 0` is the first-attempt-per-position population (684) and
        # is the default; the rest are available without re-extracting.
        seen_at: dict[int, int] = {}
        for line in trace.open():
            if '"llm_call"' not in line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("type") != "llm_call":
                continue
            gseq = row.get("game_seq")
            if gseq not in failures:
                continue
            decision = decisions.get(gseq)
            if decision is None:
                # The failure is real but the decision that framed it was not recorded at
                # this position. Kept out rather than reconstructed: an enum built from a
                # guess is the one thing this control cannot afford.
                continue
            attempt = seen_at.get(gseq, 0)
            seen_at[gseq] = attempt + 1
            out.append(
                {
                    "source": str(seat),
                    "game_seq": gseq,
                    "attempt": attempt,
                    "request": row.get("request"),
                    "choices": decision.get("choices"),
                    "respond_with": decision.get("respond_with"),
                    "action_type": decision.get("action_type"),
                    "chosen": failures[gseq],
                }
            )
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True, help="an evidence dir, e.g. .../v3-h2h-4452")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    root = pathlib.Path(args.root)
    if not root.is_dir():
        print(f"FATAL: no evidence dir at {root}", file=sys.stderr)
        return 2
    frames = frames_for(root)
    if not frames:
        print(f"FATAL: zero frames from {root}. Expected hundreds; a zero here is a broken "
              f"join, not an empty population.", file=sys.stderr)
        return 1
    dest = pathlib.Path(args.out)
    dest.parent.mkdir(parents=True, exist_ok=True)
    with dest.open("w") as fh:
        for f in frames:
            fh.write(json.dumps(f) + "\n")
    firsts = sum(1 for f in frames if f["attempt"] == 0)
    bound_able = sum(1 for f in frames if f.get("choices") and f.get("respond_with"))
    print(f"{root.name}: {len(frames)} frames -> {dest}")
    print(f"  distinct positions (attempt 0):                            {firsts}")
    print(f"  retry frames at those positions:                           {len(frames) - firsts}")
    print(f"  with both choices and respond_with (bindable in principle): {bound_able}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
