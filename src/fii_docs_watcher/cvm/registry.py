"""The CVM registry: CNPJ -> legal name, and the fund/class structure.

This dependency exists because the Fundos.NET listing never returns a CNPJ,
while the user registers a scope *by* CNPJ. Something has to bridge the two, and
`listarFundos` only searches by name.

The registry ships as one ZIP, refreshed daily:

    https://dados.cvm.gov.br/dados/FI/CAD/DADOS/registro_fundo_classe.zip
        registro_fundo.csv       CNPJ_Fundo, Codigo_CVM, Tipo_Fundo, Denominacao_Social, Situacao
        registro_classe.csv      ID_Registro_Fundo (FK), CNPJ_Classe, Denominacao_Social, Situacao
        registro_subclasse.csv   (unused here)

Both files are latin-1 and semicolon-delimited, and `registro_classe.csv`
carries `ID_Registro_Fundo`, which gives a *structural* fund-to-classes join
rather than a textual one.

**Availability of the CVM never stops monitoring.** A failed refresh blocks new
registrations and revalidation only; scopes already resolved keep running on the
last good snapshot. An incomplete or unparseable download never replaces a valid
snapshot -- it is discarded and the previous one stays in place.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import zipfile
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import httpx

from ..clock import SOURCE_TZ, timestamp
from ..config import CvmConfig
from ..errors import TransientSourceError
from ..scope.cnpj import normalize

log = logging.getLogger(__name__)

FUND_FILE = "registro_fundo.csv"
CLASS_FILE = "registro_classe.csv"
ENCODING = "latin-1"
DELIMITER = ";"

# `Tipo_Fundo` in registro_fundo.csv; `Tipo_Classe` in registro_classe.csv reads
# "Classes de Cotas de Fundos FII".
FII_FUND_TYPE = "FII"
FII_CLASS_MARKER = "FII"

# Situations that mean the entity is gone. Kept as a denylist because CVM adds
# wording over time and a new active-ish status should not silently disable a scope.
DEAD_SITUATIONS = frozenset({"cancelado", "liquidado"})

_STATE_FILE = "snapshot.json"


@dataclass(frozen=True)
class RegisteredFund:
    registry_id: str
    cnpj: str
    cvm_code: str
    legal_name: str
    situation: str
    fund_type: str


@dataclass(frozen=True)
class RegisteredClass:
    registry_id: str
    fund_registry_id: str
    cnpj: str
    cvm_code: str
    legal_name: str
    situation: str
    class_type: str


@dataclass(frozen=True)
class RegistryEntity:
    """A fund or a class, flattened into the one shape the resolver needs."""

    cnpj: str
    legal_name: str
    cvm_code: str
    situation: str
    kind: str  # "fund" | "class"

    @property
    def active(self) -> bool:
        return (self.situation or "").strip().lower() not in DEAD_SITUATIONS


class RegistrySnapshot:
    """An indexed, in-memory view of one downloaded registry.

    Held for the duration of a run: the spec is explicit that the registry is
    refreshed once per execution and used as a stable snapshot, rather than
    queried per scope.
    """

    def __init__(
        self,
        funds: list[RegisteredFund],
        classes: list[RegisteredClass],
        fetched_at: str,
    ) -> None:
        self.fetched_at = fetched_at
        self._funds_by_cnpj: dict[str, RegisteredFund] = {}
        self._funds_by_registry_id: dict[str, RegisteredFund] = {}
        self._classes_by_cnpj: dict[str, RegisteredClass] = {}
        self._classes_by_fund: dict[str, list[RegisteredClass]] = {}

        for fund in funds:
            self._funds_by_cnpj.setdefault(fund.cnpj, fund)
            self._funds_by_registry_id.setdefault(fund.registry_id, fund)
        for klass in classes:
            self._classes_by_cnpj.setdefault(klass.cnpj, klass)
            self._classes_by_fund.setdefault(klass.fund_registry_id, []).append(klass)

    def __len__(self) -> int:
        return len(self._funds_by_cnpj) + len(self._classes_by_cnpj)

    @property
    def fund_count(self) -> int:
        return len(self._funds_by_cnpj)

    @property
    def class_count(self) -> int:
        return len(self._classes_by_cnpj)

    def expand(self, cnpj: str) -> tuple[RegistryEntity | None, list[RegistryEntity]]:
        """Resolve a CNPJ to its anchor entity and the entities in its scope.

        The CNPJ may name a fund or a class, and the two cases differ:

        - a **fund** CNPJ yields the fund plus its active classes;
        - a **class** CNPJ yields only that class, because registering a class
          is an explicit request to monitor just that one.

        In a monoclass fund -- the shape of most listed FIIs -- the fund and its
        single class share a CNPJ. The class is preferred as the anchor there,
        since that is the entity the documents are actually filed under.
        """
        key = normalize(cnpj)
        if key is None:
            return None, []

        klass = self._classes_by_cnpj.get(key)
        fund = self._funds_by_cnpj.get(key)

        if fund is not None:
            # Deduplicated on (CNPJ, legal name): the registry contains at least
            # one fund whose classes are registered twice under the same CNPJ and
            # the same name. Left in, the duplicate would make the robot query
            # and archive the same entity twice.
            entities: list[RegistryEntity] = []
            seen: set[tuple[str, str]] = set()
            siblings = self._classes_by_fund.get(fund.registry_id, [])
            for sibling in siblings:
                entity = _class_entity(sibling)
                key_pair = (entity.cnpj, entity.legal_name)
                if entity.active and key_pair not in seen:
                    seen.add(key_pair)
                    entities.append(entity)

            anchor = _fund_entity(fund)
            # Monoclass: the fund and its only class share the CNPJ, so listing
            # both would query the same entity twice.
            if not any(entity.cnpj == anchor.cnpj for entity in entities):
                entities.insert(0, anchor)
            return anchor, entities

        if klass is not None:
            entity = _class_entity(klass)
            return entity, [entity]

        return None, []

    def lookup(self, cnpj: str) -> RegistryEntity | None:
        """Find one entity by CNPJ, whether it is registered as a fund or a class."""
        key = normalize(cnpj)
        if key is None:
            return None
        klass = self._classes_by_cnpj.get(key)
        if klass is not None:
            return _class_entity(klass)
        fund = self._funds_by_cnpj.get(key)
        return _fund_entity(fund) if fund is not None else None


def _fund_entity(fund: RegisteredFund) -> RegistryEntity:
    return RegistryEntity(
        cnpj=fund.cnpj,
        legal_name=fund.legal_name,
        cvm_code=fund.cvm_code,
        situation=fund.situation,
        kind="fund",
    )


def _class_entity(klass: RegisteredClass) -> RegistryEntity:
    return RegistryEntity(
        cnpj=klass.cnpj,
        legal_name=klass.legal_name,
        cvm_code=klass.cvm_code,
        situation=klass.situation,
        kind="class",
    )


def _read_csv(archive: zipfile.ZipFile, name: str) -> list[dict[str, str]]:
    with archive.open(name) as raw:
        text = io.TextIOWrapper(raw, encoding=ENCODING, newline="")
        return list(csv.DictReader(text, delimiter=DELIMITER))


def parse_archive(data: bytes) -> RegistrySnapshot:
    """Parse the registry ZIP into a snapshot, keeping only FII entities.

    Raises on a truncated or malformed archive so the caller can keep the
    previous snapshot instead of installing a broken one.
    """
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        names = set(archive.namelist())
        missing = {FUND_FILE, CLASS_FILE} - names
        if missing:
            raise ValueError(f"registry archive is missing {', '.join(sorted(missing))}")

        funds: list[RegisteredFund] = []
        for row in _read_csv(archive, FUND_FILE):
            if (row.get("Tipo_Fundo") or "").strip().upper() != FII_FUND_TYPE:
                continue
            cnpj = normalize(row.get("CNPJ_Fundo"))
            if cnpj is None:
                continue
            funds.append(
                RegisteredFund(
                    registry_id=(row.get("ID_Registro_Fundo") or "").strip(),
                    cnpj=cnpj,
                    cvm_code=(row.get("Codigo_CVM") or "").strip(),
                    legal_name=(row.get("Denominacao_Social") or "").strip(),
                    situation=(row.get("Situacao") or "").strip(),
                    fund_type=(row.get("Tipo_Fundo") or "").strip(),
                )
            )

        classes: list[RegisteredClass] = []
        for row in _read_csv(archive, CLASS_FILE):
            class_type = (row.get("Tipo_Classe") or "").strip()
            if FII_CLASS_MARKER not in class_type.upper():
                continue
            cnpj = normalize(row.get("CNPJ_Classe"))
            if cnpj is None:
                continue
            classes.append(
                RegisteredClass(
                    registry_id=(row.get("ID_Registro_Classe") or "").strip(),
                    fund_registry_id=(row.get("ID_Registro_Fundo") or "").strip(),
                    cnpj=cnpj,
                    cvm_code=(row.get("Codigo_CVM") or "").strip(),
                    legal_name=(row.get("Denominacao_Social") or "").strip(),
                    situation=(row.get("Situacao") or "").strip(),
                    class_type=class_type,
                )
            )

    if not funds and not classes:
        raise ValueError("registry archive contained no FII funds or classes")

    return RegistrySnapshot(funds, classes, fetched_at=timestamp())


class RegistryCache:
    """On-disk cache of the registry archive, with the last-known-good guarantee."""

    def __init__(self, config: CvmConfig, cache_dir: Path) -> None:
        self.config = config
        self.cache_dir = cache_dir
        self.archive_path = cache_dir / "registro_fundo_classe.zip"
        self.state_path = cache_dir / _STATE_FILE

    def _state(self) -> dict[str, str]:
        try:
            return json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def _is_fresh(self) -> bool:
        state = self._state()
        stamp = state.get("fetched_at")
        if not stamp or not self.archive_path.is_file():
            return False
        try:
            fetched = datetime.fromisoformat(stamp)
        except ValueError:
            return False
        if fetched.tzinfo is None:
            fetched = fetched.replace(tzinfo=SOURCE_TZ)
        age = datetime.now(SOURCE_TZ) - fetched
        return age < timedelta(hours=self.config.refresh_interval_hours)

    def _download(self, user_agent: str) -> bytes:
        log.info("downloading the CVM registry", extra={"url": self.config.registry_url})
        try:
            with httpx.Client(
                timeout=httpx.Timeout(self.config.read_timeout_seconds, connect=15.0),
                headers={"User-Agent": user_agent},
                follow_redirects=True,
            ) as client:
                response = client.get(self.config.registry_url)
                response.raise_for_status()
                return response.content
        except httpx.HTTPError as exc:
            raise TransientSourceError(
                f"could not download the CVM registry: {exc}",
                context={"url": self.config.registry_url},
            ) from exc

    def load(self, user_agent: str, *, force_refresh: bool = False) -> RegistrySnapshot | None:
        """Return the current snapshot, refreshing it if the cache is stale.

        Returns None only when there is no usable snapshot at all -- neither
        freshly downloaded nor cached. Callers treat that as "no new
        registrations this run", never as a reason to stop.
        """
        self.cache_dir.mkdir(parents=True, exist_ok=True)

        if not force_refresh and self._is_fresh():
            snapshot = self._load_cached()
            if snapshot is not None:
                log.debug(
                    "using the cached CVM registry",
                    extra={"fetched_at": snapshot.fetched_at, "entities": len(snapshot)},
                )
                return snapshot

        try:
            data = self._download(user_agent)
            snapshot = parse_archive(data)
        except (TransientSourceError, ValueError, zipfile.BadZipFile) as exc:
            log.warning(
                "CVM registry refresh failed; falling back to the last valid snapshot",
                extra={"error": str(exc)},
            )
            return self._load_cached()

        # Only replace the cached archive once the new one has parsed. An
        # invalid download must never displace a valid snapshot.
        tmp = self.archive_path.with_suffix(".zip.part")
        tmp.write_bytes(data)
        tmp.replace(self.archive_path)
        self.state_path.write_text(
            json.dumps({"fetched_at": snapshot.fetched_at, "bytes": len(data)}, ensure_ascii=False),
            encoding="utf-8",
        )
        log.info(
            "CVM registry refreshed",
            extra={
                "funds": snapshot.fund_count,
                "classes": snapshot.class_count,
                "bytes": len(data),
            },
        )
        return snapshot

    def _load_cached(self) -> RegistrySnapshot | None:
        if not self.archive_path.is_file():
            return None
        try:
            snapshot = parse_archive(self.archive_path.read_bytes())
        except (OSError, ValueError, zipfile.BadZipFile) as exc:
            log.error(
                "the cached CVM registry is unusable; new registrations are blocked this run",
                extra={"error": str(exc), "path": str(self.archive_path)},
            )
            return None
        snapshot.fetched_at = self._state().get("fetched_at", snapshot.fetched_at)
        return snapshot
