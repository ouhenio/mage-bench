"""The engine's own mulligan rule, answered by the harness instead of the policy.

WHY THE HARNESS ANSWERS IT. A mulligan is a fixed rule over the opening hand, not
a judgement the policy is being trained to make: the corpus records mulligans but
excludes them from training (build_dataset.TRAIN_EXCLUDED_KINDS), so asking the
model at inference would be a train/inference mismatch of a new kind -- the model
conditioned to answer something it never saw supervised.

THE RULE IS LIFTED FROM ComputerPlayer.chooseMulligan, NOT PARAPHRASED:

    if (hand.size() < 6 || isTestMode() || Momir) return false;   // keep
    Set<Card> lands = hand.getCards(new FilterLandCard(), game);
    return lands.size() < 2 || lands.size() > hand.size() - 2;    // mulligan

Both engine-only conditions are absent at inference: `isTestMode` is set only by
CardTestPlayerAPIImpl, and Momir is a game type we do not run.
"""

import os

_MODES = ("engine-rule", "model")


def mulligan_mode() -> str:
    """`engine-rule` (the harness answers) or `model` (the policy answers).

    Default is `engine-rule` in our fork. Upstream has no such knob and always
    asks the model, so upstream parity means `model` by construction.
    """
    mode = os.environ.get("MAGEBENCH_MULLIGAN")
    if mode is None:
        return "engine-rule"
    if mode not in _MODES:
        raise ValueError(
            f"MAGEBENCH_MULLIGAN={mode!r} is not one of {_MODES}. `engine-rule` "
            f"answers the mulligan with ComputerPlayer.chooseMulligan without "
            f"calling the policy; `model` puts it to the policy, which is "
            f"upstream's only behaviour and the reference arm."
        )
    return mode


def is_mulligan_decision(data: dict) -> bool:
    """Is this tool result the mulligan ask?

    Gated on the SAME two things the bridge gates on. buildAskChoices sets
    response_type "boolean" for every ask and attaches `your_hand` only when the
    message mentions a mulligan, so requiring both is narrower than either: a
    non-mulligan ask has no hand, and any other decision carrying a hand is not
    a boolean.
    """
    if not isinstance(data, dict) or not data.get("action_pending"):
        return False
    if data.get("response_type") != "boolean":
        return False
    message = data.get("message")
    # Explicit rather than `.get(key, "")`: a decision with no message field and
    # one whose message is empty are different states, and the no-fallback lint
    # is right that folding them is how a missing field reads as a present one.
    if not isinstance(message, str) or "mulligan" not in message.lower():
        return False
    return isinstance(data.get("your_hand"), list)


def count_lands(hand: list[dict]) -> int:
    """Lands in the projected hand.

    `is_land` comes from CardView.isLand(); the engine counts with FilterLandCard
    on the real Card. MEASURED AGREEMENT: 745 recorded mulligan hands, 1,615
    distinct cards, ZERO disagreements -- and the comparison was shown able to
    report one (flipping a single card's is_land produced a detected mismatch),
    so the zero is a live zero rather than a dead check.

    WHAT THAT ZERO DOES NOT COVER. The predicates could diverge on a modal
    double-faced card whose back face is a land, and the deck pool this was
    measured on is 1998-2015 tournament decks -- MDFCs arrive in 2020. The four
    double-faced names in the sample are split cards (Far // Away and friends),
    none of them lands. So the condition never occurred: the zero says the
    predicates agree on this pool and says nothing about MDFCs.
    """
    return sum(1 for card in hand if card.get("is_land"))


def should_mulligan(hand: list[dict]) -> bool:
    """The engine's answer for this hand. True = mulligan, False = keep."""
    size = len(hand)
    # NOT A DEAD BRANCH, and not the paper London rule either. XMage's
    # LondonMulligan.mulligan() draws the full hand and bottoms down to the new
    # size IMMEDIATELY, inside the same call rather than at keep time, so each
    # successive decision sees a SMALLER hand. Measured over 290 games: 582
    # decisions at 7 cards, 112 at 6, 51 at 5, chaining exactly. This fired on
    # all 51 five-card hands.
    if size < 6:
        return False
    lands = count_lands(hand)
    # `size - 2`, never a literal 5: the threshold moves with the hand.
    return lands < 2 or lands > size - 2


def mulligan_choice(hand: list[dict]) -> str:
    """The answer in the policy's own vocabulary.

    ChooseActionTool defines it: "yes=mulligan/confirm, no=keep/pass". The engine
    thinks in keep/mulligan and the tool speaks yes/no; translating here keeps a
    second vocabulary out of the call site.
    """
    return "yes" if should_mulligan(hand) else "no"
