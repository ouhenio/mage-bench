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
# Training evaluates its cut with cost = len(text) + len(label), where `label` is
# json.dumps({"name": tool, "arguments": response}) -- THE SERIALISED TOOL CALL,
# which is also verbatim the assistant message's content. At inference the
# pending decision's answer does not exist yet -- that is the whole point of the
# call about to be made -- so BOTH SIDES price it at this reserve instead. They
# have to use the same number or the boundaries drift; see the cut in
# render_conversations.
#
# CENSUS OF THE SERIALISED LABEL, two sessions measuring independently:
#
#   karn-research  2,875,957 decisions, both blocks
#                  p50 56  p90 57  p99 64  p99.9 79  p99.99 96  max 1436
#   karn-engine      956,320 decisions, block 1 only
#                  p50 56  p90 57  p99 64  p99.9 79  p99.99 96  max 1034
#
# The bulk agrees to the character across both counts, and block 1's max is one
# of the six outliers in the full set (1436, 1307, 1231, 1034, 982, 931). All six
# are `choose_action` with an `attackers` argument: a comma-separated list of
# permanent short ids, so a mass attack makes it long -- the 1436 lists about 300
# attackers. Rate: 2.09 per million.
#
# 4096 IS HEADROOM, NOT A BOUND, and saying so is the point. The previous value
# was 256, set from a maximum of 161 over a 300,000-decision sample -- and at
# 2.09 per million that sample expected 0.63 outliers, so P(it contained none)
# was 0.53. The bulk statistics were right; the extreme condition simply had no
# room to occur. A maximum is a bound only when the sample gave the extreme a
# chance to appear, and nothing here bounds `attackers` except board width, so a
# bigger sample's maximum would be the same mistake with a bigger number.
#
# What settles the value is that headroom is nearly free:
#
#   reserve   256   worst-case segment loss 0.081% of the 316,539-char budget
#   reserve  2048   0.647%
#   reserve  4096   1.294%
#   reserve  8192   2.588%
#
# 4096 covers 2.9x the largest label in 2.9M decisions for 1.3% of a segment.
# The guard in render_conversations is what makes an exceedance loud rather than
# silent, and it prints the full distribution so the next person sizes this from
# data instead of from one error message.
PENDING_ANSWER_RESERVE_CHARS = 4096

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


def segment_max_tokens() -> int:
    """The segment budget in tokens, with an RL override.

    MTG_RL_SEGMENT_BUDGET lets an RL loop cut at a smaller budget than the SFT
    corpus uses. Unset means 131,072 and the SFT corpus is unaffected.

    ONLY DOWNWARDS. A value above the default is refused rather than clamped:
    the budget's whole job is to keep a segment inside what the server will
    accept, and silently honouring a larger number would produce segments that
    the token-anchored guard then rejects mid-game -- trading a loud
    misconfiguration for a quiet one.

    Read at CALL TIME, not captured into a constant at import, so a loop that
    sets it after this module is first imported is not silently ignored.
    """
    raw = os.environ.get("MTG_RL_SEGMENT_BUDGET")
    if raw is None:
        return SEGMENT_MAX_TOKENS
    try:
        value = int(raw)
    except ValueError:
        empty = "" if raw else (
            " An EMPTY value is not the same as unset here, deliberately: a "
            "launcher writing MTG_RL_SEGMENT_BUDGET=\"${RL_BUDGET:-}\" with "
            "RL_BUDGET unset would otherwise get the full 131,072 silently while "
            "believing it had set a smaller one. Leave the variable out entirely "
            "to take the default."
        )
        raise ValueError(
            f"MTG_RL_SEGMENT_BUDGET={raw!r} is not an integer number of tokens.{empty}"
        ) from None
    if value <= 0:
        raise ValueError(
            f"MTG_RL_SEGMENT_BUDGET={value} must be positive; a zero or negative "
            f"budget would cut before every decision and make no progress."
        )
    if value > SEGMENT_MAX_TOKENS:
        raise ValueError(
            f"MTG_RL_SEGMENT_BUDGET={value} exceeds the {SEGMENT_MAX_TOKENS}-token "
            f"default. This override exists to cut SMALLER than the SFT corpus, "
            f"not larger: a bigger budget produces segments the serving guard "
            f"rejects mid-game, which is a quiet failure where this is a loud one."
        )
    return value


def segment_budget_chars(max_tokens: int | None = None) -> float:
    """Characters a segment may hold, from the token budget it has to fit.

    The inverse of the token estimate: approx_tokens = (chars/3) /
    CHARS_PER_TOKEN_WORST, so chars = tokens * 3 * CHARS_PER_TOKEN_WORST.

    THIS IS A LOOSE BOUND ON TOKENS AND IS NOT THE SERVING GUARD. Measured over
    10,875 recorded prompts the real (chars/3)/tokens ratio runs 0.771 to 0.944,
    and CHARS_PER_TOKEN_WORST is 0.805 -- above the observed minimum. At that
    minimum this budget is 136,938 tokens, 4.5% past the 131,072 it names. The
    guard that actually bounds the server is the token-anchored one in
    render_context, which counts the engine's own reported tokens.

    TWO POPULATIONS, TWO ANSWERS, AND BOTH ARE RIGHT. The 0.771 above is the
    minimum over 10,875 recorded INFERENCE prompts. karn-research measured 200
    rendered TRAINING rows through apply_chat_template and got a minimum of
    0.931 (chars/token 2.794 to 3.600, p50 2.994). Those disagree about whether
    0.805 is a valid lower bound, and the disagreement is not an error: the
    serving guard is applied to inference prompts, where 0.771 governs and is
    why SERVE_MIN_MODEL_LEN sits above SEGMENT_MAX_TOKENS; the character budget
    is applied to training renders, where 0.931 governs and the constant is
    conservative by ~24%.

    So the constant is safe in both directions but for different reasons, and
    the cost on the training side is rows rather than correctness -- segments
    close earlier than the token cap requires. DO NOT 'CORRECT' IT TOWARD EITHER
    MEASURED MEDIAN: raising it makes the guard fire later on the inference side,
    which is the failure this constant exists to prevent. Anything needing a real
    ratio should measure it, and anything needing a row count should read
    stats.json, where it is counted rather than derived.
    """
    if max_tokens is None:
        max_tokens = segment_max_tokens()
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
