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
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

from . import __version__
from .clock import retention_window, to_dir_name, today
from .config import CONFIG_SEARCH_PATH, Config, describe_source, load
from .cvm.registry import RegistryCache, servable_fund_types
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

MAIN_EPILOG = """\
Typical use:

  cp config.example.toml config.toml     copy and edit the roots
  fii-docs-watcher doctor                check the environment
  fii-docs-watcher add --name "kinea"    register a fund
  fii-docs-watcher run                   do the work (schedule this daily)

  fii-docs-watcher list kinea            find a registered fund
  fii-docs-watcher ticker kinea          annotate it with a B3 ticker
  fii-docs-watcher rm kinea              stop following it
  fii-docs-watcher status                what is in the archive

Configuration is discovered automatically, so --config is rarely needed:
  --config PATH, then $FII_WATCHER_CONFIG, then ./config.toml,
  ./fii-docs-watcher.toml, ~/.config/fii-docs-watcher/config.toml.
If none is found the built-in defaults are used and a warning says so.

Any value can be overridden by environment: FII_WATCHER_<SECTION>_<KEY>,
for example FII_WATCHER_RETENTION_DAYS=14.

Exit codes:
  0  clean
  1  ran, but something failed in isolation and was skipped
  2  invalid configuration or arguments
  3  another instance holds the lock

Full reference: USAGE.md
"""


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
        epilog=MAIN_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        parents=[common],
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    # No `set_defaults` here: `parents=` shares Action objects rather than
    # copying them, so setting a default on the parent would overwrite the
    # SUPPRESS sentinel on every subparser too, and the flag would stop working
    # before the subcommand. The fallback is applied after parsing instead.
    sub = parser.add_subparsers(dest="command", required=True)

    def add_command(
        name: str, help_text: str, description: str = "", epilog: str = ""
    ) -> argparse.ArgumentParser:
        # `help` is the one-liner in the command list; `description` is what
        # `<command> --help` shows. Without the second, each subcommand's own
        # help page is a bare usage line.
        return sub.add_parser(
            name,
            help=help_text,
            description=description or help_text,
            epilog=epilog,
            formatter_class=argparse.RawDescriptionHelpFormatter,
            parents=[common],
        )

    run_cmd = add_command(
        "run",
        "run the pipeline once (the canonical mode)",
        description=(
            "Run the whole pipeline once and exit. This is the only command a scheduler\n"
            "needs; everything else is for setting up and inspecting.\n\n"
            "In order: reconcile anything a previous run left half-done, refresh the CVM\n"
            "registry snapshot, resolve any scope that needs it, query the discovery\n"
            "window per entity, download what is new, delete any version a re-filing has\n"
            "replaced, write the inbox index, purge past the frontier, then run the global\n"
            "audit.\n\n"
            "There are two profiles. Plain `run` sweeps [discovery].days, which follows\n"
            "[retention].days unless the config says otherwise. `run --monitor` sweeps the\n"
            "narrower [discovery].monitor_days and skips the global audit, so it is cheap\n"
            "enough to schedule often. Everything else is the same in both.\n\n"
            "Safe to run as often as you like: rediscovering a document updates its status\n"
            "and never downloads it again."
        ),
        epilog=(
            "Note: this source answers in either ~0.3s or ~60s, so a run with several\n"
            "entities taking minutes is normal, not stuck. Progress is logged per step."
        ),
    )
    # A profile, never a number: a cron line says which profile sweeps, so
    # retuning how many days it covers stays an edit to the config file.
    run_cmd.add_argument(
        "--monitor",
        action="store_true",
        help="the frequent profile: sweep [discovery].monitor_days and skip the audit",
    )
    run_cmd.add_argument(
        "--dry-run", action="store_true", help="resolve scopes and stop before discovery"
    )
    run_cmd.add_argument("--skip-audit", action="store_true", help="skip the global audit scan")

    add_cmd = add_command(
        "add",
        "register a fund to monitor",
        description=(
            "Register a fund by CNPJ, or search for one by name and pick from the matches.\n\n"
            "You give one CNPJ; the robot works out the rest. Since RCVM 175 a fund's\n"
            "documents may be filed by its share classes, each with its own CNPJ and its own\n"
            "Fundos.NET id, so registering a fund CNPJ monitors the fund and its active\n"
            "classes. Registering a class CNPJ monitors only that class."
        ),
        epilog=(
            "Examples:\n"
            "  fii-docs-watcher add --cnpj 08.431.747/0001-06 --ticker HGBS11\n"
            '  fii-docs-watcher add --name "kinea renda"\n'
        ),
    )
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

    list_cmd = add_command(
        "list",
        "show registered funds, optionally narrowed by a search term",
        description=(
            "List registered funds with their resolved entities.\n\n"
            "With a QUERY, only matching funds are shown. The search covers the ticker, the\n"
            "legal name, the Fundos.NET description and the CNPJ digits, ignoring accents,\n"
            "case and punctuation."
        ),
        epilog="Examples:\n  fii-docs-watcher list\n  fii-docs-watcher list kinea\n"
        "  fii-docs-watcher list 08431747\n",
    )
    list_cmd.add_argument(
        "query", nargs="?", metavar="QUERY", help="narrow the listing to matching funds"
    )

    rm_cmd = add_command(
        "rm",
        "stop following a fund and remove it from the watch list",
        description=(
            "Remove a fund from the watch list, so it is no longer queried.\n\n"
            "Documents already in the archive stay where they are by default and age out\n"
            "through the normal retention window; anything discovered but not yet\n"
            "downloaded is stood down so it is never fetched. Pass --delete-documents to\n"
            "remove the archived files immediately instead.\n\n"
            "Manifest rows are kept either way -- they are a record of what the source\n"
            "published while you were following the fund, and cost almost nothing.\n\n"
            "The previous watch list is saved as funds.yaml.bak, which is the undo."
        ),
        epilog=(
            "Examples:\n"
            "  fii-docs-watcher rm kinea                     # pick, confirm\n"
            "  fii-docs-watcher rm 12005956 --yes            # no prompt\n"
            "  fii-docs-watcher rm kinea --yes --delete-documents\n"
        ),
    )
    rm_cmd.add_argument("query", metavar="QUERY", help="search term identifying the fund")
    rm_cmd.add_argument("-y", "--yes", action="store_true", help="do not ask for confirmation")
    rm_cmd.add_argument(
        "--delete-documents",
        action="store_true",
        help="also delete this fund's files from the archive now",
    )

    ticker_cmd = add_command(
        "ticker",
        "set or clear the ticker on an already-registered fund",
        description=(
            "Attach a B3 ticker to a fund you already registered.\n\n"
            "The ticker is your annotation: the robot never fills it in or validates it,\n"
            "because no native source for it exists in CVM or Fundos.NET data. It is used\n"
            "as the prefix of downloaded filenames.\n\n"
            "Existing files are never renamed -- a filename records the prefix that was true\n"
            "when it was downloaded. Only future downloads use the new one."
        ),
        epilog=(
            "Examples:\n"
            "  fii-docs-watcher ticker kinea              # pick and prompt\n"
            "  fii-docs-watcher ticker kinea --set KNRI11 # non-interactive\n"
            "  fii-docs-watcher ticker kinea --clear\n"
        ),
    )
    ticker_cmd.add_argument("query", metavar="QUERY", help="search term identifying the fund")
    ticker_cmd.add_argument("--set", metavar="TICKER", help="set this ticker without prompting")
    ticker_cmd.add_argument("--clear", action="store_true", help="remove the ticker")

    resolve_cmd = add_command(
        "resolve",
        "re-resolve scopes against the current sources",
        description=(
            "Re-run the CNPJ -> entities -> Fundos.NET id resolution and write the result\n"
            "back to funds.yaml. Refreshes the CVM registry first.\n\n"
            "Useful after a fund is renamed, after a new share class is created, or when the\n"
            "audit reports a document that per-entity discovery did not capture."
        ),
    )
    resolve_cmd.add_argument("--all", action="store_true", help="re-resolve even resolved scopes")

    add_command(
        "reconcile",
        "heal intermediate states and sweep stale staging files",
        description=(
            "Settle anything an interrupted run left behind, then remove orphaned .part\n"
            "files. `run` does this automatically at startup; this exposes it on its own.\n\n"
            "A file whose bytes still validate is adopted into the archive rather than\n"
            "downloaded again; one that is missing or corrupt goes back into the queue."
        ),
    )
    add_command(
        "purge",
        "apply the retention frontier now",
        description=(
            "Delete date directories older than today - (N - 1), where N is\n"
            "[retention].days. `run` does this at the end of every run.\n\n"
            "Manifest rows are kept and marked purged: knowing a document existed is cheap\n"
            "and useful, and only the file is temporary."
        ),
    )
    add_command(
        "audit",
        "run the global-listing cross-check now",
        description=(
            "Scan the global listing and report documents whose fund name matches a\n"
            "monitored scope but which per-entity discovery did not capture.\n\n"
            "Detective only. It never files a document -- routing by name is precisely the\n"
            "silent failure this design exists to avoid -- and never fails the job. A hit\n"
            "means a scope needs revalidating: a new class, or a stale Fundos.NET id."
        ),
    )
    add_command(
        "status",
        "summarise the manifest",
        description=(
            "Show the retention window, document counts by state, and any entity whose last\n"
            "complete scan predates the retention frontier (documents in such a gap were\n"
            "published and purged without ever being seen, and cannot be recovered)."
        ),
    )
    add_command(
        "doctor",
        "check the environment before a first run",
        description=(
            "Verify everything a run depends on: which config file was resolved, both roots\n"
            "writable, staging on the same filesystem as the archive (or `rename` would not\n"
            "be atomic), the manifest openable, and both sources reachable.\n\n"
            "Run this first on a new machine."
        ),
    )

    return parser


def _bootstrap(args: argparse.Namespace) -> Config:
    # These may be absent rather than None: the shared flags use SUPPRESS so the
    # subparser cannot overwrite a value given before the subcommand.
    config = load(getattr(args, "config", None))
    if getattr(args, "verbose", False):
        config = _with_debug(config)
    configure(config.logging)

    # Announced only now, once logging exists. A defaults-only load is a warning
    # rather than a note: the built-in roots are `./var/...`, so someone with a
    # config file elsewhere would otherwise be writing to a different archive
    # than they think.
    if config.source_path is None:
        log.warning(
            "no config file found; using built-in defaults",
            extra={
                "searched": ", ".join(str(p) for p in CONFIG_SEARCH_PATH),
                "data_root": str(config.paths.data_root),
                "documents_root": str(config.paths.documents_root),
            },
        )
    else:
        log.debug("configuration loaded", extra={"config_file": str(config.source_path)})
    return config


def _with_debug(config: Config) -> Config:
    from dataclasses import replace

    return replace(config, logging=replace(config.logging, level="DEBUG"))


# --------------------------------------------------------------------- commands


def cmd_run(config: Config, args: argparse.Namespace) -> int:
    report = execute(
        config, monitor=args.monitor, skip_audit=args.skip_audit, dry_run=args.dry_run
    )
    _print_summary(report)
    return report.exit_code


def cmd_add(config: Config, args: argparse.Namespace) -> int:
    if not args.cnpj and not args.name:
        print("error: give either --cnpj or --name", file=sys.stderr)
        return ExitCode.CONFIG

    funds_file = FundsFile.load(config.paths.funds_file)
    mode = ScopeMode.THIS_ENTITY_ONLY if args.this_entity_only else ScopeMode.FUND_AND_CLASSES

    cnpj_input = args.cnpj
    ticker = args.ticker
    if cnpj_input is None:
        cnpj_input = _choose_by_name(config, args.name)
        if cnpj_input is None:
            return ExitCode.CONFIG
        # Having just picked a fund by name, being asked for its ticker is the
        # natural next question. Only when one was not already given, and only
        # with a terminal present -- a scheduled run must never block on input.
        if ticker is None and sys.stdin.isatty():
            try:
                ticker = input("Ticker (optional, Enter to skip): ").strip() or None
            except (EOFError, KeyboardInterrupt):
                ticker = None

    normalized = normalize(cnpj_input)
    if normalized is None:
        print(f"error: {cnpj_input!r} is not a usable CNPJ", file=sys.stderr)
        return ExitCode.CONFIG
    if not is_valid(normalized):
        # A warning, not a refusal: the check digits catch typos, but the user
        # may legitimately know better than this arithmetic.
        print(f"warning: {format_masked(normalized)} fails the CNPJ check digits", file=sys.stderr)

    scope = Scope(cnpj=cnpj_input, mode=mode, ticker=ticker)

    if not funds_file.add_scope(scope):
        # Already registered. Re-running `add` to attach a ticker is a natural
        # thing to do, so apply what was asked for rather than reporting the
        # duplicate and dropping the flag on the floor.
        if funds_file.update_user_fields(scope):
            funds_file.save(backup=config.paths.funds_backup)
            print(f"{format_masked(normalized)} was already registered; updated it.")
            if ticker:
                print(f"  ticker set to {ticker}")
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
            f"type={entity.fnet_fund_type}  {entity.fnet_fund_description[:60]}"
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


@dataclass(frozen=True)
class _Column:
    """One column of the `list` table.

    `caption` marks a column that describes a fund rather than identifying it:
    when every listed row agrees on its value, it is printed once above the
    table under that caption instead of repeated on every line. The caption has
    room for words the column header cannot fit.
    """

    key: str
    title: str
    caption: str = ""
    align_right: bool = False
    elastic: bool = False

    @property
    def hoistable(self) -> bool:
        return bool(self.caption)


_LIST_COLUMNS = (
    _Column("ticker", "TICKER"),
    _Column("state", "ST", caption="state"),
    _Column("cnpj", "CNPJ"),
    _Column("confirmed", "CONFIRMED", caption="cnpj confirmed by a download"),
    _Column("fnet_id", "FNET ID", align_right=True),
    _Column("fnet_type", "TYPE", caption="fnet fund type", align_right=True),
    _Column("cvm", "CVM", align_right=True),
    _Column("cvm_status", "CVM STATUS", caption="cvm status"),
    _Column("mode", "MODE", caption="mode"),
    _Column("expansion", "EXPANSION", caption="expansion"),
    _Column("name", "NAME", elastic=True),
)

# Below this a name column stops being worth reading, so a narrow terminal gets
# a line that overflows rather than a column of ellipses.
_MIN_ELASTIC_WIDTH = 24


def _terminal_width() -> int:
    """Usable width, with a sane answer when the output is a pipe or a file."""
    return max(60, shutil.get_terminal_size(fallback=(100, 24)).columns)


def _shorten(value: str, width: int) -> str:
    return value if len(value) <= width else value[: max(1, width - 1)] + "\u2026"


def _list_rows(scopes: list[Scope]) -> list[dict[str, str]]:
    """One row per entity, with the fund's own cells blank after the first.

    A multiclass scope is still one fund to the reader, so repeating its name,
    mode and CVM registration on every class row would bury the ids and CNPJs
    that are the only things differing between them. An unresolved scope has no
    entity yet and contributes a single row.
    """
    rows: list[dict[str, str]] = []
    for scope in scopes:
        fund = {
            "ticker": scope.ticker or "-",
            "state": "ok" if scope.resolved else "UNRESOLVED",
            # Blank, not a dash: an unknown cell says nothing, and `_hoist_common`
            # reads it as "no opinion" rather than as a value the rows disagree on.
            "cvm": scope.cvm_code or "",
            "cvm_status": scope.cvm_status or "",
            "mode": scope.mode.value,
            "expansion": scope.expansion.value,
            "name": scope.legal_name or "",
        }
        if not scope.entities:
            rows.append({**fund, "cnpj": format_masked(scope.cnpj) or scope.cnpj})
            continue
        for position, entity in enumerate(scope.entities):
            entity_cells = {
                "cnpj": format_masked(entity.cnpj) or entity.cnpj,
                # Whether a downloaded file has proved this entity's CNPJ, not
                # how the fund was registered -- see `fetch._validate_and_confirm_cnpj`.
                "confirmed": "yes" if entity.cnpj_confirmed else "not yet",
                "fnet_id": str(entity.fundosnet_id),
                "fnet_type": str(entity.fnet_fund_type),
            }
            if position == 0:
                name = fund["name"] or entity.fnet_fund_description
                rows.append({**fund, **entity_cells, "name": name})
            else:
                rows.append({**entity_cells, "name": entity.fnet_fund_description})
    return rows


def _hoist_common(
    rows: list[dict[str, str]], columns: tuple[_Column, ...]
) -> tuple[dict[str, str], list[_Column]]:
    """Split the columns into what every row agrees on and what still varies.

    Blank cells are continuation rows of a fund already described above, not
    disagreement, so they are ignored; a column nobody filled in at all is
    dropped entirely rather than hoisted as an empty fact.
    """
    common: dict[str, str] = {}
    kept: list[_Column] = []
    for column in columns:
        values = {row.get(column.key, "") for row in rows}
        values.discard("")
        if not values:
            continue
        if column.hoistable and len(values) == 1:
            common[column.caption] = values.pop()
        else:
            kept.append(column)
    return common, kept


def _render_table(rows: list[dict[str, str]], columns: list[_Column], width: int) -> list[str]:
    widths = {
        column.key: max(len(column.title), *(len(row.get(column.key, "")) for row in rows))
        for column in columns
    }
    elastic = [column for column in columns if column.elastic]
    if elastic:
        gutters = 2 * (len(columns) - 1)
        fixed = sum(w for key, w in widths.items() if key not in {c.key for c in elastic})
        share = (width - fixed - gutters) // len(elastic)
        for column in elastic:
            widths[column.key] = max(_MIN_ELASTIC_WIDTH, min(widths[column.key], share))

    def line(cells: dict[str, str]) -> str:
        parts = []
        for column in columns:
            text = _shorten(cells.get(column.key, ""), widths[column.key])
            size = widths[column.key]
            parts.append(text.rjust(size) if column.align_right else text.ljust(size))
        return "  ".join(parts).rstrip()

    rule = "  ".join("-" * widths[column.key] for column in columns)
    return [line({column.key: column.title for column in columns}), rule, *(line(r) for r in rows)]


def cmd_list(config: Config, args: argparse.Namespace) -> int:
    funds_file = FundsFile.load(config.paths.funds_file)
    all_scopes = funds_file.scopes()
    if not all_scopes:
        print("No scopes configured. Add one with `fii-docs-watcher add --cnpj ...`.")
        return ExitCode.OK

    query = getattr(args, "query", None) or ""
    scopes = [s for s in all_scopes if s.matches(query)]
    if query and not scopes:
        print(f"No registered fund matches {query!r}. {len(all_scopes)} registered in total.")
        return ExitCode.OK

    rows = _list_rows(scopes)
    common, columns = _hoist_common(rows, _LIST_COLUMNS)
    entities = sum(len(scope.entities) for scope in scopes)
    print(f"\n{len(scopes)} fund(s), {entities} entity(ies)")

    if common:
        caption_width = max(len(caption) for caption in common)
        print("\ncommon to all")
        for caption, value in common.items():
            print(f"  {caption.ljust(caption_width)}  {value}")

    print()
    for text in _render_table(rows, columns, _terminal_width()):
        print(text)
    print()
    if query:
        print(f"showing {len(scopes)} of {len(all_scopes)} registered fund(s)\n")
    return ExitCode.OK


def _select_scope(scopes: list[Scope], query: str) -> Scope | None:
    """Narrow registered scopes to one, asking the user only when it is ambiguous."""
    matches = [scope for scope in scopes if scope.matches(query)]
    if not matches:
        print(f"No registered fund matches {query!r}.", file=sys.stderr)
        return None
    if len(matches) == 1:
        return matches[0]

    print(f"\n{len(matches)} registered funds match {query!r}:\n")
    for position, scope in enumerate(matches, start=1):
        current = f"  ticker={scope.ticker}" if scope.ticker else ""
        print(
            f"  {position:2}. {format_masked(scope.cnpj)}  "
            f"{(scope.legal_name or '(unresolved)')[:58]}{current}"
        )

    if not sys.stdin.isatty():
        print(
            "\nerror: several funds match and there is no terminal to ask on; "
            "narrow the search term",
            file=sys.stderr,
        )
        return None
    try:
        raw = input("\nNumber (blank to cancel): ").strip()
    except (EOFError, KeyboardInterrupt):
        print("\nCancelled.", file=sys.stderr)
        return None
    if not raw:
        return None
    if not raw.isdigit() or not 1 <= int(raw) <= len(matches):
        print(f"error: {raw!r} is not one of the listed numbers", file=sys.stderr)
        return None
    return matches[int(raw) - 1]


def cmd_ticker(config: Config, args: argparse.Namespace) -> int:
    """Set or clear the ticker on an already-registered fund."""
    funds_file = FundsFile.load(config.paths.funds_file)
    scopes = funds_file.scopes()
    if not scopes:
        print("No scopes configured. Add one with `fii-docs-watcher add --cnpj ...`.")
        return ExitCode.OK

    scope = _select_scope(scopes, args.query)
    if scope is None:
        return ExitCode.CONFIG

    if args.clear:
        new_ticker = ""
    elif args.set is not None:
        new_ticker = args.set.strip()
    elif sys.stdin.isatty():
        current = f" [{scope.ticker}]" if scope.ticker else ""
        try:
            new_ticker = input(f"Ticker for {scope.label}{current} (blank clears): ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nCancelled.", file=sys.stderr)
            return ExitCode.OK
    else:
        print("error: give --set VALUE or --clear when there is no terminal", file=sys.stderr)
        return ExitCode.CONFIG

    if (scope.ticker or "") == new_ticker:
        print(f"{scope.label}: ticker unchanged.")
        return ExitCode.OK

    previous = scope.ticker
    scope.ticker = new_ticker or None
    if not funds_file.update_user_fields(scope):
        print(f"{scope.label}: nothing to change.")
        return ExitCode.OK
    funds_file.save(backup=config.paths.funds_backup)

    if scope.ticker:
        print(f"{format_masked(scope.cnpj)}: ticker {previous or '(none)'} -> {scope.ticker}")
    else:
        print(f"{format_masked(scope.cnpj)}: ticker cleared (was {previous})")
    # Worth saying plainly: the prefix in a filename is a snapshot taken when the
    # document was downloaded, and old files are never renamed (spec 5.4).
    print("Existing files keep their old names; only future downloads use the new prefix.")
    return ExitCode.OK


def cmd_rm(config: Config, args: argparse.Namespace) -> int:
    """Stop following a fund: remove it from the watch list."""
    funds_file = FundsFile.load(config.paths.funds_file)
    scopes = funds_file.scopes()
    if not scopes:
        print("No scopes configured; nothing to remove.")
        return ExitCode.OK

    scope = _select_scope(scopes, args.query)
    if scope is None:
        return ExitCode.CONFIG

    entity_ids = [entity.fundosnet_id for entity in scope.entities]
    print(f"\n{scope.label}  {format_masked(scope.cnpj)}")
    if scope.legal_name:
        print(f"  {scope.legal_name}")
    print(f"  {len(entity_ids)} monitored entity(ies)")

    # Say what will happen to the documents before asking, because "remove the
    # fund" and "delete its files" are different things and only one of them is
    # reversible.
    connection = connect(config.paths.manifest_file)
    try:
        repo = ManifestRepo(connection)
        on_disk = repo.available_for_entities(entity_ids)
        print(f"  {len(on_disk)} document(s) currently in the archive")
        if args.delete_documents:
            print("  those files WILL BE DELETED (--delete-documents)")
        else:
            print(
                f"  those files stay and age out normally within "
                f"{config.retention.days} day(s)"
            )

        if not args.yes:
            if not sys.stdin.isatty():
                print(
                    "\nerror: refusing to remove without confirmation; pass --yes",
                    file=sys.stderr,
                )
                return ExitCode.CONFIG
            try:
                answer = input("\nStop following this fund? [y/N] ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                print("\nCancelled.", file=sys.stderr)
                return ExitCode.OK
            if answer not in {"y", "yes"}:
                print("Cancelled.")
                return ExitCode.OK

        if not funds_file.remove_scope(scope):
            print("error: the entry disappeared before it could be removed", file=sys.stderr)
            return ExitCode.PARTIAL
        funds_file.save(backup=config.paths.funds_backup)

        # Discovery stops asking about this fund, but the download queue is
        # built from the manifest, so its backlog has to be stood down too.
        abandoned = repo.abandon_pending(entity_ids)
        # The watermark and last error describe monitoring that is over. The
        # document rows stay: those record what the source actually published.
        repo.forget_entities(entity_ids)

        removed_files = 0
        if args.delete_documents and on_disk:
            removed_files = _delete_documents(config, repo, on_disk)
    finally:
        connection.close()

    print(f"\nRemoved {format_masked(scope.cnpj)} from the watch list.")
    if abandoned:
        print(f"  {abandoned} queued document(s) will no longer be downloaded")
    if args.delete_documents:
        print(f"  {removed_files} archived file(s) deleted")
    elif on_disk:
        print(f"  {len(on_disk)} archived file(s) left in place; they age out with retention")
    print(f"  a copy of the previous list is in {config.paths.funds_backup}")
    return ExitCode.OK


def _delete_documents(config: Config, repo: ManifestRepo, documents: list) -> int:
    """Delete archived files for a removed fund, then mark their rows purged."""
    from .pipeline import purge

    removed = purge.remove_files(config, documents)
    repo.mark_documents_purged(removed)
    return len(removed)


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
    monitored_ids = {
        entity.fundosnet_id
        for scope in FundsFile.load(config.paths.funds_file).scopes()
        for entity in scope.entities
    }
    connection = connect(config.paths.manifest_file)
    try:
        repo = ManifestRepo(connection)
        counts = repo.counts_by_state()
        available = repo.available_in_window(window.first, window.last)
        # Only funds still on the watch list: a gap for a fund you removed on
        # purpose is not a loss to report.
        stale = repo.stale_watermarks(window.first, monitored_ids)
        cursors = repo.all_listing_cursors()
    finally:
        connection.close()

    print(f"retention window: {window}  ({window.days} dates, including today)")
    for line in _window_lines(config):
        print(f"                  {line}")
    print(f"manifest:         {config.paths.manifest_file}")
    print("\ndocuments by state:")
    for state, count in sorted(counts.items()):
        print(f"  {state:<12} {count}")
    if not counts:
        print("  (empty)")
    print(f"\navailable inside the window: {len(available)}")

    if cursors:
        print("\nmonitor listing cursors:")
        for row in cursors:
            print(f"  tipoFundo={row['fund_type']}: through {row['last_delivery_at']}")

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
    from .clock import DEFAULT_TIMEZONE, now, source_tz
    from .run import prepare_roots

    problems: list[str] = []
    print(f"fii-docs-watcher {__version__}")
    print(f"config:          {describe_source(config)}")
    origin = "default" if config.source.timezone == DEFAULT_TIMEZONE else "configured"
    print(f"timezone:        {source_tz()} ({origin}; independent of the host)")
    print(f"local now:       {now().isoformat(timespec='seconds')}")
    print(f"today:           {to_dir_name(today())}")
    print(f"data root:       {config.paths.data_root}")
    print(f"documents root:  {config.paths.documents_root}")
    print(f"retention:       {config.retention.days} dates")
    for line in _window_lines(config):
        print(f"                 {line}")
    formats = ", ".join(config.download.formats)
    suffix = "" if config.download.all_formats else "  (other formats are not downloaded)"
    print(f"formats:         {formats}{suffix}")
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
        counts = []
        with FnetClient(config.source) as client:
            for fund_type in servable_fund_types():
                payload = client.get(
                    "listarTodasCategoriaPorTipoFundo", {"idTipoFundo": fund_type}
                ).json()
                counts.append(f"type {fund_type}: {len(payload)}")
        print(f"fundos.net:      ok (document categories -- {', '.join(counts)})")
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
                f"cvm registry:    ok ({snapshot.fund_count} funds, "
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


def _window_lines(config: Config) -> list[str]:
    """Both discovery windows resolved against today, each named after its profile.

    Printed by `doctor` and `status` so that which dates each profile asks
    about is verifiable without spending a sweep to find out.
    """
    lines = []
    for command, monitor in (("run", False), ("run --monitor", True)):
        window = retention_window(config.sweep_days(monitor=monitor))
        lines.append(f"{window} ({window.days} dates), swept by `{command}`")
    return lines


def _print_summary(report: RunReport) -> None:
    print()
    print(f"profile           {'monitor' if report.monitor else 'sweep'}")
    print(f"window            {report.window}")
    # Only when it differs: a run that asked about two days must not read like
    # one that asked about seven, and repeating an identical window would just
    # be noise on the profile that has only ever had the one.
    if report.discovery_window is not None and report.discovery_window != report.window:
        print(f"discovery window  {report.discovery_window}")
    if report.reconcile:
        print(
            f"reconciled        promoted={report.reconcile.promoted} "
            f"requeued={report.reconcile.requeued} staging_removed={report.reconcile.parts_removed}"
        )
    print(
        f"scopes            {report.scopes_resolved} resolved, {report.scopes_failed} failed, "
        f"{report.scopes_total} total"
    )
    if report.abandoned:
        print(
            f"stood down        {report.abandoned} queued document(s) whose fund left the "
            "watch list"
        )
    if report.discovery:
        d = report.discovery
        print(
            f"discovery         entities={d.entities_scanned} failed={d.entities_failed} "
            f"seen={d.documents_seen} new={d.documents_new}"
        )
    if report.supersede and (
        report.supersede.detected
        or report.supersede.files_removed
        or report.supersede.deferred
    ):
        sp = report.supersede
        extras = f"  deferred={sp.deferred}" if sp.deferred else ""
        print(
            f"superseded        detected={sp.detected} files_removed={sp.files_removed} "
            f"cancelled={sp.pending_cancelled}{extras}"
        )
    if report.downloads:
        f = report.downloads
        extras = ""
        if f.skipped:
            extras += f"  skipped_by_format={f.skipped}"
        if f.deferred:
            extras += f"  deferred={f.deferred}"
        print(
            f"downloads         ok={f.downloaded} failed={f.failed} "
            f"bytes={f.bytes_written:,}{extras}"
        )
    if report.inbox:
        i = report.inbox
        extras = f", {i.superseded} superseded" if i.superseded else ""
        print(f"inbox             {i.path} ({i.documents} document(s){extras})")
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
    "rm": cmd_rm,
    "ticker": cmd_ticker,
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
