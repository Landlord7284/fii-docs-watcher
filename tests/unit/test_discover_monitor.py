"""The monitor's discovery gate: what it queries, what it never touches.

The one stated exception to the global listing being detective-only: rows are
matched against the watch list to decide which per-entity queries the firing
spends. These tests pin the boundary from both sides -- a match routes through
`idFundo` exactly like the sweep, a miss costs nothing at all -- and the cursor
advance rule that keeps a failed gated query retryable.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from conftest import FakeFnet, make_row
from fii_docs_watcher.clock import parse_delivery, retention_window, today
from fii_docs_watcher.config import SourceConfig
from fii_docs_watcher.errors import TransientSourceError
from fii_docs_watcher.fnet.client import FnetClient
from fii_docs_watcher.fnet.listing import NewestReadResult
from fii_docs_watcher.manifest.db import connect
from fii_docs_watcher.manifest.repo import ManifestRepo
from fii_docs_watcher.pipeline import discover
from fii_docs_watcher.scope.models import Entity, Scope

CNPJ = "08431747000106"
OTHER_CNPJ = "99999999000199"
FUND_NAME = "HEDGE BRASIL SHOPPING FUNDO DE INVESTIMENTO IMOBILIÁRIO"


def _scope(
    *,
    cnpj: str = CNPJ,
    fundosnet_id: int = 77,
    description: str = FUND_NAME,
    legal_name: str = FUND_NAME,
    fund_type: int = 1,
) -> Scope:
    return Scope(
        cnpj=cnpj,
        legal_name=legal_name,
        entities=[
            Entity(
                cnpj=cnpj,
                fundosnet_id=fundosnet_id,
                fnet_fund_description=description,
                fnet_fund_type=fund_type,
            )
        ],
    )


@pytest.fixture
def repo(tmp_path):
    connection = connect(tmp_path / "manifest.sqlite")
    yield ManifestRepo(connection)
    connection.close()


def _client(fake: FakeFnet) -> FnetClient:
    config = SourceConfig(
        base_url="https://fnet.test/fnet/publico",
        min_request_interval_seconds=0.0,
        backoff_base_seconds=0.0,
        backoff_max_seconds=0.0,
        max_retries=2,
    )
    return FnetClient(config, transport=fake.transport)


def _searches(fake: FakeFnet) -> list[str]:
    return [e for e in fake.request_log if "pesquisarGerenciadorDocumentosDados" in e]


def _entity_searches(fake: FakeFnet) -> list[str]:
    return [e for e in _searches(fake) if "idFundo" in e]


def _run(client, repo, scopes, *, days: int = 2):
    return discover.run_monitor(
        client,
        repo,
        scopes,
        retention_window(days),
        page_length=200,
        retention=retention_window(7),
    )


class TestGate:
    def test_a_matching_row_routes_through_the_normal_per_entity_query(self, repo) -> None:
        fake = FakeFnet()
        fake.add_documents(77, [make_row(1001, fund=FUND_NAME)])
        with _client(fake) as client:
            report = _run(client, repo, [_scope()])

        assert report.entities_scanned == 1
        assert any("idFundo=77" in entry for entry in _entity_searches(fake))
        # What entered the manifest came from the per-entity query: the row
        # carries the queried entity's id and CNPJ, which no global row has.
        document = repo.get(1001, 1)
        assert document is not None
        assert document.fundosnet_id == 77
        assert document.entity_cnpj == CNPJ

    def test_a_match_on_the_legal_name_alone_still_gates(self, repo) -> None:
        fake = FakeFnet()
        fake.add_documents(77, [make_row(1002, fund=FUND_NAME)])
        legal = "Hedge Brasil Shopping fundo de investimento imobiliario"
        scope = _scope(description="", legal_name=legal)
        with _client(fake) as client:
            report = _run(client, repo, [scope])
        assert report.entities_scanned == 1

    def test_an_unmatched_row_costs_no_per_entity_request(self, repo) -> None:
        fake = FakeFnet()
        fake.add_documents(55, [make_row(1003, fund="SOME OTHER FUND")], fund_type=1)
        # The monitored fund published nothing; the other fund's row must not
        # trigger anything, and the firing costs exactly the one global read.
        fake.add_documents(77, [], fund_type=1)
        with _client(fake) as client:
            report = _run(client, repo, [_scope()])

        assert report.entities_scanned == 0
        assert _entity_searches(fake) == []
        assert repo.get(1003, 1) is None

    def test_matching_is_exact_after_folding_never_substring(self, repo) -> None:
        # The row's name contains the monitored name as a prefix; substring
        # matching is the failure mode revision 3 removed, so no gate.
        fake = FakeFnet()
        fake.add_documents(55, [make_row(1004, fund=f"{FUND_NAME} II")])
        with _client(fake) as client:
            report = _run(client, repo, [_scope()])
        assert report.entities_scanned == 0
        assert _entity_searches(fake) == []

    def test_a_row_never_gates_a_homonymous_entity_of_another_fund_type(self, repo) -> None:
        # The type-1 row's spelling matches a monitored type-11 entity. The
        # name only means something within its own type, so nothing is gated.
        fake = FakeFnet()
        fake.add_documents(55, [make_row(1005, fund=FUND_NAME)], fund_type=1)
        fake.add_documents(88, [], fund_type=11)
        scope_11 = _scope(fundosnet_id=88, fund_type=11)
        scope_1 = _scope(
            cnpj=OTHER_CNPJ,
            fundosnet_id=66,
            description="UNRELATED FUND",
            legal_name="UNRELATED FUND",
            fund_type=1,
        )
        with _client(fake) as client:
            report = _run(client, repo, [scope_11, scope_1])
        assert report.entities_scanned == 0
        assert _entity_searches(fake) == []

    def test_a_folded_collision_gates_every_matching_entity(self, repo) -> None:
        fake = FakeFnet()
        fake.add_documents(55, [make_row(1006, fund=FUND_NAME)])
        fake.add_documents(77, [])
        fake.add_documents(78, [])
        first = _scope(fundosnet_id=77)
        second = _scope(cnpj=OTHER_CNPJ, fundosnet_id=78)
        with _client(fake) as client:
            report = _run(client, repo, [first, second])
        assert report.entities_scanned == 2
        queried = {e for e in _entity_searches(fake) if "idFundo=77" in e or "idFundo=78" in e}
        assert len(queried) == 2


class TestCursor:
    def _instant(self, time: str):
        return parse_delivery(f"{today().strftime('%d/%m/%Y')} {time}")

    def test_the_cursor_advances_after_a_successful_gated_discovery(self, repo) -> None:
        fake = FakeFnet()
        fake.add_documents(77, [make_row(2001, fund=FUND_NAME, delivery_time="10:15")])
        with _client(fake) as client:
            _run(client, repo, [_scope()])
        assert repo.listing_cursor(1) == self._instant("10:15")

    def test_a_quiet_second_firing_costs_one_search_per_fund_type(self, repo) -> None:
        fake = FakeFnet()
        fake.add_documents(77, [make_row(2002, fund=FUND_NAME, delivery_time="10:15")])
        with _client(fake) as client:
            _run(client, repo, [_scope()])
            fake.request_log.clear()
            report = _run(client, repo, [_scope()])

        assert report.entities_scanned == 0
        assert len(_searches(fake)) == 1
        assert _entity_searches(fake) == []

    def test_a_new_tie_identity_at_the_cursor_minute_still_gates(self, repo) -> None:
        fake = FakeFnet()
        fake.add_documents(77, [make_row(2003, fund=FUND_NAME, delivery_time="10:15")])
        with _client(fake) as client:
            _run(client, repo, [_scope()])
            # A second document lands carrying the same minute the cursor
            # already points at: the timestamp cannot distinguish it, but its
            # (id, versao) is unknown to the manifest, so it must gate.
            fake.add_documents(77, [make_row(2004, fund=FUND_NAME, delivery_time="10:15")])
            fake.request_log.clear()
            report = _run(client, repo, [_scope()])

        assert report.entities_scanned == 1
        assert repo.get(2004, 1) is not None

    def test_a_failed_gated_discovery_freezes_the_cursor_and_the_next_firing_retries(
        self, repo, monkeypatch
    ) -> None:
        fake = FakeFnet()
        fake.add_documents(77, [make_row(2005, fund=FUND_NAME, delivery_time="10:15")])

        def failing_scan(_client, **_kwargs):
            raise TransientSourceError("stalled")

        monkeypatch.setattr(discover, "scan", failing_scan)
        with _client(fake) as client:
            report = _run(client, repo, [_scope()])
        assert report.entities_failed == 1
        # Advancing here would hand the document to the sweep alone; frozen,
        # the next firing re-reads the same rows and retries the entity.
        assert repo.listing_cursor(1) is None

        monkeypatch.undo()
        with _client(fake) as client:
            retried = _run(client, repo, [_scope()])
        assert retried.entities_scanned == 1
        assert repo.listing_cursor(1) == self._instant("10:15")

    def test_an_incomplete_gated_scan_freezes_the_cursor_and_the_next_firing_retries(
        self, repo
    ) -> None:
        fake = FakeFnet()
        fake.add_documents(77, [make_row(2008, fund=FUND_NAME, delivery_time="10:15")])
        # The source claims more records than pagination ever hands over: the
        # per-entity scan comes back short without raising. Nothing about the
        # newest-first read is affected -- it ends normally on a short page.
        fake.records_filtered_override = 99

        with _client(fake) as client:
            report = _run(client, repo, [_scope()])

        assert report.entities_scanned == 1
        assert report.incomplete_scans == 1
        # An advance here would put the gating row below the frontier, so the
        # rows pagination omitted would never be gated again and only the
        # sweep would ever reach them.
        assert repo.listing_cursor(1) is None

        fake.records_filtered_override = None
        fake.request_log.clear()
        with _client(fake) as client:
            retried = _run(client, repo, [_scope()])

        assert retried.entities_scanned == 1
        assert retried.incomplete_scans == 0
        assert any("idFundo=77" in entry for entry in _entity_searches(fake))
        assert repo.listing_cursor(1) == self._instant("10:15")

    def test_an_aborted_read_gates_nothing_and_freezes_the_cursor(self, repo, monkeypatch) -> None:
        fake = FakeFnet()
        fake.add_documents(77, [make_row(2006, fund=FUND_NAME)])

        def broken_read(_client, **_kwargs):
            return NewestReadResult(
                complete=False, contract_broken=True, failure="descending order violated"
            )

        monkeypatch.setattr(discover, "scan_newest", broken_read)
        with _client(fake) as client:
            report = _run(client, repo, [_scope()])

        assert report.listing_read_failures == 1
        assert report.entities_scanned == 0
        assert _entity_searches(fake) == []
        assert repo.listing_cursor(1) is None

    def test_a_failed_read_for_one_type_does_not_stop_the_other(self, repo, monkeypatch) -> None:
        fake = FakeFnet()
        fake.add_documents(88, [make_row(2007, fund="AGRO FUND")], fund_type=11)
        real = discover.scan_newest

        def selective(_client, **kwargs):
            if kwargs["fund_type"] == 1:
                raise TransientSourceError("stalled")
            return real(_client, **kwargs)

        monkeypatch.setattr(discover, "scan_newest", selective)
        fii = _scope(fundosnet_id=77, fund_type=1)
        agro = _scope(
            cnpj=OTHER_CNPJ,
            fundosnet_id=88,
            description="AGRO FUND",
            legal_name="AGRO FUND",
            fund_type=11,
        )
        with _client(fake) as client:
            report = _run(client, repo, [fii, agro])

        assert report.listing_read_failures == 1
        assert report.entities_scanned == 1
        assert repo.listing_cursor(1) is None
        assert repo.listing_cursor(11) is not None


class TestWatermark:
    def test_a_monitor_firing_never_advances_a_watermark(self, repo) -> None:
        # The monitor window is narrower than retention, so the gated
        # per-entity scan observed nothing about the skipped days -- the rule
        # is derived from the two windows, not from any flag.
        fake = FakeFnet()
        fake.add_documents(77, [make_row(3001, fund=FUND_NAME)])
        with _client(fake) as client:
            report = _run(client, repo, [_scope()], days=2)
        assert report.entities_scanned == 1
        assert repo.watermark(77) is None or repo.watermark(77)["last_window_end"] is None
