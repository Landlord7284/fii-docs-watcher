"""Shared fixtures.

The integration tests drive the whole pipeline against an in-process fake of
Fundos.NET, so they exercise the real client, the real retry and pagination
logic and the real state machine without touching the network.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import httpx
import pytest

from fii_docs_watcher.clock import to_dir_name, today
from fii_docs_watcher.config import (
    AuditConfig,
    Config,
    CvmConfig,
    DownloadConfig,
    FilesConfig,
    LoggingConfig,
    PathsConfig,
    RetentionConfig,
    SourceConfig,
)

FIXTURES = Path(__file__).parent / "fixtures"

# A minimal but real Informe Mensal Estruturado root, as served.
SAMPLE_XML = (
    b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\r\n'
    b"<DadosEconomicoFinanceiros><Header><CNPJ>08431747000106</CNPJ></Header>"
    b"</DadosEconomicoFinanceiros>"
)
SAMPLE_PDF = b"%PDF-1.6\r%\xe2\xe3\xcf\xd3\r\n1 0 obj\r<</Type/Catalog>>\rendobj\r%%EOF\r\n"


@pytest.fixture
def config(tmp_path: Path) -> Config:
    """A configuration rooted entirely inside a temporary directory."""
    return Config(
        paths=PathsConfig(
            data_root=tmp_path / "data",
            documents_root=tmp_path / "documents",
        ),
        retention=RetentionConfig(days=7),
        source=SourceConfig(
            base_url="https://fnet.test/fnet/publico",
            user_agent="fii-docs-watcher/test",
            min_request_interval_seconds=0.0,
            max_retries=2,
            backoff_base_seconds=0.0,
            backoff_max_seconds=0.0,
            read_timeout_seconds=5.0,
            page_length=200,
        ),
        cvm=CvmConfig(registry_url="https://cvm.test/registro_fundo_classe.zip"),
        audit=AuditConfig(frequency="never"),
        # Both formats, unlike the shipped default of PDF only: most of these
        # tests are about how an XML is validated, named and filed, so the
        # fixture has to ask for one. The tests that care about declining a
        # format narrow this themselves.
        download=DownloadConfig(stale_part_hours=6, formats=("pdf", "xml")),
        files=FilesConfig(),
        logging=LoggingConfig(level="DEBUG"),
    )


def make_row(
    document_id: int,
    *,
    delivery: date | None = None,
    version: int = 1,
    fund: str = "HEDGE BRASIL SHOPPING FUNDO DE INVESTIMENTO IMOBILIÁRIO",
    category: str = "Informes Periódicos",
    doc_type: str = "Informe Mensal Estruturado ",
    species: str = "",
    modality: str = "AP",
    status: str = "Ativo com visualização",
) -> dict:
    """One listing row, shaped exactly like the live responses.

    Includes the verified traps: the always-null identity fields, the blank
    `arquivoEstruturado`, the string-typed `formatoDataReferencia` and the
    trailing space on `tipoDocumento`.
    """
    delivery = delivery or today()
    return {
        "id": document_id,
        "versao": version,
        "descricaoFundo": fund,
        "categoriaDocumento": category,
        "tipoDocumento": doc_type,
        "especieDocumento": species,
        "dataEntrega": f"{delivery.strftime('%d/%m/%Y')} 09:30",
        "dataReferencia": "07/2026",
        "formatoDataReferencia": "2",
        "descricaoModalidade": "Apresentação",
        "modalidade": modality,
        "descricaoStatus": status,
        "status": "AC",
        "situacaoDocumento": "A",
        "fundoOuClasse": "Classe",
        "cnpjFundo": None,
        "idFundo": None,
        "nomeAdministrador": None,
        "arquivoEstruturado": " ",
        "nomePregao": "FII HEDGEBS",
    }


class FakeFnet:
    """An in-process stand-in for Fundos.NET.

    Deliberately honours the real quirks: `idFundo=0` returns nothing, the
    listing never echoes `idFundo`, and paging respects `s`/`l`.
    """

    def __init__(self) -> None:
        # fundosnet_id -> rows
        self.documents: dict[int, list[dict]] = {}
        self.funds: dict[str, list[dict]] = {}
        self.payloads: dict[int, bytes] = {}
        self.content_type: dict[int, str] = {}
        self.disposition: dict[int, str] = {}
        self.request_log: list[str] = []
        self.fail_downloads: set[int] = set()

    # ------------------------------------------------------------------ setup

    def add_documents(self, fundosnet_id: int, rows: list[dict]) -> None:
        self.documents.setdefault(fundosnet_id, []).extend(rows)
        for row in rows:
            self.payloads.setdefault(row["id"], SAMPLE_XML)
            self.content_type.setdefault(row["id"], "text/xml; charset=UTF-8")
            self.disposition.setdefault(
                row["id"],
                'attachment; filename="08431747000106-IFP'
                f"{row['dataEntrega'][:10].replace('/', '')}"
                f"V{row['versao']:02d}-{row['id']:09d}.xml\"",
            )

    def add_fund(self, term: str, entries: list[dict]) -> None:
        self.funds[term.upper()] = entries

    # ---------------------------------------------------------------- handler

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.rsplit("/", 1)[-1]
        params = dict(request.url.params)
        self.request_log.append(f"{path}?{request.url.query.decode()}")

        if path == "pesquisarGerenciadorDocumentosDados":
            return self._search(params)
        if path == "listarFundos":
            return self._list_funds(params)
        if path == "downloadDocumento":
            return self._download(params)
        if path == "listarTodasCategoriaPorTipoFundo":
            return httpx.Response(200, json=[{"id": 1, "descricao": "Assembleia", "sigla": "AS"}])
        return httpx.Response(404, text="not found")

    def _search(self, params: dict) -> httpx.Response:
        rows: list[dict]
        if "idFundo" in params:
            fundosnet_id = int(params["idFundo"])
            # Verified: 0 is not "all", it is a nonexistent id.
            rows = [] if fundosnet_id == 0 else list(self.documents.get(fundosnet_id, []))
        else:
            rows = [row for group in self.documents.values() for row in group]

        first = _parse_wire(params.get("dataInicial"))
        last = _parse_wire(params.get("dataFinal"))
        if first and last:
            rows = [row for row in rows if first <= _row_date(row) <= last]

        # The real endpoint only orders reliably by dataEntrega; mirror that so
        # tests exercise the same code path the production sort relies on.
        rows.sort(key=lambda r: (_row_date(r), r["id"]))

        start = int(params.get("s", 0))
        length = int(params.get("l", 200))
        if length > 200:
            return httpx.Response(500, text="Internal Server Error")

        page = rows[start : start + length]
        return httpx.Response(
            200,
            json={
                "draw": int(params.get("d", 1)),
                "recordsTotal": len(rows),
                "recordsFiltered": len(rows),
                "data": page,
            },
        )

    def _list_funds(self, params: dict) -> httpx.Response:
        term = str(params.get("term", "")).upper()
        page = int(params.get("page", 1))
        matches: list[dict] = []
        for key, entries in self.funds.items():
            if key in term or term in key:
                matches.extend(entries)
        # Real behaviour: 20 per page, with a `more` flag.
        window = matches[(page - 1) * 20 : page * 20]
        return httpx.Response(
            200, json={"results": window, "more": len(matches) > page * 20}
        )

    def _download(self, params: dict) -> httpx.Response:
        document_id = int(params["id"])
        if document_id in self.fail_downloads:
            return httpx.Response(500, text="Internal Server Error")
        body = self.payloads.get(document_id, SAMPLE_XML)
        return httpx.Response(
            200,
            content=body,
            headers={
                "Content-Type": self.content_type.get(document_id, "application/octet-stream"),
                "Content-Disposition": self.disposition.get(document_id, ""),
            },
        )

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)


def _parse_wire(value: str | None) -> date | None:
    if not value:
        return None
    day, month, year = value.split("/")
    return date(int(year), int(month), int(day))


def _row_date(row: dict) -> date:
    return _parse_wire(row["dataEntrega"][:10]) or date.min


@pytest.fixture
def fake_fnet() -> FakeFnet:
    return FakeFnet()


@pytest.fixture
def yesterday() -> date:
    return today() - timedelta(days=1)


def write_funds_yaml(
    path: Path, cnpj: str, fundosnet_id: int, *, ticker: str | None = None
) -> None:
    """Write a pre-resolved funds.yaml so a test can skip CVM resolution."""
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "cnpj": cnpj,
        "scope": "fund_and_classes",
        "legal_name": "HEDGE BRASIL SHOPPING FUNDO DE INVESTIMENTO IMOBILIÁRIO",
        "expansion": "complete",
        "registered_at": to_dir_name(today()),
        "entities": [
            {
                "kind": "class",
                "cnpj": cnpj,
                "fundosnet_id": fundosnet_id,
                "fnet_fund_description": "HEDGE BRASIL SHOPPING FUNDO DE INVESTIMENTO IMOBILIÁRIO",
                "validated_at": to_dir_name(today()),
                "cnpj_confirmed": False,
            }
        ],
    }
    if ticker:
        entry["ticker"] = ticker
    # Written as JSON, which is a subset of YAML, to keep the fixture honest
    # about quoting without depending on the writer under test.
    path.write_text(json.dumps({"scopes": [entry]}, ensure_ascii=False), encoding="utf-8")
