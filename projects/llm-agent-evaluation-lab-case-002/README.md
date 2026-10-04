# LLM Agent Evaluation & Guardrails Lab — Case 002

A provider-neutral, fully offline **typed tool-routing** lab. A planner emits a multi-step `tool_plan_v1` with per-tool JSON contracts. A router admits or rejects the whole plan against capability allowlists, role, workspace, data classification, argument bindings, sequence rules, and token/step budgets. An executor resolves `$step.path` bindings, retries transient tool faults, trips a per-tool circuit breaker, and scores the run on routing precision, recall, and sequence match.

No hosted LLM is called. Plans come from a scripted `FakePlanner` or a deterministic `HeuristicPlanner`. Packets, catalog assets, notes, and calendar slots are synthetic.

This case is a different pipeline from the single-action ticket lab in `projects/llm-agent-evaluation-lab/`. That lab classifies one ticket into one tool. This lab plans an ordered tool graph, binds data between steps, and evaluates routing quality.

## Problem

Knowledge and content-ops desks do not hand an agent a single verb. A work packet such as "draft a recap from AST-101, cite the style guide, submit for review" is a **route** across typed tools:

- the planner output has to be a versioned plan schema, not free-form prose
- each tool has its own argument contract; binds from earlier payloads have to satisfy that contract
- a viewer must not compose; a restricted workspace must not publish; a public workspace must not read a restricted asset
- `publish.queue` without an earlier `review.submit` in the same plan is a policy failure, even for a publisher
- parse errors and transient tool I/O should retry; role and classification denials should not
- a flaky catalog should open a circuit so later packets fail fast
- mutating tools must be idempotent (same draft id, same slot hold, same publish queue entry)
- operators need a dry-run that still executes reads and returns predicted write payloads without committing them
- quality is routing quality: tool-set precision/recall and ordered sequence against a gold plan, not a single happy-path demo

This project is a compact, standard-library-only sketch of that loop. It is a teaching/portfolio sample, not a production router.

## Architecture

```
Work packet (goal, role, workspace, max_steps, token_budget)
  -> input guardrails (injection, credentials, empty goal, external send)
  -> idempotent run cache (packet + contract version + input hash + dry/live + auto/approved)
  -> planner.complete(system contract, JSON packet, json_object, temperature 0)
  -> extract / parse / validate tool_plan_v1
  -> retry with exponential backoff on parse, schema, and transient planner faults
  -> output guardrail (credential-like text in rationale or step args)
  -> plan-level admission (unknown tool, capability allowlist, role, workspace,
       classification of static ids, unknown sources, bind forward-refs,
       draft provenance, arg/bind overlap, duplicate ids,
       forbidden sequences, step limit, token budget)
  -> plan-level approval (write/sensitive tools + low confidence or needs_human)
  -> sequential executor
       resolve $sN.path binds from prior payloads
       validate merged args against the per-tool schema
       runtime classification check on bound ids
       search hits filtered to the packet workspace clearance
       per-tool retry + circuit breaker (transient faults only)
       step ledger replay on identical args
  -> gold-set evaluation (status, goal_kind, tool precision/recall, sequence, schema)
```

Package layout:

- `src/brief_router_lab/contracts.py` — JSON extraction and `tool_plan_v1` plus a small schema checker (`pattern`, `minItems`, finite numbers)
- `src/brief_router_lab/registry.py` — typed tool specs: argument schema, token cost, min role, allowed workspaces, side effect
- `src/brief_router_lab/policy.py` — plan admission and a confidence / `needs_human` gate that only applies to write/sensitive tools
- `src/brief_router_lab/bindings.py` — `$s1.hits[0].asset_id` resolver
- `src/brief_router_lab/provider.py` — planner protocol, `FakePlanner`, `HeuristicPlanner`
- `src/brief_router_lab/workspace.py` — synthetic catalog, notes, drafts, calendar, publish queue, optional tool-fault script
- `src/brief_router_lab/retry.py` — retryability, seeded backoff, per-tool circuit breaker
- `src/brief_router_lab/store.py` — packet cache and step ledger
- `src/brief_router_lab/orchestrator.py` — `BriefRouter` pipeline, traces, structured log events
- `src/brief_router_lab/evaluation.py` — gold labels and routing metrics
- `examples/` — synthetic packets, workspace, gold labels, planner script, empty fault script

## Admission boundaries

| Tool | Side effect | Min role | Allowed workspaces |
|------|-------------|----------|--------------------|
| `catalog.search` / `catalog.get` / `kb.search` / `kb.get` | read | viewer | public, internal, restricted |
| `draft.compose` / `draft.cite` / `review.submit` / `calendar.hold` | write | editor | internal, restricted |
| `publish.queue` | sensitive | publisher | internal |
| `refuse` | none | viewer | public, internal, restricted |

Reads are further limited by classification: a public workspace may fetch public assets only; internal may fetch public and internal; restricted may fetch all three. `catalog.search` and `kb.search` silently omit hits above the workspace clearance, so a public search never returns a restricted title. Source ids on `draft.compose` use the same rule and must name a catalog asset; an unknown source is denied as `unknown_source`. Workspace records without a valid `classification` are rejected at load time rather than defaulting to public.

`draft.cite`, `review.submit`, `publish.queue`, and `calendar.hold` must bind `draft_id` from an earlier `draft.compose` step in the same plan (`draft_provenance`). A static `draft_id` is denied, so a plan cannot submit or publish a draft that another packet composed in a different workspace. Re-composing an existing draft id with different content fails with `draft_conflict` instead of silently replaying the old draft.

Goal-kind allowlists stop cross-capability routing: a `lookup` plan cannot call `calendar.hold`. Sequence rules require `review.submit` before `publish.queue`, and `draft.compose` before `draft.cite` or `calendar.hold`.

If the planner sets `needs_human: true` or reports `confidence` below `0.55`, a plan that contains any write/sensitive tool is held as `require_approval`. Read-only plans stay `auto_allow`. The model's own signals can only hold writes; they never loosen role, workspace, or classification denials. Admission is plan-level: a viewer compose plan is denied before any step runs.

Dry-run still executes reads against the catalog. Writes return predicted payloads (`would_mutate: true`) and skip durable drafts, holds, and the publish queue. `--approve` releases `require_approval` plans; role and workspace denials still stand. None of the ten bundled packets triggers the approval gate, so `--approve` does not change the bundled report; the gate is covered by unit tests.

## Run

From the repository root (Python 3.10+, standard library only, no network access needed):

```text
python -m unittest discover -s projects/llm-agent-evaluation-lab-case-002/tests -v
```

Offline demo against the synthetic gold set:

```text
python projects/llm-agent-evaluation-lab-case-002/run_lab.py
python projects/llm-agent-evaluation-lab-case-002/run_lab.py --provider fake
python projects/llm-agent-evaluation-lab-case-002/run_lab.py --dry-run
python projects/llm-agent-evaluation-lab-case-002/run_lab.py --approve
```

The gold labels describe the default (live, unapproved) mode, so `--dry-run` changes status on packets that would otherwise complete.

Missing or malformed input files (`--packets`, `--gold`, `--workspace`, `--script`) produce a one-line error and exit code `2`.

## Design decisions

- **A plan is a typed graph, not a single action.** `tool_plan_v1` carries ordered steps, per-step `args`, and `bind` maps. Tool names are validated by the registry after parse so an `external.send` hallucination is an `unknown_tool` routing failure, not a schema miss.
- **Capability matching is explicit.** Each `goal_kind` has an allowlist. That is the routing contract the evaluator scores.
- **Binds are dataflow.** `$s1.hits[0].asset_id` is checked for forward references at admission and resolved against prior payloads at execution. Empty hits fail closed with `bind_path` and are not cached.
- **Fail closed before the first mutation.** Role, workspace, classification, sequence, budget, and unknown tools deny the whole plan. Partial compose-then-deny does not happen.
- **Retry only recoverable faults.** Planner parse/schema errors and transient provider/tool I/O retry with capped exponential backoff and jitter derived from `seed:packet_id`. Policy codes, bind errors, and auth failures do not. Exhausted failures are not cached.
- **Circuit breaker is per tool.** Three consecutive transient faults open the tool; later packets fail with `circuit_open` until cooldown elapses. Business outcomes such as `slot_taken` or `not_found` do not count toward the breaker.
- **Idempotency has two layers.** Packet cache keys include dry/live and auto/approved. The step ledger keys packet id, packet input hash (which covers role and workspace), step id, canonical args, and mode. Compose uses a stable `DRF-{packet_id}`; calendar holds and publish queue entries replay when the same draft already owns them.
- **Evaluation is routing-first.** Gold cases score status, goal_kind, tool-set precision and recall, ordered sequence, schema validity, and expected error codes. A test mutates the fake script to prove the evaluator detects a wrong route.

## Limitations

- There is no live OpenAI, Anthropic, Azure, or local GPU model in this repository.
- The heuristic planner is keyword matching, not an LLM.
- Backoff sleep is injectable. The default `WallClock` really sleeps; the CLI and tests use a recording sleeper that advances a manual clock instead of blocking the process.
- Approval is a boolean CLI flag, not a ticket-queue UI or IAM system.
- Classification and injection checks are marker- and rank-based, not a full policy engine. The credential markers are substring matches and can false-positive on harmless text such as "api key rotation".
- The workspace and caches are in-memory, so idempotency only holds within one process.
- There is no compensating transaction if a later step fails after an earlier write; fail-stop leaves the draft in place and the packet is not cached so a rerun can replay idempotent writes.
- The perfect scores on the bundled gold set reflect that the heuristic rules and the fake script were written for those ten packets. They show the harness works; they are not an accuracy benchmark.
- Log events are JSON-serializable dicts collected in memory; there is no log sink or tracing backend.

## What it demonstrates

- Structured multi-step plans with per-tool JSON contracts and markdown-fence extraction
- Tool routing with capability allowlists, role/workspace gates, and classification
- Runtime argument binding across steps
- Guardrails, plan-level approval for low-confidence writes, and dry-run predicted writes
- Planner retries, per-tool retries, backoff, and a circuit breaker
- Packet and step idempotency
- Offline evaluation metrics: status, goal_kind, tool precision/recall, sequence, schema validity, budget-ok rate
- Deterministic unittest coverage of the example gold set for both planners, plus a regression test showing the evaluator detects a degraded route
