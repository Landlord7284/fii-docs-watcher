# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project state

`arquitetura-fii-monitor-pipeline-a-rev3.md` is the architecture and boundary-conditions document (revision 3) for **Pipeline A** — a robot that downloads Brazilian real-estate fund (FII) documents daily from Fundos.NET and files them into per-day directories for human reading, over a sliding N-day retention window.

Pipeline A is implemented in Python 3.12+ under `src/fii_docs_watcher/`, with three dependencies: `httpx`, `ruamel.yaml` (because §3.6 requires comments to survive a rewrite, which PyYAML cannot do) and `tzdata` (because `clock` resolves a zone at import time and a minimal Linux image ships no IANA database, making it a crash before any error handling runs). Everything else is standard library.

```bash
python -m venv .venv && .venv/bin/pip install -e ".[dev]"

.venv/bin/pytest                 # unit + contract + integration; live tests deselected
.venv/bin/pytest -m live         # re-measure the real source (slow; needs network)
.venv/bin/ruff check src tests docker   # lint
```

Running it, once a `config.toml` exists (copy `config.example.toml`):

```bash
python -m fii_docs_watcher doctor    # check config, roots, timezone, both sources
python -m fii_docs_watcher add --cnpj 08.431.747/0001-06 --ticker HGBS11
python -m fii_docs_watcher run       # the canonical one-shot mode
```

**`USAGE.md` at the repo root is the user-facing command reference** — keep it in step with the CLI.

Other subcommands: `list [QUERY]`, `rm QUERY`, `ticker QUERY`, `resolve`, `reconcile`, `purge`, `audit`, `status`. Exit codes: `0` clean, `1` ran with isolated failures, `2` bad configuration, `3` another instance holds the lock.

## Container packaging

`Dockerfile`, `compose.yaml`, `.env.example` and `docker/` package the robot as an image published to GHCR by `.github/workflows/docker-publish.yml`. Nothing in the package knows about any of it — §7's portability rule holds, and the image is one way to run the same one-shot CLI.

**`config.toml` is the application's configuration in both modes**, mounted at `/config/config.toml` in the container and native on a host. `.env` carries only what has no TOML counterpart: `IMAGE_TAG`, `DOCUMENTS_PATH`, `PUID`/`PGID`, `RUN_SCHEDULE`, `RUN_ON_START`. Do not grow `.env.example` into a second copy of the settings — a config file plus targeted `FII_WATCHER_*` overrides is the arrangement; two parallel documented surfaces is not, and one of them would inevitably drift.

The image pins `FII_WATCHER_PATHS_DATA_ROOT=/data` and `FII_WATCHER_PATHS_DOCUMENTS_ROOT=/documents`, so those two keys in a mounted config are inert. That is deliberate: the container paths follow from the volume layout, and a user who copied the example without repointing `./var/…` would otherwise write the archive into the container's ephemeral layer and lose it on the next recreate.

`docker/scheduler.py` is the periodic driver and lives **outside** the package, because §7 requires a daemon mode to be built on top of one-shot rather than the other way round. It spawns `python -m fii_docs_watcher run` as a child process — so a crash inside a run cannot kill the loop — and matches its 5-field cron expression by waking each minute and testing the current time in `clock.source_tz()`. Schedules therefore mean the same thing as directory names. It is tested by `tests/unit/test_scheduler.py`, which loads it by path.

**A fund can leave the watch list two ways, and both must stop its backlog.** `discover` stops querying a fund once it leaves `funds.yaml`, but `fetch` builds its queue from the manifest, not from the scope list. Three mechanisms keep that honest:

- `rm` calls `abandon_pending()` for the entities it removes, and `forget_entities()` to drop their `sync_state`;
- `run.execute` sweeps with `abandon_pending_outside(configured_ids)`, catching a scope edited out of the YAML by hand. It uses the ids of **all** configured scopes, resolved or not, so a fund that merely failed to resolve this run keeps its queue;
- `fetch.run` defers any pending document whose entity is not among the scopes it was given. That is a correctness guard, not tidiness: `fetch_one` can only run the §3.3 CNPJ check when it knows the entity, so downloading without one would archive a document with that check silently skipped.

Files already archived are left to age out through the normal frontier; `rm --delete-documents` removes them immediately. `stale_watermarks()` takes an entity filter for the same reason — otherwise a deliberately removed fund warns about an unrecoverable gap on every run forever.

The config file is **discovered** — `--config` → `$FII_WATCHER_CONFIG` → `./config.toml` → `./fii-docs-watcher.toml` → `~/.config/fii-docs-watcher/config.toml` → built-in defaults — so the flag is rarely needed and works before or after the subcommand. Falling through to the defaults logs a warning on purpose: they point at `./var/…`, and a silent fallback means operating on a different archive than the one intended.

**Do not lower `[source].read_timeout_seconds` below ~90s.** Successful responses from this host are bimodal — ~0.3s or ~60.3s, nothing between — so a conventional 30s timeout fails roughly half of all *successful* requests.

**`[download].formats`** (default `["pdf"]`) selects which formats are archived. **XML is opt-in**: the archive is a reading queue for people, and the XML is the same filing in a machine-readable shape that Pipeline B fetches for itself. Adding a format declines the others *before the request* wherever the listing allows the format to be predicted; a mispredict is declined after download with a warning. Declined documents are recorded as `skipped` rather than dropped, and `pending_downloads()` re-evaluates them every run, so widening the list picks them up without re-discovery.

## The spec is the authority

`arquitetura-fii-monitor-pipeline-a-rev3.md` is the source of truth. It defines *what* and *why*, never *how*.

- Requirements marked **Invariante** cannot be violated.
- Section 10 explicitly grants the implementer freedom over language and runtime, module structure, HTTP/YAML/persistence libraries, scheduling and packaging, logging and observability, CLI framework, test strategy, concurrency (within rate limits), exact database schema and migrations, and the exact format of the `_inbox` index.
- Section 2 is empirically verified behavior of the real system — treat it as observed fact, but note the source has no API contract and can change without notice.
- Section 9 lists what is still unverified. Confirm those points empirically during implementation; do not assume any of them.

Divergence from an invariant must be **stated and justified, never silent**.

## Language and naming — strict

**All code, configuration, filenames, database schema, log output, comments, and documentation in this repository are in English.** The architecture document and day-to-day conversation are in Portuguese; that does not carry into the repository.

The only exception is data we do not own: field and endpoint names in Fundos.NET responses are quoted verbatim when describing the wire format, and `CNPJ` keeps its regulatory name.

The spec names things in Portuguese. Translate them; do not transliterate. This mapping is fixed so that different sessions do not invent competing vocabularies:

| Spec (pt) | Code (en) |
|---|---|
| `raiz_dados` / `raiz_documentos` | `data_root` / `documents_root` |
| `escopo` / `entidades` | `scope` / `entities` |
| `descoberto` → `baixando` → `disponivel` | `discovered` → `downloading` → `available` |
| `falha` / `purgado` | `failed` / `purged` |
| `fundos.yaml` / `manifesto.sqlite` / `robo.lock` / `cache-cvm/` | `funds.yaml` / `manifest.sqlite` / `watcher.lock` / `cvm-cache/` |
| tables `documentos` / `tentativas_download` / `sync_estado` | `documents` / `download_attempts` / `sync_state` |
| `id_documento` / `versao` / `id_fundosnet` | `document_id` / `version` / `fundosnet_id` |
| `data_entrega` / `data_referencia` / `formato_data_referencia` | `delivery_date` / `reference_date` / `reference_date_format` |
| `estado_local` / `hash_conteudo` / `data_purga` / `visto_em` | `local_state` / `content_hash` / `purged_at` / `seen_at` |
| `denominacao_social` / `codigo_cvm` / `situacao_cvm` | `legal_name` / `cvm_code` / `cvm_status` |
| `expansao: parcial` | `expansion: partial` |
| `{prefixo_entidade}` in filenames | `{entity_prefix}` |

The table is illustrative of the rule, not exhaustive. Anything the spec names in Portuguese and this table omits still gets an English name.

## Non-negotiable invariants

Section pointers refer to `arquitetura-fii-monitor-pipeline-a-rev3.md`.

- **Publication identity is `(document_id, version)`** — never the document id alone. That pair is the key for dedupe, idempotency, and the filename. The content hash serves integrity and audit and is **never** the dedupe key (§2.4).
- **Discovery queries per entity with `idFundo`** — never by matching `descricaoFundo` text. The listing returns `cnpjFundo` and `idFundo` as `null` on every row, even when filtering by `idFundo`, so text routing is a silent failure mode; revision 3 reverted to per-entity queries precisely to kill it (§2.3, §4.1). The global listing is **detective-only audit**: it raises alerts, never routes a document into the archive and never serves as a discovery path (§4.5).
- **Every run queries the whole retention window** `[today - (N-1), today]` per entity. There is no incremental interval. The watermark records completed progress and raises alerts; it is not an input to the interval calculation (§4.2, §4.3).
- **Rediscovered documents update mutable fields and never trigger a re-download.** `status` in the manifest means "last state observed inside the retention window" (§4.2).
- **The stored extension is decided by the actual response**, in this order of confidence: content signature (decisive) > `Content-Disposition` > `Content-Type` (least reliable). The "Estruturado" heuristic is for *early routing* only (§2.5).
- **Validate content; a successful parse is not enough.** Recognize PDF by the `%PDF-` signature; require a plausible root for XML; explicitly reject an `html` root and error-page bodies even when well-formed; parse with external entity resolution disabled; cap response size; treat unrecognized content as a noisy failure and never write it silently. HTTP 200 with an HTML error body is a real failure mode in this system (§2.5, §8).
- **Two separate roots.** `data_root` is private (funds YAML, SQLite manifest, lock, CVM cache) and must live on a filesystem local to the process — SQLite over SMB/NFS has unreliable locking and durability. `documents_root` is the shareable archive. Download temporaries (`.part`) go under `documents_root/.tmp/` so `rename` stays atomic within one filesystem (§5.1).
- **Date directories are `yyyy-mm-dd`, zero-padded, keyed on `dataEntrega`** — not on download date. Lexicographic order must equal chronological order; purge and human reading depend on it (§5.2).
- **`N` is the number of dates retained, including today**: `first_retained_date = today - (N-1)`. Purge, query window, and the `_inbox` index use that one frontier, or discovery downloads what purge then deletes (§5.6).
- **The run lock is `flock`, not a pidfile.** The kernel releases it when the holder dies, so there is no stale lock to detect and a crash can never strand the robot. A pidfile cannot be honest about this: PIDs are namespace-local and reused, so a lock recorded by a dead process can name a PID that is alive again — near-certain in a container, where the run is often PID 1 — and the liveness probe then blocks every subsequent run with exit 3 forever. The JSON payload inside `watcher.lock` is diagnostic only; nothing reads it to make a decision, and the file is left in place on release because unlinking a flocked file is racy. This relies on `data_root` being local, which §5.1 already requires.
- **Download state machine** `discovered → downloading → available`, with reconciliation of intermediate states at startup. Filesystem and SQLite do not form one atomic transaction, so idempotency rests on the manifest plus reconciliation — never on file existence alone (§5.5).
- **YAML write protection.** Atomic temp file + `rename`, *and* compare the hash of the on-disk content against what was loaded before renaming; if it changed, do not overwrite — record a visible conflict and keep the human's edit. `mtime` is insufficient. Comments must survive the rewrite. CNPJ is a string through the whole cycle, or a YAML parser eats the leading zero (§3.6).
- **Timezone is anchored to the source, never to the host or container**, for "today", directory dates, retention frontier, index, watermark, and **log timestamps** — `logging_setup` overrides `formatTime` for exactly this reason, since `logging`'s default uses libc `localtime` and would otherwise stamp an event with one date while the same event is filed under another. **Stated deviation from §5.8:** the spec calls this fixed and not a user setting; it is exposed as `[source].timezone`, defaulting to `America/Sao_Paulo`, so that a run can be reproduced against a differently-zoned source and so tests can vary it. The invariant that survives is the one that matters — the zone is *never* read from the host, is installed exactly once by `config.load()` via `clock.set_timezone()`, and an unrecognised name refuses to start rather than falling back. Read it with `clock.source_tz()`; never `from .clock import` the zone by value, which would bind the default at import time and silently ignore the configuration.
- **Portability.** Nothing may depend on Docker, TrueNAS, systemd, cron, or any orchestrator. Running once from a shell with a config file must work; a daemon mode, if any, is built on top of one-shot. No hardcoded paths, CNPJs, or personal preferences. Logs to stdout/stderr by default. No credentials — the source is public and unauthenticated (§7).
- **Isolated failure never kills the batch.** A bad scope, entity, or document is recorded and skipped; the rest proceed. Severity ladder: `WARNING` transient/retryable, `ERROR` needs human action, `CRITICAL` the source contract likely changed or a CNPJ validation diverged (§8).
- **Pipeline A and Pipeline B share only the downloader.** B never reads files written by A, and A's purge never depends on B's progress. If B needs an XML, it downloads its own (§1).

## Source behavior worth not re-deriving

Host `fnet.bmfbovespa.com.br`; `idTipoFundo=1` / `tipoFundo=1` means real-estate fund. No endpoint required a captcha or authenticated session (§2.1).

| Endpoint | Use |
|---|---|
| `listarFundos` | name → internal `id` |
| `listarTodasCategoriaPorTipoFundo` | category vocabulary |
| `pesquisarGerenciadorDocumentosDados` | document listing (DataTables-style paging) |
| `downloadDocumento` | fetch the file (PDF **or** XML from the same endpoint) |

Verified traps (§2.2, §2.3, §2.6):

- `dataInicial`/`dataFinal` filter on `dataEntrega`, not `dataReferencia`; both ends are inclusive.
- Omitting `idFundo` returns every fund. `idFundo=0` returns **empty** — `0` is not "all", it is a nonexistent id.
- The `cnpj` filter is silently ignored by this search.
- `cnpjFundo`, `idFundo`, `nomeAdministrador` are `null` on every row.
- `nomePregao` is often empty and is an internal alias, **not** the B3 ticker.
- `arquivoEstruturado` arrives as `" "` even for XML documents — not a usable flag.
- `tipoDocumento` is inconsistent (code, text, or empty, sometimes with stray spaces); for assemblies the meaning lives in `especieDocumento`.
- `dataReferencia` has three formats, discriminated by `formatoDataReferencia` (2 = month/year competence, 3 = date, 4 = date with time), and can be in the future.
- `Content-Disposition` carries the emitting entity's CNPJ in the served filename — the only place the entity CNPJ appears. Parsing it is **best-effort**: on failure, log visibly and fall back to the CNPJ that originated the query; never halt the pipeline.
- Volume is roughly 540 documents/day across the whole FII market.

This is scraping of a UI endpoint, not a contracted API: rate-limit, back off exponentially, send an identifiable `User-Agent`, and re-verify these observations rather than trusting them indefinitely (§8).

## Section 9's open questions — answered empirically on 2026-08-14

Measured against the live source; encoded in the code and pinned by `tests/contract/`. The source has no API contract, so re-measure with `pytest -m live` rather than trusting these forever.

**§9.5 — pagination silently loses rows, and `recordsFiltered` does not catch it.** This is the finding that matters most. Paging a full day at `l=50` with no sort parameter returned 217 rows for a `recordsFiltered` of 217 while containing only **175 distinct ids — 42 rows (19%) skipped**, with duplicates masking the loss one-for-one. `o[0][id]` and `o[0][0]` are ignored outright. Only **`o[0][dataEntrega]=asc`** paginates cleanly (217/217). Therefore:

- every listing request sends `o[0][dataEntrega]=asc` — not optional, not configurable;
- coverage is asserted on **distinct `(id, versao)` vs `recordsFiltered`**, never on row count, because the row count matched perfectly while a fifth of the day went missing.

**§9.2 — `l` is honoured to 200; `l>=250` returns HTTP 500**, even when politely spaced, so it is a real ceiling rather than rate limiting. The endpoint never truncates silently — it either returns exactly what was asked for or errors.

**A small page length is not a cheap query.** `scan()` paginates until the whole window is covered, so `scan(..., page_length=1)` costs *one request per document* — 74 of them for a fund like KINEA RENDA, each able to stall ~60s, and the lot retried if coverage falls short. Combined with the bimodal latency that is an apparent hang, not a slow command. Anything that only needs "does this id exist, and what is it called?" must use `listing.probe()`, which is exactly one request and reads `recordsFiltered` for the total.

**§9.3 — `listarFundos` does expose classes**, each with its own id (`URBANITY CORPORATE` → fund 25256, `CLASSE A` 25257, `CLASSE B` 25258). Two further behaviours the spec does not mention: it pages at **20 results** with a `more` flag, and **the same name can map to several ids** (`CLASSE A DE COTAS DO VBI ULIVING MULTICLASSE` → 1054 *and* 20524), so a candidate is never chosen on a name match alone.

**§9.1 — the listing keeps only the live version.** A document re-filed as v2 returns v2 only; v1 disappears. The v1 file stays on disk and is marked `superseded_at` in the manifest — that history is the point of the archive.

**§9.4 — `Content-Disposition` format confirmed** as `{cnpj14}-{SIGLA}{ddMMyyyy}V{NN}-{id9}.{ext}`. For a monoclass fund the CNPJ is the fund's, confirming §2.7's mitigating factor. The fund-vs-class question for a genuinely multiclass fund remains untested; §3.3's rule (compare against the **queried entity's** CNPJ) already handles either answer.

**§9.7 — cost is not a concern.** A monoclass scope costs 1–2 requests per run; the dominant cost is the ~60s stalls, not request volume.

Wire-schema facts worth not re-deriving: `fundoOuClasse` is now overwhelmingly `Classe` (199 vs 7 on a sample day) — the entity model is the common path, not the exotic one; `formatoDataReferencia` is a **string** while `versao` is an **int**; `codSegNegociacao` is universally null, independently reconfirming that B3 ticker resolution has no native source.

### The CVM registry is one ZIP, not two CSVs

`https://dados.cvm.gov.br/dados/FI/CAD/DADOS/registro_fundo_classe.zip` (~6.7 MB, refreshed daily) contains `registro_fundo.csv`, `registro_classe.csv` and `registro_subclasse.csv`, **latin-1**, `;`-delimited. `registro_classe.csv` carries `ID_Registro_Fundo`, giving a *structural* fund→classes join.

**Stated deviation from §3.2.** The spec prefers expanding classes through `listarFundos` and treats the registry's class file as a last resort, because each external dependency is permanent cost. That cost is already paid: the same ZIP is mandatory for CNPJ→legal-name, since the listing never returns a CNPJ. Given that, the structural join is used for expansion — it avoids reintroducing name-substring matching, the exact failure mode revision 3 exists to eliminate, and sidesteps the duplicate-id hazard above. `listarFundos` remains the only source of `id_fundosnet`. Watch out: the registry contains one fund whose classes are registered **twice under the same CNPJ and name**, so expansion deduplicates on `(cnpj, legal_name)`.

## Out of scope

- Parsing document contents — that is Pipeline B.
- Long-term preservation. This is a sliding N-day window, not an archive.
- **B3 trading ticker resolution — a closed decision.** No native source exists in CVM or Fundos.NET data (verified across the FCA, the Open Data Portal monthly-report CSVs, and the structured monthly report XML, which carries only `CodigoISIN`). Do not introduce an external dependency for it.
- Correlating a document with its later re-filing when they arrive under different ids — "logical correlation", as distinct from publication identity (§1, §2.4). Note the spec's own cross-reference to "6.1" here is dangling; section 6 has no subsections.
