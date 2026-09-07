"""What was in force when this game was played, written into the game itself.

THE DEFECT THIS EXISTS FOR. On 2026-09-07 two nodes generated the same corpus
from the same config and the same recorded harness sha, and one of them absorbed
63% of its decisions in the harness while the other absorbed none: lascar's
`MAGEBENCH_AUTO_RESOLVE_EMPTY_PRIORITY=1` was read by nothing, because that
node's tree did not contain the reader. 75.7 auto-resolves per game on one node,
0 in 82 of 82 games on the other, and **nothing in either corpus recorded the
difference** -- `grep -ic "auto.resolv"` returns 0 in both nodes' server logs and
no event type names the setting. A setting that halves the corpus left no trace
in the corpus, so which value was in force is unrecoverable from a finished run.

The generalisation, and it is the reason this module is a registry rather than a
line in `game_start`: **an environment variable is a REQUEST, and a request is
not a fact about the run.** The fact is what the code resolved, and the two part
company silently -- on a rename, on a stale tree, on a typo, on a flag whose
reader lives in a different component. So this records three things, and the
third is the one that would have caught the incident:

    resolved      what the harness's own accessors returned, called here rather
                  than re-read from the environment. A second reconstruction of
                  a value the code already computed is how `seat_won` went wrong;
                  this asks the same functions the pilot asks.
    constants     values that shape the corpus and are not settings at all --
                  the completion reserve moved tonight and nothing recorded it.
    unread        every MAGEBENCH_* variable present in the environment that no
                  accessor in this registry claims. THE UNKNOWN FLAG LANDS HERE.
                  A build that cannot read a flag reports the flag as unread; a
                  build that can, does not.

WHERE THE REFUSAL LIVES, since it is not here. The pilot observes; the LAUNCHER
asserts. A launcher set the variable and knows why, so the sbatch is where "this
run depended on that knob" can be a hard check, and the manifest is where the
run's own answer is recorded. Splitting them that way is deliberate: a fatal in
this module would either fire on variables another component owns or need a list
it cannot maintain. (mtg-0f owns the launcher half.)

NOT DEPLOYED IN THE 2026-09-07 RELAUNCH, on purpose and worth stating because it
looks like an omission. The corrected lascar tree relaunches as ranokau's exact
as-run mixture and nothing else; adding this module would put bytes on the node
that neither node has ever run, and comparability with the in-flight ranokau
block is the entire purpose of that relaunch. So the corpus generated that night
is knowingly the last one that cannot describe its own settings -- which is the
single-variable rule beating a good idea, in the one place where the good idea is
about provenance.

ALSO NOT IN THE v9 EXPORT. export_llm_events selects fields and the schema would
need a version bump, so this reaches `game.jsonl` -- what the corpus tooling
reads -- and not the exported game. That makes it auditable by a person and
invisible to a downstream consumer, which is half a fix; the bump is owned
jointly with karn-engine and touches readers as well as the writer.

WHY IT WARNS AND DOES NOT REFUSE, in a repo whose rule is fail loud. The pilot is
not the only consumer of the MAGEBENCH_ namespace -- MAGEBENCH_DISP_WIDTH and
MAGEBENCH_DECK_BLOCK are read by the runner and the training-side renderer, and
refusing on them would break every launcher that sets one. The pilot cannot tell
"nobody reads this" from "somebody else reads this", so it records the request
and says so at WARNING; the artifact is what makes it answerable afterwards. That
is a deliberate non-refusal, not a silent fallback: nothing is defaulted, nothing
is guessed, and the unresolved request is written down under its own name.
"""

from __future__ import annotations

import os
from typing import Any, Callable

from magebench.pilot.auto_resolve import auto_resolve_enabled, card_text_mode
from magebench.pilot.context_segments import (
    SEGMENT_MAX_TOKENS,
    SERVE_MIN_MODEL_LEN,
    context_window_mode,
    segment_max_tokens,
)
from magebench.pilot.mulligan import mulligan_mode
from magebench.pilot.pilot_rendering import MAX_TOKENS

# name -> (env vars it is requested through, accessor that returns what the code
# actually decided). The accessor is the point: a raw env read here would be a
# second reconstruction of the same value and could disagree with the first.
_RESOLVERS: dict[str, tuple[tuple[str, ...], Callable[[], Any]]] = {
    "auto_resolve_forced": (("MAGEBENCH_AUTO_RESOLVE_FORCED",), auto_resolve_enabled),
    "card_text": (("MAGEBENCH_CARD_TEXT",), card_text_mode),
    "context_window": (("MAGEBENCH_CONTEXT_WINDOW", "MAGEBENCH_APPEND_ONLY"), context_window_mode),
    "mulligan": (("MAGEBENCH_MULLIGAN",), mulligan_mode),
    "segment_budget_tokens": (("MTG_RL_SEGMENT_BUDGET",), segment_max_tokens),
}

# Settings the pilot reads inline, with no accessor to call. Recorded as the RAW
# request with the default spelled out, so the row says what the code does rather
# than only what the environment said. Adding an accessor is the better fix; this
# is here so the manifest is complete today instead of correct-in-principle later.
_INLINE: dict[str, tuple[str, str]] = {
    "chat_prompts": ("MAGEBENCH_CHAT_PROMPTS", "on unless set to 0"),
    "compact_board": ("MAGEBENCH_COMPACT_BOARD", "off unless set to 1"),
    "decision_identity": ("MAGEBENCH_DECISION_IDENTITY", "off unless set to 1"),
    "decision_reminders": ("MAGEBENCH_DECISION_REMINDERS", "on unless set to 0"),
    "disable_thinking": ("MAGEBENCH_DISABLE_THINKING", "off unless set to 1"),
    "context_limit": ("MAGEBENCH_CONTEXT_LIMIT", "no default; append-only refuses without it"),
    "temperature": ("MAGEBENCH_TEMPERATURE", "provider default"),
    "arm": ("MAGEBENCH_ARM", "unset"),
    "hint_seats": ("MAGEBENCH_HINT_SEATS", "unset"),
}

_CLAIMED = {var for vars_, _ in _RESOLVERS.values() for var in vars_} | {
    var for var, _ in _INLINE.values()
}

# Requested through the MAGEBENCH_ namespace and read by a DIFFERENT component --
# the suite runner, the training-side renderer, the launcher. Named so they are
# not reported as unread, because reporting a variable somebody else owns as
# unread would train the reader to ignore the field. If one of these ever gets a
# pilot-side accessor, it moves up into _RESOLVERS.
_OWNED_ELSEWHERE = frozenset({
    "MAGEBENCH_DISP_WIDTH",     # the suite runner's display allocator
    "MAGEBENCH_DECK_BLOCK",     # the runner and render_conversations
    "MAGEBENCH_LOG_DIR",        # the launcher
    "MAGEBENCH_PILOT_PRESET",   # the launcher
    "MAGEBENCH_AI_NODES",       # the engine seat, not the policy seat
    "MAGEBENCH_AI_SKILLS",
    "MAGEBENCH_AI_TIME",
    "MAGEBENCH_AI_RECORD_DIR",
    "MAGEBENCH_GAME_SEED",
    "MAGEBENCH_GAME_SEEDS",
    "MAGEBENCH_LOCAL_BASE_URL",
    "MAGEBENCH_LOCAL_API_KEY",
    "MAGEBENCH_SCRYFALL_CACHE",
    "MAGEBENCH_SCRYFALL_OFFLINE",
})


def settings_manifest() -> dict[str, Any]:
    """What is in force, what was requested and not read, and what is not a setting.

    Raises nothing an accessor would not already raise: if a value is invalid the
    accessor refuses, and it refuses here at game start rather than midway through
    the first decision it touches. That is the one behaviour change this adds, and
    it moves a failure earlier rather than introducing one.
    """
    resolved: dict[str, Any] = {}
    for name, (_vars, accessor) in _RESOLVERS.items():
        resolved[name] = accessor()
    for name, (var, default_note) in _INLINE.items():
        raw = os.environ.get(var)
        resolved[name] = {"requested": raw, "default": default_note} if raw is None else raw

    unread = {
        var: os.environ[var]
        for var in sorted(os.environ)
        if var.startswith("MAGEBENCH_") and var not in _CLAIMED and var not in _OWNED_ELSEWHERE
    }

    return {
        "resolved": resolved,
        # Not settings, and recorded for the same reason: they shape every row and
        # they move. The completion reserve moved from 1024 to 2048 on 2026-09-07
        # and no artifact of the runs on either side of that says which it was.
        "constants": {
            "max_tokens": MAX_TOKENS,
            "segment_max_tokens": SEGMENT_MAX_TOKENS,
            "serve_min_model_len": SERVE_MIN_MODEL_LEN,
        },
        # The incident field. Empty is the healthy state, and it is a state the
        # check can distinguish from "nothing was set" only because `resolved`
        # sits beside it -- a manifest with an empty `unread` and an empty
        # `resolved` means the manifest itself did not run.
        "requested_but_unread": unread,
    }


def unread_warning(manifest: dict[str, Any]) -> str | None:
    """A line for the log when the environment asked for something nothing read."""
    # Indexed, not `.get(...) or {}`: this function is passed a manifest THIS
    # module built, so a missing key is a bug in the builder and must raise here
    # rather than read as "nothing was unread" -- which is the healthy answer and
    # the one that must never be produced by accident.
    unread = manifest["requested_but_unread"]
    if not unread:
        return None
    return (
        "[pilot] REQUESTED BUT UNREAD: "
        + ", ".join(f"{k}={v!r}" for k, v in sorted(unread.items()))
        + ". No accessor in this build reads these. Either another component owns "
        "them (add them to _OWNED_ELSEWHERE) or this build cannot honour them and "
        "the run differs from the one that was asked for -- which is how one node "
        "absorbed 63% of its decisions and the other absorbed none, from the same "
        "config, with nothing in either corpus recording it."
    )
