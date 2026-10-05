# Research application

Case 005 maintains one in-process client, Harbor Gauge Bulletin, across OpenAPI response and schema revisions. Six public sources shaped the classifiers, the header grammars, the schema readers, and the tests. The suite runs offline on synthetic fixtures.

## Techniques used

### 1. Classify the diff before the version, twice

Source: Souhaila Serbout and Cesare Pautasso, *How Many Web APIs Evolve Following Semantic Versioning?* (ICWE 2024).
<https://souhaila-serbout.me/pdfs/2024_APIACE_ICWE_VersioningVsChange.pdf>

`changes.py` emits structural ids, then maps each id to breaking, non-breaking, or undecidable for a strict reader and again for a tolerant reader. `compliance.py` scores the history only after that map. A patch deletion is `leaking`. A minor response-property addition is `leaking` for strict and `compatible_only` for tolerant (`examples/histories.json`, `tests/test_compliance.py`). An unparseable version does not erase the ids (`tests/test_changes.py`). A release with an undecidable id is excluded from every bucket: a decided leak elsewhere still makes the history `leaking`, and otherwise the history is `undecidable` (`tests/test_compliance.py`).

The version tuple is the paper's prose: optional `v`, one to three numeric components, optional label. `versions.py` does not compile the whitespace-damaged expression from the PDF extract. `1.2.3-alpha` to `1.2.3` stays a label change (`tests/test_versions.py`).

Cited figures are stored separately in `compliance.cited_figures` and `examples/measured_metrics.json`: 517, 1,970, and 927 are not a partition of 3,075; 16,053 and 15,856 are both kept; 96 + 66 + 33 = 195. `tests/test_compliance.py` locks those equalities and the inequality of the sum against 3,075.

An unnecessary major bump with only non-breaking edits is `compatible_only`. The paper's counts do not support an extra penalty class for that bump.

### 2. Deprecated, still callable, then removed

Source: Jerin Yasmin, Yuan Tian, and Jinqiu Yang, *A First Look at the Deprecation of RESTful APIs: An Empirical Study* (2020).
<https://arxiv.org/abs/2008.12808>

An operation is the method plus the path template. It is impacted when the operation, a parameter, or a response field is `deprecated: true`, including a field reached through local `$ref` (`tests/test_retirement.py`). The middle document of a three-document history still returns the fixture object and one description-channel warning (`tests/test_client.py`). Removal with no earlier mark is `unmanaged`, including when the version step is a major upgrade. `review_history` keeps `protocol_ok` separate from the compliance class.

The 2020 observation used by the metrics command: of 251 breaking versions, 87.3% had no prior deprecation mark; of 219 deprecation-related APIs, 3 used a proactive channel. Those figures are printed as study counts.

Replacement text uses one lab rule, `Replacement:`, documented in `retirement.py`. The paper's replacement miner is unpublished here and is not implemented. A sunset notice is not treated as the deprecation mark the study counted.

### 3. Deprecation header grammar and clock order

Source: RFC 9745, *The Deprecation HTTP Response Header Field*.
<https://www.rfc-editor.org/rfc/rfc9745>

`parse_deprecation` accepts only `@` plus an integer of at most 15 digits (the RFC 9651 sf-integer bound); an out-of-range instant is a `HeaderGrammarError`, not an `OverflowError`. `@1688169599` is `2023-06-30T23:59:59Z`. A future instant is phase `scheduled`. A past instant is `already_deprecated`. The business object is the same in both cases (`tests/test_headers.py`, `tests/test_client.py`). `Link` with `rel=deprecation` stores the target and is not fetched. Without a Deprecation date the phase stays `not_deprecated`.

When Sunset is earlier than Deprecation, the client emits `clock_order_error` and does not swap the instants. Equal instants do not emit that fault. The RFC pair `@1688169599` with `Sun, 30 Jun 2024 23:59:59 UTC` is ordered and legal. An HTTP-date string fails the Deprecation parser.

### 4. Sunset as a separate HTTP-date

Source: RFC 8594, *The Sunset HTTP Header Field*.
<https://www.rfc-editor.org/rfc/rfc8594>

`parse_sunset` accepts an IMF-fixdate with `GMT` or the `UTC` token used in the RFC 9745 example. It rejects `@` integers. Oracles include `Sat, 31 Dec 2018 23:59:59 GMT` and `Sun, 30 Jun 2024 23:59:59 UTC`. The parser accepts the weekday token as written and does not correct it against the calendar.

A past instant, and an instant equal to now, is phase `elapsed`. A future instant is `announced`. On a scripted 200 after sunset the client records `sunset_elapsed` with status 200 and does not raise. HTTP 410 at or after sunset also records `eol_transition` with detail `status_410`. A connection error records `sunset_elapsed` with an empty status and `transport_error`. A configured base-URL switch records detail `base_switch`. A 301 after sunset records `sunset_elapsed` with status 301 and is not decoded with the 200 schema. Sunset does not choose the status. A future sunset plus 410 records neither elapsed nor end of life (`tests/test_client.py`).

Header scope is the request URI. A sibling URI inherits the warning only when the caller passes `policy_map` and the source URI was already observed.

### 5. Unevaluated properties and detailed output

Source: *JSON Schema: A Media Type for Describing JSON Documents*, draft 2020-12, core specification.
<https://json-schema.org/draft/2020-12/json-schema-core>

`readers.as_strict` sets `unevaluatedProperties` to false after annotations from `properties`, `patternProperties`, and `additionalProperties` are collected, including annotations that arrive through `allOf`, local `$ref`, and a successful `if` plus the taken `then` or `else` branch. `as_tolerant` omits the keyword, and `project` copies only the names that the tolerant evaluation annotated. The same unknown sibling fails strict and passes tolerant. `instanceLocation` is the JSON Pointer of that sibling, with `~` and `/` escaped (`tests/test_readers.py`).

The negative control sets root `additionalProperties` to false. That keyword does not see properties declared inside `$ref`, so `gauge_id` fails the control and passes the strict reader. Failure nodes snapshot `instanceLocation`, `keywordLocation`, `absoluteKeywordLocation`, and `error`. The other three output formats named by the core specification are not assertion targets.

Integer checks are local and defensive: a JSON boolean is not an integer, and `1.0` is not an integer. The validation vocabulary is a different document and is not implemented.

### 6. Side-specific rule ids, stored beside compliance

Source: oasdiff, *OpenAPI Breaking Changes: The Complete List of Rules*.
<https://www.oasdiff.com/docs/breaking-changes>

`rules.py` seeds 32 directional ids. Request and response rows are distinct. Each fixture in `tests/test_rules.py` must emit that id and no twin: a widened type list does not emit the narrowed id, and a request `contentEncoding` change does not emit the response id. The optional read-only id is stored as `response-optional-property-became-read-only`.

The Breaking or Info level is transcribed and tested. `impact_for` does not copy that level into the Serbout buckets. Widening a response type list is Breaking on the oasdiff line and still `undecidable` for both readers. Narrowing a response type list is Info and still `undecidable`. The tolerant exception is explicit: `response-body-type-compatible` is non-breaking for the tolerant reader and breaking for the strict reader. Ids ending in `-type-changed` follow the type-modification rule (strict breaking, tolerant undecidable).

## How the tests use this

| Area | Tests | What is locked |
| --- | --- | --- |
| Version prose and history classes | `test_versions.py`, `test_compliance.py`, `test_changes.py` | Oracle rows, reader split, separate cited counts |
| Retirement protocol | `test_retirement.py`, `test_client.py` | Managed versus unmanaged removal, description marker, still-callable middle call |
| Header grammars | `test_headers.py`, `test_client.py`, `test_logging.py` | Cross-grammar rejection, clock order, sunset versus 410 versus transport error |
| Composed schemas | `test_readers.py`, `test_client.py` | `$ref` annotations, additionalProperties control, detailed-output pointers |
| Directional catalog | `test_rules.py` | 32 exact ids, levels ignored by compliance |
| Commands | `test_cli.py` | `metrics`, `classify`, `call` on the checked-in fixtures |

## References counted

`portfolio_manifest.json` sets `reference_count` to 6, one for each source above. Semantic Versioning 2.0.0, the JSON Schema validation vocabulary, and the OpenAPI 3.1.1 specification body were read as neighboring context. The lab pins `openapi: 3.1.1` on fixtures and does not implement those three documents, so they are not counted.
