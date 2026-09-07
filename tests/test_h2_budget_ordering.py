"""Two limits with a required ordering, set in two languages, must not drift apart.

The launcher gave up at 240s while the Java h2 retry budget faced a contention window
measured at 261s. Raising either alone fixes nothing: a deepened retry is killed from
outside before it can win, and a raised ceiling just waits longer for a retry that has
already given up. Corpus job 3135 lost a 48-game cohort to exactly that pair, and the two
numbers lived in different files in different languages with nothing relating them.

So the ordering is enforced where both are visible: the ceiling is DERIVED from the budget
when unset -- consistent by construction rather than by anyone remembering -- and CHECKED
when set.
"""
import pytest

from magebench.orchestration.config import Config


def test_the_ceiling_is_derived_when_unset(monkeypatch):
    monkeypatch.delenv("MAGEBENCH_H2_RETRY_BUDGET_MS", raising=False)
    cfg = Config()
    assert cfg.resolved_server_wait() == 600 + Config.SERVER_WAIT_MARGIN_S


def test_the_ceiling_MOVES_WITH_the_budget(monkeypatch):
    """The property a pair of constants does not have, and the whole point of deriving:
    change one and the other follows, with nothing to remember."""
    monkeypatch.setenv("MAGEBENCH_H2_RETRY_BUDGET_MS", "60000")
    assert Config().resolved_server_wait() == 60 + Config.SERVER_WAIT_MARGIN_S
    monkeypatch.setenv("MAGEBENCH_H2_RETRY_BUDGET_MS", "900000")
    assert Config().resolved_server_wait() == 900 + Config.SERVER_WAIT_MARGIN_S


def test_an_explicit_ceiling_below_the_budget_is_REFUSED(monkeypatch):
    """This is the 240-against-261 pair, reproduced. It must not be constructible."""
    monkeypatch.setenv("MAGEBENCH_H2_RETRY_BUDGET_MS", "261000")
    with pytest.raises(ValueError, match="below the h2 retry budget"):
        Config(server_wait=240).resolved_server_wait()


def test_the_refusal_names_both_numbers_and_the_way_out(monkeypatch):
    monkeypatch.setenv("MAGEBENCH_H2_RETRY_BUDGET_MS", "261000")
    with pytest.raises(ValueError) as e:
        Config(server_wait=240).resolved_server_wait()
    msg = str(e.value)
    assert "240" in msg and "261" in msg
    assert "MAGEBENCH_H2_RETRY_BUDGET_MS" in msg, "must name the other half, not only itself"


def test_an_explicit_ceiling_above_the_budget_is_honoured(monkeypatch):
    """The good state, not merely the absence of the bad one: an operator who sets a
    generous ceiling deliberately keeps it rather than having it overwritten."""
    monkeypatch.setenv("MAGEBENCH_H2_RETRY_BUDGET_MS", "60000")
    assert Config(server_wait=999).resolved_server_wait() == 999


def test_a_malformed_budget_raises_rather_than_defaulting(monkeypatch):
    monkeypatch.setenv("MAGEBENCH_H2_RETRY_BUDGET_MS", "banana")
    with pytest.raises(ValueError):
        Config.h2_retry_budget_s()


def test_the_python_default_matches_the_java_default():
    """The duplication across the language boundary, pinned. There is no way to import
    DatabaseUtils.DEFAULT_RETRY_BUDGET_MS from here, so this reads the Java source: if
    someone changes one side, this fails instead of the two drifting silently."""
    import pathlib
    import re
    java = pathlib.Path(__file__).resolve().parents[1] / (
        "Mage/src/main/java/mage/cards/repository/DatabaseUtils.java")
    m = re.search(r"DEFAULT_RETRY_BUDGET_MS\s*=\s*([0-9_]+)L", java.read_text())
    assert m, "could not find DEFAULT_RETRY_BUDGET_MS in DatabaseUtils.java"
    assert int(m.group(1).replace("_", "")) == Config.H2_RETRY_BUDGET_DEFAULT_MS
