"""The bound-choice grammar, proven by COMPILING it, not by reading the spec.

The name guard set this discipline and it is the only thing that distinguishes a constraint
that works from a tag the server accepts and never enforces. A test that asserted on the tag
DICT would pass against a tag no engine honours -- which is exactly the failure mode the
tag-order probe ran into, where four arms including an unconstrained control returned
byte-identical output in 396 of 396 cells and nothing in the artifact could say whether the
tag had been applied at all.

So the assertions here are about what a compiled grammar ACCEPTS and REJECTS, against the
real 4B tokenizer. The xgrammar/tokenizer cases skip when those are unavailable (the branch
worktrees have no venv), and the shape tests always run.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from magebench.pilot.decision_schema import (
    CHOICE_FIELD,
    CHOICE_TOOL,
    bound_tool_names,
    choice_content_schema,
    decision_schema_mode,
    decision_structural_tag,
)

SNAPSHOTS = pathlib.Path(
    "/workspace1/projects/posttrainlatamgpt/hub/models--Qwen--Qwen3.5-4B/snapshots"
)
TOOLS = ["choose_action", "get_game_state", "pass_priority"]
IDS = ["a1", "a2", "a3"]


def _qwen_call(tool: str, field: str | None = None, value: str | None = None) -> str:
    """A Qwen3 tool call in the syntax the guard's trigger and begin/end describe."""
    inner = f"<parameter={field}>\n{value}\n</parameter>\n" if field else "anything at all\n"
    return f"<tool_call>\n<function={tool}>\n{inner}</function>\n</tool_call>"


# --------------------------------------------------------------------------- shape


def test_only_choose_action_is_bound():
    tag = decision_structural_tag(TOOLS, IDS)
    assert bound_tool_names(tag) == [CHOICE_TOOL]
    for entry in tag["format"]["tags"]:
        if entry["begin"].endswith(f"<function={CHOICE_TOOL}>\n"):
            assert entry["content"]["type"] == "json_schema"
            assert entry["content"]["style"] == "qwen_xml"
        else:
            assert entry["content"]["type"] == "any_text"


def test_without_ids_it_degrades_to_the_name_guard():
    """`choice_ids=None` must not bind an invented list, and must not permit nothing."""
    tag = decision_structural_tag(TOOLS, None)
    assert bound_tool_names(tag) == []
    assert all(e["content"] == {"type": "any_text"} for e in tag["format"]["tags"])


def test_an_empty_enum_refuses_rather_than_permitting_nothing():
    """The mute failure reached by construction instead of by the model. See D6."""
    with pytest.raises(ValueError, match="empty enum permits NOTHING"):
        choice_content_schema([])


def test_duplicate_ids_refuse():
    with pytest.raises(ValueError, match="duplicates"):
        choice_content_schema(["a1", "a1"])


def test_no_tools_refuses():
    with pytest.raises(ValueError, match="permits NOTHING"):
        decision_structural_tag([], IDS)


def test_mode_refuses_an_unknown_value():
    """A typo must not silently run the baseline -- that is indistinguishable from control."""
    import os

    os.environ["MAGEBENCH_DECISION_SCHEMA"] = "bound-choice"
    try:
        with pytest.raises(ValueError, match="MAGEBENCH_DECISION_SCHEMA"):
            decision_schema_mode()
    finally:
        del os.environ["MAGEBENCH_DECISION_SCHEMA"]


# ------------------------------------------------------- the compiled-grammar proof


@pytest.fixture(scope="module")
def compiled():
    xgr = pytest.importorskip("xgrammar")
    transformers = pytest.importorskip("transformers")
    snaps = sorted(SNAPSHOTS.glob("*")) if SNAPSHOTS.is_dir() else []
    if not snaps:
        pytest.skip(f"no 4B tokenizer under {SNAPSHOTS}")
    tok = transformers.AutoTokenizer.from_pretrained(str(snaps[0]))
    compiler = xgr.GrammarCompiler(xgr.TokenizerInfo.from_huggingface(tok))
    tag = decision_structural_tag(TOOLS, IDS)
    # The modern single-argument form, matching what vLLM 0.27.1 itself calls for a tag
    # without a "structures" key (backend_xgrammar.py:110). Getting this wrong is how a
    # proof silently tests a different object than the server compiles.
    return xgr, compiler.compile_structural_tag(json.dumps(tag))


def _accepts(xgr, grammar, text: str) -> bool:
    return xgr.GrammarMatcher(grammar).accept_string(text)


@pytest.mark.parametrize(
    "label,text,expected",
    [
        ("an enumerated id is accepted", _qwen_call(CHOICE_TOOL, CHOICE_FIELD, "a1"), True),
        ("a second enumerated id too", _qwen_call(CHOICE_TOOL, CHOICE_FIELD, "a3"), True),
        # THE ASSERTION THE WHOLE MECHANISM EXISTS FOR.
        ("an id outside the enum is REJECTED", _qwen_call(CHOICE_TOOL, CHOICE_FIELD, "a9"), False),
        ("so is a free-form answer", _qwen_call(CHOICE_TOOL, CHOICE_FIELD, "attack with all"), False),
        # The name guard's properties must survive: these are the controls for "only the
        # choice argument". A constraint that also reshaped other tools or suppressed prose
        # would fail them, and would look like this one working.
        ("an unbound tool keeps free arguments", _qwen_call("get_game_state"), True),
        ("an invented tool name is still rejected", _qwen_call("ghost_pass"), False),
        ("prose with no tool call is untouched", "I think I should attack here.", True),
    ],
)
def test_compiled_grammar_accepts_and_rejects(compiled, label, text, expected):
    xgr, grammar = compiled
    assert _accepts(xgr, grammar, text) is expected, label


def test_the_matcher_can_say_yes_and_no(compiled):
    """A positive control on the test method itself.

    If `accept_string` returned False for everything -- a mis-compiled grammar, a wrong
    matcher API -- every rejection assertion above would pass for the wrong reason. This
    fails unless the same matcher produces both answers.
    """
    xgr, grammar = compiled
    yes = _accepts(xgr, grammar, _qwen_call(CHOICE_TOOL, CHOICE_FIELD, "a1"))
    no = _accepts(xgr, grammar, _qwen_call(CHOICE_TOOL, CHOICE_FIELD, "a9"))
    assert (yes, no) == (True, False)


# ------------------------------------------------- which decisions can be bound at all
# Shapes below are taken verbatim from `respond_with` strings observed in 4452/4056, not
# invented: keying the rule on the engine's own declaration is only safe if the declarations
# it was built against are the real ones.

from magebench.pilot.decision_schema import bindable_enum  # noqa: E402


def _result(respond_with, ids, **extra):
    choices = [{"id": i} if i is not None else {"index": 0} for i in ids]
    return {"respond_with": respond_with, "choices": choices, **extra}


def test_ids_plus_the_declared_escape_when_one_is_offered():
    """`no` is REQUIRED, not optional: 744 of 1,331 bound decisions in the corpus offer it,
    and an enum of ids alone would make declining those impossible -- a legal pass turned
    into silence, which becomes a nudge."""
    enum, reason = bindable_enum(_result("choice=pN to play, or choice=no to pass", ["p1", "p2"]))
    assert reason == ""
    assert enum == ["p1", "p2", "no"]


def test_ids_only_when_the_decision_is_required():
    """The 397-case shape: a target that must be picked offers no escape, so the enum has none."""
    enum, reason = bindable_enum(
        _result("choice=pN — must pick a target", ["p3", "p4"], can_cancel=False, required=True)
    )
    assert reason == ""
    assert enum == ["p3", "p4"]


def test_yes_is_carried_too_when_declared():
    enum, _ = bindable_enum(_result("choice=pN, or choice=yes to confirm", ["p1"]))
    assert enum == ["p1", "yes"]


@pytest.mark.parametrize(
    "respond_with,ids,expect_in_reason",
    [
        # COMBAT MUST NOT BE BOUND. 302 attackers/blockers failures would have become
        # unrepresentable under a global choice enum.
        ("attackers=p1,p2,... or choice=yes (confirm) or choice=no (skip)", ["p1"], "attackers"),
        ("blockers=p5:p1,p6:p2 (blocker:attacker) or choice=yes", ["p5"], "blockers"),
        ("amount=N (min=0, max=2147483647)", ["p1"], "amount"),
        ("choice=0, choice=1, etc. or text=Name (not yes/no)", ["p1"], "text"),
        # index-shaped: choices carry no id
        ("choice=0, choice=1, etc. (not yes/no)", [None, None], "carries no id"),
        # nothing declared
        (None, ["p1"], "no respond_with"),
        ("", ["p1"], "no respond_with"),
    ],
)
def test_these_decisions_are_left_unbound_with_a_reason(respond_with, ids, expect_in_reason):
    enum, reason = bindable_enum(_result(respond_with, ids))
    assert enum is None
    assert expect_in_reason in reason


def test_an_empty_choices_list_is_unbound_not_an_empty_enum():
    enum, reason = bindable_enum(_result("choice=pN to play, or choice=no to pass", []))
    assert enum is None and "empty" in reason


def test_an_unrecognised_declaration_falls_to_unbound():
    """Fail toward today's behaviour: no new constraint, rather than one nobody checked."""
    enum, reason = bindable_enum(_result("pick something reasonable", ["p1"]))
    assert enum is None and "declares no choice=id form" in reason
