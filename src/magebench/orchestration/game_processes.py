"""Process-launch and readiness helpers for orchestrator runs."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from magebench.common.llm_cost import DEFAULT_LLM_PROVIDER, required_api_key_env
from magebench.common.env import env_flag_dir, env_or_none
from magebench.common.log import get_logger
from magebench.common.process_manager import ProcessManager
from magebench.orchestration.config import Config, PilotPlayer

# Every `mvn exec:java` below inherits the DEFAULT ~/.m2 unless told otherwise, so a
# worktree built against an isolated repository still RUNS the shared jars. That makes
# an isolated build silently ineffective: the code is compiled, installed, and never
# loaded. Set MAVEN_REPO_LOCAL to point the runtime at the same repository the build
# used.
# Public because orchestrator.compile_project and the golden harness resolve from the
# same repository -- a build that installs somewhere the runtime does not read is
# compiled, installed, and never loaded.
MVN_REPO_ARGS = (
    [f"-Dmaven.repo.local={os.environ['MAVEN_REPO_LOCAL']}"]
    if os.environ.get("MAVEN_REPO_LOCAL")
    else []
)

logger = get_logger(__name__)


def ai_budget_props(ai_nodes: str | None, ai_time: str | None) -> list[str]:
    """The per-skill search budgets, as -D properties FOR THE SERVER JVM.

    THESE BELONG ON THE SERVER AND WERE ON THE CLIENT. ComputerPlayer6 lives in
    Mage.Server.Plugins and reads them with Integer.getInteger, so they are read in
    the process that runs the game:

        maxNodes         = Integer.getInteger("xmage.ai.nodes." + skill, 5000 * max(1, skill))
        maxThinkTimeSecs = Integer.getInteger("xmage.ai.time."  + skill, skill * 3)

    They used to be built into start_gui_client's argument list, which is the
    CLIENT, where nothing reads them; and the sequential runner never carried them
    at all. So MAGEBENCH_AI_NODES has never reached the engine on either path, and
    every run has used the engine's defaults while its metadata recorded whatever
    the caller asked for. The 4,624-game corpus records nodes "1:1000,8:5000" and
    cannot have used it.

    Found because a think-time cap of 1s left the measured maximum at 3.08s against
    a 3.10s baseline -- a knob that changed nothing, which is the only symptom this
    has. Nothing errors when a property lands on the wrong process.

    AND THEN IT HAPPENED AGAIN, BY POSITION RATHER THAN PROCESS. Moving these to the
    server was necessary and not sufficient: sequential_batch appended them to an
    ALREADY-BUILT java command with cmd.extend(), which puts them after the main
    class, where java hands them to main() as arguments. Correct process, wrong
    position, same silence. Caught 2026-08-28 only because a different property
    (xmage.ai.deterministicTiebreak) had a recorded consequence to check, and the
    engine reported it absent while /proc showed it on that pid's command line;
    sun.java.command read "mage.server.Main -Dxmage.ai.nodes.8=5000".

    So: these must be passed INTO build_java_cmd's system_props, never appended to
    its output. The test below asserts POSITION, because the older test asserted
    only membership ("-Dxmage.ai.nodes.1=1000" in argv) and passes either way --
    which is exactly why this survived. Third inertness of the same knob, counting
    the engine's own flat-5000 skill bug.

    Keyed by skill number rather than seat order, matching the engine.
    """
    props: list[str] = []
    for raw, name in ((ai_nodes, "nodes"), (ai_time, "time")):
        for pair in (raw.split(",") if raw else []):
            if ":" not in pair:
                continue
            skill, value = pair.split(":", 1)
            props.append(f"-Dxmage.ai.{name}.{skill.strip()}={value.strip()}")
    return props


def ai_tiebreak_props(deterministic: str | None) -> list[str]:
    """The deterministic tie-break switch, as a -D property FOR THE SERVER JVM.

    ComputerPlayer6 breaks equal-score ties at the ROOT with a coin drawn from
    RandomUtil's process-global Random, by a search running on the shared static
    simulation pool. Seeding the game does not make that reproducible -- setSeed
    fixes which values the stream yields, not which thread takes which one -- and
    it is why replaying a seed reproduces the deal and most of the play while
    diverging on about 1% of decisions. Setting this true swaps that coin for a
    Random owned by the player and re-seeded per search from (game seed, search
    ordinal), whose draws are single-threaded and cannot interleave.

    Same process as the budgets above, for the same reason: a server plugin reads
    it with Boolean.getBoolean, so it must be set on the JVM that runs the game.
    Anywhere else it is ignored in silence.

    NOT coerced to a bool by truthiness. Unset must mean "leave the engine
    default" and an explicit "false" must mean "off", and those stop being
    distinguishable the moment the string is coerced. Anything else raises: a
    typo'd "ture" read as false would silently run the nondeterministic arm of an
    experiment whose entire question is whether the arm is deterministic.
    """
    if deterministic is None:
        return []
    value = deterministic.strip().lower()
    if value not in ("true", "false"):
        raise ValueError(
            "MAGEBENCH_AI_DETERMINISTIC_TIEBREAK must be 'true' or 'false', got "
            f"{deterministic!r}"
        )
    return [f"-Dxmage.ai.deterministicTiebreak={value}"]

_SPECTATOR_TABLE_READY = "AI Puppeteer: waiting for"
_SPECTATOR_GAME_STARTED = "AI Puppeteer: all players joined"
# Matches the spectator's table announcement, which carries the table uuid.
_SPECTATOR_TABLE_ID_RE = re.compile(
    r"AI Puppeteer: waiting for .*? to join table ([0-9a-fA-F-]{36})"
)


def wait_for_spectator_table(log_path: Path, proc: subprocess.Popen, timeout: int = 300) -> str:
    """Block until the game table is ready, and return its id.

    The id matters because a bridge client launched without ``-Dxmage.bridge.tableId``
    joins the *first* table it finds in state WAITING with an open seat. That is
    correct only while exactly one table is open at a time, which is why batch setup
    has to be serialised. Returning the id here is what lets a caller pin each bridge
    to its own table and start games concurrently.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError("Spectator process exited before creating the game table")
        if log_path.exists():
            text = log_path.read_text()
            match = _SPECTATOR_TABLE_ID_RE.search(text)
            if match:
                return match.group(1)
            assert _SPECTATOR_TABLE_READY not in text, (
                f"spectator announced a table but no id could be parsed from {log_path}; "
                f"the log format changed and pinning would silently fall back to "
                f"join-any-table"
            )
        time.sleep(2)
    raise TimeoutError(f"Spectator did not create a table within {timeout}s — check {log_path}")


def wait_for_game_start(log_path: Path, proc: subprocess.Popen, timeout: int = 600) -> None:
    """Block until the spectator log indicates the game started."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return
        if log_path.exists():
            text = log_path.read_text()
            if _SPECTATOR_GAME_STARTED in text:
                return
        time.sleep(2)
    raise TimeoutError(f"Game did not start within {timeout}s — check {log_path}")


def bring_to_foreground_macos() -> None:
    """Bring the Java app to foreground on macOS using AppleScript."""
    if sys.platform != "darwin":
        return

    time.sleep(2)
    subprocess.run(
        [
            "osascript",
            "-e",
            'tell application "System Events" to set frontmost of first process whose name contains "java" to true',
        ],
        capture_output=True,
    )


def wait_with_pilot_monitoring(
    spectator_proc: subprocess.Popen,
    pilot_procs: list[tuple[str, subprocess.Popen]],
    pm: ProcessManager,
    poll_interval: float = 2.0,
) -> int:
    """Wait for the spectator to exit, aborting if any pilot dies with an error."""
    while True:
        spectator_rc = spectator_proc.poll()
        if spectator_rc is not None:
            if spectator_rc != 0:
                logger.error("Spectator exited with code %s — aborting game.", spectator_rc)
                pm.cleanup()
            return spectator_rc

        for name, proc in pilot_procs:
            rc = proc.poll()
            if rc is not None and rc != 0:
                logger.error("Pilot '%s' exited with code %s — aborting game.", name, rc)
                pm.cleanup()
                return -1

        time.sleep(poll_interval)


def start_server(
    pm: ProcessManager,
    project_root: Path,
    config: Config,
    config_path: Path,
    log_path: Path,
) -> subprocess.Popen:
    """Start the XMage server."""
    # AiDecisionRecorder lives in ComputerPlayer6, which runs HERE in the server
    # JVM -- not in the Swing client. System properties do not travel over the
    # wire, so this has to be set on the server process. (AI skill is different:
    # the client reads it and passes it through joinTable as a parameter.)
    ai_record_dir = env_flag_dir(
        "MAGEBENCH_AI_RECORD_DIR", needed_by="AiDecisionRecorder"
    )
    # AiHintProvider runs in the same place and for the same reason: it hooks
    # HumanPlayer, which lives in the server JVM. Naming the seats rather than a
    # boolean keeps the cost on the one seat being labelled -- each hint is a full
    # AI search, so hinting every seat would double the engine work per game.
    hint_seats = env_or_none("MAGEBENCH_HINT_SEATS")
    hint_dir = env_flag_dir("MAGEBENCH_HINT_DIR", needed_by="AiHintProvider")
    hint_skill = env_or_none("MAGEBENCH_HINT_SKILL")
    game_seed = env_or_none("MAGEBENCH_GAME_SEED")
    assert not hint_seats or hint_dir, (
        "MAGEBENCH_HINT_SEATS is set but MAGEBENCH_HINT_DIR is not -- hints would go to "
        "the server log instead of a file, which no consumer reads"
    )
    jvm_args = " ".join(
        [
            config.jvm_bridge_opts,
            "-Xmx1024m",
            f"-Dxmage.config.path={config_path}",
            *([f"-Dxmage.ai.recordDir={ai_record_dir}"] if ai_record_dir else []),
            *([f"-Dxmage.hint.seats={hint_seats}"] if hint_seats else []),
            *([f"-Dxmage.hint.dir={hint_dir}"] if hint_seats else []),
            *([f"-Dxmage.hint.skill={hint_skill}"] if hint_seats and hint_skill else []),
            # Common random numbers for GRPO: every game in a group shares one
            # shuffle, so the group baseline measures play rather than draw. The
            # engine seeds immediately before its shuffle, and RandomUtil is a
            # process-global static -- so this is only sound with ONE game per
            # server JVM. Two games in one JVM would interleave their draws.
            *([f"-Dxmage.game.seed={game_seed}"] if game_seed else []),
            # Read by ComputerPlayer6, which runs HERE.
            *ai_budget_props(
                env_or_none("MAGEBENCH_AI_NODES"), env_or_none("MAGEBENCH_AI_TIME")
            ),
        ]
    )

    env = {
        "XMAGE_AI_PUPPETEER": "1",
        "XMAGE_AI_PUPPETEER_USER": config.user,
        "XMAGE_AI_PUPPETEER_PASSWORD": config.password,
        "XMAGE_AI_PUPPETEER_SERVER": config.server,
        "XMAGE_AI_PUPPETEER_PORT": str(config.port),
        "XMAGE_AI_PUPPETEER_DISABLE_WHATS_NEW": "1",
        "MAVEN_OPTS": jvm_args,
    }

    return pm.start_jvm_process(
        args=["mvn", "-q", *MVN_REPO_ARGS, "exec:java"],
        cwd=project_root / "Mage.Server",
        env=env,
        log_file=log_path,
    )


def prefs_isolation_args(game_dir: Path) -> list[str]:
    """Give a client JVM its own java.util.prefs tree.

    Every Swing client writes preferences during shutdown, and WhatsNewDialog's
    cookie store does it unconditionally -- `disableWhatsNew` suppresses the dialog,
    not the store. They all share one backing store under $HOME, and java.util.prefs
    serialises access to it with a file lock.

    At 8 concurrent games this is invisible. At 20 it put `spectator.log` last in 19
    of 20 game dirs with a 33-113s tail after the server was done, and four of the
    twenty gave up with `BackingStoreException: Couldn't get file lock`. Games that
    never errored still waited -- the exception is the tip, the queue is the cost.
    It gets worse as concurrency rises, which is the direction we are going.

    A per-game tree removes the shared resource. Nothing reads it back: these clients
    are configured entirely by system properties.
    """
    prefs = game_dir / "prefs"
    prefs.mkdir(parents=True, exist_ok=True)
    return [
        f"-Djava.util.prefs.userRoot={prefs}",
        f"-Djava.util.prefs.systemRoot={prefs}",
    ]


def start_gui_client(
    pm: ProcessManager,
    project_root: Path,
    config: Config,
    log_path: Path,
    game_dir: Path | None = None,
) -> subprocess.Popen:
    """Start the GUI spectator client."""
    # TablesPanel.java:1761 reads this property and calls MatchOptions.setGameLogDir;
    # that reaches the server via TableController:684, and ServerGameEventLogCollector
    # no-ops while gameLogDir is null. Dropping game_dir here is why
    # server_game_events.jsonl was absent from every game directory, and why the winner
    # had to be inferred from last-seen life totals -- which yields "unresolved" whenever
    # neither player is at or below 0.
    #
    # The same file carries the per-decision query/response record for BOTH seats,
    # including the engine AI, which is what makes it the join target for teacher data.
    assert game_dir is not None, "game_dir is required to record server game events"
    config_json = config.get_players_config_json()

    ai_skills = env_or_none("MAGEBENCH_AI_SKILLS")

    jvm_args = " ".join(
        [
            config.jvm_opens,
            config.jvm_rendering,
            "-Xmx1536m",
            f"-Dxmage.observer.gameDir={game_dir}",
            *prefs_isolation_args(game_dir),
            *([f"-Dxmage.ai.skills={ai_skills}"] if ai_skills else []),
            # Single quotes inside the f-string ON PURPOSE. Nesting the same quote
            # type is PEP 701 and needs Python 3.12; this project declares
            # requires-python >=3.11, and uv resolves 3.11 here, so the double-quoted
            # version made THIS module -- the one that launches games -- unimportable
            # on a supported interpreter.
            "-Dxmage.aiPuppeteer.autoConnect=true",
            "-Dxmage.aiPuppeteer.autoStart=true",
            "-Dxmage.aiPuppeteer.disableWhatsNew=true",
            f"-Dxmage.aiPuppeteer.server={config.server}",
            f"-Dxmage.aiPuppeteer.port={config.port}",
            f"-Dxmage.aiPuppeteer.user={config.user}",
            f"-Dxmage.aiPuppeteer.password={config.password}",
        ]
    )

    env = {
        "XMAGE_AI_PUPPETEER": "1",
        "XMAGE_AI_PUPPETEER_USER": config.user,
        "XMAGE_AI_PUPPETEER_PASSWORD": config.password,
        "XMAGE_AI_PUPPETEER_SERVER": config.server,
        "XMAGE_AI_PUPPETEER_PORT": str(config.port),
        "XMAGE_AI_PUPPETEER_DISABLE_WHATS_NEW": "1",
        "XMAGE_AI_PUPPETEER_PLAYERS_CONFIG": config_json,
        "MAVEN_OPTS": jvm_args,
    }
    if config.match_time_limit:
        env["XMAGE_AI_PUPPETEER_MATCH_TIME_LIMIT"] = config.match_time_limit
    if config.match_buffer_time:
        env["XMAGE_AI_PUPPETEER_MATCH_BUFFER_TIME"] = config.match_buffer_time
    if config.custom_start_life:
        env["XMAGE_AI_PUPPETEER_CUSTOM_START_LIFE"] = str(config.custom_start_life)
    if config.skip_init_shuffling:
        env["XMAGE_AI_PUPPETEER_SKIP_INIT_SHUFFLING"] = "true"

    return pm.start_jvm_process(
        args=["mvn", "-q", *MVN_REPO_ARGS, "exec:java"],
        cwd=project_root / "Mage.Client",
        env=env,
        log_file=log_path,
    )


def start_sleepwalker_client(
    pm: ProcessManager,
    project_root: Path,
    config: Config,
    name: str,
    deck_path: str | None,
    log_path: Path,
    table_id: str | None = None,
) -> subprocess.Popen:
    """Start a sleepwalker client."""
    env = {"PYTHONUNBUFFERED": "1"}
    args = [
        sys.executable,
        "-m",
        "magebench.pilot.sleepwalker",
        "--server",
        config.server,
        "--port",
        str(config.port),
        "--username",
        name,
        "--project-root",
        str(project_root),
    ]
    if deck_path:
        args.extend(["--deck", str(project_root / deck_path)])
    if table_id:
        args.extend(["--table-id", table_id])

    return pm.start_process(
        args=args,
        cwd=project_root,
        env=env,
        log_file=log_path,
    )


def start_replay_client(
    pm: ProcessManager,
    project_root: Path,
    config: Config,
    name: str,
    deck_path: str | None,
    script_path: str | None,
    log_path: Path,
    game_dir: Path | None = None,
    table_id: str | None = None,
) -> subprocess.Popen:
    """Start a replay client."""
    env = {"PYTHONUNBUFFERED": "1"}
    args = [
        sys.executable,
        "-m",
        "magebench.pilot.replay",
        "--server",
        config.server,
        "--port",
        str(config.port),
        "--username",
        name,
        "--project-root",
        str(project_root),
    ]
    if deck_path:
        args.extend(["--deck", str(project_root / deck_path)])
    if script_path:
        args.extend(["--script", str(project_root / script_path)])
    if game_dir:
        args.extend(["--game-dir", str(game_dir)])
    if table_id:
        args.extend(["--table-id", table_id])

    return pm.start_process(
        args=args,
        cwd=project_root,
        env=env,
        log_file=log_path,
    )


def start_pilot_client(
    pm: ProcessManager,
    project_root: Path,
    config: Config,
    player: PilotPlayer,
    log_path: Path,
    game_dir: Path | None = None,
    table_id: str | None = None,
) -> subprocess.Popen:
    """Start an LLM-powered pilot client via MCP."""
    env = {"PYTHONUNBUFFERED": "1"}

    key_env = required_api_key_env(player.provider)
    api_key = os.environ.get(key_env)
    if api_key:
        env[key_env] = api_key

    args = [
        sys.executable,
        "-m",
        "magebench.pilot.pilot",
        "--server",
        config.server,
        "--port",
        str(config.port),
        "--username",
        player.name,
        "--project-root",
        str(project_root),
    ]

    if player.deck:
        args.extend(["--deck", str(project_root / player.deck)])
        # The own-deck card block rides on --deck, which every production seat
        # carries, so the pilot's --no-deck-block was unreachable from a
        # config-driven run: the ablated arm could not be requested at all. This
        # is the only way to ask for it, and it must be ASKED for -- an absent
        # variable leaves existing callers exactly as they were, and a value
        # that is neither "on" nor "off" raises instead of guessing which arm
        # the caller meant.
        if "MAGEBENCH_DECK_BLOCK" in os.environ:
            mode = os.environ["MAGEBENCH_DECK_BLOCK"]
            if mode not in ("on", "off"):
                raise ValueError(
                    f"MAGEBENCH_DECK_BLOCK={mode!r} is not 'on' or 'off'. It "
                    "selects which arm plays -- with the own-deck card block in "
                    "the system prompt, or without it -- so it cannot be guessed."
                )
            if mode == "off":
                args.append("--no-deck-block")
    if player.model:
        args.extend(["--model", player.model])
    if player.provider != DEFAULT_LLM_PROVIDER:
        args.extend(["--provider", player.provider])

    assert player.system_prompt, f"Pilot player {player.name} has no system_prompt (check preset)"
    effective_prompt = player.system_prompt
    if player.prompt_suffix:
        effective_prompt += (
            "\n\n## Chat Personality\n"
            "You have a chat personality described below. Use it to flavor your "
            "narration and trash-talk — be expressive, have fun with it, and "
            "react to your opponent's chat messages in character. But your actual "
            "gameplay decisions (card choices, attacks, blocks, targets, sequencing) "
            "must always be based on optimal Magic strategy. Never let the persona "
            "influence which play you choose.\n\n" + player.prompt_suffix
        )
    args.extend(["--system-prompt", effective_prompt])
    if player.max_interactions_per_turn is not None:
        args.extend(["--max-interactions-per-turn", str(player.max_interactions_per_turn)])
    if player.reasoning_effort:
        args.extend(["--reasoning-effort", player.reasoning_effort])
    if player.tools is not None:
        args.extend(["--tools", ",".join(player.tools)])
    if player.ignore_providers:
        args.extend(["--ignore-providers", ",".join(player.ignore_providers)])
    if player.provider_order:
        args.extend(["--provider-order", ",".join(player.provider_order)])
    if player.cache_control:
        args.extend(["--cache-control", json.dumps(player.cache_control)])
    if game_dir:
        args.extend(["--game-dir", str(game_dir)])
    if table_id:
        args.extend(["--table-id", table_id])

    return pm.start_process(
        args=args,
        cwd=project_root,
        env=env,
        log_file=log_path,
    )


def start_observer_client(
    pm: ProcessManager,
    project_root: Path,
    config: Config,
    log_path: Path,
    game_dir: Path | None = None,
) -> subprocess.Popen:
    """Start the observer spectator client."""
    # Same contract as start_gui_client: without game_dir the server-side event log
    # never gets a directory and the game produces no server_game_events.jsonl.
    assert game_dir is not None, "game_dir is required to record server game events"
    config_json = config.get_players_config_json()

    jvm_args_list = [
        config.jvm_opens,
        config.jvm_rendering,
        "-Xmx1536m",
        *prefs_isolation_args(game_dir),
        "-Dxmage.aiPuppeteer.autoConnect=true",
        "-Dxmage.aiPuppeteer.autoStart=true",
        "-Dxmage.aiPuppeteer.disableWhatsNew=true",
        f"-Dxmage.aiPuppeteer.server={config.server}",
        f"-Dxmage.aiPuppeteer.port={config.port}",
        f"-Dxmage.aiPuppeteer.user={config.user}",
        f"-Dxmage.aiPuppeteer.password={config.password}",
    ]
    jvm_args_list.append(f"-Dxmage.observer.gameDir={game_dir}")
    if config.record:
        record_path = config.record_output or (game_dir / "recording.mov")
        jvm_args_list.append(f"-Dxmage.observer.record={record_path}")

    jvm_args = " ".join(jvm_args_list)
    env = {
        "XMAGE_AI_PUPPETEER": "1",
        "XMAGE_AI_PUPPETEER_USER": config.user,
        "XMAGE_AI_PUPPETEER_PASSWORD": config.password,
        "XMAGE_AI_PUPPETEER_SERVER": config.server,
        "XMAGE_AI_PUPPETEER_PORT": str(config.port),
        "XMAGE_AI_PUPPETEER_DISABLE_WHATS_NEW": "1",
        "XMAGE_AI_PUPPETEER_PLAYERS_CONFIG": config_json,
        "MAVEN_OPTS": jvm_args,
    }
    if config.match_time_limit:
        env["XMAGE_AI_PUPPETEER_MATCH_TIME_LIMIT"] = config.match_time_limit
    if config.match_buffer_time:
        env["XMAGE_AI_PUPPETEER_MATCH_BUFFER_TIME"] = config.match_buffer_time
    if config.custom_start_life:
        env["XMAGE_AI_PUPPETEER_CUSTOM_START_LIFE"] = str(config.custom_start_life)
    if config.skip_init_shuffling:
        env["XMAGE_AI_PUPPETEER_SKIP_INIT_SHUFFLING"] = "true"

    args = ["mvn", "-q", *MVN_REPO_ARGS, "exec:java"]
    if sys.platform == "linux" and "DISPLAY" not in os.environ and "WAYLAND_DISPLAY" not in os.environ:
        xvfb = shutil.which("xvfb-run")
        assert xvfb is not None, (
            "Headless environment detected (no DISPLAY set) but xvfb-run is not installed. "
            "Install xvfb for your distribution (e.g. apt-get install xvfb or dnf install xorg-x11-server-Xvfb)."
        )
        args = [xvfb, "--auto-servernum", "--server-args=-screen 0 1920x1080x24", *args]
        logger.info("Headless environment detected — wrapping observer with xvfb-run")

    return pm.start_jvm_process(
        args=args,
        cwd=project_root / "Mage.Client.Observer",
        env=env,
        log_file=log_path,
    )
