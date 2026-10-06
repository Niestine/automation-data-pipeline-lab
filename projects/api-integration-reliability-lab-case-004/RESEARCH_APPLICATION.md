# Research application

The cold-lot grant lab uses ten public sources. Each section names the source, the code it changed, and the test that locks the behavior. Passages are not reproduced. The idempotency draft expired on 18 April 2026 and is not an RFC. The sender proof below is a lab HMAC, not the mTLS or DPoP mechanisms in RFC 8705 and RFC 9449. The numeric jitter formulas are the ones recorded for Marc Brooker's AWS Architecture Blog post; the archived HTML of that post dropped the formula images.

## Refresh-token families and replay revocation

- RFC 9700, Best Current Practice for OAuth 2.0 Security, https://www.rfc-editor.org/info/rfc9700
- RFC 6749, The OAuth 2.0 Authorization Framework, https://www.rfc-editor.org/info/rfc6749
- Auth0, Refresh Token Rotation, https://auth0.com/docs/secure/tokens/refresh-tokens/refresh-token-rotation

`AuthServer` stores a family with an active generation. A successful rotation consumes the presented refresh token and inserts the next generation in one SQLite transaction. Presenting an older generation sets the family to `reauth_required`, revokes the active descendant, and writes one `reuse_log` row plus a `refresh_reuse_detected` log. A later presentation of that family hits the status check and does not write a second reuse row. Client binding is checked before replay, so another confidential client cannot revoke the family. Rotation is a compare-and-set on the active generation, so two exchanges that race the same generation leave one rotation and one replay rather than an unhandled error. `tests/test_rotation.py` locks both presentation orders on the public kiosk: the legitimate client first and the thief first. It also locks the forced race, the single reuse row, and the cross-client refusal. `tests/test_audience.py` repeats the cross-client refusal with the limits of the Fett proof stated on the test.

## Single-flight refresh and a zero post-send budget

- RFC 9700, https://www.rfc-editor.org/info/rfc9700
- Bronson, Aghayev, Charapko, and Zhu, Metastable Failures in Distributed Systems, ACM HotOS 2021, https://sigops.org/s/conferences/hotos/2021/papers/hotos21-s11-bronson.pdf (DOI 10.1145/3458336.3465286)
- Auth0, Refresh Token Rotation, https://auth0.com/docs/secure/tokens/refresh-tokens/refresh-token-rotation

`GrantClient` holds one `threading.Lock` across the refresh, including retries. `POST_SEND_BUDGET` is 0. `ResponseDropped` after the handler has committed sets the client to `reauth_required` and does not resend. The server family stays `active` at generation 2 until something presents the old refresh token. `tests/test_refresh_controller.py` locks one send for two callers, two sends when the leader absorbs one pre-commit failure, and the dropped-response stop. The default demo and `--drop-refresh` print the same split.

## Closed token-endpoint errors and a pre-commit budget of three

- RFC 6749, sections 5.1, 5.2, and 6, https://www.rfc-editor.org/info/rfc6749
- Bronson et al., https://sigops.org/s/conferences/hotos/2021/papers/hotos21-s11-bronson.pdf
- Marc Brooker, Exponential Backoff And Jitter, AWS Architecture Blog, 4 March 2015, https://aws.amazon.com/blogs/architecture/exponential-backoff-and-jitter/

The token endpoint emits `invalid_request`, `invalid_client` (HTTP 401 and `WWW-Authenticate`), `invalid_grant`, `unauthorized_client`, `unsupported_grant_type`, and `invalid_scope`. `temporarily_unavailable` is not a token-endpoint code and is not in `TERMINAL_ERRORS`. HTTP 500 means the rotation transaction did not commit, so the client may retry it. The pre-commit budget is three, counting the first attempt. Password, implicit, and client-credentials grants mint nothing. `tests/test_rotation.py` and `tests/test_retry.py` lock the codes and the clock (terminal errors do not sleep). `tests/test_refresh_controller.py` locks the three-send cap.

## Capped jitter

- Brooker, Exponential Backoff And Jitter, https://aws.amazon.com/blogs/architecture/exponential-backoff-and-jitter/
- Bronson et al., https://sigops.org/s/conferences/hotos/2021/papers/hotos21-s11-bronson.pdf
- Floyd and Jacobson, The Synchronization of Periodic Routing Messages, IEEE/ACM Transactions on Networking, 1994, https://www.icir.org/floyd/papers/sync_94.pdf (DOI 10.1109/90.298431)

`retry.py` implements full jitter `random(0, min(cap, base * 2^attempt))`, equal jitter, and decorrelated jitter. The exponent stops at 62. Attempts 0 through 4 are unchanged. The wire path uses full jitter with base 0.05 seconds and cap 2 seconds. A one-slot contention sim with 32 clients records full, equal, decorrelated, and no-jitter call counts. Full jitter makes strictly fewer calls than the no-jitter arm. `tests/test_retry.py` redraws each formula with a second `Random` of the same seed. `tests/test_phase.py` locks the call counts.

## Proactive refresh phase

- Floyd and Jacobson, https://www.icir.org/floyd/papers/sync_94.pdf
- Bronson et al., https://sigops.org/s/conferences/hotos/2021/papers/hotos21-s11-bronson.pdf

The recorded countermeasure is a uniform draw on `[0.5T, 1.5T]`. `proactive_delay` returns `T` when randomization is off and that draw when it is on. `resume_after_outage` adds the same draw to the release tick. The short pre-commit retry does not use this draw; full jitter covers that. `tests/test_phase.py` uses `Random(100 + i)`, `T = 60`, and 32 clients. The spread is at least half a lifetime, and no one-second bucket holds more than eight clients.

## Idempotency-Key on restock orders

- draft-ietf-httpapi-idempotency-key-header-07, The Idempotency-Key HTTP Header Field, https://datatracker.ietf.org/doc/draft-ietf-httpapi-idempotency-key-header/ (archive text: https://www.ietf.org/archive/id/draft-ietf-httpapi-idempotency-key-header-07.txt)
- RFC 6749, https://www.rfc-editor.org/info/rfc6749

The header is an RFC 8941 quoted string. Lookup is `(client_id, method, path, key)`. The fingerprint is SHA-256 of method, path, and raw body. Authorization is not an input. Missing or unquoted keys are 400 and write no row. A schema failure is 400 and writes no row. A fingerprint mismatch is 422 and leaves the stored response. The first sight commits `in_flight`, then a second transaction writes the order and marks the row completed. An in-flight duplicate is 409. The client retries the same key and body with full jitter. The fault hook raises after the order insert, inside the transaction that would complete the row. The insert rolls back, the `in_flight` row is deleted, and the response is 500, so the retry runs and creates exactly one order. At 24 hours the row is deleted and the key can run again. One second earlier replays. The same key string on two clients is two rows. A resource 401 refreshes once and repeats the same key. A 403 does not refresh. `tests/test_orders.py` locks each of those states.

## Sealed archive pages

- RFC 5005, Feed Paging and Archiving, https://www.rfc-editor.org/info/rfc5005

The excursion feed is a mutable head plus sealed pages of 50. The cursor is the last entry id on the page. Link relations are `self`, `current`, `prev-archive`, and `next-archive`. Offset paging is not implemented. The client polls the head, upserts, and follows `prev-archive` until the cursor is checkpointed or the status is 403, 404, or 410. Those three statuses set `reconstruction_incomplete` and do not checkpoint the missing cursor. The page upsert and the checkpoint commit in one SQLite transaction. `CrashBeforeCommit` is raised inside that transaction so it rolls back. That is an in-process stand-in for a killed process. A later sync fetches the stable page and does not duplicate rows. A sealed page rejects `POST .../entries` with 409. A concurrent insert lands on the head only. `tests/test_archive.py` locks the 120-entry feed, the link graph, the crash, and the three stop statuses.

## Raw-body webhook signatures

- Standard Webhooks 1.0.0, https://github.com/standard-webhooks/standard-webhooks/blob/main/spec/standard-webhooks.md
- draft-ietf-httpapi-idempotency-key-header-07, https://datatracker.ietf.org/doc/draft-ietf-httpapi-idempotency-key-header/

The MAC input is `webhook-id`, a dot, `webhook-timestamp`, a dot, and the raw body, keyed with HMAC-SHA256. Secrets are 32 bytes derived in the lab and serialized as `whsec_` plus standard base64. Comparison uses `hmac.compare_digest` on the raw MAC bytes. A retry keeps `webhook-id` and mints a new timestamp and signature. A dot in the id or the timestamp is 400 and is not stored. A timestamp outside 300 seconds is 401 and is not stored. A bad MAC is 401 and is not stored. A duplicate id inside the 300-second retention is 204 and does not run the handler again. A body over 20 KiB is 413 before the MAC. A schema-valid signature with an invalid event is 400 and is not stored. During secret rotation either current `v1` signature is enough. The optional resource reader runs inside the same write transaction as the id insert; the demo reader is an in-process collection lookup, not a second bearer GET. `tests/test_webhooks.py` locks the MAC, the retention boundary, rotation, and the rollback when the reader raises.

## Delivery policy on the virtual clock

- Standard Webhooks 1.0.0, https://github.com/standard-webhooks/standard-webhooks/blob/main/spec/standard-webhooks.md
- Brooker, Exponential Backoff And Jitter, https://aws.amazon.com/blogs/architecture/exponential-backoff-and-jitter/
- Bronson et al., https://sigops.org/s/conferences/hotos/2021/papers/hotos21-s11-bronson.pdf

The producer allows five attempts. Success is any 2xx. A 3xx records `Location` and is a failure; the next attempt still uses the original URL. HTTP 410 disables the endpoint. `Retry-After` as a finite, non-negative number is the whole delay and does not also draw jitter. Other values fall back to jitter. Other failures use full jitter. The receiver timeout value on each attempt is 15 seconds. `max_in_flight` defaults to 1, and a second delivery while one is inside the receiver returns `capped`. A `timed_out` result counts as a failure. This is a compressed schedule. The specification's multi-day ladder is not what the lab runs. `tests/test_webhooks.py` locks the 200 stop, the redirect, the 410 retirement, a `Retry-After: 7` advance of exactly 7 seconds, five failures, and the in-flight cap.

## Fail-closed token binding

- RFC 9700, https://www.rfc-editor.org/info/rfc9700
- RFC 6749, https://www.rfc-editor.org/info/rfc6749
- Fett, Küsters, and Schmitz, A Comprehensive Formal Security Analysis of OAuth 2.0, ACM CCS 2016, https://arxiv.org/abs/1601.01229

The resource server checks the HMAC, then `iss`, `aud`, `exp` (failing when `exp <= now`), and the route scope, before any idempotency lookup. A lot-ledger token is refused by the alarm board. A mutated `aud` with the old MAC, a re-signed wrong issuer, an expired `exp` with a valid MAC, and a flipped MAC byte are all 401 and write no idempotency row. Two grants do not share entry ids. The 2016 proof does not expire access tokens, does not model revocation, and does not prove refresh-family replay. The tests encode the fail-closed shape only. `tests/test_audience.py` carries that limit in its docstring.

## Scope, storage, and inactivity

- RFC 6749, https://www.rfc-editor.org/info/rfc6749
- RFC 9700, https://www.rfc-editor.org/info/rfc9700

An omitted scope keeps the grant on the access token and omits the JSON `scope` field when it equals the refresh scope. A requested subset narrows the access token, returns `scope`, and leaves the family's refresh scope unchanged. An extra member is `invalid_scope` and does not rotate. The resource server reads the access token's scope. `orders.write` is required for `POST /orders`; a narrow token is 403 and does not refresh. Refresh tokens are 32 bytes from `secrets.token_bytes`, which is within the section 10.10 guessing bound. They are urlsafe base64 without padding and stored only as the SHA-256 hex of the token string. Every token response, including errors and the pre-commit 500, sends `Cache-Control: no-store` and `Pragma: no-cache`. Logs refuse raw token and secret field names. Inactivity is three access lifetimes. The refresh fails with `invalid_grant` when `now - last_used_at` is greater than that window. It does not rotate and does not write a reuse row. Exactly three lifetimes still refreshes. An access token expires on its own `exp` while the family is inside the window. `tests/test_rotation.py` locks these, including the fact that revoking the refresh family does not invalidate an unexpired access token.

## Public rotation and the sender-constraint stand-in

- RFC 9700, https://www.rfc-editor.org/info/rfc9700
- RFC 6749, https://www.rfc-editor.org/info/rfc6749

A public client must be registered as `rotation` or `sender_constraint`. Confidential clients use HTTP Basic and rotate after the secret check. The public kiosk sends `client_id` and no Basic header, and its generation advances. Sender-constraint mode checks an HMAC over `POST./token.{timestamp}` with a 300-second tolerance. A correct proof returns a new access token and does not consume the refresh generation. A wrong proof is `invalid_grant` and does not revoke the family. Rotation mode ignores sender fields. The sender key is derived from a fixture pepper and the public key id, so the stand-in shows the server branch RFC 9700 requires, not proof of possession. `tests/test_rotation.py` locks registration, the proof window, and the generation that stays at 1.

## Fixture-scale goodput

- Bronson et al., https://sigops.org/s/conferences/hotos/2021/papers/hotos21-s11-bronson.pdf
- Brooker, Exponential Backoff And Jitter, https://aws.amazon.com/blogs/architecture/exponential-backoff-and-jitter/

`simulate_brownout` is a tick model. Forty-eight clients hold access tokens with a lifetime of 8 ticks and staggered expiry, so 6 token calls arrive per tick against a capacity of 8. When more calls arrive than the endpoint can serve and nothing sheds them, every call times out at 2 seconds and no call succeeds; that is the one overload rule. The trigger is a brownout on ticks 20 through 27, when every call times out. Without the trigger, both arms stay at 48 clients with valid tokens and under capacity, which `tests/test_goodput.py` checks as the control. With it, the unbounded arm retries every tick and has zero goodput for every tick after the brownout ends, with attempts above capacity. That is a sustaining loop, not the trigger. The budgeted arm uses the pre-commit budget of 3, full jitter between attempts, a `[0.5T, 1.5T]` resume draw after the budget is spent, and `ShedGate.reject`, which serves capacity and answers the excess with 503, `Retry-After: 1`, and the no-store headers. The shed path does two mutations and does not walk the stack. It returns to 48 and stays at or under capacity. Timed-out calls stay in the latency sample. An ablation test separates the mitigations. In this model, shedding is what restores goodput: unbounded retries plus the shed gate also recover, and the budget without the shed gate does not settle. The budget's measurable effect is fewer token attempts and fewer sheds during recovery, at the cost of recovering later. These numbers are the fixture. They do not show that a deployment at the paper's larger query rates is free of metastable failure. `tests/test_goodput.py` locks the control, both arms, and the ablation.
