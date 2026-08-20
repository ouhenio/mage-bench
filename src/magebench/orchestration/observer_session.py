"""One observer JVM, many games: the keepAlive spectator as a reusable session.

WHY THIS EXISTS
---------------
A synth game measured 35s end to end, and 28.9s of that was a server JVM
starting up. From a live run's own server.log:

    Loading database...                 2.7s
    Loading cards...                   25.1s   (87,765 card implementations)
    Started MAGE server - listening      1.1s

25.1s of a 35s game -- 72% -- was one line, and it is per-JVM static
initialisation that does not care how many games the JVM then hosts. Every game
paid it because every game was its own `--games 1` orchestrator invocation with
its own server and its own Swing spectator: two JVMs per game, 104 alive at once
for 52 concurrent games, and 500 card loads for a 500-game corpus.

The observer already knew how to do better. `xmage.observer.keepAlive` is a
stdin-driven command loop that creates one table per JSON line and cleans up
between games -- but it was reachable only from tests/conftest.py, so production
never used it. This module is that machinery moved to where the orchestrator can
reach it.

WHAT MADE IT UNUSABLE FOR REAL BATCHES UNTIL NOW
------------------------------------------------
The seed. It arrived as -Dxmage.game.seed, a JVM property, so a persistent
server dealt every game in the batch the same hand -- which is worse than slow,
because a corpus of identical deals looks like a corpus. GameOptions.gameSeed
now carries it per game and this session passes it per command.

The remaining constraint is real and unchanged: RandomUtil.random is a
process-global static, so games running CONCURRENTLY in one JVM would interleave
their draws. Sequential games are sound, and measured rather than assumed --
re-seeding after 50,000 intervening draws reproduces the shuffle exactly, and a
different seed still differs. Run one session per concurrency slot, games
sequential within a session.
"""

from __future__ import annotations

import io
import json
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from magebench.common.log import get_logger
from magebench.common.process_manager import jvm_oom_preexec_fn
from magebench.orchestration.game_processes import MVN_REPO_ARGS

logger = get_logger(__name__)

MAIN_CLASS_OBSERVER = "mage.client.observer.ObserverMain"
OBSERVER_READY_TIMEOUT_SECONDS = 240

_classpath_cache: dict[str, str] = {}
_reactor_module_cache: dict[Path, dict[str, Path]] = {}


def _find_reactor_modules(project_root: Path) -> dict[str, Path]:
    """Map artifactId -> target/classes Path for all reactor modules.

    Walks the Maven reactor by following ``<module>`` declarations in pom.xml.
    Only includes modules that have a compiled ``target/classes`` directory.
    """
    if project_root in _reactor_module_cache:
        return _reactor_module_cache[project_root]

    modules: dict[str, Path] = {}

    def _scan(parent_dir: Path) -> None:
        pom = parent_dir / "pom.xml"
        if not pom.exists():
            return
        content = pom.read_text()
        parent_end = content.find("</parent>")
        search_text = content[parent_end:] if parent_end >= 0 else content
        m = re.search(r"<artifactId>([^<]+)</artifactId>", search_text)
        if m:
            classes_dir = parent_dir / "target" / "classes"
            if classes_dir.is_dir():
                modules[m.group(1)] = classes_dir
        for child in re.findall(r"<module>([^<]+)</module>", content):
            _scan(parent_dir / child)

    _scan(project_root)
    _reactor_module_cache[project_root] = modules
    return modules


def _replace_reactor_jars(dep_classpath: str, project_root: Path) -> str:
    """Swap installed org.mage JARs for the reactor's own target/classes.

    A jar in the local repository is whatever was last installed, which is not
    necessarily what this worktree compiled. Preferring target/classes is what
    keeps a build from being compiled, installed, and never loaded.
    """
    reactor = _find_reactor_modules(project_root)
    if not reactor:
        return dep_classpath

    resolved: list[str] = []
    for entry in dep_classpath.split(":"):
        replaced = False
        for artifact_id, classes_dir in reactor.items():
            if entry.endswith(".jar") and f"/org/mage/{artifact_id}/" in entry:
                resolved.append(str(classes_dir))
                replaced = True
                break
        if not replaced:
            resolved.append(entry)
    return ":".join(resolved)


def compute_module_classpath(project_root: Path, module: str) -> str:
    """Compute a module's Java classpath once per process, then cache it.

    Resolved from the repository the runtime loads from, not whichever one mvn
    defaults to: _replace_reactor_jars neutralises stale org.mage jars, but
    every THIRD-PARTY entry would otherwise come from ~/.m2 while the game JVMs
    resolve from MAVEN_REPO_LOCAL.
    """
    if module in _classpath_cache:
        return _classpath_cache[module]
    module_dir = project_root / module
    cp_file = module_dir / "target" / "classpath.txt"
    result = subprocess.run(
        ["mvn", "-q", *MVN_REPO_ARGS, "dependency:build-classpath", f"-Dmdep.outputFile={cp_file}"],
        cwd=module_dir,
        capture_output=True,
        text=True,
        preexec_fn=jvm_oom_preexec_fn(),
    )
    assert result.returncode == 0, f"Failed to compute classpath for {module}: {result.stderr}"
    dep_classpath = _replace_reactor_jars(cp_file.read_text().strip(), project_root)
    classpath = f"{module_dir / 'target' / 'classes'}:{dep_classpath}"
    _classpath_cache[module] = classpath
    return classpath


def build_java_cmd(
    classpath: str,
    main_class: str,
    system_props: dict[str, str],
    *,
    max_heap: str | None = None,
    max_metaspace: str | None = None,
) -> list[str]:
    """Build a ``java -cp`` command with JVM flags and system properties.

    Deliberately not ``mvn exec:java``: maven's own startup is paid on every
    launch, and the whole point of this module is to stop paying per-game
    startup costs.
    """
    jvm_flags = ["--add-opens=java.base/java.io=ALL-UNNAMED"]
    if max_heap is not None:
        jvm_flags.append(f"-Xmx{max_heap}")
    if max_metaspace is not None:
        jvm_flags.append(f"-XX:MaxMetaspaceSize={max_metaspace}")
    if sys.platform == "darwin":
        jvm_flags.append("-Dapple.awt.UIElement=true")
    cmd = ["java", *jvm_flags]
    for k, v in system_props.items():
        cmd.append(f"-D{k}={v}")
    cmd.extend(["-cp", classpath, main_class])
    return cmd


def wrap_with_xvfb(cmd: list[str]) -> list[str]:
    """Run a client JVM on an isolated virtual display on Linux."""
    if sys.platform != "linux":
        return cmd
    xvfb = shutil.which("xvfb-run")
    assert xvfb is not None, (
        "The observer JVM needs xvfb-run on Linux for an isolated display. "
        "Install xvfb (e.g. apt-get install xvfb or dnf install xorg-x11-server-Xvfb)."
    )
    return [xvfb, "--auto-servernum", "--server-args=-screen 0 1920x1080x24", *cmd]


def read_health_port_file(path: Path, timeout: float = 30.0) -> int:
    """Poll for the port file the observer writes after binding its health server."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            text = path.read_text().strip()
            if text:
                return int(text)
        except FileNotFoundError:
            pass
        time.sleep(0.1)
    raise RuntimeError(f"Health port file {path} was not written within {timeout}s")


def _poll(port: int, path: str, body: dict | None, timeout: int, expect: str) -> dict:
    """Long-poll one observer health endpoint and return its decoded response."""
    url = f"http://127.0.0.1:{port}/{path}"
    if body is None:
        req = urllib.request.Request(f"{url}?timeout={timeout}")
    else:
        req = urllib.request.Request(
            url,
            data=json.dumps({**body, "timeout": timeout}).encode(),
            headers={"Content-Type": "application/json"},
        )
    try:
        with urllib.request.urlopen(req, timeout=timeout + 5) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Observer /{path} failed (HTTP {e.code}): {e.read().decode()}") from e
    value = data.get(expect)
    # "status" endpoints answer with a word, the rest with a boolean. Both are
    # falsy when they mean not-ready, but a status of "starting" is truthy and
    # must not be read as success.
    if not value or (expect == "status" and value != "ready"):
        raise RuntimeError(f"Observer /{path} returned: {data}")
    return data


class ObserverSession:
    """A persistent observer JVM that plays games one after another on one server.

    Each ``start_game`` writes a single JSON line to the JVM's stdin; the
    observer creates the table, seats the players, starts the match and watches
    it. The card database, the class loading and the lobby connection are paid
    once for the whole sequence rather than once per game.
    """

    def __init__(
        self,
        proc: subprocess.Popen,
        log_path: Path,
        *,
        health_port: int,
        label: str = "observer",
    ) -> None:
        self.proc = proc
        self.log_path = log_path
        self.health_port = health_port
        self.label = label
        assert proc.stdin is not None, "ObserverSession requires stdin=PIPE"
        self._stdin = io.TextIOWrapper(proc.stdin, encoding="utf-8", line_buffering=True)

    def wait_until_ready(self, timeout: int = 180) -> None:
        """Block until this observer can actually accept a game command.

        THE PORT FILE IS NOT READINESS. ObserverMain binds the health endpoint
        BEFORE the MageFrame cold start, deliberately, so that the harness can
        long-poll during startup -- which means the port file appears while the
        JVM is still connecting to the lobby. Sending a command at that point
        gets you a SessionHandler.createTable that returns null and an NPE from
        inside the command loop, which the loop logs and swallows, so the game
        never starts and the failure surfaces four minutes later as a readiness
        timeout on a completely different call.

        Two waits, not one: /wait-for-commands says the stdin loop is up,
        /health says the lobby connection finished. The first without the second
        is exactly the window that produced the null table.
        """
        _poll(self.health_port, "wait-for-commands", None, timeout, "status")
        _poll(self.health_port, "health", None, timeout, "status")

    def start_game(
        self,
        game_dir: Path,
        players_config: dict,
        *,
        choosing_player: str | None = None,
        game_seed: int | None = None,
        skip_init_shuffling: bool = False,
        wins_needed: int = 1,
    ) -> None:
        """Ask the observer to create and start one game."""
        cmd: dict[str, object] = {
            "gameDir": str(game_dir),
            "playersConfig": players_config,
            "skipInitShuffling": skip_init_shuffling,
            "winsNeeded": wins_needed,
        }
        if choosing_player is not None:
            cmd["choosingPlayer"] = choosing_player
        # Omitted rather than sent as null when unseeded. An absent seed leaves
        # the RNG stream alone; a seed of 0 is a legal seed, so the two must not
        # collapse into one representation.
        if game_seed is not None:
            cmd["gameSeed"] = int(game_seed)
        self._stdin.write(json.dumps(cmd, separators=(",", ":")) + "\n")
        self._stdin.flush()

    def wait_for_ready(self, game_dir: Path, timeout: int = OBSERVER_READY_TIMEOUT_SECONDS) -> str:
        """Block until the table exists, and return its id."""
        return _poll(self.health_port, "wait-for-ready", {"gameDir": str(game_dir)}, timeout, "ready")["tableId"]

    def wait_for_watching(self, game_dir: Path, timeout: int = OBSERVER_READY_TIMEOUT_SECONDS) -> None:
        """Block until the observer is attached to the game itself, not just the table."""
        _poll(self.health_port, "wait-for-watching", {"gameDir": str(game_dir)}, timeout, "watching")

    def wait_for_game_end(self, game_dir: Path, timeout: int = 1800) -> None:
        """Block until this game's event files are written and closed.

        Scoped to game_dir on purpose. A completion probe that answers "has ANY
        game finished" is the same defect as a log directory keyed to the
        second: it returns a confident answer to a question adjacent to the one
        asked, and a batch built on it kills its own games.
        """
        _poll(self.health_port, "wait-for-game-end", {"gameDir": str(game_dir)}, timeout, "done")

    def close(self) -> None:
        """Close stdin, which is how the observer JVM is asked to exit."""
        try:
            self._stdin.close()
        except (OSError, ValueError):
            pass
