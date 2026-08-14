"""Building the filename a human will actually read.

    {entity_prefix}_{category}_{species}_{id}_V{version}.{ext}

Two rules that look like details and are not.

**The name carries no mutable document field.** `status`, `modalidade` and
`situacao` change at the source after delivery. Putting any of them in the name
would mean either renaming files whenever the source changes its mind, or
letting the name quietly become a lie. They live in the manifest.

**The version is part of the name.** A re-filing can keep the same id, so
without it v2 would overwrite v1 -- destroying precisely the history the archive
exists to keep.

The prefix is a *snapshot*, not an identity. A ticker is a user annotation and a
legal name can change, so a file keeps whatever prefix was true when it was
downloaded and is never renamed afterwards. Identity lives in the manifest and in
the `(id, version)` pair inside the name itself.
"""

from __future__ import annotations

import re
import unicodedata

# Reserved on Windows, and the archive is meant to be read over SMB.
_WINDOWS_RESERVED = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)

_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_SEPARATORS = re.compile(r"[\s_]+")
_REPEATED_DASH = re.compile(r"-{2,}")

# Long enough to stay readable, short enough that the whole path survives a
# 255-byte filesystem limit once a date directory and a share prefix are added.
MAX_COMPONENT = 48


def sanitize(value: str, *, max_length: int = MAX_COMPONENT) -> str:
    """Reduce arbitrary source text to a safe, readable filename component.

    The source fields carry accents, slashes, parentheses and trailing spaces,
    all of which either break a filesystem somewhere or make a directory listing
    unreadable. Accents are folded rather than dropped, so `Informações` becomes
    `Informacoes` instead of `Informaes`.
    """
    if not value:
        return ""
    decomposed = unicodedata.normalize("NFKD", value)
    folded = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    cleaned = _UNSAFE.sub("-", folded)
    cleaned = _SEPARATORS.sub("-", cleaned.strip())
    cleaned = _REPEATED_DASH.sub("-", cleaned).strip("-.")
    if len(cleaned) > max_length:
        cleaned = cleaned[:max_length].rstrip("-.")
    if cleaned.upper() in _WINDOWS_RESERVED:
        cleaned = f"{cleaned}-doc"
    return cleaned


def entity_prefix(*, ticker: str | None, fund_description: str, cnpj: str | None) -> str:
    """Pick the leading component, in descending order of usefulness to a reader.

    A ticker is what a person recognises; failing that a short form of the legal
    name; failing that the CNPJ, which is always available and never ambiguous.
    """
    if ticker and ticker.strip():
        return sanitize(ticker, max_length=16)

    if fund_description and fund_description.strip():
        # Legal names are long and end in boilerplate ("FUNDO DE INVESTIMENTO
        # IMOBILIÁRIO DE RESPONSABILIDADE LIMITADA") that is identical across
        # funds, so the distinguishing part is at the front.
        head = re.split(
            r"\bFUNDO\s+DE\s+INVESTIMENTO\b|\bFII\b",
            fund_description.strip(),
            maxsplit=1,
            flags=re.IGNORECASE,
        )[0]
        candidate = sanitize(head or fund_description, max_length=28)
        if candidate:
            return candidate

    digits = re.sub(r"\D", "", cnpj or "")
    return digits or "unknown"


def document_filename(
    *,
    prefix: str,
    category: str,
    species: str,
    document_id: int,
    version: int,
    extension: str,
) -> str:
    """Assemble the stored filename.

    `species` is included when present because assembly documents from the same
    fund on the same day differ only by it -- without it, two genuinely different
    filings would be told apart only by their numeric id.
    """
    parts = [
        sanitize(prefix, max_length=28) or "unknown",
        sanitize(category, max_length=32) or "Documento",
    ]
    species_part = sanitize(species, max_length=32)
    if species_part and species_part.lower() != parts[1].lower():
        parts.append(species_part)
    parts.append(str(document_id))
    parts.append(f"V{version:02d}")
    return f"{'_'.join(parts)}.{extension.lstrip('.')}"


def part_filename(document_id: int, version: int) -> str:
    """Name of the staging file used while a download is in flight."""
    return f"{document_id}_V{version:02d}.part"
