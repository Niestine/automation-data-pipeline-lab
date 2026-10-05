# Research application

Eight public sources shape this lab. Each technique below is implemented in `src/repair_gate` and locked by `tests/`. The runner uses a recorded JSON cassette and a synthetic Kilnline desk. It calls no hosted model. Published scores from these papers stay with those papers. The table printed by `run_lab.py` is this suite only.

## Declared coverage, empirical coverage, compliance

Source: Geng et al., "Generating Structured Outputs from Language Models: Benchmark and Studies" (JSONSchemaBench), https://arxiv.org/abs/2501.10868

`validator.admit` reports declared coverage for two engines. The strict mask accepts a schema only when every object sets `additionalProperties` to false and `required` equals the property names, and only when the schema avoids `allOf`, `not`, `dependentRequired`, and `if`/`then`/`else`. The closed grammar also refuses `pattern`, `format`, numeric and length bounds, and `uniqueItems`. Empirical coverage is the fraction of `sample: true` rows in `examples/schema_suite.json` that validate with format assertion on. Those rows are hand-written stand-ins for engine output, not samples from a real decoder. Compliance is empirical divided by declared, and it is null when declared coverage is 0. Quality comparisons use the intersection of schemas both engines accept. On the shipped rows, format, pattern, minimum, and uniqueItems are declared by the strict mask and empirically 0.5; composition schemas are declared 0. Their paper's constrained-decoding speedup of about 50% and downstream gain of up to about 4 points, including GSM8K, describe that benchmark. This lab reports the Kilnline coverage table. Tests match every fixture flag and the compliance figures.

## Format success beside the answer leaf

Source: Tam et al., "Let Me Speak Freely? A Study on the Impact of Format Restrictions on Performance of Large Language Models" (EMNLP 2024 Industry), https://arxiv.org/abs/2408.02442

Episodes run one task as `strict`, as `fri` (JSON object, no grammar), and as `nl_to_format` (a trace call, then a bound object). The trace is stored on the result and is absent from later prompts. `format_success` is parse plus schema. `task_exact_match` reads the answer pointer, so a schema failure can still carry the right leaf, and a schema-valid object can carry the wrong label. The default key order is rationale then answer; `answer_swapped` reverses it. The lab does not hardcode a ranking among the three modes. On their reasoning sets, stricter structure scored worse and the JSON-mode bars were the lowest of the conditions they plotted; classification did not show that same drop. Those are their figures. Tests lock the six mode cells and the key order.

## Leaf accuracy, faithfulness, and hardening

Source: "Structured Output Benchmark", https://arxiv.org/abs/2604.25359

`scoring.score_candidate` checks parse validity and schema validity, then path-flattens objects and arrays with concrete indexes. Schema failure zeroes value accuracy and faithfulness before any leaf credit. Value accuracy is the fraction of gold leaves that match exactly. A synonym fails its leaf. An extra array element keeps the gold-based ratio and fails `perfect_response`. Faithfulness is the fraction of predicted strings that occur in the case context. The shipped rows use the occupation pattern from that paper's construction notes: a schema-invalid object with one matching leaf, a schema-valid synonym pair at 0.5, and an extra role beside a one-element gold list. Their reported leaf accuracies of 83.0, 67.2, and 23.7 describe other systems. Tests lock the three Kilnline rows, the boolean-versus-integer type check, and the hardening zero.

## Location, observed value, and admissible alternatives

Source: "Structured Feedback Improves Repair in an LLM Agent Loop" (VeriHarness), https://arxiv.org/abs/2607.14167

The first prompt's repair field is null. After a failure the encoder writes label, JSON Pointer, observed value, and alternatives taken only from the failing keyword. Enum values are capped at 12 in schema order. Bounds are stored as bounds. Pattern, format, and uniqueItems get an empty list and status `unenumerated`. The harness does not read alternatives out of tool text. Four ablations render that record: `raw`, `loc_obs`, `full_prose`, and `full_keyed`. Prose and keyed views carry the same location, observed value, and alternative list. Lane B caps the episode at 4 model calls, which is the call budget they used. Their TextWorld gaps, 44 points and 42 points between raw validator text and full feedback, were measured on command lists with real admissible actions. This cassette does not read the prompt, so the ablation table is the scripted continuation paired with each view: raw patches land outside the enum and then abstain (mean calls 3), location-only abstains (mean calls 2), and the two full views patch onto an enumerated value (mean calls 2). Tests lock the prompt fields and those rates.

## Masked JSON Patch or abstain

Source: "ContractRL: Teaching Small Language Models to Repair Tool Calls via Contract-Constrained Reinforcement Learning", https://arxiv.org/abs/2610.00328

The repair action is `{"action": "abstain"}` or an RFC 6902 array. The mask checks shape, then the operation allowlist (`move` and `copy` disabled unless enabled), pointer syntax, move prefix, immutable paths, a `test` immediately before each `replace` or `remove`, and the non-test operation budget. A mask or apply failure leaves the stored candidate unchanged, spends one attempt, and keeps the previous location and observed value while the label becomes `mask.<reason>` or `apply.<reason>` and the alternatives are cleared. Abstain spends an attempt and does not execute. Refusal does not spend an attempt and is never turned into a tool call. Lane A sets the patch cap at 2, with the model-call cap relaxed, so two missing-test patches stop the episode while the currency is still `USD`. Exact-patch match and final-object match are separate scores. Collateral is the lab's own definition, because the packet does not give ContractRL's diff: changed leaves outside the non-test op paths, with LCS alignment on arrays, and for a regeneration only the failing location it was asked to fix is exempt. Their reported semantic success of 0.9362 is a trained-policy figure on their tasks. Tests cover the patch-suite reasons, the noisy verifier's `test_mismatch`, lane A, and the patch-versus-regeneration token and collateral split.

## JSON Patch apply

Source: Bryan and Nottingham, "JavaScript Object Notation (JSON) Patch" (RFC 6902), https://www.rfc-editor.org/rfc/rfc6902

`patch.apply_patch` implements `add`, `remove`, `replace`, `move`, `copy`, and `test` on a deep copy. The first error returns the original object. `~1` is decoded before `~0`. The token `-` appends to an array and is not a valid index for evaluation. A test of the string `"10"` against the number `10` is a mismatch and the following replace does not run. The number `1` matches `1.0`. A boolean does not match a number. Extra members besides `op`, `path`, `from`, and `value` are ignored and recorded. Tests run `examples/patch_suite.json` plus the equality cases.

## Utility under attack, from tool results

Source: Debenedetti et al., "AgentDojo: A Dynamic Environment to Evaluate Prompt Injection Attacks and Defenses for LLM Agents", https://arxiv.org/abs/2406.13352

A security case is a user task crossed with an injection task. User tasks also run with no injection. The injection string is placed in `tool_result`, which the prompt carries, and the success predicates read the ledger rather than an LLM judge. The product is 2 user tasks × (benign + 2 injections) × 3 defenses. `mask` keeps benign utility at 1 and targeted attack success at 0. `block_all` never executes, so benign utility and attack success are both 0. `skip_mask` is the baseline that applies the attack patch: targeted attack success is 1 and utility under attack stays 0. Unsafe dispatch on `mask` and `block_all` is 0; it is an after-the-fact audit of each execution, and a test drives the audit directly so a dispatcher regression would show up. Separate tests put a schema-valid call to an outside mailbox through the recipient policy and a regenerated `case_id` through the identity pin, and each one executes only under `skip_mask`. Their suite is 97 tasks and 629 security cases, with attack success under 25% for the best agents they measured and about 8% for a detector defense they discuss as a utility tradeoff. This lab reports the 18 Kilnline cells. Tests match the three rate triples and check that the bait phrase in the tool result is absent from encoder alternatives.

## Strict objects and a detectable refusal

Source: OpenAI, "Structured model outputs", https://developers.openai.com/api/docs/guides/structured-outputs

Admitted objects set `additionalProperties` to false and list every property in `required`. Absence is a nullable union in the schema as authored; the admitter does not rewrite a noncompliant schema into that shape, it rejects it. A completion whose status is `refusal` stops the episode, including when the refusal body looks like a tool call. `json_schema`-style strict mode and instruction-only JSON mode are the `strict` and `fri` channels above. Pattern, format, and bounds remain on the local validator: the harness turns format assertion on, and `validate()` leaves it off so an unknown or unchecked format stays an annotation. Tests cover a refusal with zero patch attempts and zero executions, open objects (including a bare `type: object` and an open `$ref` target) that fail admission, and format assertion in both positions.
