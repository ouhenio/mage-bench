"""Main orchestrator for game lifecycle management."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from magebench.common.llm_cost import DEFAULT_LLM_PROVIDER, required_api_key_env
from magebench.common.log import get_logger, setup_logging
from magebench.common.port import find_available_port, wait_for_port
from magebench.common.process_manager import ProcessManager, jvm_oom_preexec_fn
from magebench.leaderboard.website_data import generate_all_website_data
from magebench.orchestration.batch_coordination import (
    GameSession,
    attach_game,
    await_game_start,
    claim_game_dir,
    claim_run_file,
    finalize_game,
    launch_game,
    wait_for_all_games,
)
from magebench.orchestration.config import Config
from magebench.orchestration.deck_choice import resolve_choice_decks
from magebench.orchestration.sequential_batch import run_sequential_batch
from magebench.orchestration.game_finalization import (
    print_run_cost_summary,
    run_git,
)
from magebench.orchestration.game_processes import (
    MVN_REPO_ARGS,
    bring_to_foreground_macos,
    start_server,
    wait_with_pilot_monitoring,
)
from magebench.orchestration.post_game_analysis import (
    AnnotationFailure,
    resolve_annotation_failures,
)
from magebench.orchestration.xml_config import modify_server_config

logger = get_logger(__name__)

_LOG_TIMESTAMP_TZ = ZoneInfo("America/Los_Angeles")


def _missing_llm_api_keys(config: Config) -> list[str]:
    """Return validation errors for LLM players missing required API keys."""
    errors: list[str] = []
    for player in config.pilot_players:
        configured_provider = player.provider
        provider = configured_provider or DEFAULT_LLM_PROVIDER
        try:
            key_env = required_api_key_env(configured_provider)
        except ValueError as exc:
            errors.append(f"{player.name} ({provider}): {exc}")
            continue
        api_key = os.environ.get(key_env)
        if not api_key or not api_key.strip():
            errors.append(f"{player.name} ({provider}) is missing the required API key")
    return errors


def _missing_llm_api_keys_for_run(config: Config) -> list[str]:
    """Return missing-key validation errors for a single config or batch manifest."""
    if not config.batch_config_files:
        return _missing_llm_api_keys(config)

    errors: list[str] = []
    for config_file in config.batch_config_files:
        game_config = Config(config_file=config_file)
        game_config.load_config()
        errors.extend(f"{config_file}: {missing}" for missing in _missing_llm_api_keys(game_config))
    return errors


def parse_args() -> Config:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(description="XMage AI Puppeteer")
    parser.add_argument("--config", type=Path, help="Path to player config JSON")
    parser.add_argument(
        "--batch-config-manifest",
        type=Path,
        help="Path to a JSON array of per-game config files",
    )
    parser.add_argument(
        "--observer",
        action="store_true",
        help="Launch the observer spectator client (auto-requests hand permissions)",
    )
    parser.add_argument(
        "--record",
        nargs="?",
        const=True,
        default=False,
        metavar="PATH",
        help="Record game to video file (optionally specify output path)",
    )
    parser.add_argument(
        "--games",
        type=int,
        default=1,
        help="Number of parallel games on the same server (default: 1)",
    )
    parser.add_argument(
        "--sequential-games",
        type=int,
        default=0,
        help=(
            "Number of games to play one after another on ONE server and ONE observer. "
            "Amortises the ~25s card load and one port across the whole sequence. "
            "Seeds come from MAGEBENCH_GAME_SEEDS as a comma-separated list."
        ),
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable DEBUG-level logging (verbose MCP details, process management)",
    )
    parser.add_argument(
        "--skip-compile",
        action="store_true",
        help="Skip compilation (caller already compiled)",
    )
    args = parser.parse_args()
    assert not (args.config and args.batch_config_manifest), (
        "--config and --batch-config-manifest are mutually exclusive"
    )
    assert not (args.sequential_games and args.games > 1), (
        "--sequential-games and --games are mutually exclusive: RandomUtil.random is a "
        "process-global static, so two games running concurrently in one server JVM "
        "would interleave their draws and neither would be reproducible"
    )

    record_output = None
    if args.record and args.record is not True:
        record_output = Path(args.record)

    batch_config_files: list[Path] = []
    config_file = args.config
    num_games = args.games
    if args.batch_config_manifest:
        manifest = json.loads(args.batch_config_manifest.read_text())
        assert isinstance(manifest, list) and manifest, (
            f"Batch config manifest must be a non-empty JSON array: {args.batch_config_manifest}"
        )
        for index, item in enumerate(manifest):
            assert isinstance(item, str) and item, (
                f"Batch config manifest entry {index} must be a non-empty string path"
            )
            path = Path(item)
            assert path.exists(), f"Batch config file not found: {path}"
            batch_config_files.append(path)
        config_file = batch_config_files[0]
        if args.games != 1:
            assert args.games == len(batch_config_files), (
                f"--games ({args.games}) must match batch config count ({len(batch_config_files)})"
            )
        num_games = len(batch_config_files)

    return Config(
        config_file=config_file,
        batch_config_files=batch_config_files,
        observer=args.observer,
        record=bool(args.record),
        record_output=record_output,
        num_games=num_games,
        sequential_games=args.sequential_games,
        debug=args.debug,
        skip_compile=args.skip_compile,
    )


def _sequential_seeds(num_games: int) -> list[int | None]:
    """Seeds for a sequential session, from MAGEBENCH_GAME_SEEDS.

    Explicit and per game rather than a base plus an index. A session is the
    first thing in this harness where "which seed did game 4 get" is a question
    with a non-obvious answer, and deriving it silently is how a resumed or
    re-run batch ends up quietly re-dealing a game it already has.

    Unset means every game is unseeded, which is a legitimate way to generate a
    corpus. Set means the list must match the game count exactly -- a short list
    silently recycled would deal the same hands twice.
    """
    raw = os.environ.get("MAGEBENCH_GAME_SEEDS")
    if raw is None or raw == "":
        return [None] * num_games
    seeds = [int(part.strip()) for part in raw.split(",") if part.strip()]
    if len(seeds) != num_games:
        raise ValueError(
            f"MAGEBENCH_GAME_SEEDS has {len(seeds)} seeds but --sequential-games is "
            f"{num_games}. Give one seed per game, or unset it for an unseeded run."
        )
    return list(seeds)


def _game_timeout() -> int:
    """Seconds to wait for one game to reach `game_end`, from MAGEBENCH_GAME_TIMEOUT.

    THE DEFAULT IS 1800 AND IS UNCHANGED, so no existing run moves. What changes is
    that the ceiling is now reachable, because a hard-coded one does not merely
    truncate a slow run -- IT SELECTS WHICH GAMES SURVIVE. A per-game deadline
    removes the long games and keeps the short ones, so for any treatment that
    affects game length the survivors are conditioned on the treatment, and no
    reweighting repairs it: the missing outcomes were never generated.

    Measured occurrence that produced this knob (issues/p1-game-timeout-is-
    hardcoded-with-no-config-surface.json5): a thinking-ON P1 game ran 30.0 minutes
    exactly, reached game_seq 468 still climbing, and died with HTTP 408 and no
    server `game_end`. The 30.0 was the fingerprint of the cap, not of the game.

    Unset means 1800. Set means the value is used and must be a positive integer --
    a value that does not parse is an error rather than a silent fall back to the
    default, because silently reverting to 1800 is exactly the failure this exists
    to make visible.
    """
    raw = os.environ.get("MAGEBENCH_GAME_TIMEOUT")
    if raw is None or raw == "":
        return 1800
    try:
        seconds = int(raw)
    except ValueError:
        raise ValueError(
            f"MAGEBENCH_GAME_TIMEOUT={raw!r} is not an integer. Give whole seconds, "
            f"or unset it for the 1800s default."
        ) from None
    if seconds <= 0:
        raise ValueError(
            f"MAGEBENCH_GAME_TIMEOUT={seconds} must be positive; a non-positive "
            f"timeout would kill every game instantly."
        )
    return seconds


def compile_project(
    project_root: Path,
    *,
    observer: bool = False,
    populate_local_repo: bool = False,
) -> bool:
    """Compile the project using Maven."""
    logger.info("Compiling project...")
    modules = "Mage.Server,Mage.Client,Mage.Client.Bridge"
    if observer:
        modules += ",Mage.Client.Observer"

    # Same repository the game JVMs resolve from. Without this the compile INSTALLS
    # into ~/.m2 while every `mvn exec:java` below loads from MAVEN_REPO_LOCAL, so the
    # modules just built are not the modules that run -- the Makefile's own version of
    # this defect, living outside the Makefile. Measured 2026-08-18: a golden session
    # wrote mage-player-ai-ma into ~/.m2 at 18:26:06 while m2-teacher still held an
    # entirely different build. Conditional, exactly as MVN_REPO_ARGS already is:
    # unset, both sides use ~/.m2 and agree.
    cmd = [
        "mvn",
        "-q",
        *MVN_REPO_ARGS,
        "-DskipTests",
        "-pl",
        modules,
        "-am",
    ]
    if populate_local_repo:
        cmd.append("-Dmaven.build.cache.enabled=false")
    cmd.append("install")

    result = subprocess.run(cmd, cwd=project_root, preexec_fn=jvm_oom_preexec_fn())
    return result.returncode == 0


def refresh_observer_resources(project_root: Path) -> bool:
    """Refresh observer client resources under target/classes."""
    result = subprocess.run(
        [
            "mvn",
            "-q",
            *MVN_REPO_ARGS,
            "-pl",
            "Mage.Client.Observer",
            "resources:resources",
        ],
        cwd=project_root,
        preexec_fn=jvm_oom_preexec_fn(),
    )
    return result.returncode == 0


def clean_stale_h2_locks(project_root: Path) -> None:
    """Remove stale H2 lock files left by previously killed server processes."""
    db_dir = project_root / "Mage.Server" / "db"
    for lock_file in db_dir.glob("*.lock.db"):
        logger.info("Removing stale DB lock file: %s", lock_file)
        lock_file.unlink()


def _check_regular_season_block(project_root: Path) -> str | None:
    """Return an error message if regular-season games should be blocked."""
    season_file = project_root / "data" / "season.json"
    if not season_file.exists():
        return None
    season_data = json.loads(season_file.read_text())
    phase = season_data.get("phase")
    if phase == "regular-season":
        return None
    season_num = season_data.get("current_season", "?")
    if phase == "tournament":
        return (
            f"Season {season_num} is in the tournament phase! Regular-season games are not allowed during tournaments."
        )
    if phase == "between-seasons":
        return (
            f"Season {season_num} has crowned a champion. "
            "Regular-season games remain blocked until the next season starts."
        )
    return f"Season {season_num} is in phase '{phase}'. Regular-season games are only allowed during regular season."


@dataclass
class OrchestratorRunResult:
    """Result of a programmatic orchestrator run."""

    exit_code: int
    sessions: list[GameSession] = field(default_factory=list)
    pilot_costs: dict[int, float] = field(default_factory=dict)
    blunder_costs: dict[int, float] = field(default_factory=dict)
    post_game_failures: list[str] = field(default_factory=list)


def run_orchestrator(config: Config, project_root: Path | None = None) -> OrchestratorRunResult:
    """Run one orchestrator job programmatically."""
    if project_root is None:
        project_root = Path.cwd().resolve()

    config.load_config()

    if not config.skip_post_game_prompts and not config.tournament_game:
        season_block = _check_regular_season_block(project_root)
        if season_block:
            logger.error(season_block)
            return OrchestratorRunResult(exit_code=2)

    pm = ProcessManager()
    port_reservation = None
    sessions: list[GameSession] = []
    batch = config.num_games > 1
    pilot_costs: dict[int, float] = {}
    blunder_costs: dict[int, float] = {}
    post_game_failures: list[str] = []

    try:
        if batch and config.record_output:
            logger.error("--record=PATH cannot be used with --games (use --record without a path instead)")
            return OrchestratorRunResult(exit_code=2)

        missing_llm_keys = _missing_llm_api_keys_for_run(config)
        if missing_llm_keys:
            logger.error("LLM players configured without required API keys:")
            for missing in missing_llm_keys:
                logger.error("  - %s", missing)
            logger.error("Set the required key(s) or use a non-LLM config (e.g. make run).")
            return OrchestratorRunResult(exit_code=2)

        config.timestamp = datetime.now(_LOG_TIMESTAMP_TZ).strftime("%Y%m%d_%H%M%S")

        if config.record and not config.observer:
            logger.info("Recording requires observer mode, enabling --observer")
            config.observer = True

        log_dir = (project_root / config.log_dir).resolve()
        log_dir.mkdir(parents=True, exist_ok=True)

        if config.skip_compile:
            logger.info("Skipping compilation (--skip-compile)")
        else:
            if not compile_project(project_root, observer=config.observer):
                logger.error("Compilation failed")
                return OrchestratorRunResult(exit_code=1)
            if config.observer:
                logger.info("Refreshing observer resources...")
                if not refresh_observer_resources(project_root):
                    logger.error("Failed to refresh observer resources")
                    return OrchestratorRunResult(exit_code=1)

        if config.sequential_games:
            # One server, one observer, N games in a row. Everything below this
            # point exists to stand a server and a spectator up per game, which
            # is the cost being removed, so the sequential path does not go
            # through it.
            seeds = _sequential_seeds(config.sequential_games)
            resolve_choice_decks(config.pilot_players, project_root, config.deck_type)
            config.resolve_random_decks(project_root)
            config.validate_deck_sizes(project_root)
            result = run_sequential_batch(
                config, project_root, log_dir, seeds, pm=pm,
                game_timeout=_game_timeout(),
            )
            if result.failed:
                for game_dir, reason in result.failed:
                    logger.error("  %s: %s", game_dir.name, reason)
            return OrchestratorRunResult(
                exit_code=0 if result.completed and not result.failed else 1
            )

        logger.info("Finding available port starting from %d...", config.start_port)
        port_reservation = find_available_port(config.start_port)
        config.port = port_reservation.port
        logger.info("Using port %d", config.port)

        # Claimed, not derived. Every one of these names is keyed on a timestamp
        # that is only unique to the second, so two orchestrators started in the
        # same second would otherwise share them silently -- one server config
        # overwritten mid-read, one server log interleaved from two JVMs, and in
        # the non-batch case one game directory holding two games' events.
        first_game_dir: Path | None = None
        if batch:
            server_config_path = claim_run_file(
                log_dir, f"server_config_{config.timestamp}", ".xml"
            )
            server_log = claim_run_file(log_dir, f"server_{config.timestamp}", ".log")
        else:
            first_game_dir = claim_game_dir(log_dir, config.timestamp)
            server_config_path = first_game_dir / "server_config.xml"
            server_log = first_game_dir / "server.log"

        modify_server_config(
            source=project_root / "Mage.Server" / "config" / "config.xml",
            destination=server_config_path,
            port=config.port,
        )

        logger.info("Server log: %s", server_log)

        if not config.skip_compile:
            clean_stale_h2_locks(project_root)

        logger.info("Starting XMage server...")
        start_server(pm, project_root, config, server_config_path, server_log)

        if not wait_for_port(config.server, config.port, config.server_wait):
            logger.error("Server failed to start within %ds", config.server_wait)
            logger.error("Check %s for details", server_log)
            return OrchestratorRunResult(exit_code=1)

        port_reservation.release()
        port_reservation = None
        logger.info("Server is ready!")

        if config.config_file:
            logger.info("Using config: %s", config.config_file)
        if batch:
            logger.info("Starting %d parallel games...", config.num_games)

        used_player_names: set[str] = set()
        cross_game_round_robin: list[tuple[str, ...]] = []
        cross_game_format_picks: list[str] = []
        # Set the batch up in three passes rather than one game at a time.
        #
        # Each game's setup blocks twice -- ~16 s waiting for the spectator to boot
        # Swing and create its table, then ~7 s waiting for the bridge to join it.
        # Done per game those waits serialise, costing ~24 s of ramp per additional
        # game (measured: 24 s, 24 s, 26 s across a 4-game batch; ~29% of its wall
        # clock, and worse at higher --parallel).
        #
        # Launching every spectator before waiting on any of them makes the waits
        # overlap, so the batch pays roughly one wait instead of N. No threads are
        # needed: the concurrency comes from the JVMs already running while we
        # block. That keeps config resolution sequential, which it must be -- it
        # mutates shared cross-game state.
        #
        # Safe only because each bridge is now pinned to its own table id. An
        # unpinned bridge joins the first table in state WAITING, so with several
        # tables open at once games would silently cross-wire.
        skipped: list[tuple[int, str]] = []

        def _skip(index: int, phase: str, exc: Exception) -> None:
            if not batch:
                raise exc
            skipped.append((index + 1, phase))
            logger.error(
                "Game %d/%d: failed to %s, skipping: %s", index + 1, config.num_games, phase, exc
            )

        launched: list[GameSession] = []
        for index in range(config.num_games):
            try:
                launched.append(
                    launch_game(
                        index,
                        config.num_games,
                        config,
                        pm,
                        project_root,
                        log_dir,
                        config.timestamp,
                        game_dir=first_game_dir,
                        used_player_names=used_player_names if batch else None,
                        cross_game_round_robin=cross_game_round_robin if batch else None,
                        cross_game_format_picks=cross_game_format_picks if batch else None,
                    )
                )
            except (TimeoutError, RuntimeError) as exc:
                _skip(index, "launch", exc)

        # N games announced must be N distinct directories. Two games sharing one
        # directory do not fail: they interleave into the same
        # server_game_events.jsonl and every downstream comparison between them
        # then reads one file twice and calls the arms identical. The claim above
        # makes that impossible; this is the witness that says so out loud, so a
        # future change that reintroduces a derived path fails here rather than
        # in somebody's conclusions six hours later.
        launched_dirs = [session.game_dir for session in launched]
        assert len(set(launched_dirs)) == len(launched_dirs), (
            f"{len(launched_dirs)} games launched into {len(set(launched_dirs))} "
            f"distinct directories -- two games are sharing a log directory and "
            f"will overwrite each other: {sorted(str(d) for d in launched_dirs)}"
        )

        attached: list[GameSession] = []
        for session in launched:
            try:
                attach_game(session, config.num_games, pm, project_root)
            except (TimeoutError, RuntimeError) as exc:
                _skip(session.index, "start its clients", exc)
                continue
            attached.append(session)

        for session in attached:
            try:
                await_game_start(session, config.num_games)
            except (TimeoutError, RuntimeError) as exc:
                _skip(session.index, "start", exc)
                continue
            sessions.append(session)

        if batch and skipped:
            # Say it once, at the end, where it cannot be lost. A skip is a single error line
            # emitted up to 300s and thousands of lines into the run, and the summary that
            # follows reports only the survivors -- so a 6-game batch that ran 4 otherwise
            # reads as success. A runner consuming this has already been silently truncated
            # once tonight; do not make it infer the count.
            logger.error(
                "Ran %d of %d requested games. Skipped: %s",
                len(sessions),
                config.num_games,
                ", ".join(f"game {i} ({phase})" for i, phase in skipped),
            )

        if batch and not sessions:
            logger.error("No games launched successfully")
            return OrchestratorRunResult(exit_code=1)

        if not batch:
            bring_to_foreground_macos()

        last_game_dir = sessions[-1].game_dir
        if config.config_file and not config.batch_config_files:
            last_link = log_dir / f"last-{config.run_tag}"
            last_link.unlink(missing_ok=True)
            last_link.symlink_to(last_game_dir.name)
        branch = run_git("rev-parse --abbrev-ref HEAD", project_root)
        if branch:
            safe_branch = branch.replace("/", "-")
            branch_link = log_dir / f"last-branch-{safe_branch}"
            branch_link.unlink(missing_ok=True)
            branch_link.symlink_to(last_game_dir.name)

        if batch:
            results = wait_for_all_games(sessions)
            deferred: list[AnnotationFailure] = []
            for session in sessions:
                spectator_rc = results.get(session.index, -1)
                pilot_costs[session.index], blunder_costs[session.index] = finalize_game(
                    session,
                    project_root,
                    spectator_rc,
                    deferred_failures=deferred,
                    post_game_failures=post_game_failures,
                )
            resolve_annotation_failures(deferred)
        else:
            session = sessions[0]
            assert session.spectator_proc is not None
            if session.pilot_procs:
                spectator_rc = wait_with_pilot_monitoring(session.spectator_proc, session.pilot_procs, pm)
            else:
                spectator_rc = session.spectator_proc.wait()
            pilot_costs[session.index], blunder_costs[session.index] = finalize_game(
                session,
                project_root,
                spectator_rc,
                post_game_failures=post_game_failures,
            )

        print_run_cost_summary(sessions, pilot_costs, blunder_costs)

        if post_game_failures:
            logger.error("")
            logger.error("!" * 60)
            logger.error("FAILURES")
            logger.error("!" * 60)
            for msg in post_game_failures:
                logger.error("  %s", msg)
            logger.error("!" * 60)

        if not config.skip_post_game_prompts:
            generate_all_website_data()
            logger.info("Website data regenerated")

        return OrchestratorRunResult(
            exit_code=0,
            sessions=sessions,
            pilot_costs=pilot_costs,
            blunder_costs=blunder_costs,
            post_game_failures=post_game_failures,
        )
    finally:
        if port_reservation is not None:
            port_reservation.release()
        pm.cleanup()


def main() -> int:
    """Main orchestrator for game lifecycle management."""
    config = parse_args()
    setup_logging(debug=config.debug)
    if config.debug:
        os.environ["PUPPETEER_LOG_LEVEL"] = "DEBUG"
    return run_orchestrator(config).exit_code
