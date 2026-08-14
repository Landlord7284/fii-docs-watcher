"""Turning a raw listing row into something safe to act on.

Validation is deliberately two-tiered. Fields whose absence or type change
would invalidate processing fail loudly, because silently filing a document
with a wrong date or a missing version is worse than not filing it. Accessory
and known-nullable fields never drop a row -- otherwise any cosmetic change at
the source would stop every entity at once.

Type notes verified against live responses, all of which the wire actually does:

    versao                 int      (1, 2)
    formatoDataReferencia  str      ('2', '3', '4')  -- a string, not an int
    tipoDocumento          str      may carry trailing spaces, may be empty
    especieDocumento       str      empty for most categories; for Assembleia it,
                                    not tipoDocumento, carries the meaning
    cnpjFundo / idFundo    null     on every row, even when filtering by idFundo
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from ..clock import parse_delivery, parse_reference
from ..errors import SourceContractError

log = logging.getLogger(__name__)

# Absence or unusable type here invalidates the record (spec section 8).
# `status` is included because "cancelled" has to be observable; `especieDocumento`
# is not, because it is legitimately empty outside Assembleia.
CRITICAL_FIELDS = (
    "id",
    "versao",
    "dataEntrega",
    "categoriaDocumento",
    "descricaoFundo",
)

# Marks a document whose payload is XML rather than PDF. Only ever used to route
# early -- what actually gets written to disk is decided by the response bytes.
STRUCTURED_MARKER = "estruturad"


def looks_structured(category: str | None, doc_type: str | None, species: str | None) -> bool:
    """Predict, from the listing alone, whether a document will arrive as XML.

    A routing hint, never a verdict: it decides what is worth fetching *before*
    fetching it, and the content signature still has the final say once the
    bytes arrive. `arquivoEstruturado` would be the obvious flag but comes back
    as `" "` even for XML, so the category, type and species text are all there
    is to go on -- and all three matter, because the marker appears in different
    ones depending on the document.
    """
    haystack = f"{category or ''} {doc_type or ''} {species or ''}".lower()
    return STRUCTURED_MARKER in haystack


@dataclass(frozen=True)
class DocumentRow:
    """One document as the source described it, normalised but not yet fetched."""

    document_id: int
    version: int
    fund_description: str
    category: str
    doc_type: str
    species: str
    delivery_at: datetime
    reference_date: str | None
    reference_date_format: str | None
    modality: str
    status: str
    fund_or_class: str

    @property
    def delivery_date(self) -> date:
        """The archiving axis: which date directory this document belongs in."""
        return self.delivery_at.date()

    @property
    def identity(self) -> tuple[int, int]:
        """Publication identity. The dedupe key, everywhere, always."""
        return (self.document_id, self.version)

    @property
    def looks_structured(self) -> bool:
        """Whether this is expected to be XML. See `looks_structured` above."""
        return looks_structured(self.category, self.doc_type, self.species)


def _text(value: Any) -> str:
    """Coerce a wire value to clean text.

    Trailing whitespace is real in this data (`'Informe Mensal Estruturado '`)
    and would otherwise leak into filenames and grouping keys.
    """
    if value is None:
        return ""
    return str(value).strip()


def _int(value: Any, field: str, row: dict[str, Any]) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise SourceContractError(
            f"field {field!r} is not an integer: {value!r}",
            context={"field": field, "value": value, "document_id": row.get("id")},
        ) from exc


def parse_row(raw: dict[str, Any]) -> DocumentRow:
    """Validate and normalise one row from `pesquisarGerenciadorDocumentosDados`.

    Raises `SourceContractError` when a critical field is missing or unusable.
    """
    missing = [name for name in CRITICAL_FIELDS if raw.get(name) in (None, "")]
    if missing:
        raise SourceContractError(
            f"listing row is missing critical field(s): {', '.join(missing)}",
            context={"missing": missing, "document_id": raw.get("id")},
        )

    document_id = _int(raw.get("id"), "id", raw)
    version = _int(raw.get("versao"), "versao", raw)
    delivery_at = parse_delivery(_text(raw.get("dataEntrega")))

    # Arrives as a string ('2'/'3'/'4'); kept as one so the discriminator round-trips.
    reference_format = _text(raw.get("formatoDataReferencia")) or None
    reference_date = parse_reference(_text(raw.get("dataReferencia")), reference_format)

    return DocumentRow(
        document_id=document_id,
        version=version,
        fund_description=_text(raw.get("descricaoFundo")),
        category=_text(raw.get("categoriaDocumento")),
        doc_type=_text(raw.get("tipoDocumento")),
        species=_text(raw.get("especieDocumento")),
        delivery_at=delivery_at,
        reference_date=reference_date,
        reference_date_format=reference_format,
        modality=_text(raw.get("modalidade")),
        status=_text(raw.get("descricaoStatus")) or _text(raw.get("status")),
        fund_or_class=_text(raw.get("fundoOuClasse")),
    )


def parse_rows(raws: list[dict[str, Any]]) -> tuple[list[DocumentRow], list[SourceContractError]]:
    """Parse a page, isolating bad rows instead of losing the good ones.

    A single malformed row is recorded and skipped; the caller decides how loudly
    to report it. Returning the errors rather than logging them here keeps the
    decision about severity with the code that knows which entity is affected.
    """
    rows: list[DocumentRow] = []
    errors: list[SourceContractError] = []
    for raw in raws:
        try:
            rows.append(parse_row(raw))
        except SourceContractError as exc:
            errors.append(exc)
    return rows, errors
