"""What `add` does, and -- more to the point -- what it no longer does.

Registering a fund used to resolve it on the spot: one Fundos.NET query per
class, against a source whose successful responses take a minute as often as
not. That made a bookkeeping command feel like a run, and the escape hatch was
a flag nobody wants to remember. `add` now writes the watch list and stops,
leaving resolution to the next run, so these tests pin both halves: the CVM
registry (a local file) is read, and Fundos.NET is not touched at all.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fii_docs_watcher import cli
from fii_docs_watcher.config import Config
from fii_docs_watcher.cvm.registry import RegisteredClass, RegisteredFund, RegistrySnapshot
from fii_docs_watcher.run import ExitCode
from fii_docs_watcher.scope.models import ScopeMode
from fii_docs_watcher.scope.yaml_store import FundsFile

FUND_CNPJ = "08431747000106"
CLASS_A_CNPJ = "08431747000287"
CLASS_B_CNPJ = "08431747000368"


def _snapshot() -> RegistrySnapshot:
    """One multiclass fund, so expansion has something to expand."""
    fund = RegisteredFund(
        registry_id="1",
        cnpj=FUND_CNPJ,
        cvm_code="306006",
        legal_name="HEDGE BRASIL SHOPPING FUNDO DE INVESTIMENTO IMOBILIÁRIO",
        situation="Em Funcionamento Normal",
        family="FII",
    )
    classes = [
        RegisteredClass(
            registry_id=str(index),
            fund_registry_id="1",
            cnpj=cnpj,
            cvm_code="306006",
            legal_name=name,
            situation="Em Funcionamento Normal",
            family="FII",
        )
        for index, (cnpj, name) in enumerate(
            [(CLASS_A_CNPJ, "CLASSE A DO HEDGE BRASIL SHOPPING"), (CLASS_B_CNPJ, "CLASSE B")],
            start=2,
        )
    ]
    return RegistrySnapshot([fund], classes, fetched_at="2026-08-28T00:00:00Z")


class _CachedRegistry:
    """The registry cache, standing in for the archive already on disk."""

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        pass

    def load(self, *_args: object, **_kwargs: object) -> RegistrySnapshot:
        return _snapshot()


class _ForbiddenClient:
    """Fundos.NET, which `add` must never reach for."""

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        raise AssertionError("add must not contact Fundos.NET")


@pytest.fixture
def registered(config: Config, monkeypatch: pytest.MonkeyPatch):
    config.paths.data_root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(cli, "RegistryCache", _CachedRegistry)
    monkeypatch.setattr(cli, "FnetClient", _ForbiddenClient)

    def add(cnpj: str, *, ticker: str | None = None, this_entity_only: bool = False) -> int:
        args = argparse.Namespace(
            cnpj=cnpj, name=None, ticker=ticker, this_entity_only=this_entity_only
        )
        return cli.cmd_add(config, args)

    return config, add


def test_a_registered_fund_is_named_from_the_registry_but_left_unresolved(registered) -> None:
    config, add = registered

    assert add(FUND_CNPJ, ticker="HGBS11") == ExitCode.OK

    scope = FundsFile.load(config.paths.funds_file).scopes()[0]
    assert scope.ticker == "HGBS11"
    assert scope.legal_name.startswith("HEDGE BRASIL SHOPPING")
    assert scope.cvm_code == "306006"
    # The Fundos.NET ids are the next run's work, and nothing here pretends
    # otherwise: an empty entity list is what makes the run resolve it.
    assert scope.entities == []
    assert scope.resolved is False


def test_the_classes_a_fund_cnpj_brings_with_it_are_reported(registered, capsys) -> None:
    _, add = registered

    add(FUND_CNPJ, ticker="HGBS11")

    printed = capsys.readouterr().out
    assert "3 entities to monitor:" in printed
    assert "08.431.747/0002-87" in printed


def test_this_entity_only_reports_the_one_entity_it_registered(registered, capsys) -> None:
    _, add = registered

    add(CLASS_A_CNPJ, ticker="HGBS11", this_entity_only=True)

    printed = capsys.readouterr().out
    assert "entities to monitor" not in printed
    assert "CLASSE A DO HEDGE BRASIL SHOPPING" in printed


def test_a_cnpj_the_registry_has_never_heard_of_is_registered_with_a_warning(
    registered, capsys
) -> None:
    """The snapshot is a daily file, so an absent CNPJ may be a typo or may be a
    fund registered this morning. The user decides which; the entry is written."""
    config, add = registered

    assert add("11222333000181") == ExitCode.OK

    assert "no fund or class this robot can monitor" in capsys.readouterr().err
    scope = FundsFile.load(config.paths.funds_file).scopes()[0]
    assert scope.legal_name is None
    assert scope.mode is ScopeMode.FUND_AND_CLASSES
