"""Port availability checking."""

import fcntl
import os
import socket
import tempfile
import time
from types import TracebackType

from magebench.common.log import get_logger


logger = get_logger(__name__)

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


def find_available_port(start_port: int, max_attempts: int = 100) -> PortReservation:
    """Find an available port starting from start_port, holding flock reservations.

    Returns a PortReservation that holds exclusive locks on the primary port
    and the secondary port (port+8). Caller must release() the reservation
    after the server has bound the port.
    """
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


# ---------------------------------------------------------------- displays


def _lock_path_for_display(display: int) -> str:
    """Lock-file path for a reserved X display."""
    return os.path.join(tempfile.gettempdir(), f"mage-display-{display}.lock")


def _try_lock_display(display: int) -> int | None:
    """Same flock as a port, on a per-display file."""
    try:
        fd = os.open(_lock_path_for_display(display), os.O_CREAT | os.O_RDWR, 0o644)
    except OSError:
        return None
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd
    except OSError:
        os.close(fd)
        return None


def reserve_display(preferred: int, max_attempts: int = 64) -> PortReservation:
    """Reserve an X display, starting at `preferred` and stepping up if held.

    THE DISPLAY HAD NO RESERVATION AT ALL, and that is what made one orphan
    expensive. sequential_batch derived it as `200 + (port - 17171)` and used it
    unchecked, so a leftover Xvfb on that number failed every later worker drawing
    that port -- measured on block 2: port 17206 -> display 235, eight workers,
    eight failures, while every sibling port in the same window was fine.

    THE SAME MECHANISM AS THE PORT, deliberately, rather than a second one: an
    flock on a per-display file, held until the caller releases it. Two allocators
    with different semantics is how the two bands came to overlap in the first
    place.

    Held displays are STEPPED PAST AND LOGGED, never skipped silently -- a
    reservation that quietly hands out a different number than the caller asked
    for is the no-silent-fallback rule's territory, and the skip is the evidence
    that an orphan is out there.

    A live X server whose lock we do not hold is also treated as taken: the flock
    is ours and /tmp/.X<n>-lock is X's, and only checking both covers an Xvfb
    started outside this mechanism -- which, until this function existed, was all
    of them.
    """
    for offset in range(max_attempts):
        display = preferred + offset
        if os.path.exists(f"/tmp/.X{display}-lock"):
            logger.warning(
                "display :%d is held by a running X server; trying :%d",
                display, display + 1,
            )
            continue
        fd = _try_lock_display(display)
        if fd is None:
            logger.warning(
                "display :%d is reserved by another session; trying :%d",
                display, display + 1,
            )
            continue
        if display != preferred:
            logger.warning(
                "display :%d was taken; reserved :%d instead (an orphaned Xvfb "
                "on :%d would otherwise fail every worker drawing this port)",
                preferred, display, preferred,
            )
        return PortReservation(display, [fd])
    raise RuntimeError(
        f"no free X display in {preferred}..{preferred + max_attempts - 1}. "
        f"Every one is held by a running X server or reserved by another "
        f"session; reap orphaned Xvfb processes before retrying."
    )
