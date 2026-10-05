# LLM Agent Evaluation & Guardrails Lab — Case 006

Offline tide desk for one harbor notice. The router asks scripted providers for a closed JSON berth slip and chooses the next provider with a quality-minus-cost cascade, a dollar budget, and a separate retry-token bucket. Nothing in this project calls a hosted model or the network. The suite uses the Python standard library.

The desk actor is `desk-alpha`. Vessels in the fixtures are `MV NORTHMARK`, `MV SOUTHLINE`, and `MV WESTREACH`. Berths are `B-4`, `C-2`, and `A-1`. Model ids are `skiff`, `yawl`, and `barque`. Every body is synthetic JSON under `examples/`.

## Problem

A desk that can call more than one model needs a routing rule that is allowed to start on an expensive model, a way to stop when the answer is good enough or the leftover budget cannot improve it, and a failure table that does not treat every HTTP 429 as the same event. Billing on one credential has to stop that credential. A rate limit has to back off, then be willing to try the same credential on a later request. Spend for the caller and retry tokens for the client are different ledgers.

The slip contract is a JSON object whose keys are exactly `berth`, `vessel`, and `window`, each a non-empty string. An invalid body scores 0, is not cached, is still charged, and the cascade continues. If no called model returns a valid slip, the route ends with a `contract-invalid` problem (HTTP 422).

## Architecture

```
RouteRequest
  -> input token/byte cap, caller request quota, caller token quota
  -> estimate each model: output_price * expected_output + input_price * input + per_request
  -> if the cheapest eligible estimate exceeds dollars: budget-exhausted, no call
  -> cascade: tau = quality_hat - lambda * cost_hat over supermodels
  -> FakeProvider.complete (max_retries is 0)
  -> vendor code, then HTTP status -> failure class
  -> retry token bucket and fake-clock backoff, or failover, or stop
  -> external scorer in [0, 1]; accept at the stage threshold
  -> exact cache of above-threshold slips
  -> 200 application/json slip, or one application/problem+json document
```

| Piece | Role |
| --- | --- |
| `cost` | Three-part price card. Rank is recomputed from the query's expected output length. |
| `routing` | Supermodel tau. Quality is the max member. Cost is the sum. Negative marginal tau prunes that set and its supersets. Gamma breaks ties. |
| `classify` | `error.code` / `error.type` win over the status class. Message text is ignored. |
| `bucket` | Capacity 500. Transient retry 14 tokens and 50 ms base. Throttling retry 5 tokens and 1000 ms base. Full jitter, cap 20000 ms. Integer Retry-After is a floor and is not clamped. |
| `router` | Product policy `cascade_routing`. Baselines `threshold_cascade` and `one_shot`. Per-caller dollars. One shared retry bucket. |
| `cache` | Key `(model_id, prompt_version, canonical query)`. Hits charge 0. |
| `problems` | RFC 9457 document. `type` is the class key. `detail` is not parsed. |
| `scorer` | Fixture table. With probability epsilon the score becomes `1 - g`. The answer text is ignored. |
| `fit` | Drops a costlier model that repeats a cheaper model's validation answers, and picks a threshold inside a cost budget. Unit-tested utilities; the shipped benchmark does not feed their output back into its config. |
| `evaluate` | Non-decreasing convex hull, AIQ, Zero, Oracle, and the epsilon scan. Oracle cost uses the full price card on each query's input length. |

Package path: `src/tide_route`.

A supermodel's quality with no uncertainty is the best member. After each call, the next candidate set must contain every model already used. The call itself is the cheapest unused member. When no unused member remains, the route returns the best valid slip it already holds and does not cache a below-threshold stop. A provider failure writes that model's quality as 0 so the cascade can still select another credential.

`gamma` of 0 always keeps the costliest tied supermodel and does not draw. A unique best does not draw.

The dollar ledger is per caller. The retry bucket is one bucket on the router. A charge uses reported token usage for successes, failures, and an SSE error after HTTP 200. A timeout with no usage (a timeout exception, HTTP 504, or `timeout_error`) reserves the pre-call estimate. The reservation is not reconciled later, because the fake provider never sends a late usage report. A retry must fit the remaining dollars before it pays retry tokens or sleeps. If the priced amount exceeds the remaining dollars, the charge is clamped and the trace records `consumption_spike`. The epsilon scan counts any clamped charge as a budget breach, so clamping cannot hide overspend in `within_budget`.

Block scope:

| `blocks` | Effect |
| --- | --- |
| `hold` | That credential is skipped for the rest of this `route()` only. Used when a rate limit does not allow failover. |
| `credential` | The credential stays blocked on later routes. Terminal billing and 401/403. |
| `model` | That model id stays blocked. HTTP 404. |
| `all` | The route stops. Request defects and HTTP 409. |
| empty | The cascade may continue. |

`failover_allowed` in a problem document means another model on the same credential may serve the request. A different credential can still be tried when its own estimate fits the budget, which is how a billing failure on one key fails over to another provider.

A 409 also records `(model, prompt_version, canonical query)`. A later route for that body gets a `conflict` problem with no provider call until the request sets `conflict_resolved=True`. Then exactly one call is made and the record is cleared.

The in-memory trace and the anomaly list are the observability record. The clock is a `FakeClock`; tests record sleep in milliseconds and do not wall-sleep.

## Run

From the repository root, with Python 3.10 or newer:

```bash
python -m unittest discover -s projects/llm-agent-evaluation-lab-case-006/tests -v
python projects/llm-agent-evaluation-lab-case-006/run_lab.py
python projects/llm-agent-evaluation-lab-case-006/run_lab.py --dry-run
python projects/llm-agent-evaluation-lab-case-006/run_lab.py --out report.json
```

`--examples` selects a directory that contains `eval_fixture.json` and `validation_split.json`. `--dry-run` prints the table and does not create `--out`. A missing file, malformed JSON, or a fixture with missing or mistyped fields prints one stderr line, `fixture_error` plus the exception class name, and exits 2. The process exits 0 when the table is printed.

The shipped fixture prints:

```text
tide-desk case-006
validation_kept skiff,barque
dry_run false
Q1 skiff 0.250000 ok calls=1
Q2 yawl 0.500000 ok calls=1
Q3 barque 1.000000 ok calls=1
ladder_mean_cost 0.916667 ladder_mean_quality 1.000000
oracle_quality 1.000000
product_aiq 0.851852
zero_aiq 0.722222
crossover_epsilon 0.4
```

Those three routes are the lambda `0.5` cascade. Their mean cost is `0.583333` at quality `1`. The lambda `5.0` cascade stays on `skiff` (mean cost `0.25`, mean quality `1/3`) because every pair has a worse tau. Those two operating points are the product hull on `[0.25, 1.0]`. The printed ladder is the cheap-to-expensive baseline at the same quality `1`, and it spends more. `crossover_epsilon 0.4` is the first scanned judge-noise value in `{0, 0.1, 0.2, 0.4}` where product AIQ is no longer above Zero. At epsilon `0` the product AIQ is the larger number.

Read that win carefully. On this fixture the ex-ante quality table is the gold table, so the lambda `0.5` cascade lands on the Oracle's choice for every query. The comparison shows that the tau rule, the stop rule, and the AIQ arithmetic work end to end. It does not show that a router with a realistic, noisy quality estimator beats Zero. The epsilon scan flips only the post-hoc scorer, and `test_noise_axes` corrupts ex-ante quality separately on a two-model fixture.

`validation_kept` is the output of `prune_agreeing` on `examples/validation_split.json`. That split is a separate toy fixture for the pruning utility. The benchmark does not apply it: all three models stay in the eval routes, and `yawl` still serves `Q2`. The threshold `0.5` and the lambdas `[0.5, 5.0]` in `eval_fixture.json` are hand-set, not fitted.

## Design decisions

The product path is cascade routing over any subset of the configured models. The threshold ladder exists so the evaluation can show a fixed cheap-to-expensive order spending more at the same quality on this fixture. One-shot tau routing is the ablation that moves when ex-ante quality is corrupted and stays put when only the post-hoc score is flipped.

Prices live on the model spec. A static order built from a short expected output is the wrong order for a long completion, and `test_long_output_reorders_price_cards` locks that flip.

OpenAI billing codes stay terminal for that credential, including when `error.type` is `insufficient_quota`. `slow_down` is a rate limit. `server_is_overloaded` stays HTTP 503. Claude 529 is overloaded and may fail over. A Claude 429 with no Retry-After gets one bounded backoff; the second consecutive bare 429 on that credential is terminal billing. Any success or other Claude response on that credential resets the count. OpenAI does not use that escalation. Both rules are labeled on the decision (`first_bare_429`, `second_bare_429`, `spend_limit_400`).

Timeouts are not retried. Transient 500s and network failures use the 14-token bucket. Throttling uses the 5-token bucket. A failover is a new cascade step, not a free retry. The provider's own retry count is fixed at 0.

The cache is the idempotency mechanism: the same model, prompt version, and canonical query return the stored slip and do not call the provider. A 429 is not served from cache on a later route. Whitespace-only differences collapse. A different `prompt_version` misses.

Problem types are absolute URIs under `https://portfolio.example/problems/`. `detail` is `{model_id} {vendor code} classified as {failure_class}`, built from identifiers only. The provider's message text is never copied. Catalog statuses include budget exhausted 402, caller quota 429, input too large 413, terminal billing 402, rate limited 429, overloaded 503, timeout 504, request defect 400, unavailable 503, conflict 409, unauthorized 401, network 511, and contract invalid 422.

## What this demonstrates

- A provider-neutral cascade that can open on the expensive model, prune a negative-marginal upgrade, and stop on a stage threshold or on a budget that cannot pay the next estimate.
- Structured output as a closed slip, with invalid JSON kept out of the cache.
- Vendor failure classification, request-scoped rate-limit holds, and persistent billing blocks.
- A dollar ledger per caller and a retry-token bucket with full jitter on a fake clock.
- Evaluation against a threshold ladder, single-model Zero, and an Oracle ceiling, including a judge-noise scan.
- Dry-run output and a non-zero exit on a bad fixture, with no live API.

## Limitations

Paper savings figures and published AIQ numbers are citations. This lab's comparison is the fixture printed above.

Lambda and the stage thresholds need a validation split from the same task distribution. Here they are hand-set. The shipped split is three synthetic models and two answer rows. `prune_agreeing` keeps `skiff` and `barque` on that split, and the benchmark ignores that result. A weak external scorer can make a cascade more expensive than the best single model. A binary pass/fail judge makes a threshold cascade degenerate, because the first model either stops the ladder or always falls through.

The scorer does not read the slip text. Gold and ex-ante quality on the eval fixture are the same 0/1 table. There is no learned router and no DistilBERT.

The archived RFC 9110 text used here does not include the HTTP-date ABNF, so only an integer Retry-After is parsed. An HTTP-date stays on the jitter path and can under-wait. The lab also does not replay a 409 inside the call that received it.

The bucket numbers are the AWS SDK standard-mode policy implemented locally. They are not an OpenAI or Claude retry contract. The Claude bare-429 escalation is a lab rule on top of the vendor table.

No hosted model is called. The provider is a script of status, error code, headers, and a slip body.
