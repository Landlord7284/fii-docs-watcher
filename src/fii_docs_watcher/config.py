"""Configuration: a TOML file, overridable by environment variables.

Portability rule from the spec: running the program once from a shell with a
config file has to work, with no orchestrator, no baked-in path, no CNPJ and no
credential anywhere in the code. Everything the robot needs to find is declared
here, and every value can be overridden by `FII_WATCHER_<SECTION>_<KEY>` so a
container can be configured without rewriting the file.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any

from .errors import ConfigError

ENV_PREFIX = "FII_WATCHER_"

# Verified ceiling: l=200 is honoured; l>=250 returns HTTP 500 even when the
# requests are spaced generously, so this is a real server limit rather than
# rate limiting. Requesting more fails loudly, so clamping here is a courtesy.
MAX_PAGE_LENGTH = 200


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
    user_agent: str = "fii-docs-watcher/0.1 (+https://github.com/marcowb/fii-docs-watcher)"
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


def load(path: Path | str | None = None) -> Config:
    """Load configuration from `path`, then apply environment overrides.

    A missing file is only an error when it was explicitly requested; with no
    path at all the defaults plus environment are a legitimate configuration.
    """
    raw: dict[str, Any] = {}
    if path is not None:
        config_path = Path(path).expanduser()
        if not config_path.is_file():
            raise ConfigError(f"config file not found: {config_path}")
        try:
            with config_path.open("rb") as fh:
                raw = tomllib.load(fh)
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(f"invalid TOML in {config_path}: {exc}") from exc

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

    config = Config(**sections)
    _validate(config)
    return config


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
