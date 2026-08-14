"""The funds file and the run lock: the two places concurrency bites."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from fii_docs_watcher.errors import LockHeldError, YamlConflictError
from fii_docs_watcher.lock import ProcessLock
from fii_docs_watcher.scope.models import Entity, ExpansionState, Scope
from fii_docs_watcher.scope.yaml_store import FundsFile

AUTHORED = """\
# My funds -- keep this comment.
scopes:
  # the shopping fund
  - cnpj: "08.431.747/0001-06"
    ticker: "HGBS11"   # my own note
  - cnpj: "34895752000180"
"""


def _resolved(scope: Scope) -> Scope:
    scope.legal_name = "HEDGE BRASIL SHOPPING FUNDO DE INVESTIMENTO IMOBILIÁRIO"
    scope.cvm_code = "306006"
    scope.cvm_status = "Em Funcionamento Normal"
    scope.expansion = ExpansionState.COMPLETE
    scope.entities = [
        Entity(cnpj="08431747000106", fundosnet_id=21348, fnet_fund_description="HEDGE ...")
    ]
    return scope


class TestFundsFile:
    def test_a_missing_file_is_created_with_guidance(self, tmp_path: Path) -> None:
        path = tmp_path / "funds.yaml"
        funds = FundsFile.load(path)
        assert path.exists()
        assert funds.scopes() == []
        assert "Always quote the CNPJ" in path.read_text(encoding="utf-8")

    def test_the_cnpj_survives_as_a_string_with_its_leading_zero(self, tmp_path: Path) -> None:
        path = tmp_path / "funds.yaml"
        path.write_text(AUTHORED, encoding="utf-8")
        scope = FundsFile.load(path).scopes()[0]
        assert scope.cnpj == "08.431.747/0001-06"
        assert scope.normalized_cnpj == "08431747000106"

    def test_an_unquoted_cnpj_is_recovered_rather_than_silently_wrong(
        self, tmp_path: Path
    ) -> None:
        # YAML reads 08431747000106 as an integer and eats the leading zero.
        path = tmp_path / "funds.yaml"
        path.write_text("scopes:\n  - cnpj: 08431747000106\n", encoding="utf-8")
        assert FundsFile.load(path).scopes()[0].normalized_cnpj == "08431747000106"

    def test_comments_and_the_users_fields_survive_a_rewrite(self, tmp_path: Path) -> None:
        path = tmp_path / "funds.yaml"
        path.write_text(AUTHORED, encoding="utf-8")
        funds = FundsFile.load(path)
        funds.update_scope(_resolved(funds.scopes()[0]))
        funds.save()

        text = path.read_text(encoding="utf-8")
        assert "keep this comment" in text
        assert "the shopping fund" in text
        assert "my own note" in text
        # The user's fields are theirs; the robot never rewrites them.
        assert '"08.431.747/0001-06"' in text
        assert '"HGBS11"' in text
        # And the resolved fields landed.
        assert "fundosnet_id: 21348" in text

    def test_a_concurrent_human_edit_is_never_overwritten(self, tmp_path: Path) -> None:
        path = tmp_path / "funds.yaml"
        path.write_text(AUTHORED, encoding="utf-8")
        funds = FundsFile.load(path)

        # The user saves while the robot is working.
        edited = AUTHORED + '  - cnpj: "11222333000181"\n'
        path.write_text(edited, encoding="utf-8")

        funds.update_scope(_resolved(funds.scopes()[0]))
        with pytest.raises(YamlConflictError, match="changed on disk"):
            funds.save()

        # Their edit is intact and the robot's update was dropped, not merged.
        assert path.read_text(encoding="utf-8") == edited

    def test_a_backup_of_the_previous_version_is_kept(self, tmp_path: Path) -> None:
        path = tmp_path / "funds.yaml"
        backup = tmp_path / "funds.yaml.bak"
        path.write_text(AUTHORED, encoding="utf-8")
        funds = FundsFile.load(path)
        funds.update_scope(_resolved(funds.scopes()[0]))
        funds.save(backup=backup)
        assert backup.read_text(encoding="utf-8") == AUTHORED

    def test_an_entry_without_a_cnpj_is_skipped_not_fatal(self, tmp_path: Path) -> None:
        # Hand-edited entries must carry a CNPJ; one bad line cannot stop the rest.
        path = tmp_path / "funds.yaml"
        path.write_text(
            'scopes:\n  - ticker: "XPTO11"\n  - cnpj: "08431747000106"\n', encoding="utf-8"
        )
        scopes = FundsFile.load(path).scopes()
        assert len(scopes) == 1
        assert scopes[0].normalized_cnpj == "08431747000106"

    def test_a_ticker_given_on_the_command_line_is_kept(self, tmp_path: Path) -> None:
        # The robot never invents or validates a ticker, but discarding one the
        # user supplied would silently change every filename it should prefix.
        path = tmp_path / "funds.yaml"
        funds = FundsFile.load(path)
        scope = Scope(cnpj="08.431.747/0001-06", ticker="HGBS11")
        funds.add_scope(scope)
        funds.update_scope(_resolved(scope))
        funds.save()

        reloaded = FundsFile.load(path).scopes()[0]
        assert reloaded.ticker == "HGBS11"

    def test_the_users_fields_stay_above_the_generated_ones(self, tmp_path: Path) -> None:
        path = tmp_path / "funds.yaml"
        funds = FundsFile.load(path)
        scope = Scope(cnpj="08.431.747/0001-06", ticker="HGBS11")
        funds.add_scope(scope)
        funds.update_scope(_resolved(scope))
        funds.save()

        text = path.read_text(encoding="utf-8")
        assert text.index("ticker:") < text.index("legal_name:") < text.index("entities:")

    def test_a_sync_never_overwrites_the_users_ticker(self, tmp_path: Path) -> None:
        path = tmp_path / "funds.yaml"
        path.write_text(AUTHORED, encoding="utf-8")
        funds = FundsFile.load(path)
        funds.update_scope(_resolved(funds.scopes()[0]))
        funds.save()
        assert FundsFile.load(path).scopes()[0].ticker == "HGBS11"

    def test_a_ticker_can_be_attached_to_an_already_registered_scope(
        self, tmp_path: Path
    ) -> None:
        # Regression: re-running `add` with --ticker on a known CNPJ reported
        # "already registered" and silently dropped the ticker.
        path = tmp_path / "funds.yaml"
        funds = FundsFile.load(path)
        funds.add_scope(Scope(cnpj="12.005.956/0001-65"))
        funds.save()

        again = FundsFile.load(path)
        assert not again.add_scope(Scope(cnpj="12005956000165", ticker="KNRI11"))
        assert again.update_user_fields(Scope(cnpj="12005956000165", ticker="KNRI11"))
        again.save()

        assert FundsFile.load(path).scopes()[0].ticker == "KNRI11"

    def test_updating_user_fields_reports_when_nothing_changed(self, tmp_path: Path) -> None:
        path = tmp_path / "funds.yaml"
        funds = FundsFile.load(path)
        funds.add_scope(Scope(cnpj="12.005.956/0001-65", ticker="KNRI11"))
        # Same values again: the caller needs to know there is nothing to save.
        assert not funds.update_user_fields(Scope(cnpj="12005956000165", ticker="KNRI11"))

    def test_adding_the_same_cnpj_twice_is_refused(self, tmp_path: Path) -> None:
        path = tmp_path / "funds.yaml"
        funds = FundsFile.load(path)
        assert funds.add_scope(Scope(cnpj="08.431.747/0001-06"))
        # Same fund, different spelling.
        assert not funds.add_scope(Scope(cnpj="08431747000106"))

    def test_a_partial_write_cannot_leave_a_truncated_file(self, tmp_path: Path) -> None:
        path = tmp_path / "funds.yaml"
        path.write_text(AUTHORED, encoding="utf-8")
        funds = FundsFile.load(path)
        funds.update_scope(_resolved(funds.scopes()[0]))
        funds.save()
        # No stray temporary left behind by the atomic rename.
        assert [p.name for p in tmp_path.iterdir() if p.name.startswith(".")] == []


class TestScopeSearch:
    """`Scope.matches` — how a person finds a fund they registered earlier."""

    def _scope(self) -> Scope:
        scope = Scope(cnpj="12.005.956/0001-65", ticker="KNRI11")
        scope.legal_name = "KINEA RENDA IMOBILIÁRIA FUNDO DE INVESTIMENTO IMOBILIÁRIO"
        scope.entities = [
            Entity(
                cnpj="12005956000165",
                fundosnet_id=21189,
                fnet_fund_description="KINEA RENDA IMOBILIÁRIA FII",
            )
        ]
        return scope

    def test_matches_by_ticker(self) -> None:
        assert self._scope().matches("KNRI11")
        assert self._scope().matches("knri")

    def test_matches_by_a_word_of_the_name(self) -> None:
        assert self._scope().matches("kinea")
        assert self._scope().matches("RENDA IMOB")

    def test_accents_do_not_have_to_be_typed(self) -> None:
        assert self._scope().matches("imobiliaria")
        assert self._scope().matches("IMOBILIÁRIA")

    def test_matches_by_cnpj_in_any_punctuation(self) -> None:
        assert self._scope().matches("12005956")
        assert self._scope().matches("12.005.956/0001-65")
        assert self._scope().matches("0001-65")

    def test_matches_the_fundos_net_description_too(self) -> None:
        # An unresolved scope has no legal name, but a resolved one may only be
        # recognisable by what Fundos.NET calls it.
        assert self._scope().matches("FII")

    def test_an_empty_query_matches_everything(self) -> None:
        # `list` with no argument shows all funds.
        assert self._scope().matches("")
        assert self._scope().matches("   ")

    def test_an_unrelated_query_does_not_match(self) -> None:
        assert not self._scope().matches("hedge")
        assert not self._scope().matches("99999999")

    def test_a_scope_with_nothing_resolved_still_matches_its_cnpj(self) -> None:
        bare = Scope(cnpj="12.005.956/0001-65")
        assert bare.matches("12005956")
        assert not bare.matches("kinea")


class TestTickerEditing:
    def test_a_ticker_can_be_cleared_and_the_key_disappears(self, tmp_path: Path) -> None:
        path = tmp_path / "funds.yaml"
        funds = FundsFile.load(path)
        funds.add_scope(Scope(cnpj="12.005.956/0001-65", ticker="KNRI11"))
        funds.save()

        again = FundsFile.load(path)
        scope = again.scopes()[0]
        scope.ticker = None
        assert again.update_user_fields(scope)
        again.save()

        # Removed rather than written as an empty string, so the file still
        # reads like something a person wrote. (Checked on the mapping key, not
        # on the word: the template's comment header mentions "ticker" too.)
        lines = path.read_text(encoding="utf-8").splitlines()
        assert not [line for line in lines if line.strip().startswith("ticker:")]
        assert FundsFile.load(path).scopes()[0].ticker is None

    def test_changing_a_ticker_replaces_it(self, tmp_path: Path) -> None:
        path = tmp_path / "funds.yaml"
        funds = FundsFile.load(path)
        funds.add_scope(Scope(cnpj="12.005.956/0001-65", ticker="OLD11"))
        scope = funds.scopes()[0]
        scope.ticker = "KNRI11"
        assert funds.update_user_fields(scope)
        funds.save()
        assert FundsFile.load(path).scopes()[0].ticker == "KNRI11"


class TestProcessLock:
    def test_a_second_instance_is_refused_while_the_first_holds_it(self, tmp_path: Path) -> None:
        path = tmp_path / "watcher.lock"
        with ProcessLock(path):
            with pytest.raises(LockHeldError, match="another instance is running"):
                ProcessLock(path).acquire()

    def test_the_lock_is_released_on_the_way_out(self, tmp_path: Path) -> None:
        path = tmp_path / "watcher.lock"
        with ProcessLock(path):
            assert path.exists()
        assert not path.exists()

    def test_it_is_released_even_when_the_run_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "watcher.lock"
        with pytest.raises(RuntimeError), ProcessLock(path):
            raise RuntimeError("boom")
        assert not path.exists()

    def test_a_lock_left_by_a_dead_process_is_reclaimed(self, tmp_path: Path) -> None:
        # Otherwise a crash would block the robot until a human intervened.
        path = tmp_path / "watcher.lock"
        path.write_text(json.dumps({"pid": 999_999, "acquired_at": "x"}), encoding="utf-8")

        with ProcessLock(path):
            owner = json.loads(path.read_text(encoding="utf-8"))
            assert owner["pid"] == os.getpid()

    def test_a_corrupt_lock_file_is_reclaimed_rather_than_blocking_forever(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "watcher.lock"
        path.write_text("not json at all", encoding="utf-8")
        with ProcessLock(path):
            assert json.loads(path.read_text(encoding="utf-8"))["pid"] == os.getpid()
