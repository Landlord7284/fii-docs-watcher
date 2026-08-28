"""The monitor's newest-first read: the stop rule and its integrity checks.

An early-stopped descending read cannot assert distinct identities against
`recordsFiltered` -- the check that caught the ascending scan's silent row loss
-- so order validation is its substitute. These tests pin both halves: the stop
rule never splits a minute tie group, and any violation of the descending
contract aborts the read rather than returning rows that might be missing
their neighbours.
"""

from __future__ import annotations

import sys
from datetime import timedelta
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from conftest import FakeFnet, make_row
from fii_docs_watcher.clock import parse_delivery, today
from fii_docs_watcher.config import SourceConfig
from fii_docs_watcher.fnet.client import FnetClient
from fii_docs_watcher.fnet.listing import scan_newest

DESC_PARAM = "o%5B0%5D%5BdataEntrega%5D=desc"


def _client(transport: httpx.BaseTransport) -> FnetClient:
    config = SourceConfig(
        base_url="https://fnet.test/fnet/publico",
        min_request_interval_seconds=0.0,
        backoff_base_seconds=0.0,
        backoff_max_seconds=0.0,
        max_retries=2,
    )
    return FnetClient(config, transport=transport)


def _instant(day, time: str):
    return parse_delivery(f"{day.strftime('%d/%m/%Y')} {time}")


class TestStopRule:
    def test_without_a_cursor_the_whole_window_is_read_descending(self) -> None:
        fake = FakeFnet()
        day = today()
        fake.add_documents(
            77,
            [
                make_row(1, delivery=day - timedelta(days=1), delivery_time="09:00"),
                make_row(2, delivery=day, delivery_time="10:00"),
                make_row(3, delivery=day, delivery_time="11:30"),
            ],
        )
        with _client(fake.transport) as client:
            result = scan_newest(
                client, first=day - timedelta(days=1), last=day, fund_type=1, cursor=None
            )
        assert result.complete and result.failure is None
        assert [row.document_id for row in result.rows] == [3, 2, 1]
        assert result.newest == _instant(day, "11:30")
        assert any(DESC_PARAM in entry for entry in fake.request_log)

    def test_the_read_stops_at_the_first_row_strictly_below_the_cursor(self) -> None:
        # Six documents, one per minute, paged two at a time. A cursor at the
        # third-newest minute stops the read inside page two -- the row below
        # it crosses the frontier -- and page three, which holds only older
        # rows, is never requested.
        fake = FakeFnet()
        day = today()
        for i in range(6):
            row = make_row(10 + i, delivery=day, delivery_time=f"09:{10 + i:02d}")
            fake.add_documents(77, [row])
        cursor = _instant(day, "09:13")
        with _client(fake.transport) as client:
            result = scan_newest(
                client, first=day, last=day, fund_type=1, cursor=cursor, page_length=2
            )
        assert result.complete
        assert result.pages == 2
        assert {row.document_id for row in result.rows} == {15, 14, 13}
        assert all(row.delivery_at >= cursor for row in result.rows)

    def test_a_tie_group_at_the_cursor_minute_is_never_split(self) -> None:
        # Three documents share the cursor's minute and span a page boundary.
        # All three must be kept: dataEntrega resolves to the minute, so the
        # timestamp alone cannot say which of them the last firing accounted
        # for -- deduplication against the manifest is on (id, versao) instead.
        fake = FakeFnet()
        day = today()
        fake.add_documents(
            77,
            [
                make_row(21, delivery=day, delivery_time="10:00"),
                make_row(22, delivery=day, delivery_time="10:00"),
                make_row(23, delivery=day, delivery_time="10:00"),
                make_row(24, delivery=day, delivery_time="10:05"),
                make_row(25, delivery=day, delivery_time="09:55"),
            ],
        )
        with _client(fake.transport) as client:
            result = scan_newest(
                client,
                first=day,
                last=day,
                fund_type=1,
                cursor=_instant(day, "10:00"),
                page_length=2,
            )
        assert result.complete
        assert {row.document_id for row in result.rows} == {24, 23, 22, 21}

    def test_an_empty_window_completes_with_no_newest_instant(self) -> None:
        fake = FakeFnet()
        day = today()
        with _client(fake.transport) as client:
            result = scan_newest(client, first=day, last=day, fund_type=1, cursor=None)
        assert result.complete
        assert result.newest is None
        assert result.rows == [] and result.pages == 1

    def test_a_quiet_firing_costs_exactly_one_request(self) -> None:
        # The ordinary case the whole mechanism is sized for: nothing new since
        # the cursor means the first page already crosses the frontier.
        fake = FakeFnet()
        day = today()
        fake.add_documents(77, [make_row(31, delivery=day, delivery_time="09:00")])
        with _client(fake.transport) as client:
            result = scan_newest(
                client, first=day, last=day, fund_type=1, cursor=_instant(day, "09:01")
            )
        assert result.complete
        assert result.rows == []
        assert result.pages == 1


def _page(rows: list[dict], total: int) -> dict:
    return {"draw": 1, "recordsTotal": total, "recordsFiltered": total, "data": rows}


class TestIntegrityChecks:
    """Violations abort the read; nothing partial ever looks complete."""

    def _serve_pages(self, pages: list[dict]) -> httpx.MockTransport:
        def handler(request: httpx.Request) -> httpx.Response:
            start = int(dict(request.url.params).get("s", 0))
            length = int(dict(request.url.params).get("l", 200))
            return httpx.Response(200, json=pages[start // length])

        return httpx.MockTransport(handler)

    def test_an_order_violation_is_a_broken_contract(self) -> None:
        day = today()
        transport = self._serve_pages(
            [
                _page(
                    [
                        make_row(41, delivery=day, delivery_time="10:00"),
                        make_row(42, delivery=day, delivery_time="11:00"),
                    ],
                    total=2,
                )
            ]
        )
        with _client(transport) as client:
            result = scan_newest(client, first=day, last=day, fund_type=1, cursor=None)
        assert not result.complete
        assert result.contract_broken
        assert "descending order violated" in (result.failure or "")

    def test_a_repeated_identity_across_pages_is_a_broken_contract(self) -> None:
        # The exact signature of the silent loss the ascending sort fixed:
        # duplicates masking skipped rows one-for-one.
        day = today()
        transport = self._serve_pages(
            [
                _page([make_row(51, delivery=day, delivery_time="11:00")], total=2),
                _page([make_row(51, delivery=day, delivery_time="11:00")], total=2),
            ]
        )
        with _client(transport) as client:
            result = scan_newest(
                client, first=day, last=day, fund_type=1, cursor=None, page_length=1
            )
        assert not result.complete
        assert result.contract_broken
        assert "identity repeated" in (result.failure or "")

    def test_a_records_filtered_drift_aborts_without_breaking_the_contract(self) -> None:
        # A re-filing removes the replaced version mid-read, shrinking the
        # total: legitimate source behavior, so the read aborts as a warning
        # rather than an error, and the next firing simply re-reads.
        day = today()
        transport = self._serve_pages(
            [
                _page([make_row(61, delivery=day, delivery_time="11:00")], total=3),
                _page([make_row(62, delivery=day, delivery_time="10:00")], total=2),
            ]
        )
        with _client(transport) as client:
            result = scan_newest(
                client, first=day, last=day, fund_type=1, cursor=None, page_length=1
            )
        assert not result.complete
        assert not result.contract_broken
        assert "recordsFiltered" in (result.failure or "")
