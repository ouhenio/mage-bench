"""Forced decisions: answered by the harness, never put to the policy.

A decision that offers NO options is not a decision. Measured on block 2,
948,511 decisions: 310,980 (32.8%) offer zero choices, and 310,954 of those --
99.99% -- are answered by passing. They carry roughly a third of all prompt
tokens (see docs/runs/prompt-token-budget.md) for no signal at all: nothing is
learned by predicting an answer that was never chosen.

WHY ZERO AND NOT "FEWER THAN TWO", which is where the token-budget report drew
the line. A decision offering ONE option is play-or-pass, and the teacher takes
the play one time in seven: of 124,007 such decisions, 7,269 answered with a pN
and 9,960 answered `{"attackers": "p7"}` or `{"blockers": "p31:p21"}`. A single
available creature is not "no choice", it is *attack with it or not*. Folding
those in would have suppressed every single-attacker attack and every
single-blocker block in the corpus, on both sides of the train/inference line.
"""

import os

# The two response types the zero-choice class actually contains: 310,738 select
# and 242 index, of 310,980. ZERO boolean.
#
# This is a GUARD, not a description. A `choose_use` or a mulligan reaches the
# pilot as response_type "boolean" carrying no `choices` key, so a predicate
# written only as `len(choices) == 0` would call those forced and auto-pass them
# -- declining an ability, or keeping a hand the mulligan rule wanted to throw.
# They do not appear in the corpus's zero-choice class because the recorder
# builds options for them, but the bridge projection is a different object and
# the pilot sees that one.
AUTO_RESOLVABLE_RESPONSE_TYPES = frozenset({"select", "index"})

# What the engine answers on a forced decision, 310,954 times out of 310,980.
FORCED_ANSWER = {"choice": "no"}


def auto_resolve_enabled() -> bool:
    """Whether the harness answers forced decisions itself.

    Default on in our fork. Upstream has no such knob and always asks the model,
    so upstream parity means off by construction rather than by a default here.
    """
    value = os.environ.get("MAGEBENCH_AUTO_RESOLVE_FORCED")
    if value is None:
        return True
    if value not in ("0", "1"):
        raise ValueError(
            f"MAGEBENCH_AUTO_RESOLVE_FORCED={value!r} is not 0 or 1. 1 answers a "
            f"decision offering no options without calling the policy; 0 puts "
            f"every decision to the policy, which is upstream's behaviour and the "
            f"reference arm."
        )
    return value == "1"


def is_forced_decision(data: dict) -> bool:
    """True when this decision offers the policy nothing to choose between.

    `choices` ABSENT means zero offered, not missing data -- measured by
    karn-research on 120,000 block-1 decisions: of 39,853 rows with no `choices`
    key, 39,849 answer `{"choice": "no"}`, and their `respond_with` reads
    "choice=pN to play, or choice=no to pass" with no pN available.
    """
    if not isinstance(data, dict) or not data.get("action_pending"):
        return False
    if data.get("response_type") not in AUTO_RESOLVABLE_RESPONSE_TYPES:
        return False
    choices = data.get("choices")
    if choices is None:
        # THE ONE PLACE A MISSING KEY LEGITIMATELY MAPS TO A VALUE, and only
        # because the mapping was measured rather than assumed: absent means zero
        # offered. Written out rather than as `or []` so the mapping is a
        # statement someone can disagree with, not a shrug in an expression.
        return True
    return len(choices) == 0


def chose_unoffered(data: dict, response: dict) -> bool:
    """Answered with a play while offering nothing. Rare, real, and counted.

    26 of block 2's 310,980 zero-choice decisions (0.01%) answer with a pN
    despite offering no options -- 18 select and 8 index. Auto-resolving passes
    those, so the harness declines a play the engine would have made. ACCEPTED
    KNOWINGLY at that rate; this exists so the audit can count them rather than
    have the loss show up as an unexplained behaviour change.
    """
    if not is_forced_decision(data):
        return False
    if response is None:
        return False
    choice = response.get("choice")
    return bool(choice) and choice != "no"


def render_auto_resolved(decision_index: int) -> str:
    """The one line a forced decision contributes to the transcript.

    THE SAVING IS THE RENDERING, not the skipped call. A forced decision used to
    render a full board -- the same board as the decision before it, since
    nothing happened in between -- and that is where the third of prompt tokens
    went. It still APPEARS, because a decision silently missing from the
    transcript would make the index disagree with the event stream and nothing
    downstream could reconcile the two.

    One function, imported by the pilot renderer and the training renderer both,
    so the two cannot drift into rendering the same event differently.
    """
    return f"[Decision {decision_index}] No action available; passed automatically."


# ---------------------------------------------------------------- card text

_CARD_TEXT_MODES = ("first-reveal", "none", "always")


def card_text_mode() -> str:
    """How much oracle text the rendered decision carries.

    THE THREE ARMS OF THE READING TEST, and they are one knob rather than two
    booleans because the fourth combination has no meaning.

        first-reveal  a card's text appears ONCE, the first time it is seen,
                      and never again. Today's behaviour and the default.
        none          no oracle text at all.
        always        the text of every card on the board, on every decision.

    WHY `always` EXISTS, which is the part worth reading. Under `first-reveal` a
    policy is asked to have read something up to several hundred decisions ago
    and carried it, so a small gap between `first-reveal` and `none` cannot
    distinguish a policy that cannot READ from one that cannot REMEMBER -- both
    score the same on the same cards. With `always`, that ambiguity is
    resolvable: if first-reveal is close to none while always beats both, the
    model reads and does not remember, which is a different and more actionable
    finding than "does not read".

    NOTE FOR ARM B: there is no deck block at inference. The training rows carry
    own-deck oracle text in the system prompt; the pilot only ever shows text for
    cards on the board. So `none` removes all card text the PILOT emits, and the
    training-side deck block is suppressed separately by render_conversations'
    own --no-deck-text flag.
    """
    mode = os.environ.get("MAGEBENCH_CARD_TEXT")
    if mode is None:
        return "first-reveal"
    if mode not in _CARD_TEXT_MODES:
        raise ValueError(
            f"MAGEBENCH_CARD_TEXT={mode!r} is not one of {_CARD_TEXT_MODES}. "
            f"Defaulting would run an arm nobody selected, which in a three-arm "
            f"ablation is the one failure that cannot be seen in the results."
        )
    return mode
