# fii-docs-watcher

A robot that downloads Brazilian real-estate fund (FII) documents from
[Fundos.NET](https://fnet.bmfbovespa.com.br) every day and files them into per-day directories, so
you can open today's folder and see what is new.

It keeps a sliding window of `N` days and deletes anything older: this is a reading queue, not a
long-term archive. You register the funds you care about by CNPJ; each run queries the whole
retention window per entity, downloads what it has not seen, writes an index of what arrived today,
and purges past the frontier.

Documents are filed by **delivery date** (`dataEntrega`), not by download date, so a machine that
was off for three days lands the backlog in the three past directories it belongs to.

Parsing the documents is out of scope — that is a separate pipeline.

## Install

Python 3.12 or newer. Two runtime dependencies (`httpx`, `ruamel.yaml`); everything else is standard
library.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .          # or: pip install -e ".[dev]"  for pytest + ruff
```

Dependencies live in `pyproject.toml`; there is no `requirements.txt`.

## Configure

```bash
cp config.example.toml config.toml
```

Then set `paths.data_root` (private state — must be on a local filesystem) and
`paths.documents_root` (the shareable archive). The config file is discovered automatically, so
`--config` is rarely needed.

## Run

```bash
fii-docs-watcher doctor                # check config, roots and both sources
fii-docs-watcher add --cnpj 12.345.678/0001-90 --ticker ABCD11
fii-docs-watcher run                   # the canonical one-shot mode
```

Then open `documents_root/_inbox/<today>.md`. Schedule `run` daily with cron or a timer; it takes no
arguments and exits `0` clean, `1` isolated failure, `2` bad configuration, `3` already running.

Runs taking minutes is normal — the source answers in either ~0.3s or ~60s, with nothing in between.

## Documentation

- **[USAGE.md](USAGE.md)** — the full command reference: every subcommand, every setting,
  what lands on disk, and troubleshooting.
- `arquitetura-fii-monitor-pipeline-a-rev3.md` — the architecture specification (in Portuguese),
  which is the authority on behaviour.

## Tests

```bash
pytest                    # unit + contract + integration
pytest -m live            # re-measure the real source (slow; needs network)
ruff check src tests
```
