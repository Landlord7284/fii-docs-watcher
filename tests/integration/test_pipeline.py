"""End-to-end tests of one run against an in-process fake of Fundos.NET.

These cover the guarantees that are hard to get right and easy to break:
idempotency, offline recovery across delivery dates, crash recovery between the
rename and the commit, the retention frontier, and the CNPJ check.
"""

from __future__ import annotations

import sys
from datetime import timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from conftest import SAMPLE_PDF, FakeFnet, make_row, write_funds_yaml
from fii_docs_watcher.clock import to_dir_name, today
from fii_docs_watcher.config import Config
from fii_docs_watcher.fnet.client import FnetClient
from fii_docs_watcher.manifest.db import connect
from fii_docs_watcher.manifest.repo import LocalState, ManifestRepo
from fii_docs_watcher.pipeline import discover, fetch, inbox, purge, reconcile
from fii_docs_watcher.run import prepare_roots
from fii_docs_watcher.scope.yaml_store import FundsFile

CNPJ = "08431747000106"
FUND_ID = 21348


@pytest.fixture
def env(config: Config, fake_fnet: FakeFnet):
    """A prepared environment with one resolved scope and a live manifest."""
    prepare_roots(config)
    write_funds_yaml(config.paths.funds_file, CNPJ, FUND_ID, ticker="HGBS11")
    connection = connect(config.paths.manifest_file)
    repo = ManifestRepo(connection)
    scopes = FundsFile.load(config.paths.funds_file).scopes()
    client = FnetClient(config.source, transport=fake_fnet.transport)
    try:
        yield config, fake_fnet, repo, scopes, client
    finally:
        client.close()
        connection.close()


def _cycle(config, repo, scopes, client, window):
    """Discovery + fetch, the two stages that move documents onto disk."""
    d = discover.run(client, repo, scopes, window, page_length=config.source.page_length)
    f = fetch.run(client, repo, config, scopes)
    return d, f


class TestDiscoveryAndDownload:
    def test_documents_are_filed_under_their_delivery_date(self, env) -> None:
        config, fake, repo, scopes, client = env
        from fii_docs_watcher.clock import retention_window

        window = retention_window(config.retention.days)
        two_days_ago = today() - timedelta(days=2)
        fake.add_documents(
            FUND_ID,
            [make_row(1001, delivery=today()), make_row(1002, delivery=two_days_ago)],
        )

        _cycle(config, repo, scopes, client, window)

        # Not everything dumped into today's directory: the question "what
        # happened on that day?" has to keep its answer.
        assert (config.paths.documents_root / to_dir_name(today())).is_dir()
        assert (config.paths.documents_root / to_dir_name(two_days_ago)).is_dir()
        assert len(list((config.paths.documents_root / to_dir_name(two_days_ago)).iterdir())) == 1

    def test_a_second_run_downloads_nothing_new(self, env) -> None:
        config, fake, repo, scopes, client = env
        from fii_docs_watcher.clock import retention_window

        window = retention_window(config.retention.days)
        fake.add_documents(FUND_ID, [make_row(1001), make_row(1002)])

        _, first = _cycle(config, repo, scopes, client, window)
        assert first.downloaded == 2

        _, second = _cycle(config, repo, scopes, client, window)
        assert second.downloaded == 0
        assert repo.counts_by_state() == {LocalState.AVAILABLE.value: 2}

    def test_rediscovery_updates_mutable_fields_without_redownloading(self, env) -> None:
        config, fake, repo, scopes, client = env
        from fii_docs_watcher.clock import retention_window

        window = retention_window(config.retention.days)
        fake.add_documents(FUND_ID, [make_row(1001, status="Ativo com visualização")])
        _cycle(config, repo, scopes, client, window)
        original = repo.get(1001, 1)
        assert original is not None and original.status == "Ativo com visualização"

        # The source cancels the document after delivery.
        fake.documents[FUND_ID][0]["descricaoStatus"] = "Cancelado"
        _, second = _cycle(config, repo, scopes, client, window)

        updated = repo.get(1001, 1)
        assert updated is not None
        assert updated.status == "Cancelado"
        assert second.downloaded == 0
        # The file is not demoted just because the source changed its mind.
        assert updated.local_state == LocalState.AVAILABLE
        assert updated.content_hash == original.content_hash

    def test_a_refiling_is_a_separate_publication_and_supersedes_the_first(self, env) -> None:
        config, fake, repo, scopes, client = env
        from fii_docs_watcher.clock import retention_window

        window = retention_window(config.retention.days)
        fake.add_documents(FUND_ID, [make_row(1001, version=1)])
        _cycle(config, repo, scopes, client, window)

        # The listing replaces v1 with v2 -- verified behaviour of the source.
        fake.documents[FUND_ID] = [make_row(1001, version=2, modality="RE")]
        fake.payloads[1001] = SAMPLE_PDF
        fake.content_type[1001] = "application/pdf"
        fake.disposition[1001] = (
            'attachment; filename="08431747000106-IFP14082026V02-000001001.pdf"'
        )
        d, f = _cycle(config, repo, scopes, client, window)

        assert d.superseded == 1
        assert f.downloaded == 1
        v1, v2 = repo.get(1001, 1), repo.get(1001, 2)
        assert v1 is not None and v2 is not None
        assert v1.superseded_at is not None
        # v1's file survives: that history is exactly what the archive is for.
        assert (config.paths.documents_root / v1.path).is_file()
        assert (config.paths.documents_root / v2.path).is_file()
        assert v1.path != v2.path

    def test_the_stable_sort_is_sent_on_every_listing_request(self, env) -> None:
        config, fake, repo, scopes, client = env
        from fii_docs_watcher.clock import retention_window

        fake.add_documents(FUND_ID, [make_row(1001)])
        discover.run(client, repo, scopes, retention_window(7), page_length=200)

        searches = [r for r in fake.request_log if "pesquisar" in r]
        assert searches
        # Without this parameter the source silently drops ~19% of rows while
        # recordsFiltered still matches. It is not optional.
        assert all("dataEntrega%5D=asc" in r or "dataEntrega]=asc" in r for r in searches)

    def test_pagination_collects_every_row(self, env) -> None:
        config, fake, repo, scopes, client = env
        from fii_docs_watcher.clock import retention_window

        fake.add_documents(FUND_ID, [make_row(2000 + i) for i in range(45)])
        result = discover.run(client, repo, scopes, retention_window(7), page_length=10)

        assert result.documents_seen == 45
        assert result.incomplete_scans == 0


class TestCandidateConfirmation:
    def test_confirming_a_candidate_costs_exactly_one_request(self, env) -> None:
        """A confirmation must not paginate, however many documents the fund has.

        Regression: this used to call `scan(..., page_length=1)`, which pages
        until the whole window is covered -- one request per document. For a
        large fund that is dozens of sequential requests, each able to stall for
        a minute and the lot retried on a short scan, so registering a busy fund
        appeared to hang forever.
        """
        config, fake, repo, scopes, client = env
        from fii_docs_watcher.fnet.funds import FundCandidate
        from fii_docs_watcher.scope.resolver import confirm_candidate

        # A fund as busy as KINEA RENDA over the confirmation window.
        fake.add_documents(
            FUND_ID, [make_row(3000 + i, delivery=today() - timedelta(days=i % 400))
                      for i in range(74)]
        )
        fake.request_log.clear()

        result = confirm_candidate(client, FundCandidate(FUND_ID, "HEDGE BRASIL SHOPPING"))

        searches = [r for r in fake.request_log if "pesquisar" in r]
        assert len(searches) == 1, f"expected one request, issued {len(searches)}"
        assert result.confirmed
        assert result.document_count == 74  # still learns the true total
        assert result.fnet_description  # and captures the exact description

    def test_a_candidate_with_no_documents_is_not_rejected(self, env) -> None:
        # Quiet funds are common; rejecting one would block a valid registration.
        config, fake, repo, scopes, client = env
        from fii_docs_watcher.fnet.funds import FundCandidate
        from fii_docs_watcher.scope.resolver import confirm_candidate

        result = confirm_candidate(client, FundCandidate(99999, "SOME QUIET FUND"))
        assert not result.confirmed
        assert result.document_count == 0

    def test_probe_reads_the_total_without_reading_every_row(self, env) -> None:
        config, fake, repo, scopes, client = env
        from fii_docs_watcher.clock import retention_window
        from fii_docs_watcher.fnet.listing import probe

        window = retention_window(7)
        fake.add_documents(FUND_ID, [make_row(4000 + i) for i in range(30)])
        fake.request_log.clear()

        result = probe(client, first=window.first, last=window.last, fundosnet_id=FUND_ID)

        assert result.exists
        assert result.records_filtered == 30
        assert result.first_row is not None
        assert len([r for r in fake.request_log if "pesquisar" in r]) == 1


class TestFormatFilter:
    """[download].formats — declining a format must cost nothing and lose nothing."""

    def _setup(self, config, fake, formats):
        """Publish one XML (Estruturado) and one PDF, and return a config wanting `formats`."""
        from dataclasses import replace

        fake.add_documents(
            FUND_ID,
            [
                make_row(
                    7001,
                    category="Informes Periódicos",
                    doc_type="Informe Mensal Estruturado ",
                )
            ],
        )
        fake.add_documents(FUND_ID, [make_row(7002, category="Fato Relevante", doc_type="")])
        fake.payloads[7002] = SAMPLE_PDF
        fake.content_type[7002] = "application/pdf"
        fake.disposition[7002] = (
            'attachment; filename="08431747000106-FRE14082026V01-000007002.pdf"'
        )
        return replace(config, download=replace(config.download, formats=formats))

    def test_declining_a_format_costs_no_request(self, env) -> None:
        config, fake, repo, scopes, client = env
        from fii_docs_watcher.clock import retention_window

        pdf_only = self._setup(config, fake, ("pdf",))
        discover.run(client, repo, scopes, retention_window(7), page_length=200)
        fake.request_log.clear()

        report = fetch.run(client, repo, pdf_only, scopes)

        assert report.downloaded == 1
        assert report.skipped == 1
        downloads = [r for r in fake.request_log if "downloadDocumento" in r]
        assert len(downloads) == 1, "the declined document must not be requested at all"
        assert "id=7002" in downloads[0]
        assert repo.get(7001, 1).local_state == LocalState.SKIPPED
        assert repo.get(7002, 1).local_state == LocalState.AVAILABLE

    def test_the_converse_selection_works_too(self, env) -> None:
        config, fake, repo, scopes, client = env
        from fii_docs_watcher.clock import retention_window

        xml_only = self._setup(config, fake, ("xml",))
        discover.run(client, repo, scopes, retention_window(7), page_length=200)
        fetch.run(client, repo, xml_only, scopes)

        assert repo.get(7001, 1).local_state == LocalState.AVAILABLE
        assert repo.get(7002, 1).local_state == LocalState.SKIPPED

    def test_a_skipped_document_is_not_written_to_the_archive(self, env) -> None:
        config, fake, repo, scopes, client = env
        from fii_docs_watcher.clock import retention_window

        pdf_only = self._setup(config, fake, ("pdf",))
        discover.run(client, repo, scopes, retention_window(7), page_length=200)
        fetch.run(client, repo, pdf_only, scopes)

        archived = [p.name for p in (config.paths.documents_root).rglob("*.xml")]
        assert archived == []

    def test_widening_the_config_picks_up_what_was_skipped(self, env) -> None:
        """The point of `skipped` rather than a hard drop."""
        config, fake, repo, scopes, client = env
        from fii_docs_watcher.clock import retention_window

        pdf_only = self._setup(config, fake, ("pdf",))
        discover.run(client, repo, scopes, retention_window(7), page_length=200)
        fetch.run(client, repo, pdf_only, scopes)
        assert repo.get(7001, 1).local_state == LocalState.SKIPPED

        # Later run, both formats wanted, and crucially no fresh discovery.
        both = self._setup(config, fake, ("pdf", "xml"))
        report = fetch.run(client, repo, both, scopes)

        assert report.downloaded == 1
        assert repo.get(7001, 1).local_state == LocalState.AVAILABLE

    def test_re_skipping_stays_free_on_every_later_run(self, env) -> None:
        config, fake, repo, scopes, client = env
        from fii_docs_watcher.clock import retention_window

        pdf_only = self._setup(config, fake, ("pdf",))
        discover.run(client, repo, scopes, retention_window(7), page_length=200)
        fetch.run(client, repo, pdf_only, scopes)
        fake.request_log.clear()

        second = fetch.run(client, repo, pdf_only, scopes)

        assert second.skipped == 1
        assert [r for r in fake.request_log if "downloadDocumento" in r] == []

    def test_a_mispredicted_format_is_declined_after_download(self, env) -> None:
        """The signature has the last word, even against the routing hint."""
        from dataclasses import replace

        config, fake, repo, scopes, client = env
        from fii_docs_watcher.clock import retention_window

        # Categorised as Estruturado -- so predicted XML, and wanted -- but the
        # bytes that come back are a PDF.
        fake.add_documents(
            FUND_ID,
            [
                make_row(
                    7100,
                    category="Informes Periódicos",
                    doc_type="Informe Mensal Estruturado ",
                )
            ],
        )
        fake.payloads[7100] = SAMPLE_PDF
        fake.content_type[7100] = "application/pdf"
        xml_only = replace(config, download=replace(config.download, formats=("xml",)))

        discover.run(client, repo, scopes, retention_window(7), page_length=200)
        report = fetch.run(client, repo, xml_only, scopes)

        assert report.downloaded == 0
        assert report.skipped == 1
        assert repo.get(7100, 1).local_state == LocalState.SKIPPED
        assert list(config.paths.documents_root.rglob("*.pdf")) == []
        # The request really happened, so it is recorded rather than pretended away.
        assert repo.attempt_count(7100, 1) == 1

    def test_both_formats_is_the_default_and_changes_nothing(self, env) -> None:
        config, fake, repo, scopes, client = env
        from fii_docs_watcher.clock import retention_window

        self._setup(config, fake, ("pdf", "xml"))
        discover.run(client, repo, scopes, retention_window(7), page_length=200)
        report = fetch.run(client, repo, config, scopes)

        assert report.downloaded == 2
        assert report.skipped == 0


class TestRemovingAFund:
    """`rm` — what has to stop happening once a fund is no longer followed."""

    def test_a_removed_funds_backlog_is_never_downloaded(self, env) -> None:
        """The bug this guards against.

        `discover` stops asking about a removed fund, but `fetch` builds its
        queue from the manifest rather than from the scope list. Without
        standing the backlog down, the next run would keep downloading documents
        for a fund nobody is following.
        """
        config, fake, repo, scopes, client = env
        from fii_docs_watcher.clock import retention_window

        fake.add_documents(FUND_ID, [make_row(8001), make_row(8002)])
        discover.run(client, repo, scopes, retention_window(7), page_length=200)
        assert len(repo.pending_downloads()) == 2

        abandoned = repo.abandon_pending([FUND_ID])

        assert abandoned == 2
        assert repo.pending_downloads() == []
        fake.request_log.clear()
        report = fetch.run(client, repo, config, [])
        assert report.downloaded == 0
        assert [r for r in fake.request_log if "downloadDocumento" in r] == []

    def test_documents_already_on_disk_are_left_alone(self, env) -> None:
        config, fake, repo, scopes, client = env
        from fii_docs_watcher.clock import retention_window

        fake.add_documents(FUND_ID, [make_row(8001)])
        _cycle(config, repo, scopes, client, retention_window(7))
        stored = repo.get(8001, 1)
        assert stored.local_state == LocalState.AVAILABLE

        repo.abandon_pending([FUND_ID])

        # Still archived, and still readable until retention takes it.
        after = repo.get(8001, 1)
        assert after.local_state == LocalState.AVAILABLE
        assert (config.paths.documents_root / after.path).is_file()

    def test_abandoning_is_scoped_to_the_removed_entity(self, env) -> None:
        config, fake, repo, scopes, client = env
        from fii_docs_watcher.clock import retention_window

        other_id = 99999
        fake.add_documents(FUND_ID, [make_row(8001)])
        fake.add_documents(other_id, [make_row(8002)])
        scopes[0].entities[0].fundosnet_id = FUND_ID
        discover.run(client, repo, scopes, retention_window(7), page_length=200)
        # Record a document for the other entity directly, as discovery only
        # covers the monitored one.
        from fii_docs_watcher.fnet.schema import parse_row

        repo.upsert_discovered(
            parse_row(make_row(8002)), fundosnet_id=other_id, entity_cnpj=None
        )

        repo.abandon_pending([FUND_ID])

        assert repo.get(8001, 1).local_state == LocalState.ABANDONED
        assert repo.get(8002, 1).local_state == LocalState.DISCOVERED

    def test_a_skipped_document_is_abandoned_too(self, env) -> None:
        # Otherwise it would sit in the queue being re-evaluated forever.
        config, fake, repo, scopes, client = env
        from fii_docs_watcher.clock import retention_window

        fake.add_documents(FUND_ID, [make_row(8001)])
        discover.run(client, repo, scopes, retention_window(7), page_length=200)
        repo.set_state(8001, 1, LocalState.SKIPPED)

        assert repo.abandon_pending([FUND_ID]) == 1
        assert repo.pending_downloads() == []

    def test_deleting_documents_removes_the_files_and_marks_the_rows(self, env) -> None:
        config, fake, repo, scopes, client = env
        from fii_docs_watcher.cli import _delete_documents
        from fii_docs_watcher.clock import retention_window

        fake.add_documents(FUND_ID, [make_row(8001), make_row(8002)])
        _cycle(config, repo, scopes, client, retention_window(7))
        on_disk = repo.available_for_entities([FUND_ID])
        assert len(on_disk) == 2
        paths = [config.paths.documents_root / d.path for d in on_disk]

        removed = _delete_documents(config, repo, on_disk)

        assert removed == 2
        assert not any(p.exists() for p in paths)
        assert repo.get(8001, 1).local_state == LocalState.PURGED
        assert repo.get(8001, 1).purged_at is not None
        # No empty date directory left behind.
        assert not paths[0].parent.exists()

    def test_available_for_entities_ignores_other_funds(self, env) -> None:
        config, fake, repo, scopes, client = env
        from fii_docs_watcher.clock import retention_window

        fake.add_documents(FUND_ID, [make_row(8001)])
        _cycle(config, repo, scopes, client, retention_window(7))

        assert len(repo.available_for_entities([FUND_ID])) == 1
        assert repo.available_for_entities([12345]) == []
        assert repo.available_for_entities([]) == []


class TestUnwatchedEntities:
    """What happens to a fund's queue once nobody is following it.

    Removing a fund by editing funds.yaml is as legitimate as using `rm`, and
    both have to stop the backlog: `discover` reads the scope list, but `fetch`
    reads the manifest, so without this the next run keeps downloading for a
    fund nobody follows -- and does it with the CNPJ check skipped, because
    `fetch_one` can only run that check when it knows the entity.
    """

    def test_a_document_with_no_known_entity_is_never_requested(self, env) -> None:
        config, fake, repo, scopes, client = env
        from fii_docs_watcher.clock import retention_window

        fake.add_documents(FUND_ID, [make_row(9001)])
        discover.run(client, repo, scopes, retention_window(7), page_length=200)
        fake.request_log.clear()

        # No scopes passed: the entity is unknown to this run.
        report = fetch.run(client, repo, config, [])

        assert report.downloaded == 0
        assert report.deferred == 1
        assert [r for r in fake.request_log if "downloadDocumento" in r] == []

    def test_deferring_leaves_the_state_untouched(self, env) -> None:
        """A CVM outage must not permanently drop a real backlog."""
        config, fake, repo, scopes, client = env
        from fii_docs_watcher.clock import retention_window

        fake.add_documents(FUND_ID, [make_row(9001)])
        discover.run(client, repo, scopes, retention_window(7), page_length=200)
        before = repo.get(9001, 1).local_state

        fetch.run(client, repo, config, [])

        assert repo.get(9001, 1).local_state == before

    def test_the_deferred_document_downloads_once_its_scope_returns(self, env) -> None:
        config, fake, repo, scopes, client = env
        from fii_docs_watcher.clock import retention_window

        fake.add_documents(FUND_ID, [make_row(9001)])
        discover.run(client, repo, scopes, retention_window(7), page_length=200)
        fetch.run(client, repo, config, [])

        report = fetch.run(client, repo, config, scopes)

        assert report.downloaded == 1
        assert report.deferred == 0
        assert repo.get(9001, 1).local_state == LocalState.AVAILABLE

    def test_a_fund_edited_out_of_the_yaml_has_its_queue_stood_down(self, env) -> None:
        config, fake, repo, scopes, client = env
        from fii_docs_watcher.clock import retention_window

        fake.add_documents(FUND_ID, [make_row(9001), make_row(9002)])
        discover.run(client, repo, scopes, retention_window(7), page_length=200)

        # No scope in funds.yaml claims this entity any more.
        assert repo.abandon_pending_outside(set()) == 2

        assert repo.get(9001, 1).local_state == LocalState.ABANDONED
        assert repo.pending_downloads() == []

    def test_a_configured_but_unresolved_fund_keeps_its_queue(self, env) -> None:
        """The distinction that makes the sweep safe."""
        config, fake, repo, scopes, client = env
        from fii_docs_watcher.clock import retention_window

        fake.add_documents(FUND_ID, [make_row(9001)])
        discover.run(client, repo, scopes, retention_window(7), page_length=200)

        # Still configured -- it just did not resolve this run -- so it is
        # passed as a kept id and must not be abandoned.
        assert repo.abandon_pending_outside({FUND_ID}) == 0
        assert repo.get(9001, 1).local_state == LocalState.DISCOVERED

    def test_documents_already_archived_are_not_touched_by_the_sweep(self, env) -> None:
        config, fake, repo, scopes, client = env
        from fii_docs_watcher.clock import retention_window

        fake.add_documents(FUND_ID, [make_row(9001)])
        _cycle(config, repo, scopes, client, retention_window(7))

        repo.abandon_pending_outside(set())

        assert repo.get(9001, 1).local_state == LocalState.AVAILABLE


    def test_re_adding_a_fund_revives_its_abandoned_backlog(self, env) -> None:
        """Otherwise remove-then-re-add leaves a permanently stranded queue.

        Discovery would keep finding these rows every run while nothing ever
        downloaded them, so the fund would look monitored and quietly not be.
        """
        config, fake, repo, scopes, client = env
        from fii_docs_watcher.clock import retention_window

        window = retention_window(7)
        fake.add_documents(FUND_ID, [make_row(9001)])
        discover.run(client, repo, scopes, window, page_length=200)
        repo.abandon_pending_outside(set())
        assert repo.get(9001, 1).local_state == LocalState.ABANDONED

        # The fund is back on the watch list, so it is queried again.
        discover.run(client, repo, scopes, window, page_length=200)

        assert repo.get(9001, 1).local_state == LocalState.DISCOVERED
        assert fetch.run(client, repo, config, scopes).downloaded == 1

    def test_rediscovery_does_not_disturb_any_other_state(self, env) -> None:
        config, fake, repo, scopes, client = env
        from fii_docs_watcher.clock import retention_window

        window = retention_window(7)
        fake.add_documents(FUND_ID, [make_row(9001), make_row(9002)])
        _cycle(config, repo, scopes, client, window)
        repo.mark_failed(9002, 1)

        discover.run(client, repo, scopes, window, page_length=200)

        # available stays available, failed stays failed and keeps retrying.
        assert repo.get(9001, 1).local_state == LocalState.AVAILABLE
        assert repo.get(9002, 1).local_state == LocalState.FAILED

    def test_a_purged_document_is_not_revived(self, env) -> None:
        config, fake, repo, scopes, client = env
        from fii_docs_watcher.clock import retention_window

        window = retention_window(7)
        fake.add_documents(FUND_ID, [make_row(9001)])
        discover.run(client, repo, scopes, window, page_length=200)
        repo.abandon_pending_outside(set())
        repo.mark_documents_purged([(9001, 1)])

        discover.run(client, repo, scopes, window, page_length=200)

        assert repo.get(9001, 1).local_state == LocalState.PURGED


class TestWatermarkWarnings:
    def _stale(self, repo, fundosnet_id: int) -> None:
        from fii_docs_watcher.clock import to_dir_name, today

        repo.advance_watermark(fundosnet_id, today() - timedelta(days=90))
        assert repo.watermark(fundosnet_id)["last_window_end"] == to_dir_name(
            today() - timedelta(days=90)
        )

    def test_a_gap_is_reported_for_a_monitored_fund(self, env) -> None:
        config, fake, repo, scopes, client = env
        from fii_docs_watcher.clock import retention_window

        self._stale(repo, FUND_ID)

        warnings = discover.check_watermarks(repo, retention_window(7), {FUND_ID})

        assert len(warnings) == 1
        assert str(FUND_ID) in warnings[0]

    def test_no_gap_is_reported_for_a_fund_nobody_follows(self, env) -> None:
        # Otherwise removing a fund on purpose warns on every run forever, and a
        # warning that always fires is one nobody reads when it matters.
        config, fake, repo, scopes, client = env
        from fii_docs_watcher.clock import retention_window

        self._stale(repo, FUND_ID)

        assert discover.check_watermarks(repo, retention_window(7), set()) == []
        assert discover.check_watermarks(repo, retention_window(7), {12345}) == []

    def test_without_a_filter_every_entity_is_still_considered(self, env) -> None:
        config, fake, repo, scopes, client = env
        from fii_docs_watcher.clock import retention_window

        self._stale(repo, FUND_ID)

        assert len(discover.check_watermarks(repo, retention_window(7))) == 1

    def test_forgetting_an_entity_drops_only_its_sync_state(self, env) -> None:
        config, fake, repo, scopes, client = env
        from fii_docs_watcher.clock import retention_window

        fake.add_documents(FUND_ID, [make_row(9001)])
        _cycle(config, repo, scopes, client, retention_window(7))
        assert repo.watermark(FUND_ID) is not None

        assert repo.forget_entities([FUND_ID]) == 1

        assert repo.watermark(FUND_ID) is None
        # The document record survives: it is still true that this was published.
        assert repo.get(9001, 1) is not None


class TestReconciliation:
    def test_a_file_renamed_but_not_committed_is_adopted_not_redownloaded(self, env) -> None:
        config, fake, repo, scopes, client = env
        from fii_docs_watcher.clock import retention_window

        window = retention_window(config.retention.days)
        fake.add_documents(FUND_ID, [make_row(1001)])
        _cycle(config, repo, scopes, client, window)

        stored = repo.get(1001, 1)
        assert stored is not None

        # Simulate the crash: the file is in place, the manifest is not.
        repo.connection.execute(
            "UPDATE documents SET local_state = ?, content_hash = NULL, downloaded_at = NULL "
            "WHERE document_id = 1001",
            (LocalState.DOWNLOADING.value,),
        )

        report = reconcile.run(repo, config)
        assert report.promoted == 1
        assert report.requeued == 0

        healed = repo.get(1001, 1)
        assert healed is not None
        assert healed.local_state == LocalState.AVAILABLE
        assert healed.content_hash == stored.content_hash

    def test_a_missing_destination_goes_back_to_the_queue(self, env) -> None:
        config, fake, repo, scopes, client = env
        from fii_docs_watcher.clock import retention_window

        window = retention_window(config.retention.days)
        fake.add_documents(FUND_ID, [make_row(1001)])
        _cycle(config, repo, scopes, client, window)

        stored = repo.get(1001, 1)
        assert stored is not None
        (config.paths.documents_root / stored.path).unlink()
        repo.connection.execute(
            "UPDATE documents SET local_state = ? WHERE document_id = 1001",
            (LocalState.DOWNLOADING.value,),
        )

        report = reconcile.run(repo, config)
        assert report.requeued == 1
        assert repo.get(1001, 1).local_state == LocalState.DISCOVERED

        # And the retry actually recovers it.
        again = fetch.run(client, repo, config, scopes)
        assert again.downloaded == 1

    def test_a_corrupted_archived_file_is_not_adopted(self, env) -> None:
        config, fake, repo, scopes, client = env
        from fii_docs_watcher.clock import retention_window

        window = retention_window(config.retention.days)
        fake.add_documents(FUND_ID, [make_row(1001)])
        _cycle(config, repo, scopes, client, window)

        stored = repo.get(1001, 1)
        (config.paths.documents_root / stored.path).write_bytes(b"<html>error</html>")
        repo.connection.execute(
            "UPDATE documents SET local_state = ? WHERE document_id = 1001",
            (LocalState.DOWNLOADING.value,),
        )

        report = reconcile.run(repo, config)
        # Existence alone is never evidence; the bytes have to re-validate.
        assert report.promoted == 0
        assert report.requeued == 1

    def test_stale_staging_files_are_swept(self, config: Config) -> None:
        import os
        import time

        prepare_roots(config)
        stale = config.paths.tmp_dir / "999_V01.part"
        stale.write_bytes(b"partial")
        old = time.time() - config.download.stale_part_hours * 3600 - 60
        os.utime(stale, (old, old))

        fresh = config.paths.tmp_dir / "888_V01.part"
        fresh.write_bytes(b"in flight")

        assert reconcile.sweep_staging(config) == 1
        assert not stale.exists()
        # A recent one may belong to a run that is still going.
        assert fresh.exists()


class TestRetention:
    def test_purge_deletes_past_the_frontier_and_keeps_the_rows(self, env) -> None:
        config, fake, repo, scopes, client = env
        from fii_docs_watcher.clock import retention_window

        old = today() - timedelta(days=10)
        fake.add_documents(
            FUND_ID, [make_row(1001, delivery=today()), make_row(1002, delivery=old)]
        )
        # A wide window first, so the old document actually lands on disk.
        _cycle(config, repo, scopes, client, retention_window(30))
        assert (config.paths.documents_root / to_dir_name(old)).is_dir()

        report = purge.run(repo, config, retention_window(7))

        assert report.directories_removed == 1
        assert not (config.paths.documents_root / to_dir_name(old)).exists()
        assert (config.paths.documents_root / to_dir_name(today())).is_dir()
        # The record that it existed is cheap and useful; only the file is temporary.
        assert report.rows_marked == 1
        assert repo.get(1002, 1).local_state == LocalState.PURGED
        assert repo.get(1002, 1).purged_at is not None

    def test_purge_never_touches_directories_it_does_not_recognise(self, env) -> None:
        config, fake, repo, scopes, client = env
        from fii_docs_watcher.clock import retention_window

        prepare_roots(config)
        notes = config.paths.documents_root / "my-notes"
        notes.mkdir()
        (notes / "keep.txt").write_text("mine")

        purge.run(repo, config, retention_window(1))

        assert notes.is_dir()
        assert (notes / "keep.txt").exists()
        assert config.paths.inbox_dir.is_dir()
        assert config.paths.tmp_dir.is_dir()

    def test_the_frontier_is_shared_so_discovery_never_fetches_what_purge_deletes(
        self, env
    ) -> None:
        config, fake, repo, scopes, client = env
        from fii_docs_watcher.clock import retention_window

        window = retention_window(3)
        fake.add_documents(
            FUND_ID,
            [make_row(1000 + i, delivery=today() - timedelta(days=i)) for i in range(8)],
        )
        _cycle(config, repo, scopes, client, window)
        purge.run(repo, config, window)

        surviving = {
            entry.name
            for entry in config.paths.documents_root.iterdir()
            if entry.is_dir() and entry.name not in {".tmp", "_inbox"}
        }
        assert surviving == {to_dir_name(d) for d in window.dates()}


class TestInbox:
    def test_the_index_lists_what_arrived_today_across_past_delivery_dates(self, env) -> None:
        config, fake, repo, scopes, client = env
        from fii_docs_watcher.clock import retention_window

        window = retention_window(config.retention.days)
        # The offline-recovery shape: new arrivals scattered over past dates.
        fake.add_documents(
            FUND_ID,
            [make_row(1000 + i, delivery=today() - timedelta(days=i)) for i in range(3)],
        )
        _cycle(config, repo, scopes, client, window)

        report = inbox.run(repo, config, window)
        assert report.documents == 3

        text = (config.paths.documents_root / report.path).read_text(encoding="utf-8")
        for offset in range(3):
            assert to_dir_name(today() - timedelta(days=offset)) in text
        # Relative links, so the archive stays portable over SMB.
        assert "](../" in text

    def test_an_empty_day_says_so_rather_than_looking_broken(self, env) -> None:
        config, fake, repo, scopes, client = env
        from fii_docs_watcher.clock import retention_window

        report = inbox.run(repo, config, retention_window(7))
        text = (config.paths.documents_root / report.path).read_text(encoding="utf-8")
        assert "Nothing new arrived today" in text


class TestFailureIsolation:
    def test_a_failing_download_does_not_stop_the_others(self, env) -> None:
        config, fake, repo, scopes, client = env
        from fii_docs_watcher.clock import retention_window

        window = retention_window(config.retention.days)
        fake.add_documents(FUND_ID, [make_row(1001), make_row(1002), make_row(1003)])
        fake.fail_downloads.add(1002)

        _, report = _cycle(config, repo, scopes, client, window)

        assert report.downloaded == 2
        assert report.failed == 1
        assert repo.get(1002, 1).local_state == LocalState.FAILED
        # The failure is recorded as history, not overwritten on the document row.
        assert repo.attempt_count(1002, 1) >= 1

    def test_a_failed_document_is_retried_on_the_next_run(self, env) -> None:
        config, fake, repo, scopes, client = env
        from fii_docs_watcher.clock import retention_window

        window = retention_window(config.retention.days)
        fake.add_documents(FUND_ID, [make_row(1001)])
        fake.fail_downloads.add(1001)
        _cycle(config, repo, scopes, client, window)
        assert repo.get(1001, 1).local_state == LocalState.FAILED

        fake.fail_downloads.clear()
        _, report = _cycle(config, repo, scopes, client, window)
        assert report.downloaded == 1
        assert repo.get(1001, 1).local_state == LocalState.AVAILABLE

    def test_a_document_serving_an_html_error_page_is_never_written(self, env) -> None:
        config, fake, repo, scopes, client = env
        from fii_docs_watcher.clock import retention_window

        window = retention_window(config.retention.days)
        fake.add_documents(FUND_ID, [make_row(1001)])
        # HTTP 200 with an HTML body is a real failure mode on this source.
        fake.payloads[1001] = b"<!DOCTYPE html><html><body>HTTP Status 500</body></html>"
        fake.content_type[1001] = "text/html"

        _, report = _cycle(config, repo, scopes, client, window)

        assert report.failed == 1
        assert repo.get(1001, 1).local_state == LocalState.FAILED
        day_dir = config.paths.documents_root / to_dir_name(today())
        assert not day_dir.exists() or not list(day_dir.iterdir())


class TestCnpjValidation:
    def test_a_matching_cnpj_confirms_the_entity(self, env) -> None:
        config, fake, repo, scopes, client = env
        from fii_docs_watcher.clock import retention_window

        assert scopes[0].entities[0].cnpj_confirmed is False
        fake.add_documents(FUND_ID, [make_row(1001)])
        _cycle(config, repo, scopes, client, retention_window(7))
        assert scopes[0].entities[0].cnpj_confirmed is True

    def test_a_foreign_cnpj_blocks_the_document_and_is_critical(self, env, caplog) -> None:
        import logging

        config, fake, repo, scopes, client = env
        from fii_docs_watcher.clock import retention_window

        fake.add_documents(FUND_ID, [make_row(1001)])
        # A CNPJ belonging to no entity of this scope: the resolution was textual,
        # and this is the check that catches it having gone wrong.
        fake.disposition[1001] = (
            'attachment; filename="99999999999999-IFP14082026V01-000001001.xml"'
        )

        with caplog.at_level(logging.CRITICAL):
            _, report = _cycle(config, repo, scopes, client, retention_window(7))

        assert report.failed == 1
        assert report.cnpj_divergences
        assert repo.get(1001, 1).local_state == LocalState.FAILED
        assert scopes[0].entities[0].cnpj_confirmed is False

    def test_an_unparseable_served_filename_does_not_block_anything(self, env) -> None:
        config, fake, repo, scopes, client = env
        from fii_docs_watcher.clock import retention_window

        fake.add_documents(FUND_ID, [make_row(1001)])
        # Best-effort by contract: the pipeline falls back to the queried CNPJ.
        fake.disposition[1001] = 'attachment; filename="unexpected-name.xml"'

        _, report = _cycle(config, repo, scopes, client, retention_window(7))

        assert report.downloaded == 1
        assert report.failed == 0
