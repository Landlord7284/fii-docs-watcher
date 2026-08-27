"""The inbox index: answering "what arrived today?".

Filing by delivery date is right for the archive and wrong for the daily
question it is meant to answer. After the machine has been off for three days,
the newly downloaded documents land in three past directories and today's
directory can look empty -- exactly when there is the most to read.

So each run writes `_inbox/{today}.md`, keyed on the *download* date, linking
into the delivery-date directories with relative paths.

An index rather than symlinks, deliberately: it works over SMB and on Windows
without link privileges, it duplicates nothing, and when purge removes a
directory the index for that day goes with it instead of leaving broken links.

Every index inside the retention window is rewritten on every run, not just
today's. A document downloaded on Monday can be superseded on Wednesday, and
Monday's index would otherwise keep a link to a file that no longer exists. The
rewrite also moves that entry into the trailing "Superseded versions" section,
so an entry never disappears from an index without an explanation.
"""

from __future__ import annotations

import logging
import os
from collections import defaultdict
from contextlib import suppress
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import quote

from ..clock import RetentionWindow, timestamp, to_dir_name, today
from ..config import Config
from ..manifest.repo import LocalState, ManifestDocument, ManifestRepo

log = logging.getLogger(__name__)


@dataclass
class InboxReport:
    path: str | None = None
    documents: int = 0
    superseded: int = 0
    files_written: int = 0
    written: bool = False


def _write_atomic(path: Path, content: str, mode: int) -> None:
    """Publish one generated index without exposing a partial final file."""
    temporary = path.with_name(f".{path.name}.tmp")
    data = content.encode("utf-8")
    fd = os.open(temporary, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, mode)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        with suppress(OSError):
            temporary.chmod(mode)
        temporary.replace(path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _link(document: ManifestDocument) -> str:
    """A relative link from `_inbox/x.md` to the document, URL-escaped.

    The `..` is what makes it work from inside `_inbox/`. Escaping matters
    because the names carry characters that Markdown viewers otherwise mangle.
    """
    return "../" + quote((document.path or "").replace("\\", "/"))


def _describe(document: ManifestDocument) -> str:
    parts = [part for part in (document.category, document.doc_type, document.species) if part]
    # The three fields overlap: the type often repeats the category, and species
    # is empty outside assemblies. Deduplicate while preserving order.
    seen: set[str] = set()
    unique = [p for p in parts if not (p.lower() in seen or seen.add(p.lower()))]
    return " / ".join(unique) or "Document"


def _entry(document: ManifestDocument, *, always_version: bool = False) -> str:
    """The descriptive tail shared by a live entry and a superseded one.

    `always_version` is for the superseded section, where which version was
    replaced is the whole point -- a bare `v1` is normally left implicit.
    """
    reference = f" · ref. {document.reference_date}" if document.reference_date else ""
    version = f" · v{document.version}" if always_version or document.version > 1 else ""
    normalized_status = document.status.casefold().strip() if document.status else ""
    status = (
        f" · **{document.status}**"
        if document.status and not normalized_status.startswith("ativo")
        else ""
    )
    return f"{_describe(document)}{reference}{version}{status}"


def render(
    documents: list[ManifestDocument],
    superseded: list[ManifestDocument] | None = None,
    *,
    for_date: date,
    window: RetentionWindow,
) -> str:
    """Render the index. Grouped by delivery date, newest first."""
    superseded = superseded or []
    by_delivery: dict[str, list[ManifestDocument]] = defaultdict(list)
    for document in documents:
        by_delivery[document.delivery_date].append(document)

    lines = [
        f"# Documents downloaded on {to_dir_name(for_date)}",
        "",
        f"{len(documents)} document(s), grouped by the date they were filed at Fundos.NET.",
        f"Retention window: {window} ({window.days} day(s), including today).",
        "",
    ]

    if not documents and not superseded:
        lines += [
            "Nothing new arrived today.",
            "",
            "That is not necessarily a problem: many funds go days without publishing.",
            "",
        ]

    for delivery_date in sorted(by_delivery, reverse=True):
        group = by_delivery[delivery_date]
        suffix = " — today" if delivery_date == to_dir_name(for_date) else ""
        lines.append(f"## {delivery_date}{suffix} ({len(group)})")
        lines.append("")
        for document in sorted(group, key=lambda d: (d.fund_description or "", d.document_id)):
            name = document.fund_description or f"entity {document.fundosnet_id}"
            lines.append(f"- [{name}]({_link(document)}) — {_entry(document)}")
        lines.append("")

    if superseded:
        # Last, and without links: these files are gone. The point of the section
        # is that an entry which was here yesterday does not vanish unexplained,
        # not to offer anything to open. The replacement is listed in the body of
        # whichever day's index it arrived on.
        lines.append(f"## Superseded versions ({len(superseded)})")
        lines.append("")
        lines.append("Replaced by a corrected re-filing. These files were removed.")
        lines.append("")
        for document in sorted(
            superseded, key=lambda d: (d.fund_description or "", d.document_id)
        ):
            name = document.fund_description or f"entity {document.fundosnet_id}"
            replacement = document.superseded_by
            by = (
                f" — replaced by {replacement[0]} v{replacement[1]}"
                if replacement
                else " — replaced by a later filing"
            )
            lines.append(f"- {name} — {_entry(document, always_version=True)}{by}")
        lines.append("")

    lines.append("---")
    lines.append("")
    lines.append(f"Generated by fii-docs-watcher at {timestamp()}.")
    lines.append("")
    return "\n".join(lines)


def run(repo: ManifestRepo, config: Config, window: RetentionWindow) -> InboxReport:
    """Write the index for every day in the window that has one, plus today's."""
    for_date = today()

    inbox_dir = config.paths.inbox_dir
    inbox_dir.mkdir(parents=True, exist_ok=True)
    # A share may refuse chmod outright; the index is still worth writing.
    with suppress(OSError):
        inbox_dir.chmod(config.files.directory_mode)

    report = InboxReport()
    for day in window.dates():
        path = inbox_dir / f"{to_dir_name(day)}.md"
        # Today's index is always written. A past day is only rewritten, never
        # invented: a first run must not fabricate indexes for days the robot
        # was not there for.
        if day != for_date and not path.exists():
            continue

        documents, superseded = _for_day(repo, day, window)
        _write_atomic(
            path,
            render(documents, superseded, for_date=day, window=window),
            config.files.file_mode,
        )
        report.files_written += 1

        if day == for_date:
            report.path = str(path.relative_to(config.paths.documents_root))
            report.documents = len(documents)
            report.superseded = len(superseded)
            report.written = True

    log.info(
        "inbox index written",
        extra={
            "file": report.path,
            "documents": report.documents,
            "superseded": report.superseded,
            "files": report.files_written,
        },
    )
    return report


def _for_day(
    repo: ManifestRepo, day: date, window: RetentionWindow
) -> tuple[list[ManifestDocument], list[ManifestDocument]]:
    """What arrived on `day`, split into what is still live and what was replaced."""
    documents = [
        document
        for document in repo.downloaded_between(
            f"{to_dir_name(day)}T00:00:00", f"{to_dir_name(day + timedelta(days=1))}T00:00:00"
        )
        # A document delivered outside the window is on its way out; linking to
        # it would produce an index entry that purge is about to invalidate.
        if window.contains_str(document.delivery_date)
    ]
    live = [d for d in documents if d.local_state == LocalState.AVAILABLE.value]
    gone = [d for d in documents if d.local_state == LocalState.SUPERSEDED.value]
    return live, gone
