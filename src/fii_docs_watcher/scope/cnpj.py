"""CNPJ handling: normalise for comparison, preserve what the human typed.

Three sources spell the same CNPJ three ways -- the user writes
`08.431.747/0001-06`, the CVM registry stores `08431747000106`, and Fundos.NET
embeds it in a served filename. Comparing any of those as raw strings produces
false mismatches, so every comparison in this codebase goes through
`normalize()` while the YAML keeps the user's own formatting untouched.
"""

from __future__ import annotations

import re

_NON_DIGITS = re.compile(r"\D")
CNPJ_LENGTH = 14

# Weights for the two check digits, applied right-to-left over the base digits.
_FIRST_WEIGHTS = (5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2)
_SECOND_WEIGHTS = (6, *_FIRST_WEIGHTS)


def normalize(value: str | int | None) -> str | None:
    """Reduce any spelling of a CNPJ to its 14 digits, or None if that is impossible.

    Integers are accepted because a YAML file written without quotes yields one --
    and, having lost its leading zero, it is re-padded here rather than silently
    compared as a shorter string.
    """
    if value is None:
        return None
    digits = _NON_DIGITS.sub("", str(value))
    if not digits:
        return None
    if len(digits) < CNPJ_LENGTH:
        digits = digits.zfill(CNPJ_LENGTH)
    if len(digits) != CNPJ_LENGTH:
        return None
    return digits


def is_valid(value: str | int | None) -> bool:
    """Check the two verification digits.

    Used to catch typos at registration time. Discovery never rejects a document
    over this: the CNPJ arrives from the source and is not ours to second-guess.
    """
    digits = normalize(value)
    if digits is None:
        return False
    if digits == digits[0] * CNPJ_LENGTH:
        return False  # Repunits pass the arithmetic but are not real CNPJs.

    def check_digit(base: str, weights: tuple[int, ...]) -> str:
        total = sum(int(digit) * weight for digit, weight in zip(base, weights, strict=True))
        remainder = total % 11
        return "0" if remainder < 2 else str(11 - remainder)

    first = check_digit(digits[:12], _FIRST_WEIGHTS)
    second = check_digit(digits[:12] + first, _SECOND_WEIGHTS)
    return digits[12:] == first + second


def format_masked(value: str | int | None) -> str | None:
    """Render as `NN.NNN.NNN/NNNN-NN`, for logs and for freshly written entries."""
    digits = normalize(value)
    if digits is None:
        return None
    return f"{digits[:2]}.{digits[2:5]}.{digits[5:8]}/{digits[8:12]}-{digits[12:]}"


def same(left: str | int | None, right: str | int | None) -> bool:
    """Compare two CNPJs from different sources. Never compare them as raw strings."""
    normalized_left = normalize(left)
    return normalized_left is not None and normalized_left == normalize(right)
