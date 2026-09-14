"""The manifest, and the one field that would have caught the incident.

GATE NOTE, because the count this file contributes to is not a fixed number.
`tests/weird/test_convention_exports.py` parametrises over game exports CHANGED
since master -- unless the diff touches `src/magebench/game/game-export-v*`, in
which case it validates EVERY recorded export instead. Touching the export schema
therefore takes this suite from ~1,561 collected to ~2,343, and the extra ~770 are
two tests each over ~385 real exports. A pass count from a branch that changed the
schema is not comparable with one from a branch that did not, and should not be:
the escalation is the guard doing its job. Quote the collected count beside the
verdict, and say what triggered it.


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
    m = settings_manifest(driver="pilot")
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
    m = settings_manifest(driver="pilot")
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
    assert settings_manifest(driver="pilot")["requested_but_unread"] == {}


def test_resolved_values_come_from_the_accessors_not_a_second_env_read(monkeypatch):
    """The manifest asks the same functions the pilot asks.

    A re-read of the environment here would be a second reconstruction of a value
    the code already computed, and the two can disagree -- which is exactly how
    `seat_won` went wrong.
    """
    monkeypatch.setenv("MAGEBENCH_MULLIGAN", "model")
    monkeypatch.setenv("MAGEBENCH_CARD_TEXT", "none")
    m = settings_manifest(driver="pilot")["resolved"]
    assert m["mulligan"] == "model"
    assert m["card_text"] == "none"


def test_an_invalid_setting_refuses_at_game_start(monkeypatch):
    """Earlier failure, not a new one: the accessor already refuses, here it does it first."""
    monkeypatch.setenv("MAGEBENCH_MULLIGAN", "sometimes")
    with pytest.raises(ValueError, match="MAGEBENCH_MULLIGAN"):
        settings_manifest(driver="pilot")


def test_constants_that_shape_the_corpus_are_recorded(monkeypatch):
    """The completion reserve moved tonight and no artifact on either side says which it was."""
    assert settings_manifest(driver="pilot")["constants"]["max_tokens"] == MAX_TOKENS


def test_an_absent_inline_setting_records_its_default_rather_than_nothing(monkeypatch):
    """`None` in a row is unreadable: it cannot say whether the default is on or off."""
    monkeypatch.delenv("MAGEBENCH_CHAT_PROMPTS", raising=False)
    chat = settings_manifest(driver="pilot")["resolved"]["chat_prompts"]
    assert chat == {"requested": None, "default": "on unless set to 0"}


def test_every_writer_names_its_driver_so_absent_means_one_thing():
    """The collision karn-engine found: replay emitted no manifest at all.

    A replayed game with no `settings` key is indistinguishable from a game
    generated before this module existed, and a reader cannot recover from that
    afterwards. `driver` is required rather than defaulted for the same reason: a
    default would let the next new writer inherit "pilot" silently, which is the
    same defect wearing the fix's clothes.
    """
    import inspect

    from magebench.pilot import settings_manifest as module

    sig = inspect.signature(module.settings_manifest)
    assert sig.parameters["driver"].default is inspect.Parameter.empty, (
        "driver must have no default"
    )
    assert sig.parameters["driver"].kind is inspect.Parameter.KEYWORD_ONLY

    for name in ("pilot", "replay"):
        assert settings_manifest(driver=name)["driver"] == name


def test_the_replay_path_emits_a_manifest():
    """Read from the source, because the assertion is about a call site existing.

    A unit test of replay.py would need a live bridge; what has to hold is that the
    path writes a manifest at all, which is exactly what was missing.
    """
    from pathlib import Path as _Path

    src = _Path(module_path()).read_text()
    assert 'settings_manifest(driver="replay")' in src
    assert "settings=manifest" in src


def module_path() -> str:
    from magebench.pilot import replay

    return replay.__file__


# ---------------------------------------------------------------------------
# opponent_log_delta, folded in from s/run-b-merge's top-level game_start key.
# ---------------------------------------------------------------------------


def test_the_log_delta_setting_is_resolved_and_not_reported_unread(monkeypatch):
    """Both directions, and the unread field is the one that matters.

    Before the registration, `MAGEBENCH_OPPONENT_LOG_DELTA` landed in
    `requested_but_unread` -- correctly, since nothing in the registry claimed it. The
    assertion that it is now ABSENT from that dict is a zero, so the companion test below
    shows the same check returning non-zero.
    """
    from magebench.pilot.log_delta import SETTING_NAME

    monkeypatch.setenv("MAGEBENCH_OPPONENT_LOG_DELTA", "1")
    manifest = settings_manifest(driver="pilot")
    assert manifest["resolved"][SETTING_NAME] is True
    assert "MAGEBENCH_OPPONENT_LOG_DELTA" not in manifest["requested_but_unread"]

    monkeypatch.delenv("MAGEBENCH_OPPONENT_LOG_DELTA")
    assert settings_manifest(driver="pilot")["resolved"][SETTING_NAME] is False


def test_an_unregistered_setting_is_still_reported_unread(monkeypatch):
    """The positive control for the test above: this field can still return its bad state."""
    monkeypatch.setenv("MAGEBENCH_NOT_A_REAL_SETTING", "1")
    manifest = settings_manifest(driver="pilot")
    assert manifest["requested_but_unread"]["MAGEBENCH_NOT_A_REAL_SETTING"] == "1"


def test_the_manifest_emits_nothing_while_resolving(monkeypatch, caplog):
    """An accessor this registry calls must be pure enough to call twice.

    `log_delta.enabled()` used to print its provenance line, and registering it would have
    made the manifest -- whose job is to observe -- the apparent cause of a log line at game
    start. The general rule is what this pins: calling the manifest resolves values and emits
    nothing, so the next accessor someone registers cannot smuggle a side effect into
    game_start. `log_delta.announce()` is where the line lives, and the pilot loop calls it at
    the first real use.
    """
    import logging

    from magebench.pilot import log_delta

    monkeypatch.setenv("MAGEBENCH_OPPONENT_LOG_DELTA", "1")
    with caplog.at_level(logging.DEBUG):
        settings_manifest(driver="pilot")
        settings_manifest(driver="pilot")
    assert [r.getMessage() for r in caplog.records if "log_delta" in r.getMessage()] == []

    # And the announcement still happens, exactly once -- otherwise "emits nothing" would be
    # satisfied by a line nobody prints at all.
    log_delta._announced = False
    with caplog.at_level(logging.INFO):
        log_delta.announce()
        log_delta.announce()
    lines = [r.getMessage() for r in caplog.records if "opponent log delta" in r.getMessage()]
    assert len(lines) == 1 and "ON (EXPLICIT" in lines[0]


# ---------------------------------------------------------------------------
# Reading the flag back out, across the shape change.
# ---------------------------------------------------------------------------


def test_the_treatment_flag_reads_from_either_shape():
    from magebench.pilot.log_delta import SETTING_NAME, setting_from_game_start

    assert setting_from_game_start({SETTING_NAME: True}) is True
    assert setting_from_game_start({SETTING_NAME: False}) is False
    assert setting_from_game_start({"settings": {"resolved": {SETTING_NAME: True}}}) is True
    assert setting_from_game_start({"settings": {"resolved": {SETTING_NAME: False}}}) is False
    # Both shapes agreeing is fine -- it is one game in one arm, said twice.
    assert setting_from_game_start(
        {SETTING_NAME: True, "settings": {"resolved": {SETTING_NAME: True}}}
    ) is True


def test_an_absent_flag_raises_rather_than_reading_as_off():
    """The defect this reader exists for, asserted rather than described.

    Absent must not be spelled the same way as False. Each of these rows is a distinct way of
    failing to say which arm the game was in, and every one of them has to raise -- the
    partial-manifest cases especially, because a `.get` chain would turn them into False
    without ever touching a missing key.
    """
    import pytest as _pytest

    from magebench.pilot.log_delta import setting_from_game_start

    for row in (
        {},                                             # predates the flag entirely
        {"model": "x"},                                 # a real row, other keys only
        {"settings": {}},                               # manifest present, no resolved
        {"settings": {"resolved": {}}},                 # resolved present, key absent
        {"settings": {"resolved": {"mulligan": "on"}}},  # other settings resolved, not this one
        {"settings": None},                             # manifest key present and null
    ):
        with _pytest.raises(KeyError, match="opponent_log_delta"):
            setting_from_game_start(row)


def test_a_row_that_says_both_arms_raises():
    import pytest as _pytest

    from magebench.pilot.log_delta import SETTING_NAME, setting_from_game_start

    with _pytest.raises(ValueError, match="disagrees between shapes"):
        setting_from_game_start(
            {SETTING_NAME: True, "settings": {"resolved": {SETTING_NAME: False}}}
        )


def test_nothing_else_names_the_flag_by_hand():
    """The reader is only a fix if it is the ONLY way in.

    `setting_from_game_start` is currently uncalled -- run B's readout does not exist yet --
    and an available-but-unused function is the shape that fired zero times in 2,012 decisions
    once already. This test is what makes it load-bearing before it has a caller: any future
    census that reaches for the literal instead gets caught here, in the module that owns the
    name, rather than shipping a `.get` chain that reads absent as off.

    Scoped to the literal in CODE, so the comments explaining the shape change are allowed to
    say the name.
    """
    import ast
    from pathlib import Path

    from magebench.pilot import log_delta

    owner = Path(log_delta.__file__).resolve()
    roots = [owner.parents[2], owner.parents[3] / "tools"]
    offenders = []
    for root in roots:
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*.py")):
            if path.resolve() == owner:
                continue
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and node.value == "opponent_log_delta":
                    offenders.append(f"{path}:{node.lineno}")
    assert not offenders, (
        "These name the treatment flag by literal instead of going through "
        "log_delta.setting_from_game_start / SETTING_NAME:\n  " + "\n  ".join(offenders)
    )
