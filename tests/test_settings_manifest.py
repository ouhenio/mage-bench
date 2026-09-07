"""The manifest, and the one field that would have caught the incident.

Two nodes generated the same corpus from the same config and the same recorded
harness sha; one absorbed 63% of its decisions in the harness and the other
absorbed none, because one tree contained the reader for
MAGEBENCH_AUTO_RESOLVE_EMPTY_PRIORITY and the other did not. Nothing in either
corpus recorded the difference. `requested_but_unread` is that difference, made
into a field.
"""

import pytest

from magebench.pilot.pilot_rendering import MAX_TOKENS
from magebench.pilot.settings_manifest import settings_manifest, unread_warning


def test_a_flag_no_accessor_reads_is_reported_as_unread(monkeypatch):
    """THE INCIDENT. The env asks for it, the build cannot honour it, the row says so."""
    monkeypatch.setenv("MAGEBENCH_AUTO_RESOLVE_EMPTY_PRIORITY", "1")
    m = settings_manifest()
    assert m["requested_but_unread"] == {"MAGEBENCH_AUTO_RESOLVE_EMPTY_PRIORITY": "1"}
    warning = unread_warning(m)
    assert warning is not None
    assert "MAGEBENCH_AUTO_RESOLVE_EMPTY_PRIORITY" in warning


def test_a_flag_an_accessor_reads_is_not_reported_as_unread(monkeypatch):
    """Positive control: the field is about READERS, not about the namespace.

    Without this, a manifest that called every MAGEBENCH_ variable unread would
    pass the test above and mean nothing.
    """
    monkeypatch.setenv("MAGEBENCH_AUTO_RESOLVE_FORCED", "0")
    m = settings_manifest()
    assert m["requested_but_unread"] == {}
    assert m["resolved"]["auto_resolve_forced"] is False
    assert unread_warning(m) is None


def test_a_variable_another_component_owns_is_not_reported_as_unread(monkeypatch):
    """MAGEBENCH_DECK_BLOCK is read by the runner and the renderer, not the pilot.

    Reporting it here would put a permanent false positive in every run, which is
    how a warning field stops being read.
    """
    monkeypatch.setenv("MAGEBENCH_DECK_BLOCK", "on")
    monkeypatch.setenv("MAGEBENCH_DISP_WIDTH", "256")
    assert settings_manifest()["requested_but_unread"] == {}


def test_resolved_values_come_from_the_accessors_not_a_second_env_read(monkeypatch):
    """The manifest asks the same functions the pilot asks.

    A re-read of the environment here would be a second reconstruction of a value
    the code already computed, and the two can disagree -- which is exactly how
    `seat_won` went wrong.
    """
    monkeypatch.setenv("MAGEBENCH_MULLIGAN", "model")
    monkeypatch.setenv("MAGEBENCH_CARD_TEXT", "none")
    m = settings_manifest()["resolved"]
    assert m["mulligan"] == "model"
    assert m["card_text"] == "none"


def test_an_invalid_setting_refuses_at_game_start(monkeypatch):
    """Earlier failure, not a new one: the accessor already refuses, here it does it first."""
    monkeypatch.setenv("MAGEBENCH_MULLIGAN", "sometimes")
    with pytest.raises(ValueError, match="MAGEBENCH_MULLIGAN"):
        settings_manifest()


def test_constants_that_shape_the_corpus_are_recorded(monkeypatch):
    """The completion reserve moved tonight and no artifact on either side says which it was."""
    assert settings_manifest()["constants"]["max_tokens"] == MAX_TOKENS


def test_an_absent_inline_setting_records_its_default_rather_than_nothing(monkeypatch):
    """`None` in a row is unreadable: it cannot say whether the default is on or off."""
    monkeypatch.delenv("MAGEBENCH_CHAT_PROMPTS", raising=False)
    chat = settings_manifest()["resolved"]["chat_prompts"]
    assert chat == {"requested": None, "default": "on unless set to 0"}
