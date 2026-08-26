"""Periodic driver for the one-shot pipeline.

This lives outside the package on purpose. `fii_docs_watcher` is strictly
one-shot -- do the work, exit with a meaningful code -- and §7 requires that
anything periodic be packaging built on top of that, never the other way round.
So this module never imports the pipeline; it spawns `python -m
fii_docs_watcher run` as a child process, exactly as a scheduler on the host
would, which also means a crash inside a run cannot take the loop down with it.

It reads the configuration only to learn the timezone. Schedules are evaluated
in `clock.source_tz()` -- the same zone the archive is dated by -- so "08:00"
means the same instant here as it does in a directory name, whatever the
container's own idea of local time. Nothing here consults `TZ`, and nothing
should: `[source].timezone` is the only place the project declares a zone.
`tests/unit/test_scheduler.py` pins that by running the match under a `TZ` set
twelve hours away, so reintroducing the coupling fails a test rather than
quietly shifting when every run fires.

Matching is done by waking at the top of every minute and asking whether the
current minute satisfies the expression. That is a great deal simpler than
computing the next firing instant, and it has no DST edge cases to get wrong:
there is no arithmetic across a transition, only a comparison of the fields of
whatever time it currently is.
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime

from fii_docs_watcher import clock, config
from fii_docs_watcher.errors import ConfigError, WatcherError
from fii_docs_watcher.logging_setup import configure

log = logging.getLogger("fii_docs_watcher.scheduler")

DEFAULT_SCHEDULE = "0 8/6 * * *"

# minute, hour, day-of-month, month, day-of-week -- the standard five, in the
# standard order, with the ranges Vixie cron uses.
_FIELD_BOUNDS = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 6))
_FIELD_NAMES = ("minute", "hour", "day-of-month", "month", "day-of-week")


class ScheduleError(ValueError):
    """The cron expression could not be understood."""


def _parse_field(spec: str, low: int, high: int, name: str) -> frozenset[int]:
    """Expand one cron field into the set of values it matches.

    Accepts `*`, `a`, `a-b`, `a,b,c`, `*/n`, `a-b/n` and the bare-step form
    `a/n`, which several cron implementations read as `a-<high>/n` and which is
    what makes `8/6` mean 8, 14 and 20.
    """
    values: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            raise ScheduleError(f"empty value in the {name} field")

        body, slash, step_text = part.partition("/")
        step = 1
        if slash:
            if not step_text.isdigit() or int(step_text) < 1:
                raise ScheduleError(f"step must be a positive integer in {part!r} ({name})")
            step = int(step_text)

        if body == "*":
            start, stop = low, high
        elif "-" in body:
            start_text, _, stop_text = body.partition("-")
            start, stop = _as_int(start_text, name), _as_int(stop_text, name)
        else:
            start = _as_int(body, name)
            # A bare step counts up to the top of the field; a bare value is
            # itself.
            stop = high if step > 1 else start

        if not (low <= start <= high and low <= stop <= high):
            raise ScheduleError(f"{part!r} is outside {low}-{high} ({name})")
        if start > stop:
            raise ScheduleError(f"{part!r} runs backwards ({name})")
        values.update(range(start, stop + 1, step))

    return frozenset(values)


def _as_int(text: str, name: str) -> int:
    try:
        return int(text.strip())
    except ValueError:
        raise ScheduleError(f"{text.strip()!r} is not a number ({name})") from None


@dataclass(frozen=True)
class Schedule:
    """A parsed 5-field cron expression."""

    minutes: frozenset[int]
    hours: frozenset[int]
    days: frozenset[int]
    months: frozenset[int]
    weekdays: frozenset[int]
    restricts_day: bool
    restricts_weekday: bool
    text: str

    @classmethod
    def parse(cls, expression: str) -> Schedule:
        fields = expression.split()
        if len(fields) == 6:
            # Quartz-style, seconds first. Rejected rather than reinterpreted:
            # guessing which end to drop would silently reschedule the job.
            raise ScheduleError(
                f"{expression!r} has six fields; this takes the standard five "
                f"(minute hour day-of-month month day-of-week). Sub-minute "
                f"scheduling is meaningless here -- a single request to the "
                f"source can take 60 seconds. Drop the leading seconds field: "
                f"{' '.join(fields[1:])!r}"
            )
        if len(fields) != 5:
            raise ScheduleError(
                f"{expression!r} has {len(fields)} field(s); expected five "
                f"(minute hour day-of-month month day-of-week)"
            )

        expanded = [
            _parse_field(field, low, high, name)
            for field, (low, high), name in zip(fields, _FIELD_BOUNDS, _FIELD_NAMES, strict=True)
        ]
        return cls(
            minutes=expanded[0],
            hours=expanded[1],
            days=expanded[2],
            months=expanded[3],
            weekdays=expanded[4],
            restricts_day=fields[2].strip() != "*",
            restricts_weekday=fields[4].strip() != "*",
            text=expression,
        )

    def matches(self, moment: datetime) -> bool:
        if moment.minute not in self.minutes or moment.hour not in self.hours:
            return False
        if moment.month not in self.months:
            return False

        # Sunday is 0 in cron and 6 in Python's weekday().
        weekday = (moment.weekday() + 1) % 7
        day_ok = moment.day in self.days
        weekday_ok = weekday in self.weekdays

        # Cron's long-standing oddity: when both day fields are restricted the
        # match is a union, not an intersection.
        if self.restricts_day and self.restricts_weekday:
            return day_ok or weekday_ok
        return day_ok and weekday_ok


def _env_flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


class Runner:
    """Spawns the pipeline and forwards shutdown to it."""

    def __init__(self) -> None:
        self.stopping = False
        self._child: subprocess.Popen[bytes] | None = None

    def install_signal_handlers(self) -> None:
        for signum in (signal.SIGTERM, signal.SIGINT):
            signal.signal(signum, self._handle)

    def _handle(self, signum: int, _frame: object) -> None:
        self.stopping = True
        name = signal.Signals(signum).name
        if self._child is not None and self._child.poll() is None:
            # The pipeline stops at its next step boundary, releases the lock
            # and closes the database. Let it; do not kill it.
            log.warning("%s received; asking the running pipeline to stop", name)
            self._child.send_signal(signum)
        else:
            log.warning("%s received; stopping", name)

    def run_once(self) -> int:
        log.info("starting a run")
        started = time.monotonic()
        self._child = subprocess.Popen([sys.executable, "-m", "fii_docs_watcher", "run"])
        try:
            code = self._child.wait()
        finally:
            self._child = None

        elapsed = time.monotonic() - started
        # A non-zero exit is reported and then forgotten: isolated failures are
        # normal, and the next tick is the retry.
        level = logging.INFO if code == 0 else logging.WARNING
        log.log(level, "run finished", extra={"exit_code": code, "seconds": round(elapsed, 1)})
        return code

    def sleep_until_next_minute(self) -> None:
        """Wait for the top of the next minute, in slices, so signals land."""
        while not self.stopping:
            now = clock.now()
            remaining = 60.0 - (now.second + now.microsecond / 1_000_000)
            if remaining <= 0:
                return
            time.sleep(min(remaining, 1.0))
            if remaining <= 1.0:
                return


def main() -> int:
    try:
        loaded = config.load()
    except ConfigError as exc:
        # Logging is not configured yet, so say it on stderr directly.
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2
    configure(loaded.logging)

    try:
        schedule = Schedule.parse(os.environ.get("RUN_SCHEDULE") or DEFAULT_SCHEDULE)
    except ScheduleError as exc:
        log.error("RUN_SCHEDULE is not usable: %s", exc)
        return 2

    runner = Runner()
    runner.install_signal_handlers()
    log.info(
        "scheduler started",
        extra={
            "schedule": schedule.text,
            "timezone": str(clock.source_tz()),
            "config": config.describe_source(loaded),
        },
    )

    if _env_flag("RUN_ON_START", default=True):
        runner.run_once()

    last_fired: str | None = None
    while not runner.stopping:
        runner.sleep_until_next_minute()
        if runner.stopping:
            break

        now = clock.now()
        stamp = now.strftime("%Y-%m-%dT%H:%M")
        # A run can outlast its own slot; without this the minute it finishes
        # in could fire it a second time.
        if stamp == last_fired or not schedule.matches(now):
            continue
        last_fired = stamp
        try:
            runner.run_once()
        except WatcherError:
            # run_once only spawns a process, but a loop that dies on an
            # unexpected error would stop the archive silently.
            log.exception("the run could not be started; continuing")

    log.info("scheduler stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
