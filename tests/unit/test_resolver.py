"""Resolving a registry entity to a Fundos.NET id *and* fund type.

The type has to be discovered rather than assumed: a name is only listed under
the category it is filed as, and querying the wrong one comes back empty instead
of failing. These tests pin the probing order and what gets persisted.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from conftest import FakeFnet, make_row
from fii_docs_watcher.cvm.registry import RegistryEntity
from fii_docs_watcher.errors import ScopeResolutionError, SourceContractError, TransientSourceError
from fii_docs_watcher.fnet.client import FnetClient
from fii_docs_watcher.scope import resolver
from fii_docs_watcher.scope.models import Entity, Scope
from fii_docs_watcher.scope.resolver import resolve_scope

REAL_ESTATE = 1
AGRO = 11

CNPJ = "57305969000198"
NAME = "POLLI FIAGRO - DIREITOS CREDITORIOS"
FUND_ID = 15104


class FakeSnapshot:
    """Just enough of a `RegistrySnapshot` to drive the resolver."""

    def __init__(self, entity: RegistryEntity | None) -> None:
        self.entity = entity

    def expand(self, cnpj: str):
        del cnpj
        return (self.entity, [self.entity]) if self.entity else (None, [])

    def lookup(self, cnpj: str):
        del cnpj
        return self.entity


def _entity(candidate_types: tuple[int, ...]) -> RegistryEntity:
    return RegistryEntity(
        cnpj=CNPJ,
        legal_name=NAME,
        cvm_code="267546",
        situation="Em Funcionamento Normal",
        kind="class",
        candidate_fnet_types=candidate_types,
    )


def _client(config, fake_fnet: FakeFnet) -> FnetClient:
    return FnetClient(config.source, transport=fake_fnet.transport)


class TestFundTypeDiscovery:
    def test_an_entity_listed_only_under_agro_resolves_there(self, config, fake_fnet) -> None:
        fake_fnet.add_fund(NAME, [{"id": FUND_ID, "text": NAME}], fund_type=AGRO)
        fake_fnet.add_documents(FUND_ID, [make_row(1001)], fund_type=AGRO)

        scope = Scope(cnpj=CNPJ)
        with _client(config, fake_fnet) as client:
            resolve_scope(client, FakeSnapshot(_entity((AGRO, REAL_ESTATE))), scope)

        assert [(e.fundosnet_id, e.fnet_fund_type) for e in scope.entities] == [(FUND_ID, AGRO)]

    def test_the_first_type_that_names_the_entity_wins(self, config, fake_fnet) -> None:
        """A FIAGRO registered as real-estate is served under type 1, not 11.

        The candidate order puts the agro catalogue first, so this also proves
        the fallback runs rather than the search stopping at the first miss.
        """
        fake_fnet.add_fund(NAME, [{"id": FUND_ID, "text": NAME}], fund_type=REAL_ESTATE)
        fake_fnet.add_documents(FUND_ID, [make_row(1001)], fund_type=REAL_ESTATE)

        scope = Scope(cnpj=CNPJ)
        with _client(config, fake_fnet) as client:
            resolve_scope(client, FakeSnapshot(_entity((AGRO, REAL_ESTATE))), scope)

        assert [(e.fundosnet_id, e.fnet_fund_type) for e in scope.entities] == [
            (FUND_ID, REAL_ESTATE)
        ]

    def test_a_single_candidate_type_is_not_probed_further(self, config, fake_fnet) -> None:
        fake_fnet.add_fund(NAME, [{"id": FUND_ID, "text": NAME}], fund_type=AGRO)
        fake_fnet.add_documents(FUND_ID, [make_row(1001)], fund_type=AGRO)

        scope = Scope(cnpj=CNPJ)
        with _client(config, fake_fnet) as client:
            with pytest.raises(ScopeResolutionError):
                resolve_scope(client, FakeSnapshot(_entity((REAL_ESTATE,))), scope)

        searched = [
            entry for entry in fake_fnet.request_log if entry.startswith("listarFundos")
        ]
        assert searched and all("idTipoFundo=1" in entry for entry in searched)

    def test_an_unknown_cnpj_is_refused_without_touching_the_source(
        self, config, fake_fnet
    ) -> None:
        scope = Scope(cnpj=CNPJ)
        with _client(config, fake_fnet) as client:
            with pytest.raises(ScopeResolutionError):
                resolve_scope(client, FakeSnapshot(None), scope)

        assert fake_fnet.request_log == []


class TestResolutionFailureSafety:
    def test_a_contract_error_during_confirmation_propagates(
        self, config, fake_fnet, monkeypatch
    ) -> None:
        fake_fnet.add_fund(NAME, [{"id": FUND_ID, "text": NAME}])

        def fail_probe(*_args, **_kwargs):
            raise SourceContractError("listing schema changed")

        monkeypatch.setattr(resolver, "probe", fail_probe)
        with _client(config, fake_fnet) as client:
            with pytest.raises(SourceContractError, match="schema changed"):
                resolve_scope(client, FakeSnapshot(_entity((REAL_ESTATE,))), Scope(cnpj=CNPJ))

    def test_a_transient_confirmation_failure_keeps_the_candidate_unconfirmed(
        self, config, fake_fnet, monkeypatch
    ) -> None:
        fake_fnet.add_fund(NAME, [{"id": FUND_ID, "text": NAME}])

        def fail_probe(*_args, **_kwargs):
            raise TransientSourceError("temporary outage")

        monkeypatch.setattr(resolver, "probe", fail_probe)
        scope = Scope(cnpj=CNPJ)
        with _client(config, fake_fnet) as client:
            resolve_scope(client, FakeSnapshot(_entity((REAL_ESTATE,))), scope)

        assert scope.entities[0].fundosnet_id == FUND_ID
        assert scope.entities[0].cnpj_confirmed is False
        assert scope.entities[0].validated_at is None

    def test_more_than_five_exact_matches_are_not_truncated_and_guessed(
        self, config, fake_fnet
    ) -> None:
        fake_fnet.add_fund(
            NAME, [{"id": FUND_ID + index, "text": NAME} for index in range(6)]
        )

        with _client(config, fake_fnet) as client:
            with pytest.raises(ScopeResolutionError):
                resolve_scope(client, FakeSnapshot(_entity((REAL_ESTATE,))), Scope(cnpj=CNPJ))

        probes = [
            entry
            for entry in fake_fnet.request_log
            if entry.startswith("pesquisarGerenciadorDocumentosDados")
        ]
        assert probes == []


class TestEntityConsolidation:
    def test_exact_duplicate_identities_are_collapsed_and_confirmation_is_carried(self) -> None:
        previous = Entity(
            cnpj=CNPJ,
            fundosnet_id=FUND_ID,
            fnet_fund_type=AGRO,
            validated_at="2026-08-26",
            cnpj_confirmed=True,
        )
        resolved = [
            Entity(cnpj=CNPJ, fundosnet_id=FUND_ID, fnet_fund_type=AGRO),
            Entity(cnpj=CNPJ, fundosnet_id=FUND_ID, fnet_fund_type=AGRO),
        ]

        entities, conflicts = resolver._consolidate_entities(resolved, [previous])

        assert conflicts == 0
        assert len(entities) == 1
        assert entities[0].cnpj_confirmed is True
        assert entities[0].validated_at == "2026-08-26"

    def test_one_id_assigned_to_different_cnpjs_is_rejected(self) -> None:
        resolved = [
            Entity(cnpj=CNPJ, fundosnet_id=FUND_ID, fnet_fund_type=REAL_ESTATE),
            Entity(cnpj="08431747000106", fundosnet_id=FUND_ID, fnet_fund_type=REAL_ESTATE),
        ]

        entities, conflicts = resolver._consolidate_entities(resolved, [])

        assert entities == []
        assert conflicts == 1

    def test_confirmation_is_not_carried_to_a_different_fund_type(self) -> None:
        previous = Entity(
            cnpj=CNPJ,
            fundosnet_id=FUND_ID,
            fnet_fund_type=AGRO,
            validated_at="2026-08-26",
            cnpj_confirmed=True,
        )
        resolved = Entity(cnpj=CNPJ, fundosnet_id=FUND_ID, fnet_fund_type=REAL_ESTATE)

        entities, conflicts = resolver._consolidate_entities([resolved], [previous])

        assert conflicts == 0
        assert entities[0].cnpj_confirmed is False
        assert entities[0].validated_at is None
