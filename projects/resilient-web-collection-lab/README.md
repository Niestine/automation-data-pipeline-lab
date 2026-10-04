# Resilient Web Data Collection Lab

A fully offline collection pipeline against a local HTML fixture site. The collector fetches `robots.txt`, honors `Disallow` and `Crawl-delay`, spaces requests with a token-bucket limiter, retries transient HTTP faults, parses listing and product pages, normalizes money and text, diffs against last week's snapshot, and writes a tool-usable UTF-8 CSV.

No live third-party site is contacted. The fixture speaks HTTP request/response shapes in-process and never opens a socket. Catalog rows, robots rules, faults, and the previous snapshot are synthetic.

## Problem

Weekly extract jobs fail in the gaps between "the happy-path GET printed a table once":

- a crawler that ignores `robots.txt` will fetch `/private/` pages the operator never meant to collect
- unthrottled loops trip 429s; 429/5xx/timeouts should retry with backoff, while 400/404 must not
- listing HTML has extra whitespace, `&amp;`, `&nbsp;`, relative image URLs, and mixed `$29.00` / `24,50 EUR` / `¥2,400` prices
- a latin-1 product page (`Café`) must not become mojibake in the CSV the downstream tool reads
- `float("19.99") * 100` stores 1998 cents and the next weekly diff looks like a price change
- a display price that disagrees with `data-cents` is poison, not a bargain
- last week's SKU set must produce added / removed / field-level changed, not a full rewrite
- a crash after two product fetches must resume without dropping or duplicating rows

This project is a compact, standard-library-only sketch of that loop. It is a teaching/portfolio sample, not a production crawler.

## Architecture

```
CollectJob
  -> GET /robots.txt
       parse User-agent groups, longest-match Allow/Disallow, Crawl-delay (capped)
       token-bucket min interval = max(config, robots crawl-delay)
  -> walk GET /catalog?page=  (each listing page is robots-checked first)
       html.parser cards + rel=next
       skip off-origin and robots-denied hrefs
  -> GET /products/{slug}  (If-None-Match when an ETag is on file)
       decode charset (Content-Type, then meta, then utf-8 / latin-1)
       parse article.product
       normalize SKU / title / money / availability / image URL
       reject poison rows (hash/schema/`data-cents` mismatch)
  -> snapshot diff vs previous week (added / removed / changed / unchanged)
  -> atomic JSON snapshot + RFC 4180 CSV
  -> crash injection after N product fetches; resume skips completed paths
```

Package layout:

- `src/web_collection_lab/fixture_site.py` — in-process HTML catalog, robots, ETags, one-shot faults
- `src/web_collection_lab/robots.py` — robots.txt groups, prefix matching, crawl-delay cap
- `src/web_collection_lab/rate_limit.py` — token bucket plus minimum interval
- `src/web_collection_lab/retry.py` / `transport.py` — retry classification, Retry-After, seeded backoff
- `src/web_collection_lab/decode.py` / `parse.py` — charset sniff, `html.parser` listing and product pages
- `src/web_collection_lab/normalize.py` — Decimal money, SKU/title/URL coercions
- `src/web_collection_lab/detect.py` — field-level snapshot diff (`collected_at` excluded)
- `src/web_collection_lab/collector.py` — orchestrator, dry-run, fail-fast, simulated crash
- `src/web_collection_lab/csv_export.py` / `catalog.py` / `checkpoint.py` — atomic UTF-8 writes
- `examples/` — frozen catalog, previous snapshot, robots.txt, fault script

## Run

From the repository root (Python 3.10+, standard library only, no network access needed):

```text
python -m unittest discover -s projects/resilient-web-collection-lab/tests -v
```

Offline demo against the synthetic fixture:

```text
python projects/resilient-web-collection-lab/run_lab.py
python projects/resilient-web-collection-lab/run_lab.py --dry-run
python projects/resilient-web-collection-lab/run_lab.py --state-dir <state-dir> --crash-after-products 2
python projects/resilient-web-collection-lab/run_lab.py --state-dir <state-dir>
python projects/resilient-web-collection-lab/run_lab.py --force
python projects/resilient-web-collection-lab/run_lab.py --log-jsonl 2> <log-file>
```

`<state-dir>` is any writable directory outside the repository (for example a temp folder). The third and fourth commands show crash + resume: the rerun with the same `--state-dir` and no crash flag picks up from the checkpoint. Without `--state-dir` the CLI creates a fresh temp directory and reports it as `state_dir`. `--dry-run` never creates or writes files and reports `"state_dir": null`.

The output JSON is printed as UTF-8 when the console can encode it and falls back to ASCII-escaped JSON on consoles such as Windows cp932, so `Café Apron` never crashes the CLI after state has been written.

The default clock is `2026-01-04T12:00:00Z` (`1767528000000` ms). Default fixture rows come from `examples/catalog.json` (six sellable products plus a poison card and a robots-denied private link). Change detection uses `examples/previous_snapshot.json` (stamped `2025-12-28T12:00:00Z`, one week earlier): `SKU-1001` still at 2800 cents, `SKU-1002` and `SKU-1005` unchanged, `SKU-OLD1` retired.

The default demo loads `examples/fault_script.json`: a 503 on `/robots.txt`, a 429 with `Retry-After: 0` on `/catalog`, and a timeout on `/products/sku-1001`. A successful default run reports `listing_pages: 2`, `products_parsed: 6`, `products_rejected: 1`, `robots_skipped: 1`, `retries: 3`, `added: 3`, `removed: 1`, `changed: 1`, `unchanged: 2`, and `csv_rows: 6`. New SKUs are `SKU-1003`, `SKU-1004`, and `SKU-1006`. `SKU-1001` moves from 2800 to 2900 cents. `SKU-OLD1` is gone. `/private/hidden` is discovered on the listing and never fetched. `/products/poison` is fetched and rejected because `data-cents` disagrees with the displayed price.

`--dry-run` shows the same counts with no snapshot, CSV, or checkpoint files.

To exercise ETags, pass a written snapshot back in as the previous week: `run_lab.py --state-dir <new-dir> --snapshot <state-dir>/lab-collect.snapshot.json` sends `If-None-Match` and reports `products_not_modified: 6`, `unchanged: 6`. Adding `--force` skips `If-None-Match` and refetches every product body (`products_not_modified: 0`).

Logging: every event is a flat JSON object with `event`, `ts_ms` (from the injectable clock), and fields such as `path`, `sku`, `charset`, and `waited_ms`. `--log-jsonl` streams events to stderr as ASCII-escaped JSON Lines (safe on non-UTF-8 consoles); `--print-events` embeds them in the final stdout report.

Exit codes: `0` the run finished (rejected poison rows still count as a completed collect), `3` simulated crash (prints a small JSON error object; durable snapshot and checkpoint are left behind; rerun with the same `--state-dir` and no crash flag to resume), `2` missing/malformed `--catalog` / `--faults` / `--snapshot` / `--robots` files or out-of-range arguments, `1` a runtime lab error such as a non-retryable HTTP error, a schema failure under `--fail-fast`, or a corrupt checkpoint. Errors print a one-line message rather than a traceback.

Image URLs are recorded on the canonical row. The lab does not download image bytes.

## Design decisions

- **HTTP shapes without sockets.** `InProcessTransport` keeps tests deterministic and offline while still exercising status codes, headers, ETags, and bodies.
- **Robots before listings.** Listing pages and product pages are both checked against robots.txt before they are fetched. The crawler's product token (`LabCollector`) is matched exactly and case-insensitively, as in RFC 9309, so a `LabCollectorX` or `L` group does not apply; several groups naming the same agent are merged. The LabCollector group wins over `*`. Longest matching path rule wins; Allow beats Disallow on a tie. Empty `Disallow:` allows all. `/private` does not match `/privateer`. `Crawl-delay` is converted to milliseconds and capped at 60s so a malicious robots file cannot stall the job.
- **Rate limit every request, retries included.** Crawl-delay and `--min-interval-ms` gate `SiteClient.get`. A retry first sleeps for Retry-After / exponential backoff and then also waits for the limiter, so `Retry-After: 0` on a 429 cannot bypass Crawl-delay. Limiter waits are rounded up to whole milliseconds so a fetch is never admitted early. Burst > 1 allows a short front-load.
- **Retry only what is safe.** GET retries on 408/425/429/500/502/503/504 and transport timeouts. 400/401/403/404/422 never retry. `Retry-After` (delta-seconds) / `X-Retry-After-Ms` override backoff and are capped by `RetryPolicy.max_retry_after_ms`. HTTP-date `Retry-After` values fall back to backoff. Jitter is seeded so tests are deterministic.
- **Charset then parse.** Bytes are decoded from the Content-Type charset, then `<meta charset>`, then utf-8, then iso-8859-1. SKU-1003 is served as iso-8859-1 `Café Apron` so the latin-1 path is exercised.
- **Integer money.** USD/EUR store cents, JPY stores yen. Amounts must be plain digits with an optional fraction (no `NaN`, `Infinity`, exponents, or signs) before `Decimal` parsing, which refuses extra fractional digits. EUR accepts decimal commas; JPY accepts thousands commas and rejects a decimal point.
- **`data-cents` is a checksum.** When the attribute is present it must equal the parsed display price. The poison product (`SKU-POISON`, `$29.00` vs `data-cents="1"`) is rejected.
- **Same-origin hrefs only.** `javascript:` and off-host links are dropped. Image URLs are resolved with `urllib.parse.urljoin` against the product URL and must stay on `https://fixture.example.invalid`.
- **Weekly identity is the content hash.** `collected_at` is the job start stamp (not per-row fetch time) so a CSV regenerates without a false diff. A resumed job keeps the interrupted run's stamp, so one extract never mixes two stamps. Field-level diffs ignore `collected_at`.
- **Listings always refetch; product pages use ETags.** New cards can appear on `/catalog`. Product `If-None-Match` yields 304 and reuses the previous canonical row.
- **Dry-run stages, never commits.** Checkpoints, snapshots, and CSV files are not written. In-memory counts still match a real run.
- **Crash resume.** `--crash-after-products N` raises after N product HTTP fetches. The checkpoint stores `completed_paths` in fetch order; resume re-walks listings and skips those paths. Already-written snapshot rows are kept. A fresh (non-resumed) run starts from an empty snapshot so rows from an older run cannot leak into a new extract. Counters in a resumed run's report (`products_parsed`, `retries`, ...) cover only that invocation; `csv_rows` and the diff cover the whole snapshot.
- **User-Agent is required.** The fixture returns 400 when it is missing. The default is `LabCollector/1.0 (+https://collection.example.invalid/lab)`.

## Limitations

- There is no live HTTP server, public website, or third-party scrape in this repository.
- Backoff and crawl-delay sleeps are injectable. The default `WallClock` really sleeps; the CLI and tests use a recording sleeper that advances a `ManualClock`.
- The snapshot, CSV, and checkpoint are JSON/text files (or in-memory). They are not a database and they are not multi-process-safe beyond `os.replace` on a single host.
- `https://fixture.example.invalid` and `https://collection.example.invalid` are reserved RFC 2606 names. They are not real hosts.
- Image files are stubs. The collector records canonical image URLs and does not fetch `/images/*`.
- robots.txt coverage is User-agent / Allow / Disallow / Crawl-delay. `*` and `$` path patterns are not expanded (they are compared as literal prefixes), percent-encoding is not normalized, and `Sitemap` is ignored.
- Fail-fast on a poison row that remains in the fixture will fail again on resume until the source is fixed.
- Time is an injectable, frozen `ManualClock`. The CLI clock defaults to `2026-01-04T12:00:00Z` and can be moved with `--now-ms`.

## What it demonstrates

- A respectful Python collector with robots.txt, crawl-delay, and a token-bucket limiter
- Exponential backoff, Retry-After, and non-retryable 4xx handling against an HTML fixture
- `html.parser` listing pagination and product extraction, including entities and messy whitespace
- Charset-aware decoding of an iso-8859-1 page without storing mojibake
- Decimal money normalization for USD / EUR / JPY and `data-cents` checksums
- Weekly change detection (added / removed / field-level changed) and ETag 304 reuse
- Tool-usable RFC 4180 CSV (quoted commas, UTF-8, stable column order)
- Dry-run, fail-fast, structured logs, crash resume, and offline unittest coverage
