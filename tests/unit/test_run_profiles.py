"""What the `--monitor` profile changes about a run, and what it must not.

The profile is one decision taken at the composition root: which configured
integer becomes the discovery window. Everything below it works from windows,
so these tests assert on the windows each step was handed rather than on a flag
being passed around.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import replace
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from conftest import write_funds_yaml
from fii_docs_watcher import cli, run
from fii_docs_watcher.config import Config, DiscoveryConfig, RetentionConfig
from fii_docs_watcher.pipeline import audit, discover, fetch

CNPJ = "08431747000106"
FUND_ID = 21348


class _NoRegistry:
    """A registry cache that is present and empty.

    The scopes in this fixture are already resolved, so a run needs nothing
    from CVM -- which is the point: monitoring survives a registry outage.
    """

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        pass

    def load(self, *_args: object, **_kwargs: object) -> None:
        return None


class _NoClient:
    """Stands in for the HTTP client. Every step that would use it is stubbed."""

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        pass

    def __enter__(self) -> _NoClient:
        return self

    def __exit__(self, *_exc: object) -> bool:
        return False


@pytest.fixture
def profiled(config: Config, monkeypatch: pytest.MonkeyPatch):
    """A run whose network steps are recorded instead of performed."""
    config = replace(
        config,
        retention=RetentionConfig(days=7),
        discovery=DiscoveryConfig(days=7, monitor_days=2),
    )
    run.prepare_roots(config)
    write_funds_yaml(config.paths.funds_file, CNPJ, FUND_ID, ticker="HGBS11")

    seen: dict[str, object] = {"audits": 0}

    def fake_discover(_client, _repo, _scopes, window, **_kwargs):
        seen["discovery_window"] = window
        seen["retention"] = _kwargs.get("retention")
        return discover.DiscoveryReport()

    def fake_fetch(*_args, **_kwargs):
        return fetch.FetchReport()

    def fake_audit(*_args, **_kwargs):
        seen["audits"] = int(seen["audits"]) + 1
        return audit.AuditReport(ran=True)

    monkeypatch.setattr(run, "RegistryCache", _NoRegistry)
    monkeypatch.setattr(run, "FnetClient", _NoClient)
    monkeypatch.setattr(run.discover, "run", fake_discover)
    monkeypatch.setattr(run.fetch, "run", fake_fetch)
    monkeypatch.setattr(run.audit, "run", fake_audit)
    return config, seen


class TestTheProfileSelectsOneThing:
    def test_the_sweep_discovers_over_the_full_window(self, profiled) -> None:
        config, seen = profiled

        report = run.execute(config)

        assert seen["discovery_window"].days == 7
        assert report.discovery_window == report.window

    def test_the_monitor_discovers_over_the_narrow_window(self, profiled) -> None:
        config, seen = profiled

        report = run.execute(config, monitor=True)

        assert seen["discovery_window"].days == 2
        assert report.discovery_window.days == 2
        assert report.monitor is True

    def test_retention_is_untouched_by_the_profile(self, profiled) -> None:
        # Purge, the inbox and the frontier answer to the archive's promise,
        # not to how often somebody chose to look.
        config, seen = profiled

        report = run.execute(config, monitor=True)

        assert report.window.days == 7
        assert report.purge is not None and report.inbox is not None

    def test_discovery_is_told_the_retention_window_as_well(self, profiled) -> None:
        # This is what lets the watermark rule be derived from the two windows
        # instead of from the profile flag.
        config, seen = profiled

        run.execute(config, monitor=True)

        assert seen["retention"].days == 7
        assert seen["discovery_window"].last == seen["retention"].last

    def test_both_windows_end_on_the_same_today(self, profiled) -> None:
        config, _seen = profiled

        report = run.execute(config, monitor=True)

        assert report.discovery_window.last == report.window.last


class TestTheMonitorDoesNotAudit:
    """The audit scans the whole day's global listing, per fund type.

    It is the costliest request a run makes and it is detective-only, so it
    belongs to the daily sweep. Paying it seventeen times a day would spend the
    request budget on a check that has already been made.
    """

    def test_the_sweep_audits(self, profiled) -> None:
        config, seen = profiled

        run.execute(config)

        assert seen["audits"] == 1

    def test_the_monitor_does_not(self, profiled) -> None:
        config, seen = profiled

        run.execute(config, monitor=True)

        assert seen["audits"] == 0

    def test_skip_audit_still_works_for_the_sweep(self, profiled) -> None:
        config, seen = profiled

        run.execute(config, skip_audit=True)

        assert seen["audits"] == 0


class TestTheFlagReachesTheRun:
    def _namespace(self, **overrides: object) -> argparse.Namespace:
        defaults = {"monitor": False, "dry_run": False, "skip_audit": False}
        return argparse.Namespace(**{**defaults, **overrides})

    @pytest.fixture
    def recorded(self, monkeypatch: pytest.MonkeyPatch) -> list[dict]:
        calls: list[dict] = []

        def fake_execute(_config, **kwargs):
            calls.append(kwargs)
            return run.RunReport()

        monkeypatch.setattr(cli, "execute", fake_execute)
        monkeypatch.setattr(cli, "_print_summary", lambda _report: None)
        return calls

    def test_plain_run_is_the_sweep(self, config: Config, recorded: list[dict]) -> None:
        cli.cmd_run(config, self._namespace())
        assert recorded[0]["monitor"] is False

    def test_the_flag_selects_the_monitor(self, config: Config, recorded: list[dict]) -> None:
        cli.cmd_run(config, self._namespace(monitor=True))
        assert recorded[0]["monitor"] is True

    def test_the_parser_accepts_it_before_the_other_options(self) -> None:
        args = cli.build_parser().parse_args(["run", "--monitor", "--dry-run"])
        assert (args.monitor, args.dry_run) == (True, True)

    def test_the_parser_defaults_to_the_sweep(self) -> None:
        assert cli.build_parser().parse_args(["run"]).monitor is False


class TestARefreshedSpellingIsPersisted:
    """A description refresh must reach funds.yaml through the existing save.

    The gate matches against the stored spelling, so a refresh that stayed
    in memory would be recomputed -- and re-logged -- on every single run.
    """

    def _saves(self, monkeypatch: pytest.MonkeyPatch) -> list[int]:
        saves: list[int] = []
        monkeypatch.setattr(run, "_save", lambda *_a, **_k: saves.append(1))
        return saves

    def test_a_refresh_triggers_the_save(self, profiled, monkeypatch) -> None:
        config, _seen = profiled
        saves = self._saves(monkeypatch)
        monkeypatch.setattr(
            run.discover,
            "run",
            lambda *_a, **_k: discover.DiscoveryReport(descriptions_refreshed=1),
        )

        run.execute(config)

        assert saves

    def test_no_refresh_and_no_confirmation_saves_nothing(self, profiled, monkeypatch) -> None:
        config, _seen = profiled
        saves = self._saves(monkeypatch)

        run.execute(config)

        assert not saves
