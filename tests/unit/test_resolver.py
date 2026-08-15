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
from fii_docs_watcher.errors import ScopeResolutionError
from fii_docs_watcher.fnet.client import FnetClient
from fii_docs_watcher.scope.models import Scope
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
