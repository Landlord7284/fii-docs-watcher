"""Contract tests: the observed behaviour of Fundos.NET that this design rests on.

Section 2 of the architecture document is empirical, and this source has no API
contract, no versioning and no SLA. So these tests come in two halves:

- the default ones assert that *our* code still honours what was measured;
- the `live` ones re-measure it against the real host, and are the early warning
  that the source changed underneath us.

Run the live half deliberately:  pytest -m live
"""

from __future__ import annotations

from datetime import date, timedelta

import httpx
import pytest

from fii_docs_watcher.config import MAX_PAGE_LENGTH, SourceConfig
from fii_docs_watcher.cvm.registry import servable_fund_types
from fii_docs_watcher.errors import SourceContractError, TransientSourceError
from fii_docs_watcher.fnet import funds as fnet_funds
from fii_docs_watcher.fnet.client import FnetClient
from fii_docs_watcher.fnet.listing import STABLE_SORT, scan

LIVE_CONFIG = SourceConfig(min_request_interval_seconds=2.0, max_retries=3)


class TestEncodedFindings:
    def test_the_only_ordering_this_endpoint_honours_is_recorded(self) -> None:
        # Measured: with no sort, a full-day scan at l=50 returned 217 rows for a
        # recordsFiltered of 217 while containing only 175 distinct ids -- 42
        # rows silently dropped. Sorting by dataEntrega returned all 217.
        assert STABLE_SORT == {"o[0][dataEntrega]": "asc"}

    def test_the_page_length_ceiling_is_enforced_in_configuration(self) -> None:
        # Measured: l=200 honoured; l>=250 returns HTTP 500 even when politely
        # spaced. The endpoint never truncates silently, so asking for more
        # fails the request rather than losing rows.
        assert MAX_PAGE_LENGTH == 200

    def test_the_read_timeout_clears_the_observed_stall(self) -> None:
        # Latency is bimodal: successful responses take ~0.3s or ~60.3s, with
        # nothing in between. A 30s timeout would fail about half of them.
        assert SourceConfig().read_timeout_seconds > 60.0


class TestClientBehaviour:
    def _client(self, handler) -> FnetClient:
        config = SourceConfig(
            base_url="https://fnet.test/fnet/publico",
            min_request_interval_seconds=0.0,
            backoff_base_seconds=0.0,
            backoff_max_seconds=0.0,
            max_retries=2,
        )
        return FnetClient(config, transport=httpx.MockTransport(handler))

    def test_a_transient_500_is_retried_and_then_succeeds(self) -> None:
        calls = {"n": 0}

        def handler(_request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            if calls["n"] < 3:
                return httpx.Response(500, text="Internal Server Error")
            return httpx.Response(200, json={"ok": True})

        with self._client(handler) as client:
            assert client.get("x").json() == {"ok": True}
        assert calls["n"] == 3

    def test_a_persistent_failure_raises_rather_than_hanging_the_batch(self) -> None:
        with self._client(lambda _r: httpx.Response(503)) as client:
            with pytest.raises(TransientSourceError, match="giving up"):
                client.get("x")

    def test_a_timeout_is_treated_as_retryable(self) -> None:
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            if calls["n"] == 1:
                raise httpx.ReadTimeout("stalled", request=request)
            return httpx.Response(200, json={"ok": True})

        with self._client(handler) as client:
            assert client.get("x").json() == {"ok": True}

    def test_an_oversized_response_is_refused_instead_of_buffered(self) -> None:
        config = SourceConfig(
            base_url="https://fnet.test/f",
            min_request_interval_seconds=0.0,
            max_response_bytes=100,
            max_retries=0,
        )
        transport = httpx.MockTransport(lambda _r: httpx.Response(200, content=b"x" * 5000))
        with FnetClient(config, transport=transport) as client:
            with pytest.raises(SourceContractError, match="max_response_bytes"):
                client.get("x")

    def test_an_identifiable_user_agent_is_always_sent(self) -> None:
        seen: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["ua"] = request.headers["User-Agent"]
            return httpx.Response(200, json={})

        with self._client(handler) as client:
            client.get("x")
        assert "fii-docs-watcher" in seen["ua"]


class TestListarFundosPagination:
    def test_all_pages_are_followed_because_the_page_size_is_twenty(self) -> None:
        # Undocumented in the spec, verified live: 20 results per page with a
        # `more` flag. Reading only the first page loses funds.
        def handler(request: httpx.Request) -> httpx.Response:
            page = int(dict(request.url.params).get("page", 1))
            if page == 1:
                return httpx.Response(
                    200,
                    json={
                        "results": [{"id": i, "text": f"FUND {i}"} for i in range(20)],
                        "more": True,
                    },
                )
            return httpx.Response(
                200, json={"results": [{"id": 100, "text": "FUND LAST"}], "more": False}
            )

        config = SourceConfig(
            base_url="https://fnet.test/f", min_request_interval_seconds=0.0, max_retries=0
        )
        with FnetClient(config, transport=httpx.MockTransport(handler)) as client:
            found = fnet_funds.search(client, "FUND")
        assert len(found) == 21
        assert found[-1].fundosnet_id == 100

    def test_the_display_alias_is_stripped_but_a_real_name_is_not(self) -> None:
        aliased = fnet_funds.FundCandidate(
            20814, "FII BRIO III - BRIO REAL ESTATE III - FUNDO DE INVESTIMENTO IMOBILIÁRIO"
        )
        assert aliased.denomination.startswith("BRIO REAL ESTATE III")

        # A legal name that merely contains " - " must survive intact.
        bare = fnet_funds.FundCandidate(
            25256, "URBANITY CORPORATE - FUNDO DE INVESTIMENTO IMOBILIÁRIO"
        )
        assert bare.denomination == "URBANITY CORPORATE - FUNDO DE INVESTIMENTO IMOBILIÁRIO"


@pytest.mark.live
class TestLiveSource:
    """Re-measures the real host. Deselected unless you ask for `-m live`."""

    def test_page_length_ceiling_still_holds(self) -> None:
        window_end = date.today()
        window_start = window_end - timedelta(days=1)
        with FnetClient(LIVE_CONFIG) as client:
            ok = scan(client, first=window_start, last=window_end, page_length=MAX_PAGE_LENGTH)
            assert ok.records_filtered > 0

            # Above the ceiling the endpoint answers HTTP 500 rather than
            # truncating, so the client exhausts its retries and gives up.
            with pytest.raises(TransientSourceError):
                scan(client, first=window_start, last=window_end, page_length=500)

    def test_a_paginated_scan_still_covers_every_row(self) -> None:
        # The regression that matters most: if this fails, the source's ordering
        # changed and documents are being dropped without any error.
        window_end = date.today()
        with FnetClient(LIVE_CONFIG) as client:
            result = scan(client, first=window_end, last=window_end, page_length=50)
        assert result.complete, (
            f"paginated scan covered {len(result.rows)} of {result.records_filtered} rows; "
            "the ordering guarantee has changed"
        )

    def test_listar_fundos_still_exposes_classes_with_their_own_ids(self) -> None:
        # If this stops holding, class expansion has to change shape.
        with FnetClient(LIVE_CONFIG) as client:
            candidates = fnet_funds.search(client, "URBANITY CORPORATE")
        texts = [c.text.upper() for c in candidates]
        assert any("CLASSE A" in t for t in texts)
        assert len({c.fundosnet_id for c in candidates}) == len(candidates)

    def test_the_always_null_fields_are_still_null(self) -> None:
        window_end = date.today()
        with FnetClient(LIVE_CONFIG) as client:
            payload = client.get(
                "pesquisarGerenciadorDocumentosDados",
                {
                    "d": 1, "s": 0, "l": 5, "tipoFundo": 1,
                    "dataInicial": window_end.strftime("%d/%m/%Y"),
                    "dataFinal": window_end.strftime("%d/%m/%Y"),
                    "idCategoriaDocumento": 0, "idTipoDocumento": 0, "idEspecieDocumento": 0,
                    "isSession": "false", **STABLE_SORT,
                },
            ).json()
        rows = payload.get("data") or []
        if not rows:
            pytest.skip("no documents published yet today")
        for row in rows:
            # If any of these ever carries a value, per-entity querying could be
            # relaxed -- but nothing should rely on it until it is re-verified.
            assert row.get("cnpjFundo") is None
            assert row.get("idFundo") is None

    def test_every_monitorable_fund_type_still_answers(self) -> None:
        # The categories endpoint is the cheapest proof that a fund type exists:
        # one request, no window, no paging. A type that goes empty here means
        # the vocabulary changed and `SERVABLE_FAMILIES` needs re-measuring.
        with FnetClient(LIVE_CONFIG) as client:
            for fund_type in servable_fund_types():
                payload = client.get(
                    "listarTodasCategoriaPorTipoFundo", {"idTipoFundo": fund_type}
                ).json()
                assert payload, f"fund type {fund_type} returned no document categories"

    def test_an_agro_fund_is_listed_only_under_its_own_type(self) -> None:
        # The finding the discovered-type design rests on: a name is invisible
        # under any catalogue but its own, and the miss is silent -- an empty
        # result rather than an error.
        with FnetClient(LIVE_CONFIG) as client:
            agro = fnet_funds.search(client, "POLLI FIAGRO", fund_type=11)
            real_estate = fnet_funds.search(client, "POLLI FIAGRO", fund_type=1)
        assert agro, "the agro catalogue no longer lists a known FIAGRO"
        assert not real_estate
