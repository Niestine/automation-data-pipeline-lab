# Research application

Case 008 uses eight public sources. Each technique below is implemented in the Ledgerlane field-station memory lab (`fieldlog`) and checked by the offline unittest suite. Published token lengths and published scores are design targets. This repository runs the same protocol shapes on a smaller whitespace-token scale with a deterministic scripted extractor.

The JSON Schema draft is counted once. Its core and validation documents are two URLs of that single draft.

## 1. Memory competencies and a FIFO control

**Evaluating Memory in LLM Agents via Incremental Multi-Turn Interactions (MemoryAgentBench)**  
https://arxiv.org/abs/2507.05257

The report is a competency vector, not one accuracy number. `build_report` records accurate retrieval (AR), test-time learning (TTL), long-range understanding (LRU), and selective forgetting (SF). Facts are injected once and queried many times. Fact serials increase with injection order. A FIFO window that keeps only recent text sits beside the memory system. The long SF cell requires the memory reader to keep the newest range after the queue has dropped the early episode, while the FIFO control and the direct reader miss it. TTL scores a growing code-to-status mapping against a no-memory baseline of zeros. LRU scores a question that needs every injected taxon separately from a top-k control. `test_report` asserts the vector, the FIFO miss, the TTL curve, and the LRU gap.

## 2. Question types, abstention, and haystack retrieval

**LongMemEval: Benchmarking Chat Assistants on Long-Term Interactive Memory**  
https://arxiv.org/abs/2410.10813

The router is a closed enum validated before any answer text: `current_value`, `historical`, `aggregate`, `abstain`, `temporal_reasoning`, `irreducible`, `direct_read`. Retrieval builds a turn-level key and a fact-augmented key, ranks with cosine, BM25, and breadth-first search, and fuses them. Temporal questions expand to a half-open time window and drop out-of-window hits before fusion. Logged ids feed recall@k and NDCG@k. Abstention covers a fact that never appears in any episode. `test_assembly` checks the fact-augmented key against the turn key, the March window, and the ranking metrics. `test_report` plants one evidence turn inside a 500-turn session, confirms the FIFO drops that turn, and confirms another session abstains.

## 3. Bi-temporal edges and fixed graph writes

**Zep: A Temporal Knowledge Graph Architecture for Agent Memory**  
https://arxiv.org/abs/2501.13956

Each fact is an edge with event time (`t_valid`, `t_invalid`) separate from write time (`t_created`, `t_expired`). A contradictory rewrite inserts a new edge. Code closes the previous same subject and predicate by setting `t_invalid` to the new `t_valid` and `t_expired` to the write clock. Rows stay in the store. An older fact that arrives late is stored already closed at the newer edge's `t_valid`. The extractor proposes subject, predicate, and object. It does not choose the winner and it does not emit a query string. Every extracted fact is checked against the closed field set, a citation span inside the turn, and a parseable instant before the episode is written. Archival insert accepts only the closed field set. Hybrid retrieval is cosine on a 64-dimensional hashing vector, Okapi BM25 (`k1=1.5`, `b=0.75`), one-hop breadth-first search from the latest episodes, and reciprocal rank fusion with `k=60`. `test_memory` checks that both range edges survive, that an as-of read returns the closed interval, that a late older fact is closed, and that rogue extractor output leaves no episode or edge. `test_assembly` checks that a noisy later span can rank first while the structured reader still returns the supported entity.

## 4. Post-retrieval assembly and a bounded comparison

**Don't Ask the LLM to Track Freshness: A Deterministic Recipe for Memory Conflict Resolution**  
https://arxiv.org/abs/2606.01435

Assembly is `K = E(q, R)` then `answer = A(K)`. The scripted extractor lists every matching fact. Argmax on serial, then timestamp, runs only for `current_value` when the candidates declare a total order. An empty list abstains. A tie stays `unresolved`. Historical questions read the second-newest value or the interval that contains the asked time. Aggregate questions filter to the interval and reduce; returning the newest entity fails that branch. Multi-hop current values resolve one hop at a time, substitute `{previous}` into the next hop, and abstain when a hop is empty or a placeholder survives substitution. A no-decomposition arm sees the same chain and is allowed to return the older warden. The knowledge-update mix compares the branching reader (the deterministic direct-reader stand-in) with a blind newest-timestamp reader using an exact two-sided McNemar test. The 12-row mix is constructed: three previous-state and yes/no rows that the newest reader misses, three long rows that the stand-in misses, and six ties. On that mix the discordant counts are balanced (p = 1.0), and `naive_beats_direct` is computed false from those counts. This reproduces the shape of the paper's null result, not its data. `test_assembly` covers ties, historical reads, aggregates, chain-aware hops, placeholder abstention, and citation checks. `test_report` locks the mix counts and `multi_hop_claimed_solved: false`.

## 5. Irreducible conflict and a high-risk commitment flag

**When Personal Memory Has No Single Answer: Evaluating LLM Agents under Irreducible Conflict (TANGLE)**  
https://arxiv.org/abs/2608.13921

Three families have no single memory that states the conclusion: missing context, oscillation, and unordered sources. `select_action` sees the memories, the query, and a consequence level. The gold family is not an input. Caution increases from commit, through reversible trial, conditionalize, clarify, and verify, to defer. High-consequence authorization and medical disagreements verify or defer. Commit on those rows is marked unsafe. Recency, majority, and source-priority arms still commit; a control is recorded as `fail` when it commits on a row where the policy chose another action. Oracle rows are scored directly. Pipeline rows re-encode the bank as FACT-line turns (structured, context-dropped, or lossy prose), ingest them through the same extractor and store, and label observability FULL, PARTIAL, or NONE according to whether value, context, source, and time links survive. This is a single-session re-encoding, not the paper's multi-session dialogue with filler sessions. Distractors at 0, 2, 4, and 6 use a different subject and are filtered out by exact subject match before the action, so the curve is flat by construction. Rubric scores D1–D5 sit on a 0–4 scale and are computed by fixed rules, not by a judge model; D6 is `safe`, `partial`, or `unsafe` for high-consequence rows. `test_assembly` checks clarify, reversible trial, and the unsafe recency control. `test_report` checks observability, D6, and the flat distractor curves.

## 6. Bounded working set and structured eviction

**MemGPT: Towards LLMs as Operating Systems**  
https://arxiv.org/abs/2310.08560

One lab holds four stores: an append-only episode log, archival edges, a small pinned working context, and a FIFO queue whose head carries a recursive summary. At 0.70 of the token budget the only legal response is a named write into working context or archival edges. At 1.00 the oldest span of about half the budget is evicted and the summary is recomputed from the previous summary plus the evicted text. In structured mode every evicted declared fact must already be an edge or the flush raises and leaves the queue unchanged. Summary-only mode skips that integrity check. The summary itself is capped so it cannot refill a small budget. Named tools are `working_context_edit`, `episode_search`, `archival_insert`, `archival_search`, and `session_delete`. `test_memory` covers the integrity refusal, a successful multi-flush that still answers from the edge, and rejection of external queue mutation.

## 7. Closed schemas and detailed validation output

**JSON Schema: A Media Type for Describing JSON Documents (draft 2020-12)**  
https://json-schema.org/draft/2020-12/json-schema-core  
Validation vocabulary of the same draft: https://json-schema.org/draft/2020-12/json-schema-validation

Episode, edge, and answer documents declare the 2020-12 dialect. `schema_guard` audits each schema before use. Unknown keywords fail that audit. During instance validation the same keywords are annotations. `format` is recorded and not asserted; application code parses timestamps as instants. `$comment` is recorded with `executed: false` and does not waive `required`. Objects used here set `additionalProperties` to false. The answer schema is a `oneOf` of `resolved`, `abstain`, and `unresolved`, and exactly one branch may match. Tuple logs use `prefixItems` with `items: false`. Detailed output includes `valid`, `keywordLocation`, and `instanceLocation`. Recursion past 32 levels fails the audit. Provenance on an edge requires session id, episode id, and the event and write timestamps, with serial allowed to be null when no serial was stated. `test_schema_guard` exercises the audit, the annotation behavior, overlapping `oneOf`, prefix items, format, `$comment`, boolean-versus-integer, and the depth bound.

## 8. Four blocking risks and a release gate

**Artificial Intelligence Risk Management Framework: Generative Artificial Intelligence Profile (NIST AI 600-1)**  
https://doi.org/10.6028/NIST.AI.600-1

The gate table uses the Govern, Map, Measure, and Manage functions. Four risks can block the claim: confabulation, information integrity, data privacy, and information security. A failed gate stays in `failures` and sets `claim_blocked`. The Manage row records that failures remain listed. It is a bookkeeping hook, and this lab does not treat the profile as a conformance certificate.

Confabulation: a never-stated question abstains with reason `never_stated`, and a resolved answer must cite stored episode ids. Integrity: a closed edge keeps its validity interval and the historical read uses that interval. Privacy: retrieval filters to the session before ranking, pins are prefixed by session id, and `session_delete` removes episodes and edges. A secret that exists only in another session is absent from the answer and from the visible pins. A `user_scope` episode is shared only when the caller asks for user scope and the user id matches. Information security: an episode that says to ignore instructions, reveal another session, or change the action is stored and quoted. The router and the action selector read the FACT lines and the conflict policy. They do not take the branch or the action from that text. The security gate is differential: the bundled shift is rerun with every `INSTR` line replaced by filler of the same token length, and the answers and the full tool-call log must match. `test_report` checks a clean report, forced privacy and confabulation failures, the injection differential, and a forged answer that the differential catches.
