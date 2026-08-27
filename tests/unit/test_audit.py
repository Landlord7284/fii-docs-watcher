"""Global audit reporting across all monitored Fundos.NET fund types."""

from __future__ import annotations

from datetime import date

from fii_docs_watcher.config import AuditConfig
from fii_docs_watcher.errors import TransientSourceError
from fii_docs_watcher.pipeline import audit
from fii_docs_watcher.scope.models import Entity, Scope


class _Repo:
    def known_identities_for_entity(self, _fundosnet_id: int) -> set[tuple[int, int]]:
        return set()


def test_failures_from_multiple_fund_types_are_all_preserved(monkeypatch) -> None:
    scope = Scope(
        cnpj="08431747000106",
        legal_name="TEST FUND",
        entities=[
            Entity(cnpj="08431747000106", fundosnet_id=1, fnet_fund_type=1),
            Entity(cnpj="99999999000199", fundosnet_id=2, fnet_fund_type=11),
        ],
    )

    def fail_scan(_client, **kwargs):
        fund_type = kwargs["fund_type"]
        raise TransientSourceError(f"failure for type {fund_type}")

    monkeypatch.setattr(audit, "scan", fail_scan)
    report = audit.run(
        object(),  # type: ignore[arg-type]
        _Repo(),  # type: ignore[arg-type]
        [scope],
        AuditConfig(frequency="daily"),
        reference=date(2026, 8, 27),
    )

    assert report.error is not None
    assert "tipoFundo=1" in report.error
    assert "tipoFundo=11" in report.error
