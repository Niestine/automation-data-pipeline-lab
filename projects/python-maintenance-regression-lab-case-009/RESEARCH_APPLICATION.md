# Research application

This lab uses seven public sources. The tests are the check that each technique is actually in the code. `batchnote` inventories synthetic dock-note files. Stdout is the data record. Stderr is the diagnostic record. The wait status is only 0, 1, or 2.

## Frozen three-code status

Python `argparse` documentation, "error and exit status".
https://docs.python.org/3/library/argparse.html

FreeBSD `sysexits.h`.
https://github.com/freebsd/freebsd-src/blob/main/include/sysexits.h

POSIX.1-2017, Shell and Utilities, Section 1.4 Utility Description Defaults.
https://pubs.opengroup.org/onlinepubs/9699919799/utilities/V3_chap01.html

PEP 387 — Backwards Compatibility Policy.
https://peps.python.org/pep-0387/

The argparse page says an invalid argument list prints to standard error and exits 2, and that `exit_on_error=False` (added in 3.9) raises `ArgumentError` instead. Help and version in this lab stay on stdout with status 0. Every other handled failure returns 1. `KeyboardInterrupt` and `SystemExit` are not caught, so a signal death is left to the interpreter.

The FreeBSD header lists `EX_USAGE` 64 through `EX_CONFIG` 78 and says those values exist for interface compatibility and are deprecated for FreeBSD base software. `status.HISTORIC_SYSEXITS` keeps that crosswalk. `run` never returns those integers. Moving usage from 2 to 64 would be a new contract, which PEP 387 does not allow without a deprecation. `USAGE_REMOVAL` stays `None`, so the gate cannot open.

`test_policy.py` asserts the three codes, asserts usage is not 64, and asserts every catalog status is 0, 1, or 2. `test_process_contract.py` asserts `--bogus` is 2 rather than 64. The subprocess rows for `--help` and `--version` are status 0 with empty stderr.

## finalize so a count cannot wrap to success

GNU Coding Standards, section 4.2 Writing Robust Programs.
https://www.gnu.org/prep/standards/html_node/Semantics.html

POSIX.1-2017, Section 1.4, Consequences of Errors.
https://pubs.opengroup.org/onlinepubs/9699919799/utilities/V3_chap01.html

The GNU section says an error count must not be the exit status, because the status is 8 bits and 256 errors would be reported as 0. `finalize` returns 0 only when the count is 0 and returns 1 otherwise. The POSIX section says a failed operand still produces a non-zero final status. `test_policy.py` asserts `finalize(0) == 0`, `finalize(1) == 1`, and `finalize(256) == 1`. The subprocess row of 256 missing paths exits 1 with 256 stderr lines and empty stdout.

## Two-speed operand loop and stream split

POSIX.1-2017, Section 1.4, Consequences of Errors.
https://pubs.opengroup.org/onlinepubs/9699919799/utilities/V3_chap01.html

Command Line Interface Guidelines.
https://clig.dev/

The POSIX text says a utility either continues after an operand failure or exits immediately, as its own description chooses, and that an unrecoverable error exits non-zero. Diagnostics go to standard error. The guidelines put machine-readable output on stdout and errors on stderr, and they prefer operations that do not leave a half-finished result.

A missing file, a permission error, a directory operand, and a `[[reject]]` stamp record one problem and the loop continues. Windows reports a directory as `PermissionError`, so the default opener re-raises it as `IsADirectoryError` and the type is `io` on every platform; a subprocess test passes the `examples` directory as an operand. Usage errors, a bad config file, a generic `OSError`, a software failure, and an `OSError` while writing stdout return before later operands are opened. Each stdout record is assembled in memory and written with one `write`, including the newline. `test_library_contract.py` checks the two-valid-plus-missing case, the stdout-write failure that opens nothing further, and a dead stderr that still returns 1. `test_process_contract.py` checks the same partial-success shape on the real process.

## Additive problem document

RFC 9457, Problem Details for HTTP APIs.
https://www.rfc-editor.org/rfc/rfc9457.html

FreeBSD `sysexits.h`, as the class names, not as the wait status.
https://github.com/freebsd/freebsd-src/blob/main/include/sysexits.h

RFC 9457 defines one problem-details document: `type` is the identifier, `title` is stable per type aside from localization, `detail` is occurrence prose that consumers should not parse, `status` must match the real status, and consumers must ignore unknown extension members. Section 5 says a problem is not a place for a stack dump. This lab does not send `application/problem+json` over HTTP. The `status` member is the process exit code, and the README says so.

One `Problem` value feeds both renderers. The default report stays a human line. `--report json` is an added flag. `consume` reads `type` only. `test_library_contract.py` and `test_process_contract.py` assert that every JSON `status` equals the process status, that two missing files share a title and not a detail, and that `vendorNote` plus a deleted `detail` still returns the type. A future URN under `urn:batchnote:problem:` is accepted.

## PEP 387 gate

PEP 387 — Backwards Compatibility Policy, including the soft-deprecation section and the 2025-01-27 changelog.
https://peps.python.org/pep-0387/

Public behavior here is the status integers, the stdout keys, the problem `type` and `title`, and the help text. Scraping `detail` or the human line is soft-deprecated: the README and the help epilog say to use `type` and `locator`, the old prose is still produced, and that path emits no `DeprecationWarning`. A hard change sets `USAGE_REMOVAL` to a version string. `usage_status` then warns with a `DeprecationWarning` whose text contains that version, and it still returns 2. The lab records the PEP's five-year preference and its two-release minimum. It does not pretend the wait has already happened. `test_policy.py` locks the `None` gate, the warning helper, and the still-status-2 run.

## Dual runner

Python `argparse`, `exit_on_error`.
https://docs.python.org/3/library/argparse.html

PEP 387, on behavior that help and tests already show.
https://peps.python.org/pep-0387/

GNU Coding Standards, on keeping a documented convention.
https://www.gnu.org/prep/standards/html_node/Errors.html

`run` builds the parser with `exit_on_error=False` and replaces `error` and `exit` so a usage failure returns 2 instead of raising `SystemExit`. `__main__.py` is the only `SystemExit` in the package: `raise SystemExit(run(sys.argv[1:]))`. An AST walk fails if `sys.exit`, `os._exit`, or `SystemExit` appears anywhere else. Library tests call `run` and also assert that `--bogus` does not write the process stdout or stderr. Subprocess tests run `python -m batchnote`, because an overridden `exit` inside the library is not the status a script observes. Help's epilog and `README.md` carry the same public sentences, and a test fails if they diverge.

## Human line, NUL bytes, suggestions, and quiet failures

GNU Coding Standards, sections 4.2 Writing Robust Programs and 4.4 Formatting Error Messages.
https://www.gnu.org/prep/standards/html_node/Semantics.html
https://www.gnu.org/prep/standards/html_node/Errors.html

Command Line Interface Guidelines, on suggestions, stdout/stderr, and stack traces.
https://clig.dev/

RFC 9457, section 5.
https://www.rfc-editor.org/rfc/rfc9457.html

Python `argparse`, `suggest_on_error` (added in 3.14).
https://docs.python.org/3/library/argparse.html

The GNU error section prints `PROGRAM:SOURCEFILE:LINENO:COLUMN: MESSAGE`, with a space before the message, a message that does not start with a capital after the prefix, and no trailing period. Column numbers start at 1, and tabs use stops of width 8. This lab inserts a space after each colon so the program prefix stays easy to see: `batchnote: path:line:column: message`. The JSON `title` is a different string from that message. OS errors include the filename and `strerror`. The delivered GNU text also says to accept NUL and other non-printing bytes and to size buffers dynamically. Operands are read in full as `bytes` and decoded with `surrogateescape`. JSON escapes those code points. A lone `0xFF` does not raise `UnicodeError`.

`allow_abbrev` is false, so `--verbos` is not a second spelling of `--verbose`. A single `difflib` candidate at cutoff 0.8 is a hint, and the status stays 2. `suggest_on_error` is passed only when the installed `ArgumentParser` accepts that keyword. On this workspace that keyword is absent, so the choice-typo test requires status 2 and the parser's `invalid choice` text. A unit test still checks that a `maybe you meant` sentence survives rendering.

The `batchnote` logger has a `NullHandler` and does not propagate. `--verbose` attaches a handler whose lines use the prefix `batchnote: debug: `, which is outside the problem grammar. That handler drops its own write errors, so a dead stderr under `--verbose` cannot make `logging` print `--- Logging error ---` and a traceback; a library test checks the real `sys.stderr` stays empty. `problems.emit` is the failure path. A software failure includes a traceback only when `BATCHNOTE_DEBUG=1`. The default matrix asserts that stderr contains neither `Traceback` nor `File "`.

Full `wcwidth` is not used. Characters other than tab count as width 1. The guidelines' interactive confirmation prompt is not used; a hint is printed and the typed command is not run.
