"""The own-deck card reference that goes in the system prompt.

ONE BUILDER, BOTH SIDES. Every SFT row's system prompt carries this block --
render_conversations writes `f"{system}\n\n{block}"` -- and until this module
existed the PILOT emitted none. A model trained with ~870 tokens of its own
deck's oracle text in the system prompt met, at inference, a system prompt
without it. That is a train/inference mismatch on every game the pilot has ever
played, not a subtlety of any one experiment.

It lives in the harness rather than in the mtg pipeline for the same reason
context_segments does: the pilot cannot import the pipeline, and two copies of a
renderer is one copy that will drift. mtg's records_to_sft imports these names.

THE SOURCE IS MTG_ORACLE_CARDS, DELIBERATELY, and not the harness's own
game/scryfall.py cache. They are different sources and can carry different text
for the same card; the block has to come from the one TRAINING used, or
"byte-identical" is a claim about two things that were never compared. This is
why the module reads an mtg data path and refuses rather than degrades when it
is absent.
"""

import json
import os
import pathlib
import re
import unicodedata
from collections.abc import Iterable

MTG_DATA_ROOT = pathlib.Path(os.environ.get(
    "MTG_DATA_ROOT", "/workspace1/projects/posttrainlatamgpt/ouhenio/mtg"))
def oracle_cards_path() -> pathlib.Path:
    """Where the training oracle bulk file lives, resolved AT CALL TIME.

    Not a module-level constant. A constant is read once, when the module is
    first imported, so anything that sets MTG_ORACLE_CARDS afterwards is
    silently ignored -- and "silently ignored" for a path that decides which
    card text a corpus is built from is the failure this codebase keeps paying
    for. Callers that want the value at import time can still take one.
    """
    root = pathlib.Path(os.environ.get(
        "MTG_DATA_ROOT", "/workspace1/projects/posttrainlatamgpt/ouhenio/mtg"))
    return pathlib.Path(os.environ.get(
        "MTG_ORACLE_CARDS", root / "scryfall" / "oracle_cards.jsonl"))


ORACLE_CARDS = oracle_cards_path()


def fold_name(name: str) -> str:
    """Accent- and case-insensitive key for joining card names to oracle data.

    Deck files carry ASCII spellings ("Seance") where Scryfall's canonical name
    is accented ("Séance"). An exact join drops the card, and a dropped card
    contributes NO text -- so the deck reads as a small deck rather than a broken
    one. Same fold as mage-bench `game/scryfall.py:_fold`; keep them agreeing.
    """
    stripped = "".join(
        c for c in unicodedata.normalize("NFKD", name) if not unicodedata.combining(c)
    )
    return stripped.casefold()


def load_oracle(path: pathlib.Path | None = None) -> dict[str, dict[str, str]]:
    """name -> {name, mana_cost, type_line, oracle_text}, keyed by folded name.

    Fails loudly if the file is missing. A renderer that quietly produces empty
    card text would train a model on decks with no text at all and look healthy
    doing it.
    """
    path = path if path is not None else oracle_cards_path()
    if not path.exists():
        raise FileNotFoundError(
            f"oracle card data not found at {path}. Set MTG_ORACLE_CARDS, or see "
            "the README beside the canonical copy for how to refresh it."
        )
    out: dict[str, dict[str, str]] = {}
    with path.open() as f:
        for line in f:
            c = json.loads(line)
            if c.get("layout") in ("token", "double_faced_token", "emblem", "art_series"):
                continue
            text = c.get("oracle_text")
            cost = c.get("mana_cost")
            if c.get("card_faces"):
                # Multi-face cards carry no top-level oracle_text. Reading only
                # the top level drops the whole class silently -- it already cost
                # us every DFC in the corpus once.
                if text is None:
                    # A face with no oracle_text contributes an empty string to
                    # the join, which is correct -- a vanilla face has no rules.
                    # Written out because `.get(k, "")` cannot say whether the
                    # emptiness was found or invented.
                    faces = []
                    for face in c["card_faces"]:
                        face_text = face.get("oracle_text")
                        faces.append("" if face_text is None else face_text)
                    text = "\n//\n".join(faces)
                if not cost:
                    front_cost = c["card_faces"][0].get("mana_cost")
                    cost = "" if front_cost is None else front_cost
            entry = {
                "name": c["name"],
                # Explicit: a card with no mana cost (a land) and a card whose
                # cost field is absent both render as "", but they are different
                # states and the no-fallback lint is right to want that said.
                "mana_cost": "" if cost is None else cost,
                # Same reasoning as mana_cost above: a card with no type line and
                # a record missing the field are different states, and a vanilla
                # creature genuinely has empty oracle text. Written out so the
                # emptiness is a decision rather than a shrug.
                "type_line": "" if c.get("type_line") is None else c["type_line"],
                "oracle_text": "" if text is None else text,
                # Stats the BOARD shows and the deck block did not, so the model
                # met its own creatures' sizes only once they were in play. Same
                # explicit-None convention as the fields above: a card with no
                # power (an instant) and a record missing the field are different
                # states, and "" says the first without pretending to say the
                # second.
                "power": "" if c.get("power") is None else c["power"],
                "toughness": "" if c.get("toughness") is None else c["toughness"],
                "loyalty": "" if c.get("loyalty") is None else c["loyalty"],
            }
            out.setdefault(fold_name(c["name"]), entry)
            if " // " in c["name"]:
                out.setdefault(fold_name(c["name"].split(" // ")[0]), entry)
    return out

# The (action_type, response_type, respond_with) triples the pilot emits, taken
# from 250 banked pilot games rather than invented. `kind` is the recorder's name
# for the decision; the tuple is what the tool result must carry so the blob is
# indistinguishable in shape from a real one.
BLOB_SHAPE: dict[str, tuple[str, str, str]] = {
    "priority_action": (
        "GAME_SELECT",
        "select",
        "choice=pN to play, or choice=no to pass",
    ),
    "select_attackers": (
        "GAME_SELECT",
        "select",
        "attackers=p1,p2,... or choice=yes (confirm) or choice=no (skip)",
    ),
    "declare_blockers": (
        "GAME_SELECT",
        "select",
        "blockers=p5:p1,p6:p2 (blocker:attacker) or choice=yes (confirm) or choice=no (skip)",
    ),
    "choose_target": (
        "GAME_TARGET",
        "index",
        "choice=pN, or choice=no to cancel",
    ),
    "choose_use": (
        "GAME_ASK",
        "boolean",
        "choice=yes or choice=no",
    ),
    "choose_mode": (
        "GAME_CHOOSE_ABILITY",
        "index",
        "choice=0, choice=1, etc. (not yes/no)",
    ),
    # Same shape as choose_use, and taken from the bridge rather than assumed:
    # BridgePublishedQueryBuilder.buildAskChoices sets response_type "boolean"
    # and respond_with "choice=yes or choice=no" for every ask, then attaches
    # `your_hand` when the message mentions a mulligan. So a mulligan reaches the
    # policy as an ordinary yes/no ask that happens to carry the hand.
    "choose_mulligan": (
        "GAME_ASK",
        "boolean",
        "choice=yes or choice=no",
    ),
}

# Decisions whose label is the engine's placeholder rather than its judgement.
# chooseMode takes the first valid mode (its own TODO says so) and chooseUse
# answers a blanket yes. They are kept because the format asks for them, but a
# caller training on judgement should drop them.
PLACEHOLDER_KINDS = frozenset({"choose_mode", "choose_use"})

# The bridge runs every rules string through BridgePromptFormatting.stripHtml
# before it reaches a prompt, so a recorder that stores raw engine text has to
# apply the same transform or the prompts differ in a way a model can see.
# Measured before this was applied: "<br" in 100% of our prompts and 0% of real
# ones, "hintstart" 100% vs 0%, "<i>" 80% vs 0%. Ported verbatim, same order.
_BR = re.compile(r"(?i)<br\s*/?>")
_TAG = re.compile(r"<[^>]+>")
_HEX_SUFFIX = re.compile(r" \[[0-9a-f]{3}\]")


BASIC_LANDS = frozenset(
    ["Plains", "Island", "Swamp", "Mountain", "Forest", "Wastes",
     "Snow-Covered Plains", "Snow-Covered Island", "Snow-Covered Swamp",
     "Snow-Covered Mountain", "Snow-Covered Forest"]
)

# "4 [EXO:129] Survival of the Fittest" / "SB: 2 [TMP:308] Scroll Rack"
_DCK_LINE = re.compile(r"^(SB:\s*)?(\d+)\s*\[([^\]]*)\]\s*(.+?)\s*$")


def maindeck_entries(decklist: Iterable[str]) -> list[tuple[int, str]]:
    """(count, name) for the MAINDECK, in first-appearance order.

    TWO THINGS THIS KEEPS THAT `maindeck_names` THREW AWAY, both of which the
    model needs and neither of which it had:

      * THE COUNT. `_DCK_LINE` has always captured it in group 2 and the caller
        discarded it, so a deck with 4 Lightning Bolt and one with 1 rendered
        identically. A seat could not tell its own redraw odds from its prompt.
      * THE BASIC LANDS. They were skipped outright, so "Mountain" appeared in a
        60-card mono-red prompt only inside other cards' rules text. A seat could
        not count its own mana base.

    Repeats of one name are SUMMED rather than listed twice: .dck files split a
    playset across lines when the printings differ, and four 1-ofs of the same
    card is not what that means.
    """
    counts: dict[str, int] = {}
    order: list[str] = []
    if decklist is None:
        return []
    for raw in decklist:
        m = _DCK_LINE.match(str(raw).strip())
        if not m or m.group(1):
            continue
        name = m.group(4)
        if name not in counts:
            counts[name] = 0
            order.append(name)
        counts[name] += int(m.group(2))
    return [(counts[n], n) for n in order]


def maindeck_names(decklist: Iterable[str]) -> list[str]:
    """Unique non-basic MAINDECK card names, in first-appearance order.

    Sideboard lines are dropped: the model never draws them, so their text is
    budget spent on cards that cannot appear. Order is the deck file's, which is
    stable for a given deck and therefore reproducible.

    Lines that are not card entries (`AUTHOR:`, `# Lands`, `//Created with Mage`)
    are skipped rather than counted -- measured on the bundled pool, 187 such
    lines across 1,059 decks and not one of them a card.
    """
    out: list[str] = []
    seen: set[str] = set()
    if decklist is None:
        return []
    for raw in decklist:
        m = _DCK_LINE.match(str(raw).strip())
        if not m or m.group(1):
            continue
        name = m.group(4)
        if name in BASIC_LANDS or name in seen:
            continue
        seen.add(name)
        out.append(name)
    return out


def deck_text_block(entries, oracle: dict[str, dict]) -> tuple[str, list[str]]:
    """The system-prompt card reference, and the names it actually covered.

    Returns the covered names too, because the caller must pre-seed
    `seen_oracle_cards` with EXACTLY what was written. Seeding a name whose text
    was not emitted would suppress its inline text and leave the model with no
    text for that card anywhere -- strictly worse than not doing this at all.

    An unresolved card is therefore omitted from BOTH, so it keeps its inline
    text. Silence about it would be the same failure the importer had: a card
    that contributes nothing reads as a card that costs nothing.
    """
    lines: list[str] = []
    covered: list[str] = []
    for count, name in entries:
        e = oracle.get(fold_name(name))
        if e is None:
            continue
        cost = f" {e['mana_cost']}" if e["mana_cost"] else ""
        # P/T in parentheses because that is how the BOARD renders it -- the
        # in-game text is "Goblin Guide (2/2) [tapped]". One card should not have
        # two shapes depending on where the model reads it.
        if e["power"] or e["toughness"]:
            stats = f" ({e['power']}/{e['toughness']})"
        elif e["loyalty"]:
            stats = f" (loyalty {e['loyalty']})"
        else:
            stats = ""
        head = f"{count} {e['name']}{cost}{stats}"
        lines.append(f"{head}\n{e['type_line']}\n{e['oracle_text']}".rstrip())
        covered.append(name)
    if not lines:
        return "", []
    body = "\n\n".join(lines)
    return (
        "## Your deck\n\n"
        "Every card in your deck, with its rules text. These are not repeated "
        "later, so read them here.\n\n" + body + "\n",
        covered,
    )


def build_deck_block(deck_path) -> tuple[str, list[str]]:
    """The system-prompt block for a seat's deck file, and the names it covered.

    REFUSES RATHER THAN DEGRADES. A missing deck file or a missing oracle cache
    yields no block, and a pilot that silently ran without one would be playing
    the ablated arm while reporting the treatment arm -- the failure this whole
    move exists to remove, reintroduced one layer down.

    The `covered` list is not decoration: the caller must pre-seed
    `seen_oracle_cards` with EXACTLY these names, because training does. Without
    it every card in the block ALSO gets its inline text at first sight, and the
    inference transcript stops matching the training transcript in the other
    direction.
    """
    if deck_path is None:
        raise ValueError(
            "no deck file given, so the own-deck card block cannot be built. "
            "Every SFT row's system prompt carries this block; a pilot without "
            "one is running the ablated arm. Pass --deck, or set "
            "MAGEBENCH_CARD_TEXT=none to ablate deliberately."
        )
    path = pathlib.Path(deck_path)
    if not path.exists():
        raise FileNotFoundError(f"deck file not found for the card block: {path}")
    lines = path.read_text(errors="replace").splitlines()
    return deck_text_block(maindeck_entries(lines), load_oracle())
