# LLM Agent Evaluation & Guardrails Lab

A provider-neutral, fully offline simulation of an internal-operations LLM agent. The lab shows how to take a ticket, demand a structured contract from a model stand-in, validate the JSON, apply safety and approval rules, retry recoverable failures, execute local tools, and score the run against a gold set.

No hosted LLM is called. Completions come from a scripted `FakeProvider` or a deterministic `HeuristicProvider`. All tickets, records, and labels are synthetic.

## Problem

Internal automation agents are asked to look up records, draft summaries, change state, and export data. A free-form completion is not enough:

- the output has to match a schema before any tool runs
- prompt injection, bulk personal-data dumps, and offsite sends have to fail closed
- role and action decide whether a step is auto-allowed, held for approval, or denied
- parse errors and transient provider faults should retry; policy denials should not
- mutating tools must be idempotent across reruns
- quality has to be measured against labeled cases, not a single happy-path demo

This project is a compact, standard-library-only sketch of a loop that handles those concerns. It is a teaching/portfolio sample, not a production service.

## Architecture

```
Ticket
  -> input guardrails (injection, bulk PII, credentials, exfiltration)
  -> idempotent run cache (ticket + contract version + input hash + mode; failed runs are not cached)
  -> provider.complete(system contract, JSON ticket, json_object, temperature 0)
  -> extract / parse / validate agent_output_v1
  -> retry with exponential backoff on parse, schema, and transient errors
  -> output guardrails (credential-like leakage)
  -> approval matrix (role x action, tightened by model needs_human / low confidence)
  -> local tools (lookup, summarize, allowlisted-field update, aggregate export, escalate, refuse)
  -> gold-set evaluation (status, intent, action, approval, schema validity)
```

Package layout:

- `src/llm_agent_lab/contracts.py` — JSON extraction and `agent_output_v1` schema checks
- `src/llm_agent_lab/provider.py` — provider protocol, `FakeProvider`, `HeuristicProvider`
- `src/llm_agent_lab/guardrails.py` — input/output checks and approval boundaries
- `src/llm_agent_lab/retry.py` — retryability and seeded backoff
- `src/llm_agent_lab/tools.py` — synthetic record store and mutating vs dry-run tools
- `src/llm_agent_lab/store.py` — idempotent run store
- `src/llm_agent_lab/orchestrator.py` — pipeline, traces, structured log events
- `src/llm_agent_lab/telemetry.py` — wall/manual clocks, recording sleeper, in-memory event logger
- `src/llm_agent_lab/evaluation.py` — gold labels and aggregate metrics
- `examples/` — synthetic tickets, records, gold labels, and a fake-provider script

## Approval boundaries

| Action           | intern | analyst | admin            |
|------------------|--------|---------|------------------|
| lookup / summarize / escalate / refuse | auto_allow | auto_allow | auto_allow |
| update_record    | deny   | require_approval | require_approval |
| export_data      | deny   | deny    | require_approval |

If the model sets `needs_human: true` or reports `confidence` below `0.6`, an otherwise auto-allowed `lookup_record` / `summarize` is held as `require_approval`. The model's own signals can only tighten policy, never loosen it; `escalate` and `refuse` are never held.

Even an approved export is limited to `aggregate_counts`; a `full` dump is denied by the tool layer. Even an approved update may only touch `status`, `assignee`, or `queue`; other fields are denied as `field_not_updatable`. Dry-run skips mutations; denials and approval holds still surface as `denied` / `pending_approval`.

## Run

From the repository root (Python 3.10+, standard library only, no network access needed):

```text
python -m unittest discover -s projects/llm-agent-evaluation-lab/tests -v
```

Offline demo against the synthetic gold set:

```text
python projects/llm-agent-evaluation-lab/run_lab.py
python projects/llm-agent-evaluation-lab/run_lab.py --provider fake
python projects/llm-agent-evaluation-lab/run_lab.py --dry-run
python projects/llm-agent-evaluation-lab/run_lab.py --approve
```

`--approve` is an explicit human-gate override for `require_approval` actions in this lab. Interns are still denied on updates and exports. The gold labels describe the default (live, unapproved) mode, so `--approve` and `--dry-run` intentionally score below `1.0` on tickets whose expected status changes.

Missing or malformed input files (`--tickets`, `--gold`, `--records`, `--script`) produce a one-line error and exit code `2`.

## Design decisions

- **Contracts over prose.** The orchestrator always requests `response_format=json_object` at temperature `0` and rejects extra fields, bad enums, wrong types, `NaN`/`Infinity`, and out-of-range confidence.
- **Provider-neutral boundary.** Anything implementing `complete(request) -> CompletionResponse` can be plugged in; a hosted-vendor adapter would live behind that interface, but none ships here. Tests inject scripts; the CLI defaults to a keyword heuristic so it stays offline.
- **Fail closed on safety.** Injection, bulk email dumps, credential requests, and external sends never reach the provider.
- **Retry only recoverable faults.** Parse/schema errors and transient provider faults retry with capped exponential backoff and deterministic jitter derived from `seed:ticket_id`. Auth failures, malformed scripts, and policy codes do not retry. Exhausted or permanent failures are not cached, so a later rerun can recover.
- **Idempotency key includes mode.** Live, dry-run, and approved runs are cached separately so a dry-run cannot hide a live mutation, and a repeated approved call in the same process will not update the store twice (`force=True` deliberately bypasses this).
- **Defense in depth on mutations.** Role policy, approval hold, and tool-level checks (export scope, update field allowlist) are independent.
- **Evaluation is a first-class artifact.** The gold set in `examples/gold_labels.json` is the source of truth for the ten synthetic tickets. Schema validity is scored only when a completion was judged (blocked-before-provider runs are N/A), and a test injects regressions into the fake script to show the evaluator catches them.

## Limitations

- There is no live OpenAI, Anthropic, Azure, or local GPU model in this repository.
- The heuristic provider is keyword matching, not an LLM.
- Backoff sleep is injectable. The default `WallClock` really sleeps; the CLI and tests use a recording sleeper that advances a manual clock instead of blocking the process.
- Approval is a boolean CLI flag, not a ticket-queue UI or IAM system.
- PII and injection checks are marker-based, not a full policy engine.
- The record store and run cache are in-memory, so idempotency only holds within one process.
- The perfect scores on the bundled gold set reflect that the heuristic rules and the fake script were written for those ten tickets. They show the harness works; they are not an accuracy benchmark.
- Log events are JSON-serializable dicts collected in memory; there is no log sink or tracing backend.

## What it demonstrates

- Structured outputs with schema validation and markdown-fence extraction
- Agent orchestration with per-run traces and structured log events
- Guardrails, explicit approval boundaries, and a confidence / `needs_human` gate
- Retries, backoff, and non-retryable policy failures
- Idempotent tool execution and dry-run
- Offline evaluation metrics: status, intent, action, approval, schema validity, retry rate
- Deterministic unittest coverage of the example gold set for both providers, plus a regression test showing the evaluator detects a degraded provider
