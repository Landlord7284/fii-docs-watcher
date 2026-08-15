"""The container's cron expression parser.

`docker/scheduler.py` is packaging rather than part of the package -- the
pipeline itself stays one-shot -- but the schedule is the one piece of new
logic with real edge cases, so it is tested like anything else.
"""

from __future__ import annotations

import importlib.util
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

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
    def test_the_default_fires_three_times_a_day(self) -> None:
        # `0 8/6 * * *` -- the bare-step form, which is what makes 8/6 mean
        # "from 8, every 6 hours" rather than a fraction.
        assert _fires_at(scheduler.DEFAULT_SCHEDULE, 2026, 8, 14) == ["08:00", "14:00", "20:00"]

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
