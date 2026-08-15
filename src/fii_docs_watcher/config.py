"""Configuration: a TOML file, overridable by environment variables.

Portability rule from the spec: running the program once from a shell with a
config file has to work, with no orchestrator, no baked-in path, no CNPJ and no
credential anywhere in the code. Everything the robot needs to find is declared
here, and every value can be overridden by `FII_WATCHER_<SECTION>_<KEY>` so a
container can be configured without rewriting the file.

The file is discovered rather than demanded, so the common case needs no flag.
The one thing that must never happen quietly is falling back to the built-in
defaults: those point at `./var/...`, and a user whose real config names
different roots would otherwise operate on an entirely different archive without
being told. So a fallback is announced, and `Config.source_path` records what
was actually read.
"""

from __future__ import annotations

import logging
import os
import tomllib
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any

from .clock import DEFAULT_TIMEZONE, set_timezone
from .errors import ConfigError

log = logging.getLogger(__name__)

ENV_PREFIX = "FII_WATCHER_"

# Environment variable naming the config file outright, for containers and cron.
ENV_CONFIG_PATH = f"{ENV_PREFIX}CONFIG"

# Searched in order when no path is given. Relative entries are resolved against
# the working directory, which is why the project-local names come first: running
# from a checkout should pick up that checkout's configuration.
CONFIG_SEARCH_PATH = (
    Path("config.toml"),
    Path("fii-docs-watcher.toml"),
    Path.home() / ".config" / "fii-docs-watcher" / "config.toml",
)

# Verified ceiling: l=200 is honoured; l>=250 returns HTTP 500 even when the
# requests are spaced generously, so this is a real server limit rather than
# rate limiting. Requesting more fails loudly, so clamping here is a courtesy.
MAX_PAGE_LENGTH = 200

# `downloadDocumento` serves exactly these two, from the same endpoint.
SUPPORTED_FORMATS = frozenset({"pdf", "xml"})


@dataclass(frozen=True)
class PathsConfig:
    data_root: Path = Path("./var/data")
    documents_root: Path = Path("./var/documents")

    @property
    def funds_file(self) -> Path:
        return self.data_root / "funds.yaml"

    @property
    def funds_backup(self) -> Path:
        return self.data_root / "funds.yaml.bak"

    @property
    def manifest_file(self) -> Path:
        return self.data_root / "manifest.sqlite"

    @property
    def lock_file(self) -> Path:
        return self.data_root / "watcher.lock"

    @property
    def cvm_cache(self) -> Path:
        return self.data_root / "cvm-cache"

    @property
    def tmp_dir(self) -> Path:
        """Download staging.

        Deliberately inside the documents root: `rename` is only atomic within a
        single filesystem, and the documents root may well be a different mount
        from the data root.
        """
        return self.documents_root / ".tmp"

    @property
    def inbox_dir(self) -> Path:
        return self.documents_root / "_inbox"


@dataclass(frozen=True)
class RetentionConfig:
    days: int = 7


@dataclass(frozen=True)
class SourceConfig:
    base_url: str = "https://fnet.bmfbovespa.com.br/fnet/publico"
    user_agent: str = "fii-docs-watcher/0.2 (+https://github.com/Landlord7284/fii-docs-watcher)"
    # The timezone the source publishes in, never the host's. It belongs to
    # [source] rather than to a settings-of-taste section because that is what
    # it is: changing it re-dates the whole archive. See config.example.toml.
    timezone: str = DEFAULT_TIMEZONE
    # Responses arrive either in ~0.3s or in ~60.3s. A 30s timeout would fail
    # about half of all requests; see config.example.toml.
    read_timeout_seconds: float = 120.0
    connect_timeout_seconds: float = 15.0
    page_length: int = MAX_PAGE_LENGTH
    min_request_interval_seconds: float = 1.5
    max_retries: int = 3
    backoff_base_seconds: float = 2.0
    backoff_max_seconds: float = 30.0
    max_response_bytes: int = 100 * 1024 * 1024


@dataclass(frozen=True)
class CvmConfig:
    registry_url: str = "https://dados.cvm.gov.br/dados/FI/CAD/DADOS/registro_fundo_classe.zip"
    refresh_interval_hours: int = 24
    read_timeout_seconds: float = 180.0


@dataclass(frozen=True)
class AuditConfig:
    frequency: str = "daily"  # daily | weekly | never


@dataclass(frozen=True)
class DownloadConfig:
    stale_part_hours: int = 6

    # Which content formats are worth keeping. PDF only by default: this is a
    # reading queue for people, and the XML is a machine-readable duplicate of
    # the same filing that Pipeline B fetches for itself. Adding "xml" here
    # archives both. The decision is made before the request wherever the
    # listing allows the format to be predicted, so a declined format usually
    # costs no bandwidth at all.
    formats: tuple[str, ...] = ("pdf",)

    def wants(self, extension: str) -> bool:
        return extension.lower().lstrip(".") in self.formats

    @property
    def all_formats(self) -> bool:
        return set(self.formats) >= SUPPORTED_FORMATS


@dataclass(frozen=True)
class FilesConfig:
    directory_mode: int = 0o755
    file_mode: int = 0o644


@dataclass(frozen=True)
class LoggingConfig:
    level: str = "INFO"
    format: str = "text"  # text | json
    file: Path | None = None


@dataclass(frozen=True)
class Config:
    paths: PathsConfig = field(default_factory=PathsConfig)
    retention: RetentionConfig = field(default_factory=RetentionConfig)
    source: SourceConfig = field(default_factory=SourceConfig)
    cvm: CvmConfig = field(default_factory=CvmConfig)
    audit: AuditConfig = field(default_factory=AuditConfig)
    download: DownloadConfig = field(default_factory=DownloadConfig)
    files: FilesConfig = field(default_factory=FilesConfig)
    # Where these settings came from, or None when the built-in defaults are in
    # use. Reported by `doctor` and at the start of a run so that "which archive
    # am I operating on?" is never a guess.
    source_path: Path | None = None
    logging: LoggingConfig = field(default_factory=LoggingConfig)


_SECTIONS: dict[str, type] = {
    "paths": PathsConfig,
    "retention": RetentionConfig,
    "source": SourceConfig,
    "cvm": CvmConfig,
    "audit": AuditConfig,
    "download": DownloadConfig,
    "files": FilesConfig,
    "logging": LoggingConfig,
}


def _coerce(value: Any, annotation: Any, where: str) -> Any:
    """Coerce a raw TOML/env value to the field's declared type."""
    optional = False
    if isinstance(annotation, str):
        # Dataclass annotations are strings under `from __future__ import annotations`.
        optional = annotation.endswith("| None")
        annotation = annotation.removesuffix(" | None").strip()
        if annotation.startswith("tuple["):
            # TOML gives a real array; an environment override can only give a
            # string, so a comma-separated list is accepted there.
            if isinstance(value, str):
                items = [part.strip() for part in value.split(",")]
            elif isinstance(value, (list, tuple)):
                items = [str(part).strip() for part in value]
            else:
                raise ConfigError(f"{where} must be a list of strings, got {value!r}")
            return tuple(item.lower() for item in items if item)
        annotation = {
            "Path": Path, "int": int, "float": float, "bool": bool, "str": str,
        }.get(annotation, str)
    if optional and (value is None or value == ""):
        return None
    try:
        if annotation is Path:
            return Path(str(value)).expanduser()
        if annotation is bool:
            if isinstance(value, bool):
                return value
            return str(value).strip().lower() in {"1", "true", "yes", "on"}
        if annotation is int:
            # Accept "0o755" and "755" for file modes, which TOML cannot express.
            if isinstance(value, str):
                text = value.strip()
                if text.lower().startswith("0o"):
                    return int(text, 8)
                return int(text, 10)
            return int(value)
        if annotation is float:
            return float(value)
        return str(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"invalid value for {where}: {value!r} ({exc})") from exc


def _env_overrides(section: str, keys: set[str]) -> dict[str, str]:
    """Collect `FII_WATCHER_<SECTION>_<KEY>` variables for one section."""
    out: dict[str, str] = {}
    for key in keys:
        env_name = f"{ENV_PREFIX}{section.upper()}_{key.upper()}"
        if env_name in os.environ:
            out[key] = os.environ[env_name]
    return out


def discover(path: Path | str | None = None) -> Path | None:
    """Resolve which config file to read, or None to use the built-in defaults.

    An explicitly requested file that does not exist is an error rather than a
    silent fallback: the user named it, so failing to find it is a mistake worth
    reporting, not a cue to use different settings.
    """
    if path is not None:
        candidate = Path(path).expanduser()
        if not candidate.is_file():
            raise ConfigError(f"config file not found: {candidate}")
        return candidate

    from_env = os.environ.get(ENV_CONFIG_PATH)
    if from_env:
        candidate = Path(from_env).expanduser()
        if not candidate.is_file():
            raise ConfigError(f"{ENV_CONFIG_PATH} points at a missing file: {candidate}")
        return candidate

    for candidate in CONFIG_SEARCH_PATH:
        resolved = candidate.expanduser()
        if resolved.is_file():
            return resolved
    return None


def load(path: Path | str | None = None) -> Config:
    """Load configuration, discovering the file when no path is given.

    Falling back to the built-in defaults is legitimate but never silent: those
    defaults point at `./var/...`, so a user with a real config elsewhere would
    otherwise be operating on a different archive with no indication.
    """
    raw: dict[str, Any] = {}
    config_path = discover(path)
    if config_path is not None:
        try:
            with config_path.open("rb") as fh:
                raw = tomllib.load(fh)
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(f"invalid TOML in {config_path}: {exc}") from exc
        except OSError as exc:
            raise ConfigError(f"could not read {config_path}: {exc}") from exc

    sections: dict[str, Any] = {}
    for name, cls in _SECTIONS.items():
        assert is_dataclass(cls)
        declared = {f.name: f for f in fields(cls)}
        provided = dict(raw.get(name) or {})
        provided.update(_env_overrides(name, set(declared)))

        unknown = set(provided) - set(declared)
        if unknown:
            raise ConfigError(f"unknown key(s) in [{name}]: {', '.join(sorted(unknown))}")

        kwargs = {
            key: _coerce(value, declared[key].type, f"[{name}].{key}")
            for key, value in provided.items()
        }
        sections[name] = cls(**kwargs)

    config = Config(**sections, source_path=config_path)
    _validate(config)
    # Installed here, and only here, so that no entry point can compute a date
    # under the default while the configuration names a different zone. It is
    # process-wide by nature: the archive's directory names depend on it.
    set_timezone(config.source.timezone)
    # Announcing a defaults-only load is left to the caller, which configures
    # logging immediately after this returns; emitting it here would go out
    # through the last-resort handler, unformatted and unfiltered.
    return config


def describe_source(config: Config) -> str:
    """One line naming where the settings came from, for logs and `doctor`."""
    if config.source_path is not None:
        return str(config.source_path)
    return (
        "built-in defaults (no config file found; searched "
        + ", ".join(str(p) for p in CONFIG_SEARCH_PATH)
        + ")"
    )


def _validate(config: Config) -> None:
    """Reject configurations that cannot work, before any side effect happens."""
    if config.retention.days < 1:
        raise ConfigError(f"[retention].days must be >= 1, got {config.retention.days}")
    if config.source.page_length < 1:
        raise ConfigError(f"[source].page_length must be >= 1, got {config.source.page_length}")
    if config.source.page_length > MAX_PAGE_LENGTH:
        raise ConfigError(
            f"[source].page_length is {config.source.page_length}, but the source returns "
            f"HTTP 500 above {MAX_PAGE_LENGTH}"
        )
    if not config.download.formats:
        raise ConfigError(
            "[download].formats is empty, which would archive nothing; list at least one of "
            f"{', '.join(sorted(SUPPORTED_FORMATS))}"
        )
    unsupported = set(config.download.formats) - SUPPORTED_FORMATS
    if unsupported:
        raise ConfigError(
            f"[download].formats contains {', '.join(sorted(unsupported))}; this source serves "
            f"only {', '.join(sorted(SUPPORTED_FORMATS))}"
        )
    if config.audit.frequency not in {"daily", "weekly", "never"}:
        raise ConfigError(
            f"[audit].frequency must be daily, weekly or never, got {config.audit.frequency!r}"
        )
    if config.logging.format not in {"text", "json"}:
        raise ConfigError(f"[logging].format must be text or json, got {config.logging.format!r}")
    if config.source.max_retries < 0:
        raise ConfigError("[source].max_retries must be >= 0")

    data_root = config.paths.data_root.resolve()
    documents_root = config.paths.documents_root.resolve()
    if data_root == documents_root:
        raise ConfigError(
            "[paths].data_root and [paths].documents_root must differ: the documents root is "
            "meant to be shared and the data root must never be"
        )
    if documents_root.is_relative_to(data_root) or data_root.is_relative_to(documents_root):
        raise ConfigError(
            f"[paths].data_root ({data_root}) and [paths].documents_root ({documents_root}) "
            "must not be nested inside one another"
        )
