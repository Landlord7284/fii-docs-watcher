"""Single-instance lock, with stale-lock detection.

Cron happily starts a second run while the first is still going, and two
instances sharing one SQLite manifest and one archive would interleave
downloads and purges. The lock is a file in the data root holding the owning
PID, so a crashed run leaves evidence rather than a permanent blockade.

`O_CREAT | O_EXCL` gives the atomic acquire. The PID inside is what lets a
later run distinguish "still running" from "died without cleaning up" -- a
zero-byte lockfile would strand the robot until a human deleted it.
"""

from __future__ import annotations

import contextlib
import errno
import json
import logging
import os
import signal
from pathlib import Path
from types import TracebackType

from .clock import timestamp
from .errors import LockHeldError

log = logging.getLogger(__name__)


def _process_alive(pid: int) -> bool:
    """Is `pid` a live process we could signal?

    Signal 0 performs the permission and existence checks without delivering
    anything. EPERM means the process exists but belongs to another user, which
    still counts as alive.
    """
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError as exc:  # pragma: no cover - platform dependent
        return exc.errno != errno.ESRCH
    return True


class ProcessLock:
    """Context manager holding the run lock for the lifetime of a `with` block."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._acquired = False

    def _read_owner(self) -> dict[str, object] | None:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            {"pid": os.getpid(), "acquired_at": timestamp(), "host": os.uname().nodename},
            ensure_ascii=False,
        )
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            owner = self._read_owner()
            pid = int(owner.get("pid", 0)) if owner else 0
            if owner is None or not _process_alive(pid):
                # The previous run died. Reclaim rather than block forever.
                log.warning(
                    "removing stale lock file",
                    extra={"lock": str(self.path), "stale_pid": pid or "unknown"},
                )
                # Another instance may reclaim it first; the retry below then
                # loses that race cleanly and reports the lock as held.
                with contextlib.suppress(FileNotFoundError):
                    self.path.unlink()
                return self.acquire()
            raise LockHeldError(
                f"another instance is running (pid {pid}); lock held at {self.path}",
                context={"pid": pid, "lock": str(self.path)},
            ) from None
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
        self._acquired = True
        log.debug("lock acquired", extra={"lock": str(self.path), "pid": os.getpid()})

    def release(self) -> None:
        """Release the lock, but only if we still own it.

        Checking ownership matters: if this run was declared stale and another
        instance reclaimed the file, deleting it here would hand a third
        instance a lock the second one still believes it holds.
        """
        if not self._acquired:
            return
        owner = self._read_owner()
        if owner is not None and int(owner.get("pid", -1)) != os.getpid():
            log.warning("lock file was taken over by another process; not removing it")
        else:
            with contextlib.suppress(FileNotFoundError):
                self.path.unlink()
        self._acquired = False
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
