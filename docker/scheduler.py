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

There are two profiles, each with a schedule of its own: the daily `sweep`,
which covers `[discovery].days` and runs the global audit, and the frequent
`monitor`, which covers the narrower `[discovery].monitor_days` and does not.
A schedule says how often to look and a window says how far back, which are
different questions -- so schedules live here, in the environment, and windows
live in `config.toml`. A crontab entry names a profile and never a number, and
retuning coverage stays a configuration edit.

The loop is serial: it spawns one run and waits for it. A firing that lands
while a run is still going is skipped rather than queued, so the two profiles
never contend for the lock, and a monitor missed under a long sweep costs
nothing -- its window overlaps the next firing's.

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

#: The frequent profile, hourly through the publishing day.
DEFAULT_MONITOR_SCHEDULE = "0 7-23 * * *"

#: The full sweep, once a day and early, so the audit and the wider window land
#: before anyone opens the archive.
DEFAULT_SWEEP_SCHEDULE = "10 5 * * *"

#: Retired in favour of the two above. Still read, because a deployment that
#: was configured before the profiles existed must not be silently rescheduled.
LEGACY_SCHEDULE_VAR = "RUN_SCHEDULE"

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


#: What each profile costs the CLI, and the argument that says so. The monitor
#: also declines the global audit, but that follows from the profile inside the
#: pipeline rather than from a second flag here.
PROFILE_ARGUMENTS = {"sweep": ("run",), "monitor": ("run", "--monitor")}


def _env_flag(name: str, default: bool) -> bool:
    """Read a boolean variable, defaulting only when it is unset or blank."""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _schedule_for(profile: str, expression: str, fallback: str) -> Schedule | None:
    """The schedule one profile fires on, or None when it is switched off.

    `<PROFILE>_ENABLED=false` drops the profile entirely rather than pushing it
    somewhere it will never fire, so a disabled profile is visible in the log
    as absent instead of having to be inferred from an odd expression.
    """
    if not _env_flag(f"{profile.upper()}_ENABLED", default=True):
        return None
    return Schedule.parse(expression or fallback)


def _sweep_schedule() -> Schedule | None:
    """The sweep's schedule, honouring the variable this predates.

    `RUN_SCHEDULE` was the only schedule before the profiles existed, and what
    it named -- the whole run, audit included -- is now the sweep. A deployment
    already carrying it in a `.env` must keep firing when it always has, so it
    is read rather than ignored, and said out loud rather than read silently.
    Set alongside a differing `SWEEP_SCHEDULE` it is refused: two answers to
    one question is how a schedule drifts away from what its operator believes.
    """
    legacy = (os.environ.get(LEGACY_SCHEDULE_VAR) or "").strip()
    declared = (os.environ.get("SWEEP_SCHEDULE") or "").strip()
    if legacy and declared and legacy != declared:
        raise ScheduleError(
            f"{LEGACY_SCHEDULE_VAR}={legacy!r} and SWEEP_SCHEDULE={declared!r} disagree; "
            f"{LEGACY_SCHEDULE_VAR} is the retired spelling of the same setting, so drop it"
        )
    if legacy and not declared:
        log.warning(
            "%s is retired; read as SWEEP_SCHEDULE for now, but rename it",
            LEGACY_SCHEDULE_VAR,
            extra={"schedule": legacy},
        )
        declared = legacy
    return _schedule_for("sweep", declared, DEFAULT_SWEEP_SCHEDULE)


def _run_on_start() -> str:
    """Which profile a container start runs, if any: `sweep`, `monitor` or `none`.

    The sweep, by default. A start usually follows a restart, an image update
    or downtime -- the gap case exactly, and the one the monitor's narrow
    window cannot see.

    `true` and `false` are the spellings this predates and still mean what they
    meant, mapped to `sweep` and `none`: an operator who wrote `true` asked for
    a catch-up run, and that run is the sweep.
    """
    raw = (os.environ.get("RUN_ON_START") or "").strip().lower()
    if not raw:
        return "sweep"
    legacy = {"1": "sweep", "true": "sweep", "yes": "sweep", "on": "sweep",
              "0": "none", "false": "none", "no": "none", "off": "none"}
    if raw in legacy:
        log.warning(
            "RUN_ON_START=%s is the retired spelling; read as %r, but write the profile",
            raw,
            legacy[raw],
        )
        return legacy[raw]
    if raw not in PROFILE_ARGUMENTS and raw != "none":
        raise ScheduleError(
            f"RUN_ON_START must be sweep, monitor or none, not {raw!r}"
        )
    return raw


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

    def run_once(self, profile: str = "sweep") -> int:
        """Spawn one run of one profile and wait for it.

        Waiting is what keeps the two profiles from ever contending for the
        lock: a firing that lands mid-run is skipped by the loop rather than
        queued behind this one.
        """
        arguments = PROFILE_ARGUMENTS[profile]
        log.info("starting a run", extra={"profile": profile})
        started = time.monotonic()
        self._child = subprocess.Popen([sys.executable, "-m", "fii_docs_watcher", *arguments])
        try:
            code = self._child.wait()
        finally:
            self._child = None

        elapsed = time.monotonic() - started
        # A non-zero exit is reported and then forgotten: isolated failures are
        # normal, and the next tick is the retry.
        level = logging.INFO if code == 0 else logging.WARNING
        log.log(
            level,
            "run finished",
            extra={"profile": profile, "exit_code": code, "seconds": round(elapsed, 1)},
        )
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


def due(schedules: dict[str, Schedule | None], moment: datetime) -> str | None:
    """Which profile this minute calls for, if either.

    When both match the same minute the sweep wins and the monitor is dropped:
    the sweep covers every date the monitor would have, so running both would
    be one wasted pass over the same days -- and the loop is serial, so the
    second would only start after the first finished anyway.
    """
    for profile in ("sweep", "monitor"):
        schedule = schedules.get(profile)
        if schedule is not None and schedule.matches(moment):
            return profile
    return None


def main() -> int:
    try:
        loaded = config.load()
    except ConfigError as exc:
        # Logging is not configured yet, so say it on stderr directly.
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2
    configure(loaded.logging)

    try:
        schedules = {
            "sweep": _sweep_schedule(),
            "monitor": _schedule_for(
                "monitor",
                (os.environ.get("MONITOR_SCHEDULE") or "").strip(),
                DEFAULT_MONITOR_SCHEDULE,
            ),
        }
        on_start = _run_on_start()
    except ScheduleError as exc:
        log.error("the schedule is not usable: %s", exc)
        return 2

    if not any(schedules.values()):
        # Both profiles off is a container that would run nothing while looking
        # like it works, which is worse than refusing to start.
        log.error("both profiles are disabled; this scheduler would never run anything")
        return 2

    runner = Runner()
    runner.install_signal_handlers()
    log.info(
        "scheduler started",
        extra={
            "sweep": schedules["sweep"].text if schedules["sweep"] else "disabled",
            "monitor": schedules["monitor"].text if schedules["monitor"] else "disabled",
            "run_on_start": on_start,
            "timezone": str(clock.source_tz()),
            "config": config.describe_source(loaded),
        },
    )

    if on_start != "none":
        runner.run_once(on_start)

    last_fired: str | None = None
    while not runner.stopping:
        runner.sleep_until_next_minute()
        if runner.stopping:
            break

        now = clock.now()
        stamp = now.strftime("%Y-%m-%dT%H:%M")
        # A run can outlast its own slot; without this the minute it finishes
        # in could fire it a second time.
        if stamp == last_fired:
            continue
        profile = due(schedules, now)
        if profile is None:
            continue
        last_fired = stamp
        try:
            runner.run_once(profile)
        except WatcherError:
            # run_once only spawns a process, but a loop that dies on an
            # unexpected error would stop the archive silently.
            log.exception("the run could not be started; continuing")

    log.info("scheduler stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
