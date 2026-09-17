"""Make an invalid decision unrepresentable: `choose_action` bound to the enumerated ids.

WHAT THIS ADDS TO `tool_name_guard`. That module constrains the tool NAME and leaves
arguments free, deliberately, because a hallucinated argument was survivable where a
hallucinated name was fatal. This module goes one step further for ONE tool: when the
harness has already enumerated the legal choices, `choose_action`'s `choice` argument is
constrained to exactly those ids. Every other tool keeps `any_text`.

THE POPULATION THIS FITS, measured before it was built. The 4B's 18 invented tool names
across 4452 + both 4056 arms are **all 18 in the first tool call** -- the slot a triggered
structural tag governs. That is the structural argument for this mechanism, and it is worth
contrasting with the one that failed: `get_game_log`'s elevation could never have been
explained by the tag, because 42 of its 71 occurrences are in the SECOND slot, which the tag
does not reach. A constraint is only as good as its overlap with where the defect lives.

WHY AN ENUM AND NOT A FREE STRING. This is the `PlanAction` move: the model does not choose
whether its answer is in range, it chooses among the things in range. An id outside the list
cannot be generated, so the "invalid decision" branch stops being a runtime error and becomes
an unrepresentable state.

WHAT IT DOES NOT DO, so nobody reads more into it:

  * It binds SLOT ONE. D10's caveat stands: a second tool call in the same response is
    outside the triggered region, so multi-call responses are reported separately rather
    than counted as guarded.
  * It cannot make a decision correct, only in-range. Choosing a legal-but-terrible action
    is untouched, and the win-rate readout is where that shows up.
  * It says nothing about the OTHER tools' arguments, which stay `any_text` for the reason
    the name guard gives.

THE RISK TO REGISTER, from D6. A constraint the model cannot satisfy produces silence, and
silence in this harness produces a nudge ("Respond with a tool call."). So the mute-mode
readout is not an inference from the win rate: it is the guarded arm's nudge rate and its
`(none)` share against the unguarded arm on the same seeds. Baselines measured on the
unguarded 4B: nudge 2.27-2.38% of calls, and of nudge frames 8.3% emit nothing at all.

FORMAT PROVENANCE. `style="qwen_xml"` is not a guess: vLLM's own `qwen_3_5` /
`qwen_3_coder` builder passes exactly that for tool parameters
(`xgrammar/builtin_structural_tag.py:1011`), because the Qwen tool-call syntax carries
parameters as XML rather than JSON. The trigger and begin/end strings are imported from
`tool_name_guard` rather than restated, so there is one place for them to be wrong.
"""

from __future__ import annotations

import os

from magebench.pilot.tool_name_guard import BEGIN_TEMPLATE, END, TRIGGER

# The tool whose arguments this constrains, and the argument it constrains.
CHOICE_TOOL = "choose_action"
CHOICE_FIELD = "choice"

_MODES = ("off", "bound_choice")


def decision_schema_mode() -> str:
    """`off` (default) or `bound_choice`. Refuses anything else.

    Default off for the reason the name guard is default off: this changes what the serving
    engine may generate, and the run that adopts it is a pre-registered A/B. A typo that
    silently ran the baseline would be indistinguishable from the control arm.
    """
    mode = os.environ.get("MAGEBENCH_DECISION_SCHEMA")
    if mode is None:
        return "off"
    if mode not in _MODES:
        raise ValueError(
            f"MAGEBENCH_DECISION_SCHEMA={mode!r} is not one of {_MODES}. "
            f"`bound_choice` constrains {CHOICE_TOOL}'s {CHOICE_FIELD!r} to the ids the "
            f"harness enumerated; `off` is the unbound baseline."
        )
    return mode


def choice_content_schema(choice_ids: list[str]) -> dict:
    """The tag content that permits exactly `{"choice": <one of these ids>}`.

    `additionalProperties: false` is part of the point: an extra argument is how a model
    smuggles a free-form answer past an enum.
    """
    if not choice_ids:
        raise ValueError(
            "choice_content_schema called with no ids. An empty enum permits NOTHING, which "
            "would make every decision impossible to express -- the mute failure, arrived at "
            "by construction rather than by the model's inability. A decision with no "
            "enumerated choices must leave this tool unconstrained instead."
        )
    if len(set(choice_ids)) != len(choice_ids):
        raise ValueError(
            f"choice ids contain duplicates: {choice_ids!r}. Two identical ids make the "
            "model's answer ambiguous to the caller that has to resolve it."
        )
    return {
        "type": "json_schema",
        # qwen_xml, not json: the Qwen tool-call syntax carries parameters as XML. See the
        # module docstring's format provenance.
        "style": "qwen_xml",
        "json_schema": {
            "type": "object",
            "properties": {CHOICE_FIELD: {"type": "string", "enum": list(choice_ids)}},
            "required": [CHOICE_FIELD],
            "additionalProperties": False,
        },
    }


def decision_structural_tag(tool_names: list[str], choice_ids: list[str] | None) -> dict:
    """The name guard's tag, with `choose_action`'s content bound when ids are known.

    `choice_ids=None` means the harness did not enumerate choices for this decision, and
    then this returns a tag identical to the name guard's -- every content `any_text`. That
    is the honest degradation: binding an enum we do not have would either permit nothing or
    require inventing the list.
    """
    if not tool_names:
        raise ValueError(
            "decision_structural_tag called with no tools. An empty tag permits NOTHING "
            "inside a tool call, which is a silent way to make every decision impossible."
        )
    bind = choice_ids is not None and CHOICE_TOOL in tool_names
    tags = []
    for name in tool_names:
        if bind and name == CHOICE_TOOL:
            content = choice_content_schema(list(choice_ids))
        else:
            content = {"type": "any_text"}
        tags.append(
            {
                "type": "tag",
                "begin": BEGIN_TEMPLATE.format(name=name),
                "content": content,
                "end": END,
            }
        )
    return {
        "type": "structural_tag",
        "format": {"type": "triggered_tags", "triggers": [TRIGGER], "tags": tags},
    }


def bound_tool_names(tag: dict) -> list[str]:
    """Which tools in this tag have a bound (non-`any_text`) content.

    For the artifact: a run must be able to say what its own tag constrained, rather than
    leaving a reader to infer it from an environment variable that may not have been read.
    """
    out = []
    # INDEXED, not `.get(... ) or {}`: this reads a tag THIS module built, so a missing key is
    # a bug in the builder and must raise here rather than read as "nothing was bound" -- which
    # is the healthy answer and would hide the defect.
    for entry in tag["format"]["tags"]:
        if entry["content"]["type"] != "any_text":
            begin = entry["begin"]
            name = begin[len("<tool_call>\n<function=") :].rstrip(">\n")
            out.append(name)
    return out


# ---------------------------------------------------------------------------------------------
# WHICH DECISIONS CAN BE BOUND AT ALL. Measured before this was written, over 72,023
# `choose_action` calls across 4452 and both 4056 arms: 2,699 failed (3.75%), and a
# choice-only enum applied to EVERY decision would have prevented 1,854 of them while making
# 663 UNREPRESENTABLE -- 325 index-shaped, 302 attackers/blockers, 36 amount. The 302 is
# combat: every declare-attackers and declare-blockers would become ungeneratable, the model
# would fall silent, and silence in this harness becomes a nudge. That is D6's mute mode
# created BY the fix, on the most consequential decisions in the game.
#
# So binding is per decision, and the engine is asked rather than inferred. `respond_with` is
# the engine's own sentence stating the valid answer forms -- "choice=pN to play, or choice=no
# to pass", "attackers=p1,p2,... or choice=yes (confirm)". `can_cancel` is NOT usable for
# this: it is None on 538 of the 1,100+ results sampled, so a rule keyed on it would silently
# treat "unknown" as "not cancellable" and drop the declared escape value.
#
# UNRECOGNISED FORMS ARE LEFT UNBOUND, with the reason recorded. That is today's behaviour, so
# the failure direction is "no new constraint" rather than "a constraint nobody checked".
# ---------------------------------------------------------------------------------------------

# Keys other than `choice` that an answer may use. Any of these in `respond_with` means the
# decision is not a single-choice decision and must not be bound.
_OTHER_ANSWER_KEYS = ("attackers=", "blockers=", "amount=", "amounts=", "text=", "index=",
                      "mana_plan=", "until=")

# Literal `choice=` values that are words rather than ids -- the declared escapes. These MUST
# be in the enum when the engine offers them: an enum of ids alone makes declining impossible,
# which turns a legal pass into silence.
_CHOICE_LITERAL_RE = __import__("re").compile(r"choice=(no|yes)\b")
_CHOICE_ID_FORM_RE = __import__("re").compile(r"choice=(pN|\d)")


def bindable_enum(choices_result: dict) -> tuple[list[str] | None, str]:
    """The enum for this decision, or None with the reason it cannot be bound.

    Returns `(enum, "")` when bindable and `(None, reason)` otherwise. The reason goes in the
    artifact per decision: a null result is unreadable without knowing what fraction of
    decisions the mechanism could apply to at all.
    """
    respond_with = choices_result.get("respond_with")
    if not isinstance(respond_with, str) or not respond_with.strip():
        return None, "no respond_with declaration"
    for key in _OTHER_ANSWER_KEYS:
        if key in respond_with:
            return None, f"not single-choice: respond_with offers {key.rstrip('=')}"
    if not _CHOICE_ID_FORM_RE.search(respond_with):
        return None, f"respond_with declares no choice=id form: {respond_with[:40]!r}"

    raw = choices_result.get("choices")
    if not isinstance(raw, list) or not raw:
        return None, "choices list is empty"
    ids = [c.get("id") if isinstance(c, dict) else None for c in raw]
    if any(i is None for i in ids):
        # The index-shaped decisions ("choice=0, choice=1") have no ids. A follow-up may bind
        # those to 0..len-1; today they stay unbound rather than being bound to a guess.
        return None, "at least one choice carries no id"
    if any(not isinstance(i, str) or not i for i in ids):
        return None, "a choice id is not a non-empty string"

    literals = sorted(set(_CHOICE_LITERAL_RE.findall(respond_with)))
    enum = list(dict.fromkeys(list(ids) + literals))
    if len(enum) != len(set(enum)):
        return None, "an id collides with a declared escape literal"
    return enum, ""


def decision_coverage(choices_result: dict) -> dict:
    """The per-decision artifact row: bound or not, and why not."""
    enum, reason = bindable_enum(choices_result)
    return {
        "bound": enum is not None,
        "reason": reason,
        "n_enum": len(enum) if enum else 0,
        "action_type": choices_result.get("action_type"),
        "response_type": choices_result.get("response_type"),
    }
