# Resilient Web Data Collection Lab — Case 002

Offline incremental crawler for one allowlisted catalog fixture. Each horizon revalidates stored pages with `ETag` conditional GET, classifies byte changes as cosmetic or material, and keeps a SQLite frontier that can resume after a crash. Nothing in this lab opens a socket or contacts a third-party site.

## Problem

A weekly catalog pull wastes its page budget when it downloads unchanged HTML, and it double-counts work when a process dies after a request has been committed but before the response is stored. The collector needs a durable frontier, a fixed gap between requests to the same host, and a way to tell a timestamp edit from a rewritten page.

The fixture origin is `https://catalog.example.invalid`. The product token is `FrontierBot`. The demo runs two virtual weeks from 2026-10-05 00:00:00 UTC. Week 1 discovers `/catalog/` plus three catalog pages and refuses `/private/cost`, `/secret`, and any other origin. Week 2 receives `304` for the index and the linen page, a gzip-compressed timestamp edit for the wool page, and a rewritten field-notes page.

## Architecture

```
examples/site.json  ->  FixtureOrigin  ->  FixtureClient  ->  Crawler
                                                              |  observe, robots, backoff
                                                              v
                                                         SQLite frontier
                                                              |
                                                              v
                                                         report.json
```

- `ManualClock` is the only clock. `sleep` advances that clock. The lab does not call the wall clock and does not pause the operating system.
- `FixtureOrigin` serves `robots.txt` and HTML in process, including `gzip`, `deflate`, and UNIX `compress` (LZW, block mode off).
- `Store` uses SQLite. A file-backed frontier sets `journal_mode=WAL` and `synchronous=FULL`, and writes inside `BEGIN IMMEDIATE`. The in-flight mark and the host's next-request time commit before the fixture is called. Opening the file does not recover a run; `Crawler.run` does.
- The crawler fetches one URL at a time. Newly admitted URLs go before refreshes while the remaining page budget is above the audit floor (`max(1, budget // 10)`). The floor keeps the oldest synced URLs. The other due URLs are ordered by the configured objective (`freshness` by default, or `age`, `uniform`, `proportional`).
- `observe` unwraps `Content-Encoding`, hashes the raw bytes with SHA-256, and decodes text with the charset declared on `Content-Type`. Simhash then separates cosmetic edits from material rewrites.
- The report's freshness and age use the fixture's change log and the sync events whose detail has `oracle_sync`. A weak `304` updates scheduling state and still spends a page, and it is omitted from that oracle.

## Run

From the repository root (one line, any shell):

```text
python projects/resilient-web-collection-lab-case-002/run_lab.py --config projects/resilient-web-collection-lab-case-002/examples/config.json --site projects/resilient-web-collection-lab-case-002/examples/site.json --dry-run
```

`--dry-run` keeps the frontier in memory and prints one JSON object. Exit 0 is a finished run (or `--help`), exit 2 is a bad argument or config, and exit 3 is `--crash-at pre_request` or `--crash-at post_result`.

A durable run writes `crawl.sqlite`, `report.json`, and `collection.jsonl`:

```text
python projects/resilient-web-collection-lab-case-002/run_lab.py --config projects/resilient-web-collection-lab-case-002/examples/config.json --site projects/resilient-web-collection-lab-case-002/examples/site.json --state-dir projects/resilient-web-collection-lab-case-002/state
```

`--log-jsonl PATH` writes the same events stored in SQLite. `--weeks` defaults to 2.

Verification, also from the repository root:

```text
python -m unittest discover -s projects/resilient-web-collection-lab-case-002/tests -v
```

The dry-run report for the checked-in fixture is:

| Field | Value |
| --- | --- |
| requests | 10 |
| not_modified_304 | 2 |
| cosmetic_change_count | 1 |
| material_change_count | 1 |
| byte_change_count | 2 |
| robots_blocks | 2 |
| retries | 0 |
| page_budget_left | 4 |
| retry_budget_left | 8 |
| rate_censored_urls | 1 |
| possible_soft_errors | 0 |
| simhash_k | 23 |
| freshness_source | fixture_oracle |
| time_average_freshness | 0.9999917325650126 |
| time_average_age | 0.00010334319365445983 |

Age and freshness are measured in seconds on `[first horizon start, last horizon end]`. The wool and field-notes bodies change at the week-2 boundary, a few seconds before those URLs are fetched again, which is why freshness is just under 1 and the average age is a fraction of a second.

## Design decisions

- The same-host gap defaults to 10 seconds and covers robots fetches, catalog fetches, and retries. `Crawl-delay` is outside the robots standard this parser implements, so a `Crawl-delay` line does not change the gap and does not end a group.
- Retry delay is full jitter: `uniform(0, min(cap, base * 2^attempt))` with attempt 1 on the first retry. A parsed `Retry-After` is a lower bound, and the jitter is added on top. A `Retry-After` that is neither delta-seconds nor an HTTP-date is ignored, so plain full jitter applies. A required wait above 3600 seconds defers that host until the horizon ends; the clock does not advance by the full delay.
- A stored `ETag`, weak or strong, is sent only as `If-None-Match`. `If-Modified-Since` is sent when the row has `Last-Modified` and no `ETag`. Changing `Accept` or `Accept-Language` drops the validator.
- A strong `304` keeps the stored body and checksum, refreshes validators, and counts as a no-change sample. A weak `304` does the same bookkeeping for the visit and does not set `byte_identity_known`, does not move `material_change_count`, and does not count as an oracle sync.
- `gzip`, `deflate`, and UNIX `compress` are removed before SHA-256. An unknown coding or compress block mode fails the fetch, so compressed bytes are never hashed.
- URLs are canonicalized before they become frontier keys: lowercase scheme and host, default port removed, userinfo and fragment dropped, relative links resolved against the page URL. A link with a malformed authority (for example a non-numeric port) is skipped. A catalog `3xx` admits its target as a new URL with no validator carried over, and only when the target is on the same origin and the allowlist; otherwise the target is logged as `off_origin` and never requested.
- The charset is the `Content-Type` parameter. A body that does not decode with the declared charset is treated like a missing charset. A missing or unknown charset stores the checksum, stores `simhash` as null, and still extracts links with a Latin-1 decode. A `<meta charset>` tag is ignored.
- Simhash is 64-bit. Features are casefolded `[0-9A-Za-z]+` tokens with the stopword list removed. Each feature hash is the first 8 bytes of BLAKE2s. Bit `i` is 1 only when that coordinate is strictly positive, so a zero coordinate stays 0. On this corpus the timestamp edit is Hamming distance 6, the ad edit is 4, and the paragraph edit is 24, so `simhash_k` is 23. A cosmetic checksum change at distance `<= k` does not increment the material count or the change rate.
- The empty body and the fixture's not-found page are soft errors: the bytes are stored, the material count stays put, and the visit is not a rate sample.
- Importance is `1 / (1 + depth)`. When the collection is full, a new URL replaces the lowest-importance live or pending URL outside the audit set only if the new importance is strictly higher. `404` and `410` mark the URL gone and free the slot.
- The per-URL rate is material changes divided by the span from the first comparable observation to the latest. Zero changes yield rate 0, so those URLs are reached through the audit floor. A URL whose every comparable sample changed is censored to at least one change per horizon. The field-notes page is the censored URL in the demo.
- The default refresh score is `w * (1 - e^(-λτ)) * (1 - e^(-λT)) / (λT)`. The age score is `w * (τ - (1 - e^(-λτ)) / λ)`. Both are built from the expected-freshness and expected-age functions in `estimate.py`, and both are 0 when `λ` or `τ` is 0. The crawler and the simulation share these scores; the simulation uses its own batch selector over the same audit floor, not the crawler loop. `tests/test_schedule.py` runs the eight-week comparison (budget 25, horizon 7 days, the 50/30/20 rate mix, seed 20261005): proportional freshness is below uniform, the freshness policy is at least the uniform freshness, and the age policy's time-average age is below the freshness policy's age.
- A crash after the in-flight commit and before send leaves the row `in_flight` and does not charge the page budget. The next `run` sets it back to `pending`, honors `next_request_at`, repeats the GET once, and charges once. A crash after the result commit leaves the row live; a later run inside the same horizon does not refetch it and does not refill the budget. `GET` is repeated because the request is idempotent.
- `429` and `431` spend retry budget and do not spend page budget. `431` is retried with the one validator the client already sends.

## Limitations

- The origin is the in-process fixture. A resumed process rebuilds that fixture from `examples/site.json` and applies horizon revisions again; the durable artifact is the frontier database and the files written under `--state-dir`.
- The freshness oracle is computed in the process that observed the fixture change log.
- One virtual clock serves every host, so a cooldown on one host pauses selection until that instant.
- `simhash_k` 23 is the cut that separates this corpus. It is not a web-scale constant.
- There is no real HTTP client. `FixtureClient` calls the in-process origin, so DNS, TLS, sockets, and wall-clock timeouts are not exercised; timeouts are injected faults.
- Robots caching follows `Cache-Control` and `Expires` on the robots response. An `Expires` value that is not an HTTP-date, such as `0`, counts as already expired. A catalog `Cache-Control: max-age` does not skip a URL that is due.
- The UNIX compress reader accepts block mode off and widths 9 through 16. Other compress dialects fail the fetch.

## What it demonstrates

A Python collection loop, exercised against an in-process fixture, with an allowlist, URL canonicalization, robots matching, a fixed host gap, full-jitter retries, content-encoding removal, charset-aware normalization, ETag revalidation, cosmetic versus material change detection, censored change rates, freshness and age scheduling, durable checkpoints, and deterministic offline tests.
