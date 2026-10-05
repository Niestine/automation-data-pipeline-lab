# Python Maintenance & Regression Lab — Case 009

`batchnote 1.0.0` inventories synthetic dock-note files. Each successful operand writes one JSON line to stdout. Diagnostics stay on stderr. The maintenance problem is the process image: a caller that branches on the wait status, or that reads one of the two streams, has to keep working after a cleanup.

The fixtures are synthetic. The tool never opens a network connection. The status, stream, and problem-record claims below are exercised by `python -m unittest`. The Limitations section lists what is not.

## Problem

A small CLI has grown several incompatible ideas of "what an error is":

- A usage failure might be changed from argparse's status 2 to a historic sysexits value such as 64. Scripts that test `$?` against 2 break.
- An operand loop that returns the error count reports 256 failures as success, because a wait status is 8 bits.
- One missing file aborts the rest of the batch, or a write error on stdout keeps opening later files.
- Expected failures print a traceback, and the human sentence is the only stable field a caller can scrape.
- A long-option typo is silently accepted as an abbreviation, which publishes a second spelling.

The cleanup keeps three statuses, continues after recoverable operand failures, and adds a JSON problem record without changing the default human line.

## Architecture

```text
path [path ...]  -->  batchnote.run
                        status.py      0, 1, 2, and finalize(count)
                        problems.py    one Problem, human line or JSON
                        __main__.py    the only SystemExit
stdout  JSON lines {path, bytes, text}
stderr  diagnostics, or batchnote: debug: lines when --verbose
```

| Path | Role |
| --- | --- |
| `src/batchnote/` | Library and `python -m batchnote`. Standard library only. |
| `tests/` | Offline `unittest` modules. `helpers.py` inserts `src` on `sys.path`. |
| `examples/` | Synthetic notes, a NUL byte file, a lone 0xFF file, config files, and one problem fixture. |
| `run_lab.py` | Project-local entry that puts `src` on `sys.path` and runs the module. |

`run(argv, *, stdout, stderr, opener)` returns an integer. It does not exit the process. The default opener reads a file in binary. Tests pass an opener that returns `BytesIO` or raises. `__main__.py` reconfigures `sys.stderr` to UTF-8 and then runs:

```python
raise SystemExit(run(sys.argv[1:]))
```

The logger name is `batchnote`. It has a `NullHandler` and does not propagate. `--verbose` attaches a stream handler on the provided stderr. Those lines use the prefix `batchnote: debug: `, which is not a problem line. Failures go through `problems.emit`. A software-failure traceback is written only when `BATCHNOTE_DEBUG=1`.

## Contract

Public statuses: 0 success, 1 failure, 2 usage.

| Outcome | Return | Loop |
| --- | --- | --- |
| every operand written | 0 | finishes |
| `--help`, `-h`, `--version` | 0 | writes stdout and returns |
| bad option, bad choice, missing operand | 2 | returns before any operand open |
| one or more recoverable operand failures | 1 | finishes, then `finalize` |
| bad config, software failure, generic OS error, stdout write error | 1 | returns immediately |

`USAGE_REMOVAL is None.`

While that constant is `None`, usage stays 2. Setting it to a version string emits a `DeprecationWarning` whose text contains that version, and the return value stays 2. The preferred removal window is five years, and the minimum is two minor releases. A shell caller usually cannot see a Python warning, so this lab does not change the status when the warning cannot reach that caller.

Help prints this block, and this README repeats it:

```text
Public statuses: 0 success, 1 failure, 2 usage.
A file containing the stamp [[reject]] is a data error and is skipped.
stdout is one JSON object per successful file, with keys path, bytes, and text.
Diagnostics go to stderr. The default report is human. --report json is additive.
Scraping the human line or the detail string is soft-deprecated.
Machine readers use --report json and read type and locator.
Options go before operands. Every token after -- is an operand.
```

Stdout keys are `path` (the operand as passed), `bytes` (the length of the raw file), and `text` (UTF-8 decoded with `surrogateescape`). A NUL or a non-UTF-8 byte stays in `text` and is escaped in the JSON line. `bytes` is the length, not the payload.

`--report human` is the default. `--report json` writes one JSON object per problem. Members, in order, are `type`, `title`, `status`, `detail`, then `locator` when a path, line, column, or argv index exists, then `suggestion` when a single long-option candidate exists. `status` inside the object equals the integer `run` returns. It is the process exit code, not an HTTP status. `detail` is occurrence prose. Callers use `type` and `locator`.

| type | title | status | speed |
| --- | --- | --- | --- |
| `urn:batchnote:problem:usage` | Command line usage error | 2 | immediate |
| `urn:batchnote:problem:data` | Input data error | 1 | recoverable |
| `urn:batchnote:problem:no-input` | Input unavailable | 1 | recoverable |
| `urn:batchnote:problem:io` | Input/output error | 1 | recoverable for a directory operand; immediate when stdout write fails |
| `urn:batchnote:problem:config` | Configuration error | 1 | immediate |
| `urn:batchnote:problem:os` | Operating system error | 1 | immediate |
| `urn:batchnote:problem:software` | Internal software error | 1 | immediate |

`locator.argv_index` is a 0-based index into the argv list passed to `run` (the process argv without the program name).

Human stderr is UTF-8, one trailing newline, no color:

- `batchnote: path:line:column: message`
- `batchnote: path: message`
- `batchnote: message`

When `message` starts with a letter, that letter is lowercase. `message` has no trailing period. `title` is not reused as `message`. For `OSError`, the body includes the filename and `strerror`. In human mode, a usage failure also writes argparse's `usage: batchnote ...` banner before the problem line. A single closest match adds `batchnote: closest match: --flag`.

Historic sysexits names are documented here and never returned:

| Name | Value |
| --- | --- |
| EX_USAGE | 64 |
| EX_DATAERR | 65 |
| EX_NOINPUT | 66 |
| EX_UNAVAILABLE | 69 |
| EX_SOFTWARE | 70 |
| EX_OSERR | 71 |
| EX_OSFILE | 72 |
| EX_CANTCREAT | 73 |
| EX_IOERR | 74 |
| EX_CONFIG | 78 |

`EX__MAX` in the FreeBSD header is 78, the same integer as `EX_CONFIG`. Mail-oriented names (`EX_NOUSER`, `EX_NOHOST`, `EX_TEMPFAIL`, `EX_PROTOCOL`, `EX_NOPERM`) are omitted until a feature needs them. Success is 0, which that header calls `EX_OK`.

## What the loop does

1. Build `ArgumentParser(prog="batchnote", exit_on_error=False, allow_abbrev=False)`. Pass `suggest_on_error=True` only when the constructor accepts that keyword. Pass `color=False` only when that keyword exists.
2. `--help` and `--version` write stdout and return 0 when their action runs. A bad choice earlier on the same command line is still usage status 2. Tokens after `--` are operands, so `--help` after `--` is a filename. `-` is a literal path, not stdin. Options go before operands: `a.txt --verbose b.txt` is usage status 2 and opens nothing.
3. Unknown arguments and a missing operand return 2 before any operand is opened. If one unknown long option has one close match at cutoff 0.8, human mode adds a second line and JSON mode adds `suggestion`. `--bogus` is not close enough to `--verbose`, so it gets no hint. Status stays 2 either way.
4. `--config file` is a JSON object with a non-empty string `batch`. It is a gate. It does not change the stdout record. A missing file, a non-UTF-8 file, invalid JSON, or a missing `batch` string is a configuration error, status 1, and later operands are not opened.
5. Walk operands in order. Open through `opener` in binary.
   - `FileNotFoundError` or `PermissionError`: recoverable `no-input`, no stdout line.
   - `IsADirectoryError`: recoverable `io`. Windows raises `PermissionError` for a directory, so the default opener re-raises it as `IsADirectoryError` when the path is a directory.
   - Any other `OSError`: immediate `os`.
   - Decode UTF-8 with `surrogateescape`.
   - A line containing `[[reject]]` is a recoverable data error. The locator's line and column are 1-based. A tab advances to the next stop of width 8. Other characters count as width 1.
   - Success: assemble one JSON line, then write it.
   - `OSError` on that write: emit `io`, return 1, do not open the rest.
6. Return `finalize(recoverable_count)`.

An injected exception that is not one of those OS errors becomes `software`. The default detail is `internal invariant failed`. The exception text and the traceback are written only when `BATCHNOTE_DEBUG=1`.

## Maintenance workflow

1. Reproduce the invocation and keep the status, stdout, and stderr.
2. Classify with `type` and `locator`, not with the human sentence.
3. Add a regression that pins the status and both streams before changing code. Library tests call `run`. The subprocess module runs `python -m batchnote`, which is the image scripts see.
4. Fix the defect. Do not return an error count. Do not move usage off 2 while `USAGE_REMOVAL` is `None`.
5. Re-run `finalize` cases, including a count of 256, and the partial-operand cases.
6. A status change or a switch of the default report dialect needs a removal version, a `DeprecationWarning` that names it, and the old behavior remaining the default for at least two minor releases. The five-year preference is the target window, not a wait this lab has already performed.

## Run

From the repository root:

```text
python -m unittest discover -s projects/python-maintenance-regression-lab-case-009/tests -v
```

Offline commands, also from the repository root. Set `PYTHONPATH` for `python -m batchnote` (PowerShell shown; in a POSIX shell use `export PYTHONPATH=projects/python-maintenance-regression-lab-case-009/src`). `run_lab.py` puts `src` on the path itself.

```text
$env:PYTHONPATH = "projects/python-maintenance-regression-lab-case-009/src"
python -m batchnote --version
python -m batchnote projects/python-maintenance-regression-lab-case-009/examples/ready_note.txt
python -m batchnote --report json projects/python-maintenance-regression-lab-case-009/examples/absent/one.txt
python projects/python-maintenance-regression-lab-case-009/run_lab.py --version
```

The `--report json` command names a path that is not in the tree. It exits 1 and writes one problem object. The other three commands exit 0.

Python 3.9 or newer (`exit_on_error` was added in 3.9). No third-party package is installed. This suite was run on Python 3.10.11 and 3.13.9. Neither interpreter's argparse accepts `suggest_on_error` (added in 3.14). The choice-typo test still requires status 2.

A successful record looks like this (the `bytes` value is the raw length of the fixture, including its newlines):

```json
{"path": "<operand>", "bytes": 28, "text": "Dock notice\nLane 4 is clear\n"}
```

`examples/ready_note.txt` is that text. The test compares `bytes` to the file's actual length rather than to a hard-coded 28, so a newline translation in the fixture still has to round-trip.

A human diagnostic for an injected missing file:

```text
batchnote: missing.txt: cannot read missing.txt: No such file or directory
```

A live operating system uses its own `strerror` text. The English string above is the library fixture.

## Design decisions

- Three returned codes, not the 64–78 sysexits range. The class moves into the `type` URN. Callers that need the class pass `--report json`.
- `finalize` collapses any positive count to 1, including 256.
- Recoverable operand failures continue. Unrecoverable failures return immediately. The utility description is this README, and the tests check the external effects.
- Stdout lines are built completely before the write, so a later failure does not leave a torn JSON line.
- The JSON problem record is additive. The default dialect stays human.
- Long-option abbreviations are off. A typo is status 2 plus at most one hint. The typed command is not executed.
- `suggest_on_error` is enabled only on interpreters whose `ArgumentParser` accepts the keyword, and only for choice and subparser typos. This program has no subcommands, so the subparser half is unused.
- Config is a gate. The batch string is not copied into the stdout record.
- The reference consumer accepts any `urn:batchnote:problem:` type, including one this version does not catalogue, and ignores unknown members such as `vendorNote`. It does not parse `detail`.

## Limitations

- There is no HTTP problem-details transport. JSON `status` is the process exit code.
- Shell `case` statements written for 64–78 see 1 for every handled failure until they opt into `--report json`.
- Human lines are ambiguous when a Windows path contains a colon. Machine readers use `--report json` and `locator.path`.
- Column width does not call `wcwidth`. Tab stops are width 8. Every other character counts as width 1.
- `-` is a filename. It does not read standard input.
- Options are not permuted after operands. `parse_known_intermixed_args` was rejected because on Python 3.10 it runs `--help` even after `--`; Python 3.13 treats that `--help` as an operand.
- With `--verbose`, debug lines are best effort. If stderr fails, they are dropped without a logging traceback.
- `python -m batchnote` reconfigures stderr to UTF-8 with `backslashreplace`. A caller of `run` who passes its own stream owns that stream's encoding.
- The `batchnote` logger is process-global. Two `--verbose` runs on different threads would share handlers.
- The program reads each operand fully into memory. There is no size cap.
- Soft deprecation leaves the scrapeable human line and `detail` in place on purpose.
- The five-year removal preference is policy text plus the `USAGE_REMOVAL is None` gate. This lab does not simulate the wait.
- `suggest_on_error` wording was not executed; neither 3.10.11 nor 3.13.9 has the keyword. The renderer test feeds it a hint sentence directly. The process test requires that wording only when the keyword exists.
- `BATCHNOTE_DEBUG=1` prints a traceback for software failures. Other failures do not.
- Partial stdout with status 1 means the caller reads the status before trusting a short stream.
- No live service is contacted. Localized `strerror` text is not pinned by the subprocess tests; those tests pin the path, the status, and the problem type.
- On Windows, Python's text streams may translate a written `\n` to `\r\n` in a pipe. Records stay one per line. The version check compares split lines, not a single newline byte.

## What this demonstrates

- A CLI error contract cleaned up without breaking argparse's status 2 or help/version status 0.
- Regression tests on the process image, plus in-process tests that cannot hide a `SystemExit`.
- An 8-bit status that cannot report 256 errors as success.
- Backwards-compatible JSON diagnostics, a consumer that ignores unknown members, and a deprecation gate that refuses a silent status change.
- Defensive reads of NUL and non-UTF-8 bytes, a data-rule locator, and logging that stays off stderr until `--verbose`.
