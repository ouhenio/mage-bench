"""Per-game setup and batch coordination helpers for orchestrator runs."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from magebench.common.log import get_logger
from magebench.common.process_manager import ProcessManager, kill_tree
from magebench.game.game_log import merge_game_log
from magebench.orchestration.config import Config
from magebench.orchestration.deck_choice import resolve_choice_decks
from magebench.orchestration.game_finalization import (
    ensure_game_over_event,
    print_game_summary,
    run_git,
    write_error_log,
    write_game_meta,
)
from magebench.orchestration.game_processes import (
    start_gui_client,
    start_observer_client,
    start_pilot_client,
    start_replay_client,
    start_sleepwalker_client,
    wait_for_game_start,
    wait_for_spectator_table,
)
from magebench.orchestration.post_game_analysis import (
    AnnotationFailure,
    upload_and_export,
)

logger = get_logger(__name__)


@dataclass
class GameSession:
    """State for a single game within a parallel run."""

    index: int
    game_dir: Path
    config: Config
    spectator_proc: subprocess.Popen | None = None
    pilot_procs: list[tuple[str, subprocess.Popen]] = field(default_factory=list)


def _claim(parent: Path, stem: str, ext: str, *, directory: bool) -> Path:
    """Create `parent/stem+ext`, or the next free `stem-N+ext`, and return it.

    THE POINT IS THE ABSENCE OF exist_ok. `game_<timestamp>` is unique only to
    the SECOND, and the game directory used to be created with
    `mkdir(exist_ok=True)`, so two orchestrators launched in the same second
    silently landed in ONE directory and overwrote each other's
    server_game_events.jsonl. That cost three verification runs: 18 parallel
    runs collapsed into 2 directories, after which every arm was reading the
    same file, every "identical" verdict was a file compared against itself,
    and a hook bisection built on it accused an innocent hook. The tell was
    that only 2 of 18 runs had written an output file at all.

    mkdir and O_EXCL are atomic on POSIX, so exactly one process can win a
    name; the loser takes the next suffix instead of sharing. No lock, no
    registry, and it holds between processes that know nothing about each other.

    The name stays byte-identical to the old one whenever nothing collides,
    which is deliberate rather than lazy. game_dir.name IS the game_id
    downstream -- in exports, tournament brackets and uploads -- so a pid or
    random suffix would reshape every id in the corpus to fix a case that only
    arises in parallel. Collisions get `-1`, `-2`; solitary runs are unchanged.
    """
    for attempt in range(1000):
        candidate = parent / f"{stem}{'' if attempt == 0 else f'-{attempt}'}{ext}"
        try:
            if directory:
                candidate.mkdir(parents=True)
            else:
                parent.mkdir(parents=True, exist_ok=True)
                candidate.touch(exist_ok=False)
            return candidate
        except FileExistsError:
            continue
    raise RuntimeError(
        f"Could not claim a unique name for {stem}{ext} under {parent} after 1000 tries"
    )


def claim_game_dir(log_dir: Path, timestamp: str, suffix: str = "") -> Path:
    """Claim this game's log directory, refusing to share one with another run."""
    return _claim(log_dir, f"game_{timestamp}{suffix}", "", directory=True)


def claim_run_file(log_dir: Path, stem: str, ext: str) -> Path:
    """Claim a batch-level file (server config, server log) the same way.

    Same defect, second surface, and nobody had hit it because nobody had run
    the batch path in parallel: the orchestrator puts `server_config_<ts>.xml`
    and `server_<ts>.log` at the log-dir level under the same second-granular
    key, so two same-second batches would share one server config file and
    interleave one server log.
    """
    return _claim(log_dir, stem, ext, directory=False)


def launch_game(
    index: int,
    num_games: int,
    base_config: Config,
    pm: ProcessManager,
    project_root: Path,
    log_dir: Path,
    timestamp: str,
    game_dir: Path | None = None,
    used_player_names: set[str] | None = None,
    cross_game_round_robin: list[tuple[str, ...]] | None = None,
    cross_game_format_picks: list[str] | None = None,
) -> GameSession:
    """Resolve config, create the game dir, and start the spectator JVM.

    Phase one of three. Returns as soon as the spectator process is spawned,
    without waiting for it to create its table -- that wait is `attach_game`.
    Splitting them lets a batch launch every spectator before blocking on any of
    them, which is what removes the ~24 s per-game serial ramp.

    Config resolution stays here and stays sequential: it mutates the shared
    `used_player_names`, `cross_game_round_robin` and `cross_game_format_picks`
    across games, and duplicate player names would put two bridge clients on one
    XMage username, where they kick each other off the server forever.
    """
    batch = num_games > 1
    game_label = f"Game {index + 1}/{num_games}: " if batch else ""

    if batch:
        config_file = base_config.config_file
        if base_config.batch_config_files:
            assert index < len(base_config.batch_config_files), f"Missing batch config for game {index + 1}/{num_games}"
            config_file = base_config.batch_config_files[index]
        game_config = base_config.new_game_config(
            config_file=config_file,
            user=f"spectator{index + 1}",
            num_games=num_games,
            port=base_config.port,
            timestamp=timestamp,
        )
        game_config.load_config(
            cross_game_used_names=used_player_names,
            cross_game_round_robin=cross_game_round_robin,
            cross_game_format_picks=cross_game_format_picks,
        )
        if used_player_names is not None:
            all_players = game_config.pilot_players + game_config.sleepwalker_players
            for player in all_players:
                assert player.name not in used_player_names, (
                    f"Duplicate player name {player.name!r} across parallel games — "
                    f"two bridge clients with the same XMage username will "
                    f"endlessly kick each other. Reduce num_games or use "
                    f"unique player names."
                )
                used_player_names.add(player.name)
    else:
        game_config = base_config

    # A caller that already needed the directory -- the non-batch path claims it
    # early, to put server_config.xml and server.log inside it -- passes it in.
    # Re-deriving it here from the timestamp is precisely what let two sites
    # disagree about which directory this game owns.
    if game_dir is None:
        game_dir = claim_game_dir(log_dir, timestamp, f"_g{index + 1}" if batch else "")

    manifest: dict[str, str | list[str] | int | None] = {
        "timestamp": timestamp,
        "branch": run_git("rev-parse --abbrev-ref HEAD", project_root),
        "commit": run_git("rev-parse HEAD", project_root),
        "commit_log": run_git("log --oneline -10", project_root).splitlines(),
        "command": sys.argv,
        "config_file": str(game_config.config_file) if game_config.config_file else None,
    }
    if batch:
        manifest["game_index"] = index + 1
        manifest["num_games"] = num_games
    (game_dir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")

    if game_config.config_file:
        shutil.copy2(game_config.config_file, game_dir / "config.json")

    resolve_choice_decks(game_config.pilot_players, project_root, game_config.deck_type)
    game_config.resolve_random_decks(project_root)
    game_config.validate_deck_sizes(project_root)
    write_game_meta(game_dir, game_config, project_root)

    spectator_log = game_dir / "spectator.log"
    logger.info("%sGame logs: %s", game_label, game_dir)
    logger.info("%sSpectator log: %s", game_label, spectator_log)
    if game_config.record:
        record_path = game_config.record_output or (game_dir / "recording.mov")
        logger.info("%sRecording to: %s", game_label, record_path)

    if game_config.observer:
        logger.info("%sStarting observer spectator client...", game_label)
        start_spectator_client = start_observer_client
    else:
        start_spectator_client = start_gui_client

    spectator_proc = start_spectator_client(pm, project_root, game_config, spectator_log, game_dir=game_dir)
    return GameSession(
        index=index,
        game_dir=game_dir,
        config=game_config,
        spectator_proc=spectator_proc,
    )


def _terminate_session(session: GameSession) -> None:
    """Stop a half-set-up game so a failure in one game does not leak JVMs."""
    if session.spectator_proc is not None and session.spectator_proc.poll() is None:
        session.spectator_proc.terminate()
    for _, proc in session.pilot_procs:
        if proc.poll() is None:
            # kill_tree, not terminate: these are python processes that spawned the bridge JVM
            # as a grandchild, and pilot.py installs no SIGTERM handler, so terminate() kills
            # the parent without running the cleanup that stops the JVM. wait_for_all_games
            # already uses kill_tree here for the same reason.
            kill_tree(proc.pid)


def attach_game(
    session: GameSession,
    num_games: int,
    pm: ProcessManager,
    project_root: Path,
) -> None:
    """Wait for this game's table, then start its bridge clients pinned to it.

    Separated from `launch_game` so a batch can start every spectator first and
    then wait on them. The waits are the expensive part (~16 s of Swing boot and
    table creation each), and once all the spectators are running they elapse
    concurrently, so N games cost roughly one wait rather than N.

    Pinning is what makes that safe: an unpinned bridge joins the first table it
    finds in state WAITING, so with several tables open at once games would
    cross-wire instead of failing. See `wait_for_spectator_table`.
    """
    batch = num_games > 1
    game_label = f"Game {session.index + 1}/{num_games}: " if batch else ""
    game_config = session.config
    game_dir = session.game_dir
    spectator_log = game_dir / "spectator.log"
    assert session.spectator_proc is not None, "attach_game requires a launched spectator"

    bridge_count = (
        len(game_config.sleepwalker_players) + len(game_config.pilot_players) + len(game_config.replay_players)
    )
    if bridge_count == 0:
        return

    try:
        table_id = wait_for_spectator_table(spectator_log, session.spectator_proc, timeout=300)
        # Every bridge in a batch must be pinned. Setup is concurrent now, so several tables
        # are open at once; an unpinned bridge joins the first WAITING table with an open seat
        # and would silently land in another game -- both games then finish and both look
        # healthy. tryJoinTable (BridgeClient.java:300) skips non-matching tables entirely when
        # pinned, so a wrong id fails loudly instead. Guard the invariant rather than trusting
        # every call site below to keep passing it.
        assert not batch or table_id, (
            "batch setup requires a pinned table id; an unpinned bridge cross-wires games"
        )

        for sleepwalker_player in game_config.sleepwalker_players:
            log_path = game_dir / f"{sleepwalker_player.name}_mcp.log"
            logger.info(
                "%sSleepwalker (%s) log: %s",
                game_label,
                sleepwalker_player.name,
                log_path,
            )
            # Record the handle. It used to be discarded, which left the sleepwalker bridge
            # invisible to _terminate_session and to wait_for_all_games -- a game that failed
            # setup leaked its bridge JVM. Harmless while one game ran at a time; not once a
            # batch has several in flight.
            session.pilot_procs.append((
                sleepwalker_player.name,
                start_sleepwalker_client(
                    pm,
                    project_root,
                    game_config,
                    sleepwalker_player.name,
                    sleepwalker_player.deck,
                    log_path,
                    table_id=table_id,
                ),
            ))

        for pilot_player in game_config.pilot_players:
            log_path = game_dir / f"{pilot_player.name}_pilot.log"
            logger.info("%sPilot (%s) log: %s", game_label, pilot_player.name, log_path)
            proc = start_pilot_client(
                pm,
                project_root,
                game_config,
                pilot_player,
                log_path,
                game_dir=game_dir,
                table_id=table_id,
            )
            session.pilot_procs.append((pilot_player.name, proc))

        for replay_player in game_config.replay_players:
            log_path = game_dir / f"{replay_player.name}_replay.log"
            logger.info("%sReplay (%s) log: %s", game_label, replay_player.name, log_path)
            proc = start_replay_client(
                pm,
                project_root,
                game_config,
                replay_player.name,
                replay_player.deck,
                replay_player.script,
                log_path,
                game_dir=game_dir,
                table_id=table_id,
            )
            session.pilot_procs.append((replay_player.name, proc))
    except (TimeoutError, RuntimeError):
        _terminate_session(session)
        raise


def await_game_start(session: GameSession, num_games: int) -> None:
    """Block until this game reports that every player has joined.

    Run as its own pass over the batch, after every game has its clients started,
    so these waits overlap too.
    """
    if num_games <= 1:
        return
    bridge_count = (
        len(session.config.sleepwalker_players)
        + len(session.config.pilot_players)
        + len(session.config.replay_players)
    )
    if bridge_count == 0:
        return
    assert session.spectator_proc is not None, "await_game_start requires a launched spectator"
    try:
        wait_for_game_start(session.game_dir / "spectator.log", session.spectator_proc)
    except (TimeoutError, RuntimeError):
        _terminate_session(session)
        raise


def setup_game(
    index: int,
    num_games: int,
    base_config: Config,
    pm: ProcessManager,
    project_root: Path,
    log_dir: Path,
    timestamp: str,
    game_dir: Path | None = None,
    used_player_names: set[str] | None = None,
    cross_game_round_robin: list[tuple[str, ...]] | None = None,
    cross_game_format_picks: list[str] | None = None,
) -> GameSession:
    """Set up one game end to end: launch, attach clients, wait for the start.

    The single-game path. A batch drives the three phases separately so that the
    blocking waits overlap across games.
    """
    session = launch_game(
        index,
        num_games,
        base_config,
        pm,
        project_root,
        log_dir,
        timestamp,
        game_dir=game_dir,
        used_player_names=used_player_names,
        cross_game_round_robin=cross_game_round_robin,
        cross_game_format_picks=cross_game_format_picks,
    )
    attach_game(session, num_games, pm, project_root)
    await_game_start(session, num_games)
    return session


def wait_for_all_games(
    sessions: list[GameSession],
    poll_interval: float = 2.0,
) -> dict[int, int]:
    """Wait for all parallel games to complete."""
    results: dict[int, int] = {}
    active = list(sessions)

    while active:
        time.sleep(poll_interval)
        for session in list(active):
            assert session.spectator_proc is not None

            spectator_rc = session.spectator_proc.poll()
            if spectator_rc is not None:
                if spectator_rc != 0:
                    game_label = f"Game {session.index + 1}"
                    logger.error(
                        "%s: spectator exited with code %s — aborting game.",
                        game_label,
                        spectator_rc,
                    )
                    for _name, pilot_proc in session.pilot_procs:
                        if pilot_proc.poll() is None:
                            kill_tree(pilot_proc.pid)
                results[session.index] = spectator_rc
                active.remove(session)
                continue

            for name, pilot_proc in session.pilot_procs:
                pilot_rc = pilot_proc.poll()
                if pilot_rc is not None and pilot_rc != 0:
                    logger.error(
                        "Game %d: pilot '%s' exited with code %s — aborting game.",
                        session.index + 1,
                        name,
                        pilot_rc,
                    )
                    session.spectator_proc.terminate()
                    for _pilot_name, proc in session.pilot_procs:
                        if proc.poll() is None:
                            kill_tree(proc.pid)
                    results[session.index] = -1
                    active.remove(session)
                    break

    return results


def finalize_game(
    session: GameSession,
    project_root: Path,
    spectator_rc: int,
    *,
    deferred_failures: list[AnnotationFailure] | None = None,
    post_game_failures: list[str] | None = None,
) -> tuple[float, float]:
    """Run post-game processing for a single game session."""
    game_label = f"Game {session.index + 1}: " if session.config.num_games > 1 else ""
    ensure_game_over_event(session.game_dir, spectator_rc)
    write_error_log(session.game_dir)
    try:
        merge_game_log(session.game_dir)
        logger.info("  %sMerged game log: %s", game_label, session.game_dir / "game.jsonl")
    except (OSError, UnicodeError) as exc:
        logger.warning("  %sFailed to merge game log: %s", game_label, exc)

    pilot_cost = print_game_summary(session.game_dir)
    if not session.config.skip_post_game_prompts:
        blunder_cost = upload_and_export(
            session.game_dir,
            project_root,
            deferred_failures=deferred_failures,
            post_game_failures=post_game_failures,
        )
        return pilot_cost, blunder_cost
    return pilot_cost, 0.0
