"""The multi-face key-mismatch: a front-face query resolved by Scryfall must be
keyed back to the QUERY name, or the caller's join reports a found card as
missing and drops it (this deleted every multi-face card in all 81 imported
decks, and with them the training corpus's whole card class)."""
from unittest.mock import patch

from magebench.game import scryfall


def _fake_collection(names):
    found = []
    for n in names:
        if n == "Malakir Rebirth":  # front-face query -> full-name card
            found.append({"name": "Malakir Rebirth // Malakir Mire",
                          "set": "znr", "collector_number": "111"})
        elif n == "Lightning Bolt":
            found.append({"name": "Lightning Bolt",
                          "set": "clu", "collector_number": "141"})
    not_found = [{"name": n} for n in names
                 if n not in ("Malakir Rebirth", "Lightning Bolt")]
    return found, not_found


def test_front_face_query_is_keyed_by_query_name():
    with patch.object(scryfall, "collection", side_effect=_fake_collection):
        resolved = scryfall.resolve_cards(["Malakir Rebirth", "Lightning Bolt"])
    assert resolved["Malakir Rebirth"] == ("ZNR", "111")
    assert resolved["Lightning Bolt"] == ("CLU", "141")
    # the failure mode: keyed under the full name the caller never asked for
    assert "Malakir Rebirth // Malakir Mire" not in resolved


def test_unrequested_card_is_not_guessed(capsys):
    def bad_collection(names):
        return [{"name": "Wrenn and Six", "set": "mh1", "collector_number": "217"}], []
    with patch.object(scryfall, "named", return_value=None), \
         patch.object(scryfall, "collection", side_effect=bad_collection):
        resolved = scryfall.resolve_cards(["Fury"])
    assert "Wrenn and Six" not in resolved
    assert "unrequested" in capsys.readouterr().out
