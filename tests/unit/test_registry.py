"""CVM registry indexing: expansion, name search, and the duplicates in real data."""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import httpx
import pytest

from fii_docs_watcher.config import CvmConfig
from fii_docs_watcher.cvm.registry import (
    SERVABLE_FAMILIES,
    RegisteredClass,
    RegisteredFund,
    RegistryCache,
    RegistrySnapshot,
    class_family,
    fold_name,
    parse_archive,
)
from fii_docs_watcher.errors import SourceContractError


def _archive(
    *, funds: list[tuple[str, str, str, str]], classes: list[tuple[str, str, str, str, str]]
) -> bytes:
    """Build a registry ZIP with the real encoding, delimiter and column names."""
    fund_rows = ["ID_Registro_Fundo;CNPJ_Fundo;Codigo_CVM;Tipo_Fundo;Denominacao_Social;Situacao"]
    fund_rows += [
        f"{registry_id};{cnpj};1;{family};{name};Em Funcionamento Normal"
        for registry_id, cnpj, name, family in funds
    ]
    class_rows = [
        "ID_Registro_Classe;ID_Registro_Fundo;CNPJ_Classe;Codigo_CVM;Tipo_Classe;"
        "Denominacao_Social;Situacao"
    ]
    class_rows += [
        f"{registry_id};{fund_id};{cnpj};1;{tipo};{name};Em Funcionamento Normal"
        for registry_id, fund_id, cnpj, name, tipo in classes
    ]

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("registro_fundo.csv", "\r\n".join(fund_rows).encode("latin-1"))
        archive.writestr("registro_classe.csv", "\r\n".join(class_rows).encode("latin-1"))
    return buffer.getvalue()


def _fund(
    registry_id: str,
    cnpj: str,
    name: str,
    situation: str = "Em Funcionamento Normal",
    family: str = "FII",
):
    return RegisteredFund(
        registry_id=registry_id,
        cnpj=cnpj,
        cvm_code="1",
        legal_name=name,
        situation=situation,
        family=family,
    )


def _klass(
    registry_id: str,
    fund_registry_id: str,
    cnpj: str,
    name: str,
    situation: str = "Em Funcionamento Normal",
    family: str = "FII",
):
    return RegisteredClass(
        registry_id=registry_id,
        fund_registry_id=fund_registry_id,
        cnpj=cnpj,
        cvm_code="1",
        legal_name=name,
        situation=situation,
        family=family,
    )


@pytest.fixture
def snapshot() -> RegistrySnapshot:
    funds = [
        # Monoclass: fund and its single class share the CNPJ, the common shape.
        _fund("1", "08431747000106", "HEDGE BRASIL SHOPPING FII"),
        # Genuinely multiclass, each class with its own CNPJ.
        _fund("2", "59849627000164", "REAL ESTATE TRANCOSO II FII"),
        # No class rows at all: not yet adapted to RCVM 175.
        _fund("3", "12005956000165", "KINEA RENDA IMOBILIÁRIA FII"),
        _fund("4", "99999999000199", "LIQUIDATED FUND FII", situation="Cancelado"),
    ]
    classes = [
        _klass("10", "1", "08431747000106", "HEDGE BRASIL SHOPPING FII"),
        _klass("20", "2", "59890241000104", "REAL ESTATE TRANCOSO II FII - CLASSE A"),
        _klass("21", "2", "59891323000165", "REAL ESTATE TRANCOSO II FII - CLASSE B"),
        _klass("22", "2", "11111111000191", "REAL ESTATE TRANCOSO II FII - CLASSE C",
               situation="Cancelado"),
    ]
    return RegistrySnapshot(funds, classes, fetched_at="2026-08-14T00:00:00-03:00")


class TestExpansion:
    def test_a_monoclass_fund_collapses_to_one_entity(self, snapshot) -> None:
        # The fund and its only class share a CNPJ; listing both would query the
        # same entity twice.
        anchor, entities = snapshot.expand("08431747000106")
        assert anchor is not None
        assert len(entities) == 1
        assert entities[0].kind == "class"

    def test_a_fund_cnpj_yields_the_fund_and_its_active_classes(self, snapshot) -> None:
        _anchor, entities = snapshot.expand("59849627000164")
        cnpjs = {e.cnpj for e in entities}
        assert cnpjs == {"59849627000164", "59890241000104", "59891323000165"}
        # The cancelled class is excluded.
        assert "11111111000191" not in cnpjs

    def test_a_class_cnpj_yields_only_that_class(self, snapshot) -> None:
        # Registering a class is an explicit request to monitor just that one.
        anchor, entities = snapshot.expand("59890241000104")
        assert anchor is not None and anchor.kind == "class"
        assert [e.cnpj for e in entities] == ["59890241000104"]

    def test_a_fund_with_no_classes_still_resolves_to_itself(self, snapshot) -> None:
        # 694 of the real registry's FII funds are in this state; returning
        # nothing would leave them unmonitorable.
        _anchor, entities = snapshot.expand("12005956000165")
        assert len(entities) == 1
        assert entities[0].kind == "fund"

    def test_duplicate_class_registrations_are_collapsed(self) -> None:
        # Real data: one fund's classes are registered twice under the same CNPJ
        # and the same name. Left in, the robot would archive it twice.
        funds = [_fund("1", "38498758000174", "IC LOTEAMENTOS E RECEBÍVEIS FII")]
        classes = [
            _klass("34940", "1", "38498758000174", "IC LOTEAMENTOS E RECEBÍVEIS FII"),
            _klass("34941", "1", "38498758000174", "IC LOTEAMENTOS E RECEBÍVEIS FII"),
        ]
        _anchor, entities = RegistrySnapshot(funds, classes, "x").expand("38498758000174")
        assert len(entities) == 1

    def test_duplicate_class_registrations_merge_candidate_types(self) -> None:
        funds = [
            _fund("1", "57305969000198", "POLLI FIAGRO"),
            _fund("2", "57305969000198", "POLLI FIAGRO", family="FIAGRO"),
        ]
        classes = [
            _klass("10", "1", "59890241000104", "POLLI CLASS", family="FII"),
            _klass("20", "2", "59890241000104", "POLLI CLASS", family="FIAGRO"),
        ]

        _anchor, entities = RegistrySnapshot(funds, classes, "x").expand("57305969000198")

        entity = next(item for item in entities if item.cnpj == "59890241000104")
        assert entity.candidate_fnet_types == (1, 11)

    def test_an_unknown_cnpj_resolves_to_nothing(self, snapshot) -> None:
        anchor, entities = snapshot.expand("00000000000191")
        assert anchor is None
        assert entities == []

    def test_a_cancelled_fund_still_resolves_but_is_marked_inactive(self, snapshot) -> None:
        # Monitoring continues; the situation is surfaced rather than enforced.
        anchor, _entities = snapshot.expand("99999999000199")
        assert anchor is not None
        assert not anchor.active


class TestFamilies:
    def test_the_class_family_is_the_token_after_the_prefix(self) -> None:
        assert class_family("Classes de Cotas de Fundos FII") == "FII"
        assert class_family("Classes de Cotas de Fundos FIAGRO") == "FIAGRO"
        # A parenthesised sub-kind does not change the family.
        assert class_family("Classes de Cotas de Fundos FIF (FAPI)") == "FIF"
        assert class_family("something else entirely") == ""

    def test_the_index_fund_family_is_not_mistaken_for_a_real_estate_one(self) -> None:
        # `FII` is a prefix of `FIIM`, so a substring test quietly admits
        # index funds -- a different category, served under a different type.
        assert class_family("Classes de Cotas de Fundos FIIM") == "FIIM"
        assert "FIIM" not in SERVABLE_FAMILIES

    def test_a_real_estate_fund_is_only_looked_for_under_its_own_type(self) -> None:
        snapshot = RegistrySnapshot([_fund("1", "08431747000106", "A FII")], [], "x")
        anchor, _entities = snapshot.expand("08431747000106")
        assert anchor is not None
        assert anchor.candidate_fnet_types == (1,)

    def test_an_agro_fund_falls_back_to_the_real_estate_type(self) -> None:
        # The registry types a FIAGRO-Imobiliario as `FII` and the agro-only
        # ones as `FIAGRO`, but the split is not reliable enough to bet one
        # request on, so the second type is tried when the first finds nothing.
        snapshot = RegistrySnapshot(
            [_fund("1", "57305969000198", "POLLI FIAGRO", family="FIAGRO")], [], "x"
        )
        anchor, _entities = snapshot.expand("57305969000198")
        assert anchor is not None
        assert anchor.candidate_fnet_types == (11, 1)

    def test_one_cnpj_registered_under_two_families_offers_both_types(self) -> None:
        # Real data: 72 CNPJs carry rows in more than one family. Keeping only
        # the first row seen would decide the type by the order of the file.
        snapshot = RegistrySnapshot(
            [
                _fund("1", "57305969000198", "POLLI FIAGRO"),
                _fund("2", "57305969000198", "POLLI FIAGRO", family="FIAGRO"),
            ],
            [],
            "x",
        )
        anchor, _entities = snapshot.expand("57305969000198")
        assert anchor is not None
        assert anchor.candidate_fnet_types == (1, 11)

    def test_classes_of_every_registration_of_one_cnpj_are_expanded(self) -> None:
        # A fund re-registered under a second ID_Registro_Fundo keeps classes on
        # both; expanding only one of them would silently drop the others.
        snapshot = RegistrySnapshot(
            [
                _fund("1", "59849627000164", "TRANCOSO II"),
                _fund("2", "59849627000164", "TRANCOSO II"),
            ],
            [
                _klass("10", "1", "59890241000104", "TRANCOSO II - CLASSE A"),
                _klass("20", "2", "59891323000165", "TRANCOSO II - CLASSE B"),
            ],
            "x",
        )
        _anchor, entities = snapshot.expand("59849627000164")
        assert {e.cnpj for e in entities} == {
            "59849627000164",
            "59890241000104",
            "59891323000165",
        }

    def test_an_active_registration_describes_a_cnpj_that_also_has_a_dead_one(self) -> None:
        snapshot = RegistrySnapshot(
            [
                _fund("1", "26324298000189", "OLD NAME", situation="Cancelado"),
                _fund("2", "26324298000189", "CURRENT NAME"),
            ],
            [],
            "x",
        )
        anchor, _entities = snapshot.expand("26324298000189")
        assert anchor is not None
        assert anchor.legal_name == "CURRENT NAME"
        assert anchor.active


class TestParsing:
    def test_only_the_monitorable_families_survive_the_archive(self) -> None:
        archive = _archive(
            funds=[
                ("1", "08431747000106", "A FII", "FII"),
                ("2", "57305969000198", "A FIAGRO", "FIAGRO"),
                ("3", "26324298000189", "SOMETHING ELSE", "FIF"),
            ],
            classes=[
                ("10", "1", "08431747000106", "A FII", "Classes de Cotas de Fundos FII"),
                ("11", "9", "64802589000124", "AN INDEX FUND",
                 "Classes de Cotas de Fundos FIIM"),
            ],
        )
        snapshot = parse_archive(archive)
        assert snapshot.fund_count == 2
        assert snapshot.class_count == 1
        assert snapshot.lookup("26324298000189") is None
        assert snapshot.lookup("64802589000124") is None
        assert snapshot.lookup("57305969000198") is not None

    def test_an_archive_with_nothing_monitorable_is_refused(self) -> None:
        archive = _archive(
            funds=[("1", "26324298000189", "SOMETHING ELSE", "FIF")],
            classes=[],
        )
        with pytest.raises(ValueError):
            parse_archive(archive)

    def test_a_missing_structural_column_is_refused(self) -> None:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr(
                "registro_fundo.csv",
                "CNPJ_Fundo;Codigo_CVM;Tipo_Fundo;Denominacao_Social;Situacao\r\n"
                "08431747000106;1;FII;A FII;Em Funcionamento Normal",
            )
            archive.writestr(
                "registro_classe.csv",
                "ID_Registro_Classe;ID_Registro_Fundo;CNPJ_Classe;Codigo_CVM;Tipo_Classe;"
                "Denominacao_Social;Situacao\r\n",
            )

        with pytest.raises(ValueError, match="ID_Registro_Fundo"):
            parse_archive(buffer.getvalue())

    def test_an_empty_structural_id_is_refused(self) -> None:
        archive = _archive(
            funds=[("", "08431747000106", "A FII", "FII")],
            classes=[],
        )

        with pytest.raises(ValueError, match="empty registry id"):
            parse_archive(archive)

    def test_an_archive_with_excessive_expanded_size_is_refused(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        archive = _archive(
            funds=[("1", "08431747000106", "A FII", "FII")],
            classes=[],
        )
        monkeypatch.setattr("fii_docs_watcher.cvm.registry._MAX_UNCOMPRESSED_REGISTRY_BYTES", 1)

        with pytest.raises(ValueError, match="expands beyond"):
            parse_archive(archive)


class TestCache:
    def test_non_object_state_is_treated_as_invalid(self, tmp_path: Path) -> None:
        cache = RegistryCache(CvmConfig(), tmp_path)
        cache.state_path.write_text("[]", encoding="utf-8")
        cache.archive_path.write_bytes(b"not relevant")

        assert cache._state() == {}
        assert not cache._is_fresh()

    def test_refresh_publishes_digest_bound_atomic_files(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        data = _archive(
            funds=[("1", "08431747000106", "A FII", "FII")],
            classes=[],
        )
        cache = RegistryCache(CvmConfig(), tmp_path)
        monkeypatch.setattr(cache, "_download", lambda _user_agent: data)

        snapshot = cache.load("test/1.0")

        assert snapshot is not None
        state = json.loads(cache.state_path.read_text(encoding="utf-8"))
        assert len(state["sha256"]) == 64
        assert cache._is_fresh()
        assert list(tmp_path.glob("*.tmp")) == []

    def test_digest_mismatch_makes_the_cache_stale(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        data = _archive(
            funds=[("1", "08431747000106", "A FII", "FII")],
            classes=[],
        )
        cache = RegistryCache(CvmConfig(), tmp_path)
        monkeypatch.setattr(cache, "_download", lambda _user_agent: data)
        snapshot = cache.load("test/1.0")
        assert snapshot is not None

        cache.archive_path.write_bytes(data + b"changed")

        assert not cache._is_fresh()

    def test_registry_response_is_capped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        transport = httpx.MockTransport(lambda _request: httpx.Response(200, content=b"x" * 11))
        real_client = httpx.Client

        def client_factory(**kwargs):
            return real_client(transport=transport, **kwargs)

        monkeypatch.setattr("fii_docs_watcher.cvm.registry.httpx.Client", client_factory)
        cache = RegistryCache(CvmConfig(max_response_bytes=10), tmp_path)

        with pytest.raises(SourceContractError, match="max_response_bytes"):
            cache._download("test/1.0")


class TestNameSearch:
    def test_finds_a_fund_by_a_fragment_of_its_name(self, snapshot) -> None:
        matches = snapshot.search_by_name("kinea renda")
        assert [m.cnpj for m in matches] == ["12005956000165"]

    def test_search_ignores_accents_and_case(self, snapshot) -> None:
        assert snapshot.search_by_name("IMOBILIARIA")
        assert snapshot.search_by_name("imobiliária")

    def test_funds_are_listed_before_classes(self, snapshot) -> None:
        matches = snapshot.search_by_name("TRANCOSO")
        assert matches[0].kind == "fund"

    def test_a_monoclass_class_is_not_listed_twice(self, snapshot) -> None:
        matches = snapshot.search_by_name("HEDGE BRASIL")
        assert len(matches) == 1

    def test_inactive_entities_sort_last(self, snapshot) -> None:
        matches = snapshot.search_by_name("FII")
        assert matches[-1].active is False

    def test_an_empty_term_matches_nothing(self, snapshot) -> None:
        assert snapshot.search_by_name("") == []
        assert snapshot.search_by_name("   ") == []


class TestFoldName:
    def test_folds_accents_case_punctuation_and_runs_of_space(self) -> None:
        assert fold_name("Hedge  Brasil - Imobiliário") == "HEDGE BRASIL IMOBILIARIO"

    def test_two_spellings_of_one_name_fold_together(self) -> None:
        assert fold_name("REAL ESTATE - FII") == fold_name("Real Estate   FII")
