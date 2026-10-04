# API Integration Reliability Lab

A fully offline simulation of a REST/webhook integration. A local mock fulfillment API serves cursor-paginated orders and accepts idempotent shipment acks. The client walks pages, retries transient faults, validates every payload, upserts into a versioned ledger, checkpoints progress, and applies signed webhook snapshots. A simulated crash mid-page is recovered by replaying the same cursor; idempotent writes keep the ledger unique.

No hosted API is called. The mock lives in-process and speaks HTTP request/response shapes without opening a socket. Catalog rows, webhook events, and fault scripts are synthetic.

## Problem

Production integrations fail in the gaps between "the happy-path GET worked once":

- list endpoints page, and a crash between pages must resume without skipping or duplicating records
- 429/5xx/timeouts should retry with backoff; 401/400/409/422 must not
- POST retries are unsafe unless an Idempotency-Key is present and stable across process restarts
- source payloads lie: extra fields, broken envelopes, NaN, amount mismatches
- webhooks are at-least-once, out of order, and trivially forgeable without HMAC + a replay window
- a destination ledger has to apply snapshots by version, not by arrival time

This project is a compact, standard-library-only sketch of that loop. It is a teaching/portfolio sample, not a production service.

## Architecture

```
SyncJob
  -> GET /health
  -> snapshot phase
       GET /v1/orders?cursor&limit   (keyset pagination on updated_at,id)
       schema-validate envelope and each order
       ledger.upsert by (id, version)
       POST /v1/acks with Idempotency-Key  {sync_id}:ack:{order_id}:{version}
       atomic JSON checkpoint of cursor / pages_done
  -> webhook phase
       HMAC-SHA256 over "{timestamp}.{raw_body}"
       replay window, event-id dedup, version compare
  -> crash injection (optional) at post_upsert | post_ack | post_checkpoint
  -> resume from checkpoint + durable ledger
```

Package layout:

- `src/api_reliability_lab/mock_service.py` — in-process source API, one-shot faults, idempotent acks
- `src/api_reliability_lab/client.py` — REST client, page walk, schema rejection of poison rows
- `src/api_reliability_lab/retry.py` / `transport.py` — retry classification, Retry-After, seeded backoff
- `src/api_reliability_lab/schema.py` — order / page / webhook / ack contracts; rejects NaN/Infinity
- `src/api_reliability_lab/ledger.py` — versioned destination store, optional durable JSON
- `src/api_reliability_lab/webhooks.py` — signature, replay window, 200 on duplicate/stale
- `src/api_reliability_lab/checkpoint.py` — atomic JSON checkpoints
- `src/api_reliability_lab/sync.py` — orchestrator, dry-run, fail-fast, simulated crash
- `examples/` — frozen catalog, webhook events, fault script

## Run

From the repository root (Python 3.10+, standard library only, no network access needed):

```text
python -m unittest discover -s projects/api-integration-reliability-lab/tests -v
```

Offline demo against the synthetic catalog:

```text
python projects/api-integration-reliability-lab/run_lab.py
python projects/api-integration-reliability-lab/run_lab.py --dry-run
python projects/api-integration-reliability-lab/run_lab.py --checkpoint-dir <state-dir> --crash-after-pages 1
python projects/api-integration-reliability-lab/run_lab.py --checkpoint-dir <state-dir>
```

`<state-dir>` is any writable directory outside the repository (for example a temp folder). Without `--checkpoint-dir` the CLI creates a fresh temp directory and prints its path.

The default demo loads `examples/fault_script.json`: a 503, a 429 with `Retry-After: 0`, and a timeout on the first list call, then a 500 and a *lost response* (the mock commits the ack, then the client times out) on the first ack. The report shows `retries: 5`, `acks_sent: 4`, `acks_replayed: 1`: the retried ack hit the stored idempotent response instead of being applied twice.

Exit codes: `0` success, `3` simulated crash (durable ledger and checkpoint are left behind; rerun with the same `--checkpoint-dir` and no crash flag to resume), `2` missing/malformed `--catalog` / `--faults` / `--webhooks` files or out-of-range arguments, `1` a runtime lab error such as auth failure, a non-retryable HTTP error, a schema failure under `--fail-fast`, or a corrupt checkpoint. Each prints a one-line message rather than a traceback.

Each CLI invocation builds a new in-process mock, so acks sent before a crash are re-sent (not replayed) after a CLI resume; the mock's ack state is not persisted. Replay across a crash/resume against the same source is covered by `tests/test_idempotency.py`.

## Design decisions

- **HTTP shapes without sockets.** `InProcessTransport` keeps tests deterministic and offline while still exercising status codes, headers, and bodies.
- **Keyset pagination.** Cursors encode `(updated_at, id)`. A later incremental list from the last cursor sees only rows after that key. Rows inserted *between* already-walked keys are a documented cursor-pagination gap; webhooks cover those updates.
- **Retry only what is safe.** GET retries on 408/425/429/500/502/503/504 and transport timeouts. POST/PUT retry only with an `Idempotency-Key`. 400/401/403/409/422 never retry. `Retry-After` (delta-seconds) / `X-Retry-After-Ms` override exponential backoff and are capped by `RetryPolicy.max_retry_after_ms`; HTTP-date `Retry-After` values fall back to backoff. Jitter is seeded so tests are deterministic.
- **Idempotency keys are derived, not random.** Ack keys are `{sync_id}:ack:{order_id}:{version}`, so a retry or a resumed run sends the same key. The mock replays the stored response when the body hash matches and returns 409 when it does not.
- **Poison rows vs broken envelopes.** A broken page envelope is a `SchemaError` (the page did not load). A single invalid order on an otherwise valid page is skipped and counted in `orders_rejected`; `--fail-fast` stops before checkpointing that page.
- **Webhooks carry full order snapshots.** Apply by version. Duplicate event ids and equal-version snapshots return HTTP 200 so the sender stops retrying. Stale versions are ignored. Signatures use `hmac.compare_digest` on the raw body bytes.
- **Checkpoint after successful acks.** Crash `post_upsert` or `post_ack` replays the page (ledger upserts and acks are idempotent). Crash `post_checkpoint` resumes at the next cursor. A `failed` checkpoint also resumes from its cursor.
- **Dry-run stages, never commits.** Dry-run skips acks and checkpoints. Ledger upserts go to a discardable in-memory overlay, so later records in the same dry run see earlier ones and the preview counts match a real run.

## Limitations

- There is no live HTTP server, public API, or third-party webhook endpoint in this repository.
- Backoff sleep is injectable. The default `WallClock` really sleeps; the CLI and tests use a recording sleeper that advances a `ManualClock`.
- The ledger and checkpoint are JSON files (or in-memory). They are not a database and they are not multi-process-safe beyond `os.replace` on a single host.
- The bearer token and HMAC secret (`LAB_TOKEN`, `LAB_WEBHOOK_SECRET` in `models.py`) are placeholder values for the in-process mock. They are not credentials for any real system.
- Cursor pagination will miss a source row that is omitted from a page whose cursor still advances. The client logs `short_page` when `has_more` is true and `len(items) < limit`; it does not attempt to heal the hole.
- Fail-fast on a poison row that remains in the source will fail again on resume until the source is fixed.

## What it demonstrates

- Cursor/keyset REST pagination with resume from a stored cursor
- Exponential backoff, Retry-After, and non-retryable auth/validation errors
- Idempotent POST with deterministic keys, replay after a lost response, and 409 on key reuse
- Schema validation including extra fields, enums, amounts, and non-finite JSON
- Checkpointing and crash recovery that does not duplicate destination rows
- HMAC webhook verification, replay window, duplicate delivery, and stale snapshots
- Dry-run, fail-fast, structured in-memory logs, and offline unittest coverage
