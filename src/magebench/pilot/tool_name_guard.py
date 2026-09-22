"""Make an invented tool name impossible to emit, without touching anything else.

THE DEFECT. Across the 4452 head-to-head, 7 of the 8 policy-determined deaths were
a hallucinated MCP tool name -- `choose`, `choice`, `action`, `ghost_pass`,
`protect_priority` -- each fatal with `ToolExecutionError: Unknown tool`, none of
them about Magic. All 7 arrived as SERVER-PARSED `message.tool_calls` with
`finish_reason: "tool_calls"`, not through the harness's unwrapped-call recovery,
so the name was generated inside a well-formed tool call and the fix belongs at
generation time rather than in recovery.

WHY THE SERVER DID NOT ALREADY STOP IT, which is the part worth knowing. vLLM's
strict tool calling is ON by default (`VLLM_ENFORCE_STRICT_TOOL_CALLING = True` in
both 0.26.0 and 0.27.1), and the Qwen3 tool parser declares
`structural_tag_model = "qwen_3_coder"`. But the builder short-circuits:

    if tool_choice == "auto" and not _any_tool_strict(tools):
        return None
        -- vllm/tool_parsers/structural_tag_registry.py

The harness sends `tool_choice: "auto"` with tools that carry no `strict` flag, so
NO structural tag is built and the function name is unconstrained. Verified by
calling vLLM's own builder: strict=False returns None; strict=True returns a
`triggered_tags` tag enumerating exactly the offered names.

WHY NOT JUST SET `strict: true`, which is the one-line version. vLLM's tag also
attaches each tool's JSON schema as the tag CONTENT, so arguments become
schema-constrained along with the name. That is a second change riding on the
first, and it collides with the open p2 recording that a hallucinated ARGUMENT is
survivable where a hallucinated NAME is fatal. So this builds the same tag from
the same template and replaces the content with `any_text`: the name is
constrained, the arguments are not, and prose outside a tool call is untouched.

PROVEN BY COMPILING THE GRAMMAR, not by reading the spec. Against the real 4B
tokenizer, with xgrammar:

    6 of 6 real tool names          accepted
    5 of 5 invented names observed  REJECTED (choose, choice, action,
                                     ghost_pass, protect_priority)
    prose with no tool call         accepted
    free-form, non-JSON arguments   accepted

The last two are the controls for "only the name": a constraint that also
suppressed prose or reshaped arguments would fail them.

FORMAT PROVENANCE. The trigger and begin strings are the Qwen3 tool call syntax,
taken verbatim from what vLLM's builder emits for this parser rather than
hand-written from documentation. `test_tool_name_guard.py` re-derives them from vLLM
when it is importable and asserts byte equality, so a serving-side change to the
syntax fails a test here instead of silently producing a tag that never triggers --
which would look exactly like the guard working. END is the exception: a deliberate
correction of vLLM's, documented at its definition, and the same test pins vLLM's
value too, so an upstream fix would fail loudly and tell us to drop the deviation.
"""

from __future__ import annotations

import json
import os

# TRIGGER and BEGIN are verbatim from vllm.tool_parsers.structural_tag_registry's
# qwen_3_coder builder, 0.27.1 and 0.26.0. A tag only constrains generation once its
# TRIGGER appears, so a trigger that does not match what the model emits is a guard
# that never fires and cannot be told apart from a guard with nothing to catch.
TRIGGER = "<tool_call>\n<function="
BEGIN_TEMPLATE = "<tool_call>\n<function={name}>\n"

# END IS A DELIBERATE, DOCUMENTED DEVIATION FROM vLLM -- the one string here that is not
# verbatim, and the check that pins the other two pins this relationship instead.
#
# vLLM's builder emits VLLM_END, which BEGINS with "\n" -- the same newline BEGIN_TEMPLATE
# already ends with. A zero-argument call is written `<function=NAME>\n</function>`: ONE
# newline, which BEGIN consumes, so VLLM_END can never match there. The tag never closes,
# its `any_text` body swallows the next call in the response unconstrained, and the tool
# parser -- which splits on delimiters -- still extracts both. vLLM does this even for tools
# it is told take no arguments (`properties: {}`), which is exactly the case that breaks.
#
# Measured, not argued (2026-09-22). The schema eval's g14 emitted a valid zero-argument
# `get_action_choices` and then `(http://127.0.0.1:5000/api/v2/popular)` as a tool name; the
# exact tag ACCEPTED the exact 49-token completion, and the invented call killed the pilot.
# Over all ~28,700 traced completions from both eval arms, this END versus VLLM_END changes
# exactly 97 verdicts and every one is a correct rejection: 94 bound `choose_action` choices
# OUTSIDE their enum that the swallowing had let through, 2 invented tool names, and 1
# hallucinated extra argument the engine silently ignored. No genuine completion is newly
# rejected -- including bound `choose_action` calls, whose schema content supplies the newline.
#
# So the bug did not only leak invented names: it BYPASSED the bound choice constraint on the
# second call of any response whose first call took no arguments.
VLLM_END = "\n</function>\n</tool_call>"
END = "</function>\n</tool_call>"

_MODES = ("off", "structural_tag")


def tool_name_guard_mode() -> str:
    """`off` (default) or `structural_tag`.

    Default OFF because this changes what the serving engine is allowed to
    generate, and the run that adopts it is a pre-registered A/B against a
    baseline. Refuses an unknown value rather than falling back: a typo that
    silently ran the unguarded arm would be indistinguishable from the control.
    """
    mode = os.environ.get("MAGEBENCH_TOOL_NAME_GUARD")
    if mode is None:
        return "off"
    if mode not in _MODES:
        raise ValueError(
            f"MAGEBENCH_TOOL_NAME_GUARD={mode!r} is not one of {_MODES}. "
            f"`structural_tag` constrains the emitted function name to the tools "
            f"this seat was offered; `off` is the unguarded baseline."
        )
    return mode


def tool_name_structural_tag(tool_names: list[str]) -> dict:
    """The tag that permits exactly these names inside a tool call.

    Order follows the toolset, so the tag is a deterministic function of what the
    seat was offered -- two seats with the same tools produce byte-identical tags,
    which is what makes the request comparable across a paired run.
    """
    if not tool_names:
        raise ValueError(
            "tool_name_structural_tag called with no tools. An empty tag would "
            "permit NOTHING inside a tool call, which is a silent way to make "
            "every decision impossible."
        )
    return {
        "type": "structural_tag",
        "format": {
            "type": "triggered_tags",
            "triggers": [TRIGGER],
            "tags": [
                {
                    "type": "tag",
                    "begin": BEGIN_TEMPLATE.format(name=name),
                    # any_text, NOT the tool's JSON schema: the arguments are
                    # deliberately left free. See the module docstring.
                    "content": {"type": "any_text"},
                    "end": END,
                }
                for name in tool_names
            ],
        },
    }


def structured_outputs_field(tool_names: list[str]) -> dict:
    """The exact `extra_body` fragment the request carries when the guard is on.

    One field, `structured_outputs.structural_tag`, a JSON string. It lands in
    `llm_trace`'s recorded request, so whether the guard was in force is readable
    per CALL from a finished corpus rather than inferred from a launcher.
    """
    return {"structural_tag": json.dumps(tool_name_structural_tag(tool_names))}
