"""Near-decision content injection: the seam arm D needs.

WHAT THIS IS FOR. The prompt experiment delivers ONE document two ways. Arm S
appends the whole thing to the system prompt; arm D surfaces ONE SECTION at the
decision whose class the section claims to move. The prose is byte-identical
between the arms by construction -- the triggers SELECT, they never rewrite. That
is what makes a difference between the arms attributable to delivery rather than
to content.

INERT WHEN UNCONFIGURED, AND THAT IS A REGISTERED CONDITION, NOT A COURTESY.
docs/eval/prompt-experiment.md:161-164 makes the cheap control usable only if this
build is shown inert on the game path with no configuration -- no injection, no
changed rendering. Arm C runs on THIS build with the seam present and empty, so if
the seam perturbs anything at all when unconfigured, the control stops being a
control and the cheapest arm becomes the least comparable one. Hence: with no
config, `inject_near_decision` returns the empty list and the caller extends by
nothing, which is byte-identical to the build without it.

The existing `land_drop_reminder` is deliberately NOT replaced. It is measured
behaviour (+5.65 life/game, p=0.00025) and is part of the control's baseline; a
seam that rewrote it would confound the experiment with a change to the thing the
experiment measures against.
"""

from __future__ import annotations

import os
import pathlib
import re

# One section per decision, never more. Two imperatives for one slot is the same
# defect land_drop_reminder's docstring calls out for reminders, and it would also
# make "which section moved this class" unanswerable -- which is the specificity
# control, the design's load-bearing test.
_SECTION_RE = re.compile(r"^##\s+(\w+)\b", re.M)

_MAIN_PHASES = {"PRECOMBAT_MAIN", "POSTCOMBAT_MAIN", "Precombat Main", "Postcombat Main"}

# THE INERT RESULT IS A SINGLETON, at ranokau's request, so that inertness is
# checkable by IDENTITY rather than by argument: a test asserts
# `inject_near_decision(...) is INERT` and cannot be satisfied by a different empty
# list that happens to compare equal. It also makes the no-op explicit at the call
# site -- extending by this object provably cannot change `lines`.
#
# Never mutate it. It is returned to every caller on the control build, and a single
# `.append` anywhere would inject into every decision of every game at once.
INERT: list[str] = []


def content_path() -> pathlib.Path | None:
    """The configured content document, or None for the inert (control) build.

    No default path on purpose. A default would make the seam active by accident on
    any box where the file happened to exist, and the arm would be discovered at
    scoring rather than declared at launch.
    """
    raw = os.environ.get("MAGEBENCH_INJECT_CONTENT")
    if raw is None or not raw.strip():
        return None
    p = pathlib.Path(raw)
    if not p.is_file():
        # LOUD. A misspelled path that silently disabled injection would run arm D
        # as a second copy of arm C, and the two would be indistinguishable in every
        # artifact -- the arm label would say D and the transcript would be C.
        raise FileNotFoundError(
            f"MAGEBENCH_INJECT_CONTENT={raw!r} does not exist. Refusing to run an "
            f"injection arm with no content; that is arm C wearing arm D's label."
        )
    return p


def parse_sections(text: str) -> dict[str, str]:
    """Split the content document into `## Name` sections, verbatim.

    Verbatim matters: the pre-registration requires the prose be byte-identical
    between arms, so this may split the document and must never reflow, strip or
    reformat what it hands back.
    """
    marks = [(m.group(1).lower(), m.start()) for m in _SECTION_RE.finditer(text)]
    out: dict[str, str] = {}
    for i, (name, start) in enumerate(marks):
        end = marks[i + 1][1] if i + 1 < len(marks) else len(text)
        out[name] = text[start:end].rstrip("\n")
    return out


def _has_action(choices: list, action: str) -> bool:
    for choice in choices:
        if getattr(choice, "action", None) == action:
            return True
    return False


def select_section(
    action_type: str | None, choices: list, phase: str | None, message: str | None
) -> str | None:
    """Which section this decision triggers, or None.

    THE FIRST VERSION OF THIS MAP COULD NEVER FIRE, and it is worth saying why in
    the code rather than only in a ledger. I keyed it on `select_attackers`,
    `declare_blockers`, `priority_action` -- the vocabulary of the ENGINE-side
    recorder (`kind` in ai_decisions.jsonl), which is the artifact I had been
    reading all week. But this function is handed a PILOT-side Decision, whose
    `action_type` comes straight off the bridge blob and uses the client callback
    vocabulary: GAME_SELECT, GAME_TARGET, GAME_PLAY_MANA, and so on
    (ClientCallbackMethod.java:47-66). Two vocabularies for one concept, and I wrote
    triggers in the one my code does not see. karn-ranokau-stable caught it by
    rendering 400 real corpus decisions and finding zero injections.

    ATTACKERS AND BLOCKERS ARE BOTH GAME_SELECT and `message` is what separates
    them, from HumanPlayer.java where the events are fired:
        :1889  fireSelectEvent(playerId, "Select attackers", options)
        :2162  fireSelectEvent(playerId, "Select blockers", options)
    So the role/race split is a message test, not an action_type test. Checked in
    the Java rather than inferred from a sample, because a sample that happens to
    contain no blocker decisions would agree with any guess.
    """
    if action_type == "GAME_SELECT":
        # An explicit test, not `message or ""`. The repo's own no-fallback lint
        # catches that idiom and is right to: a GAME_SELECT with no message is a
        # decision this map cannot classify, and coercing it to "" would silently
        # sort it into neither class while looking like a considered answer.
        if message is None:
            return None
        # Prefix match, not equality: the engine appends context to some select
        # messages, and an equality test would silently stop firing the first time
        # it did -- the same failure as the map this replaces.
        if message.startswith("Select attackers"):
            return "role"
        if message.startswith("Select blockers"):
            return "race"
        # FALL THROUGH, do not return None. Priority decisions also arrive as
        # GAME_SELECT -- "Play spells and abilities", "Play instants and activated
        # abilities" are 482 of 600 in ranokau's stratified sample -- so returning
        # here claimed every priority decision for the combat branch and then
        # refused it, which is why `sequencing` fired ZERO times in a stratum
        # built to contain it. GAME_SELECT names the DIALOG, not the decision
        # class; only the message and the offered choices name the class.
    if action_type in ("GAME_PLAY_MANA", "GAME_PLAY_XMANA"):
        # The harness section's registered class: payment collisions.
        return "harness"
    if phase in _MAIN_PHASES and _has_action(choices, "cast"):
        # Sequencing: a sorcery-speed decision with something castable on the table.
        # Deliberately NOT gated on action_type -- the previous gate was the bug, and
        # this predicate is the one that survived because it reads the choices the
        # decision actually offers.
        return "sequencing"
    return None


def inject_near_decision(
    action_type: str | None, choices: list, phase: str | None, message: str | None = None
) -> list[str]:
    """Zero or one section, as rendered lines. EMPTY when unconfigured.

    Returning [] rather than None so the caller's `lines.extend(...)` is a no-op on
    the control build -- the inertness condition is that this call changes nothing,
    and an extend-by-empty is the only shape that guarantees it without the caller
    needing a branch.
    """
    path = content_path()
    if path is None:
        return INERT
    name = select_section(action_type, choices, phase, message)
    if name is None:
        return INERT
    section = parse_sections(path.read_text(encoding="utf-8")).get(name)
    if section is None:
        raise KeyError(
            f"trigger selected section {name!r} but {path} has no '## {name}' heading. "
            f"The trigger map and the content document have diverged, and the arm "
            f"would run with that decision class silently untreated."
        )
    return section.splitlines()
