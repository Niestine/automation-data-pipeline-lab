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

- **[LLM Agent Evaluation & Guardrails Lab](projects/llm-agent-evaluation-lab/)** — Provider-neutral local simulation of an LLM agent pipeline with structured task contracts, deterministic fake-provider tests, validation, retries, evaluation metrics, and explicit safety/approval boundaries.
