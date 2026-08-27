"""Reading and rewriting funds.yaml without ever losing a human's edit.

This file is shared by a person and a robot, which creates two distinct hazards.

**Truncation.** A crash mid-write leaves invalid YAML. Solved by writing a
temporary file in the same directory and renaming it into place, so the
replacement is atomic.

**The lost update.** Atomic writing does not help here at all: the robot loads
the file, the user edits and saves it, the robot then renames its own copy over
the top and the edit is gone. So before renaming, the on-disk content is hashed
and compared with what was loaded. If it changed, the write is abandoned, the
user's version stays, and a visible conflict is recorded. `mtime` is not enough
-- it has coarse resolution and can be identical across a fast edit.

Two smaller rules that both cost data when broken:

- Comments and key order survive the rewrite, which is why this uses ruamel's
  round-trip mode rather than a plain YAML dump.
- CNPJ is a string at every step. Unquoted, `08431747000106` parses as an
  integer and loses its leading zero.
"""

from __future__ import annotations

import hashlib
import logging
import os
import tempfile
from io import StringIO
from pathlib import Path

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq
from ruamel.yaml.error import YAMLError
from ruamel.yaml.scalarstring import DoubleQuotedScalarString as Quoted

from ..clock import to_dir_name, today
from ..errors import ConfigError, YamlConflictError
from .cnpj import format_masked, normalize
from .models import Entity, ExpansionState, Scope, ScopeMode

log = logging.getLogger(__name__)

ROOT_KEY = "scopes"

_TEMPLATE = """\
# Monitored scopes for fii-docs-watcher.
#
# Add a fund by writing its CNPJ and nothing else -- the robot fills in the rest
# on the next run and writes it back here so you can inspect what it resolved:
#
#   scopes:
#     - cnpj: "08.431.747/0001-06"
#
# Yours to edit:   cnpj, scope, ticker
# The robot's:     everything else. It is a cache and gets overwritten.
#
# Editing by hand accepts a CNPJ only. The CLI (`fii-docs-watcher add`) also
# accepts a partial name, because it can ask you which fund you meant; this file
# is read unattended and FII names collide too often to guess.
#
# Always quote the CNPJ. Unquoted, YAML reads it as a number and eats the
# leading zero.

scopes: []
"""


def _yaml() -> YAML:
    yaml = YAML()
    yaml.preserve_quotes = True
    yaml.width = 4096  # Never wrap: a folded legal name is painful to read and edit.
    yaml.indent(mapping=2, sequence=4, offset=2)
    return yaml


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _stage_write(path: Path, data: bytes) -> Path:
    """Write and sync a unique same-directory temporary, returning its path."""
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False
        ) as handle:
            temporary = Path(handle.name)
            os.fchmod(handle.fileno(), 0o644)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        return temporary
    except BaseException:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        raise


def _fsync_directory(path: Path) -> None:
    """Make a completed rename durable, not merely atomic to concurrent readers."""
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_atomic(path: Path, data: bytes) -> None:
    temporary = _stage_write(path, data)
    try:
        temporary.replace(path)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


class FundsFile:
    """A loaded funds.yaml, remembering exactly what it was loaded from."""

    def __init__(self, path: Path, document: CommentedMap, source_digest: str | None) -> None:
        self.path = path
        self._document = document
        self._source_digest = source_digest

    # ------------------------------------------------------------------ loading

    @classmethod
    def load(cls, path: Path) -> FundsFile:
        """Load the file, creating a commented template if it does not exist."""
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(_TEMPLATE, encoding="utf-8")
            log.info("created a new funds file", extra={"path": str(path)})

        raw = path.read_bytes()
        try:
            document = _yaml().load(raw.decode("utf-8")) or CommentedMap()
        except (YAMLError, UnicodeDecodeError) as exc:
            raise ConfigError(f"{path} is not valid YAML: {exc}") from exc

        if not isinstance(document, CommentedMap):
            raise ConfigError(f"{path} must contain a mapping at the top level")
        if ROOT_KEY not in document or document[ROOT_KEY] is None:
            document[ROOT_KEY] = CommentedSeq()
        if not isinstance(document[ROOT_KEY], CommentedSeq):
            raise ConfigError(f"{path}: '{ROOT_KEY}' must be a list")

        return cls(path, document, _digest(raw))

    # ------------------------------------------------------------------ scopes

    def scopes(self) -> list[Scope]:
        """Parse the entries into `Scope` objects, skipping unusable ones.

        An entry with no CNPJ is reported and skipped rather than fatal: one bad
        line must not stop the other funds from being monitored.
        """
        parsed: list[Scope] = []
        for index, entry in enumerate(self._document[ROOT_KEY]):
            if not isinstance(entry, dict):
                log.error(
                    "skipping a scope entry that is not a mapping",
                    extra={"index": index, "path": str(self.path)},
                )
                continue
            cnpj_raw = entry.get("cnpj")
            if normalize(cnpj_raw) is None:
                log.error(
                    "skipping a scope entry with a missing or unusable cnpj; "
                    "hand-edited entries must carry a CNPJ",
                    extra={"index": index, "value": repr(cnpj_raw), "path": str(self.path)},
                )
                continue
            parsed.append(_scope_from_entry(entry))
        return parsed

    def _entry_for(self, scope: Scope) -> CommentedMap | None:
        target = scope.normalized_cnpj
        for entry in self._document[ROOT_KEY]:
            if isinstance(entry, dict) and normalize(entry.get("cnpj")) == target:
                return entry
        return None

    def add_scope(self, scope: Scope) -> bool:
        """Append a scope, writing only the fields the user owns.

        Deliberately does not write the resolved fields: leaving them to
        `update_scope` keeps the user's own keys at the top of the entry, where
        they are the first thing a reader sees, instead of buried after the
        machine-generated entity list.
        """
        if self._entry_for(scope) is not None:
            return False
        entry = CommentedMap()
        entry["cnpj"] = Quoted(scope.cnpj)
        entry["scope"] = scope.mode.value
        if scope.ticker:
            # Supplied on the command line. The robot never invents or validates
            # a ticker, but it must not throw away one the user gave it either.
            entry["ticker"] = Quoted(scope.ticker)
        self._document[ROOT_KEY].append(entry)
        return True

    def remove_scope(self, scope: Scope) -> bool:
        """Drop a scope's entry entirely. True if one was found and removed.

        Matched on the normalised CNPJ, so the punctuation the user happened to
        type is irrelevant. The previous file is kept as `funds.yaml.bak` by the
        usual save path, which is the undo for this.
        """
        target = scope.normalized_cnpj
        entries = self._document[ROOT_KEY]
        for index, entry in enumerate(entries):
            if isinstance(entry, dict) and normalize(entry.get("cnpj")) == target:
                del entries[index]
                return True
        return False

    def update_user_fields(self, scope: Scope) -> bool:
        """Apply the fields the user owns to an existing entry. True if anything changed.

        Exists so that re-running `add` on a CNPJ that is already registered can
        still attach a ticker, instead of reporting "already registered" and
        silently discarding what was asked for.
        """
        entry = self._entry_for(scope)
        if entry is None:
            return False
        changed = False

        current = str(entry.get("ticker") or "")
        wanted = scope.ticker or ""
        if wanted != current:
            if wanted:
                entry["ticker"] = Quoted(wanted)
            elif "ticker" in entry:
                # Clearing removes the key rather than writing an empty string,
                # so the file keeps looking like something a person wrote.
                del entry["ticker"]
            changed = True

        if str(entry.get("scope") or "") != scope.mode.value:
            entry["scope"] = scope.mode.value
            changed = True
        return changed

    def update_scope(self, scope: Scope) -> None:
        """Write a scope's resolved fields back, leaving the user's fields alone."""
        entry = self._entry_for(scope)
        if entry is None:
            self.add_scope(scope)
            entry = self._entry_for(scope)
            if entry is None:  # pragma: no cover - add_scope just created it
                return
        _apply_scope(entry, scope)

    # ------------------------------------------------------------------ writing

    def render(self) -> str:
        buffer = StringIO()
        _yaml().dump(self._document, buffer)
        return buffer.getvalue()

    def save(self, *, backup: Path | None = None) -> None:
        """Write the file back atomically, refusing to clobber a concurrent edit.

        Raises `YamlConflictError` when the file changed on disk since it was
        loaded. The caller reports it; the user's version stays untouched.
        """
        current: bytes | None = None
        if self.path.exists():
            current = self.path.read_bytes()
        current_digest = _digest(current) if current is not None else None

        if current_digest != self._source_digest:
            raise YamlConflictError(
                f"{self.path} changed on disk since it was loaded; keeping your edit and "
                "discarding the robot's update. It will be reapplied on the next run.",
                context={"path": str(self.path)},
            )

        rendered = self.render().encode("utf-8")

        # Keep the last good version. Cheap insurance on a file a human maintains.
        if backup is not None and current is not None:
            _write_atomic(backup, current)

        # Prepare everything before the final digest check, leaving the smallest
        # practical interval between checking the human's file and replacing it.
        temporary = _stage_write(self.path, rendered)
        try:
            latest = self.path.read_bytes() if self.path.exists() else None
            latest_digest = _digest(latest) if latest is not None else None
            if latest_digest != self._source_digest:
                raise YamlConflictError(
                    f"{self.path} changed on disk while its update was prepared; keeping your "
                    "edit and discarding the robot's update. It will be reapplied on the next run.",
                    context={"path": str(self.path)},
                )
            temporary.replace(self.path)
            _fsync_directory(self.path.parent)
        finally:
            temporary.unlink(missing_ok=True)

        self._source_digest = _digest(rendered)
        log.debug("funds file written", extra={"path": str(self.path)})


def _scope_from_entry(entry: dict) -> Scope:
    raw_mode = str(entry.get("scope") or ScopeMode.FUND_AND_CLASSES.value).strip()
    try:
        mode = ScopeMode(raw_mode)
    except ValueError:
        log.error(
            "unknown scope mode; falling back to fund_and_classes",
            extra={"value": raw_mode, "cnpj": str(entry.get("cnpj"))},
        )
        mode = ScopeMode.FUND_AND_CLASSES

    raw_expansion = str(entry.get("expansion") or ExpansionState.UNRESOLVED.value).strip()
    try:
        expansion = ExpansionState(raw_expansion)
    except ValueError:
        expansion = ExpansionState.UNRESOLVED

    entities: list[Entity] = []
    for raw in entry.get("entities") or []:
        if not isinstance(raw, dict):
            continue
        cnpj = normalize(raw.get("cnpj"))
        fundosnet_id = raw.get("fundosnet_id")
        if cnpj is None or fundosnet_id is None:
            log.error(
                "skipping an entity with no CNPJ or no fundosnet_id",
                extra={"entity": dict(raw), "cnpj": str(entry.get("cnpj"))},
            )
            continue
        try:
            resolved_id = int(fundosnet_id)
        except (TypeError, ValueError):
            log.error(
                "skipping an entity whose fundosnet_id is not an integer",
                extra={"value": repr(fundosnet_id)},
            )
            continue
        confirmed = _opt_bool(raw.get("cnpj_confirmed"), default=False)
        entities.append(
            Entity(
                cnpj=str(raw.get("cnpj")),
                fundosnet_id=resolved_id,
                fnet_fund_description=str(raw.get("fnet_fund_description") or ""),
                kind=str(raw.get("kind") or "fund_or_class"),
                fnet_fund_type=_opt_int(raw.get("fnet_fund_type"), default=1),
                validated_at=_opt_str(raw.get("validated_at")) if confirmed else None,
                cnpj_confirmed=confirmed,
            )
        )

    return Scope(
        cnpj=str(entry.get("cnpj")),
        mode=mode,
        ticker=_opt_str(entry.get("ticker")),
        legal_name=_opt_str(entry.get("legal_name")),
        cvm_code=_opt_str(entry.get("cvm_code")),
        cvm_status=_opt_str(entry.get("cvm_status")),
        registered_at=_opt_str(entry.get("registered_at")),
        expansion=expansion,
        entities=entities,
    )


def _opt_str(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _opt_int(value: object, *, default: int) -> int:
    """Read an optional integer, falling back rather than refusing the entity.

    A watch list written before the field existed simply has no value, and a
    hand-edited one may have nonsense. Neither is worth dropping an otherwise
    resolved entity over: the fallback is the value that was implicit before.
    """
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        log.warning(
            "entity has a non-integer fnet_fund_type; using the default",
            extra={"value": repr(value), "default": default},
        )
        return default


def _opt_bool(value: object, *, default: bool) -> bool:
    """Read a YAML boolean without treating every non-empty string as true."""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "yes", "1"}:
            return True
        if normalized in {"false", "no", "0", ""}:
            return False
    log.warning(
        "entity has an invalid cnpj_confirmed value; using the default",
        extra={"value": repr(value), "default": default},
    )
    return default


def _apply_scope(entry: CommentedMap, scope: Scope) -> None:
    """Write the robot-owned fields into an existing entry, in a stable order.

    `cnpj` and `ticker` are left exactly as the user wrote them: the reference
    CNPJ's formatting is theirs, and the ticker is an annotation the robot never
    fills in or validates -- there is no native source for a B3 ticker in either
    CVM or Fundos.NET data.
    """
    entry["scope"] = scope.mode.value
    if scope.legal_name:
        entry["legal_name"] = scope.legal_name
    if scope.cvm_code:
        entry["cvm_code"] = Quoted(scope.cvm_code)
    if scope.cvm_status:
        entry["cvm_status"] = scope.cvm_status
    entry["registered_at"] = Quoted(scope.registered_at or to_dir_name(today()))
    entry["expansion"] = scope.expansion.value

    entities = CommentedSeq()
    for entity in scope.entities:
        item = CommentedMap()
        item["kind"] = entity.kind
        # Freshly written CNPJs get the readable mask; a quoted scalar keeps the
        # leading zero safe through the next load.
        item["cnpj"] = Quoted(format_masked(entity.cnpj) or entity.cnpj)
        item["fundosnet_id"] = entity.fundosnet_id
        item["fnet_fund_type"] = entity.fnet_fund_type
        item["fnet_fund_description"] = entity.fnet_fund_description
        if entity.cnpj_confirmed and entity.validated_at:
            item["validated_at"] = Quoted(entity.validated_at)
        item["cnpj_confirmed"] = entity.cnpj_confirmed
        entities.append(item)
    entry["entities"] = entities
