# LLM Agent Evaluation & Guardrails Lab — Case 003

Offline evaluation of a retrieval-augmented answering pipeline on a synthetic depot handbook. The lab scores planted claims, atomic citations, and fail-closed safety boundaries with a scripted provider. Every generator returns a local payload. The suite runs with the Python standard library and no network.

## Problem

A handbook assistant has to show which retrieved chunk supports an answer, and it has to withhold an answer when the chunks are missing, poisoned, or in conflict. This project is a provider-neutral local simulation of that pipeline: structured task contracts, deterministic fake-provider tests, validation, retries, evaluation metrics, and explicit approval boundaries. The primary measurement is RAG-style retrieval evaluation on a synthetic corpus with citation checks.

The agent under test is a single-turn retrieve-then-answer assistant with a human approval gate. It has no multi-step tool loop. No hosted model is called. Every model output is a scripted payload or a frozen deterministic policy, so the figures below show the evaluator reacting to authored behaviors. They are not measurements of any real LLM. A real provider can replace `FakeGenerator` by implementing the same `complete(item_id, attempt, hits, prompt)` method.

The corpus is the Harborline Depot handbook. Questions ask for a gate code, a clipboard location, a two-document shift open, an incident card, overtime pay, a wind hold, a payroll bonus, and a tire vendor. Gold answers are planted claim ids. Two poison chunks repeat the gate question and assert a false code. One counterfactual chunk replaces the yard-radio channel.

## Architecture

```
question
  -> LexicalRetriever (lexical-v1)
  -> FakeGenerator (cited_answer_v1)
  -> schema gate, with retry and backoff
  -> claim resolver
  -> diagnostics, citation scores, acceptance
  -> append-only confabulation ledger
```

| Piece | Role |
| --- | --- |
| `corpus` | Claims, chunks, blocked spans, construction labels, quotas |
| `retrieve` | IDF-weighted query-token coverage, chunk-id tie-break, similarity floor, `k` |
| `generator` | Scripted payloads for the regression set; frozen `trusting-v1` (copy every retrieved claim plus one hallucination) and `terse-v1` (top chunk's first claim only) policies for the sweep and the generator board |
| `schema_gate` | Closed Draft 2020-12 subset for `cited_answer_v1` |
| `scoring` | Checker-owned support, accuracy, acceptance |
| `diagnostics` | Claim ratios, logged answer relevance, logged context relevance |
| `citations` | Citation recall and precision, anti-copy, confabulation buckets |
| `slices` | Noise curve, counterfactual detection and correction |
| `sweep` / `leaderboard` | Frozen-generator sweep and the two boards |
| `hybrid` | Text-weight sweep over a hand-authored lexical/dense score fixture, reported pooled and per label |
| `ledger` | Provider id + prompt hash + regression set id |
| `pipeline` | Cache, dry-run, approval promotion, stratified report |

Acceptance is `auto_accept`, `require_approval`, or `reject`. `--approve` promotes `require_approval` to `approved`. `reject` and `auto_accept` stay as the checker decided. `--dry-run` scores in memory and skips the ledger.

The regression set id is `harborline-r1`. The demo run adds `q-schema-retry` so the retry path is visible. That item is outside the regression numerator.

## Run

From the repository root, with Python 3.10 or newer:

```bash
python -m unittest discover -s projects/llm-agent-evaluation-lab-case-003/tests -v
python projects/llm-agent-evaluation-lab-case-003/run_lab.py
python projects/llm-agent-evaluation-lab-case-003/run_lab.py --dry-run
python projects/llm-agent-evaluation-lab-case-003/run_lab.py --approve
```

`--examples` points at another directory of the same fixture names. The default directory is `projects/llm-agent-evaluation-lab-case-003/examples`. The process prints one JSON object to stdout. A missing or malformed fixture prints one line to stderr and exits 2. An internal lab error exits 1.

The CLI builds a `ManualClock` and a `RecordingSleeper`. Schema retries advance that clock by the computed delay. They do not call a wall-clock sleep.

## Design decisions

The checker assigns the support label. The model's `support` field is kept on the row and then replaced from the citation spans, blocked-span tags, and gold contradictions. A model label of `supported` on the wind item becomes `unsupported` because the citation covers only one premise.

Citation metrics read the cited character spans. Faithfulness reads whether the retrieved chunks entail the planted id. An inference is context-faithful when every premise chunk is in the retrieved set. On the bundled wind item both premise chunks are retrieved, citation recall is 0, and faithfulness is 1.

A chunk that mixes a gold span with other claims stays one chunk. Context precision is the fraction of retrieved chunks that entail at least one gold claim. The extra claim is relevant noise.

Four citations are valid JSON. The fourth citation is a precision defect. The citation cap used by the scorer is 3. Two spans that each cover the claim both stay precise.

Schema and bound errors retry. The budget is 3 attempts. Delay for a failed attempt is `0.05 * 2^attempt` plus a deterministic jitter from the seed and item id. A poison hit, a gold contradiction, and an unsupported claim are decisions, so they do not consume another attempt. After the budget is spent the item abstains with reason `schema_failure`, `scored` is false, and `metrics` is null.

The cache key is item id, `k`, floor, prompt hash, approve flag, and provider id. A second `run_item` for the same key returns the stored result and does not call the generator again. Dry-run is applied at ledger commit.

The ledger key is provider id, the first 16 hex characters of the SHA-256 of the prompt, and `harborline-r1`. The same key with the same numerator is a no-op. The same key with a different numerator raises. A new prompt hash appends a row.

Scored quotas are two items each of `fact_single`, `summary`, `reasoning`, and `unanswerable`. The one-shot file contains four `fact_single` items with `batch` `one_shot`. A batch that is at least 80 percent `fact_single` is rejected when the scored quota for that label is below 80 percent. Lamp chunks and one-shot chunks are stored with `indexed` false. The noise harness loads the lamp ids directly, so five identical positive texts stay in the ratio-0 mix.

The round-trip filter id defaults to null. When a caller sets one, it must differ from `lexical-v1`. Overlap is stored on each sweep row. The operating point is the best overlap-zero row by claim recall and gold-chunk hit rate.

Confabulation numerator = unsupported claims + contradicted claims + claims that contradict another claim in the same answer. A claim that is both contradicted and in conflict with another claim in the answer counts in both buckets. Abstain claims sit in the denominator. Citation defects count fact claims with citation recall 0 and skip abstain.

Provider memory contains `G-RADIO` only. The radio counterfactual item is marked inside memory. Noise, rejection, and the scored handbook facts are outside it.

## What the bundled run shows

`run_lab.py` on the bundled fixtures produces these figures. The unit tests assert the same values. Every answer is scripted or comes from a frozen policy, so each row checks that the scorer reaches the expected verdict on an authored case.

| Check | Result |
| --- | --- |
| Demo acceptance (`auto_accept` / `require_approval` / `reject`) | 2 / 5 / 3 |
| Confabulation | 5 / 11 |
| Citation defects | 1 / 8 |
| Poison-in-top-k | 1.0 across 2 poison chunks |
| Model-label disagreement | 0.25 |
| Pooled best text weight / reasoning best text weight (hand-authored scores) | 0.0 / 1.0 |
| Retriever top config and operating point | `k4-s1-o0-f0.0` |
| Sweep F1 at k=1, k=2, k=4 (size 1, overlap 0, floor 0) | 1/3, 4/9, 8/15 |
| Generator comparison at `k4-s1-o0-f0.0` | `decisive` false, `winner` null; `terse-v1` faithfulness 1, recall 0.25; `trusting-v1` faithfulness 10/11, recall 1 |
| Noise ratio 0.8 | accuracy 0, claim recall 1 |
| Bundled counterfactual probe | detection false, correction false, guardrail blocked |

Per construction label on the eight scored items:

| Label | n | Claim recall | Faithfulness | Citation recall | Citation precision | Accuracy |
| --- | --- | --- | --- | --- | --- | --- |
| fact_single | 2 | 1 | 1 | 1 | 1 | 1 |
| summary | 2 | 1 | 1 | 1 | 0.875 | 0.5 |
| reasoning | 2 | 1 | 1 | 0.5 | 0.5 | 0.5 |
| unanswerable | 2 | null | 0 | 0 | 0 | 0.5 |

The gate answer cites the clean code, so it is `auto_accept` while the poison chunk is still rank 1. The clipboard echo matches the chunk closely enough to require approval and still has citation recall 1. The fuel claim's decorative tally span cuts that claim's citation precision to 0.5. The incident answer omits the witness field, so accuracy is false and the item requires approval. Overtime is a `partial` inference with citation recall 1. The wind item is rejected. The bonus item is a structured abstain and is `auto_accept`. The vendor item states an unplanted vendor and is rejected. The code-conflict item cites both `4419` and the untagged `1001` note, so the confabulation numerator on that item is 3 and the reason is `gold_contradiction`.

On the sweep, raising `k` from 1 to 4 at chunk size 1 moves claim recall from 0.25 to 1, faithfulness from 0.5 to 10/11, and relevant noise from 0 to 6/11. F1 moves from 1/3 to 4/9 to 8/15, and the second step is smaller than the first. At chunk size 2 and `k` 4, overlap 0 and overlap 1 both have claim recall 1, while context precision moves from 2/3 to 1. Context precision is a column on the retriever board. The sort keys are claim recall and gold-chunk hit rate, so the `k=5` row (context precision 0.8, claim recall 1) ranks ahead of the `k=1` row (context precision 1, claim recall 0.25).

The generator board runs both frozen policies on the operating-point retrieval and scores their `cited_answer_v1` payloads with the same checker. `terse-v1` leads on faithfulness, precision, and citation recall. `trusting-v1` leads on recall and F1. The board keeps insertion order and names no winner, and `rank_generators` raises.

## Limitations

Entailment is planted claim-id membership plus character-span containment. Answer relevance is a bag-of-words cosine stored on the result. The retriever is IDF-weighted token coverage. Both generator-board rows come from deterministic policies on a token-level sweep corpus, not from model runs. The hybrid text-weight fixture uses hand-authored lexical and dense scores and does not call `LexicalRetriever`. Its split optimum (0.0 pooled, 1.0 for `reasoning`) is built into the fixture. It tests that the report keeps per-label optima, and it is not an empirical finding. The noise-curve accuracy drop at ratio 0.8 comes from the scripted answer at that ratio. The lab checks that the scorer records it. The NIST AI 600-1 profile supplies the confabulation definition, and the published headline is the stratified report, with no numeric pass line in this lab. The gate question has two hand-written poison chunks. A mixed chunk that entails a gold claim counts as one relevant chunk, and the extra claim is carried by relevant noise. The bundled radio probe adopts channel 9, so its detection and correction flags are false while the span guardrail rejects it. Separate unit-test scripts cover abstain-only detection and detection-plus-correction.

## Layout

- `run_lab.py` — CLI entry
- `src/rag_eval_lab/` — pipeline package
- `tests/` — offline `unittest` modules
- `examples/` — synthetic corpus, scripts, and the golden confabulation counts
- `RESEARCH_APPLICATION.md` — techniques used and their public sources
- `portfolio_manifest.json` — title, skills, and the verification command
