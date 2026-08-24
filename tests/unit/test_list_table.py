"""How `list` decides what to repeat and what to say once.

The table's whole point is that a watch list of near-identical funds should not
print the same five facts on every line. Both halves of that are pure -- rows
in, rows out -- so the rules are pinned here: what gets hoisted above the table,
what stays a column because it varies, and what is dropped for having no value
anywhere.
"""

from __future__ import annotations

from fii_docs_watcher.cli import _LIST_COLUMNS, _hoist_common, _list_rows, _render_table
from fii_docs_watcher.scope.models import Entity, ExpansionState, Scope, ScopeMode


def scope(
    ticker: str,
    cnpj: str,
    *,
    status: str = "Em Funcionamento Normal",
    entities: list[Entity] | None = None,
) -> Scope:
    return Scope(
        cnpj=cnpj,
        ticker=ticker,
        legal_name=f"{ticker} FUNDO DE INVESTIMENTO IMOBILIÁRIO",
        cvm_code="310020",
        cvm_status=status,
        expansion=ExpansionState.COMPLETE,
        entities=[Entity(cnpj=cnpj, fundosnet_id=21189)] if entities is None else entities,
    )


def test_a_fact_every_fund_agrees_on_is_hoisted_out_of_the_table() -> None:
    rows = _list_rows([scope("KNRI11", "12005956000165"), scope("HGBS11", "08431747000106")])
    common, columns = _hoist_common(rows, _LIST_COLUMNS)

    assert common["mode"] == ScopeMode.FUND_AND_CLASSES.value
    assert common["cvm status"] == "Em Funcionamento Normal"
    assert "mode" not in {column.key for column in columns}


def test_a_fact_that_differs_stays_a_column() -> None:
    rows = _list_rows(
        [
            scope("KNRI11", "12005956000165"),
            scope("HGBS11", "08431747000106", status="Em Liquidação"),
        ]
    )
    common, columns = _hoist_common(rows, _LIST_COLUMNS)

    assert "cvm status" not in common
    assert "cvm_status" in {column.key for column in columns}


def test_a_column_nobody_filled_in_is_dropped_entirely() -> None:
    """An unresolved scope has no entity, so there is no id to show and no
    hoisted claim to make about one."""
    rows = _list_rows([Scope(cnpj="11222333000144")])
    common, columns = _hoist_common(rows, _LIST_COLUMNS)

    keys = {column.key for column in columns}
    assert "fnet_id" not in keys
    assert "fnet fund type" not in common


def test_a_fund_describes_itself_once_however_many_classes_it_has() -> None:
    entities = [
        Entity(cnpj="99999999000199", fundosnet_id=25256, fnet_fund_description="URBANITY"),
        Entity(cnpj="99999999000299", fundosnet_id=25257, fnet_fund_description="CLASSE A"),
    ]
    rows = _list_rows([scope("MULT11", "99999999000199", entities=entities)])

    assert len(rows) == 2
    assert rows[0]["ticker"] == "MULT11"
    assert rows[1].get("ticker", "") == ""
    assert rows[1].get("cvm", "") == ""
    assert rows[1]["cnpj"] == "99.999.999/0002-99"


def test_the_name_column_gives_way_first_when_the_terminal_is_narrow() -> None:
    rows = _list_rows([scope("KNRI11", "12005956000165")])
    _, columns = _hoist_common(rows, _LIST_COLUMNS)
    lines = _render_table(rows, columns, width=80)

    assert all(len(line) <= 80 for line in lines)
    assert lines[-1].endswith("\u2026")


def test_the_name_column_stops_shrinking_before_it_becomes_unreadable() -> None:
    """Past the floor the line is allowed to overflow. A column of bare
    ellipses would be tidier and would tell the reader nothing."""
    rows = _list_rows([scope("KNRI11", "12005956000165")])
    _, columns = _hoist_common(rows, _LIST_COLUMNS)
    lines = _render_table(rows, columns, width=40)

    name = lines[-1].rsplit("  ", 1)[-1]
    assert len(name) == 24 and name.endswith("\u2026")
    assert len(lines[-1]) > 40
