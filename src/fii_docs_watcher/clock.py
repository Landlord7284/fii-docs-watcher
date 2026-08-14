"""Time, fixed to America/Sao_Paulo.

`dataEntrega` arrives without a timezone, so "today" has to mean today in the
source's timezone regardless of where this process happens to run. That single
choice governs the archive's directory names, the retention frontier, the query
window, the _inbox filename and the watermark. A container running in UTC must
produce byte-identical output to a laptop in São Paulo.

Nothing here reads the host timezone, and nothing here is user-configurable:
this is a property of the data source, not a preference.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from .errors import SourceContractError

SOURCE_TZ = ZoneInfo("America/Sao_Paulo")

# How the source formats dates on the wire, and how we format them on disk.
WIRE_DATETIME = "%d/%m/%Y %H:%M"
WIRE_DATE = "%d/%m/%Y"
WIRE_MONTH = "%m/%Y"
DIR_DATE = "%Y-%m-%d"
_DIR_DATE_SHAPE = re.compile(r"\d{4}-\d{2}-\d{2}")

# Discriminator values for `dataReferencia`, which arrives in three shapes.
# The source sends these as strings; callers normalise before comparing.
REF_FORMAT_MONTH = "2"  # MM/yyyy competence
REF_FORMAT_DATE = "3"  # dd/MM/yyyy
REF_FORMAT_DATETIME = "4"  # dd/MM/yyyy HH:mm


def now() -> datetime:
    """Current instant in the source's timezone."""
    return datetime.now(SOURCE_TZ)


def today() -> date:
    """Today in the source's timezone. The archive's only notion of 'today'."""
    return now().date()


def timestamp() -> str:
    """An ISO-8601 instant for manifest columns, with the source's offset attached."""
    return now().isoformat(timespec="seconds")


def to_dir_name(value: date) -> str:
    """Format a date as the archive directory name.

    Always zero-padded `yyyy-mm-dd`, never locale-dependent: purge and human
    reading both rely on lexicographic order matching chronological order.
    """
    return value.strftime(DIR_DATE)


def parse_dir_name(name: str) -> date | None:
    """Parse an archive directory name, or None if it is not one.

    Used by purge to tell date directories apart from `_inbox`, `.tmp` and
    anything a human may have dropped into the archive root.

    The shape is checked before parsing because `strptime` happily accepts
    `2026-8-4`, which is not a name this robot ever writes. Purge deletes what
    this function recognises, so recognising something we did not create would
    be the wrong kind of generous.
    """
    if not _DIR_DATE_SHAPE.fullmatch(name):
        return None
    try:
        return datetime.strptime(name, DIR_DATE).date()
    except ValueError:
        return None


def to_wire_date(value: date) -> str:
    """Format a date for `dataInicial` / `dataFinal`. Both ends are inclusive."""
    return value.strftime(WIRE_DATE)


def parse_delivery(raw: str) -> datetime:
    """Parse `dataEntrega` (`dd/MM/yyyy HH:mm`), the discovery and archiving axis.

    Naive on the wire, so it is stamped with the source timezone here. A value
    this pipeline cannot parse is a contract change, not a bad row: the whole
    archive is keyed on this field.
    """
    text = (raw or "").strip()
    for fmt in (WIRE_DATETIME, WIRE_DATE):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=SOURCE_TZ)
        except ValueError:
            continue
    raise SourceContractError(
        f"unparseable dataEntrega: {raw!r}", context={"field": "dataEntrega", "value": raw}
    )


def parse_reference(raw: str | None, ref_format: str | None) -> str | None:
    """Normalise `dataReferencia` to a sortable string, using its declared format.

    Returns `yyyy-mm` for a monthly competence and `yyyy-mm-dd` (optionally with
    a time) otherwise. Reference dates can legitimately be in the future, so no
    range check belongs here.

    Unlike `dataEntrega` this is metadata, never a filing decision, so an
    unrecognised shape degrades to the raw string instead of failing the row.
    """
    text = (raw or "").strip()
    if not text:
        return None
    fmt = str(ref_format).strip() if ref_format is not None else ""
    attempts: tuple[tuple[str, str], ...]
    if fmt == REF_FORMAT_MONTH:
        attempts = ((WIRE_MONTH, "%Y-%m"),)
    elif fmt == REF_FORMAT_DATETIME:
        attempts = ((WIRE_DATETIME, "%Y-%m-%dT%H:%M"), (WIRE_DATE, DIR_DATE))
    else:
        attempts = ((WIRE_DATE, DIR_DATE), (WIRE_DATETIME, "%Y-%m-%dT%H:%M"), (WIRE_MONTH, "%Y-%m"))
    for parse_fmt, out_fmt in attempts:
        try:
            return datetime.strptime(text, parse_fmt).strftime(out_fmt)
        except ValueError:
            continue
    return text


@dataclass(frozen=True)
class RetentionWindow:
    """The one frontier shared by purge, the query window and the index.

    `days` counts dates kept *including* today, so N=7 on the 14th keeps the 8th
    through the 14th. Deriving all three consumers from this single object is
    what stops discovery from downloading documents that purge deletes minutes
    later.
    """

    first: date
    last: date
    days: int

    def contains(self, value: date) -> bool:
        return self.first <= value <= self.last

    def contains_str(self, value: str) -> bool:
        """Range check on a stored `yyyy-mm-dd` string, without reparsing it.

        Sound only because the format is fixed and zero-padded, which makes
        lexicographic order identical to chronological order -- the same
        property purge and human directory listings depend on.
        """
        return to_dir_name(self.first) <= value <= to_dir_name(self.last)

    def dates(self) -> list[date]:
        return [self.first + timedelta(days=offset) for offset in range(self.days)]

    def __str__(self) -> str:
        return f"[{to_dir_name(self.first)}, {to_dir_name(self.last)}]"


def retention_window(days: int, reference: date | None = None) -> RetentionWindow:
    """Build the retention window ending on `reference` (default: today)."""
    if days < 1:
        raise ValueError(f"retention days must be >= 1, got {days}")
    last = reference or today()
    return RetentionWindow(first=last - timedelta(days=days - 1), last=last, days=days)
