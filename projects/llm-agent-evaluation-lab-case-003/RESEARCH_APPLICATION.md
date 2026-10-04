# Research application

Nine public sources shape this lab. Each technique below is implemented in `src/rag_eval_lab` and locked by `tests/`. The checker uses planted claim ids and character spans. It does not call a hosted model. Generator outputs are scripted payloads or frozen deterministic policies, so the tests show that the evaluator reaches the right verdict on authored cases. None of the published figures from these papers is reproduced here.

## Planted-claim diagnostics

Source: Ru et al., "RAGChecker: A Fine-grained Framework for Diagnosing Retrieval-Augmented Generation", https://arxiv.org/abs/2408.08067

`diagnostics.diagnose` reports claim precision, claim recall, F1, retriever claim recall, chunk-level context precision, faithfulness, relevant-noise, irrelevant-noise, hallucination, self-knowledge, and context utilization. A retrieved chunk stays one chunk when it mixes a gold span with extra claims, so context precision can stay at 1 while relevant-noise records the extra claim. An empty retrieval sets claim recall and context precision to 0, and a correct claim with no entailing chunk is self-knowledge. The metric sweep runs the frozen `TrustingGenerator` (copy every planted claim in the retrieved chunks, plus one unplanted hallucination). It validates each payload against `cited_answer_v1` and moves only `k`, chunk size, overlap, and the similarity floor. Tests check the hand ratios and the sweep fractions, including the rise in claim recall, faithfulness, and noise as the window grows, and the smaller F1 gain from `k=2` to `k=4` than from `k=1` to `k=2`.

## Atomic citations

Source: Gao et al., "Enabling Large Language Models to Generate Text with Citations", https://arxiv.org/abs/2305.14627

`citations.score_citations` sets citation recall to 1 only when at least one citation exists and the concatenation of the spans entails the claim. A span that does not entail the claim alone is irrelevant when the remaining citations still do, and that span's precision is 0. Recall 0 forces every citation precision to 0. A fourth citation is a precision defect; the cap is 3. Two spans that each fully support the claim both stay precise. An inference whose premises are all covered is `partial`. `supported` or `partial` with an empty citation list becomes `abstain` and increments `claim_schema_defects`. The anti-copy check flags a surface that reproduces most of a retrieved chunk and leaves a short extract alone, which replaces a distributional fluency score for this offline fixture. Tests cover the decorative dock-tally span, the clipboard echo, the four-citation defect, and the empty-citation abstain.

## Unplanted statements

Source: Es et al., "Ragas: Automated Evaluation of Retrieval Augmented Generation", https://arxiv.org/abs/2309.15217

A response claim with a null planted id carries a fallback object: an explanation and a yes/no verdict, optionally tied to a chunk id. A yes verdict whose chunk was retrieved can count as faithful or as noise. It never joins the gold set, so it cannot raise response recall or retriever claim recall. A no verdict, or a mix of yes and no, is unsupported and can lower precision and raise hallucination. Answer relevance is a bag-of-words cosine between the question and the scripted reverse questions. Context relevance is the share of retrieved sentences that cover a gold span, and it is 0 when the item abstains. Both values are logged. `decide_acceptance` does not take them as arguments. Tests show a high restatement cosine beside an F1 of 0 and a reject, a yes-only fallback that leaves gold recall at 0, context relevance of 0.5 when one of two retrieved sentences covers the gold span, and 0 on a structured abstain.

## Construction labels and one weight per task

Source: "Know Your RAG: Dataset Taxonomy and Generation Strategies for Evaluating RAG Systems", https://arxiv.org/abs/2411.19710

Scored items use fixed quotas: two `fact_single`, two `summary`, two `reasoning`, and two `unanswerable`. Statement-first traces record theme, factual statements, three summary statements, three conclusion statements, and the sampled answer. The loader checks each trace: a `fact_single` sample must be one stored span, a `summary` needs two or more, and a `reasoning` sample must be a conclusion that is not a stored span. Unanswerable items carry no trace because they are built from topical negatives. The one-shot control is four `fact_single` items, and `reject_if_collapsed` refuses to treat that batch as the scored set. `require_stratified` fails a report that omits a label. The model label is stored only as a disagreement rate; scoring uses the construction label. The text-weight sweep reports the pooled best weight and the reasoning best weight. Its lexical and dense scores are hand-authored, so the 0.0 and 1.0 optima are built into the fixture. That checks the reporting, not retrieval behavior. Tests lock the quotas, a quota-manifest mismatch, malformed statement-first traces, the collapsed-batch rejection, the 0.25 disagreement rate, and the split optima.

## Noise, rejection, and integration

Source: Chen et al., "Benchmarking Large Language Models in Retrieval-Augmented Generation", https://arxiv.org/abs/2309.01431

The lamp slice mixes five documents at noise ratios 0, 0.2, 0.4, 0.6, and 0.8. Negatives are topical lamp sentences, and the ratio 0.8 point keeps claim recall at 1 while accuracy falls to 0 because the scripted answer cites the wall sentence. Rejection success is one `insufficient_evidence` claim with support `abstain` and no citations. The canonical refusal sentence is logged with `surface_refusal_match` and is not required for that success. Information integration requires every premise chunk in the retrieved set; overtime with only the hours chunk has claim recall 0, and a fuel claim cited to the tally span has citation recall 0. Noise, rejection, and integration facts sit outside provider memory. The yard-radio fact is inside it. Tests cover the curve, the refusal wording, the missing premise, the mis-bound citation, and the memory split.

## Synthetic rank is not a generator winner

Source: "Can we Evaluate RAGs with Synthetic Data?", https://arxiv.org/abs/2508.11758

The retriever board sorts by claim recall and gold-chunk hit rate. Context precision is a diagnostic column. `rank_retriever` sets `decisive` true. The generator board runs two frozen policies, `terse-v1` and `trusting-v1`, on the operating-point retrieval with one checker. `generator_board` keeps insertion order, sets `decisive` false, and sets `winner` to null. `rank_generators` raises. On the fixture, `terse-v1` leads on faithfulness and `trusting-v1` leads on recall. The operating point is chosen on the overlap-zero rows, so overlap is reported and is not a tuning target. A round-trip filter id must differ from `lexical-v1`; the default filter is off. The lab has no human-written question set, so it does not compute a synthetic-versus-human score gap. It handles that limit by keeping the generator comparison non-decisive. Tests check that `k=5` outranks `k=1` despite a lower context precision, that overlap 0 and overlap 1 at chunk size 2 can share claim recall while context precision moves, and that a higher-recall overlap row cannot become the operating point.

## Poison and counterfactual spans

Source: Zou et al., "PoisonedRAG: Knowledge Corruption Attacks to Retrieval-Augmented Generation of Large Language Models", https://arxiv.org/abs/2402.07867

The gate question has two hand-written poison chunks. That is the whole poison set for the question, and `poison_chunk_count` records 2. Each chunk repeats the question and adds a false code. Poison-in-top-k is computed separately from accuracy: the default answer cites the clean span, stays accurate, and still records the poison hit. A citation that overlaps a poison or counterfactual span is `contradicted` and fail-closed, including when that chunk is rank 1. An untagged span that contradicts a gold id is also `contradicted`, with reason `gold_contradiction`. Error detection means the model does not mark the false claim supported and does abstain or mark the document contradicted. Error correction also states the planted true claim. The bundled radio probe adopts the false channel, so detection and correction are both false while the span guardrail blocks the answer. The acceptance function does not take a perplexity threshold, a query paraphrase, or a duplicate-text filter, and both poison chunks stay in the index. Tests lock rank-1 poison, the cited-span reject, the adopt probe, an abstain-only detection, and a correction that remains fail-closed because the false span is cited.

## Confabulation ledger

Source: NIST, "Artificial Intelligence Risk Management Framework: Generative Artificial Intelligence Profile" (AI 600-1), https://doi.org/10.6028/NIST.AI.600-1

The confabulation numerator is the sum of three buckets: unsupported claims, contradicted claims, and claims that contradict another claim in the same answer. One claim can count in two buckets. Abstain claims stay in the denominator. The citation-defect rate counts fact claims whose citation recall is 0 and excludes abstain. The Harborline regression stores 5/11 and 1/8 under the provider id and the prompt hash. The same key must reproduce those integers. A new prompt hash appends a row. The headline is the stratified report. This lab sets no numeric pass line. Tests match the golden numerator and show the idempotent key.

## Closed response schema

Source: "JSON Schema Validation: A Vocabulary for Structural Validation of JSON (draft 2020-12)", https://json-schema.org/draft/2020-12/json-schema-validation

`cited_answer_v1` declares `$schema` as `https://json-schema.org/draft/2020-12/schema`. Objects set `additionalProperties` false, list every field in `required`, and set `minProperties` and `maxProperties` to the field count. Offsets are integers with minimum 0. `format` is not an assertion in this validator. `end >= start`, unknown chunk ids, and end-past-chunk are checked in code. A schema failure retries on a fixed budget with deterministic backoff, then abstains with reason `schema_failure`. That item is unscored: metrics are null, and it receives no faithfulness, citation-recall, or accuracy credit. The same reject disposition covers insufficient evidence that is not a clean abstain, a cited poison or counterfactual span, and an internal contradiction. The nested Ragas fallback object and its statements use the same closed-object pattern. Tests cover extra keys, a missing key, a bad enum, `start: -1`, an extra key inside the nested fallback, the ignored format keyword, end-before-start, a two-attempt recovery, and a three-attempt unscored abstain whose delays increase.
