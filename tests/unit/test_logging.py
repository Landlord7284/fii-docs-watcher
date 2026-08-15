"""Log formatting -- specifically, which clock the timestamps come from."""

from __future__ import annotations

import json
import logging
from datetime import datetime

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
