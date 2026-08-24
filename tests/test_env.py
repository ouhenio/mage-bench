"""Tests for env reads that separate ABSENT from EMPTY."""

from __future__ import annotations

import pytest

from magebench.common.env import env_flag_dir, env_or_none


class TestAbsentAndEmptyAreNotTheSameAnswer:
    """`os.environ.get(NAME, "")` folds two different situations into one falsy value.

    Absent means the feature was not requested, which is a legitimate answer.
    Set-and-empty means it WAS requested and the value did not survive -- an
    unquoted expansion, a variable that resolved to nothing, a config that
    produced a blank. Under the old form both disabled the feature and neither
    said anything, which is how a run gets configured to record and records
    nothing.
    """

    def test_absent_is_none(self, monkeypatch):
        monkeypatch.delenv("MAGEBENCH_TEST_VAR", raising=False)

        assert env_or_none("MAGEBENCH_TEST_VAR") is None

    def test_a_value_comes_back_verbatim(self, monkeypatch):
        monkeypatch.setenv("MAGEBENCH_TEST_VAR", "1:250,8:5000")

        assert env_or_none("MAGEBENCH_TEST_VAR") == "1:250,8:5000"

    def test_set_but_empty_is_loud(self, monkeypatch):
        monkeypatch.setenv("MAGEBENCH_TEST_VAR", "")

        with pytest.raises(ValueError, match="set but empty"):
            env_or_none("MAGEBENCH_TEST_VAR")

    def test_whitespace_is_empty_too(self, monkeypatch):
        # VAR=" " out of a shell is the same accident with a space in it.
        monkeypatch.setenv("MAGEBENCH_TEST_VAR", "   ")

        with pytest.raises(ValueError, match="set but empty"):
            env_or_none("MAGEBENCH_TEST_VAR")

    def test_a_directory_switch_names_what_stops_working(self, monkeypatch):
        monkeypatch.setenv("MAGEBENCH_AI_RECORD_DIR", "")

        # The message has to say what was LOST, not only which variable was wrong:
        # the reader is looking at a run that produced no records and needs to know
        # those two facts are the same fact.
        with pytest.raises(ValueError, match="AiDecisionRecorder writes nothing"):
            env_flag_dir("MAGEBENCH_AI_RECORD_DIR", needed_by="AiDecisionRecorder")

    def test_a_directory_switch_still_allows_absent(self, monkeypatch):
        monkeypatch.delenv("MAGEBENCH_AI_RECORD_DIR", raising=False)

        assert env_flag_dir("MAGEBENCH_AI_RECORD_DIR", needed_by="AiDecisionRecorder") is None
