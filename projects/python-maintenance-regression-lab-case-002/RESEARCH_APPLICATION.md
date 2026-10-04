# Research application

Five public sources shape this lab. Each technique below is implemented under `src/slip_lab` and locked by `tests/`. The parsers run on synthetic lane-card bytes. No published bug-yield figure from these papers is reused as a result of this project.

## Paired execution and a recorded decision

Source: William M. McKeeman, "Differential Testing for Software", Digital Technical Journal, 1998. https://www.cs.tufts.edu/comp/150FP/archive/bill-mckeeman/DifferentailTesting.pdf

`differential.combine` runs the frozen legacy parser and the hardened parser on the same bytes. Each side is a structured value, a crash, or a hang. The paired outcome is `CRASH` if either side crashes, otherwise `HANG` if either side does not finish, otherwise `AGREE` or `DISAGREE` by deep equality of the field sequence and the stable error code. Returned error codes are ordinary results. Message strings are not part of equality. A returned value outside the dialect contract counts as a crash of that side (`InvalidResult`). Reports sort crashes, then hangs, then disagreements, then agreements.

`isolate.run_targets` starts the two parsers in child processes. Each child imports its target and then sends `ready`. The timeout starts after `ready`, and a miss terminates the child. A child that exits without a result is a crash (`ProcessExit`). A truncated payload is flagged and is not decoded, so a cut buffer cannot compare equal to a full parse. The planted suite covers agreement, disagreement, a crash on each side, a legacy hang killed inside a 2 second budget, truncation, a child process exit, an import failure, and an invalid return value.

The fixed corpus stays on every check. Generated splice failures go through `budget.apply_cap` before they can be written. Past the cap, candidates are counted as overflow and are not shrunk or stored. That is the bound on an automatic finder: the suite stays red until every non-agreement has one disposition (`bug_new`, `bug_legacy`, `accept_compat`, `accept_spec`, or `malformed_probe`). `accept_compat` requires a fresh agreement. `accept_spec` pins the hardened value to a stored oracle. A missing label fails the ledger. Agreement is still a weak oracle; the metamorphic checks below are what catch a fault both parsers share.

## Follow-ups from a successful parse

Source: T. Y. Chen, S. C. Cheung, and S. M. Yiu, "Metamorphic Testing: A New Approach for Generating Next Test Cases", HKUST-CS98-01, arXiv:2002.12543. https://arxiv.org/abs/2002.12543

On a successful record list, `metamorphic.check_success` builds follow-ups in memory and does not rewrite corpus files. The quote check serializes with the single dialect quoter and reparses. Headerless rows may be reordered; the output rows must move with them. A clean multi-field row also yields the separator-neighbor used as Chen's adjacent-key probe: the characters that touch the first separator are swapped, and each adjacent field is parsed alone. The relation is the literal fields around that separator. A parser that is right on the seed row and shifts the neighbor fails the check, including when it does not crash. A follow-up that crashes also fails it. The dialect declares the preconditions: a row starting with the `@slip` header mark blocks permutation and the boundary edit, because moving that field first changes the parse, and those cases are counted as skips. `full=False` keeps one reverse permutation and one adjacent-field probe. The stock check uses the full set: every order up to five rows, then rotations plus the reversal so the check cannot grow factorially. A relation that holds is not treated as a proof.

## Grammar splice of a historical fragment

Source: Christian Holler, Kim Herzig, and Andreas Zeller, "Fuzzing with Code Fragments", USENIX Security 2012. https://www.usenix.org/conference/usenixsecurity12/technical-sessions/presentation/holler

`grammar.parse_tree` is the only fragment learner. It cuts records, fields, quoted fields, separators, and breaks from corpus bytes. A valid skeleton replaces its first data field with a historical quoted fragment; with the stock corpus that is the doubled-quote field from `SLIP-001`, spliced under the `@slip` header of `CASE-header`, and the result is a fresh disagreement. A character delete and a field add supply the nearby edits, and an edit is labelled `valid` only if the grammar still accepts it. Fragments the grammar rejects, other than NUL, CR, and the hang token, are prepended only in a `malformed_probe` slot. The batch is classified by the differential harness before `apply_cap` can write anything. `contexts_still_failing` replays a crashing fragment in a second skeleton after a patch that special-cases only the first skeleton. The paper's defect definition is abnormal termination, so this lab does not score a splice by crashes alone.

## Shortlex shrink under a validity gate

Source: David R. MacIver and Alastair F. Donaldson, "Test-Case Reduction via Test-Case Generation: Insights from the Hypothesis Reducer", ECOOP 2020. https://drops.dagstuhl.de/entities/document/10.4230/LIPIcs.ECOOP.2020.13

`shrink.shrink` is an external reducer. Interestingness, from `make_interesting`, keeps the outcome class, the side, the truncation flag, and the parent's dialect validity: a valid parent must stay valid, and an invalid parent must stay invalid. Passes delete one record, then one field, then one character, and each pass repeats until it sticks. Among accepted edits the shortlex-smaller byte string is kept (shorter, then lexicographically smaller). A second shrink of the result is a no-op, and no explored one-edit neighbor is both interesting and smaller. The padded quote disagreement reduces to the four quote bytes `""""`. An edit that breaks the doubled-quote form is rejected. This is not Hypothesis's internal choice-sequence reducer. The passes are structural so a reader can see which bytes the failure still needs. They are local minimizers.

## Upper bound on the stdout handler

Source: Python Software Foundation, "Logging Cookbook". https://docs.python.org/3/howto/logging-cookbook.html

`logging_setup.configure` calls `logging.config.dictConfig` with the root level at DEBUG, `disable_existing_loggers` false, and the formatter `%(levelname)-8s %(name)s %(message)s`. The file handler is DEBUG. The stdout handler is INFO and carries `MaxLevelFilter`, which keeps a record only when `levelno <= WARNING`. The stderr handler is ERROR. INFO carries the case id, outcome, side, and disposition. ERROR carries the exception type and the side. DEBUG, which stays in the run file, carries the byte length, SHA-256, parent ids, and the truncation flag. Field payloads are not placed on the INFO line. The log directory is created or refused before a parser starts. A path that is not a directory raises `LogConfigError`.

## Limits kept in view

A passing differential run can hide a shared bug. A passing relation does not prove the parser correct. The shrink stops at a local minimum. A timeout that is tighter than the parser's real work becomes a false hang, so isolated rows store the timeout that was used. Splice depth is reported and is not an acceptance gate. Branch coverage on the hardened parser is the same kind of side report: after the stock check, `hang_reject` is the untouched branch because the hang token is not in the in-process corpus.
