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


def cap_hit_with_call_open(
    choice: object,
    response: object,
    *,
    max_tokens: int,
    offered: set[str],
) -> tuple[bool, dict]:
    """Three terms, all required, exactly as agreed with karn-research's classifier.

    Returns (matched, detail). `detail` is emitted whether or not a retry happens,
    so the population stays countable after it stops being fatal -- a fix that
    hides its own trigger is how the truncation record went unread for a corpus.
    """
    finish = getattr(choice, "finish_reason", None)
    usage = getattr(response, "usage", None)
    completion = getattr(usage, "completion_tokens", None) if usage else None
    message = getattr(choice, "message", None)
    tool_calls = getattr(message, "tool_calls", None) if message is not None else None

    detail: dict = {
        "finish_reason": finish,
        "completion_tokens": completion,
        "max_tokens": max_tokens,
    }
    if finish != "tool_calls" or not tool_calls:
        return False, detail
    if not isinstance(completion, int) or completion < max_tokens:
        return False, detail

    fn = getattr(tool_calls[0], "function", None)
    # None and "" are DIFFERENT facts and the row keeps them apart: no name
    # attribute at all is a malformed response, an empty string is a call whose
    # name was cut before its first character. Collapsing them with `or ""` is the
    # absent-vs-empty conflation, and the lint was right to refuse it.
    name = getattr(fn, "name", None)
    arguments = getattr(fn, "arguments", None)
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
    return matched, detail


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
