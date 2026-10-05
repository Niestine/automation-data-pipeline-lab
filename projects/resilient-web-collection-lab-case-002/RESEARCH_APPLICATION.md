# Research application

The crawler uses nine public sources. Each one below changes a behavior that the unit tests exercise. Local notes and archived copies are not part of this repository.

## Incremental frontier and the host gap

Cho, Junghoo, and Hector Garcia-Molina. "The Evolution of the Web and Implications for an Incremental Crawler." VLDB 2000. <https://www.vldb.org/conf/2000/P200.pdf>

The frontier keeps one durable row per URL and spends a fixed page budget each horizon. Importance is `1 / (1 + depth)`. When capacity is full, a new URL can replace the lowest-importance occupant that is outside the audit set, and only when the new page is strictly more important. The same-host gap is a fixed 10 seconds by default, including robots fetches and retries, so the lab has an explicit measurement interval. Because each URL is observed at most once per sample, several origin edits between two fetches count as one material detection. `tests/test_crawl.py` covers the gap, capacity eviction, the 410 slot release, and two edits inside one sample that raise the material count by one.

## Freshness, age, and refresh scores

Cho, Junghoo, and Hector Garcia-Molina. "Effective Page Refresh Policies for Web Crawlers." ACM Transactions on Database Systems 28, no. 4 (2003). <https://www.csd.uoc.gr/~hy561/papers/integration/crawling/Effective%20Page%20Refresh%20Policies%20For%20Web%20Crawlers.pdf>

Freshness is 1 only while the stored copy matches the origin, and age is the time since the first modification after the last sync. Later edits before the next sync do not move either value. The time average integrates the state at the left edge of each piece, so a sync on a boundary does not leak backward. Expected freshness is `e^(-λt)` and expected age is `t * (1 - (1 - e^(-λt)) / (λt))`. The default scheduler ranks due URLs by the freshness gain of syncing now, `w * (1 - E[freshness](λ, τ)) * (time-average freshness over T)`; the age objective ranks by `w * E[age](λ, τ)`. Both scores call the closed-form functions in `estimate.py`. Uniform and proportional baselines are included. This score is derived from the TODS definitions; it is not the paper's closed-form optimum. An audit floor of `max(1, budget // 10)` still visits zero-rate URLs. `tests/test_metrics.py` locks the integrals `(0.2, 3.2)` and `(0.6, 0.8)`. `tests/test_schedule.py` checks the closed forms at `λ = 1, t = 1`, checks both scores against hand-written formulas, and locks the eight-week ordering: proportional freshness is worse than uniform, the freshness policy is at least as fresh as uniform, and the age policy has the lower time-average age. That simulation uses its own batch selector with the same scores and audit floor. `tests/test_crawl.py` checks the crawler's own audit floor: with a binding budget, the oldest zero-rate URL is fetched while two positive-rate URLs wait.

The per-URL `λ` used by that score is the material-change count divided by the observation span. A URL that changes on every comparable sample is rate-censored to at least one change per horizon, which is the handling used for the field-notes page in the demo. `tests/test_crawl.py` checks that the censor flag clears once a later sample shows no change.

## Same-URL near-duplicate cut

Manku, Gurmeet Singh, Arvind Jain, and Anish Das Sarma. "Detecting Near-Duplicates for Web Crawling." WWW 2007. <https://static.googleusercontent.com/media/research.google.com/en//pubs/archive/33026.pdf>

The fingerprint is Charikar's simhash: casefold, tokenize, drop stopwords, hash each feature, add or subtract its weight per bit, and keep the sign. A zero coordinate becomes bit 0. Hamming distance is the popcount of the XOR against the fingerprint stored for that same URL. The cut is calibrated on the checked-in catalog texts (cosmetic distances 6 and 4, paragraph distance 24), which yields `k = 23`. The web-scale suggestion of 3 is not used. A checksum change at distance `<= k` is cosmetic and does not increment the material count. `tests/test_fingerprint.py` recomputes the calibration and checks that `examples/config.json` stores 23.

## Validator precedence

Fielding, Roy T., Mark Nottingham, and Julian Reschke, eds. "HTTP Semantics." RFC 9110. <https://www.rfc-editor.org/rfc/rfc9110>

A stored `ETag` is sent as `If-None-Match`, and `If-Modified-Since` is omitted for that request. `Last-Modified` is used only when the row has no `ETag`. The fixture applies `If-None-Match` first and compares validators weakly, so `W/"v"` matches `"v"`. A weak validator does not prove the stored bytes are unchanged: the first response with a weak `ETag` stores the checksum with `byte_identity_known` clear, and a later weak `304` does not count as a no-change rate sample or an oracle sync. `tests/test_observe.py` and `tests/test_crawl.py` cover both the strong and weak paths.

## Cached 304 and robots lifetime

Fielding, Roy T., Mark Nottingham, and Julian Reschke, eds. "HTTP Caching." RFC 9111. <https://www.rfc-editor.org/rfc/rfc9111>

A `304` carries no body. The crawler keeps the stored body and checksum and refreshes any validators the response includes. `Cache-Control: max-age` on a robots response overrides `Expires`; `no-cache` and `no-store` expire immediately, and so does an `Expires` value that is not an HTTP-date. The same header on a catalog page does not skip a URL that is due. `tests/test_robots.py` checks lifetime precedence, and `tests/test_crawl.py` checks that a fresh robots response is not downloaded again inside its lifetime.

## Robots exclusion

Koster, Martijn, Gary Illyes, Henner Zeller, and Lizzi Sassman, eds. "Robots Exclusion Protocol." RFC 9309. <https://www.rfc-editor.org/rfc/rfc9309>

The product-token group is the only group that applies. `*` is the fallback when the token matches nothing, and it is not merged into a specific match. Longest match wins, and an equal-length `Allow` wins over `Disallow`. Empty patterns add no constraint. `*`, `$`, and percent-decoding of unreserved octets are implemented. Path matching stays case-sensitive. A robots `4xx` is allow-all for the cached lifetime. A `5xx`, timeout, or redirect chain longer than five is disallow-all for the run and is fetched again on the next run. Rules are stored for the original catalog origin even when a redirect leaves that host. `tests/test_robots.py` replays the RFC's own examples (the Section 5.1 file with `*`, `foobot`, `barbot`/`bazbot`, and the empty `quxbot` group, and the Section 5.2 longest-match case), and together with the robots cases in `tests/test_crawl.py` covers these branches. `Crawl-delay` is not a rule in this protocol.

## 429 and 431

Nottingham, Mark, and Roy T. Fielding. "Additional HTTP Status Codes." RFC 6585. <https://www.rfc-editor.org/rfc/rfc6585>

`429` and `431` are retries. They consume retry budget and do not count as page observations. `431` is sent again with the single conditional validator the client already chose. A `Retry-After` above one hour defers the host instead of blocking the clock. `tests/test_crawl.py` covers `429`, `431`, `503`, and the long `Retry-After` deferral.

## Commit before send

Internet Archive. "Heritrix 3: Operating — Checkpointing and Crawl Recovery." <https://heritrix.readthedocs.io/en/latest/operating.html>

The frontier commits `next_request_at` and `status='in_flight'` before the fixture is called, using a full synchronous SQLite transaction. Recovery runs at the start of `Crawler.run`, resets in-flight rows to pending, and keeps the cooldown and the budgets. The interrupted GET is repeated once. A crash after the result transaction does not refetch that URL inside the horizon and does not refill the page budget. `tests/test_checkpoint.py` reopens the database between the crash and the resume.

## Full jitter

Brooker, Marc. "Exponential Backoff and Jitter." AWS Architecture Blog, 2015. <https://aws.amazon.com/blogs/architecture/exponential-backoff-and-jitter/>

The runtime delay is full jitter, capped at 300 seconds by default. Equal jitter and no-jitter exist only so tests can show the shape: no-jitter sleeps match across clients, full jitter stays inside `[0, cap]` and is not a single value, and equal jitter stays in the upper half of the same temp. `tests/test_backoff.py` runs those checks for 100 clients and checks that `Retry-After` is never undercut, including the HTTP-date form.
