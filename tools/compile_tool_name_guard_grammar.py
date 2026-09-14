#!/usr/bin/env python3
"""Compile the tool-name guard's structural tag and test what it accepts.

    <a venv with xgrammar and transformers>/bin/python tools/compile_tool_name_guard_grammar.py

THE PROOF LIVES HERE RATHER THAN IN A COMMIT MESSAGE, because a proof nobody can
re-run is a claim. It needs xgrammar and the model's tokenizer, neither of which
is a harness dependency, so it is a tool rather than a test: run it on a box that
serves.

Two directions, and the second is the one that makes the first mean anything:

  ACCEPT   every real tool name; prose with no tool call at all; free-form
           non-JSON arguments inside a real call. A guard that suppressed prose or
           reshaped arguments would be a different change from the one we agreed.
  REJECT   every invented name observed in production, including the SYNTAX
           forms -- a doubled opener and an escaped one -- which are covered by
           the tag's construction but were, until this ran, covered by argument
           while the bare names were covered by demonstration. That difference is
           exactly the one this file exists to remove.

It also pins a KNOWN LIMIT rather than hiding it: text that never contains the
trigger is never constrained. If a parser ever reconstructs a call from such text,
the guard does not cover it. No such case has been observed -- the one that looked
like it (`&lt;function=choose_action`, corpus-v3-lascar-3205 g1096) turned out to
carry a real outer trigger -- but the boundary is stated where someone can find it.
"""

import json
import sys

SNAPSHOT = ("/workspace1/projects/posttrainlatamgpt/hub/"
            "models--Qwen--Qwen3.5-4B/snapshots/851bf6e806efd8d0a36b00ddf55e13ccb7b8cd0a")

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent / "src"))
from magebench.pilot.tool_name_guard import tool_name_structural_tag  # noqa: E402

NAMES = ["choose_action", "pass_priority", "get_game_state",
         "get_game_log", "get_oracle_text", "get_action_choices"]

# Every invented name observed in production, by corpus. The two at the end are
# SYNTAX inventions rather than name inventions: the model emitted the opener
# twice, once raw and once HTML-escaped, and the parser swallowed the prefix into
# the name.
INVENTED_BARE = ["choose", "choice", "action", "ghost_pass", "protect_priority"]
INVENTED_SYNTAX = ["<function=choose_action", "&lt;function=choose_action"]


def call(name: str, args: str = '{"choice": "p1"}') -> str:
    return f"<tool_call>\n<function={name}>\n{args}\n</function>\n</tool_call>"


def main() -> int:
    import xgrammar as xgr
    from transformers import AutoTokenizer

    tok = AutoTokenizer.from_pretrained(SNAPSHOT)
    compiled = xgr.GrammarCompiler(
        xgr.TokenizerInfo.from_huggingface(tok)
    ).compile_structural_tag(json.dumps(tool_name_structural_tag(NAMES)))

    def accepts(text: str) -> bool:
        matcher = xgr.GrammarMatcher(compiled)
        return all(matcher.accept_token(t) for t in tok.encode(text, add_special_tokens=False))

    failures = []

    def check(label: str, text: str, want: bool) -> None:
        got = accepts(text)
        mark = "ok " if got == want else "FAIL"
        if got != want:
            failures.append(label)
        print(f"  {mark}  {label:<46} accepted={got} wanted={want}")

    print("real tool names:")
    for n in NAMES:
        check(n, "I will act.\n" + call(n), True)

    print("\ninvented names observed in production (bare):")
    for n in INVENTED_BARE:
        check(n, "I will act.\n" + call(n), False)

    print("\ninvented SYNTAX observed in production (doubled / escaped opener):")
    for n in INVENTED_SYNTAX:
        check(n, "I will act.\n" + call(n), False)

    print("\nonly the name is constrained:")
    check("prose with no tool call", "Let me think about the board first.", True)
    check("free-form non-JSON arguments", call("choose_action", "anything at all"), True)

    print("\nknown limit, pinned rather than hidden:")
    check("escaped opener as PROSE, no real trigger",
          "I considered writing &lt;function=ghost_pass&gt; but did not.", True)

    print()
    if failures:
        print(f"FAILURES: {failures}")
        return 1
    print(f"all {len(NAMES) + len(INVENTED_BARE) + len(INVENTED_SYNTAX) + 3} cases as expected")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
