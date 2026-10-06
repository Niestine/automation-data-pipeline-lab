# Google Sheets / Apps Script Automation Lab

Offline lab for a synthetic course-desk session sheet. Each row is a work session with an ISO date, a desk code, hours, a JPY-per-hour rate, and a JPY amount. The Python core checks the row, computes the week total, and plans one `spreadsheets.batchUpdate`-shaped commit. An Apps Script file shows the lock and the operation-id column. Nothing in this project calls Google or reads a credential.

## Problem

A sheet can look cell-valid and still carry a wrong week total: every session amount equals hours times rate, and the stored total is only the first row. Retrying an append duplicates the row. A loop of separate writes can keep the valid write when a sibling request is invalid. Two writers that both read the same row version can overwrite each other, and a script that reads a counter before it holds a lock can issue the same next value twice.

This lab separates those failures. Per-cell checks, declared dimensions, an independent total, a shallow staging formula, an operation-id ledger, and a validate-then-flush batch each catch a different one. The baselines next to them show the cases they do not catch.

## Architecture

```
examples/week_sessions.json
        |
        v
schema.py + dimensions.py     per-cell type, sign, and declared unit
transform.py                  hours * rate, then the week total
invariants.py                 literal expected total and row count
formula_budget.py             local gates on the staging formula only
idempotency.py + retry.py     operation id, fingerprint, replay, conflict
plan.py + lockstub.py         one batch, row version, flush, script lock
commit.py                     dry-run or commit
gas/Code.gs                   lock example, not executed
```

Data cells are planned as `RAW` (operation ids, ISO dates, desk codes, fingerprints, and already-typed numbers). The only `USER_ENTERED` cell is the staging formula, `=SUM(F2:F4)` for the sample week. It sums every stored session amount and sits on the row below the last stored session, so a later commit that adds one session moves it down instead of overwriting a stored amount. The formula is outside the operation fingerprint. A local stub shows what a UI parse can do to a date or a leading-zero string. The stub is not the Sheets parser. `SpreadsheetApp.setValues` has no `RAW` option, so the Apps Script example sets the plain-text format `@` on the text columns before it writes them.

`row_version` is a local stand-in for a collaborator changing the sheet after a read. It is not an HTTP `If-Match` precondition.

## Run

From the repository root:

```text
python -m unittest discover -s projects/sheets-gas-automation-lab/tests -v
```

Dry-run the sample week, and print the control counts the tests exercise:

```text
python projects/sheets-gas-automation-lab/run_lab.py --dry-run
python projects/sheets-gas-automation-lab/run_lab.py --measure
```

The sample week is three synthetic sessions. The fixture literal `15500` is `10000 + 3500 + 2000`. No package install is required. The code uses the Python standard library.

## Design decisions

- Column units are declared (`hour`, `JPY/hour`, `JPY`, `date`, dimensionless). The header word `Amount` does not become JPY.
- Addition and ordered comparison require the same dimension, including the same conversion factor. Multiplication combines exponents, so hours times JPY/hour is JPY. Fahrenheit-to-Celsius is rejected because a pure factor has no offset.
- The week total lives in `transform.group_total`. The checker in `invariants.py` compares that result, or a seeded wrong total, with a number stored in the fixture. It does not recompute the expected total.
- Staging formulas must have parse-tree height at most 2, at most one reference group, and no conditional function. Those ceilings are local policy. The raw count of cells inside one range is measured and is not a gate. A separate local ceiling of 4 applies to a chain of formula cells.
- A retry is the same operation id and the same pre-coercion fingerprint. A second id with the same hours, rate, date, and desk is a second session. Fingerprinting only the business fields would drop it.
- One logical commit is one request list. Invalid requests apply nothing. Staged cells stay invisible until flush. `try_lock` failure writes nothing. Pending cells discarded without flush never become visible. The lock stub's `flush(sheet)` flushes the sheet's pending cells before `release`, the order the Lock page asks for.
- In-progress conflicts are retried with an injected sleeper and the same id and payload. There is no wall-clock sleep.
- Quota and trigger limits are transcribed in `DEPLOYMENT_BOUNDARY.md`. The test checks that the sentences are present. It does not measure remaining quota or fire a trigger.

## Limitations

- No live spreadsheet, Apps Script runtime, or Google account is used. `gas/Code.gs` is an example the tests read as text; its plain-text-format write was not run against Sheets.
- The `USER_ENTERED` stub only rewrites an ISO date, a numeric-looking string, and a leading `=`. It does not reproduce locale rules or the Sheets UI.
- `1900-03-01` serial `61` is derived from the published epoch and a 28-day February 1900. The datetime page prints `2.5` and `33.625`. It does not print `61`.
- Passing the dimension check does not mean the week total is right. Fixture A is the demonstration.
- An oracle that calls the same dropping total as the writer reports a pass. Fixture C is that blind spot. The file literal still fails.
- `JPY * JPY` is allowed by the factor model and is not JPY. A general "valid dimension" predicate is not implemented.
- Ordered comparison uses the addition rule's equal-dimension precondition. The dimension paper's addition section does not state a relational-operator rule.
- The idempotency draft expired on 18 April 2026 and says it is work in progress. Outcome names are local. The HTTP numbers in comments are analogues, not a wire implementation.
- Atomic flush does not roll back a later collaborator edit.
- A chain of eight legal formulas still passes the per-formula budget. Only the separate chain ceiling rejects it.
- Quotas can change without notice. The transcribed numbers are not remaining capacity.
- One `Ledger` is assumed to describe one sheet: ledger row `n` is sheet row `n + 1`. The invariant covers the sessions passed to one `commit_week` call, while the staging formula sums every stored row.
- The Python ledger and the sheet cells are updated in one process. A crash between flush and the ledger finish is not injected. The Apps Script example treats the operation-id column as the record a real script would re-read under the lock.

## What it demonstrates

The suite shows the split between baselines and the checks above.

| Observation | Baseline | This lab |
| --- | --- | --- |
| Fixture A, total is the first row only | Schema passes, 0 schema errors | Invariant fails on the literal `15500` |
| Fixture B, amount text `TBD` | Schema fails | The oracle is not what detects it |
| Fixture C, writer drops the last row | Shared function agrees with itself | File literal `15500` fails |
| `1000` JPY plus `2` hours | Numeric add returns `1002` | Dimension add raises |
| `=SUM(A2:A100)` versus `=SUM(A2,B2)` | 99 references can pass | 2 references in 2 groups fail the range gate |
| Eight height-1 formulas | Each formula passes | Chain length 8 fails the local ceiling of 4; length 4 passes |
| Same id and payload twice | Append stores 2 rows | Ledger stores 1 and replays |
| Two ids, same business fields | Content hash stores 1 | Ledger stores 2 |
| Hash after the date stub | Fingerprint changes | Hash before the stub still replays |
| Invalid request beside a valid write | Separate writes keep 1 cell | Batch keeps 0 cells |
| Second plan still holding version 7 | — | `stale_version`, final version 8, late cell absent |
| Lock already held | — | `not_acquired`, ledger unchanged |
| 1900-03-01 | Leap-year control returns `62` | Epoch conversion returns derived `61` |

`python projects/sheets-gas-automation-lab/run_lab.py --measure` prints those control counts. Published spreadsheet error rates, Spearman coefficients, and header-inference counts are not inputs and are not results of this run. See `RESEARCH_APPLICATION.md`.
