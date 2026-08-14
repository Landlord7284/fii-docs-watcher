"""Text folding, shared by everything that has to match a fund name.

Three sources spell the same fund three ways: the CVM registry, Fundos.NET's
`descricaoFundo`, and whatever a human types at the command line. Accents,
double spaces, stray hyphens and case all vary between them, so raw equality is
useless and every comparison goes through `fold_name` first.

This lives in its own module rather than beside any one caller so that the CVM
registry, the resolver and the scope model can all use it without importing one
another.
"""

from __future__ import annotations

import re
import unicodedata

_WHITESPACE = re.compile(r"\s+")
_PUNCTUATION = re.compile(r"[^\w\s]")


def fold_name(value: str) -> str:
    """Fold a name for comparison: no accents, no punctuation, no case.

    Accents are stripped rather than dropped, so `IMOBILIÁRIO` folds to
    `IMOBILIARIO` and still matches what someone types without the accent.
    """
    stripped = unicodedata.normalize("NFKD", value or "")
    stripped = "".join(ch for ch in stripped if not unicodedata.combining(ch))
    stripped = _PUNCTUATION.sub(" ", stripped)
    return _WHITESPACE.sub(" ", stripped).strip().upper()
