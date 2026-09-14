#!/usr/bin/env python3
"""Count calls cut off with a tool call already open, per model.

    tools/count_cap_hits.py <evidence-dir> [<evidence-dir> ...]

`cap_hit_with_call` is a NAMED COLUMN here because the population was invisible to
every counter we had: when the parser finds a call, `finish_reason` is
"tool_calls" and not "length", so a completion cut off mid-call was counted
nowhere. It needs its own counter rather than a footnote on the truncation rate.

THE PREDICATE IS IMPORTED, NOT REIMPLEMENTED -- `magebench.pilot.cap_retry.classify`,
the same function the live retry uses. Two counts of this population have to be
the same object, and a criterion agreed in prose and written twice is two
criteria. Any other census (karn-research's classify_out_of_schema.py) should
import the same function rather than match its wording.

Columns, nested, each meaningless without the one above it:

    calls                 llm_call rows with a parseable response
    at_cap_any            completion used every token, tool call or not
    finish_length         the OTHER truncation shape, for comparison
    cap_hit_with_call     the POPULATION: a call open when the cap landed,
                          whether or not anything about it looks unfinished
    retry_trigger         of those, the three-term predicate: name not offered OR
                          arguments empty. This is what the live retry fires on,
                          and it is a SUBSET -- a cap-hit whose call looks complete
                          is in the population and not in the trigger
    ... out_of_schema     of the trigger, a name not in that request's offered tools
    ... name_is_prefix    of those, a PREFIX of a real name -- truncation wearing
                          an invention's clothes
    ... genuine_at_cap    of those, NOT a prefix: an invention that also hit the cap
    ... args_empty        of those, arguments empty or unparseable

`undecidable` counts rows whose request carries no max_tokens: the condition
cannot be evaluated there, which is not the same as evaluating to false.
"""

from __future__ import annotations

import collections
import json
import sys
from multiprocessing import Pool
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from magebench.pilot.cap_retry import cap_hit_from_trace_row  # noqa: E402


def scan(path_str: str) -> collections.Counter:
    counter: collections.Counter = collections.Counter()
    try:
        with open(path_str) as handle:
            for line in handle:
                if '"llm_call"' not in line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if row.get("type") != "llm_call":
                    continue
                request = row.get("request")
                response = row.get("response")
                if not isinstance(request, dict) or not isinstance(response, dict):
                    continue
                choices = response.get("choices")
                if not isinstance(choices, list) or not choices:
                    continue
                model = request.get("model")
                if model is None:
                    model = "(model absent from request)"
                counter[(model, "calls")] += 1
                usage = response.get("usage")
                completion = usage.get("completion_tokens") if isinstance(usage, dict) else None
                cap = request.get("max_tokens")
                message = choices[0].get("message")
                tool_calls = message.get("tool_calls") if isinstance(message, dict) else None
                at_cap = isinstance(completion, int) and isinstance(cap, int) and completion >= cap
                if at_cap:
                    counter[(model, "at_cap_any")] += 1
                if choices[0].get("finish_reason") == "length":
                    counter[(model, "finish_length")] += 1
                # THE BROAD POPULATION, counted independently of the retry trigger.
                # These are two different questions and an earlier version of this
                # file answered the second under the first one's name: 33 against
                # the 47 an ad-hoc census reported for the same corpus. The 14
                # missing were cap-hits whose call looked COMPLETE -- a real name
                # with real arguments -- which belong in the population and not in
                # the trigger.
                if at_cap and choices[0].get("finish_reason") == "tool_calls" and tool_calls:
                    counter[(model, "cap_hit_with_call")] += 1

                hit, detail = cap_hit_from_trace_row(row)
                if detail.get("undecidable"):
                    counter[(model, "undecidable")] += 1
                    continue
                if not hit:
                    continue
                counter[(model, "retry_trigger")] += 1
                if not detail.get("name_offered"):
                    counter[(model, "out_of_schema")] += 1
                    key = "name_is_prefix" if detail.get("name_is_prefix_of_offered") else "genuine_at_cap"
                    counter[(model, key)] += 1
                if detail.get("arguments_empty"):
                    counter[(model, "args_empty")] += 1
                if detail.get("tool_name_missing"):
                    counter[(model, "name_missing")] += 1
    except (OSError, UnicodeDecodeError):
        pass
    return counter


COLUMNS = ("finish_length", "at_cap_any", "cap_hit_with_call", "retry_trigger",
           "out_of_schema", "name_is_prefix", "genuine_at_cap", "args_empty",
           "name_missing", "undecidable")


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    for arg in argv:
        root = Path(arg)
        if not root.exists():
            print(f"FATAL: {root} does not exist", file=sys.stderr)
            return 1
        files = [str(p) for p in sorted(root.rglob("*_llm_trace.jsonl"))]
        total: collections.Counter = collections.Counter()
        with Pool(8) as pool:
            for part in pool.map(scan, files, chunksize=2):
                total.update(part)
        print(f"\n### {root}   ({len(files)} traces)")
        for model in sorted({m for m, _ in total}):
            calls = total[(model, "calls")]
            if not calls:
                continue
            print(f"  {model}   calls {calls}")
            for column in COLUMNS:
                value = total[(model, column)]
                print(f"    {column:<20} {value:>7}   ({100 * value / calls:.3f}% of calls)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
