# API Integration Reliability Lab — Case 003

A community-garden registrar exports plot allotments into a local ledger. The catalog is paged with an opaque cursor over a frozen snapshot, each page is upserted with a replay key, and a checkpoint records the barrier between pages. A signed webhook can revise one allotment. The registrar, the export worker, and the webhook sender all run in one process against SQLite. Nothing listens on a socket and nothing calls a live API.

Neighboring portfolio labs cover a fulfillment-order shipment walk and a quay cargo-release inbox. This case is the registrar export: cursor pagination with checkpointed recovery, and idempotent upserts of synthetic plot rows.

## Problem

A multi-page export fails in ways a single happy-path client hides:

- an empty `next_page_token` is the only end of the list, so a short page, including an empty page, can still have more rows
- deleting a row already returned shifts later offset pages and drops a row the export should have kept
- a live keyset walk misses a row that commits late with an earlier key, and it can emit a bumped sort key twice
- the first page of a walk was fetched with no token, so recovery that repeats that public call opens a new snapshot
- a response dropped after commit must replay the stored body, while the same key with a different body must not write
- a crash after the page upsert and before the checkpoint write leaves rows ahead of the barrier
- a webhook acknowledged before commit tells the sender to stop, and the revision is gone

## Architecture

```
ExportWorker                         Service
  lease one scope                      GET  /v1/{parent}/resources
  list one snapshot page               POST /v1/{parent}/resources:upsert
  upsert the page                      POST /webhooks/events
  then write the checkpoint                 |
        |                                   v
        +-------- one SQLite file -------- Store
                    source_rows, snapshot_rows, page_tokens
                    ledger, idempotency, webhook_inbox, checkpoints
```

Package `src/plot_allotment`:

- `service.py` — in-process HTTP: snapshot list, idempotent upsert, webhook receiver
- `store.py` — SQLite schema, snapshot copy, ledger, idempotency, inbox, checkpoint
- `worker.py` — acyclic page loop, lease, split and atomic commit, gap replay
- `client.py` — list and upsert client with the retry policy
- `retry.py` — capped exponential backoff, full jitter, delta-seconds `Retry-After`
- `webhooks.py` — raw-body HMAC and the shortened sender ladder
- `walks.py` — offset and keyset cost models used by the tests
- `occsim.py` — local one-row optimistic-concurrency comparison of backoff algorithms
- `schema.py` — resource contract, fingerprints, and the synthetic HMAC key
- `journal.py` — fault plus resume decision for each recovery branch

`examples/allotments.json` holds parent `garden-north` and seven synthetic lots. `lot-003` has `note=closed`. The other six are `open`. Plot codes run `N-01` through `N-07` and holder labels `holder-001` through `holder-007`. `examples/fault_script.json` sets `filter=note=open`, `order=sort_key`, and `page_size=2`, so the export is three pages of six rows. `examples/webhook_event.json` is a sample `allotment.revised` body. The HMAC key is the 32-byte lab string in `schema.py`. The examples do not contain a `whsec_` value.

## Run

From the repository root, Python 3.10 or newer, standard library only:

```text
python -m unittest discover -s projects/api-integration-reliability-lab-case-003/tests -v
```

Offline demo. The clock is `ManualClock`. The default run injects one checkpoint I/O error after the first page, resumes in the same process, prints one JSON report, and exits 0. The temporary SQLite directory is removed.

```text
python projects/api-integration-reliability-lab-case-003/run_lab.py
python projects/api-integration-reliability-lab-case-003/run_lab.py --dry-run
python projects/api-integration-reliability-lab-case-003/run_lab.py --state <dir> --crash-before-checkpoint
python projects/api-integration-reliability-lab-case-003/run_lab.py --state <dir>
```

`<dir>` is a writable directory. The command stores `allotments.sqlite` there. `--crash-before-checkpoint` requires `--state`. It stops after the injected failure and exits 3. The JSON names `apply_ahead`, `crashed`, `ledger_rows`, and `state`. A later invocation of the same `--state` directory, without the crash flag, sees the checkpoint and resumes. It does not inject the failure again, and it does not insert the example rows a second time.

Exit 0 is a finished export or a dry run. Exit 2 is a missing or malformed example, a bad argument, or `--crash-before-checkpoint` without `--state`. Exit 3 is the crash-only stop. Exit 1 is any other lab error, including an injection that never fired. Expected failures print one `error:` line on stderr.

The success report includes `crashed_then_resumed`, `dry_run`, `duplicate_executions`, `faults`, `ledger_rows`, `pages_this_run`, `parent`, `replayed`, `seen_ids`, and `snapshot_id`. On the example feed the finished export has six ledger rows (`lot-001`, `lot-002`, `lot-004`, `lot-005`, `lot-006`, `lot-007`) and `duplicate_executions` 0. `duplicate_executions` counts resource ids with more than one completed idempotency record. The ledger primary key cannot show a duplicate on its own, but a replay that opened a new snapshot would derive new keys and push that count above 0. The crash-only stop has two ledger rows and `apply_ahead` true. `--dry-run` lists the three pages, writes no ledger rows, and writes no checkpoint. It does open a read snapshot, because that is how the list API serves the pages.

## Design decisions

- **Opaque cursor over a copied snapshot.** `GET /v1/{parent}/resources` accepts `page_size`, `page_token`, `filter`, and `order`. The first call with no token copies the matching source rows into `snapshot_rows` and stores the last `(sort_key, resource_id)` as the bound. Later pages read that copy with the compound keyset `(sort_key, resource_id)`, ordered the same way. The token is 32 URL-safe characters. The server stores the snapshot id, the cursor, the bound, the caller, the parent, the filter, the order, and an expiry of three days. The token string does not carry those fields. A token minted for another caller is 403. A token used with a different parent, filter, or order is 400. Changing `page_size` on a later page is honored. Omitted or zero `page_size` is 50. A value above 1000 is coerced to 1000. A negative value is 400. `offset`, `skip`, and any other query key are 400. An empty `next_page_token` is the only end signal. A short page, and an injected empty page, still carry a token while the snapshot has more rows.
- **The snapshot is the export metric.** Deleting the first row of page 1 before page 2 makes an offset walk miss the shifted row. The snapshot walk still returns it. A row inserted with a key between the cursor and the bound, and a sort-key bump of a row already read, stay out of the rest of the snapshot walk. The paired live-keyset walk misses a row that commits late with an earlier key and can emit the bumped row twice. `walks.py` is a separate cost model: on 5000 sorted keys, offset 1000 and limit 20 examine 1020 rows, and the keyset page examines 20.
- **Idempotency-Key before If-Match.** `POST /v1/{parent}/resources:upsert` requires `Idempotency-Key` as a quoted lowercase UUID. The header is parsed before any idempotency-table read. Missing or malformed is 400 and does not increment the lookup counter. The fingerprint is SHA-256 of the method, the path, and canonical JSON of the validated body. `request_id` is accepted on the request, checked as a lowercase UUID, and omitted from the fingerprint and from the ledger. Lookup is `(caller, key)`. No row inserts `in_progress`. The same fingerprint while `in_progress` is 409 and is not stored. A completed row with the same fingerprint returns the stored status and body with `x-idempotency-replay: stored`. A different fingerprint is 422 and does not write. Another caller with the same key string has a separate row. Retention is 24 hours. One second before expiry still replays. At expiry the handler runs again. If the stored body has been pruned and the resource remains, the response is the current resource with `x-idempotency-replay: current-resource`. If the handler transaction rolls back, the `in_progress` claim is deleted and the response is 500, so the client's retry with the same key runs once instead of being held at 409 until the TTL.
- **Strong ETag is the tag this server issued.** A fresh upsert compares `If-Match` to the current ledger tag, which looks like `"e-0001"`, by exact string equality. A match writes a new tag. A completed key replayed with a stale tag returns the stored success and does not write again. A new key with a stale tag is 412, the `in_progress` row is deleted, and the same key can then succeed with the corrected tag and the same body. A weak `W/` validator is 400 and does not touch the idempotency table. The natural page-replay key is `uuid5` of caller, snapshot id, resource id, and source version, so a replayed page does not run the handler twice. The ledger primary key is `(parent, resource_id)`. The export worker does not call the HTTP upsert route. It runs the same `prepare_idempotency` / `finish_idempotency` steps inside its own SQLite transaction, which is what lets atomic mode commit the page and the barrier together.
- **Barrier after the page.** The worker acquires a compare-and-swap lease for 60 seconds on the injected clock and renews it before each page. The same owner can re-acquire. A second owner exits with `LeaseDenied` and writes nothing. Every barrier write is fenced on `lease_owner`, so a worker whose lease expired and was taken over gets `LeaseDenied`, and its page upserts in that transaction roll back. A different parent, filter, or order on an existing scope is `CheckpointMismatch`. Checkpoint columns are `scope`, `consumed_token`, `param_fingerprint`, `snapshot_id`, `lease_owner`, and `lease_until`, plus `next_token` (null before the first barrier, empty at the end), `applied_token`, `apply_ahead`, and `done`. `consumed_token` is the token used to fetch the page that just committed. The in-flight page body is not stored. `apply_atomic` commits the upserts and the barrier together. An injected checkpoint error in that mode fires after the upserts and rolls them back with the barrier. `apply_split` commits the upserts, sets `apply_ahead`, and can fail in `write_barrier` before the checkpoint update. Recovery of that gap replays the page. The first page's fetch token is empty, and a public list with an empty token would open a new snapshot, so page 1 is re-read with `read_snapshot_page` on the stored snapshot id. Later pages re-fetch the opaque `applied_token` through the public list. Tests cover a gap on page 1 and on page 2. `fail_next_checkpoint` raises `CheckpointIOError` once. The worker logs `checkpoint_io` / `surface_error` and re-raises. Resume logs `gap_detected` / `replay_uncheckpointed_page`. A kill after the barrier (`max_pages=1`, then a new run) does not insert those rows again.
- **Webhook ack after commit.** `POST /webhooks/events` reads the raw body first. Bodies larger than 20 KiB are 413 before the MAC. The MAC is HMAC-SHA256 over `webhook-id`, a dot, `webhook-timestamp`, a dot, and the raw body. Each `v1` tag in `webhook-signature` is compared in constant time, and the first match wins. `v1a` is ignored. A dot in the id or the timestamp is 400. A bad MAC or a timestamp more than 300 seconds from the injected clock is 401. The inbox insert and the ledger upsert share one transaction. 204 is returned after commit. A duplicate `webhook-id` is 204 and does not write again. A forced commit failure is 500, rolls back the inbox row and the execution buffer, and the sender's retry then applies once. The sender keeps the id, refreshes the timestamp, and re-signs. One test wires `WebhookSender` to the in-process receiver to check that end to end.
- **Retries on an injected clock.** List calls, 409, and 429/5xx retry up to 5 attempts. The delay is `uniform(0, min(cap, base * 2^attempt))` with base 0.05 seconds and cap 2 seconds. The exponent stops at 62. A `Retry-After` that is delta-seconds, including a decimal, is the whole wait. `Retry-After: 2` on a 503 advances the clock by 2.0. An HTTP-date `Retry-After` does not parse, and the client uses full jitter inside the cap. Statuses 400, 401, 403, 412, and 422 are not retried. 409 is retried unchanged. The webhook sender uses the shortened ladder 0, 1, 5, 30, 120 seconds and advances `ManualClock` by that wait. Throttle statuses 429, 502, 503, and 504 use full jitter unless `Retry-After` is present. 3xx records `Location` and retries the original URL. 410 disables the endpoint and stops. The specification ladder, ending at 75:35:05, is a constant used to check retention. Inbox rows are kept for 4 days, which is longer than that ladder. `prune_inbox` drops rows whose `received_at` is at or before `now - retention`.
- **Logged decisions.** `Journal.record` requires a fault and a decision. The suite executes timeout-after-commit (`replay_stored_body`), 409 (`retry_without_changes`), 412 (`stop_not_a_replay`), 422 (`stop_payload_mismatch`), and checkpoint I/O (`surface_error`). A checkpoint error is not swallowed.
- **Local backoff comparison.** `occsim.compare_occ` re-runs a one-row optimistic-concurrency setup with a private `random.Random`. Heap entries are `(time, sequence, handler, reply, payload)`, so equal timestamps do not compare bound methods. The test calls `compare_occ(clients=12, trials=8, seed=20261006)` and asserts that full jitter issues fewer writes than no backoff on that run. The counts are whatever that process returns.

## Limitations

- The demo and the tests advance `ManualClock`. They do not sleep on the wall clock for the retry ladder or the webhook schedule. The concurrent 409 test joins a thread so the in-progress row can be observed.
- HTTP is `Request` and `Response` objects inside the process. There is no socket server.
- `Idempotency-Key` follows draft-ietf-httpapi-idempotency-key-header-07. That draft expired on 18 April 2026. It is a lab convention here, and the draft is not an RFC.
- ETag comparison is exact equality of the quoted tag this server issued. The lab does not implement a general strong-validator byte comparison for tags minted elsewhere.
- One walk uses one snapshot id, created by copying the matching rows when the first page is opened. That copy is the page-walk snapshot. It is not an implementation of Adya's snapshot-isolation definition.
- Recovery leaves one committed upsert per resource id inside that snapshot. The live-keyset test shows a late commit of an earlier key is missed, so the equality check is against the snapshot.
- `WebhookSender` waits `SHORT_LADDER_SECONDS`. `SPEC_LADDER_SECONDS` records the specification table and is not the sender's schedule. The lab does not report Carbone's Flink throughput figures, and `walks.py` does not report Postgres timings.
- Page tokens and snapshot ids are drawn from the injected `random.Random` so runs are reproducible. That generator is not a CSPRNG. A deployment would use `secrets`. Here a guessed token still fails the caller check (403).
- The webhook receiver applies a revision without comparing `source_version`, so an older event delivered late overwrites a newer row.
- `v1a` (ed25519) is not implemented. The lease is process-local SQLite, added so two workers can contend; it is not part of the snapshot-barrier paper. Dry-run still opens a read snapshot and page tokens.

## What it demonstrates

- An opaque snapshot cursor whose only end signal is an empty token, with page-size coercion, token expiry, and caller binding.
- Offset and live-keyset walks losing or doubling rows that the frozen snapshot walk keeps once.
- A three-state idempotency record, lost-response replay, fingerprint mismatch, and If-Match evaluated after that lookup.
- A split commit that leaves `apply_ahead` set, a raised checkpoint error, and a resume that replays one page without a second ledger row.
- Raw-body webhook verification, ack after commit, and a sender that records scheduled waits on an injected clock.
- Full jitter inside a cap, delta-seconds `Retry-After` as the whole wait, and a seeded local comparison in which full jitter writes less than no backoff.
