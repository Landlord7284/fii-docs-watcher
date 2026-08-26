"""Log formatting -- specifically, which clock the timestamps come from."""

from __future__ import annotations

import json
import logging
import time
from datetime import UTC, datetime

import pytest

from fii_docs_watcher.clock import DEFAULT_TIMEZONE, set_timezone, source_tz
from fii_docs_watcher.logging_setup import _JsonFormatter, _TextFormatter


def _record() -> logging.LogRecord:
    return logging.LogRecord(
        name="fii_docs_watcher.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="something happened",
        args=None,
        exc_info=None,
    )


def _offset_for(record: logging.LogRecord) -> str:
    return datetime.fromtimestamp(record.created, tz=source_tz()).strftime("%z")


class TestTimestampsFollowTheSource:
    """The host timezone must not reach the logs.

    A container left in UTC would otherwise stamp an event at one date while
    filing the document under another, for the very same event.
    """

    @pytest.fixture(autouse=True)
    def restore_default(self):
        # Process-wide state; leaving it changed re-dates every later test.
        yield
        set_timezone(DEFAULT_TIMEZONE)

    def test_text_timestamps_carry_the_source_offset(self) -> None:
        record = _record()
        rendered = _TextFormatter().format(record)
        assert _offset_for(record) in rendered

    def test_json_timestamps_carry_the_source_offset(self) -> None:
        record = _record()
        payload = json.loads(_JsonFormatter().format(record))
        assert payload["ts"].endswith(_offset_for(record))

    def test_changing_the_zone_changes_the_stamp(self) -> None:
        record = _record()

        set_timezone("UTC")
        in_utc = _TextFormatter().format(record)
        set_timezone("Asia/Tokyo")
        in_tokyo = _TextFormatter().format(record)

        assert "+0000" in in_utc
        assert "+0900" in in_tokyo
        assert in_utc != in_tokyo

    def test_the_zone_is_read_per_call_not_bound_at_import(self) -> None:
        # A formatter built before the configuration is loaded still has to
        # honour the zone installed afterwards -- `config.load()` calls
        # `set_timezone()` after this module has long been imported.
        formatter = _TextFormatter()
        set_timezone("UTC")
        assert "+0000" in formatter.format(_record())

    def test_a_hostile_host_zone_does_not_reach_the_stamp(
        self, hostile_host_timezone: str
    ) -> None:
        # The tests above never set `TZ`, so they would pass even if
        # `formatTime` fell back to libc `localtime`. This one arms that trap:
        # the host says +0900 while the source says -0300, and a leak shows up
        # as the wrong offset on every line.
        set_timezone("America/Sao_Paulo")
        record = _record()

        assert "-0300" in _TextFormatter().format(record)
        assert json.loads(_JsonFormatter().format(record))["ts"].endswith("-0300")
        # Vacuity guard: without this the host may simply have been -0300 too.
        assert time.localtime().tm_gmtoff == 9 * 3600

    def test_a_hostile_host_zone_does_not_shift_the_date(
        self, hostile_host_timezone: str
    ) -> None:
        # The failure this whole arrangement exists to prevent: an event logged
        # under one date while the document it describes is filed under another.
        set_timezone("America/Sao_Paulo")
        record = _record()
        record.created = datetime(
            2026, 8, 15, 1, 30, tzinfo=UTC
        ).timestamp()

        stamp = _TextFormatter().format(record)
        assert "2026-08-14" in stamp  # 22:30 in Sao Paulo
        assert "2026-08-15" not in stamp  # 10:30 in Tokyo, the wrong day
