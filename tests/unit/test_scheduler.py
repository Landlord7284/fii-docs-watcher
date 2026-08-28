"""The container's cron expression parser.

`docker/scheduler.py` is packaging rather than part of the package -- the
pipeline itself stays one-shot -- but the schedule is the one piece of new
logic with real edge cases, so it is tested like anything else.
"""

from __future__ import annotations

import importlib.util
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from fii_docs_watcher import clock
from fii_docs_watcher.clock import DEFAULT_TIMEZONE, set_timezone

_SCHEDULER = Path(__file__).resolve().parents[2] / "docker" / "scheduler.py"


def _load_scheduler():
    spec = importlib.util.spec_from_file_location("_docker_scheduler", _SCHEDULER)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


scheduler = _load_scheduler()
Schedule = scheduler.Schedule
ScheduleError = scheduler.ScheduleError


def _fires_at(expression: str, year: int, month: int, day: int) -> list[str]:
    """Every minute of one day at which `expression` fires."""
    schedule = Schedule.parse(expression)
    tz = ZoneInfo("America/Sao_Paulo")
    hits = []
    for hour in range(24):
        for minute in range(60):
            moment = datetime(year, month, day, hour, minute, tzinfo=tz)
            if schedule.matches(moment):
                hits.append(f"{hour:02d}:{minute:02d}")
    return hits


class TestParsing:
    def test_the_bare_step_form(self) -> None:
        # `0 8/6 * * *` -- what makes 8/6 mean "from 8, every 6 hours" rather
        # than a fraction. It was the shipped default before the profiles
        # existed and is still what a deployment may carry in its .env.
        assert _fires_at("0 8/6 * * *", 2026, 8, 14) == ["08:00", "14:00", "20:00"]

    def test_the_shipped_sweep_fires_once_a_day(self) -> None:
        assert _fires_at(scheduler.DEFAULT_SWEEP_SCHEDULE, 2026, 8, 14) == ["05:10"]

    def test_the_shipped_monitor_fires_through_the_publishing_day(self) -> None:
        hits = _fires_at(scheduler.DEFAULT_MONITOR_SCHEDULE, 2026, 8, 14)
        assert hits[0] == "07:00" and hits[-1] == "23:00" and len(hits) == 17

    def test_a_plain_time(self) -> None:
        assert _fires_at("30 8 * * *", 2026, 8, 14) == ["08:30"]

    def test_a_list_of_hours(self) -> None:
        assert _fires_at("0 8,18 * * *", 2026, 8, 14) == ["08:00", "18:00"]

    def test_a_range_of_hours(self) -> None:
        assert _fires_at("0 8-11 * * *", 2026, 8, 14) == [
            "08:00",
            "09:00",
            "10:00",
            "11:00",
        ]

    def test_a_step_over_the_whole_field(self) -> None:
        assert _fires_at("*/15 9 * * *", 2026, 8, 14) == [
            "09:00",
            "09:15",
            "09:30",
            "09:45",
        ]

    def test_a_step_within_a_range(self) -> None:
        assert _fires_at("0 8-20/6 * * *", 2026, 8, 14) == ["08:00", "14:00", "20:00"]

    def test_a_star_minute_fires_every_minute_of_the_hour(self) -> None:
        assert len(_fires_at("* 3 * * *", 2026, 8, 14)) == 60


class TestDayFields:
    def test_a_weekday_restriction(self) -> None:
        # 2026-08-14 is a Friday; 2026-08-15 a Saturday.
        assert _fires_at("0 9 * * 1-5", 2026, 8, 14) == ["09:00"]
        assert _fires_at("0 9 * * 1-5", 2026, 8, 15) == []

    def test_sunday_is_zero(self) -> None:
        # 2026-08-16 is a Sunday.
        assert _fires_at("0 9 * * 0", 2026, 8, 16) == ["09:00"]
        assert _fires_at("0 9 * * 0", 2026, 8, 14) == []

    def test_a_day_of_month_restriction(self) -> None:
        assert _fires_at("0 9 14 * *", 2026, 8, 14) == ["09:00"]
        assert _fires_at("0 9 14 * *", 2026, 8, 15) == []

    def test_both_day_fields_restricted_is_a_union_not_an_intersection(self) -> None:
        # Cron's long-standing oddity, and a real difference in behaviour:
        # "the 1st, or any Monday". 2026-08-14 is a Friday and not the 1st.
        assert _fires_at("0 9 1 * 1", 2026, 8, 14) == []
        assert _fires_at("0 9 1 * 1", 2026, 8, 17) == ["09:00"]  # a Monday
        assert _fires_at("0 9 1 * 1", 2026, 8, 1) == ["09:00"]  # a Saturday, but the 1st

    def test_a_month_restriction(self) -> None:
        assert _fires_at("0 9 * 8 *", 2026, 8, 14) == ["09:00"]
        assert _fires_at("0 9 * 7 *", 2026, 8, 14) == []


class TestRejections:
    def test_six_fields_are_refused_with_the_five_field_form(self) -> None:
        # Quartz style, seconds first. Guessing which end to drop would
        # silently reschedule the job, so it is refused -- but the message has
        # to hand back the expression that would have worked.
        with pytest.raises(ScheduleError, match="six fields") as caught:
            Schedule.parse("0 0 8/6 * * *")
        assert "'0 8/6 * * *'" in str(caught.value)

    def test_too_few_fields(self) -> None:
        with pytest.raises(ScheduleError, match="expected five"):
            Schedule.parse("8 * *")

    def test_a_value_outside_its_field(self) -> None:
        with pytest.raises(ScheduleError, match="outside 0-23"):
            Schedule.parse("0 25 * * *")

    def test_a_backwards_range(self) -> None:
        with pytest.raises(ScheduleError, match="runs backwards"):
            Schedule.parse("0 18-8 * * *")

    def test_a_zero_step(self) -> None:
        with pytest.raises(ScheduleError, match="positive integer"):
            Schedule.parse("*/0 * * * *")

    def test_something_that_is_not_a_number(self) -> None:
        with pytest.raises(ScheduleError, match="not a number"):
            Schedule.parse("0 eight * * *")

    def test_an_empty_value_in_a_list(self) -> None:
        with pytest.raises(ScheduleError, match="empty value"):
            Schedule.parse("0 8,,18 * * *")


class TestTimezone:
    def test_matching_follows_the_zone_of_the_moment_it_is_given(self) -> None:
        # The loop passes `clock.now()`, which is in the source's zone, so the
        # schedule means the same thing as the directory names. The same
        # instant in another zone is a different wall clock and must not match.
        schedule = Schedule.parse("0 8 * * *")
        instant = datetime(2026, 8, 14, 8, 0, tzinfo=ZoneInfo("America/Sao_Paulo"))
        assert schedule.matches(instant)
        assert not schedule.matches(instant.astimezone(ZoneInfo("UTC")))


class TestTheHostTimezoneIsIrrelevant:
    """`[source].timezone` is the only zone this project declares.

    The scheduler stays independent of the host because it feeds `matches()`
    from `clock.now()` and never consults `TZ`. That is true today only because
    nothing happens to read it -- which is not a guarantee, so these tests run
    the real composition with `TZ` twelve hours away and pin it.
    """

    @pytest.fixture(autouse=True)
    def restore_default(self):
        # Process-wide state; leaving it changed re-dates every later test.
        yield
        set_timezone(DEFAULT_TIMEZONE)

    def test_the_clock_the_loop_reads_is_the_source_clock(
        self, hostile_host_timezone: str
    ) -> None:
        # Reached through `scheduler.clock`, not an import of our own: the
        # point is that the moment *the loop* feeds to `matches()` is anchored
        # to the source, so it is that object which has to be checked.
        set_timezone("America/Sao_Paulo")
        assert scheduler.clock.now().utcoffset() == timedelta(hours=-3)
        # The host really is set to something else, or the assertion above is
        # vacuous and this test would pass with the coupling reintroduced.
        assert time.localtime().tm_gmtoff == 9 * 3600

    def test_the_loop_never_consults_the_host_clock(self) -> None:
        # The two tests above catch a leak through `clock`; this catches one
        # that bypasses it. `TZ`, `tzset` and libc `localtime` have no business
        # in this module, and a bare `datetime.now()` is the easy accident --
        # it returns a naive local time that would match against host wall
        # clock. Cheap to assert, and the only thing standing between a future
        # edit and a schedule that silently fires at the wrong hour.
        source = _SCHEDULER.read_text()
        for forbidden in ('"TZ"', "'TZ'", "tzset", "localtime", "utcnow"):
            assert forbidden not in source, f"{forbidden} reintroduces the host clock"
        assert "datetime.now(" not in source
        assert "date.today(" not in source

    def test_the_schedule_fires_on_source_wall_clock_not_host_wall_clock(
        self, hostile_host_timezone: str
    ) -> None:
        set_timezone("America/Sao_Paulo")
        # 2026-08-14 08:00 in Sao Paulo is 2026-08-14 20:00 in Tokyo: the two
        # zones disagree on the hour, so an implementation that read the host
        # clock would fire at the wrong time and this test would catch it.
        instant = datetime(2026, 8, 14, 11, 0, tzinfo=UTC)
        assert instant.astimezone(clock.source_tz()).hour == 8
        assert instant.astimezone(ZoneInfo("Asia/Tokyo")).hour == 20

        schedule = Schedule.parse("0 8 * * *")
        assert schedule.matches(instant.astimezone(clock.source_tz()))
        assert not schedule.matches(instant.astimezone(ZoneInfo("Asia/Tokyo")))

    def test_the_day_can_differ_between_the_two_zones(
        self, hostile_host_timezone: str
    ) -> None:
        # Late evening in Sao Paulo is already the next day in Tokyo, so a
        # schedule keyed on the day of the month is wrong by a whole date if
        # the host clock ever leaks in.
        set_timezone("America/Sao_Paulo")
        instant = datetime(2026, 8, 15, 1, 0, tzinfo=UTC)
        in_source = instant.astimezone(clock.source_tz())
        in_host = instant.astimezone(ZoneInfo("Asia/Tokyo"))
        assert (in_source.day, in_host.day) == (14, 15)

        schedule = Schedule.parse("0 22 14 * *")
        assert schedule.matches(in_source)
        assert not schedule.matches(in_host)


class TestProfiles:
    """Two schedules, one loop, and a deployment that predates both."""

    @pytest.fixture(autouse=True)
    def clean_environment(self, monkeypatch: pytest.MonkeyPatch):
        for name in (
            "MONITOR_SCHEDULE",
            "MONITOR_ENABLED",
            "SWEEP_SCHEDULE",
            "SWEEP_ENABLED",
            "RUN_SCHEDULE",
            "RUN_ON_START",
        ):
            monkeypatch.delenv(name, raising=False)
        return monkeypatch

    def _due(self, hour: int, minute: int, **schedules) -> str | None:
        parsed = {
            profile: (None if expression is None else Schedule.parse(expression))
            for profile, expression in schedules.items()
        }
        moment = datetime(2026, 8, 14, hour, minute, tzinfo=ZoneInfo("America/Sao_Paulo"))
        return scheduler.due(parsed, moment)

    def test_each_profile_fires_on_its_own_schedule(self) -> None:
        assert self._due(5, 10, sweep="10 5 * * *", monitor="0 7-23 * * *") == "sweep"
        assert self._due(9, 0, sweep="10 5 * * *", monitor="0 7-23 * * *") == "monitor"
        assert self._due(6, 0, sweep="10 5 * * *", monitor="0 7-23 * * *") is None

    def test_the_sweep_wins_a_minute_they_share(self) -> None:
        # It covers every date the monitor would, and the loop is serial, so
        # running both would be one wasted pass over the same days.
        assert self._due(5, 10, sweep="10 5 * * *", monitor="10 5 * * *") == "sweep"

    def test_a_disabled_profile_never_fires(self) -> None:
        assert self._due(9, 0, sweep=None, monitor="0 7-23 * * *") == "monitor"
        assert self._due(9, 0, sweep="10 5 * * *", monitor=None) is None

    def test_each_profile_spawns_the_command_that_belongs_to_it(self) -> None:
        # A profile name on the schedule, never a number: retuning how many
        # days it covers has to stay an edit to config.toml.
        assert scheduler.PROFILE_ARGUMENTS["sweep"] == ("run",)
        assert scheduler.PROFILE_ARGUMENTS["monitor"] == ("run", "--monitor")

    def test_the_enabled_flags_default_to_on(self, clean_environment) -> None:
        assert scheduler._schedule_for("monitor", "", "0 7-23 * * *") is not None

    def test_a_profile_can_be_switched_off(self, clean_environment) -> None:
        clean_environment.setenv("MONITOR_ENABLED", "false")
        assert scheduler._schedule_for("monitor", "", "0 7-23 * * *") is None


class TestTheSweepIsMandatoryUnderTheMonitor:
    """The sweep absorbs every document the monitor's name gate misses.

    Disabling it under an enabled monitor turns those misses from latency into
    losses -- while the archive keeps looking current, because the monitor is
    still filing documents hourly. The scheduler refuses to start that way
    (exit 2) rather than running an archive that quietly stopped being
    complete.
    """

    def _schedules(self, *, sweep: str | None, monitor: str | None):
        return {
            "sweep": None if sweep is None else Schedule.parse(sweep),
            "monitor": None if monitor is None else Schedule.parse(monitor),
        }

    def test_the_monitor_without_the_sweep_is_refused(self) -> None:
        refusal = scheduler.startup_refusal(
            self._schedules(sweep=None, monitor="0 7-23 * * *")
        )
        assert refusal is not None and "backstop" in refusal

    def test_both_profiles_together_start(self) -> None:
        schedules = self._schedules(sweep="10 5 * * *", monitor="0 7-23 * * *")
        assert scheduler.startup_refusal(schedules) is None

    def test_the_sweep_alone_still_starts(self) -> None:
        # The robot as it was before the monitor existed.
        schedules = self._schedules(sweep="10 5 * * *", monitor=None)
        assert scheduler.startup_refusal(schedules) is None

    def test_both_disabled_is_still_refused(self) -> None:
        refusal = scheduler.startup_refusal(self._schedules(sweep=None, monitor=None))
        assert refusal is not None and "disabled" in refusal


class TestTheDeploymentThatPredatesTheProfiles:
    """RUN_SCHEDULE and RUN_ON_START=true are live in somebody's .env.

    What they named -- the whole run, audit included -- is now the sweep. They
    are read rather than ignored, because silently rescheduling a running
    archive is the failure this project refuses everywhere else.
    """

    @pytest.fixture(autouse=True)
    def clean_environment(self, monkeypatch: pytest.MonkeyPatch):
        for name in ("SWEEP_SCHEDULE", "SWEEP_ENABLED", "RUN_SCHEDULE", "RUN_ON_START"):
            monkeypatch.delenv(name, raising=False)
        return monkeypatch

    def test_run_schedule_is_read_as_the_sweep(self, clean_environment) -> None:
        clean_environment.setenv("RUN_SCHEDULE", "0 8/6 * * *")
        schedule = scheduler._sweep_schedule()
        assert schedule is not None and schedule.text == "0 8/6 * * *"

    def test_the_new_name_wins_when_both_agree(self, clean_environment) -> None:
        clean_environment.setenv("RUN_SCHEDULE", "0 6 * * *")
        clean_environment.setenv("SWEEP_SCHEDULE", "0 6 * * *")
        assert scheduler._sweep_schedule().text == "0 6 * * *"

    def test_two_disagreeing_answers_are_refused(self, clean_environment) -> None:
        # Not resolved by preference: an operator who set both believes one of
        # them, and picking silently is how a schedule drifts.
        clean_environment.setenv("RUN_SCHEDULE", "0 6 * * *")
        clean_environment.setenv("SWEEP_SCHEDULE", "0 7 * * *")
        with pytest.raises(scheduler.ScheduleError, match="retired spelling"):
            scheduler._sweep_schedule()

    def test_the_default_applies_when_neither_is_set(self, clean_environment) -> None:
        assert scheduler._sweep_schedule().text == scheduler.DEFAULT_SWEEP_SCHEDULE

    def test_run_on_start_defaults_to_the_sweep(self, clean_environment) -> None:
        # A start usually follows a restart, an update or downtime -- the gap
        # case, which the monitor's narrow window cannot see.
        assert scheduler._run_on_start() == "sweep"

    def test_the_boolean_spellings_still_mean_what_they_meant(
        self, clean_environment
    ) -> None:
        clean_environment.setenv("RUN_ON_START", "true")
        assert scheduler._run_on_start() == "sweep"
        clean_environment.setenv("RUN_ON_START", "false")
        assert scheduler._run_on_start() == "none"

    def test_a_profile_name_is_taken_as_written(self, clean_environment) -> None:
        clean_environment.setenv("RUN_ON_START", "monitor")
        assert scheduler._run_on_start() == "monitor"
        clean_environment.setenv("RUN_ON_START", "none")
        assert scheduler._run_on_start() == "none"

    def test_anything_else_is_refused(self, clean_environment) -> None:
        clean_environment.setenv("RUN_ON_START", "hourly")
        with pytest.raises(scheduler.ScheduleError, match="sweep, monitor or none"):
            scheduler._run_on_start()
