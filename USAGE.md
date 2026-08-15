# fii-docs-watcher — command reference

Downloads Brazilian real-estate fund (FII) documents from Fundos.NET every day and files them into
per-day directories, so you can open today's folder and see what is new. It keeps a sliding window
of `N` days and deletes anything older; it is a reading queue, not an archive.

---

## Install

```bash
python -m venv .venv
.venv/bin/pip install -e .
```

Python 3.12 or newer. Two dependencies: `httpx` and `ruamel.yaml`.

Either run it as `python -m fii_docs_watcher …` or, with the venv active, as `fii-docs-watcher …`.
This document uses the short form.

## Quick start

```bash
cp config.example.toml config.toml       # set data_root and documents_root
fii-docs-watcher doctor                  # check the environment
fii-docs-watcher add --name "fund name"
fii-docs-watcher run
```

Then open `documents_root/_inbox/<today>.md` for what arrived today, or the dated directories for
what was published on a given day.

To keep it current, schedule `run` daily — cron, a systemd timer, whatever you already use. It
takes no arguments and exits with a meaningful code.

```cron
30 8 * * *  cd /srv/fii && /srv/fii/.venv/bin/fii-docs-watcher run >> /var/log/fii.log 2>&1
```

---

## Configuration

The config file is **discovered**, so `--config` is rarely needed. First match wins:

1. `--config PATH` — an explicit path that does not exist is an error, never a fallback
2. `$FII_WATCHER_CONFIG`
3. `./config.toml`
4. `./fii-docs-watcher.toml`
5. `~/.config/fii-docs-watcher/config.toml`
6. built-in defaults — **a warning says so**, because the defaults point at `./var/…` and you
   would otherwise be working on a different archive than you think

`doctor` prints which file it resolved.

Any value can be overridden by environment variable, named `FII_WATCHER_<SECTION>_<KEY>`:

```bash
FII_WATCHER_RETENTION_DAYS=14 fii-docs-watcher run
FII_WATCHER_DOWNLOAD_FORMATS=xml fii-docs-watcher run
```

### Settings

| Key | Default | What it does |
|---|---|---|
| `paths.data_root` | `./var/data` | Private state: `funds.yaml`, `manifest.sqlite`, the lock, the CVM cache. **Must be on a local filesystem** — SQLite over SMB/NFS is not safe. |
| `paths.documents_root` | `./var/documents` | The archive. This is the one you share. |
| `retention.days` | `7` | Dates kept, **including today**. `N=7` on the 14th keeps the 8th to the 14th. |
| `download.formats` | `["pdf", "xml"]` | Which formats to keep. See [Choosing formats](#choosing-formats). |
| `download.stale_part_hours` | `6` | Age at which an orphaned `.part` file is swept. |
| `source.read_timeout_seconds` | `120` | **Do not lower below ~90.** See [Why runs are slow](#why-runs-are-slow). |
| `source.page_length` | `200` | Listing page size. 200 is the server's ceiling; above it returns HTTP 500. |
| `source.min_request_interval_seconds` | `1.5` | Courtesy pause between requests. |
| `source.user_agent` | identifiable string | Sent on every request. Keep it identifiable. |
| `audit.frequency` | `daily` | `daily`, `weekly` or `never` — cadence of the global cross-check. |
| `files.directory_mode` / `file_mode` | `0o755` / `0o644` | Creation modes in the archive, so whoever mounts the share can read it. |
| `logging.level` / `format` | `INFO` / `text` | `format` may be `json`. Logs go to stdout/stderr; `logging.file` adds a file. |

There are no credentials anywhere: the source is public and unauthenticated.

### Choosing formats

`downloadDocumento` serves PDF and XML from the same endpoint. By default both are archived.
Naming one makes the robot decline the other:

```toml
[download]
formats = ["pdf"]     # only PDFs; XML is not downloaded at all
```

Where the format can be predicted from the listing — documents whose category or type says
"Estruturado" come back as XML — the decision is made **before the request**, so an unwanted format
costs no bandwidth. If the prediction turns out wrong, the file is still declined and a warning is
logged.

Declined documents are recorded in the manifest as `skipped`, not forgotten. Widen the list later
and the next `run` picks them up without needing to rediscover them.

---

## Commands

Every command accepts `--config PATH` and `--verbose`, before or after the command name.

### `run`

Runs the pipeline once and exits. **This is the only command a scheduler needs.**

In order: reconcile whatever a previous run left half-done → refresh the CVM registry snapshot →
resolve any scope that needs it → query the whole retention window per entity → download what is
new → write the inbox index → purge past the frontier → run the global audit.

```bash
fii-docs-watcher run
fii-docs-watcher run --dry-run      # resolve scopes, stop before discovery
fii-docs-watcher run --skip-audit
```

Safe to run as often as you like. Rediscovering a document refreshes its status and never downloads
it again.

### `add`

Registers a fund. Give it a CNPJ, or a partial name to search for.

```bash
fii-docs-watcher add --cnpj 12.345.678/0001-90 --ticker ABCD11
fii-docs-watcher add --name "fund name"        # lists matches, you pick
```

You give **one** CNPJ; the robot works out the rest. Since RCVM 175 a fund's documents may be filed
by its share classes, each with its own CNPJ and its own Fundos.NET id, so registering a *fund*
CNPJ monitors the fund and its active classes. Registering a *class* CNPJ monitors only that class.

`--name` searches the CVM registry (local, instant) and shows the CNPJ of each match, because
Fundos.NET can search by name but never returns a CNPJ. After you pick, it offers to set a ticker.

| Flag | Effect |
|---|---|
| `--ticker ABCD11` | Your annotation, used as the filename prefix. The robot never invents or validates it. |
| `--this-entity-only` | Monitor only this entity, not the fund's other classes. |
| `--no-resolve` | Write the entry without contacting the sources; the next `run` resolves it. |

Re-running `add` on a CNPJ that is already registered updates the fields you own rather than
refusing. To stop following a fund, see [`rm`](#rm-query).

### `list [QUERY]`

Shows registered funds and their resolved entities. With a `QUERY`, only matching ones.

```bash
fii-docs-watcher list
fii-docs-watcher list fund-name
fii-docs-watcher list 08756747
```

The search covers ticker, legal name, Fundos.NET description and CNPJ digits, ignoring accents,
case and punctuation.

### `rm QUERY`

Stops following a fund and removes it from the watch list.

```bash
fii-docs-watcher rm fund-name                             # pick, then confirm
fii-docs-watcher rm 12982956 --yes                        # no prompt
fii-docs-watcher rm fund-name --yes --delete-documents    # also delete its files
```

`QUERY` searches the same way `list` does. If it matches several funds you are shown a numbered
list; without a terminal, an ambiguous query is an error rather than a guess.

What happens to what it already collected:

| | Default | With `--delete-documents` |
|---|---|---|
| Files in the archive | Left in place; they age out within `retention.days` | Deleted now, and empty date directories are removed |
| Documents discovered but not yet downloaded | Stood down, never fetched | Same |
| Manifest rows | Kept | Kept, marked purged |

Manifest rows are kept either way: they are a record of what the source published while you were
following the fund, and they cost almost nothing.

`--yes` skips the confirmation, which is what you want in a script. The previous watch list is
saved as `funds.yaml.bak`, though note that is only one level deep: the next command that writes the
file overwrites it.

#### Removing a fund by editing `funds.yaml`

Deleting a scope's block by hand does the same thing, and the next `run` notices:

- no new documents are discovered for it — discovery only queries entities in the watch list;
- its queued-but-not-yet-downloaded documents are **stood down** (`abandoned`), so they are never
  fetched. Without that they would still be downloaded, because the download queue is built from
  the manifest rather than from the watch list;
- files already in the archive stay, and age out through the retention window as usual;
- its manifest rows stay, as the record of what the source published while you followed it;
- it stops appearing in watermark-gap warnings, which only concern funds you still follow.

The run summary reports what it stood down:

```
stood down        4 queued document(s) whose fund left the watch list
```

A fund that is still in `funds.yaml` but failed to resolve on a given run — say the CVM registry
was unreachable — keeps its queue. Those documents are reported as `deferred` and are picked up on
the next run that resolves the fund, so an outage never costs you a backlog.

### `ticker QUERY`

Sets or clears the ticker on a fund you already registered.

```bash
fii-docs-watcher ticker fund.name                 # pick from matches, then prompt
fii-docs-watcher ticker fund.name --set ABCD11    # non-interactive
fii-docs-watcher ticker fund.name --clear
```

If `QUERY` matches several funds you are shown a numbered list. Without a terminal, `--set` or
`--clear` is required and an ambiguous query is an error rather than a guess.

**Existing files are not renamed.** A filename records the prefix that was true when the document
was downloaded; only future downloads use the new one.

### `resolve`

Re-runs CNPJ → entities → Fundos.NET id and writes the result back to `funds.yaml`, refreshing the
CVM registry first.

```bash
fii-docs-watcher resolve          # only unresolved scopes
fii-docs-watcher resolve --all    # every scope
```

Use it after a fund is renamed, after a new share class appears, or when `audit` reports a document
that discovery did not capture.

### `status`

Retention window, document counts by state, and any entity whose last complete scan predates the
retention frontier — documents in such a gap were published and purged without ever being seen, and
cannot be recovered.

States: `discovered` (queued) · `downloading` (in flight) · `available` (on disk) · `failed` (will
retry) · `skipped` (not a configured format) · `abandoned` (its fund was removed from the watch
list) · `purged` (aged out; the row is kept, the file is not).

### `doctor`

Checks everything a run depends on: which config file was resolved, both roots writable, staging on
the same filesystem as the archive (or `rename` would not be atomic), the manifest openable, and
both sources reachable. **Run this first on a new machine.**

### `reconcile`, `purge`, `audit`

`run` does all three at the right moment; these expose them individually.

- **`reconcile`** — settle anything an interrupted run left behind and sweep orphaned `.part`
  files. A file whose bytes still validate is adopted rather than downloaded again.
- **`purge`** — delete date directories older than the frontier now.
- **`audit`** — scan the global listing for documents whose fund name matches a monitored scope but
  which discovery did not capture. **Detective only:** it never files a document and never fails
  the job. A hit means a scope needs revalidating.

---

## What lands on disk

```
documents_root/
  _inbox/
    2026-08-14.md                     what arrived today, with links
  2026-08-13/
    ABCD11_Informes-Periodicos_1450363_V01.xml
  2026-08-14/
    ABCD11_Informes-Periodicos_1293424_V01.xml
  .tmp/                               downloads in flight
```

Directories are named for the **delivery date** (`dataEntrega`), not the day you downloaded them.
After the machine has been off for three days, the new documents land in three past directories —
which is why `_inbox/<today>.md` exists: it lists what actually arrived today, wherever it was
filed.

Filenames are `{prefix}_{category}_{id}_V{version}.{ext}`. The version is part of the name because a
re-filing can reuse the document id, and without it v2 would overwrite v1. The extension is decided
by the file's actual content, never by what the server claimed.

`data_root` holds `funds.yaml` (yours to edit, and the robot writes resolved data back into it),
`manifest.sqlite`, `watcher.lock` and the CVM cache. It is never meant to be shared.

---

## Exit codes

| Code | Meaning |
|---|---|
| `0` | Clean. |
| `1` | Ran, but something failed in isolation and was skipped. The rest completed. |
| `2` | Invalid configuration or arguments. Nothing ran. |
| `3` | Another instance holds the lock. |

---

## Why runs are slow

Fundos.NET answers in either **~0.3 seconds or ~60 seconds**, with nothing in between, on every
endpoint. That is the source's behaviour, not a bug here, and it is why the read timeout defaults to
120s — a conventional 30s timeout would fail about half of all *successful* requests.

So a run covering several entities taking minutes is normal. Every step logs as it happens, so you
can tell a slow run from a stuck one. Use `--verbose` for per-request detail.

## Troubleshooting

**"no config file found; using built-in defaults"** — you are writing to `./var/…` relative to the
current directory. Either `cd` to where your `config.toml` lives, or pass `--config`, or set
`$FII_WATCHER_CONFIG`.

**Exit code 3 / "another instance is running"** — a previous run is still going, or was killed hard.
A lock left by a dead process is detected and reclaimed automatically on the next run.

**A scope stays `UNRESOLVED`** — run `fii-docs-watcher resolve` and read the log. Common causes: the
CNPJ is not a registered FII in the CVM registry, or the CVM registry could not be downloaded (in
which case already-resolved funds keep working and only new registrations are blocked).

**A `CRITICAL` about CNPJ divergence** — a document arrived whose CNPJ matches no entity of the
scope it was filed under, which means the name → id resolution may have picked the wrong fund. The
document is not archived. Run `resolve --all` and check the entities.

**Documents appear in `status` as `failed`** — transient failures are retried on later runs, up to
five attempts per document. Persistent ones are usually content that failed validation; run with
`--verbose` to see why.

**Nothing new for days** — normal. Many funds go a week without publishing. `audit` is the check
that would catch documents being missed rather than absent.
