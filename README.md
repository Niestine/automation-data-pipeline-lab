# Automation Data Pipeline Lab

A small, reproducible portfolio project demonstrating an AI-assisted data workflow.

## What it demonstrates

- Python data normalization and validation
- duplicate and missing-value handling
- deterministic JSON/CSV output
- simple aggregation
- Google Apps Script example for writing normalized rows to Google Sheets
- Git branching, commits, tags, remote pushes and pull-request workflow

The included data is synthetic. No client data or secrets are stored in this repository.

## Run

Run: python pipeline.py sample_input.json output

The command writes normalized.json, normalized.csv, and summary.json.

## Portfolio Labs

- **[Reliable Automation Job Runner Lab](projects/automation-job-runner-lab/)** — Offline Python automation runner with declarative DAG jobs, windowed scheduling, idempotent execution, retry/backoff, structured logs, atomic checkpoints, leases, and dry-run overlays.

- **[API Integration Reliability Lab](projects/api-integration-reliability-lab/)** — Offline REST/webhook integration lab that paginates a local mock API, retries with backoff, validates schemas, writes idempotently, checkpoints progress, and recovers from mid-sync failure.

- **[LLM Agent Evaluation & Guardrails Lab](projects/llm-agent-evaluation-lab/)** — Provider-neutral local simulation of an LLM agent pipeline with structured task contracts, deterministic fake-provider tests, validation, retries, evaluation metrics, and explicit safety/approval boundaries.
