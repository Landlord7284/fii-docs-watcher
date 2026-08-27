"""Content validation, the security-relevant part.

The source is public, unauthenticated and untrusted, and it is known to answer
HTTP 200 with an HTML error page. Everything here is a case that must never
reach the archive.
"""

from __future__ import annotations

import pytest

from fii_docs_watcher.errors import ContentValidationError
from fii_docs_watcher.fnet.content_disposition import ServedFile, parse
from fii_docs_watcher.fnet.download import ContentKind, validate, validate_file

REAL_XML = (
    b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\r\n'
    b"<DadosEconomicoFinanceiros><x/></DadosEconomicoFinanceiros>"
)
REAL_PDF = (
    b"%PDF-1.6\r%\xe2\xe3\xcf\xd3\r\n218 0 obj\r<</Linearized 1>>\rendobj\r%%EOF\r\n"
)


def _validate(content: bytes, **kwargs: object):
    return validate(
        content,
        served=kwargs.pop("served", ServedFile()),  # type: ignore[arg-type]
        content_type=kwargs.pop("content_type", None),  # type: ignore[arg-type]
        document_id=1,
        version=1,
    )


class TestAccepts:
    def test_pdf_recognised_by_signature(self) -> None:
        assert _validate(REAL_PDF) == (ContentKind.PDF, "pdf")

    def test_xml_recognised_by_parse_and_a_plausible_root(self) -> None:
        assert _validate(REAL_XML) == (ContentKind.XML, "xml")

    def test_the_signature_wins_over_a_lying_content_type(self) -> None:
        # Legacy systems answer application/octet-stream for everything, so the
        # weakest source must never override the strongest.
        assert _validate(REAL_PDF, content_type="application/octet-stream")[0] is ContentKind.PDF
        assert _validate(REAL_XML, content_type="application/pdf")[0] is ContentKind.XML


class TestRejects:
    def test_html_error_page(self) -> None:
        with pytest.raises(ContentValidationError, match="error page"):
            _validate(b"<!DOCTYPE html><html><body>HTTP Status 500</body></html>")

    def test_well_formed_xhtml_error_page(self) -> None:
        # The whole point: a successful parse proves nothing.
        with pytest.raises(ContentValidationError):
            _validate(
                b'<?xml version="1.0"?><html xmlns="http://www.w3.org/1999/xhtml">'
                b"<body>error</body></html>"
            )

    def test_xml_whose_root_is_html_is_rejected_by_the_root_check(self) -> None:
        # Namespaced, so the literal "<html" error-page marker does not fire and
        # the root-element rule has to carry the rejection on its own.
        with pytest.raises(ContentValidationError, match="root element"):
            _validate(
                b'<?xml version="1.0"?>'
                b'<x:html xmlns:x="http://www.w3.org/1999/xhtml"><x:body>hi</x:body></x:html>'
            )

    def test_xxe_payload_is_refused_before_any_entity_is_resolved(self) -> None:
        payload = (
            b'<?xml version="1.0"?>'
            b'<!DOCTYPE r [<!ENTITY x SYSTEM "file:///etc/passwd">]>'
            b"<r>&x;</r>"
        )
        with pytest.raises(ContentValidationError, match="entities"):
            _validate(payload)

    def test_billion_laughs_is_refused_at_the_declaration(self) -> None:
        payload = (
            b'<?xml version="1.0"?>'
            b'<!DOCTYPE l [<!ENTITY a "aa"><!ENTITY b "&a;&a;&a;&a;">]>'
            b"<l>&b;</l>"
        )
        with pytest.raises(ContentValidationError, match="entities"):
            _validate(payload)

    def test_empty_body(self) -> None:
        with pytest.raises(ContentValidationError, match="empty"):
            _validate(b"")

    def test_truncated_pdf_signature(self) -> None:
        with pytest.raises(ContentValidationError, match="unrecognised"):
            _validate(b"%PDF")

    def test_pdf_with_a_header_but_no_terminal_marker_is_rejected(self) -> None:
        with pytest.raises(ContentValidationError, match="truncated or corrupt"):
            _validate(b"%PDF-1.7\n1 0 obj\n")

    def test_arbitrary_bytes(self) -> None:
        with pytest.raises(ContentValidationError, match="unrecognised"):
            _validate(b"\x00\x01\x02 not a document")

    def test_malformed_xml(self) -> None:
        with pytest.raises(ContentValidationError, match="well-formed"):
            _validate(b'<?xml version="1.0"?><unclosed>')

    def test_a_truncated_xml_document_is_rejected(self) -> None:
        # This host stalls mid-response often enough that a half-written body is
        # a realistic outcome, and its opening tag looks entirely healthy -- so
        # validation has to parse through to the end, not stop at the root.
        truncated = REAL_XML[: len(REAL_XML) // 2]
        with pytest.raises(ContentValidationError, match="truncated or corrupt"):
            _validate(truncated)


class TestValidateFile:
    def test_returns_a_hash_for_valid_bytes(self) -> None:
        assert validate_file(REAL_PDF, document_id=1, version=1) is not None

    def test_returns_none_for_invalid_bytes_so_reconciliation_requeues(self) -> None:
        assert validate_file(b"<html>error</html>", document_id=1, version=1) is None


class TestContentDisposition:
    def test_the_verified_real_world_forms(self) -> None:
        served = parse('attachment; filename="34895752000180-IFP14082026V01-001291164.xml"')
        assert served.parsed
        assert served.cnpj == "34895752000180"
        assert served.category_acronym == "IFP"
        assert served.document_id == 1291164
        assert served.version == 1
        assert served.extension == "xml"

    def test_rfc5987_extended_form_takes_precedence(self) -> None:
        served = parse(
            "attachment; filename=\"fallback.pdf\"; "
            "filename*=UTF-8''08431747000106-OPD11082026V01-001283463.pdf"
        )
        assert served.cnpj == "08431747000106"
        assert served.extension == "pdf"

    @pytest.mark.parametrize("header", [None, "", "inline", "attachment"])
    def test_absent_or_useless_headers_never_raise(self, header: str | None) -> None:
        served = parse(header)
        assert not served.parsed
        assert served.cnpj is None

    def test_an_unfamiliar_shape_degrades_to_the_bare_filename(self) -> None:
        # Best-effort by contract: a format change must not halt the pipeline.
        served = parse('attachment; filename="something-else.pdf"')
        assert not served.parsed
        assert served.filename == "something-else.pdf"
        assert served.cnpj is None

    @pytest.mark.parametrize(
        ("served", "message"),
        [
            (ServedFile(document_id=2, version=1), "document id"),
            (ServedFile(document_id=1, version=2), "version"),
        ],
    )
    def test_served_publication_identity_must_match_the_request(
        self, served: ServedFile, message: str
    ) -> None:
        with pytest.raises(ContentValidationError, match=message):
            _validate(REAL_PDF, served=served)
