# Python Maintenance & Regression Lab — Case 006

A small offline interchange for partner notes. Callers drop UTF-8 text that Windows and Linux editors saved with different newlines, and the lab writes one CSV byte profile a downstream tool can hash. The defects under test are encoding and path portability: a locale text open, a second newline translation, an overlong or surrogate UTF-8 sequence, a leading BOM, a case-fold file collision, and a replace that runs while the temp handle is still open.

This is a maintenance sample, not a live service. Fixtures are synthetic. No network calls are made.

The defect under test is cross-platform text interchange. The lab does not map catalog fields, parse a private grammar, schedule jobs, recover a ledger, or score API revisions.

## Problem

The same note bytes do not survive a careless Python open:

- On a Japanese Windows host, `open(path)` with UTF-8 mode off uses cp932. The UTF-8 bytes of `é` (`C3 A9`) become two halfwidth katakana characters and no exception is raised. Forcing cp1252 on the undefined byte `0x81` raises `UnicodeDecodeError: 'charmap' codec can't decode byte 0x81`.
- The csv excel dialect already writes CRLF. A text-mode open on Windows translates the writer's `\n` again, so the file contains `\r\r\n`.
- A decoder that accepts the overlong sequence `C0 80` produces U+0000. `C0 AF` produces `/`. The surrogate bytes `ED A1 8C ED BE B4` produce U+233B4. None of those are UTF-8.
- `str.splitlines()` breaks on U+0085, U+2028, and U+2029. A regex written for a logical line then fails, or a character that is not a record separator disappears into the line structure.
- Creating `readme.TXT` next to `Readme.txt` works on a case-sensitive disk and collides on Windows.
- `os.replace` while the temp file is still open raises WinError 32 on Windows.

## Architecture

```
foreign bytes
  -> leading-BOM refusal
  -> strict UTF-8 checker (shortest form, no surrogates, no 5-octet leads)
  -> logical lines on CRLF, LF, CR only
  -> in-memory text joined with LF

rows of str
  -> reject C1, lone surrogates, unassigned (Cn), and CR/LF inside a field
  -> NFC with this interpreter's unicodedata
  -> csv excel dialect into a text file opened with encoding utf-8 and newline ""
  -> profile check on the bytes (no BOM, CRLF only, no CR CR LF, no C1)
  -> close the temp handle, then os.replace

diagnose_mojibake(text) -> int
  -> count of suspicious pairs, used by logs and tests
  -> not called by emit, and it does not rewrite the field
```

Package `netunicode_lab`:

- `policy.py` — encoding name, strict error handler, newline, NFC, C1, and unassigned checks
- `utf8strict.py` — byte checker
- `ingest.py` — `read_foreign_text`
- `emit.py` — `emit_interchange_csv` and `assert_interchange_bytes`
- `files.py` — `atomic_write_bytes`, pathlib paths, case-fold check, close-then-replace
- `diagnose.py` — badness counter
- `legacy.py` — the unfixed patterns the tests contrast; ingest and emit do not import it
- `logsetup.py` — one warning line per failure: boundary, codec, optional hex byte, optional Unicode version

Public functions:

- `read_foreign_text(path) -> str`
- `emit_interchange_csv(path, rows) -> None`
- `assert_interchange_bytes(data) -> None`
- `atomic_write_bytes(path, data) -> None`
- `diagnose_mojibake(text) -> int`

A failure log looks like `boundary=foreign-ingest codec=strict-utf8 byte=0x81` or `boundary=interchange-emit codec=nfc unidata_version=...`. The file payload is not included.

Maintenance loop the tests encode:

1. Reproduce the failure from a `bytes` fixture or from `legacy.py`.
2. Name the signature: `charmap-byte`, `newline-bytes`, `winerror-32`, `casefold`, or `context-dependent`.
3. Change `policy.py` or a single call-site argument that imports it.
4. Re-run the unittest command below. An encoding repair is finished when the first four signatures stay empty. `context-dependent` failures are not fixed by touching codecs.

## Run

From the repository root, Python 3.10 or newer, standard library only:

```text
python -m unittest discover -s projects/python-maintenance-regression-lab-case-006/tests -v
```

Offline CLI. Commands print one ASCII JSON object, so a cp932 console does not have to encode the note text:

```text
python projects/python-maintenance-regression-lab-case-006/run_lab.py ingest projects/python-maintenance-regression-lab-case-006/examples/e_acute.bin
python projects/python-maintenance-regression-lab-case-006/run_lab.py ingest projects/python-maintenance-regression-lab-case-006/examples/foreign_mixed.bin
python projects/python-maintenance-regression-lab-case-006/run_lab.py ingest projects/python-maintenance-regression-lab-case-006/examples/overlong_nul.bin
python projects/python-maintenance-regression-lab-case-006/run_lab.py emit <dest.csv> --rows projects/python-maintenance-regression-lab-case-006/examples/rows.json
python projects/python-maintenance-regression-lab-case-006/run_lab.py check <dest.csv>
python projects/python-maintenance-regression-lab-case-006/run_lab.py diagnose "é$"
python projects/python-maintenance-regression-lab-case-006/run_lab.py --log-signatures ingest projects/python-maintenance-regression-lab-case-006/examples/overlong_nul.bin
```

`--log-signatures` attaches a stderr handler for the duration of the command, so the signature line (`WARNING netunicode_lab boundary=foreign-ingest codec=strict-utf8 byte=0xc0`) is printed before the JSON error record. Without it the library logger has only a `NullHandler`.

`<dest.csv>` must live in a directory that already exists. Emit replaces an existing file of the same spelling and refuses a second spelling that casefolds to an existing name. The checked-in oracle is `tests/golden/rows.csv`.

`examples/rows.json` is the golden row set `[["café", "Ω"], ["x", "y"]]`. Emitting it produces `tests/golden/rows.csv`: 15 octets, UTF-8, no BOM, CRLF after each record, sha256 `6eba8588c5f2f573229b74003b682de76fb6b18baa473d3096bcae9d4efab653`.

The project's `.gitattributes` marks `tests/golden/**` and `examples/*.bin` as `-text`. Without it, a checkout with `core.autocrlf=true` stores the golden CSV with LF line endings and a Linux clone fails the byte oracle.

Ingest of `examples/overlong_nul.bin` exits 1 with `StrictUtf8Error` and `strict-utf8 rejected byte 0xc0 at offset 0`.

## Design decisions

- Two pipelines share the strict decoder and do not share newline policy. Ingest normalizes CR, LF, and CRLF into logical lines. Emit writes CRLF only. Mixing them is what produces `\r\r\n`.
- The codec, the error handler, and `newline=""` live in `policy.py`. Emit passes those names into `open`. `tests/test_gate.py` fails if that text open drops `encoding`, `newline`, or `errors`.
- The profile checker compares bytes. `str.splitlines()` is not the oracle. U+2028 inside a note stays one logical line; the same character is not a CSV record break.
- NFC uses `unicodedata` for this interpreter. The assignment failure log names `unicodedata.unidata_version`, which is the Unicode-plus-NFC pair the sender is using.
- Paths are `pathlib` values. The library does not rewrite slash characters. The case-fold check runs before create, including on Windows, where the other spelling would open the existing file.
- Temp bytes are published only after the `with` block closes the handle. A recording double around `open` and `os.replace` checks that every handle is closed when replace runs; a mutant that replaces inside the block fails it. If replace fails, the temp file is removed and the old destination stays. The WinError 32 mutant stays in `legacy.py`.
- Mojibake detection counts pairs in text that is already Unicode. A positive count flags a short field. Emit still accepts legal text such as `é$` and writes it unchanged.
- The static gate mirrors Ruff's unspecified-encoding rule for text opens and `read_text` / `write_text`. Binary opens are allowed. The gate is necessary and it is not the behavioral oracle. This project does not invoke the Ruff executable.

## What it demonstrates

- A strict UTF-8 reject for overlong NUL, overlong slash, surrogate bytes, truncated sequences, and scalars above U+10FFFF, with round-trip identity for accepted bytes. A seeded differential test checks that the byte walker accepts exactly what CPython's strict decoder accepts on 5,000 short byte strings.
- The charmap signature for cp1252 byte `0x81`, and a production error that says `strict-utf8` instead.
- One golden CSV digest when emit runs in a child process under `PYTHONUTF8=0` and `PYTHONUTF8=1`. On the Windows host where this was run, `PYTHONUTF8=0` leaves the locale codec at cp932. `PYTHONIOENCODING` only changes stdio, so those cells show that console settings do not leak into the file.
- Portable mutation checks: forcing `cp1252` or `newline="\r\n"` into emit (what a Windows ANSI-locale text open does) makes emit raise and leave the directory empty, on any OS.
- NFC identity for U+2126 versus U+03A9, and for `a` plus U+0300 versus U+00E0.
- A case-fold collision that names both entries and leaves the original bytes in place.
- Close-then-replace on every platform, and WinError 32 for the still-open mutant on Windows.
- A mojibake counter that does not sit on the emit call path.

## Limitations

- The suite was executed on Windows (Python 3.10, cp932 ANSI code page). A second OS should run the same unittest modules and classify any diff as `charmap-byte`, `newline-bytes`, `winerror-32`, `casefold`, or `context-dependent`. A Linux run is not claimed here. The real-file text-mode mutant is skipped unless `os.linesep` is CRLF, and the WinError 32 assertion is skipped off Windows. The forced-codec and forced-newline mutants and the close-before-replace order run everywhere.
- On a host whose locale is already UTF-8, the `PYTHONUTF8=0` child cell does not exercise a legacy codec. The forced-`cp1252` test is the portable check for that case.
- `casefold()` is not a full Windows ordinal-casing table. It flags pairs a particular filesystem might still distinguish. The portable outcome is to rename.
- The mojibake count is a heuristic. It is not proof, and it does not repair the string. Longer text is more likely to contain a hit; the suite does not fail the build for that.
- U+0000 is well-formed UTF-8 and is not given an extra ban. Private-use characters (category Co) are not category Cn, so they can be emitted. Form feed is not treated as structure.
- There is no UTF-8 mode switch, no filesystem surrogate round-trip, no WHATWG replacement decoder, and no NFKC or NFKD path. Legacy multibyte encodings are not sniffed. A cp932 reading of partner-note bytes is a different string and is not written to the CSV.
- The parent directory must already exist. Emit does not create it.
- Golden bytes follow this machine's `unicodedata` for the two RFC pairs, which have been stable NFC mappings. A future Unicode upgrade that changes NFC for a character you add to the golden file will change the digest; the assignment log is there so the upgrade is visible.
- No durability `fsync` is performed. The publish rule is close, then replace.
