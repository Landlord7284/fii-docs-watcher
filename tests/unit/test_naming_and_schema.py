"""Filename construction and listing-row validation."""

from __future__ import annotations

import logging
from datetime import date

import pytest

from fii_docs_watcher.errors import SourceContractError
from fii_docs_watcher.fnet.schema import parse_row, parse_rows
from fii_docs_watcher.pipeline import naming


class TestSanitize:
    def test_accents_are_folded_rather_than_dropped(self) -> None:
        assert naming.sanitize("Informações Periódicas") == "Informacoes-Periodicas"

    def test_path_separators_and_control_characters_cannot_survive(self) -> None:
        assert "/" not in naming.sanitize("a/b\\c")
        assert naming.sanitize("x\x00y") == "x-y"
        for char in '<>:"|?*':
            assert char not in naming.sanitize(f"a{char}b")

    def test_trailing_whitespace_from_the_source_is_removed(self) -> None:
        # Real value: 'Informe Mensal Estruturado ' with a trailing space.
        assert naming.sanitize("Informe Mensal Estruturado ") == "Informe-Mensal-Estruturado"

    def test_windows_reserved_names_are_defused(self) -> None:
        # The archive is meant to be read over SMB.
        assert naming.sanitize("CON") == "CON-doc"
        assert naming.sanitize("aux") == "aux-doc"

    def test_long_values_are_truncated_without_a_trailing_separator(self) -> None:
        out = naming.sanitize("A" * 200)
        assert len(out) <= naming.MAX_COMPONENT
        assert not out.endswith(("-", "."))

    def test_empty_input_yields_empty_output(self) -> None:
        assert naming.sanitize("") == ""


class TestEntityPrefix:
    def test_a_ticker_wins_when_the_user_supplied_one(self) -> None:
        assert (
            naming.entity_prefix(ticker="HGBS11", fund_description="ANYTHING", cnpj="0843")
            == "HGBS11"
        )

    def test_otherwise_the_distinguishing_head_of_the_legal_name_is_used(self) -> None:
        # The boilerplate tail is identical across funds, so it carries no signal.
        prefix = naming.entity_prefix(
            ticker=None,
            fund_description="HEDGE BRASIL SHOPPING FUNDO DE INVESTIMENTO IMOBILIÁRIO "
            "DE RESPONSABILIDADE LIMITADA",
            cnpj="08431747000106",
        )
        assert prefix == "HEDGE-BRASIL-SHOPPING"

    def test_the_cnpj_is_the_last_resort_and_always_available(self) -> None:
        assert (
            naming.entity_prefix(ticker=None, fund_description="", cnpj="08.431.747/0001-06")
            == "08431747000106"
        )

    def test_nothing_at_all_still_produces_a_usable_component(self) -> None:
        assert naming.entity_prefix(ticker=None, fund_description="", cnpj=None) == "unknown"


class TestDocumentFilename:
    def test_the_version_is_present_so_a_refiling_cannot_overwrite_v1(self) -> None:
        v1 = naming.document_filename(
            prefix="HGBS11", category="Fato Relevante", species="",
            document_id=1277824, version=1, extension="pdf",
        )
        v2 = naming.document_filename(
            prefix="HGBS11", category="Fato Relevante", species="",
            document_id=1277824, version=2, extension="pdf",
        )
        assert v1 == "HGBS11_Fato-Relevante_1277824_V01.pdf"
        assert v1 != v2

    def test_species_distinguishes_same_day_assembly_documents(self) -> None:
        # For Assembleia it is especieDocumento, not tipoDocumento, that carries
        # the meaning; two filings can otherwise differ only by numeric id.
        edital = naming.document_filename(
            prefix="HGBS11", category="Assembleia", species="Edital de Convocação",
            document_id=1, version=1, extension="pdf",
        )
        carta = naming.document_filename(
            prefix="HGBS11", category="Assembleia", species="Carta Consulta",
            document_id=2, version=1, extension="pdf",
        )
        assert "Edital-de-Convocacao" in edital
        assert "Carta-Consulta" in carta

    def test_a_species_that_merely_repeats_the_category_is_not_duplicated(self) -> None:
        name = naming.document_filename(
            prefix="X", category="Regulamento", species="Regulamento",
            document_id=9, version=1, extension="pdf",
        )
        assert name.count("Regulamento") == 1

    def test_no_mutable_document_field_appears_in_the_name(self) -> None:
        # status/modality/situation change at the source after delivery; putting
        # them here would make the filename lie or force renames.
        name = naming.document_filename(
            prefix="HGBS11", category="Informes Periódicos", species="",
            document_id=1, version=1, extension="xml",
        )
        for mutable in ("Ativo", "Cancelado", "AP", "RE"):
            assert mutable not in name

    def test_part_filename_is_keyed_on_publication_identity(self) -> None:
        assert naming.part_filename(1291164, 2) == "1291164_V02.part"


class TestSchemaValidation:
    def _row(self, **overrides: object) -> dict:
        base = {
            "id": 1291164,
            "versao": 1,
            "descricaoFundo": "BRIO REAL ESTATE III",
            "categoriaDocumento": "Informes Periódicos",
            "tipoDocumento": "Informe Mensal Estruturado ",
            "especieDocumento": "",
            "dataEntrega": "14/08/2026 09:30",
            "dataReferencia": "07/2026",
            "formatoDataReferencia": "2",
            "modalidade": "AP",
            "descricaoStatus": "Ativo com visualização",
            "fundoOuClasse": "Classe",
        }
        base.update(overrides)
        return base

    def test_a_real_row_parses_and_is_cleaned(self) -> None:
        row = parse_row(self._row())
        assert row.identity == (1291164, 1)
        assert row.doc_type == "Informe Mensal Estruturado"  # trailing space gone
        assert row.delivery_date == date(2026, 8, 14)
        assert row.reference_date == "2026-07"

    def test_the_string_typed_discriminator_is_preserved_as_a_string(self) -> None:
        # The wire sends '2'/'3'/'4', not integers.
        assert parse_row(self._row()).reference_date_format == "2"

    def test_versao_arrives_as_an_int_but_a_string_is_accepted(self) -> None:
        assert parse_row(self._row(versao="2")).version == 2

    @pytest.mark.parametrize(
        "field", ["id", "versao", "dataEntrega", "categoriaDocumento", "descricaoFundo"]
    )
    def test_a_missing_critical_field_fails_loudly(self, field: str) -> None:
        with pytest.raises(SourceContractError, match="critical"):
            parse_row(self._row(**{field: None}))

    def test_accessory_fields_may_be_absent_without_dropping_the_row(self) -> None:
        # A cosmetic change at the source must not stop every entity at once.
        row = parse_row(
            self._row(especieDocumento=None, modalidade=None, dataReferencia=None,
                      formatoDataReferencia=None)
        )
        assert row.species == ""
        assert row.reference_date is None

    def test_the_always_null_identity_fields_are_simply_ignored(self) -> None:
        row = parse_row(self._row(cnpjFundo=None, idFundo=None, nomeAdministrador=None))
        assert row.identity == (1291164, 1)

    def test_structured_routing_uses_the_text_because_the_flag_is_useless(self) -> None:
        # arquivoEstruturado arrives as " " even for XML, so it cannot be used.
        assert parse_row(self._row(arquivoEstruturado=" ")).looks_structured
        assert not parse_row(
            self._row(categoriaDocumento="Assembleia", tipoDocumento="AGO")
        ).looks_structured

    def test_one_bad_row_does_not_take_the_good_ones_with_it(self, caplog) -> None:
        caplog.set_level(logging.ERROR)
        rows, errors = parse_rows([self._row(), self._row(id=None), self._row(id=99)])
        assert [r.document_id for r in rows] == [1291164, 99]
        assert len(errors) == 1
