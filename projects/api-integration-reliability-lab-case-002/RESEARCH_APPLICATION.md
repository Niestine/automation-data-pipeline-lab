# Research application

Ten public sources shape this lab. Each technique below is implemented in `src/quay_inbox` and locked by `tests/`. The receiver, publisher, worker, and reconciler are in-process. They do not call Stripe, GitHub, or any other live endpoint. Placeholder `whsec_` values are synthetic HMAC keys for the mock.

## Raw-body MAC, version allowlist, and per-route secret

Sources:

- Standard Webhooks specification, https://github.com/standard-webhooks/standard-webhooks/blob/main/spec/standard-webhooks.md
- Stripe, "Receive Stripe events in your webhook endpoint", https://docs.stripe.com/webhooks
- OWASP, "Webhook Security Cheat Sheet", https://cheatsheetseries.owasp.org/cheatsheets/Webhook_Security_Cheat_Sheet.html

`mac.verify_standard` signs `webhook-id`, a dot, the timestamp text, a dot, and the raw body with HMAC-SHA256. The key is the base64 payload of `whsec_`, and this lab rejects decoded keys shorter than 32 bytes. A dot in the id or the timestamp fails before `hmac.new` runs. The signature header is a space-separated list; only an exact `v1` tag can succeed, so `v1a` and `v0` do not. Any one active secret is enough, which is the overlap window. Removing the old secret makes a tag from that secret fail. `mac.verify_stripe` is a separate function: it splits `Stripe-Signature` on commas, keeps `t` and every `v1`, discards `v0`, and uses the endpoint secret string as the HMAC key. A dashboard secret and a CLI secret are different strings. A retry keeps `event.id` and mints a new `t` and `v1`. Reserializing the JSON, reordering keys, or appending a newline fails verification.

## Real HMAC, not a keyed hash

Source: Bellare, Canetti, and Krawczyk, "Keying Hash Functions for Message Authentication" (1996), https://cseweb.ucsd.edu/~mihir/papers/kmd5.pdf

Verification calls `hmac.new(..., hashlib.sha256)` and `hmac.compare_digest` on equal-length bytes (hex strings for the Stripe profile, after a length check). `prefix_sha256`, SHA-256 over `secret || body`, is a negative vector. A `v1` tag built from it fails the Standard Webhooks verifier, and the same construction fails the coverage profile. The GitHub-style helper `github_style_body_mac` signs the body only. The receiver never calls it. In the test, one captured body and tag arrive under two unsigned delivery ids. Both verify under that helper, and a dedupe map keyed on the delivery header processes both. The same id swap fails `verify_standard`, because the id is inside the MAC.

## Freshness cache is not the inbox

Sources: the Standard Webhooks specification and the OWASP cheat sheet above, plus the Stripe webhook guide.

The attempt timestamp must sit within 300 seconds of the injected clock. A tolerance of 0 is refused at configuration time because it turns the check off. `Store.fresh_remember` keeps an accepted id until `timestamp + tolerance`, so a timestamp 300 seconds ahead is still suppressed 600 seconds later. That map is cleared by `forget_fresh` and is not part of the snapshot. The inbox row is the correctness boundary. A new attempt at 76 hours and at three days, each with a fresh timestamp and signature, does not run the apply step again. Deleting the inbox, memos, and release rows and replaying an hour later runs the apply step a second time. That is the five-minute-key failure, and it is only constructed inside the test. `Store.retained` stays true through 30 days, which covers the spec table (sum of the delays is 75:35:05), the three-day automatic horizon, and the list API's 30-day window. `Store.prune` uses that predicate. It keeps a finished row at exactly 30 days and drops it one second later, along with its memo and outbox entry. It never drops a queued row.

## Atomic claim, memo, and 2xx after commit

Source: Ramalingam and Vaswani, "Fault Tolerance via Idempotence", POPL 2013, https://www.microsoft.com/en-us/research/publication/fault-tolerance-via-idempotence/

`Store.claim` inserts the inbox row under one lock. A second delivery sees `queued`, `ready`, or `done` and does not enqueue again. The receiver writes 200 only after that insert, except for the `ack_before_commit` switch, which returns 200 with an empty inbox so the publisher treats the event as delivered and stops. The worker's apply step uses the memo key `(0, (event_id, "apply"))`. A hit returns the stored value and does not write. The release row and the outbox row are stored in that same locked update. `Downstream.accept` records one gate row per event id. A crash after the send and before `mark` leaves the outbox `pending`; the next `drain` sends again and the gate row count stays 1. Two threads posting one id through `Receiver.handle` produce one claim and one memo miss. The negative control is a `Store` subclass in the test whose claim reads the inbox outside the lock, waits on a barrier, then inserts. Driven by the same two concurrent requests through the real receiver, it reports two inserts and two accepted deliveries.

## Stripe ordering and the event's own schema

Sources: the Stripe webhook guide above, and Stripe, "List all events", https://docs.stripe.com/api/events/list

Dedupe stays on `event.id`. `(data.object.id, event.type)` is a refetch hint. A second id for that pair, or a `created` value that is not strictly newer, fetches the current object instead of applying the snapshot and instead of dropping the id. The test delivers `release.settled` before `release.opened` with equal `created` timestamps and expects the fetched berth, not either snapshot. A later third id of the same type is kept and updates the release. Thin bodies carry only the object id; the stored release is the fetched object. `validate_snapshot` selects `2024-09-01` or `2026-01-01` from the event. The process constant `CURRENT_API_VERSION` is `2026-01-01`, and a 2024 payload still passes the receiver. `filter_subscription(None)` drops `release.audit_export`. The lab does not model Stripe's rule that a subresource event has no parent event; it only states the rule.

## Pinned coverage and Content-Digest

Sources:

- RFC 9421, "HTTP Message Signatures", https://www.rfc-editor.org/rfc/rfc9421
- RFC 9530, "Digest Fields", https://www.rfc-editor.org/rfc/rfc9530

`coverage.verify_coverage` accepts one label, `quay`, and one component list: `@method`, `@authority`, `@path`, `content-digest`, then the transmitted `@signature-params` line. `alg` must be `hmac-sha256`. `created`, `expires`, and `nonce` are rejected. The signature base is the five lines in that order, each ending in a newline, and the MAC is compared bytewise. `content_digest` is `sha-256=:base64(SHA-256(raw body)):`. A flipped body byte, a hex digest, a second algorithm in the dictionary, a `Digest` header, a `Repr-Digest` header, a covered-set subset with its own valid MAC, and a prefix hash all fail. This is not a full RFC 9421 implementation. The local extract used while designing the profile stops in section 4, so section 7 replay parameters are not implemented.

## Outbound Idempotency-Key

Source: draft-ietf-httpapi-idempotency-key-header-07, "The Idempotency-Key HTTP Header Field", https://datatracker.ietf.org/doc/draft-ietf-httpapi-idempotency-key-header/

The draft expired on 18 April 2026 and is not an RFC. `parse_idempotency_key` requires a quoted string and runs before any lookup. A missing or unquoted key is 400 with a `application/problem+json` body. The store key is `(client_id, method, path, key)`. The SHA-256 of the raw body is stored beside it. The same fingerprint replays the stored status and body, including a stored 500. A different fingerprint is 422. An in-flight retry is 409. Another client id with the same key string gets its own row. The TTL is 31 days, longer than the retry table and the 30-day replay horizon. One second before expiry replays; at expiry the handler runs again. The reconciler posts acks through this resource. A second reconcile of the same id does not run the handler again.

## Cursor recovery on the same inbox

Source: Stripe, "List all events", https://docs.stripe.com/api/events/list

`EventCatalog.list_events` orders ids newest first. `starting_after` returns older ids. `ending_before` returns newer ids. `type` and `types` together raise, and `types` longer than 20 raises. The reconciler walks backward, then catches up ids newer than the first page's head so an insertion at the head is returned once and older ids are not skipped or doubled. An offset walk of the same insertion duplicates an id; that walk is only in the test. `watermark_created` is a point on the `created` axis: the catalog time when the last complete walk started, so every event created by then was visible to that walk. It is not the oldest `created` on the pages. That choice would make a second run raise a false gap as soon as an old undelivered event passed 30 days, and the test checks that it does not. A watermark more than 30 days old raises `GapError` before any claim. A dry run leaves the checkpoint alone. An ack counts only when the idempotent POST returns 2xx. Recovered notices go through `Receiver.ingest`, the same `Store.claim` as a verified push, so a delayed webhook and a catalog row for one id apply once. `delivery_success=false` is the reconciler's default filter.

## Status-aware retries and full jitter for the herd

Sources:

- Standard Webhooks specification (retry table, 2xx, 3xx, 410, Retry-After), cited above
- Marc Brooker, "Exponential Backoff and Jitter" (4 March 2015), https://aws.amazon.com/blogs/architecture/exponential-backoff-and-jitter/

`SCHEDULE_SECONDS` is the spec table from immediate through 24 hours. Ordinary failures wait for the next slot minus a bounded draw of at most one fifth of that slot, so a 24 hour slot cannot become a sub-second sleep. Statuses 429, 502, 503, and 504 use full jitter: `rng.randrange(0, min(cap, base * 2^attempt) + 1)`. 503 is included because the jitter result is about a shared overload response, not only the three throttle codes named in the webhook table. Full jitter has its own attempt budget, `max_throttle_retries`, and does not use up rows of the table. After that budget is spent, a throttle response waits for the next table slot. In the test, an endpoint that always answers 503 gets three jittered retries and then the remaining nine table slots, and the walk still spans at least four fifths of 75:35:05. A fixed `random.Random` reproduces that sequence. Two seeds differ. `no_jitter_delay` is identical for every client and is not what the publisher uses. `Retry-After` delta-seconds replace the formula. 2xx completes the automatic schedule. 3xx records `Location` and does not open a second connection. 410 disables the endpoint. `manual_replay` restores `next_at` and `automatic_open`, so a successful manual send leaves the automatic attempt in place. The receiver then dedupes that later attempt.

## Fail-closed HTTP, redacted logs, and pinned HTTPS

Sources: the OWASP cheat sheet and the Standard Webhooks SSRF note, cited above.

POST is required; other methods are 405. Bodies larger than 20 KiB are 413 before the MAC. Malformed JSON and schema failures are 400, and a bad signature on malformed JSON is 401, so the MAC runs first. Every authentication failure returns `{"error":"unauthorized"}` with no secret and without the words `timestamp` or `signature`. `/admin/replay` exists only when a replay callable is wired. It requires `x-csrf-token` and forwards a JSON id list. The test wires it to `Publisher.manual_replay`, and the replayed delivery is deduped by the receiver. The webhook paths do not take a CSRF token. Logs keep time, source, method, status, event id, event type, and latency. They omit the body, the secret, and the signature header. `pin_https` allows `https` only, rejects metadata names (a trailing dot is stripped first), loopback, link-local, private, reserved, IPv4-mapped loopback, and unique-local addresses, and returns the first resolved address. The publisher sends to that address with the original host in Host and SNI, and it does not resolve again. A second lookup that would return 127.0.0.1 is what a rebinding client would see; the publisher's send path does not perform it. TEST-NET `203.0.113.10` is the synthetic public answer.
