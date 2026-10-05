# API Integration Reliability Lab — Case 002

A quay terminal receives cargo-release notices from a partner carrier. The partner pushes signed webhooks, retries them for days, and also exposes a cursor-paged event list for anything the push missed. This lab is the receiver, the worker, the mock publisher, and the reconciler, all in one process. Nothing listens on a socket and nothing calls a live API.

The earlier portfolio lab walks a paginated order catalog and posts shipment acks. This case is the other side of that kind of integration: prove the notice, claim it once, and recover it from a list when the push is lost.

## Problem

Webhook samples usually check an HMAC, remember the id for a few minutes, and return 200 before the effect is durable. Those shortcuts fail in different ways:

- parsing JSON and writing it back out changes the MAC, even when the document means the same thing
- a body-only MAC does not cover the delivery id, so a captured body can be replayed under a new id
- a timestamp that is five minutes ahead of the receiver is still inside a five-minute window ten minutes later, and a publisher retry can arrive days after that
- returning 200 before the inbox insert tells the publisher to stop, and the effect is gone
- two Event objects can share an object id and a type, and `created` is not a sequence number
- an offset page shifts when a new event is inserted at the head of the list

## Architecture

```
Publisher                         Receiver POST /hooks/quay | /hooks/stripe | /hooks/coverage
  pin https + address               method, body cap, profile verifier
  spec retry table                  schema for the event's api_version
  full jitter on 429/502/503/504    atomic inbox claim, then 200
  manual replay (schedule kept)          |
        |                             Worker
        |                               memo (0, (event_id, apply))
        |                               release row + outbox, same lock
        |                               yard gate, idempotent on event id
        v
EventCatalog GET-shaped list  -->  Reconciler
  starting_after / ending_before     same Store.claim
  created watermark, 30-day gap      Idempotency-Key POST /v1/releases/ack
```

Package `src/quay_inbox`:

- `mac.py` — Standard Webhooks verifier and the separate Stripe-style verifier
- `coverage.py` — one pinned HTTP message-signature profile plus Content-Digest
- `receiver.py` — POST-only HTTP surface, generic 401, redacted log, CSRF-guarded admin replay
- `store.py` — inbox, short freshness map, memo, outbox, 30-day prune, snapshot
- `worker.py` — apply step, refetch, downstream gate, crash point
- `publisher.py` / `schedule.py` — retry table, bounded slot jitter, full jitter, 410, manual replay
- `ssrf.py` — https pin and non-public deny list
- `catalog.py` — cursor list, checkpoint, reconciler
- `idempotency.py` — quoted Idempotency-Key state machine
- `schema.py` — `2024-09-01` and `2026-01-01` release objects

`examples/notices.json`, `examples/routes.json`, and `examples/fault_script.json` are synthetic. `routes.json` holds the route's HMAC key as a readable 32-byte lab string (`key_text`), not as a `whsec_` value, so nothing in the repository looks like a live signing secret. `decode_whsec` and `encode_whsec` handle the operator-facing `whsec_` format and are covered by the tests. The key is not a credential for any real endpoint.

## Run

From the repository root, Python 3.10+, standard library only:

```text
python -m unittest discover -s projects/api-integration-reliability-lab-case-002/tests -v
```

Offline demo. The fault script answers the first push with 503. The publisher waits for one full-jitter delay, retries, then the reconciler claims the missed notice:

```text
python projects/api-integration-reliability-lab-case-002/run_lab.py
python projects/api-integration-reliability-lab-case-002/run_lab.py --dry-run
python projects/api-integration-reliability-lab-case-002/run_lab.py --state <state-file>
```

`<state-file>` is a writable path. The command writes that snapshot and a sibling checkpoint whose suffix is `.checkpoint.json` (`inbox.json` becomes `inbox.checkpoint.json`). Exit `0` prints one JSON report. Exit `2` means the example files are missing or malformed. The report's `throttle_delays` entry is the full-jitter draw for seed `7`, base `8`, and cap `64`. `--dry-run` still lists missed ids and writes no inbox row, release, or ack.

The demo's pinned callback is `https://hooks.quay.example/hooks/quay`, resolved by a scripted table to `203.0.113.10`. That address is TEST-NET, used here as a synthetic public answer.

## Design decisions

- **Two verifiers, not one helper.** Standard Webhooks MACs `id.timestamp.raw_body` with the decoded `whsec_` bytes and base64 `v1` tags. The Stripe fixture MACs `t.raw_body` with the endpoint secret string and hex `v1` tags, and it ignores `v0`. A shared function that canonicalizes JSON would verify neither.
- **32-byte key floor.** The symmetric spec allows 24 bytes. HMAC-SHA256's recommended key is at least the hash length, so `decode_whsec` rejects anything outside 32–64 bytes. Asymmetric `v1a` is specified by Standard Webhooks and is not implemented.
- **Short cache versus inbox.** The freshness map expires at `timestamp + tolerance` (300 seconds, and `0` is refused). The inbox is what stops a retry at 76 hours and at three days. Both use the authenticated id: the `webhook-id` header, or `event.id` inside the Stripe body. `Store.prune(now)` drops finished rows, with their memo and outbox entries, only once they are more than 30 days old. Queued and ready rows are never pruned.
- **200 after the claim.** The worker applies the release and writes the outbox under the memo key `(0, (event_id, "apply"))`. The yard gate dedupes on that event id, so a crash between send and the completion mark still leaves one gate row. `ack_before_commit=True` is a negative switch used by a test: the publisher sees 200, stops, and the inbox stays empty.
- **Refetch instead of sorting.** A second id for the same object and type, a thin body, or a `created` value that is not strictly newer fetches the current object. The pair is not a unique key, so a later id of that type is kept. Schema selection uses the event's `api_version`, not `CURRENT_API_VERSION`.
- **Coverage is one profile.** It covers `@method`, `@authority`, `@path`, and `content-digest`, then `@signature-params`, with `hmac-sha256` only. Content-Digest is SHA-256 of the raw body as `sha-256=:base64=:`. `created`, `expires`, and `nonce` are rejected. This is not an RFC 9421 implementation.
- **Retries.** The publisher walks the spec table (immediate, 5 seconds, 5 minutes, 30 minutes, 2 hours, 5 hours, 10 hours, 14 hours, 20 hours, 24 hours) and keeps each wait inside that slot. Full jitter is only for 429, 502, 503, and 504. It has its own budget (`max_throttle_retries`, default 5) and does not use up rows of the table. After that budget, a throttle response waits for the next table slot like any other failure, so an endpoint that answers 503 for days still gets the full 75-hour schedule. `Retry-After` delta-seconds replace the draw. 3xx is a failure and is not followed. 410 disables the endpoint. A manual replay does not clear the automatic `next_at`.
- **List cursor.** Pages are newest id first. `starting_after` moves older. After the backward walk, ids newer than the first page's head are collected so a head insertion is not skipped and is not applied twice. The checkpoint's `watermark_created` is the catalog time when the last complete walk started. Every event created by then was visible to that walk. If the watermark is more than 30 days old, events may have left the list unseen, and reconcile raises `GapError` instead of reporting success. A dry run does not advance the checkpoint.
- **Operator replay.** `/admin/replay` exists only when a replay callable is wired, which the tests do with `Publisher.manual_replay`. It requires `x-csrf-token`. The webhook routes do not, because the MAC runs first. A replay of an id that was already delivered goes back through the receiver and is deduped.
- **Idempotency-Key.** POST and PATCH require a quoted key. The lookup is `(client_id, method, path, key)` plus a fingerprint of the raw body. Missing key is 400, mismatched body is 422, in-flight is 409, and a completed call replays the stored status and body. The TTL is 31 days. This follows an HTTPAPI draft that expired on 18 April 2026. It is not an RFC, and it is not how inbound webhooks are deduped.
- **Callback pin.** Registration allows `https` only, denies loopback, link-local, private, unique-local, and metadata names, and the publisher connects to the address from that single lookup.

## Limitations

- No live HTTP server, Stripe account, or third-party webhook endpoint is used. Tests inject the clock and the RNG. They do not sleep.
- `v1a` (ed25519) is not implemented. The coverage route is one profile; it does not implement RFC 9421 `created`, `expires`, or `nonce`.
- The inbox lock is one process. It is not a multi-host transaction. The yard gate is a fake that dedupes on event id. A real downstream call would need its own idempotency key.
- The Stripe profile follows the documented manual HMAC steps (secret string, hex, constant-time compare). It returns the lab's generic 401 for a bad signature. Stripe's own sample returns 400. That difference is intentional.
- Latency in the log is the injected clock's movement across `handle`. Tests leave it at 0.
- The freshness map and the idempotency rows are not a database. Snapshots are JSON files replaced with `os.replace`.
- TEST-NET addresses are treated as public so the scripted resolver has a non-private answer. A production deny list also has to track bogons and IPv6 transition ranges.
- An all-events subscription filter lives in `filter_subscription`. The example feed simply does not contain `release.audit_export`.
- The mock catalog orders events by comparing id strings, so the synthetic ids are zero-padded. Real Stripe ids are opaque, and their order comes from the API, not from comparing the strings.
- `Store.prune` is called explicitly. Nothing schedules it.

## What it demonstrates

- Standard Webhooks and Stripe-style raw-body HMAC, rotation, and a prefix-hash that does not verify
- A 300-second freshness window that is separate from a 30-day inbox
- An atomic claim, a memoized apply step, and one gate effect after a crash between send and mark
- Refetch on thin events, duplicate object/type pairs, and equal `created` values
- Cursor reconciliation that survives an insertion at the head, and an explicit gap past 30 days
- Full jitter for a shared throttle response, the spec retry table for everything else, and manual replay that does not cancel the automatic schedule
- Quoted Idempotency-Key replay, 400/409/422, and client isolation
- Body cap, generic 401, redacted logs, dry-run, and an https callback pinned to one resolved address
