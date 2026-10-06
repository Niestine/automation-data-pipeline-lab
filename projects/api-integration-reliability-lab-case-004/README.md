# API Integration Reliability Lab — Case 004

A synthetic cold-storage office issues inspection grants to handheld clients. Each grant is a refresh-token family. The client syncs sealed excursion pages into SQLite and posts idempotent restock orders. A local mock authorization server, two audience-scoped resource servers, and a webhook consumer all run in one process. Nothing listens on a socket and nothing calls a live API.

Neighboring portfolio labs cover a fulfillment-order catalog, a quay cargo-release inbox, and a plot-allotment export. This case is the refresh-family grant cycle: bounded refresh, replay revocation, archive checkpoints, and restock idempotency.

The office, the lots, the secrets, and the webhook bodies are synthetic. `lab-secret-north`, `lab-secret-south`, the HMAC key `lotcycle-lab-mac-v1`, and the two `lotcycle-lab-*-pepper` values in `params.py` are fixtures for this process. They are not live credentials.

## Problem

A grant client fails in ways a single happy-path refresh hides:

- a refresh response dropped after the server committed the new generation must not be sent again, because presenting the old refresh token revokes the family
- an HTTP 500 from this token endpoint means the rotation transaction rolled back, so that failure can be retried with the same refresh token
- two callers refreshing one family at the same time will replay a generation if they do not share a single in-flight refresh
- an access token's `exp` and a refresh token's inactivity window are different clocks
- a narrow scope belongs on the access token; the refresh token keeps the original grant
- offset pages of a changing excursion feed cannot be retried, so sealed archive pages are addressed by the last entry id
- a checkpoint written before the page is durable, or a page written without the checkpoint, leaves the client unable to tell a crash from a finished sync
- a restock POST retried after a dropped 201 needs the same Idempotency-Key and the same raw body
- re-serializing a webhook JSON body breaks the MAC, and a retry must keep `webhook-id`

## Architecture

```
GrantClient                         AuthServer          ResourceServer
  one refresh under a lock          POST /token         GET  /{audience}/entries
  sync prev-archive                 refresh families    GET  /{audience}/archives/{cursor}
  POST restock orders               SQLite families     POST /{audience}/orders
        |                           refresh hashes      POST /{audience}/archives/{cursor}/entries
        |                           reuse log                  |
        +--------------------------- one SQLite file ---------+
                                    orders, idempotency, checkpoints, webhook ids

WebhookProducer  --signed raw body-->  WebhookConsumer
                                      POST /hooks/{endpoint_id}
```

Package `src/lotcycle`:

- `auth_server.py` — token endpoint, rotation transaction, sender-constraint proof, closed error set
- `resource_server.py` — audience checks, archive documents, idempotent orders
- `client.py` — single-flight refresh, archive sync, order retries
- `archive.py` — sealed pages and the entry merge rule
- `webhooks.py` — raw-body HMAC, retention, compressed delivery schedule
- `store.py` — SQLite schema, explicit transactions, log redaction
- `retry.py` — full, equal, and decorrelated jitter; proactive phase; contention sim
- `harness.py` — fixture-scale brownout model with a shed gate and an ablation switch
- `tokens.py` — 32-byte refresh tokens and HMAC access tokens
- `schema.py` — orders, entries, webhook events, fingerprints, problem documents
- `httputil.py` — in-process transport, form parsing, Link parsing
- `world.py` / `demo.py` — example seed and the JSON report
- `params.py` — lifetimes, budgets, and fixture keys
- `clock.py` — virtual clock

Audiences are `lot-ledger` and `alarm-board`. The transport routes `/token`, `/{audience}/...`, and `/hooks/...` to those handlers. `examples/grants.json` seeds four clients. `examples/excursions.json` seeds 120 ledger rows for `handheld-north` and 3 alarm rows for `handheld-south`. `examples/fault_script.json` asks for one pre-commit token failure. `examples/webhook_event.json` is the `excursion.opened` body for `ex-120`. The examples do not contain a `whsec_` value.

## Run

From the repository root, Python 3.10 or newer, standard library only:

```text
python -m unittest discover -s projects/api-integration-reliability-lab-case-004/tests -v
```

Offline demo. The clock starts at 1_700_000_000. The default run advances one access-token lifetime, injects one pre-commit HTTP 500, refreshes on the second attempt, syncs 120 entries, posts one order, delivers one webhook, prints one JSON object, and exits 0.

```text
python projects/api-integration-reliability-lab-case-004/run_lab.py
python projects/api-integration-reliability-lab-case-004/run_lab.py --dry-run
python projects/api-integration-reliability-lab-case-004/run_lab.py --drop-refresh
python projects/api-integration-reliability-lab-case-004/run_lab.py --state <dir>
```

`<dir>` is a writable directory. The command stores `lotcycle.sqlite` there and closes the connection before it returns. `--dry-run` always uses memory. It ignores `--state`, `--drop-refresh`, and the fault script. It does not advance the clock, does not post an order, and does not deliver a webhook. `--drop-refresh` drops the refresh response after the server has committed generation 2. It does not apply the pre-commit failure. The client stops. The demo does not present generation 1 again, so the family stays `active`.

Exit 0 is a finished report or a dry run. Exit 2 is a missing or malformed example, a bad registration, or a value error while loading the feed. The message is one `error:` line on stderr. An unexpected `ReauthRequired` outside the drop path is not turned into exit 2.

The report object has these keys: `checkpoints`, `client_id`, `client_status`, `decisions`, `dry_run`, `entries`, `orders`, `reconstruction_incomplete`, `refresh_posts`, `server_generation`, `server_status`, `webhook_resource_found`, `webhook_status`.

On the default run, `client_id` is `handheld-north`, `refresh_posts` is 2, `entries` is 120, `orders` is 1, `checkpoints` is 2, `webhook_status` is 204, `webhook_resource_found` is true, `server_generation` is 2, `server_status` and `client_status` are `active`, and `decisions` includes `refresh_precommit_retry` and `refresh_ok`. The order is sku `crate-ice`, quantity 2, Idempotency-Key `restock-north-001`.

`--drop-refresh` reports `refresh_posts` 1, `client_status` `reauth_required`, `server_status` `active`, `server_generation` 2, and zeros for entries, orders, and checkpoints. `decisions` includes `refresh_post_send_stop`.

`--dry-run` reports `refresh_posts` 0, `entries` 120, `orders` 0, `checkpoints` 0, `server_generation` 1, `client_status` `active`, and null webhook fields.

## Design decisions

- **Rotation commits or it does not.** `POST /token` with `grant_type=refresh_token` is the only grant that mints a token. Confidential clients authenticate with HTTP Basic. Public clients send `client_id` and no secret. A public client must be registered as `rotation` or `sender_constraint`. Every token response, including `invalid_grant`, `invalid_scope`, and the pre-commit 500, sends `Cache-Control: no-store` and `Pragma: no-cache`. The 500 body is `server_error`, which is not a section 5.2 code. The client retries it because this server returns 500 only before the rotation transaction. The pre-commit budget is 3, including the first attempt. A connection closed before the bytes are written retries with the same refresh token. A response dropped after the handler returns does not retry.
- **One family, one active generation.** Refresh tokens are 32 bytes from `secrets.token_bytes`, encoded as urlsafe base64 without padding, and stored as the SHA-256 hex of the token string. The seeded RNG drives timers only, never token material. Rotation consumes generation `n` and inserts `n+1` in one transaction, as a compare-and-set on the family's active generation. If two exchanges of the same generation race past the read-side checks, the one that commits second fails the compare-and-set and is handled as a replay. Replaying an older generation revokes the active descendant, clears `active_generation`, and appends one reuse row. The presenter is the client id. A second presentation of the revoked family does not append another row. Another confidential client's Basic credentials presenting the victim's refresh token get `invalid_grant` and leave the family active, including when the presented generation was already consumed. Binding is checked before the replay rule.
- **Access tokens are HMAC payloads, not JWTs.** The payload carries `iss`, `aud`, `client_id`, `scope`, `exp`, `family_id`, `iat`, and `jti`. `jti` is there so two issuances at the same virtual second do not share a hash. The resource server checks the MAC before it reads claims, then checks issuer `https://auth.coldlot.example`, audience, `exp <= now`, and scope. `POST /orders` requires `orders.write`. Reads require `excursions.read`. Insufficient scope is 403, so the client does not refresh. Revoking the refresh family does not revoke an unexpired access token.
- **Scope narrows the access token only.** An omitted scope, or a scope equal to the grant, omits the JSON `scope` field. `scope=excursions.read` narrows that access token, returns `scope`, and leaves the family scope at `excursions.read orders.write`. An added member is `invalid_scope` and does not rotate. The client treats `invalid_grant` as reauthentication required. Other terminal token errors do not, and none of them advance the clock.
- **Inactivity is three lifetimes.** The access lifetime `T` is 60 virtual seconds. A refresh whose `last_used_at` is more than `3T` behind the clock is `invalid_grant`. The generation stays put and no reuse row is written. Exactly `3T` still refreshes. An access token at `exp == now` is already refused while the family is inside the window.
- **Sender constraint does not rotate.** The lab proof is HMAC-SHA256 over `POST./token.{timestamp}`, accepted within 300 seconds. A correct proof moves `last_used_at`, returns a new access token, and omits `refresh_token`. A wrong proof, a non-digit timestamp, or a timestamp 301 seconds away is `invalid_grant` and does not revoke the family. Rotation mode ignores sender fields. This proof is not mTLS and not DPoP. The sender key is derived from a fixture pepper and the public key id, so anyone with this source can compute it. It exercises the server branch, not real possession of a key.
- **Archive pages are sealed.** Page size is 50. One hundred twenty entries `ex-001` through `ex-120` produce sealed pages `ex-050` and `ex-100` and a head of `ex-101` through `ex-120`. The head links `prev-archive` to `ex-100`. `ex-100` links `prev-archive` to `ex-050`. `ex-050` links `next-archive` to `ex-100` and has no previous archive. The client checkpoints `ex-100` and `ex-050`. The head is stored and is not a checkpoint. Upsert plus checkpoint is one transaction. A higher `updated` wins. On a tie, the greater document `updated` wins. `POST` to a sealed page is 409. Status 403, 404, or 410 on an archive stops the walk, sets `reconstruction_incomplete`, and does not checkpoint that cursor.
- **Orders use a quoted Idempotency-Key.** The fingerprint covers method, path, and raw body. The first request commits `in_flight` before the business write, so a concurrent request can observe 409. The order insert and the `completed` update share one transaction. The fault hook `fail_order_commits` raises after the insert, so the insert rolls back, the `in_flight` row is deleted, and the response is 500; the same key then runs again. The client retries that 409 with full jitter up to three times. A completed row replays the stored status, content type, and body, including a stored 404 for an unstocked sku such as `missing-lot`. Stocked skus start with `crate-` or `pack-`. A different body is 422. The key expires 24 hours after `created_at`. A dropped order response is retried with the same key and no token refresh, because the key makes the retry safe. The resource retry budget is 3 and does not call `/token` unless the resource call is 401.
- **Webhooks sign the raw body.** The consumer is `POST /hooks/{endpoint_id}`. Duplicate ids within 300 seconds return 204 without another handler run. After 301 seconds the same id is processed again. The producer keeps the id, replaces the timestamp, and re-signs. Five attempts, then stop. `Retry-After: 7` advances the virtual clock by 7 and does not also draw jitter. A negative, non-finite, or HTTP-date `Retry-After` falls back to jitter. Redirects are recorded and not followed. HTTP 410 disables the endpoint. One delivery may be in flight.
- **Retries use full jitter.** The wire delay is `random(0, min(cap, base * 2^attempt))` with base 0.05 seconds and cap 2 seconds. Equal jitter and decorrelated jitter are measured in the contention sim and are not the wire policy. Proactive refresh, when randomized, draws uniformly from `[0.5T, 1.5T]`. That draw is not applied to the short pre-commit retry.
- **Logs omit credentials.** `Store.log` rejects the field names `access_token`, `authorization`, `client_secret`, `refresh_token`, `secret`, and `sender_proof`. Error logs may carry `token_hash`, `family_id`, the error code, the generation, and the presenter client id.

## Limitations

- There is no browser, no authorization-code flow, and no PKCE. Password, implicit, and client-credentials grants are refused and mint nothing.
- Access tokens are not JWTs and not RFC 9068 tokens. The authorization server and the resource server share the fixture MAC key because they are one process.
- The sender proof is not RFC 8705 mTLS and not RFC 9449 DPoP.
- An unexpired access token still works after the refresh family is revoked.
- `CrashBeforeCommit` rolls back an in-process transaction. The tests do not kill the operating-system process.
- If the process died between the committed `in_flight` row and the order transaction, that key would answer 409 until its 24-hour expiry. The lab has no sweeper for abandoned `in_flight` rows.
- Webhook delivery uses five attempts on the virtual clock. It does not implement the multi-day schedule from the webhook specification.
- The goodput harness is a tick model: 48 clients, capacity 8 calls per tick, and an all-or-nothing overload rule. It does not show that a larger deployment is free of metastable failure. In this model the shed gate is what restores goodput; the retry budget and phase draw alone do not settle.
- Idempotency-Key behavior follows draft-ietf-httpapi-idempotency-key-header-07, which expired on 18 April 2026 and is not an RFC.
- Problem documents use type `about:blank`. They do not point at a private status catalog.

## What the tests demonstrate

`tests/` is `unittest`, offline, and deterministic aside from the short wall-clock waits that observe an in-flight order and a capped webhook delivery. The suite covers rotation replay from either party, the concurrent-rotation race, cross-client refusal, the section 5.2 error table, scope narrowing, inactivity, sender-constraint refresh, single-flight refresh, the pre-commit budget, dropped refresh and dropped order responses, idempotent orders, archive links and crash rollback, audience binding, webhook MACs and delivery, the Floyd phase spread, the jitter call counts, the no-trigger control, both brownout arms, and the shed/budget ablation. The demo numbers above are the same figures `tests/test_cli.py` reads from the JSON report.
