"""Single-instance lock, held by the kernel for as long as the process lives.

Cron happily starts a second run while the first is still going, and two
instances sharing one SQLite manifest and one archive would interleave
downloads and purges. The lock is a file in the data root, and exclusion comes
from `flock` on a descriptor kept open for the duration of the run.

`flock` rather than a pidfile, because the kernel releases the lock when the
holder dies -- there is no such thing as a stale lock to detect, and therefore
no way for a crash to strand the robot until a human deletes a file. A pidfile
cannot manage that honestly: PIDs are namespace-local and get reused, so a
lock recorded by a process that has since died can name a PID that is alive
again, and the "is the owner still running?" probe then blocks the robot
forever. Inside a container, where the run is often PID 1, that is close to a
certainty rather than a corner case.

This rests on the same requirement the manifest does: `data_root` is on a
filesystem local to the process (§5.1). `flock` is unreliable over NFS and SMB,
which that root must never be.

The JSON payload written after acquiring is diagnostic only -- nothing reads it
to make a decision -- but it is what lets a blocked run say *who* is holding
the lock instead of merely that something is.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import logging
import os
import signal
from pathlib import Path
from types import TracebackType

from .clock import timestamp
from .errors import LockHeldError

log = logging.getLogger(__name__)


class ProcessLock:
    """Context manager holding the run lock for the lifetime of a `with` block."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._fd: int | None = None

    def _read_owner(self) -> dict[str, object] | None:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, ValueError):
            return None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # O_CREAT without O_EXCL: the file persists between runs and carries no
        # meaning on its own. Only the flock does.
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            # Read the payload for the message only. It may be absent or stale
            # if the holder has not written it yet; that is a worse error
            # message, never a wrong decision.
            os.close(fd)
            owner = self._read_owner() or {}
            pid = owner.get("pid", "unknown")
            raise LockHeldError(
                f"another instance is running (pid {pid}); lock held at {self.path}",
                context={"pid": pid, "lock": str(self.path)},
            ) from None

        self._fd = fd
        payload = json.dumps(
            {"pid": os.getpid(), "acquired_at": timestamp(), "host": os.uname().nodename},
            ensure_ascii=False,
        )
        os.ftruncate(fd, 0)
        os.write(fd, payload.encode("utf-8"))
        os.fsync(fd)
        log.debug("lock acquired", extra={"lock": str(self.path), "pid": os.getpid()})

    def release(self) -> None:
        """Drop the lock by closing the descriptor.

        The file is left in place rather than unlinked. Unlinking a flocked
        file is racy -- another process can be holding a lock on an inode that
        no longer has a name, and would then not exclude a third -- and there
        is nothing to clean up anyway, since an unlocked lock file means
        exactly nothing.
        """
        if self._fd is None:
            return
        with contextlib.suppress(OSError):
            os.ftruncate(self._fd, 0)
        with contextlib.suppress(OSError):
            fcntl.flock(self._fd, fcntl.LOCK_UN)
        os.close(self._fd)
        self._fd = None
        log.debug("lock released", extra={"lock": str(self.path)})

    def __enter__(self) -> ProcessLock:
        self.acquire()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.release()


class ShutdownSignal:
    """Cooperative SIGTERM/SIGINT handling.

    The run loop polls `requested` between units of work and stops at the next
    boundary, so the lock is released and the database closed on the way out.
    Any `.part` file left behind is deliberately not cleaned up here: startup
    reconciliation is what decides whether it can be resumed or should be
    dropped, and it has the manifest to decide with.
    """

    def __init__(self) -> None:
        self.requested = False
        self._previous: dict[int, object] = {}

    def _handle(self, signum: int, _frame: object) -> None:
        if self.requested:
            # Second signal: the operator is insisting. Restore default handling
            # so another one terminates the process outright.
            log.warning("second shutdown signal; exiting immediately")
            signal.signal(signum, signal.SIG_DFL)
            os.kill(os.getpid(), signum)
            return
        self.requested = True
        log.warning(
            "shutdown requested; finishing the current step",
            extra={"signal": signal.Signals(signum).name},
        )

    def __enter__(self) -> ShutdownSignal:
        for signum in (signal.SIGTERM, signal.SIGINT):
            self._previous[signum] = signal.getsignal(signum)
            signal.signal(signum, self._handle)
        return self

    def __exit__(self, *_exc: object) -> None:
        for signum, handler in self._previous.items():
            signal.signal(signum, handler)  # type: ignore[arg-type]
        self._previous.clear()
