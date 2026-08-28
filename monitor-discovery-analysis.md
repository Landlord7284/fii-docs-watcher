# The monitor's discovery mechanism — analysis

A design note, not a specification. It compares two ways of building the frequent `run --monitor`
profile: the one currently implemented on the working branch, which narrows the window of the
existing per-entity sweep, and a proposed one that reads the source's own listing newest-first and
stops when it recognises what it has already seen. It ends with the trade-offs of adopting the
second, and what would have to be accepted along with it.

Nothing here is implemented. Measurements are dated, because this source has no API contract and a
figure is a reading of the window it was taken in.

---

## 1. The problem the first attempt ran into

Discovery queries **one request per entity**, with `idFundo`, covering the whole window in that one
request — `dataInicial`/`dataFinal` are a range filter, not a day selector. A fund files few enough
documents that a 2-day window and a 7-day window both fit in a single page of 200.

The consequence is that narrowing the window does not narrow the cost:

| Profile as implemented | Requests per firing |
|---|---|
| `run` (7-day window) | one per entity |
| `run --monitor` (2-day window) | one per entity |

So the monitor costs exactly what the sweep costs, and running it hourly multiplies the request
volume by the number of firings. It buys freshness and nothing else, and the only real saving is
that it declines the global audit. That is the redundancy that prompted this note.

Splitting the window into day-by-day queries would make it strictly worse: one request per day *per
entity*, so a 2-day monitor over 20 funds would cost 40 requests instead of 20.

---

## 2. What was measured

All figures from `fnet.bmfbovespa.com.br`, `tipoFundo=1`, on **2026-08-27**, via the project's own
client at a 1.5–2 s request interval.

### Volume per day

| Delivery date | `recordsFiltered` | Pages at `l=200` | First → last delivery |
|---|---|---|---|
| 2026-08-27 (Thu) | 77 | 1 | 08:48 → 20:46 |
| 2026-08-26 (Wed) | 73 | 1 | 00:03 → 20:36 |
| 2026-08-25 (Tue) | 81 | 1 | 09:22 → 23:54 |
| 2026-08-24 (Mon) | 102 | 1 | 08:44 → 23:36 |
| 2026-08-23 (Sun) | 0 | 0 | — |
| 2026-08-22 (Sat) | 0 | 0 | — |

A four-day window returned `recordsFiltered=333`, exactly the sum of its days, confirming again that
both ends of the range are inclusive.

**These numbers do not retire the ~540 documents/day recorded in `CLAUDE.md`.** That was read in its
own window and is equally true of it. Publication volume in this source is seasonal and clustered:
the last days of a month carry the income announcements, and a filing deadline concentrates a
month's worth of one document type into one afternoon. Any cost claim below is therefore given at
several volumes, and the mechanism is sized for the peak rather than for the week it was measured
in.

### Ordering, which is the load-bearing property

§9.5 established that paging without a sort silently loses rows — 217 collected, 175 distinct, with
duplicates masking the loss — and that `o[0][id]` and `o[0][0]` are ignored outright. Only
`o[0][dataEntrega]=asc` had been shown to paginate cleanly. **Descending was never tested.** It is
now:

| Read | Window | `recordsFiltered` | Collected | Distinct | Pages | Order verified |
|---|---|---|---|---|---|---|
| `dataEntrega asc`, `l=200` | 24–27/08 | 333 | 333 | 333 | 2 | non-decreasing |
| `dataEntrega desc`, `l=50` | 24–27/08 | 333 | 333 | 333 | 7 | non-increasing |
| `dataEntrega asc`, `l=50` | 24–27/08 | 333 | 333 | 333 | 7 | — |

Zero rows lost either way, and the descending set is identical to the ascending one. The first
descending page returns the newest deliveries, most recent first. Within a single day the listing is
ordered strictly by delivery time, so **a document delivered later can only appear at the newest
end** — which is what makes "read until you recognise something" a valid stopping rule rather than a
guess.

---

## 3. The proposed mechanism

`run --monitor` stops asking the watch list what is new and asks the source instead.

1. **Read the newest end.** One request to `pesquisarGerenciadorDocumentosDados` with no `idFundo`,
   `o[0][dataEntrega]=desc`, `l=200`, over the monitor window — one such read per distinct fund type
   in the watch list.
2. **Stop on repetition.** The cursor is a high-water mark: the newest `dataEntrega` this profile
   has already accounted for. Paging stops at the first row at or below it. In the ordinary case the
   stop happens inside the first page, so the whole step is one request.
3. **Match against the watch list.** Each new row's `descricaoFundo` is folded and compared against
   the source's *own* spelling for each monitored entity, already stored in `funds.yaml` as
   `fnet_fund_description` and learned from a previous `idFundo` query, plus the scope's registered
   legal name. This is exact comparison after folding — not the substring matching revision 3
   removed.
4. **Route as always.** For each entity that matched, the normal per-entity query with `idFundo` runs
   over the monitor window, and *that* is what enters the manifest. Nothing is ever archived from a
   row read in step 1.

Then fetch, supersede, inbox and purge exactly as any other run, and no global audit.

The cursor is the "trigger in cache": it is what makes the cost proportional to the *publication
rate* rather than to the day's volume or to the size of the watch list.

### Where the cursor lives

Two options, both viable.

- **A `listing_cursor` table in the manifest** (schema version 3), keyed by fund type, holding the
  newest delivery instant accounted for. Run state belongs with run state, and it is visible to
  `status`.
- **Nothing at all.** Without a cursor the monitor re-reads the whole monitor window each firing —
  one or two pages at present volumes — and relies on the manifest to tell it that the documents are
  already known. Simpler, and costs one extra page on a busy day.

Losing the cursor is harmless in either case: the fallback is one full read of the monitor window.

---

## 4. Cost

Let **E** be monitored entities, **V** the market-wide documents per day for the monitored fund
types, **F** the monitor firings per day, and **k** the monitored entities that filed since the last
firing (normally zero).

| | Per firing | Per day at F=17 |
|---|---|---|
| Monitor as implemented | E | 17 E |
| Monitor as proposed | ceil(new rows ÷ 200), min 1, + k | ≈ 17 + Σk |

Requests per day for the monitor alone, hourly from 07:00 to 23:00:

| Watch list | As implemented | As proposed |
|---|---|---|
| 5 entities | 85 | ~20 |
| 20 entities | 340 | ~20 |
| 50 entities | 850 | ~22 |

The proposed cost is **independent of the watch list**, which is the property that matters: today
the archive is small, and the current mechanism gets more expensive with every fund added.

Pages per firing against volume, hourly, assuming publication is concentrated into roughly eight
hours of the day:

| V (market-wide per day) | Rows per firing | Pages at `l=200` |
|---|---|---|
| 100 (measured this week) | ~12 | 1 |
| 540 (recorded 2026-08-14) | ~68 | 1 |
| 2 000 (month-end peak, hypothetical) | ~250 | 2 |
| 5 000 (implausible, for a bound) | ~625 | 4 |

A single page absorbs the recorded volume with room to spare, and the mechanism degrades linearly
rather than breaking. After an outage the read is bounded by the monitor window's total volume, not
by the length of the outage.

For comparison, one daily sweep over 20 entities plus its audit costs about 22 requests, and today's
three full runs a day cost about 66. The proposed monitor delivers hourly freshness for less than
what the current schedule already spends.

---

## 5. What does not change

- **Routing stays per-entity, keyed on `idFundo`.** Every document that enters the archive is still
  observed through a query whose answer is known from its question. No document is ever filed on the
  strength of a name.
- Retention, purge, the inbox, the supersede rules, the CNPJ check of §3.3, the state machine and
  the exit codes are untouched.
- The watermark rule holds and matters more, not less: the monitor queries a narrow window and only
  some entities, so it must never record progress. That is already derived from the two windows
  rather than from the profile flag.
- The daily sweep remains exactly what it is today, and remains the archive's completeness
  guarantee.

---

## 6. Trade-offs

### 6.1 A stated deviation from §4.5

The spec makes the global listing detective-only: it raises alerts, never routes a document and
never serves as a discovery path. The proposal keeps the first two and bends the third — the listing
does not route anything, but it does decide *which* per-entity queries the frequent profile spends.

The failure mode is bounded and worth stating plainly: a row the gate fails to match means the
monitor does not query that entity **this hour**. The daily sweep queries every entity
unconditionally, so the document arrives late rather than never. The deviation buys latency, and
pays for it in latency alone — never in completeness, and never in a misfiled document.

### 6.2 Name matching is back in the hot path

Not in the routing path, but it is there, and revision 3 exists because name matching failed
silently. Each way it can fail here, and what absorbs it:

| Failure | Effect | Absorbed by |
|---|---|---|
| A new class publishes under a name not yet in `funds.yaml` | Monitor misses it | The sweep files it; the audit raises the alert |
| The source renames a fund between sweeps | Monitor misses it until the next sweep refreshes the spelling | The sweep |
| An entity registered but never yet queried has no stored spelling | Matched only against the registered legal name, which may differ | The first sweep populates it |
| `descricaoFundo` empty on a row | No match possible | The sweep |

None of these lose a document. All of them make the monitor late, which is the same as not having a
monitor for that document — the situation today.

### 6.3 The sweep becomes load-bearing

Today the sweep is the only mechanism, so disabling it obviously stops everything. Under the
proposal it is also the backstop for every gate failure above, while the archive keeps looking
current because the monitor is filing documents hourly. Disabling `SWEEP_ENABLED` should therefore
be harder to do quietly than it is now — at minimum the staleness warning already fires on every
run, since a monitor never advances a watermark.

### 6.4 Descending order becomes load-bearing, on one measurement

The clean descending pagination above is a single reading, taken on 2026-08-27 over a 333-row
window at `l=50`. §9.5's lesson is precisely that this endpoint's ordering behaviour varies by
parameter and fails silently when it varies. Mitigations: coverage is asserted on distinct
`(id, versao)` and never on row count, which is the check that caught the original loss; and the
descending read must be pinned by a live contract test, re-measured with `pytest -m live` rather
than trusted indefinitely. If descending is ever withdrawn, the same read can be done on the
verified ascending sort by offset arithmetic from the end, at the cost of one extra request to learn
`recordsFiltered`.

### 6.5 Smaller hazards

- **Minute-granularity ties.** `dataEntrega` resolves to the minute, so several documents can share
  the boundary instant. The stop rule must not halt inside a tie group; deduplication is on
  `(id, versao)`, never on the timestamp.
- **A shrinking total.** When a re-filing replaces v1 with v2, v1 disappears from the listing and
  `recordsFiltered` drops. A multi-page descending read taken across that moment can shift rows
  between pages. In the common single-page case this cannot happen at all, and the sweep covers the
  rest.
- **Re-filings arrive at the newest end.** A v2 carries its own, later `dataEntrega`, so the monitor
  sees supersessions of documents delivered days earlier — a small bonus, not a guarantee.
- **Two discovery mechanisms to maintain** instead of one, each with its own tests. This is the real
  ongoing cost of the proposal.
- **Weekends are free**: zero rows, one request, nothing matched.

---

## 7. Open questions, to answer empirically before or during implementation

Marked for `pytest -m live`, in the spirit of section 9 of the architecture document.

1. Descending pagination on a window of **more than 200 rows at `l=200`** — measured so far only at
   `l=50`.
2. Actual volume on a **month-end day**, when the income announcements land, and the resulting page
   count per firing.
3. Whether `recordsFiltered` for a fixed past window ever **decreases**, and by how much, over a day
   of observation.
4. Whether `descricaoFundo` is stable enough, and populated on every row, to carry a gate — and how
   often it diverges from the spelling stored in `funds.yaml`.
5. Whether the endpoint's descending sort is honoured for **every fund type**, not only `tipoFundo=1`.

---

## 8. The three options

| | Keep the per-entity monitor | Adopt the listing read | Drop the monitor |
|---|---|---|---|
| Cost per firing | one request per entity | one request, plus one per fund that filed | — |
| Scales with watch list | yes, badly | no | — |
| Deviates from §4.5 | no | yes, stated and bounded | no |
| Name matching in the path | no | as a gate only | no |
| Freshness | hourly, expensively | hourly, cheaply | every 6–8 hours |
| New state | none | a cursor, or none | none |
| Mechanisms to maintain | one | two | one |

**Recommendation: adopt the listing read.** The per-entity monitor is redundant with the sweep by
construction — it does the same work more often — while the listing read is a genuinely different
question asked of the source, and the only one whose cost does not grow with the archive. The
deviation it requires is narrow, it never touches routing, and its worst case is the latency the
robot already lives with today.

What survives from the work already on the branch: the `[discovery]` configuration and its ordering
rule, the `--monitor` profile, the watermark rule, the two-profile scheduler and its compatibility
with the deployment's existing variables, and the documentation and tests around all of it. What
would be replaced is the monitor's discovery step alone.
