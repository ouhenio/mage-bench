"""Launch a bridge JVM and hand back a synchronous session to it.

The pilot's `spawn_bridge_http` does the same launch but yields an ASYNC MCP SDK
session inside an `async with`. The adapter serves HTTP from threads and drives
the seat from another, so it wants the JVM's lifetime bound to an object rather
than to a coroutine's scope, and a sync session it can call from any thread.

The JVM launch itself is NOT duplicated: `build_bridge_launch_args` is the one
place that knows the `-Dxmage.bridge.*` properties, and it is imported.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from magebench.common.bridge_session import BridgeSession
from magebench.common.log import get_logger
from magebench.common.port import find_available_port, wait_for_port
from magebench.common.process_manager import jvm_oom_preexec_fn, kill_tree
from magebench.pilot.bridge_transport import build_bridge_launch_args

logger = get_logger(__name__)

_MCP_PORT_START = 19000
_BRIDGE_STARTUP_TIMEOUT_SECS = 120


class BridgeProcess:
    """A bridge JVM plus the sync session that talks to it."""

    def __init__(
        self,
        *,
        server: str,
        port: int,
        username: str,
        project_root: Path,
        deck_path: Path | None = None,
        table_id: str | None = None,
        heap_size_mb: int = 512,
        log_file: Path | None = None,
        bridge_log_path: Path | None = None,
        error_log_path: Path | None = None,
    ) -> None:
        self._server = server
        self._port = port
        self._username = username
        self._project_root = project_root
        self._deck_path = deck_path
        self._table_id = table_id
        self._heap_size_mb = heap_size_mb
        self._log_file = log_file
        self._bridge_log_path = bridge_log_path
        self._error_log_path = error_log_path
        self._proc: subprocess.Popen | None = None
        self._log_fh = None
        self.session: BridgeSession | None = None
        self.mcp_port: int | None = None

    @property
    def username(self) -> str:
        return self._username

    def start(self) -> BridgeSession:
        launch = build_bridge_launch_args(
            server=self._server,
            port=self._port,
            username=self._username,
            deck_path=self._deck_path,
            heap_size_mb=self._heap_size_mb,
            table_id=self._table_id,
            error_log_path=self._error_log_path,
            bridge_log_path=self._bridge_log_path,
        )

        reservation = find_available_port(_MCP_PORT_START)
        mcp_port = reservation.port
        env = os.environ.copy()
        env["MAVEN_OPTS"] = f"{launch.jvm_args} -Dxmage.bridge.mcpPort={mcp_port}"

        self._log_fh = open(self._log_file, "w") if self._log_file else None
        # MAVEN_REPO_LOCAL is honoured here. `spawn_bridge_http` does not pass it, so a
        # bridge launched by the pilot runs the SHARED jars even from a worktree built
        # against an isolated repository -- exactly the "compiled, installed, and never
        # loaded" failure game_processes.MVN_REPO_ARGS warns about. Reported separately;
        # this seat does not reproduce it.
        repo_args = (
            [f"-Dmaven.repo.local={os.environ['MAVEN_REPO_LOCAL']}"]
            if os.environ.get("MAVEN_REPO_LOCAL")
            else []
        )
        self._proc = subprocess.Popen(
            ["mvn", *repo_args, *launch.mvn_args],
            cwd=str(self._project_root / "Mage.Client.Bridge"),
            stdin=subprocess.PIPE,
            stdout=self._log_fh or subprocess.DEVNULL,
            stderr=subprocess.STDOUT,
            env=env,
            preexec_fn=jvm_oom_preexec_fn(),
        )
        logger.info("[bridge] %s JVM started (pid=%s), waiting for MCP on %s",
                    self._username, self._proc.pid, mcp_port)

        # 127.0.0.1 rather than "localhost": the JDK HttpServer binds IPv4 only.
        if not wait_for_port("127.0.0.1", mcp_port, _BRIDGE_STARTUP_TIMEOUT_SECS):
            rc = self._proc.poll()
            tail = ""
            if self._log_file and Path(self._log_file).exists():
                tail = f"\nLog tail:\n{Path(self._log_file).read_text()[-2000:]}"
            self.stop()
            raise RuntimeError(
                f"Bridge MCP HTTP server did not start on port {mcp_port} within "
                f"{_BRIDGE_STARTUP_TIMEOUT_SECS}s (rc={rc}){tail}"
            )
        reservation.release()

        self.mcp_port = mcp_port
        self.session = BridgeSession(f"http://127.0.0.1:{mcp_port}/mcp")
        self.session.initialize()
        logger.info("[bridge] %s MCP initialized on %s", self._username, mcp_port)
        return self.session

    def is_healthy(self) -> bool:
        if self.session is None:
            return False
        return self.session.is_responsive(timeout=5)

    def stop(self) -> None:
        if self.session is not None:
            self.session.close()
            self.session = None
        if self._proc is not None:
            if self._proc.stdin:
                try:
                    self._proc.stdin.close()
                except (OSError, ValueError):
                    pass
            try:
                self._proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                kill_tree(self._proc.pid)
            self._proc = None
        if self._log_fh is not None:
            self._log_fh.close()
            self._log_fh = None
