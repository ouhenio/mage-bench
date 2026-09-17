#!/usr/bin/env python
"""Compile the bound-choice grammar against the real tokenizer and prove what it rejects.

WHY THIS IS A GATE SCRIPT AND NOT ONLY A PYTEST CASE. The two venvs are disjoint: the repo's
test venv has pytest and no xgrammar/transformers; the serving venv (vllm-serve2) has
xgrammar and transformers and no pytest. So the compiled-grammar assertions inside
`tests/test_decision_schema.py` **skip in every environment that can run the suite** -- a
check that cannot fail is not a check, and this project has now been bitten by that shape
three times in a week.

So the proof lives here, exits non-zero when it fails, and the eval's sbatch runs it BEFORE
spending a card. The pytest file keeps the shape assertions, which do run everywhere.

    <serving venv>/bin/python pipelines/prove_decision_grammar.py --model <snapshot dir>

Exit 0 = every case behaved as registered. Exit 1 = a case failed, and the run must not
proceed: a guard that does not reject is worse than no guard, because the arm is labelled
guarded.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

DEFAULT_SNAPSHOTS = pathlib.Path(
    "/workspace1/projects/posttrainlatamgpt/hub/models--Qwen--Qwen3.5-4B/snapshots"
)
TOOLS = ["choose_action", "get_game_state", "pass_priority"]
IDS = ["a1", "a2", "a3"]


def qwen_call(tool: str, field: str | None = None, value: str | None = None) -> str:
    inner = f"<parameter={field}>\n{value}\n</parameter>\n" if field else "anything at all\n"
    return f"<tool_call>\n<function={tool}>\n{inner}</function>\n</tool_call>"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default=None, help="tokenizer dir; default: first 4B snapshot")
    ap.add_argument("--json", default=None, help="write the result here as JSON evidence")
    args = ap.parse_args()

    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
    from magebench.pilot.decision_schema import (  # noqa: E402
        CHOICE_FIELD,
        CHOICE_TOOL,
        decision_structural_tag,
    )

    import xgrammar as xgr  # noqa: E402
    from transformers import AutoTokenizer  # noqa: E402

    model = args.model
    if model is None:
        snaps = sorted(DEFAULT_SNAPSHOTS.glob("*")) if DEFAULT_SNAPSHOTS.is_dir() else []
        if not snaps:
            print(f"FATAL: no tokenizer under {DEFAULT_SNAPSHOTS}; pass --model", file=sys.stderr)
            return 2
        model = str(snaps[0])

    tag = decision_structural_tag(TOOLS, IDS)
    tok = AutoTokenizer.from_pretrained(model)
    compiler = xgr.GrammarCompiler(xgr.TokenizerInfo.from_huggingface(tok))
    # Single-argument form, matching vLLM 0.27.1's own call for a tag with no "structures"
    # key (backend_xgrammar.py:110). A different call here would prove a different object
    # than the server compiles.
    grammar = compiler.compile_structural_tag(json.dumps(tag))

    def accepts(text: str) -> bool:
        return xgr.GrammarMatcher(grammar).accept_string(text)

    cases = [
        ("an enumerated id is accepted", qwen_call(CHOICE_TOOL, CHOICE_FIELD, "a1"), True),
        ("a second enumerated id too", qwen_call(CHOICE_TOOL, CHOICE_FIELD, "a3"), True),
        ("an id OUTSIDE the enum is rejected", qwen_call(CHOICE_TOOL, CHOICE_FIELD, "a9"), False),
        ("a free-form answer is rejected", qwen_call(CHOICE_TOOL, CHOICE_FIELD, "attack all"), False),
        # Controls for "only the choice argument". A constraint that also reshaped other
        # tools or suppressed prose would fail these and would otherwise look like success.
        ("an unbound tool keeps free arguments", qwen_call("get_game_state"), True),
        ("an invented tool name is still rejected", qwen_call("ghost_pass"), False),
        ("prose with no tool call is untouched", "I think I should attack here.", True),
    ]

    results = []
    for label, text, expected in cases:
        got = accepts(text)
        results.append({"case": label, "accepted": got, "expected": expected, "ok": got == expected})
        print(f"  {'OK ' if got == expected else 'FAIL'}  {label:<38} accepted={got} expected={expected}")

    # The positive control on the METHOD: if the matcher said False to everything, every
    # rejection above would pass for the wrong reason.
    yes = accepts(qwen_call(CHOICE_TOOL, CHOICE_FIELD, "a1"))
    no = accepts(qwen_call(CHOICE_TOOL, CHOICE_FIELD, "a9"))
    method_ok = (yes, no) == (True, False)
    print(f"  {'OK ' if method_ok else 'FAIL'}  method control: matcher returns both answers")

    failed = [r["case"] for r in results if not r["ok"]] + ([] if method_ok else ["method control"])
    out = {
        "model": model,
        "tools": TOOLS,
        "ids": IDS,
        "tag": tag,
        "cases": results,
        "method_control_ok": method_ok,
        "failed": failed,
    }
    if args.json:
        pathlib.Path(args.json).write_text(json.dumps(out, indent=2) + "\n")
        print(f"  evidence -> {args.json}")

    if failed:
        print(f"FATAL: {len(failed)} case(s) failed: {failed}", file=sys.stderr)
        print("  A guard that does not reject is worse than no guard: the arm is labelled", file=sys.stderr)
        print("  guarded. Refusing before a card is spent.", file=sys.stderr)
        return 1
    print(f"all {len(cases)} cases + method control passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
