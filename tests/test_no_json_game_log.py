"""XMage's per-game JSON transcript must be off, and the proof is Java reading it back.

SessionImpl.appendJsonLog writes every action to `gamelogsJson/game-<id>.json` when
`gameLogJsonAutoSave` is true, and SessionHandler reads that preference with a default of
"true". Nothing in this codebase reads the output: there is no deserialiser for ActionData
and no reader of that path. Measured cost before this: 18 GB on lascar, 23 GB on ranokau,
9,999 files, single games up to 111 MB -- and a census found 9,896 of them were the engine
playing itself with no LLM pilot involved.

WHY THE TEST SHELLS OUT TO JAVA. The first version of this fix wrote
<userRoot>/mage/client/prefs.xml, which is what the property name suggests and what a
python-side assertion on the XML would have confirmed. Java IGNORED it in complete
silence -- FileSystemPreferences keeps its tree in `.java/.userPrefs` UNDER the userRoot,
so it created its own empty tree beside the seeded one and read the "true" default. The
fix looked applied and changed nothing. Only asking Java caught that.
"""
import os
import pathlib
import shutil
import subprocess

import pytest

from magebench.orchestration.game_processes import prefs_isolation_args

_READER = """
import java.util.prefs.Preferences;
public class ReadPref {
    public static void main(String[] a) {
        // Exactly what SessionHandler does: MageFrame.getPreferences() is
        // userNodeForPackage(MageFrame.class) -- package mage.client -- and the read
        // defaults to "true".
        Preferences p = Preferences.userRoot().node("mage/client");
        System.out.println(p.get("gameLogJsonAutoSave", "true"));
    }
}
"""


def _jdk():
    home = os.environ.get("JAVA_HOME")
    if home and (pathlib.Path(home) / "bin" / "javac").exists():
        return pathlib.Path(home) / "bin"
    javac = shutil.which("javac")
    return pathlib.Path(javac).parent if javac else None


@pytest.fixture(scope="module")
def reader(tmp_path_factory):
    bindir = _jdk()
    if bindir is None:
        pytest.skip("no JDK on PATH or at JAVA_HOME; this test asks Java, not python")
    out = tmp_path_factory.mktemp("reader")
    (out / "ReadPref.java").write_text(_READER)
    subprocess.run([str(bindir / "javac"), "-d", str(out), str(out / "ReadPref.java")],
                   check=True, capture_output=True)
    return bindir / "java", out


def _read_back(reader, user_root: pathlib.Path) -> str:
    java, classes = reader
    proc = subprocess.run(
        [str(java), f"-Djava.util.prefs.userRoot={user_root}",
         f"-Djava.util.prefs.systemRoot={user_root}", "-cp", str(classes), "ReadPref"],
        capture_output=True, text=True, check=True)
    return proc.stdout.strip().splitlines()[-1]


def test_a_seeded_game_dir_reads_FALSE(reader, tmp_path):
    game_dir = tmp_path / "g00"
    game_dir.mkdir()
    prefs_isolation_args(game_dir)
    assert _read_back(reader, game_dir / "prefs") == "false"


def test_an_UNSEEDED_tree_reads_true(reader, tmp_path):
    """The negative control, and it is the status quo this fix replaces.

    Without it, 'the seeded tree reads false' is compatible with Java reading false for
    some unrelated reason -- and it would not show that the isolation which gives every
    game a FRESH tree is precisely what guarantees the dump is on.
    """
    empty = tmp_path / "empty"
    empty.mkdir()
    assert _read_back(reader, empty) == "true"


def test_the_seed_lands_where_JAVA_looks(tmp_path):
    """Pins the layout that the first version of this fix got wrong.

    `java.util.prefs.userRoot` names a directory and FileSystemPreferences puts its tree
    in `.java/.userPrefs` UNDER it, not at it. A path assertion alone would not have
    caught the original mistake -- the Java read-back above is what did -- but once known,
    it is worth failing fast and by name if a future refactor moves it.
    """
    game_dir = tmp_path / "g01"
    game_dir.mkdir()
    prefs_isolation_args(game_dir)
    seeded = game_dir / "prefs" / ".java" / ".userPrefs" / "mage" / "client" / "prefs.xml"
    assert seeded.is_file(), "the seed is not where FileSystemPreferences reads"
    assert "gameLogJsonAutoSave" in seeded.read_text()


def test_prefs_isolation_still_returns_the_two_properties(tmp_path):
    """The non-regression: this function's original job is the per-game tree that removed
    a shared file lock at 20-way concurrency. Seeding must not have changed that."""
    game_dir = tmp_path / "g02"
    game_dir.mkdir()
    args = prefs_isolation_args(game_dir)
    assert any(a.startswith("-Djava.util.prefs.userRoot=") for a in args)
    assert any(a.startswith("-Djava.util.prefs.systemRoot=") for a in args)
