"""Fetching a document and proving the bytes are what they claim to be.

`downloadDocumento` serves PDF and XML from the same endpoint, so the stored
extension is decided by the response, in a strict order of confidence:

    1. the content signature   -- decisive
    2. Content-Disposition     -- the served filename's extension
    3. Content-Type            -- least reliable; legacy systems answer
                                  application/octet-stream for everything

**A successful parse is not proof of anything.** An HTML error page served with
HTTP 200 is a real failure mode on this host, and an XHTML error page is
perfectly well-formed XML. So validation additionally requires a plausible root
element and explicitly rejects an `html` root, however well-formed the document.

XML is parsed with entity resolution disabled. The source is external and
untrusted, and a document that expands entities can read local files or hang the
process; `defusedxml` would be the other route, but refusing DTDs outright needs
no extra dependency and is stricter.
"""

from __future__ import annotations

import hashlib
import logging
import re
import xml.parsers.expat
from dataclasses import dataclass
from enum import StrEnum

from ..errors import ContentValidationError
from .client import FnetClient
from .content_disposition import ServedFile
from .content_disposition import parse as parse_disposition

log = logging.getLogger(__name__)

DOWNLOAD_PATH = "downloadDocumento"

PDF_SIGNATURE = b"%PDF-"

# Roots seen on real documents. The check is a denylist plus a sanity rule
# rather than an allowlist: the source adds document types over time, and
# refusing an unknown-but-plausible root would reject valid new filings.
REJECTED_XML_ROOTS = frozenset({"html", "xhtml"})

# Text that betrays an error page dressed up as a document.
_ERROR_PAGE_MARKERS = (
    b"<html",
    b"<!doctype html",
    b"nginx",
    b"apache tomcat",
    b"http status 4",
    b"http status 5",
    b"whitelabel error page",
    b"exce\xc3\xa7\xc3\xa3o",
)

_XML_DECL = re.compile(rb"^\s*(?:<\?xml|<!DOCTYPE|<)", re.IGNORECASE)


class ContentKind(StrEnum):
    PDF = "pdf"
    XML = "xml"


@dataclass(frozen=True)
class DownloadedDocument:
    """Validated bytes plus everything the response disclosed about them."""

    content: bytes
    kind: ContentKind
    extension: str
    content_hash: str
    served: ServedFile
    content_type: str | None

    @property
    def size(self) -> int:
        return len(self.content)


def _looks_like_error_page(content: bytes) -> bool:
    head = content[:4096].lower()
    return any(marker in head for marker in _ERROR_PAGE_MARKERS)


def _xml_root(content: bytes) -> str:
    """Return the root element's local name, having parsed the *whole* document.

    Parsing to the end matters as much as the root name does. Stopping at the
    first start tag would accept a truncated download -- a real risk from a
    source that intermittently stalls mid-response -- because the opening tag of
    a half-written file looks exactly like the opening tag of a complete one.

    expat is driven directly so DTD and external-entity handling can be switched
    off outright, which is what makes parsing untrusted input affordable. The
    response size is already capped by the client, so the full walk is bounded.
    """
    root: list[str] = []

    parser = xml.parsers.expat.ParserCreate()

    def start_element(name: str, _attrs: dict[str, str]) -> None:
        if not root:
            root.append(name)

    def external_entity_ref(*_args: object) -> int:
        # Refuse every external entity reference. Returning 0 makes expat raise,
        # which is what closes off XXE and SSRF through a crafted document.
        return 0

    def entity_decl(*_args: object) -> None:
        # No real Fundos.NET document declares entities, and an entity
        # declaration is the first half of both XXE and a billion-laughs
        # expansion. Refusing the declaration stops both before expansion.
        raise ContentValidationError("XML declares entities, which this pipeline refuses to parse")

    parser.StartElementHandler = start_element
    parser.ExternalEntityRefHandler = external_entity_ref
    parser.EntityDeclHandler = entity_decl
    # Belt and braces: never parse parameter entities, so a DTD cannot pull in
    # anything even if the handlers above are somehow bypassed.
    parser.SetParamEntityParsing(xml.parsers.expat.XML_PARAM_ENTITY_PARSING_NEVER)

    try:
        parser.Parse(content, True)
    except xml.parsers.expat.ExpatError as exc:
        raise ContentValidationError(
            f"content is not well-formed XML (truncated or corrupt): {exc}"
        ) from exc

    if not root:
        raise ContentValidationError("XML document has no root element")
    # `{ns}tag` or `ns:tag` -> `tag`; the prefix is not part of the identity.
    return root[0].rsplit("}", 1)[-1].rsplit(":", 1)[-1].strip().lower()


def validate(
    content: bytes,
    *,
    served: ServedFile,
    content_type: str | None,
    document_id: int,
    version: int,
) -> tuple[ContentKind, str]:
    """Decide what the bytes are, or refuse them. Returns `(kind, extension)`.

    Unrecognised content is always a noisy failure -- never written to the
    archive on the assumption that someone will notice later.
    """
    context = {"document_id": document_id, "version": version}

    if not content:
        raise ContentValidationError("empty response body", context=context)

    if content.startswith(PDF_SIGNATURE):
        return ContentKind.PDF, "pdf"

    if _XML_DECL.match(content):
        # Check for an error page before parsing: an XHTML error page parses
        # perfectly well, and a hit here gives a far more useful message.
        if _looks_like_error_page(content):
            preview = content[:200].decode("utf-8", "replace")
            raise ContentValidationError(
                f"HTTP 200 carrying an HTML error page rather than a document: {preview!r}",
                context=context,
            )
        root = _xml_root(content)
        if root in REJECTED_XML_ROOTS:
            raise ContentValidationError(
                f"well-formed XML but the root element is {root!r}, which means an error page, "
                "not a document",
                context={**context, "root": root},
            )
        if not root:
            raise ContentValidationError("XML root element has no name", context=context)
        return ContentKind.XML, "xml"

    if _looks_like_error_page(content):
        preview = content[:200].decode("utf-8", "replace")
        raise ContentValidationError(
            f"response body is an error page, not a document: {preview!r}", context=context
        )

    # Nothing matched a signature. Report what the weaker sources claimed, since
    # that is the first thing a human will want when this fires.
    raise ContentValidationError(
        f"unrecognised content: {len(content)} bytes starting {content[:16]!r} "
        f"(Content-Type={content_type!r}, served filename={served.filename!r})",
        context=context,
    )


def fetch(
    client: FnetClient, *, document_id: int, version: int, expect_structured: bool = False
) -> DownloadedDocument:
    """Download one document and validate it before it is allowed anywhere near disk.

    `expect_structured` is the caller's routing hint from the listing. It is only
    used to report a mismatch: the signature decides, and a relevant conflict
    between what was expected and what arrived is worth surfacing because it
    suggests the source's own routing changed.
    """
    response = client.get(DOWNLOAD_PATH, {"id": document_id})
    served = parse_disposition(response.headers.get("Content-Disposition"))
    content_type = response.headers.get("Content-Type")

    kind, extension = validate(
        response.content,
        served=served,
        content_type=content_type,
        document_id=document_id,
        version=version,
    )

    if served.parsed and served.extension and served.extension != extension:
        log.warning(
            "served filename extension disagrees with the content signature; "
            "trusting the signature",
            extra={
                "document_id": document_id,
                "version": version,
                "served_extension": served.extension,
                "detected": extension,
            },
        )

    if expect_structured and kind is not ContentKind.XML:
        log.warning(
            "document was routed as structured but did not arrive as XML",
            extra={"document_id": document_id, "version": version, "detected": kind.value},
        )

    # The hash is for integrity and audit. It is never a dedupe key: identity is
    # (document_id, version), and two different publications may share bytes.
    content_hash = hashlib.sha256(response.content).hexdigest()

    log.debug(
        "document fetched",
        extra={
            "document_id": document_id,
            "version": version,
            "kind": kind.value,
            "bytes": len(response.content),
        },
    )
    return DownloadedDocument(
        content=response.content,
        kind=kind,
        extension=extension,
        content_hash=content_hash,
        served=served,
        content_type=content_type,
    )


def validate_file(path_bytes: bytes, *, document_id: int, version: int) -> str | None:
    """Re-validate bytes already on disk, returning their hash, or None if invalid.

    Used by startup reconciliation to decide whether a file left behind by an
    interrupted run can be consolidated or has to be downloaded again.
    """
    try:
        validate(
            path_bytes,
            served=ServedFile(),
            content_type=None,
            document_id=document_id,
            version=version,
        )
    except ContentValidationError:
        return None
    return hashlib.sha256(path_bytes).hexdigest()
