"""Process-launch and readiness helpers for orchestrator runs."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from magebench.common.llm_cost import DEFAULT_LLM_PROVIDER, required_api_key_env
from magebench.common.log import get_logger
from magebench.common.process_manager import ProcessManager
from magebench.orchestration.config import Config, PilotPlayer

# Every `mvn exec:java` below inherits the DEFAULT ~/.m2 unless told otherwise, so a
# worktree built against an isolated repository still RUNS the shared jars. That makes
# an isolated build silently ineffective: the code is compiled, installed, and never
# loaded. Set MAVEN_REPO_LOCAL to point the runtime at the same repository the build
# used.
_MVN_REPO_ARGS = (
    [f"-Dmaven.repo.local={os.environ['MAVEN_REPO_LOCAL']}"]
    if os.environ.get("MAVEN_REPO_LOCAL")
    else []
)

logger = get_logger(__name__)

_SPECTATOR_TABLE_READY = "AI Puppeteer: waiting for"
_SPECTATOR_GAME_STARTED = "AI Puppeteer: all players joined"


def wait_for_spectator_table(log_path: Path, proc: subprocess.Popen, timeout: int = 300) -> None:
    """Block until the spectator log indicates the game table is ready."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError("Spectator process exited before creating the game table")
        if log_path.exists():
            text = log_path.read_text()
            if _SPECTATOR_TABLE_READY in text:
                return
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
    ai_record_dir = os.environ.get("MAGEBENCH_AI_RECORD_DIR", "")
    # AiHintProvider runs in the same place and for the same reason: it hooks
    # HumanPlayer, which lives in the server JVM. Naming the seats rather than a
    # boolean keeps the cost on the one seat being labelled -- each hint is a full
    # AI search, so hinting every seat would double the engine work per game.
    hint_seats = os.environ.get("MAGEBENCH_HINT_SEATS", "")
    hint_dir = os.environ.get("MAGEBENCH_HINT_DIR", "")
    hint_skill = os.environ.get("MAGEBENCH_HINT_SKILL", "")
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
        args=["mvn", "-q", *_MVN_REPO_ARGS, "exec:java"],
        cwd=project_root / "Mage.Server",
        env=env,
        log_file=log_path,
    )


def start_gui_client(
    pm: ProcessManager,
    project_root: Path,
    config: Config,
    log_path: Path,
    game_dir: Path | None = None,
) -> subprocess.Popen:
    """Start the GUI spectator client."""
    # The Swing client already reads this property (TablesPanel.java:1761) and sets
    # MatchOptions.gameLogDir, which is the only thing gating
    # ServerGameEventLogCollector: it returns early while gameLogDir is null, and
    # otherwise writes server_game_events.jsonl. Dropping game_dir here is why that
    # file exists in zero game directories, and why the winner has to be
    # reconstructed from life totals rather than read. The same file carries the
    # per-decision query/response record for BOTH seats, including the engine AI --
    # which is what makes AI-vs-AI games usable as a teacher dataset.
    assert game_dir is not None, "game_dir is required to record server game events"
    config_json = config.get_players_config_json()

    ai_skills = os.environ.get("MAGEBENCH_AI_SKILLS", "")

    jvm_args = " ".join(
        [
            config.jvm_opens,
            config.jvm_rendering,
            "-Xmx1536m",
            f"-Dxmage.observer.gameDir={game_dir}",
            *([f"-Dxmage.ai.skills={ai_skills}"] if ai_skills else []),
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
        args=["mvn", "-q", *_MVN_REPO_ARGS, "exec:java"],
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
    config_json = config.get_players_config_json()

    jvm_args_list = [
        config.jvm_opens,
        config.jvm_rendering,
        "-Xmx1536m",
        "-Dxmage.aiPuppeteer.autoConnect=true",
        "-Dxmage.aiPuppeteer.autoStart=true",
        "-Dxmage.aiPuppeteer.disableWhatsNew=true",
        f"-Dxmage.aiPuppeteer.server={config.server}",
        f"-Dxmage.aiPuppeteer.port={config.port}",
        f"-Dxmage.aiPuppeteer.user={config.user}",
        f"-Dxmage.aiPuppeteer.password={config.password}",
    ]
    if game_dir:
        jvm_args_list.append(f"-Dxmage.observer.gameDir={game_dir}")
    if config.record:
        resolved_game_dir = game_dir or (project_root / config.log_dir / f"game_{config.timestamp}").resolve()
        record_path = config.record_output or (resolved_game_dir / "recording.mov")
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

    args = ["mvn", "-q", *_MVN_REPO_ARGS, "exec:java"]
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
