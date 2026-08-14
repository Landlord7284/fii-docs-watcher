"""Parsing the served filename, which is the only place the entity CNPJ appears.

The listing returns `cnpjFundo` as null on every row, so nothing in the search
response says which entity actually emitted a document. The download response
does, incidentally, in its `Content-Disposition` filename:

    34895752000180-IFP14082026V01-001291164.xml
    └─ CNPJ ────┘ └┬┘└── date ─┘└┬┘ └── id ──┘
                   │             └─ version, V01
                   └─ category acronym (IFP = Informes Periódicos)

That makes it the closing check on a resolution chain that is otherwise textual:
we matched a legal name to an internal id, and this is the CNPJ confirming we
matched the right one.

Parsing is best-effort by design. If the source changes this format the pipeline
logs it loudly and keeps going on the CNPJ that originated the query -- a
cosmetic change to a filename must never stop documents from being archived.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

log = logging.getLogger(__name__)

# `filename*=UTF-8''...` (RFC 5987) takes precedence over plain `filename=` when
# both are present, which is what the standard requires.
_EXTENDED = re.compile(r"filename\*\s*=\s*[^']*''(?P<value>[^;]+)", re.IGNORECASE)
_PLAIN = re.compile(r'filename\s*=\s*(?:"(?P<quoted>[^"]*)"|(?P<bare>[^;]+))', re.IGNORECASE)

_SERVED_NAME = re.compile(
    r"^(?P<cnpj>\d{14})"
    r"-(?P<acronym>[A-Za-z]+)(?P<delivery>\d{8})V(?P<version>\d+)"
    r"-(?P<document_id>\d+)"
    r"\.(?P<extension>[A-Za-z0-9]+)$"
)


@dataclass(frozen=True)
class ServedFile:
    """What the served filename told us. Every field may be absent."""

    filename: str | None = None
    cnpj: str | None = None
    category_acronym: str | None = None
    document_id: int | None = None
    version: int | None = None
    extension: str | None = None

    @property
    def parsed(self) -> bool:
        """Whether the structured form was recognised, as opposed to just a name."""
        return self.cnpj is not None


def extract_filename(header: str | None) -> str | None:
    """Pull the filename out of a `Content-Disposition` header value."""
    if not header:
        return None
    match = _EXTENDED.search(header)
    if match:
        from urllib.parse import unquote

        return unquote(match.group("value").strip()) or None
    match = _PLAIN.search(header)
    if not match:
        return None
    name = (match.group("quoted") or match.group("bare") or "").strip()
    return name or None


def parse(header: str | None) -> ServedFile:
    """Parse a `Content-Disposition` header. Never raises.

    A header that yields nothing, or a filename in an unfamiliar shape, produces
    a `ServedFile` with `parsed` false. Callers fall back to the CNPJ that
    originated the query.
    """
    filename = extract_filename(header)
    if filename is None:
        return ServedFile()

    match = _SERVED_NAME.match(filename)
    if not match:
        log.warning(
            "served filename does not match the known Fundos.NET pattern; "
            "falling back to the queried entity's CNPJ",
            # Not `filename`: LogRecord reserves that attribute name and logging
            # raises rather than overwrite it.
            extra={"served_filename": filename},
        )
        return ServedFile(filename=filename)

    try:
        version = int(match.group("version"))
        document_id = int(match.group("document_id"))
    except ValueError:  # pragma: no cover - the pattern only matches digits
        return ServedFile(filename=filename)

    return ServedFile(
        filename=filename,
        cnpj=match.group("cnpj"),
        category_acronym=match.group("acronym").upper(),
        document_id=document_id,
        version=version,
        extension=match.group("extension").lower(),
    )
