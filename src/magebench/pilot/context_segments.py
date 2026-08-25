"""The segment rule: ONE definition, imported by training and by inference.

The training assembler (`pipelines/synth/render_conversations.py`) splits a
trajectory that would outgrow the context into segments, each standing alone:
system prompt and deck block repeated, `seen` reset, the crossing decision
re-rendered as the first of the next segment. Inference has to do the same
thing at the same place, or the model meets a boundary in play that it never
met in training.

It lives HERE, in the harness, rather than in the pipeline, because the pilot
cannot import the pipeline: the pipeline imports magebench, not the other way
round. Two copies of a threshold is one copy that will drift.
"""

import os

from magebench.pilot.pilot_rendering import CHARS_PER_TOKEN_WORST

# The training default, from render_conversations' --max-tokens. A segment is
# cut so that its rendered characters fit this many tokens under the worst
# chars-per-token ratio.
SEGMENT_MAX_TOKENS = 131072

# render_conversations' --max-decisions. A second ceiling on the same segment:
# it bounds decisions rather than characters, and whichever binds first wins.
SEGMENT_MAX_DECISIONS = 2000

# The answer that is about to be generated, which training counts and inference
# cannot yet know.
#
# Training evaluates its cut with cost = len(text) + len(label), where `label`
# is the tool call the teacher made. At inference the pending decision's answer
# does not exist yet -- that is the whole point of the call about to be made --
# so the pending cost is reserved instead of measured.
#
# MEASURED, both sides, so the reserve covers whichever is larger:
#   training labels     n=300,000 decisions   p50 56    p99 64    max 161 chars
#   inference assistant n=1,770 messages      p50 192   p99 201   max 222 chars
# 256 rounds the larger one up. Against a 316,539-character budget the reserve
# is 0.08%, so it moves the boundary by at most one decision, and it moves it
# EARLY: inference closes a segment no later than training would.
PENDING_ANSWER_RESERVE_CHARS = 256

# The minimum max_model_len a server must advertise to run the full-context arm.
#
# NOT SEGMENT_MAX_TOKENS, and the gap is measured rather than padded. The budget
# converts tokens to characters through CHARS_PER_TOKEN_WORST = 0.805, but that
# constant is not the worst ratio: over 10,875 recorded prompts the real
# (chars/3)/tokens ratio runs
#
#     min 0.771   p1 0.778   p50 0.855   max 0.944
#
# and the minimum is BELOW the constant. So the 316,539-character budget is
# 131,072 tokens only at the constant; at the observed minimum it is 136,852,
# and the completion reserve vLLM counts against the same limit adds MAX_TOKENS:
#
#     316,539 / 3 / 0.771 + 1,024 = 137,876 tokens
#
# 143,360 (140 * 1024) is the next round figure above that, 4.0% of headroom.
# Serving at 131,072 instead would leave a band where the character budget
# permits a segment the server refuses -- and the refusal lands the game on the
# reactive reset, which is the boundary this whole change exists to remove.
SERVE_MIN_MODEL_LEN = 143360

_MODES = ("full", "windowed")


def segment_budget_chars(max_tokens: int = SEGMENT_MAX_TOKENS) -> float:
    """Characters a segment may hold, from the token budget it has to fit.

    The inverse of the token estimate: approx_tokens = (chars/3) /
    CHARS_PER_TOKEN_WORST, so chars = tokens * 3 * CHARS_PER_TOKEN_WORST.

    THIS IS A LOOSE BOUND ON TOKENS AND IS NOT THE SERVING GUARD. Measured over
    10,875 recorded prompts the real (chars/3)/tokens ratio runs 0.771 to 0.944,
    and CHARS_PER_TOKEN_WORST is 0.805 -- above the observed minimum. At that
    minimum this budget is 136,938 tokens, 4.5% past the 131,072 it names. The
    guard that actually bounds the server is the token-anchored one in
    render_context, which counts the engine's own reported tokens.
    """
    return max_tokens * 3 * CHARS_PER_TOKEN_WORST


def should_close_segment(
    *,
    used_chars: int,
    pending_cost_chars: int,
    decisions_in_segment: int,
    budget_chars: float,
    max_decisions: int,
) -> bool:
    """Whether the pending decision starts a new segment instead of joining this one.

    Lifted verbatim from render_conversations' loop condition:

        if cur and (used + cost > budget_chars or len(cur) >= args.max_decisions)

    `decisions_in_segment > 0` is the `cur` in that line and it is load-bearing:
    a segment must never be closed empty, or a single decision larger than the
    whole budget would cut forever and make no progress.
    """
    if decisions_in_segment <= 0:
        return False
    return (
        used_chars + pending_cost_chars > budget_chars
        or decisions_in_segment >= max_decisions
    )


def context_window_mode() -> str:
    """`full` (whole game, append-only) or `windowed` (recent + summarised older).

    Two spellings, one setting. MAGEBENCH_CONTEXT_WINDOW is the name the
    training-side presets and the report use; MAGEBENCH_APPEND_ONLY is the
    older name, still exported by every pipeline in the mtg repo and recorded
    into run provenance, so it keeps working.

    A rename that quietly ignored the old name would turn a stale
    `MAGEBENCH_APPEND_ONLY=0` -- the windowed reference arm -- into a no-op that
    silently ran the full-context arm instead, and the A/B it was written for
    would compare an arm against itself. So the old name is honoured, and a
    disagreement between the two is refused rather than resolved by precedence.
    """
    window = os.environ.get("MAGEBENCH_CONTEXT_WINDOW")
    legacy = os.environ.get("MAGEBENCH_APPEND_ONLY")

    if window is not None:
        if window not in _MODES:
            raise ValueError(
                f"MAGEBENCH_CONTEXT_WINDOW={window!r} is not one of {_MODES}. "
                f"`full` sends the whole game, the way training assembles it; "
                f"`windowed` keeps the most recent decisions and summarises older "
                f"tool results, which is upstream's only behaviour and the "
                f"reference arm for the paired A/B."
            )
        if legacy is not None:
            legacy_mode = "windowed" if legacy == "0" else "full"
            if legacy_mode != window:
                raise ValueError(
                    f"MAGEBENCH_CONTEXT_WINDOW={window!r} and "
                    f"MAGEBENCH_APPEND_ONLY={legacy!r} ask for different things "
                    f"({window} vs {legacy_mode}). They are two names for one "
                    f"setting; picking a winner here would silently run an arm "
                    f"nobody asked for. Set one."
                )
        return window

    if legacy is not None:
        return "windowed" if legacy == "0" else "full"

    # Our fork's default since 2026-08-17, after the paired A/B on 8 identical
    # deals. Upstream has neither name and no full-context path at all, so
    # upstream parity means windowed by construction, not by this default.
    return "full"


def require_servable_context(max_model_len: int, *, source: str) -> None:
    """Refuse a rollout whose server cannot hold a full segment.

    `max_model_len` must be the SERVING engine's own advertised value, not the
    training row cap. They are different numbers -- a rollout deliberately
    serves above the cap that truncates training rows -- and a check that
    accepted either would pass on the wrong one.
    """
    if max_model_len < SERVE_MIN_MODEL_LEN:
        raise ValueError(
            f"the server at {source} advertises max_model_len={max_model_len}, "
            f"below the {SERVE_MIN_MODEL_LEN} tokens a {SEGMENT_MAX_TOKENS}-token "
            f"segment can actually reach (see SERVE_MIN_MODEL_LEN: the character "
            f"budget is 131,072 tokens at CHARS_PER_TOKEN_WORST and 136,852 at the "
            f"worst ratio measured, plus the completion reserve). A game long "
            f"enough to reach the budget would be refused by the server instead of "
            f"cut at a decision boundary, so its trajectory would end somewhere "
            f"training never puts a boundary. Serve at least "
            f"{SERVE_MIN_MODEL_LEN}, or run the windowed arm deliberately with "
            f"MAGEBENCH_CONTEXT_WINDOW=windowed."
        )
