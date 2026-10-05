# Python Maintenance & Regression Lab — Case 005

Harbor Gauge Bulletin is a synthetic tide-reading API, `GET /gauges/{gauge_id}`. The lab is an in-process client that maintains that contract across OpenAPI revisions. A response can stay numerically the same while the schema, the version label, and the retirement notice each tell a different story. The suite locks those three stories apart.

The fixtures are synthetic. The client never opens a socket. Every claim below is exercised by `python -m unittest`.

## Problem

A maintainer inherits a small JSON API whose documents keep moving:

- A minor bump adds a response field. A strict reader must change. A tolerant reader can keep projecting the fields it already knows.
- A patch bump deletes a field. The call still returns HTTP 200, and the version label hides the break.
- An operation disappears. A major bump does not make that removal managed. A prior `deprecated: true` does.
- `Deprecation` and `Sunset` use different grammars. A client that parses both with one date parser mis-orders the window.
- A composed schema declares `gauge_id` inside `$ref`. Setting root `additionalProperties` to false rejects that property. `unevaluatedProperties` sees it.

The maintenance task is to classify the document pair, score version honesty per reader, walk the deprecation protocol, and replay a scripted call with a structured log. Those results stay in separate fields. A version leak does not fail the business call. An unmanaged removal does not become a green compliance score.

## Architecture

```
examples/*.json  -->  contract_lab
                         versions.py        (major, minor, patch, label)
                         changes.py         structural ids, then impact per reader
                         rules.py           32 seeded directional ids and their levels
                         compliance.py      consistent | leaking | compatible_only | undecidable
                         retirement.py      deprecated, still callable, then removed
                         http_lifecycle.py  Deprecation, Sunset, Link
                         readers.py         strict unevaluatedProperties, tolerant projection
                         client.py          scripted call, events, log
                         __main__.py        metrics | classify | call
```

`ContractClient` resolves the method and path template, records description-channel marks, parses the scripted headers, validates the body with both readers against the JSON schema documented for the returned status code, and returns the object selected by the active reader. A status with no documented schema (a redirect, a 410) is recorded and not decoded. Events go to the `contract_lab` logger. The library adds only a `NullHandler`, and records propagate normally, so an application's root handler sees them. Each record's message is `kind operation {json of non-empty fields}`, and the full event dict is attached as `record.event`. The command line writes JSON to stdout and, for `call`, log lines to stderr. It returns 2 for an unreadable or malformed fixture and 1 for a contract failure such as `SchemaRejected`, with a one-line message instead of a traceback.

Package layout:

| Path | Role |
| --- | --- |
| `src/contract_lab/` | Library. Standard library only. |
| `tests/` | Offline `unittest` modules. `helpers.py` inserts `src` on `sys.path`. |
| `examples/` | Version oracle, cited counts, one two-document history, one description-channel call. |
| `run_lab.py` | Project-local entry point. |

## Maintenance workflow

1. Diff the previous OpenAPI document against the next one. Change ids are produced before the version string is parsed. An illegal version such as `20240101` leaves the ids in place.
2. Score the history twice. `examples/histories.json` adds `spare_note` on `1.2.0` to `1.3.0`. Strict is `leaking`. Tolerant is `compatible_only`.
3. Walk retirement separately. A removal is `managed` only when an earlier document set `deprecated: true` on that operation, parameter, or response field. `x-sunset` is a timing notice, not that mark. `protocol_ok` is its own boolean.
4. Replay the call with `ContractClient`. A deprecated operation still returns the fixture object and one warning. The description channel reads the replacement from the marker `Replacement:`. The header channel leaves replacement empty. The two channels are recorded on their own events.
5. Read the log. `deprecation_warning` and `schema_rejected` are warnings. `clock_order_error` is an error. Other events are info.

Example `call` log line (stderr):

```text
WARNING contract_lab deprecation_warning GET /gauges/{gauge_id} {"channel": "description", "element_pointer": "#/paths/~1gauges~1{gauge_id}/get", "replacement": "/stations/{station_id}"}
```

## Run

From the repository root:

```text
python -m unittest discover -s projects/python-maintenance-regression-lab-case-005/tests -v
```

Offline commands, also from the repository root:

```text
python projects/python-maintenance-regression-lab-case-005/run_lab.py metrics
python projects/python-maintenance-regression-lab-case-005/run_lab.py classify projects/python-maintenance-regression-lab-case-005/examples/histories.json
python projects/python-maintenance-regression-lab-case-005/run_lab.py call projects/python-maintenance-regression-lab-case-005/examples/call_description.json
```

`metrics` prints the cited study counts and `seeded_rule_count` 32. `classify` prints `compliance.strict`, `compliance.tolerant`, and `protocol_ok`. `call` prints the business object, the event list, and the phase. The default clock is `2024-06-01T00:00:00Z` when a fixture omits `now`.

Python 3.10 or newer (the suite was run on 3.10 and 3.13). No third-party package is installed.

## Design decisions

**Version tuple.** Parsing follows the prose used by Serbout and Pautasso: optional `v` or `V`, one to three numeric components of one to three digits, optional label in `{alpha, beta, dev, snapshot, rc, preview, test, private}`. Missing minor and patch become 0. Components compare as integers. `1.9.0` to `1.10.0` is a minor upgrade. `1.2.3-alpha` to `1.2.3` is a label change. Build metadata (`1.2.3+build`), a fourth component, a four-digit component, and an empty or unknown label are rejected. The whitespace-damaged expression in the PDF extract is not compiled. Semantic Versioning 2.0.0 pre-release precedence is not applied.

**Reader-conditional impact.** Path and operation addition is non-breaking for both readers. Response-property addition is breaking for strict and non-breaking for tolerant. Request-parameter addition, required-element addition, and response-property deletion are breaking for both. Nullable and optional flag edits stay undecidable. A history is `consistent` when every breaking release is a `major_upgrade`, `leaking` when a break rides a smaller step, a label change, no change, or any downgrade, and `compatible_only` when no release is breaking. A release with an undecidable id or an unparseable version is excluded from every bucket. One decided leak elsewhere still makes the history `leaking`; otherwise that release makes the history `undecidable`, because `consistent` and `compatible_only` are claims about every release. An unnecessary major bump that only adds a path stays `compatible_only`.

**Directional ids.** Thirty-two oasdiff rule ids are seeded with the Breaking or Info level transcribed beside each id. That level is stored and asserted. Compliance does not read it. List widening, content-type flips, and read-only flips therefore stay `undecidable` for both readers. The one tolerant exception is `response-body-type-compatible` (`integer` to `number` on a response body): breaking for strict, non-breaking for tolerant. Ids ending in `-type-changed` are type modifications: breaking for strict, undecidable for tolerant. Request and response ids are separate, so a request `contentEncoding` edit does not emit the response twin.

**Retirement marker.** Replacement text is the trimmed remainder of the first `Replacement:` marker. An empty marker and prose without the marker yield no replacement. The lab does not mine free-form sentences. Child parameters and fields that vanish because the whole operation was removed are covered by that one operation record. Nested response fields are named by dotted path (`from_station.name`), so two fields with the same leaf name, or two properties that share one `$ref`, are tracked separately.

**Headers.** `Deprecation` accepts only `@` plus an integer Unix second. `@1688169599` is `2023-06-30T23:59:59Z`. `Sunset` accepts an IMF-fixdate with `GMT` or `UTC` and rejects `@` integers. The weekday token is stored as written: `Sat, 31 Dec 2018` is the RFC 8594 example even though that calendar day is a Monday. Sunset earlier than Deprecation emits `clock_order_error` and leaves both instants unswapped. Equal instants are clean. A `Link` with `rel=deprecation` stores the target and is never fetched. Without a `Deprecation` date the phase stays `not_deprecated`. Header scope is the request URI. A sibling URI inherits a warning only through an explicit `policy_map` after the source URI has been observed. A past sunset on HTTP 200 records `sunset_elapsed` and status 200. HTTP 410 at or after sunset also records `eol_transition`. A transport error stays a transport error. A future sunset plus 410 does not record end of life. A deprecated call does not raise. An illegal header that is present raises `HeaderGrammarError`, including a `Deprecation` integer longer than the 15 digits RFC 9651 allows or outside the representable date range. A redirect after sunset is recorded with its status and is not decoded with the 200 schema.

**Schema readers.** The strict copy sets `unevaluatedProperties` to false after collecting annotations from `properties`, `patternProperties`, and `additionalProperties`, including those reached through `allOf`, local `$ref`, and the taken `if`/`then`/`else` branch. A root `additionalProperties: false` is dropped from the strict copy; a schema-valued `additionalProperties` is kept. The tolerant copy omits `unevaluatedProperties` and projects the property names its own evaluation annotated, so an unknown sibling, an untaken `else` branch, or an unreferenced `$defs` entry never reaches the business object. The negative control sets root `additionalProperties` to false and rejects `gauge_id` when that name exists only inside `$ref`. Failures are detailed-output nodes (`instanceLocation`, `keywordLocation`, `absoluteKeywordLocation`, `error`). A boolean is not an integer, and `1.0` is not an integer.

## What the suite demonstrates

- Version-oracle accuracy on the checked-in table, including rejections.
- The strict/tolerant split on one added response property at minor scope.
- Patch deletion classified as `leaking`, a quiet path addition as `compatible_only`, and a major deletion as `consistent`.
- A major bump left beside an unmanaged removal: compliance can be `consistent` while `protocol_ok` is false.
- Description-channel and header-channel warnings, stable business objects, reversed clocks, and sunset versus 410 versus transport failure.
- The `$ref` property accepted by the strict reader and rejected by the `additionalProperties` control.
- Each of the 32 seeded directional fixtures emitting exactly its own id.
- Log lines for `deprecation_warning` and `clock_order_error`.
- The three commands above, on the checked-in JSON fixtures.

## Limitations

The break model is a static, conservative classifier over fixture documents. It compares the top-level `200` `application/json` schema and the top-level request body. Change ids do not expand `allOf` or `$ref`; the call-time readers do. Readers resolve only `$ref` values local to the response schema (`#/$defs/...`); an OpenAPI `#/components/schemas/...` reference is reported as a `$ref` failure rather than resolved. The reader implements `type`, `enum`, `required`, `properties`, `patternProperties`, `additionalProperties`, `allOf`, `$ref`, `if`/`then`/`else`, and `unevaluatedProperties`; other keywords are ignored.

The seeded table is 32 directional rules plus the structural ids in `changes.py`. It is the extract's classifier and that seeded subset. It is not the full 195-row change catalog (96 breaking, 66 non-breaking, 33 undecidable). Removal after a published sunset stays `undecidable` because the extract does not give that cell. Removal at the sunset instant uses the same cell.

The cited counts are separate findings. 517 best-case consistent APIs, 1,970 leaking APIs, and 927 never-breaking APIs are not added together, and that sum is not the 3,075 studied APIs. The abstract's 16,053 new versions and the dataset section's 15,856 versions are both stored. 87.3% of 251 breaking versions, and 3 of 219 deprecation-related APIs, describe the 2020 study. They are not a current deployment rate.

`Deprecation` and `Sunset` are hints on a scripted response. Sunset does not choose among 4xx, 3xx, and connection failure. The client records the status it was given, or records a transport error with status left empty. Integer checks in the reader are defensive local checks. They are not the JSON Schema validation vocabulary, where `1.0` can be an integer. Version comparison is not Semantic Versioning 2.0.0 precedence.
