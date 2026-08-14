"""Exception taxonomy, mapped onto the operational severity ladder.

The spec's ladder (section 8) distinguishes three things a human needs to tell
apart at a glance:

    WARNING   transient, a retry is expected to fix it
    ERROR     invalid configuration or a skipped scope/entity; needs a human
    CRITICAL  the Fundos.NET contract probably changed, or a CNPJ diverged;
              needs a human immediately

Every exception below carries its own severity so that call sites log the right
level without re-deciding it, and so that an isolated failure can be recorded
and skipped without the batch ever inspecting the exception's concrete type.
"""

from __future__ import annotations

import logging


class WatcherError(Exception):
    """Base class. `severity` is the logging level this failure should be reported at."""

    severity: int = logging.ERROR

    def __init__(self, message: str, *, context: dict[str, object] | None = None) -> None:
        super().__init__(message)
        self.context = context or {}


class ConfigError(WatcherError):
    """Invalid or unusable configuration. Fatal for the run, not for one entity."""

    severity = logging.ERROR


class LockHeldError(WatcherError):
    """Another instance is already running against the same state."""

    severity = logging.ERROR


class TransientSourceError(WatcherError):
    """Timeout, connection reset, 5xx. Retryable; expected occasionally from this source."""

    severity = logging.WARNING


class SourceContractError(WatcherError):
    """The response no longer matches what section 2 verified.

    Missing critical field, unparseable date, a page-length ceiling that moved.
    This is the loudest class of failure short of a CNPJ divergence: it means
    the assumptions this whole pipeline rests on need re-verifying.
    """

    severity = logging.CRITICAL


class CoverageError(WatcherError):
    """A paginated scan could not prove it saw every row.

    Distinct-id coverage disagreed with `recordsFiltered`. Never downgrade this
    to a warning silently: unstable ordering on this endpoint drops ~19% of rows
    while the row count still adds up, so this is the only guard that catches it.
    """

    severity = logging.WARNING


class ContentValidationError(WatcherError):
    """The downloaded bytes are not the document they claim to be.

    Covers an HTML error page served with HTTP 200, a well-formed XML whose root
    is `html`, a truncated PDF, or a body that matches no known signature.
    """

    severity = logging.ERROR


class CnpjDivergenceError(WatcherError):
    """The CNPJ in Content-Disposition matches no entity in the scope.

    The name -> id_fundosnet resolution is textual; this is the check that closes
    the circuit. A confirmed divergence means documents may be filed under the
    wrong fund, so the resolution is not consolidated.
    """

    severity = logging.CRITICAL


class ScopeResolutionError(WatcherError):
    """A scope could not be resolved into at least one entity. That scope is skipped."""

    severity = logging.ERROR


class YamlConflictError(WatcherError):
    """funds.yaml changed on disk since it was loaded; a human edit would be lost."""

    severity = logging.ERROR
