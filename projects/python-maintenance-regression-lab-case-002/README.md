# Python Maintenance & Regression Lab — Case 002

An offline maintenance harness for a frozen lane-card dialect called SLIP. Dock gates still emit pipe-separated lane records. The replacement parser has to keep that behavior except where a ledger row records a decision. The lab classifies every corpus blob with the legacy parser and the hardened parser, then refuses a change that lacks a disposition, breaks a metamorphic relation, or drifts from a pinned value.

Inputs are synthetic. Nothing in this project calls a network service.

## Problem

A parser change is hard to review when the only signal is "the sample file still loads":

- doubled quotes inside a quoted field used to mean "close the quote"; the maintained parser treats `""` as one embedded quote
- a trailing `|` used to drop the last empty field; the dialect keeps it
- a NUL byte or a carriage return used to crash the legacy process; the maintained parser returns a stable error code
- a record that starts with `#` is still dropped by the maintained parser even though `#GATE` is a legal lane id
- a successful row can hide a split bug until a neighboring field boundary is parsed
- a generated failure is too noisy to commit until records, fields, and characters that do not affect the outcome are removed
- operators need INFO on stdout, crashes on stderr, and hashes only in the run file

The suite is the maintenance contract for those decisions.

## Architecture

```text
corpus bytes
  -> dialect (records, error codes, quoter, validity)
  -> legacy parser (frozen policy) and hardened parser
  -> in-process classify, or isolate both in child processes
  -> one outcome: CRASH | HANG | DISAGREE | AGREE
  -> JSONL ledger keyed by SHA-256, with a disposition on every non-agreement
  -> metamorphic follow-ups on successful record lists
  -> grammar splice of historical fragments, then a shortlex shrink
  -> candidate cap before anything new is written
```

Package `src/slip_lab`:

- `engine.py` — shared scanner. Legacy and hardened pass a frozen `Policy`
- `legacy.py` — frozen entry point. Its flags are the compatibility baseline
- `hardened.py` — maintained parser. It still strips a leading `#` record (`SLIP-005`)
- `dialect.py` — quoting, validity, permutation rule, separator-neighbor relation
- `isolate.py` — child processes, ready handshake, timeout, output cap, child-exit handling
- `differential.py` — paired outcome. Crashes outrank hangs. Truncation skips equality. A value outside the contract is a crash
- `ledger.py` / `gate.py` — JSONL ledger and the disposition check
- `metamorphic.py` — quote round-trip, headerless permutation, boundary neighbors
- `grammar.py` / `splice.py` — fragment learning and skeleton splice
- `shrink.py` / `budget.py` — shortlex reduction and the commit cap
- `logging_setup.py` — DEBUG file, INFO/WARNING stdout, ERROR stderr
- `stock.py` — the 16 embedded corpus blobs and the publisher for `corpus/`
- `checkrun.py` — read-only stock check and the metrics report

SLIP, in this lab:

- bytes are Latin-1, so every byte is a character and shrinking bytes matches the text
- records are separated by `\n`; a final newline does not create an extra record
- fields are separated by `|`
- a quote opens only at the start of a field; the hardened parser reads `""` inside quotes as one quote character, while legacy closes and reopens. Characters after a closing quote are appended to the same field (`"a"x` is `ax` on both sides); a quote in the middle of an unquoted field is `E_BARE_QUOTE`
- a first field equal to `@slip` on the first record is a header, and data rows must have the same width. Quoting does not escape the mark
- spaces are significant
- a raw newline cannot appear inside a field
- error results are `(code,)` tuples. Extra message text is display-only

## Corpus and dispositions

Sixteen blobs live under `corpus/fixed`, `corpus/lexical`, and `corpus/committed`. `corpus/ledger.jsonl` is the published decision for each SHA-256. The embedded copies are in `stock.py`. A check fails when the files drift from those bytes, when the ledger file differs from a fresh classification, or when a file is missing; the drift is a `gate_problems` entry and exit `1`, not a traceback. The tests compare against the committed corpus and never rewrite it. `stock.publish()` is the hand-run publisher after a reviewed ledger change.

| Id | File | Disposition | Locked behavior |
| --- | --- | --- | --- |
| SLIP-001 | `fixed/quote_escape.slip` | `bug_legacy` | doubled quotes disagree: legacy yields an empty field, hardened yields one quote character |
| SLIP-002 | `fixed/trailing_bar.slip` | `accept_spec` | a trailing separator keeps an empty field; the oracle is the hardened record |
| SLIP-003 | `fixed/nul_lane.slip` | `bug_legacy` | NUL crashes legacy with `RuntimeError`; hardened returns `E_NUL` |
| SLIP-004 | `fixed/cr_lane.slip` | `malformed_probe` | CR crashes legacy; hardened returns `E_CARRIAGE` |
| SLIP-005 | `fixed/hash_lane.slip` | `bug_new` | hardened still drops the `#GATE` row and returns no rows |
| SLIP-006 | `fixed/middle_empty.slip` | `accept_compat` | a middle empty field agrees; hardened drift fails the gate |
| SLIP-007 | `committed/reduced_quote.slip` | `bug_legacy` | four quote bytes, the shortlex reduction of SLIP-001 |

The other nine blobs agree, including shared errors (`E_EMPTY_DOCUMENT`, `E_BARE_QUOTE`, `E_UNCLOSED_QUOTE`, `E_FIELD_COUNT`). An agreement does not need a disposition. `SLIP-006` carries `accept_compat` anyway so the pin is explicit. The lexical layer holds the empty file, a bare quote, an unclosed quote, a short header row, a significant space, and a 180-character lane.

Counted from that corpus: 10 agreements, 4 disagreements, 2 legacy crashes, 0 hangs. Every non-agreement has a label.

Maintenance loop the tests encode:

1. Reproduce the blob (`tests/test_corpus.py` locks the known pairs).
2. Classify in-process, or with `--isolate-one` when a hang must be killable.
3. Put one disposition on the ledger row.
4. Re-run the suite. A class change, an oracle miss, or an unlabeled non-agreement fails the gate.

## Run

From the repository root, Python 3.10 or newer, standard library only:

```text
python -m unittest discover -s projects/python-maintenance-regression-lab-case-002/tests -v
```

The stock check is read-only. Stdout carries one INFO line per case, then one JSON report object. Stderr carries the ERROR lines for crashes:

```text
python projects/python-maintenance-regression-lab-case-002/run_lab.py
python projects/python-maintenance-regression-lab-case-002/run_lab.py --list-defects
python projects/python-maintenance-regression-lab-case-002/run_lab.py --shrink-demo
python projects/python-maintenance-regression-lab-case-002/run_lab.py --blob projects/python-maintenance-regression-lab-case-002/examples/lane_batch.slip
python projects/python-maintenance-regression-lab-case-002/run_lab.py --isolate-one projects/python-maintenance-regression-lab-case-002/examples/lane_batch.slip --timeout 8
python projects/python-maintenance-regression-lab-case-002/run_lab.py --splice --seed 1 --cap 0
python projects/python-maintenance-regression-lab-case-002/run_lab.py --splice --seed 1 --cap 1 --commit --commit-dir <directory>
```

`<directory>` is any writable folder outside the corpus, for example a temp directory. `--commit` is what writes reduced splice bytes. Without it, splice counts overflow and writes nothing. `--cap 0` commits nothing and records every failure as overflow.

A green check reports `outcome_coverage` 1.0, `disposition_coverage` 1.0, and `metamorphic_violations` 0. `untouched_branches` is `["hang_reject"]` because the `__HANG__` token is not in the in-process corpus. `crash_by_side.legacy` is 2. Cases are ordered crash, hang, disagreement, agreement.

`--shrink-demo` reduces `ZZ|9`, the quote row, and `PAD|0` (18 bytes) to the four quote bytes. The ratio is `4/18`.

`examples/lane_batch.slip` is a header batch with a quoted lane id. Both parsers agree on it. The isolate path uses the same file with a child process.

Exit codes: `0` the check or the requested demo finished green, `1` the gate or a metamorphic relation failed, the published corpus drifted, or coverage is short, `2` a request was refused before any parser ran (the log path is not a writable directory, the blob path is missing, `--commit` without `--commit-dir`, a `--commit-dir` inside `corpus/`, `--cap` below 0, `--timeout` not above 0) or the shrinker exceeded its step budget. Failures still print a JSON object. A refused request includes an `error` field.

Logging goes through `dictConfig`. Pass `--log-dir` to choose the run-file directory; otherwise the CLI uses a fresh temp directory. Either way the JSON reports the run file's full path as `log_file`. INFO on stdout is `case`, `outcome`, `side`, and `disposition` (the check, `--blob`, and `--isolate-one` log cases). ERROR on stderr is `exc_type` and `side`. DEBUG in `slip-<run-id>.log` is byte length, SHA-256, parent ids, and the truncation flag. Lane text is not written on the INFO line.

## Design decisions

- **Frozen legacy policy.** `legacy.POLICY` turns doubled-quote escaping off, drops a trailing empty field, crashes on NUL and CR, and sleeps on the exact token `__HANG__`. Maintenance edits go in `hardened.py`, then into the ledger.
- **One outcome, crashes first.** A returned error code is a normal result. An uncaught exception is `CRASH`. A returned value that is not a SLIP value (unknown code, non-string field) is `CRASH` with `InvalidResult`, so a contract break cannot crash the harness itself. A child that misses the timeout is `HANG`. If one side crashes and the other hangs, the outcome is `CRASH`.
- **Timeout after ready.** The child imports the target, then sends `ready`. The timeout covers the parse, so interpreter startup is not stored as a hang. An import failure is a `CRASH` with the exception type. A child that exits without a result (for example `os._exit`) is a `CRASH` with `ProcessExit`, not a harness error. The hang fixture uses `0.2` seconds and is killed inside a 2 second budget. Stock rows store `timeout_s: null` because the default check is in-process.
- **Truncation is not equality.** An output over the byte cap is flagged, the value is discarded, and the outcome cannot be `AGREE`.
- **Dispositions are the compatibility rule.** `accept_compat` must stay an agreement. `accept_spec` must match the stored oracle and must not crash or hang. `bug_new` and `bug_legacy` keep the recorded class and the pinned hardened value until the row is edited. `malformed_probe` records an intentional out-of-dialect input.
- **Idempotent ledger.** Adding the same SHA-256 and the same row is a no-op. The same SHA-256 with a different row raises `LedgerConflict`.
- **Follow-ups do not touch the corpus.** Quote rendering, row order, and the separator neighbor are built in memory. Header rows skip permutation, and so does any row list where a row starts with `@slip`, because moving it to the top would make it a header. Rows whose neighbor would be empty, would contain a quote, a pipe, or a newline, or would put `@slip` first skip the boundary edit, and the skip is counted. A follow-up that crashes or returns a non-SLIP value counts as a violation. `full=False` checks the reversed row order and one adjacent field; the stock check uses the full set. The full set is every order up to five rows, and the rotations plus the reversal above that.
- **Fragments come from the grammar.** The learner does not ask the parser under test to cut the fragment. Valid splices replace one field; the historical quote fragment goes into the first data field, so a header skeleton keeps its header. A nearby edit is labelled `valid` only when the grammar still accepts it. Unsafe learned fragments (NUL, CR, hang token) are not inserted; a remaining learned invalid fragment is tagged `malformed_probe`.
- **Shrink before commit.** Interestingness preserves outcome class, side, and dialect validity. Pass order is record, field, character. The kept bytes are the shortlex minimum of the edits that pass. The quote fixture's local minimum is `""""`.
- **Cap before write.** Overflow candidates are counted and are not reduced. `--commit` writes only under `--commit-dir`.
- **Coverage is a side report.** Hardened branch counts show code the stock corpus never entered. They do not pass or fail the gate.
- **Dry-run by default.** The check, the shrink demo, and splice without `--commit` do not write corpus bytes.

## Limitations

- The default check classifies in-process. It will not kill a hang. The hang token is covered by the isolated harness and is absent from `corpus/`.
- Agreement can hide a bug both parsers share. The relations are a partial check, and a relation that holds is not a correctness proof.
- The reducer is a local shortlex search over record, field, and character deletions. It can stop short of a globally smallest input.
- A timeout that is tighter than the parse becomes a false hang. Isolated outcomes store the timeout that was used.
- Quotes cannot contain a raw newline. The dialect does not detect character encodings, and it does not normalize Unicode.
- The ledger is a JSONL file written by one publisher. It is not a concurrent database.
- The splice cap drops candidates on purpose. The overflow count is the record of what was dropped.
- `--shrink-demo` and the stock check do not start child processes. `--isolate-one` does.
- This is a teaching and portfolio sample of a maintenance gate, not a dock-gate product. There is no live feed, device, or hosted API.

## What it demonstrates

- A frozen legacy parser and a hardened twin with the same return contract
- Differential outcomes for agreement, disagreement, crash, hang, and truncated output
- A disposition ledger that pins compatibility, an intentional spec break, a legacy bug, an open bug, and a malformed probe
- Metamorphic quote, permutation, and separator-neighbor checks that fail silent logic bugs
- Grammar-constrained splice, a malformed-probe slot, and an incomplete-fix replay
- Validity-gated shortlex shrinking and a hard candidate cap
- Level-split logging with an upper bound so ERROR stays off stdout
- Offline `unittest` coverage on synthetic fixtures only
