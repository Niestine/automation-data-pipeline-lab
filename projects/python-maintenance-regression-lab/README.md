# Python Maintenance & Regression Lab

A compact, fully offline catalog-maintenance service. Weekly supplier dumps arrive as mixed CSV/JSON with encoding drift, v1/v2 field names, decimal-comma prices, and poison rows. The pipeline decodes bytes defensively, parses with a real CSV/JSON reader, adapts rows onto a canonical product record, applies versioned SKU updates, and writes structured logs. Sixteen historical defects stay locked behind named regression tests.

No network calls are made. Catalog rows and supplier feeds are synthetic.

## Problem

Maintenance work fails in the gaps between "the import script printed OK":

- a Japanese supplier still ships cp932, and a UTF-8 decode either crashes or stores mojibake
- v1 CSV uses `product_name` / `qty` / `image`; v2 JSON uses `title` / `stock` / `image_url`
- empty quantity must mean "leave on-hand stock alone", not zero
- `bool("false")` is True, so discontinued rows stay active
- `float("19.99") * 100` stores 1998 cents and the next weekly diff looks like a price change
- Sunday 16:00 UTC is Monday in JST, so a local-time week window bills the row to the wrong ISO week
- re-importing the same dump must not double stock
- operators need a dry-run that walks the same adapters without writing durable state

This project is a standard-library-only sketch of that loop. It is a teaching/portfolio sample, not a production PIM.

## Architecture

```
Feed bytes
  -> decode (BOM, UTF-8, cp932 if Japanese, else latin-1)
  -> preamble (# version= currency= delimiter= encoding=)
  -> parse CSV (sniffed delimiter, quoted fields) or JSON v2
  -> compatibility adapter (v1 field maps, Decimal money, SKU case-fold, token booleans)
  -> reject poison rows (relative image, slash date, short SKU)
  -> apply against a versioned SKU catalog
       insert / update / replay / stale / conflict
       stock and active omitted => unchanged
  -> feed sha256 + ISO week ledger (idempotent re-import)
  -> atomic catalog JSON + per-feed checkpoint (crash resume)
```

Package layout:

- `src/maintenance_lab/decode.py` — encoding sniff, BOM, Japanese vs latin-1 heuristic
- `src/maintenance_lab/parse.py` — CSV dialect sniff, quoted commas/newlines, JSON v2
- `src/maintenance_lab/compat.py` — v1→canonical remaps, SKU/title/image coercions, catalog diff
- `src/maintenance_lab/money.py` — Decimal minor units (USD/EUR cents, JPY yen)
- `src/maintenance_lab/window.py` — UTC ISO-8601 weeks, half-open `[start, end)`, ISO dates only
- `src/maintenance_lab/apply.py` — orchestrator, dry-run overlay, crash injection
- `src/maintenance_lab/catalog.py` / `ledger.py` / `checkpoint.py` — durable JSON with `os.replace`
- `src/maintenance_lab/telemetry.py` — injectable clock, structured JSON events
- `src/maintenance_lab/bugs.py` — historical defect registry wired to regression tests
- `examples/` — frozen catalog plus the six weekly dumps as raw bytes (`supplier_v1_jp.cp932` and `supplier_latin1.latin1` keep their legacy encodings). `supplier_v1_jp.csv` and `supplier_latin1.csv` are UTF-8 copies for reading on GitHub; the Japanese copy still declares `encoding=cp932`, so passing it as `--feed` fails with a `decode_error` rather than storing mojibake. A test keeps every example file byte-identical to `seed.py`

Maintenance workflow the tests encode:

1. Reproduce the failure with a named `test_bug_0xx_*` method.
2. Isolate the layer (decode / parse / compat / money / window / apply).
3. Guard the dangerous input shape (`bug_guard` on the structured log).
4. Re-run the suite; `HistoricalBugTests.test_every_bug_has_a_regression_method` fails if a registry entry loses its test.

## Run

From the repository root (Python 3.10+, standard library only, no network access needed):

```text
python -m unittest discover -s projects/python-maintenance-regression-lab/tests -v
```

Offline demo against the synthetic weekly dumps:

```text
python projects/python-maintenance-regression-lab/run_lab.py
python projects/python-maintenance-regression-lab/run_lab.py --dry-run
python projects/python-maintenance-regression-lab/run_lab.py --list-bugs
python projects/python-maintenance-regression-lab/run_lab.py --state-dir <state-dir> --crash-after-rows 1
python projects/python-maintenance-regression-lab/run_lab.py --state-dir <state-dir>
python projects/python-maintenance-regression-lab/run_lab.py --compat-from <a.json> --compat-to <b.json>
python projects/python-maintenance-regression-lab/run_lab.py --log-jsonl 2> <log-file>
```

`<state-dir>` is any writable directory outside the repository (for example a temp folder). Without `--state-dir` the CLI creates a fresh temp directory and reports it as `state_dir` in the output. `--dry-run` never creates or writes files: without `--state-dir` it previews against `examples/catalog.json` and reports `"state_dir": null`; with `--state-dir` it reads the existing catalog and ledger there read-only, so the preview matches what a real rerun would do.

The output JSON is printed as UTF-8 when the console can encode it and falls back to ASCII-escaped JSON on consoles such as Windows cp932, so non-ASCII titles never crash the CLI after state has been written.

The default clock is `2026-01-04T12:00:00Z` (`1767528000000` ms), Sunday of ISO week `2026-W01`. Default feeds are built in-memory from `src/maintenance_lab/seed.py` (UTF-8 v1 CSV, cp932 Japanese CSV, EU semicolon CSV, v2 JSON, latin-1 Café row, quoted-newline title). The starting catalog is `examples/catalog.json` (four SKUs).

A successful default run reports `inserted: 8`, `updated: 3`, `replayed: 2`, `conflict: 1`, `rejected: 3` and a durable `catalog_size` of 12. New SKUs are `SKU-1003`, `SKU-1004`, `SKU-1007`, `JP-2002`, `EU-3002`, `SKU-2001`, `EU-4001`, and `SKU-9001`. `SKU-1001` moves to 2150 cents / stock 12, `SKU-1002` stays at stock 5 even though qty is empty, `JP-2001` moves to 4800 yen, and `EU-3001` moves to 1490 euro-cents. Poison rows (`XX`, relative image, slash date) are rejected. The second `sku-1003` line case-folds onto `SKU-1003` and conflicts because the title differs at the same timestamp. `--dry-run` shows the same counts with `catalog_size: 4` and an empty compatibility diff.

Logging: every event is a flat JSON object with `event`, `ts_ms` (from the injectable clock), and fields such as `sku`, `status`, `remaps`, and `bug_guards`. `row_rejected` events also carry the rejected `field`, the error `message`, and the physical CSV `line` number so a bad row can be found in the source file. `--log-jsonl` streams events to stderr as JSON Lines; `--print-events` embeds them in the final stdout report.

Exit codes: `0` the run finished (rejected poison rows still count as a completed ingest), `3` simulated crash (prints a small JSON error object; durable catalog, ledger, and checkpoint are left behind; rerun with the same `--state-dir` and no crash flag to resume), `2` missing/malformed `--catalog` / `--feed` files or incomplete `--compat-*` flags, `1` a runtime lab error such as an undecodable or unparseable feed, a corrupt catalog or checkpoint, or a `--fail-fast` row rejection. Errors print a one-line message rather than a traceback.

`--list-bugs` prints the sixteen-entry registry. `--compat-from` / `--compat-to` diffs two catalog snapshots and flags removed SKUs and currency changes as breaking.

## Design decisions

- **Layered ingest.** Decode, parse, adapt, and apply are separate modules so a regression test can pin the layer that actually broke.
- **Shifted rows are rejected.** A CSV record with more non-empty cells than the header (an unquoted delimiter) is rejected with `BUG-002` instead of silently dropping the trailing cells.
- **Row bounds match the catalog schema.** `price_cents` and `image_url` length limits are enforced on ingest, so an accepted row can always be reloaded from disk.
- **Canonical SKU identity.** SKUs strip and uppercase before lookup. Length is 3–32; longer values are rejected rather than sliced to 12 characters.
- **Integer money.** USD/EUR store cents, JPY stores yen. `Decimal` parsing refuses extra fractional digits. A semicolon-delimited CSV treats comma as the decimal mark.
- **Empty qty is not zero.** Missing/blank stock means unchanged on update and 0 only when inserting a brand-new SKU. Explicit `0` is out of stock.
- **Token booleans.** `false` / `0` / `no` become boolean False. JSON native booleans keep their origin so the string bug is attributed to CSV tokens.
- **Stale vs conflict vs replay.** Older `updated_at` is stale. Equal timestamp and equal body is replay. Equal timestamp and different body is conflict. Feed `sha256` + ISO week is the idempotency key for a whole dump.
- **UTC ISO weeks.** `datetime.fromtimestamp` without a timezone follows the host zone; this lab never does that. Weeks are Monday 00:00Z half-open intervals.
- **ISO dates only.** Slash dates are rejected so `07/01/2026` cannot flip between 7 January and 1 July.
- **Dry-run overlay.** Catalog and ledger mutations run against an in-memory copy and are discarded. Checkpoints are not written.
- **Crash resume.** `--crash-after-rows N` raises after N successful inserts/updates. The checkpoint stores `last_row`; only an `in_progress` checkpoint for the same feed sha256 is resumed, skipping through that index. Already-applied SKUs keep their version. A completed feed is skipped by the ledger, and `--force` re-walks every row (which then reports `replayed` for unchanged rows).
- **Structured bug guards.** When a historically dangerous input shape is handled, the row outcome lists the bug id (`BUG-005` on empty qty, `BUG-001` on cp932 fallback, and so on).

## Limitations

- There is no live supplier HTTP client, spreadsheet GUI, or hosted catalog API in this repository.
- Encoding sniff covers UTF-8 (with BOM), cp932 when the payload looks Japanese, and latin-1 as a last resort. It does not detect UTF-16 or GBK.
- Durable state is JSON files (or in-memory), written to a temp file, fsynced, and swapped in with `os.replace`. They are not a database and are not safe for concurrent writers.
- Time is an injectable, frozen `ManualClock`. The CLI clock defaults to `2026-01-04T12:00:00Z` and can be moved with `--now-ms`; the lab never reads the wall clock, which keeps runs reproducible.
- Image URLs are checked for an `http`/`https` scheme and a host. The lab does not fetch them.
- Compatibility diffs compare catalog snapshots; they do not generate database migrations.
- This is a teaching/portfolio sample, not a production product-information system.

## What it demonstrates

- A layered Python service with decode / parse / compatibility / apply boundaries
- Defensive parsing of encodings, CSV quotes, decimal-comma prices, and token booleans
- v1/v2 field maps and a catalog compatibility diff that flags breaking currency/SKU removals
- Regression tests named after sixteen historical defects, plus a registry completeness check
- Idempotent weekly re-import, stale-snapshot protection, and dry-run overlays
- UTC ISO-week windows and ISO-8601-only timestamps
- Structured JSON logs with `bug_guards`, atomic checkpoints, and crash resume
- Offline unittest coverage with synthetic fixtures only
