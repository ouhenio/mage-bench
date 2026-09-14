"""A tool call cut off at the completion cap gets one redraw, not a dead game.

THE SHAPE, measured across 162,029 calls in four corpora. When the completion cap
lands while a tool call is already open, the parser finds a call and reports
`finish_reason: "tool_calls"` -- NOT "length". So the existing truncation handler
never sees it, and what reaches the bridge is a half-written name with empty
arguments:

    name='choose'  args='{}'  completion_tokens=1024  max_tokens=1024

`choose` is a prefix of `choose_action`. It is not an invention; it is a real name
the model was still writing. The bridge answers "Unknown tool: choose" and the
pilot treats that as fatal, so a budget we chose ends the game.

    4B ckpt-132   14,571 calls   19 cap-hit-with-call    2 out of schema, both prefixes
    4B ckpt-265   14,669 calls   12                      0
    35B 3205      53,971 calls   47                      3, all prefixes
    35B 3143      78,818 calls   53                      1, a prefix

On the 35B those four are its ONLY out-of-schema names besides one syntax case:
zero genuine inventions in 132,789 calls. The teacher's fatal-tool deaths were a
budget, not a grammar problem.

WHY A RETRY RATHER THAN A BIGGER RESERVE. Raising MAX_TOKENS makes the event
rarer and charges every call in every game to do it, while leaving it fatal when
it does happen. The retry removes the death for the price of one round trip, and
it is diagnosable exactly when it occurs. The two are different questions and this
answers the one that ends games.

WHAT IT DOES NOT DO, stated because the temptation to claim it is strong: it does
not make a truncated call correct, and it does not help a genuine invention. A
structural-tag guard cannot help here either -- a grammar constrains which tokens
may be sampled, not when generation stops -- which is why these two fixes do not
overlap and both exist.

AND IT DEPENDS ON SAMPLING. A redraw of an identical prompt under greedy decoding
is the identical draw, so at temperature 0 this can only ever burn a round trip.
The row records `deterministic_decoding` so a reader can tell a retry that could
not have differed from one that could.
"""

from __future__ import annotations

from logging import Logger
from typing import Protocol


class _FunctionLike(Protocol):
    name: str
    arguments: str | None


def _is_empty_arguments(arguments: object) -> bool:
    """`{}`, empty, or unparseable -- the shapes a cut-off argument list leaves."""
    if arguments is None or arguments == "" or arguments == {}:
        return True
    if isinstance(arguments, str):
        stripped = arguments.strip()
        if stripped in ("", "{}"):
            return True
        import json

        try:
            return json.loads(stripped) == {}
        except (ValueError, TypeError):
            # Unparseable means the generator stopped inside the argument object,
            # which is the same event as an empty one for this purpose.
            return True
    return False


# THE VERSION OF THE PREDICATE ITSELF, recorded in every detail row and printed by
# every census. A vendored copy is the right way to run a census without a built
# tree -- but a vendored copy drifts silently, and "which version decided this
# number" has to be answerable from the number rather than from a memory of when
# somebody copied the file. Bump this whenever the three terms or the detail keys
# change; never for a comment.
PREDICATE_VERSION = "cap-hit/2"


def classify(
    *,
    finish_reason: object,
    completion_tokens: object,
    max_tokens: int,
    n_tool_calls: int,
    name: str | None,
    arguments: object,
    offered: set[str],
) -> tuple[bool, dict]:
    """THE PREDICATE, in one place, for both the live path and every census.

    The live pilot holds SDK objects and a census holds JSON rows, so the two
    wrappers below extract the same six values and delegate here. A criterion
    agreed in prose and implemented twice is two criteria -- and the whole point of
    this population is that two counts of it must be the same object.

    Three terms define the POPULATION, all required:
        finish_reason == "tool_calls"     the parser found a call, so this is NOT
                                          the finish_reason "length" case
        completion_tokens == max_tokens   the generation used every token it had
        name not offered OR args empty    something about the call is unfinished

    A FOURTH term gates the RETRY and not the population: the response must carry
    exactly ONE tool call. Run A found the reason -- 5 responses of 14,442 are
    degenerate repeat loops, 68 x `pass_priority` with empty args filling the whole
    budget, and the game SURVIVES them (g175 seq 120/122/124; g168 seq 20 ends in a
    stump `pass` and finishes normally). A trailing stump after 65 identical calls
    is not a cut-off decision, it is the tail of a loop, and redrawing a loop is a
    round trip into the same loop.

    The split is deliberate: `matched` stays the three-term population so every
    count reconciled against cap-hit/1 is still the same object, and
    `detail["retry_eligible"]` carries the fourth term. A multi-call response is
    COUNTED and never retried.
    """
    detail: dict = {
        "predicate_version": PREDICATE_VERSION,
        "finish_reason": finish_reason,
        "completion_tokens": completion_tokens,
        "max_tokens": max_tokens,
    }
    detail["n_tool_calls"] = n_tool_calls
    if finish_reason != "tool_calls" or n_tool_calls < 1:
        return False, detail
    if not isinstance(completion_tokens, int) or completion_tokens < max_tokens:
        return False, detail
    # None and "" are DIFFERENT facts and the row keeps them apart: no name at all
    # is a malformed response, an empty string is a call whose name was cut before
    # its first character. Collapsing them with `or ""` is the absent-vs-empty
    # conflation, and the lint was right to refuse it.
    detail["tool"] = name
    detail["tool_name_missing"] = name is None
    detail["arguments_empty"] = _is_empty_arguments(arguments)
    detail["name_offered"] = name in offered if name else False
    # A PREFIX OF A REAL NAME is the signature of a name still being written, and
    # it is worth recording separately from a name that is simply absent: the first
    # is truncation, the second could be a genuine invention that also hit the cap.
    detail["name_is_prefix_of_offered"] = (
        any(real.startswith(name) and real != name for real in offered)
        if name
        else False
    )

    matched = (not detail["name_offered"]) or detail["arguments_empty"]
    # The fourth term. A stump at the end of 65 identical calls is the tail of a
    # loop, not a decision that ran out of room; retrying it buys another loop.
    detail["retry_eligible"] = matched and n_tool_calls == 1
    return matched, detail


def cap_hit_with_call_open(
    choice: object,
    response: object,
    *,
    max_tokens: int,
    offered: set[str],
) -> tuple[bool, dict]:
    """The live path: SDK objects from the chat completion."""
    message = getattr(choice, "message", None)
    tool_calls = getattr(message, "tool_calls", None) if message is not None else None
    usage = getattr(response, "usage", None)
    fn = getattr(tool_calls[0], "function", None) if tool_calls else None
    return classify(
        finish_reason=getattr(choice, "finish_reason", None),
        completion_tokens=getattr(usage, "completion_tokens", None) if usage else None,
        max_tokens=max_tokens,
        n_tool_calls=len(tool_calls) if tool_calls else 0,
        name=getattr(fn, "name", None) if fn is not None else None,
        arguments=getattr(fn, "arguments", None) if fn is not None else None,
        offered=offered,
    )


def cap_hit_from_trace_row(row: dict) -> tuple[bool, dict]:
    """A census: one `llm_call` row from a `*_llm_trace.jsonl`.

    Takes `max_tokens` and the offered toolset from the ROW'S OWN REQUEST rather
    than from a constant, because a corpus can contain more than one value of
    either and a census keyed on today's constant would silently misclassify
    yesterday's games.
    """
    request = row.get("request") if isinstance(row, dict) else None
    response = row.get("response") if isinstance(row, dict) else None
    if not isinstance(request, dict) or not isinstance(response, dict):
        return False, {}
    choices = response.get("choices")
    choice = choices[0] if isinstance(choices, list) and choices else None
    if not isinstance(choice, dict):
        return False, {}
    message = choice.get("message")
    tool_calls = message.get("tool_calls") if isinstance(message, dict) else None
    first = tool_calls[0] if isinstance(tool_calls, list) and tool_calls else None
    fn = first.get("function") if isinstance(first, dict) else None
    usage = response.get("usage")
    max_tokens = request.get("max_tokens")
    if not isinstance(max_tokens, int):
        # No cap in the record means the condition is undefined for this row, not
        # absent from it. Refusing to guess keeps a census from reporting zero on
        # rows it could not judge.
        return False, {"undecidable": "request carries no max_tokens"}
    tools = request.get("tools")
    offered = {
        t["function"]["name"]
        for t in (tools if isinstance(tools, list) else ())
        if isinstance(t, dict) and isinstance(t.get("function"), dict)
    }
    return classify(
        finish_reason=choice.get("finish_reason"),
        completion_tokens=usage.get("completion_tokens") if isinstance(usage, dict) else None,
        max_tokens=max_tokens,
        n_tool_calls=len(tool_calls) if isinstance(tool_calls, list) else 0,
        name=fn.get("name") if isinstance(fn, dict) else None,
        arguments=fn.get("arguments") if isinstance(fn, dict) else None,
        offered=offered,
    )


def should_retry(state, detail: dict, *, logger: Logger, temperature: float | None) -> bool:
    """One redraw per decision, then the existing fatal path. Never a loop.

    Keyed on the DECISION rather than on the call, because a decision can take
    several calls and a per-call budget would let a pathological decision retry
    without bound -- which is the failure the cap itself exists to stop.
    """
    seq = state.last_decision_seq
    if state.cap_retry_decision_seq == seq and state.cap_retry_used:
        logger.warning(
            "[pilot] cap-hit with a call open AGAIN on decision %s (tool %r): the redraw "
            "also hit the cap, so the existing fatal path takes it from here.",
            seq, detail.get("tool"),
        )
        return False
    state.cap_retry_decision_seq = seq
    state.cap_retry_used = True
    if temperature == 0:
        logger.warning(
            "[pilot] cap-hit retry at temperature 0: the redraw is the same draw, so "
            "this can only cost a round trip. Recorded as deterministic_decoding."
        )
    logger.warning(
        "[pilot] OUTPUT CUT OFF MID TOOL CALL: tool %r, %s/%s completion tokens, "
        "arguments_empty=%s. Redrawing once.",
        detail.get("tool"), detail.get("completion_tokens"),
        detail.get("max_tokens"), detail.get("arguments_empty"),
    )
    return True
