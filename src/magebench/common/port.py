"""Port availability checking."""

import fcntl
import logging
import os
import socket
import tempfile
import time
from types import TracebackType


class PortReservation:
    """Holds flock-based reservations on one or more ports.

    The locks prevent concurrent processes from selecting the same port.
    Release after the Java server has bound the port.
    """

    def __init__(self, port: int, lock_fds: list[int]) -> None:
        self.port = port
        self._lock_fds = lock_fds

    def release(self) -> None:
        """Release all held locks (idempotent)."""
        for fd in self._lock_fds:
            try:
                os.close(fd)
            except OSError:
                pass
        self._lock_fds.clear()

    def __enter__(self) -> "PortReservation":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        self.release()


def _try_lock_port(port: int) -> int | None:
    """Try to acquire an exclusive flock on a per-port lock file.

    Returns the open file descriptor on success, or None if another
    process already holds the lock.
    """
    lock_path = _lock_path_for_port(port)
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
    except OSError:
        return None
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd
    except OSError:
        os.close(fd)
        return None


logger = logging.getLogger(__name__)


def _lock_path_for_port(port: int) -> str:
    """Return the lock-file path for a reserved port."""
    return os.path.join(tempfile.gettempdir(), f"mage-port-{port}.lock")


def is_port_in_use(host: str, port: int, timeout: float = 1.0) -> bool:
    """Check if a port is in use by attempting to connect (something is listening)."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        result = sock.connect_ex((host, port))
        return result == 0  # Zero means connection succeeded = port in use
    finally:
        sock.close()


def can_bind_port(port: int) -> bool:
    """Check if we can actually bind to a port. More reliable than connect-based
    checks since it detects TIME_WAIT and other states that prevent binding."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("", port))
        return True
    except OSError:
        return False
    finally:
        sock.close()


def _port_attempts(explicit: int | None) -> int:
    """How many offsets to search, with an EXPLICIT environment value winning and saying so.

    Sized by the CALLER, not by a constant here. Each game reserves TWO ports -- `port` and
    `port + 8` -- so an occupied pair blocks two candidate offsets (its own, and the one 8
    below whose secondary it is). At CONC=48 that is ~96 of 100 offsets consumed, which is
    why 92 of 100 ports were in LISTEN on lascar and why job 3200 lost 32% of its launches
    to "No available port found" while ranokau at CONC=32 has never seen it.

    The number therefore has to track CONC, and NOTHING IN THIS PROCESS KNOWS CONC: the
    orchestrator runs one game per process. The runner does know it, so it sets
    MAGEBENCH_PORT_ATTEMPTS and this reads it -- rather than a second literal here that
    must be remembered whenever concurrency changes, which is the defect this codebase has
    now been bitten by three times.
    """
    if explicit is not None:
        return explicit
    raw = os.environ.get("MAGEBENCH_PORT_ATTEMPTS")
    if raw is None:
        logger.info("port search width: 100 (DEFAULT; nothing explicit in the environment)")
        return 100
    value = int(raw)  # a malformed value must raise, never fall back to the default
    if value < 1:
        raise ValueError(f"MAGEBENCH_PORT_ATTEMPTS={value} must be >= 1")
    logger.info("port search width: %d (EXPLICIT, from the environment)", value)
    return value


def find_available_port(start_port: int, max_attempts: int | None = None) -> PortReservation:
    """Find an available port starting from start_port, holding flock reservations.

    Returns a PortReservation that holds exclusive locks on the primary port
    and the secondary port (port+8). Caller must release() the reservation
    after the server has bound the port.
    """
    max_attempts = _port_attempts(max_attempts)
    for offset in range(max_attempts):
        port = start_port + offset
        fd_primary = _try_lock_port(port)
        if fd_primary is None:
            continue
        fd_secondary = _try_lock_port(port + 8)
        if fd_secondary is None:
            os.close(fd_primary)
            continue
        if can_bind_port(port) and can_bind_port(port + 8):
            return PortReservation(port, [fd_primary, fd_secondary])
        os.close(fd_primary)
        os.close(fd_secondary)
    raise RuntimeError(f"No available port found in range {start_port}-{start_port + max_attempts}")


def wait_for_port(host: str, port: int, timeout: int, poll_interval: float = 1.0) -> bool:
    """Wait for a port to become reachable (server started)."""
    start = time.time()
    while time.time() - start < timeout:
        if is_port_in_use(host, port):
            return True
        time.sleep(poll_interval)
    return False
