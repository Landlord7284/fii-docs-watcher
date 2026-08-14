"""Command line interface.

`run` is the whole job and the only command a scheduler needs. The others exist
because registering a fund, inspecting what was resolved and checking the
environment are things a person does interactively, and forcing them through a
full run would be both slow and confusing.

The registration split is deliberate: this command accepts a partial name
because a human is present to disambiguate, while hand-editing funds.yaml
accepts a CNPJ only. FII names collide constantly, and the robot runs
unattended -- a fuzzy match with nobody to confirm it is a silent misfiling
waiting to happen.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from . import __version__
from .clock import retention_window, to_dir_name, today
from .config import Config, load
from .cvm.registry import RegistryCache
from .errors import ConfigError, LockHeldError, WatcherError
from .fnet.client import FnetClient
from .logging_setup import configure
from .manifest.db import connect
from .manifest.repo import ManifestRepo
from .run import ExitCode, RunReport, execute
from .scope.cnpj import format_masked, is_valid, normalize
from .scope.models import Scope, ScopeMode
from .scope.resolver import resolve_scope
from .scope.yaml_store import FundsFile

log = logging.getLogger(__name__)


def build_parser() -> argparse.ArgumentParser:
    # `--config` is accepted both before and after the subcommand, because
    # writing it after is the more natural order and having that fail is a poor
    # first impression. SUPPRESS on the shared copy is what makes it work: the
    # subparser then leaves the namespace alone when the flag was given earlier,
    # instead of overwriting it with its own default.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "-c",
        "--config",
        type=Path,
        default=argparse.SUPPRESS,
        help="path to the TOML configuration file",
    )
    common.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        default=argparse.SUPPRESS,
        help="log at DEBUG level regardless of the config",
    )

    parser = argparse.ArgumentParser(
        prog="fii-docs-watcher",
        description="Download FII documents from Fundos.NET into a sliding N-day archive.",
        parents=[common],
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    # No `set_defaults` here: `parents=` shares Action objects rather than
    # copying them, so setting a default on the parent would overwrite the
    # SUPPRESS sentinel on every subparser too, and the flag would stop working
    # before the subcommand. The fallback is applied after parsing instead.
    sub = parser.add_subparsers(dest="command", required=True)

    def add_command(name: str, help_text: str) -> argparse.ArgumentParser:
        return sub.add_parser(name, help=help_text, parents=[common])

    run_cmd = add_command("run", "run the pipeline once (the canonical mode)")
    run_cmd.add_argument(
        "--dry-run", action="store_true", help="resolve scopes and stop before discovery"
    )
    run_cmd.add_argument("--skip-audit", action="store_true", help="skip the global audit scan")

    add_cmd = add_command("add", "register a fund to monitor")
    group = add_cmd.add_argument_group("identify the fund")
    group.add_argument("--cnpj", help="CNPJ, with or without punctuation")
    group.add_argument("--name", help="partial name; you will be asked to choose")
    add_cmd.add_argument("--ticker", help="optional B3 ticker, used only for filenames")
    add_cmd.add_argument(
        "--this-entity-only",
        action="store_true",
        help="monitor only this entity, not the fund's other classes",
    )
    add_cmd.add_argument(
        "--no-resolve", action="store_true", help="write the entry without contacting the sources"
    )

    add_command("list", "show configured scopes and their resolved entities")

    resolve_cmd = add_command("resolve", "re-resolve scopes against the current sources")
    resolve_cmd.add_argument("--all", action="store_true", help="re-resolve even resolved scopes")

    add_command("reconcile", "heal intermediate states and sweep stale staging files")
    add_command("purge", "apply the retention frontier now")
    add_command("audit", "run the global-listing cross-check now")
    add_command("status", "summarise the manifest")
    add_command("doctor", "check the environment before a first run")

    return parser


def _bootstrap(args: argparse.Namespace) -> Config:
    # These may be absent rather than None: the shared flags use SUPPRESS so the
    # subparser cannot overwrite a value given before the subcommand.
    config = load(getattr(args, "config", None))
    if getattr(args, "verbose", False):
        config = _with_debug(config)
    configure(config.logging)
    return config


def _with_debug(config: Config) -> Config:
    from dataclasses import replace

    return replace(config, logging=replace(config.logging, level="DEBUG"))


# --------------------------------------------------------------------- commands


def cmd_run(config: Config, args: argparse.Namespace) -> int:
    report = execute(config, skip_audit=args.skip_audit, dry_run=args.dry_run)
    _print_summary(report)
    return report.exit_code


def cmd_add(config: Config, args: argparse.Namespace) -> int:
    if not args.cnpj and not args.name:
        print("error: give either --cnpj or --name", file=sys.stderr)
        return ExitCode.CONFIG

    funds_file = FundsFile.load(config.paths.funds_file)
    mode = ScopeMode.THIS_ENTITY_ONLY if args.this_entity_only else ScopeMode.FUND_AND_CLASSES

    cnpj_input = args.cnpj
    if cnpj_input is None:
        cnpj_input = _choose_by_name(config, args.name)
        if cnpj_input is None:
            return ExitCode.CONFIG

    normalized = normalize(cnpj_input)
    if normalized is None:
        print(f"error: {cnpj_input!r} is not a usable CNPJ", file=sys.stderr)
        return ExitCode.CONFIG
    if not is_valid(normalized):
        # A warning, not a refusal: the check digits catch typos, but the user
        # may legitimately know better than this arithmetic.
        print(f"warning: {format_masked(normalized)} fails the CNPJ check digits", file=sys.stderr)

    scope = Scope(cnpj=cnpj_input, mode=mode, ticker=args.ticker)

    if not funds_file.add_scope(scope):
        # Already registered. Re-running `add` to attach a ticker is a natural
        # thing to do, so apply what was asked for rather than reporting the
        # duplicate and dropping the flag on the floor.
        if funds_file.update_user_fields(scope):
            funds_file.save(backup=config.paths.funds_backup)
            print(f"{format_masked(normalized)} was already registered; updated it.")
            if args.ticker:
                print(f"  ticker set to {args.ticker}")
        else:
            print(f"{format_masked(normalized)} is already registered; nothing to change.")
        return ExitCode.OK

    if not args.no_resolve:
        registry = RegistryCache(config.cvm, config.paths.cvm_cache)
        snapshot = registry.load(config.source.user_agent)
        if snapshot is None:
            print(
                "warning: no CVM registry snapshot available; the entry was written and will "
                "be resolved on the next run",
                file=sys.stderr,
            )
        else:
            try:
                with FnetClient(config.source) as client:
                    resolve_scope(client, snapshot, scope)
                funds_file.update_scope(scope)
            except WatcherError as exc:
                print(f"warning: could not resolve it now: {exc}", file=sys.stderr)

    funds_file.save(backup=config.paths.funds_backup)

    print(f"Registered {scope.label} ({format_masked(normalized)}).")
    for entity in scope.entities:
        print(
            f"  entity {format_masked(entity.cnpj)}  id={entity.fundosnet_id}  "
            f"{entity.fnet_fund_description[:60]}"
        )
    if not scope.entities:
        print("  not yet resolved; the next run will complete it.")
    return ExitCode.OK


def _choose_by_name(config: Config, term: str) -> str | None:
    """Interactive disambiguation, against the CVM registry rather than Fundos.NET.

    A scope is registered by CNPJ, and Fundos.NET never returns one -- so
    searching it by name could only ever show the user a list they then had to
    resolve to a CNPJ themselves. The registry has both fields, and searching it
    is local: instant, instead of waiting on a source that stalls for a minute
    at a time.
    """
    registry = RegistryCache(config.cvm, config.paths.cvm_cache)
    snapshot = registry.load(config.source.user_agent)
    if snapshot is None:
        print(
            "error: the CVM registry is unavailable, so names cannot be searched. "
            "Register by CNPJ instead: --cnpj ...",
            file=sys.stderr,
        )
        return None

    matches = snapshot.search_by_name(term)
    if not matches:
        print(f"No FII fund or class matched {term!r} in the CVM registry.", file=sys.stderr)
        return None

    print(f"\n{len(matches)} match(es) for {term!r}:\n")
    for position, entity in enumerate(matches, start=1):
        state = "" if entity.active else f"  [{entity.situation}]"
        print(
            f"  {position:2}. {format_masked(entity.cnpj)}  {entity.kind:<5} "
            f"{entity.legal_name[:66]}{state}"
        )

    try:
        raw = input("\nNumber to register (blank to cancel): ").strip()
    except (EOFError, KeyboardInterrupt):
        print("\nCancelled.", file=sys.stderr)
        return None

    if not raw:
        return None
    if not raw.isdigit() or not 1 <= int(raw) <= len(matches):
        print(f"error: {raw!r} is not one of the listed numbers", file=sys.stderr)
        return None

    chosen = matches[int(raw) - 1]
    print(f"\nSelected {chosen.legal_name}")
    return chosen.cnpj


def cmd_list(config: Config, _args: argparse.Namespace) -> int:
    funds_file = FundsFile.load(config.paths.funds_file)
    scopes = funds_file.scopes()
    if not scopes:
        print("No scopes configured. Add one with `fii-docs-watcher add --cnpj ...`.")
        return ExitCode.OK

    for scope in scopes:
        marker = "ok" if scope.resolved else "UNRESOLVED"
        print(f"\n{scope.label}  [{marker}]  {format_masked(scope.cnpj)}")
        if scope.legal_name:
            print(f"  {scope.legal_name}")
        print(f"  mode={scope.mode.value}  expansion={scope.expansion.value}", end="")
        print(f"  cvm={scope.cvm_code or '-'}  status={scope.cvm_status or '-'}")
        for entity in scope.entities:
            confirmed = "confirmed" if entity.cnpj_confirmed else "unconfirmed"
            print(
                f"    - id={entity.fundosnet_id:<8} {format_masked(entity.cnpj)}  "
                f"[{confirmed}]  {entity.fnet_fund_description[:56]}"
            )
    print()
    return ExitCode.OK


def cmd_resolve(config: Config, args: argparse.Namespace) -> int:
    funds_file = FundsFile.load(config.paths.funds_file)
    registry = RegistryCache(config.cvm, config.paths.cvm_cache)
    snapshot = registry.load(config.source.user_agent, force_refresh=True)
    if snapshot is None:
        print("error: no CVM registry snapshot available", file=sys.stderr)
        return ExitCode.PARTIAL

    failures = 0
    with FnetClient(config.source) as client:
        for scope in funds_file.scopes():
            if scope.resolved and not args.all:
                continue
            try:
                resolve_scope(client, snapshot, scope)
                funds_file.update_scope(scope)
                print(f"{scope.label}: {len(scope.entities)} entity(ies), {scope.expansion.value}")
            except WatcherError as exc:
                failures += 1
                print(f"{scope.label}: FAILED - {exc}", file=sys.stderr)

    funds_file.save(backup=config.paths.funds_backup)
    return ExitCode.PARTIAL if failures else ExitCode.OK


def cmd_reconcile(config: Config, _args: argparse.Namespace) -> int:
    from .pipeline import reconcile

    connection = connect(config.paths.manifest_file)
    try:
        report = reconcile.run(ManifestRepo(connection), config)
    finally:
        connection.close()
    print(
        f"promoted={report.promoted} requeued={report.requeued} "
        f"staging_files_removed={report.parts_removed}"
    )
    for mismatch in report.hash_mismatches:
        print(f"  hash mismatch: {mismatch}", file=sys.stderr)
    return ExitCode.PARTIAL if report.hash_mismatches else ExitCode.OK


def cmd_purge(config: Config, _args: argparse.Namespace) -> int:
    from .pipeline import purge

    window = retention_window(config.retention.days)
    connection = connect(config.paths.manifest_file)
    try:
        report = purge.run(ManifestRepo(connection), config, window)
    finally:
        connection.close()
    print(
        f"frontier={to_dir_name(window.first)} directories_removed={report.directories_removed} "
        f"files_removed={report.files_removed} rows_marked={report.rows_marked}"
    )
    return ExitCode.PARTIAL if report.errors else ExitCode.OK


def cmd_audit(config: Config, _args: argparse.Namespace) -> int:
    from .pipeline import audit

    funds_file = FundsFile.load(config.paths.funds_file)
    connection = connect(config.paths.manifest_file)
    try:
        repo = ManifestRepo(connection)
        with FnetClient(config.source) as client:
            report = audit.run(client, repo, funds_file.scopes(), config.audit)
    finally:
        connection.close()

    if not report.ran:
        print("Audit is disabled by configuration.")
        return ExitCode.OK
    print(f"examined={report.documents_examined} unmatched={len(report.unmatched)}")
    for message in report.unmatched:
        print(f"  {message}", file=sys.stderr)
    return ExitCode.OK


def cmd_status(config: Config, _args: argparse.Namespace) -> int:
    window = retention_window(config.retention.days)
    connection = connect(config.paths.manifest_file)
    try:
        repo = ManifestRepo(connection)
        counts = repo.counts_by_state()
        available = repo.available_in_window(window.first, window.last)
        stale = repo.stale_watermarks(window.first)
    finally:
        connection.close()

    print(f"retention window: {window}  ({window.days} dates, including today)")
    print(f"manifest:         {config.paths.manifest_file}")
    print("\ndocuments by state:")
    for state, count in sorted(counts.items()):
        print(f"  {state:<12} {count}")
    if not counts:
        print("  (empty)")
    print(f"\navailable inside the window: {len(available)}")

    if stale:
        print("\nwatermark gaps beyond the retention window:", file=sys.stderr)
        for row in stale:
            print(
                f"  entity {row['fundosnet_id']}: last complete scan through "
                f"{row['last_window_end']}",
                file=sys.stderr,
            )
    return ExitCode.OK


def cmd_doctor(config: Config, _args: argparse.Namespace) -> int:
    """Check everything a first run depends on, before it matters."""
    from .clock import SOURCE_TZ, now
    from .run import prepare_roots

    problems: list[str] = []
    print(f"fii-docs-watcher {__version__}")
    print(f"timezone:        {SOURCE_TZ} (fixed; independent of the host)")
    print(f"local now:       {now().isoformat(timespec='seconds')}")
    print(f"today:           {to_dir_name(today())}")
    print(f"data root:       {config.paths.data_root}")
    print(f"documents root:  {config.paths.documents_root}")
    print(f"retention:       {config.retention.days} dates")
    print(f"page length:     {config.source.page_length}")
    print(f"read timeout:    {config.source.read_timeout_seconds}s")

    try:
        prepare_roots(config)
        print("roots:           ok (staging is on the same filesystem as the archive)")
    except (OSError, WatcherError) as exc:
        problems.append(str(exc))
        print(f"roots:           FAILED - {exc}", file=sys.stderr)

    try:
        connection = connect(config.paths.manifest_file)
        connection.close()
        print("manifest:        ok")
    except Exception as exc:
        problems.append(f"manifest: {exc}")
        print(f"manifest:        FAILED - {exc}", file=sys.stderr)

    try:
        with FnetClient(config.source) as client:
            payload = client.get("listarTodasCategoriaPorTipoFundo", {"idTipoFundo": 1}).json()
        print(f"fundos.net:      ok ({len(payload)} document categories)")
    except Exception as exc:
        problems.append(f"fundos.net: {exc}")
        print(f"fundos.net:      FAILED - {exc}", file=sys.stderr)

    try:
        registry = RegistryCache(config.cvm, config.paths.cvm_cache)
        snapshot = registry.load(config.source.user_agent)
        if snapshot is None:
            problems.append("cvm registry: unavailable")
            print("cvm registry:    FAILED - unavailable", file=sys.stderr)
        else:
            print(
                f"cvm registry:    ok ({snapshot.fund_count} FII funds, "
                f"{snapshot.class_count} classes)"
            )
    except Exception as exc:
        problems.append(f"cvm registry: {exc}")
        print(f"cvm registry:    FAILED - {exc}", file=sys.stderr)

    print()
    if problems:
        print(f"{len(problems)} problem(s) found.", file=sys.stderr)
        return ExitCode.PARTIAL
    print("All checks passed.")
    return ExitCode.OK


# ---------------------------------------------------------------------- output


def _print_summary(report: RunReport) -> None:
    print()
    print(f"window            {report.window}")
    if report.reconcile:
        print(
            f"reconciled        promoted={report.reconcile.promoted} "
            f"requeued={report.reconcile.requeued} staging_removed={report.reconcile.parts_removed}"
        )
    print(
        f"scopes            {report.scopes_resolved} resolved, {report.scopes_failed} failed, "
        f"{report.scopes_total} total"
    )
    if report.discovery:
        d = report.discovery
        print(
            f"discovery         entities={d.entities_scanned} failed={d.entities_failed} "
            f"seen={d.documents_seen} new={d.documents_new} superseded={d.superseded}"
        )
    if report.downloads:
        f = report.downloads
        print(
            f"downloads         ok={f.downloaded} failed={f.failed} "
            f"bytes={f.bytes_written:,}"
        )
    if report.inbox:
        print(f"inbox             {report.inbox.path} ({report.inbox.documents} document(s))")
    if report.purge:
        p = report.purge
        print(
            f"purge             directories={p.directories_removed} files={p.files_removed} "
            f"rows_marked={p.rows_marked}"
        )
    if report.audit and report.audit.ran:
        print(
            f"audit             examined={report.audit.documents_examined} "
            f"unmatched={len(report.audit.unmatched)}"
        )
    if report.interrupted:
        print("\ninterrupted by a shutdown signal; the next run resumes from the manifest")

    for warning in report.warnings:
        print(f"\nWARNING  {warning}", file=sys.stderr)
    for error in report.errors:
        print(f"\nERROR    {error}", file=sys.stderr)
    print()


_COMMANDS = {
    "run": cmd_run,
    "add": cmd_add,
    "list": cmd_list,
    "resolve": cmd_resolve,
    "reconcile": cmd_reconcile,
    "purge": cmd_purge,
    "audit": cmd_audit,
    "status": cmd_status,
    "doctor": cmd_doctor,
}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = _bootstrap(args)
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return ExitCode.CONFIG

    try:
        return _COMMANDS[args.command](config, args)
    except LockHeldError as exc:
        log.error("another instance is already running", extra={"error": str(exc)})
        return ExitCode.LOCKED
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return ExitCode.PARTIAL
    except WatcherError as exc:
        log.log(getattr(exc, "severity", logging.ERROR), str(exc))
        return ExitCode.PARTIAL


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
