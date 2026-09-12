"""Controls for the tool-name guard, in both directions.

The claim is "an invented tool name is impossible", and the failure mode of a
constraint like this is subtle: a tag whose TRIGGER never matches what the model
emits is inert, and an inert guard looks exactly like a guard with nothing to
catch. So the tests here check three separate things -- the tag names the right
tools, its syntax is the syntax the serving engine expects, and the request
carries it only when asked.

The grammar-level control (does the compiled grammar actually reject
`ghost_pass`?) cannot run here: it needs xgrammar and the model's tokenizer,
neither of which is in the harness venv. It was run under slurm against the real
4B tokenizer and is recorded in the commit message -- 6 of 6 real names accepted,
5 of 5 observed inventions rejected, prose accepted, free-form arguments accepted.
"""

import json

import pytest

from magebench.pilot.tool_name_guard import (
    BEGIN_TEMPLATE,
    END,
    TRIGGER,
    structured_outputs_field,
    tool_name_guard_mode,
    tool_name_structural_tag,
)

NAMES = ["choose_action", "pass_priority", "get_game_state",
         "get_game_log", "get_oracle_text", "get_action_choices"]
INVENTED = ["choose", "choice", "action", "ghost_pass", "protect_priority"]


def test_the_tag_permits_exactly_the_offered_tools():
    tag = tool_name_structural_tag(NAMES)
    begins = [t["begin"] for t in tag["format"]["tags"]]
    assert begins == [BEGIN_TEMPLATE.format(name=n) for n in NAMES]
    blob = json.dumps(tag)
    for bad in INVENTED:
        # `choose` is a prefix of `choose_action`, so the check has to be about the
        # begin strings, not about the substring appearing anywhere in the tag.
        assert f"<function={bad}>" not in blob, bad


def test_only_the_name_is_constrained():
    """Arguments are any_text. If this ever becomes a json_schema, the change has
    grown a second variable and the A/B stops measuring one thing."""
    tag = tool_name_structural_tag(NAMES)
    for t in tag["format"]["tags"]:
        assert t["content"] == {"type": "any_text"}


def test_the_tag_is_deterministic_for_the_same_toolset():
    assert tool_name_structural_tag(NAMES) == tool_name_structural_tag(list(NAMES))


def test_an_empty_toolset_refuses_rather_than_permitting_nothing():
    with pytest.raises(ValueError, match="no tools"):
        tool_name_structural_tag([])


def test_the_mode_refuses_a_typo_instead_of_falling_back():
    """A typo that silently ran the unguarded arm would be indistinguishable from
    the control arm, which is the one confusion this A/B cannot survive."""
    import os
    old = os.environ.get("MAGEBENCH_TOOL_NAME_GUARD")
    try:
        os.environ["MAGEBENCH_TOOL_NAME_GUARD"] = "structural-tag"
        with pytest.raises(ValueError, match="MAGEBENCH_TOOL_NAME_GUARD"):
            tool_name_guard_mode()
        os.environ.pop("MAGEBENCH_TOOL_NAME_GUARD")
        assert tool_name_guard_mode() == "off"
        os.environ["MAGEBENCH_TOOL_NAME_GUARD"] = "structural_tag"
        assert tool_name_guard_mode() == "structural_tag"
    finally:
        os.environ.pop("MAGEBENCH_TOOL_NAME_GUARD", None)
        if old is not None:
            os.environ["MAGEBENCH_TOOL_NAME_GUARD"] = old


def test_the_request_fragment_is_one_field_and_json():
    field = structured_outputs_field(NAMES)
    assert list(field) == ["structural_tag"]
    assert json.loads(field["structural_tag"])["format"]["triggers"] == [TRIGGER]


def test_the_syntax_matches_what_vllm_builds_for_this_parser():
    """The trigger and begin/end strings are the serving engine's, not ours.

    Skipped where vLLM is absent -- it is not a harness dependency -- but it runs
    on any box that serves, which is where a syntax drift would matter. Without
    this the guard could go inert after a serving upgrade and look like success.
    """
    vllm_protocol = pytest.importorskip("vllm.entrypoints.openai.chat_completion.protocol")
    registry = pytest.importorskip("vllm.tool_parsers.structural_tag_registry")

    tools = [{"type": "function",
              "function": {"name": n, "description": n, "strict": True,
                           "parameters": {"type": "object", "properties": {},
                                          "required": [], "additionalProperties": False}}}
             for n in NAMES]
    request = vllm_protocol.ChatCompletionRequest(
        model="m", messages=[{"role": "user", "content": "x"}],
        tools=tools, tool_choice="auto")
    built = registry.get_model_structural_tag(
        model="qwen_3_coder", tools=request.tools, tool_choice="auto", reasoning=False)
    assert built is not None, (
        "vLLM built no structural tag for strict tools -- the short-circuit this "
        "guard works around has changed shape")
    fmt = built.model_dump()["format"]
    assert fmt["triggers"] == [TRIGGER]
    assert [t["begin"] for t in fmt["tags"]] == [BEGIN_TEMPLATE.format(name=n) for n in NAMES]
    assert {t["end"] for t in fmt["tags"]} == {END}


def test_vllm_builds_nothing_without_strict_which_is_why_this_module_exists():
    """The defect itself, pinned: tool_choice=auto + non-strict tools = no tag."""
    vllm_protocol = pytest.importorskip("vllm.entrypoints.openai.chat_completion.protocol")
    registry = pytest.importorskip("vllm.tool_parsers.structural_tag_registry")
    tools = [{"type": "function", "function": {"name": n, "description": n,
              "parameters": {"type": "object", "properties": {}}}} for n in NAMES]
    request = vllm_protocol.ChatCompletionRequest(
        model="m", messages=[{"role": "user", "content": "x"}],
        tools=tools, tool_choice="auto")
    assert registry.get_model_structural_tag(
        model="qwen_3_coder", tools=request.tools,
        tool_choice="auto", reasoning=False) is None
