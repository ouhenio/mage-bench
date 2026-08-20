"""Play M games on one server JVM instead of M servers on one game each.

THE MEASUREMENT THIS EXISTS FOR
-------------------------------
A synth game took 35s end to end and 28.9s of it was startup. From a live run's
own server.log, one game, tonight:

    19:29:09.557  Loading database...
    19:29:12.280   - cards: 87765                    2.7s
    19:29:12.284  Loading cards...
    19:29:37.372  Config - max seconds idle         25.1s
    19:29:38.494  Started MAGE server - listening    1.1s

Play was the remaining ~6s. The 25.1s is CardScanner.scan() over 87,765 card
implementations: per-JVM static initialisation, indifferent to how many games
the JVM then hosts. Paying it per game made a 500-game corpus pay it 500 times.

At M games per JVM the arithmetic is 25.1/M + ~6s per game, so M=10 is ~8.5s
against ~31s: about 3.5x on the same cores, with no change to how a game is
played.

WHY IT WAS NOT ALREADY DONE
---------------------------
Two reasons, and only one of them was in the harness.

The seed was a JVM property. -Dxmage.game.seed is fixed for the life of a
process, so a persistent server dealt every game in the batch the same hand.
That is worse than slow: a corpus of one deal repeated looks like a corpus.
GameOptions.gameSeed now carries it per game, and this runner supplies one seed
per game.

The spectator looked structural. batch_coordination starts one unconditionally
and `--observer` only chooses which kind, so a second JVM per game read as
something you could not turn off. It is not needed for event capture --
ServerGameEventLogCollector is server-side and writes server_game_events.jsonl
from game.getOptions().gameLogDir, while the observer only polls that file --
but it IS what creates the table. So it is kept and amortised rather than
deleted: one observer for the whole sequence, not one per game.

THE CONSTRAINT THAT REMAINS
---------------------------
RandomUtil.random is a process-global static. Games running CONCURRENTLY in one
JVM would interleave their draws and neither would be reproducible. This runner
is therefore strictly sequential within a session; run several sessions in
parallel for concurrency. Sequential reuse is sound and was measured rather than
assumed: re-seeding after 50,000 intervening draws reproduces the shuffle
exactly, after an odd offset too, and a different seed still differs.
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from magebench.common.log import get_logger
from magebench.common.port import find_available_port, wait_for_port
from magebench.common.process_manager import ProcessManager, jvm_oom_preexec_fn, kill_tree
from magebench.orchestration.batch_coordination import claim_game_dir, claim_run_file
from magebench.orchestration.config import Config
from magebench.orchestration.game_finalization import write_game_meta
from magebench.orchestration.observer_session import (
    MAIN_CLASS_OBSERVER,
    ObserverSession,
    build_java_cmd,
    compute_module_classpath,
    read_health_port_file,
    wrap_with_xvfb,
)
from magebench.orchestration.xml_config import modify_server_config

logger = get_logger(__name__)

MAIN_CLASS_SERVER = "mage.server.Main"

# Ports start here (Config.start_port), and each session's X display is offset
# from its own port so that two sessions can never land on one display.
_PORT_BASE = 17171
_DISPLAY_BASE = 200


@dataclass
class SequentialBatchResult:
    """What a session actually produced, kept separate from what it attempted."""

    completed: list[Path] = field(default_factory=list)
    failed: list[tuple[Path, str]] = field(default_factory=list)

    @property
    def attempted(self) -> int:
        return len(self.completed) + len(self.failed)


def _start_server(
    pm: ProcessManager,
    project_root: Path,
    config: Config,
    server_config_path: Path,
    server_log: Path,
    port: int,
    ai_record_dir: Path | None,
) -> subprocess.Popen:
    """Start one headless server JVM directly, without going through maven.

    `mvn exec:java` pays maven's own startup on every launch. That is tolerable
    once per game only because the game was already paying 25s for the card
    load; in a runner whose entire purpose is to stop paying startup per game it
    would be the next thing in the way.
    """
    classpath = compute_module_classpath(project_root, "Mage.Server")
    cmd = build_java_cmd(
        classpath,
        MAIN_CLASS_SERVER,
        {
            "java.awt.headless": "true",
            "xmage.config.path": str(server_config_path),
            # The recorder's ENABLE switch. Where it writes is a separate
            # question and not this property's business any more: it resolves
            # game.getOptions().gameLogDir first, so records land in each game's
            # own directory next to server_game_events.jsonl. This directory is
            # the fallback, and in a session it would collect every game's
            # records in one file.
            **({"xmage.ai.recordDir": str(ai_record_dir)} if ai_record_dir else {}),
        },
        max_heap="1024m",
    )
    env = os.environ.copy()
    env.update(
        {
            "XMAGE_AI_PUPPETEER": "1",
            "XMAGE_AI_PUPPETEER_USER": config.user,
            "XMAGE_AI_PUPPETEER_PASSWORD": config.password,
            "XMAGE_AI_PUPPETEER_SERVER": config.server,
            "XMAGE_AI_PUPPETEER_PORT": str(port),
            "XMAGE_AI_PUPPETEER_DISABLE_WHATS_NEW": "1",
        }
    )
    # start_jvm_process, not start_process: it biases Linux OOM kills toward the
    # JVM rather than the python driver, which is what you want when the driver
    # is the thing holding the batch together.
    return pm.start_jvm_process(
        args=cmd,
        cwd=project_root / "Mage.Server",
        env=env,
        log_file=server_log,
    )


def _start_observer(
    project_root: Path,
    config: Config,
    port: int,
    observer_log: Path,
    health_port_file: Path,
) -> ObserverSession:
    """Start the one keepAlive observer this session will drive."""
    health_port_file.unlink(missing_ok=True)
    classpath = compute_module_classpath(project_root, "Mage.Client.Observer")
    props = {
        "xmage.aiPuppeteer.autoConnect": "true",
        "xmage.aiPuppeteer.disableWhatsNew": "true",
        # noWindow, because nobody watches a synth game. keepAlive, because the
        # whole point is that this JVM outlives any one game.
        "xmage.observer.noWindow": "true",
        "xmage.observer.keepAlive": "true",
        "xmage.observer.healthPort": "20000",
        "xmage.observer.healthPortFile": str(health_port_file),
        "xmage.aiPuppeteer.server": config.server,
        "xmage.aiPuppeteer.port": str(port),
        "xmage.aiPuppeteer.user": config.user,
        "xmage.aiPuppeteer.password": config.password,
        # A shared java.util.prefs tree serialises every client shutdown behind
        # one file lock. One tree per session rather than per game, since the
        # session is now the process.
        "java.util.prefs.userRoot": str(observer_log.parent / "prefs"),
        "java.util.prefs.systemRoot": str(observer_log.parent / "prefs"),
    }
    # Absent and empty both mean "not set", and neither may become a default.
    # The property is JVM-wide, so it applies to every game in the session; a
    # per-player "skill" in the config overrides it per game, which is where
    # skill should be expressed for a batch.
    ai_skills = os.environ.get("MAGEBENCH_AI_SKILLS")
    if ai_skills is not None and ai_skills != "":
        props["xmage.ai.skills"] = ai_skills
    (observer_log.parent / "prefs").mkdir(parents=True, exist_ok=True)

    # Display derived from the port, because the port came from a flock-held
    # reservation and the display allocator has no reservation of its own.
    # Without this, eight sessions starting together race for one server number
    # and most of them die before they draw anything.
    display = _DISPLAY_BASE + (port - _PORT_BASE)
    cmd = wrap_with_xvfb(
        build_java_cmd(classpath, MAIN_CLASS_OBSERVER, props, max_heap="1536m"), display
    )
    env = os.environ.copy()
    env.update(
        {
            "XMAGE_AI_PUPPETEER": "1",
            "XMAGE_AI_PUPPETEER_USER": config.user,
            "XMAGE_AI_PUPPETEER_PASSWORD": config.password,
            "XMAGE_AI_PUPPETEER_SERVER": config.server,
            "XMAGE_AI_PUPPETEER_PORT": str(port),
            "XMAGE_AI_PUPPETEER_DISABLE_WHATS_NEW": "1",
        }
    )
    log_fh = open(observer_log, "w")
    proc = subprocess.Popen(
        cmd,
        cwd=project_root / "Mage.Client.Observer",
        stdin=subprocess.PIPE,
        stdout=log_fh,
        stderr=subprocess.STDOUT,
        env=env,
        preexec_fn=jvm_oom_preexec_fn(),
    )
    health_port = read_health_port_file(health_port_file, timeout=180)
    logger.info("Observer health server on port %d", health_port)
    session = ObserverSession(proc, observer_log, health_port=health_port)
    session.wait_until_ready()
    logger.info("Observer ready for commands")
    return session


def run_sequential_batch(
    config: Config,
    project_root: Path,
    log_dir: Path,
    seeds: list[int | None],
    *,
    pm: ProcessManager,
    game_timeout: int = 1800,
) -> SequentialBatchResult:
    """Play one game per entry in `seeds`, all on one server and one observer.

    `seeds` is the game list, not a parallel array to one: its length IS the
    number of games, and an entry of None means that game is unseeded. Passing
    seeds explicitly rather than deriving them from a base and an index keeps
    the caller's provenance -- a resumed batch, a fixed pairing, a re-run of one
    bad game -- visible here instead of reconstructed.
    """
    result = SequentialBatchResult()
    assert config.timestamp, (
        "config.timestamp must be set before a sequential batch: it keys the "
        "session directory and every game directory in it"
    )
    players_config_json = config.get_players_config_json()
    assert players_config_json, "Sequential batch needs a resolved players config"
    players_config = json.loads(players_config_json)

    port_reservation = find_available_port(config.start_port)
    port = port_reservation.port
    config.port = port

    session_dir = log_dir / f"session_{config.timestamp}"
    session_dir.mkdir(parents=True, exist_ok=True)
    server_config_path = claim_run_file(session_dir, "server_config", ".xml")
    server_log = claim_run_file(session_dir, "server", ".log")
    modify_server_config(
        source=project_root / "Mage.Server" / "config" / "config.xml",
        destination=server_config_path,
        port=port,
    )

    logger.info("Starting one server for %d games on port %d", len(seeds), port)
    ai_record_dir_env = os.environ.get("MAGEBENCH_AI_RECORD_DIR")
    ai_record_dir = None
    if ai_record_dir_env is not None and ai_record_dir_env != "":
        ai_record_dir = Path(ai_record_dir_env)
        ai_record_dir.mkdir(parents=True, exist_ok=True)
    server_proc = _start_server(
        pm, project_root, config, server_config_path, server_log, port, ai_record_dir
    )
    if not wait_for_port(config.server, port, config.server_wait):
        kill_tree(server_proc.pid)
        port_reservation.release()
        raise RuntimeError(f"Server failed to start within {config.server_wait}s; see {server_log}")
    port_reservation.release()
    logger.info("Server ready. The card load is now paid for; every game below reuses it.")

    observer = _start_observer(
        project_root, config, port, session_dir / "observer.log", session_dir / "health_port"
    )
    try:
        for index, seed in enumerate(seeds):
            game_dir = claim_game_dir(log_dir, config.timestamp, f"_s{index + 1}")
            logger.info(
                "Game %d/%d -> %s (seed=%s)", index + 1, len(seeds), game_dir.name,
                "unseeded" if seed is None else seed,
            )
            write_game_meta(game_dir, config, project_root)
            (game_dir / "session.json").write_text(
                json.dumps(
                    {
                        "session_dir": str(session_dir),
                        "server_log": str(server_log),
                        "observer_log": str(observer.log_path),
                        "game_index": index + 1,
                        "games_in_session": len(seeds),
                        "game_seed": seed,
                    },
                    indent=2,
                )
                + "\n"
            )
            try:
                observer.start_game(
                    game_dir,
                    players_config,
                    game_seed=seed,
                    skip_init_shuffling=config.skip_init_shuffling,
                )
                observer.wait_for_ready(game_dir)
                observer.wait_for_watching(game_dir)
                observer.wait_for_game_end(game_dir, timeout=game_timeout)
            except (RuntimeError, OSError) as exc:
                # One bad game must not cost the session. The server and the
                # observer are both still up, and the card load they represent
                # is the expensive thing being protected here.
                logger.error("Game %d/%d failed: %s", index + 1, len(seeds), exc)
                result.failed.append((game_dir, str(exc)))
                continue
            result.completed.append(game_dir)
    finally:
        observer.close()
        try:
            observer.proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            kill_tree(observer.proc.pid)
        kill_tree(server_proc.pid)

    # Same witness as the parallel path, for the same reason: two games sharing
    # one directory do not fail, they agree.
    assert len(set(result.completed)) == len(result.completed), (
        f"{len(result.completed)} games completed into "
        f"{len(set(result.completed))} distinct directories"
    )
    logger.info(
        "Session done: %d/%d games completed, %d failed",
        len(result.completed), len(seeds), len(result.failed),
    )
    return result
