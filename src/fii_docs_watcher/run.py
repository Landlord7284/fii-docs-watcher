"""One run, start to finish.

The canonical mode of operation is one-shot: do the work, exit with a meaningful
code. Anything periodic -- cron, a timer, a scheduler -- is packaging built on
top of this, never the other way round. Nothing here assumes a working
directory, a container, an orchestrator or a credential.

Order matters:

    reconcile  first, so an interrupted run is healed before anything new starts
    registry   once per run, as a snapshot rather than a per-scope lookup
    resolve    only scopes that need it
    discover   the whole retention window, per entity
    fetch      whatever is pending, including retries from earlier runs
    inbox      after fetching, so it reflects what actually landed
    purge      last, so nothing is deleted before it has been indexed
    audit      last of all, and never able to fail the job
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from .clock import RetentionWindow, retention_window
from .config import Config
from .cvm.registry import RegistryCache, RegistrySnapshot
from .errors import ScopeResolutionError, WatcherError, YamlConflictError
from .fnet.client import FnetClient
from .lock import ProcessLock, ShutdownSignal
from .manifest.db import connect
from .manifest.repo import ManifestRepo
from .pipeline import audit, discover, fetch, inbox, purge, reconcile
from .scope.models import ExpansionState, Scope
from .scope.resolver import resolve_scope
from .scope.yaml_store import FundsFile

log = logging.getLogger(__name__)


class ExitCode:
    OK = 0
    PARTIAL = 1  # Ran, but something was skipped or failed in isolation.
    CONFIG = 2
    LOCKED = 3


@dataclass
class RunReport:
    """What one run did. Rendered by the CLI and used to choose the exit code."""

    window: RetentionWindow | None = None
    reconcile: reconcile.ReconcileReport | None = None
    discovery: discover.DiscoveryReport | None = None
    downloads: fetch.FetchReport | None = None
    inbox: inbox.InboxReport | None = None
    purge: purge.PurgeReport | None = None
    audit: audit.AuditReport | None = None
    scopes_total: int = 0
    scopes_resolved: int = 0
    scopes_failed: int = 0
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    interrupted: bool = False

    @property
    def exit_code(self) -> int:
        if self.errors or self.scopes_failed or self.interrupted:
            return ExitCode.PARTIAL
        if self.discovery and (self.discovery.entities_failed or self.discovery.incomplete_scans):
            return ExitCode.PARTIAL
        if self.downloads and self.downloads.failed:
            return ExitCode.PARTIAL
        return ExitCode.OK


def prepare_roots(config: Config) -> None:
    """Create both roots and verify the one assumption that would silently break atomicity.

    The staging directory has to sit on the same filesystem as the date
    directories, because `rename` is only atomic within one filesystem. If it
    ever were not, a crash could leave a half-copied file where a complete one
    is expected -- so this is checked rather than assumed.
    """
    config.paths.data_root.mkdir(parents=True, exist_ok=True)
    config.paths.documents_root.mkdir(parents=True, exist_ok=True)
    config.paths.tmp_dir.mkdir(parents=True, exist_ok=True)
    config.paths.inbox_dir.mkdir(parents=True, exist_ok=True)

    for directory in (config.paths.documents_root, config.paths.tmp_dir, config.paths.inbox_dir):
        try:
            directory.chmod(config.files.directory_mode)
        except OSError:  # pragma: no cover - the share may forbid chmod
            log.debug("could not set directory mode", extra={"dir": str(directory)})

    documents_device = config.paths.documents_root.stat().st_dev
    tmp_device = config.paths.tmp_dir.stat().st_dev
    if documents_device != tmp_device:
        raise WatcherError(
            f"{config.paths.tmp_dir} is on a different filesystem from "
            f"{config.paths.documents_root}; rename would not be atomic and a crash could "
            "leave a partial file in the archive"
        )


def sync_scopes(
    client: FnetClient,
    funds_file: FundsFile,
    snapshot: RegistrySnapshot | None,
    config: Config,
    report: RunReport,
) -> list[Scope]:
    """Resolve any scope that needs it and write the results back to the YAML.

    A scope that cannot be resolved is reported and skipped; the others carry on.
    An already-resolved scope survives a CVM outage untouched, which is the whole
    point of separating the registry refresh from monitoring.
    """
    scopes = funds_file.scopes()
    report.scopes_total = len(scopes)

    needs_resolution = [scope for scope in scopes if not scope.resolved]
    if needs_resolution and snapshot is None:
        message = (
            f"{len(needs_resolution)} scope(s) need resolving but no CVM registry snapshot is "
            "available; they are skipped this run and already-resolved scopes continue"
        )
        report.warnings.append(message)
        log.error(
            "cannot resolve new scopes without a CVM snapshot",
            extra={"pending": len(needs_resolution)},
        )

    usable: list[Scope] = []
    changed = False

    for scope in scopes:
        if scope.resolved:
            usable.append(scope)
            report.scopes_resolved += 1
            continue
        if snapshot is None:
            report.scopes_failed += 1
            continue
        try:
            resolve_scope(client, snapshot, scope)
            funds_file.update_scope(scope)
            changed = True
            usable.append(scope)
            report.scopes_resolved += 1
        except (ScopeResolutionError, WatcherError) as exc:
            report.scopes_failed += 1
            report.errors.append(f"{scope.label}: {exc}")
            scope.expansion = ExpansionState.PARTIAL
            log.error(
                "scope could not be resolved and is skipped this run",
                extra={"scope": scope.label, "error": str(exc)},
            )

    if changed:
        _save(funds_file, config, report)
    return usable


def _save(funds_file: FundsFile, config: Config, report: RunReport) -> None:
    """Persist the funds file, treating a concurrent human edit as recoverable."""
    try:
        funds_file.save(backup=config.paths.funds_backup)
    except YamlConflictError as exc:
        report.errors.append(str(exc))
        log.error(
            "funds file was edited while the robot was running; the robot's update was "
            "discarded and will be reapplied next run",
            extra={"path": str(funds_file.path)},
        )


def execute(config: Config, *, skip_audit: bool = False, dry_run: bool = False) -> RunReport:
    """Run the pipeline once. Assumes logging is already configured."""
    report = RunReport()
    window = retention_window(config.retention.days)
    report.window = window
    log.info(
        "run starting",
        extra={
            "window": str(window),
            "retention_days": window.days,
            "dry_run": dry_run,
            "config": str(config.source_path) if config.source_path else "built-in defaults",
            "formats": ",".join(config.download.formats),
        },
    )

    prepare_roots(config)

    with ProcessLock(config.paths.lock_file), ShutdownSignal() as shutdown:
        connection = connect(config.paths.manifest_file)
        try:
            repo = ManifestRepo(connection)

            report.reconcile = reconcile.run(repo, config)

            funds_file = FundsFile.load(config.paths.funds_file)
            registry = RegistryCache(config.cvm, config.paths.cvm_cache)
            snapshot = registry.load(config.source.user_agent)

            with FnetClient(config.source) as client:
                scopes = sync_scopes(client, funds_file, snapshot, config, report)

                if not scopes:
                    log.warning(
                        "no usable scopes; add one with `fii-docs-watcher add --cnpj ...`",
                        extra={"funds_file": str(config.paths.funds_file)},
                    )
                    return report

                if dry_run:
                    log.info(
                        "dry run: stopping before discovery",
                        extra={
                            "scopes": len(scopes),
                            "entities": sum(len(s.entities) for s in scopes),
                        },
                    )
                    return report

                report.discovery = discover.run(
                    client,
                    repo,
                    scopes,
                    window,
                    page_length=config.source.page_length,
                    should_stop=lambda: shutdown.requested,
                )
                report.warnings.extend(discover.check_watermarks(repo, window))

                report.downloads = fetch.run(
                    client, repo, config, scopes, should_stop=lambda: shutdown.requested
                )

                # Confirmations earned during fetching belong in the YAML, so a
                # CNPJ already validated is not revalidated on every future run.
                if any(e.cnpj_confirmed for s in scopes for e in s.entities):
                    for scope in scopes:
                        funds_file.update_scope(scope)
                    _save(funds_file, config, report)

                report.inbox = inbox.run(repo, config, window)
                report.purge = purge.run(repo, config, window)

                if not skip_audit and not shutdown.requested:
                    report.audit = audit.run(client, repo, scopes, config.audit)
                    report.warnings.extend((report.audit or audit.AuditReport()).unmatched)

            report.interrupted = shutdown.requested
        finally:
            connection.close()

    if report.reconcile:
        report.warnings.extend(report.reconcile.hash_mismatches)
    if report.downloads:
        report.warnings.extend(report.downloads.cnpj_divergences)

    log.info("run finished", extra={"exit_code": report.exit_code})
    return report


def load_and_execute(config_path: Path | str | None, **kwargs: object) -> RunReport:
    """Convenience wrapper used by tests and by the scheduler-free entry point."""
    from .config import load
    from .logging_setup import configure

    config = load(config_path)
    configure(config.logging)
    return execute(config, **kwargs)  # type: ignore[arg-type]
