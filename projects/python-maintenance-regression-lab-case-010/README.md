# Python Maintenance & Regression Lab — Case 010

A synthetic wharf office still receives intake sheets from an older exporter. The legacy reader accepts bare line breaks, `#` remarks, single quotes, backslash escapes, a leading UTF-8 BOM, and short or long rows. The replacement is a strict recognizer. Every place the two differ is a named decision, a byte file, and a sidecar, so a later edit cannot quietly widen the language.

The sheets in this project are synthetic berth and stock rows. There is no live intake service.

## Problem

The office needs three guarantees at once:

- A file that follows the strict profile parses to a header tuple (or no header) and a tuple of string records, or it fails with a stable code and a byte offset.
- Files the legacy reader still has to accept stay on an explicit repair list, each with the date and the condition that removes the repair.
- The only writer emits bytes the strict profile accepts again. Field text is not reformatted, and a long digit string stays that string.

## Architecture

```
bytes
  -> decode.py          label check, then UTF-8 fatal or replacement
  -> recognize.py       legacy_parse or hardened_parse (parse is hardened)
  -> handler            data records only, and only after full acceptance
  -> unparse.py         the only writer: UTF-8, comma, CRLF, doubled quotes
```

`src/wharf_sheet/` is the package.

| Module | Role |
| --- | --- |
| `decisions.py` | Closed table. Repair rows carry `end_state`, `removal_condition`, and `break_date`. |
| `decode.py` | Encoding Standard labels. Only UTF-8 labels decode. |
| `recognize.py` | Both scanners. Strict success has an empty event tuple. |
| `unparse.py` | Emits strict bytes. `write_sheet` uses `newline=""`. |
| `corpus.py` | Loads `corpus/*.bin` plus the JSON sidecar and checks the oracles. |
| `differential.py` | Same bytes to both parsers. Minimize, classify, and refuse an unclassified write. |
| `mutants.py` | Six hand-placed faults. Kill rate and statement coverage are separate. |
| `logsetup.py` | Logger `wharf_sheet`. Locations only. A `NullHandler` is attached at import. |

The corpus buckets are `y_` (strict accept), `n_` (strict reject), `i_` (frozen local choice), `c_` (both profiles return the same records; agreement is compatibility, and the sidecar says `conformance_oracle: false`), and `b_` (a dated break the two profiles must still exhibit).

## Run

Python 3.10 or newer. The standard library is enough. From the repository root:

```
python -m unittest discover -s projects/python-maintenance-regression-lab-case-010/tests -v
```

The same directory holds a small command line:

```
python projects/python-maintenance-regression-lab-case-010/run_lab.py check
python projects/python-maintenance-regression-lab-case-010/run_lab.py parse projects/python-maintenance-regression-lab-case-010/examples/intake_strict.csv --header present
python projects/python-maintenance-regression-lab-case-010/run_lab.py parse projects/python-maintenance-regression-lab-case-010/examples/legacy_comment.bin --profile legacy
python projects/python-maintenance-regression-lab-case-010/run_lab.py differential --seed 20261006 --budget 8
```

`check` prints `{"cases": 39, "problems": 0}` when the sidecars match the parsers, and writes nothing to stderr on a clean run. `parse` prints one JSON object and exits 0 on acceptance, 1 on a parse failure, and 2 on a bad argument or an unreadable file. `differential` prints the campaign summary. The campaign has no write path, so `files_written` is always 0 and `corpus/` is unchanged; the tests also hash `corpus/` before and after. Add `--verbose` to `check` or `differential` to see the log lines for every expected rejection and repair.

`examples/intake_strict.csv` is a strict sheet: header present, one quoted comma, spaces kept, CRLF, no BOM. `examples/legacy_comment.bin` starts with a `#` remark. The hardened profile rejects it. The legacy profile skips the remark and returns the stock row, with a `D-comments` event.

## Design decisions

The strict profile follows RFC 4180 where the RFC states a rule, and a decision id where it does not. In three places it is deliberately narrower than RFC 4180: `'` and `\` are legal TEXTDATA in the RFC, and so is a record starting with `#`, but the legacy reader gave those bytes a different meaning, so strict mode rejects them loudly instead of reading them differently from the legacy reader.

- Records separate on CRLF. The last record may omit it. A quoted field may contain CR or LF. A bare LF or CR is `E-bare-lf` or `E-bare-cr` on the hardened profile (`D-crlf`).
- `a,b,` is three fields. That reading is frozen as `D-empty-field` because the ABNF allows an empty field.
- Spaces stay in the field. The caller says whether the first record is a header. A repeated header name fails strict mode.
- The only quote is `"`, and the only escape inside it is `""`. A `'` at field start, a backslash before a separator, quote, or backslash, text after a closing quote, a bare `"`, and an unclosed quote each have their own code. Text after a closing quote fails in both profiles, including a backslash there.
- `#` at the start of a record is `E-comment` on the hardened profile. The legacy profile skips that record and logs `D-comments`.
- The field limit counts decoded characters. Length equal to the limit succeeds. One character over is `E-limit`. The default limit is 4096. Corpus limit files use 8.
- Zero data records is `E-empty`. A blank line is one empty-field record, which is a different outcome.
- Strict decode fails on the first ill-formed UTF-8 sequence and does not call the scanner. Legacy replacement strips one leading `EF BB BF`, records `bom_stripped`, and puts an ASCII byte that follows an illegal continuation back into the stream. `utf8-bom` is an unknown label. `windows-1252`, `ascii`, and `latin1` are known labels and are rejected because this window only runs the UTF-8 hooks.
- A semicolon is data (`D-comma`). A 17-digit field stays a `str` (`D-digits`).

Legacy repairs, each with `break_date` 2026-04-01: bare breaks, duplicate header names, ragged rows, single quotes and backslash escapes, `#` remarks, one leading BOM, and replacement decoding. Each row names the condition that removes it. Every repair emits an event; a header with two repeated names gets two `D-dup-header` events.

Logging is on logger `wharf_sheet`. A clean strict success logs nothing. A failure is a WARNING with code, decision, record, field, byte, and `bom_stripped`. A repair is an INFO line with the decision and those indexes. A differential separator or quote-style normalization is an INFO line naming the fold. The field text is not part of any line. `parse`, and `check` or `differential` with `--verbose`, add a stderr handler at INFO when the only handler is the import-time `NullHandler`.

## Maintenance workflow

1. Reproduce the failure on a corpus file, or on a witness minimized by `differential.minimize`.
2. If no decision row explains it, add the row and a bucketed `.bin` / `.json` pair in the same change. Do not copy a sidecar from whichever parser just ran. `differential.admit` refuses a pair without a known decision id and bucket, a name that is not lower-case `bucket_component_condition`, and a name that already exists.
3. A change to the strict accept set edits `freeze_pairs()`, the decision row, and a corpus file together. `tests/test_contract.py` pins the `(id, strict, legacy)` tuple as a literal, so editing a row fails that test. A code change that starts accepting a pattern is caught only if an `n_` file covers that pattern; `tests/test_corpus.py` requires every error code the recognizer or decoder can return to appear in a strict corpus outcome.
4. Retire a legacy repair only after every `c_` file passes on the hardened parser and that `b_` row is removed.
5. Re-run the unittest command above. Keep the kill rate and the statement-coverage figure as separate numbers. A file stays when dropping it lowers the kill rate, including when coverage does not move. `n_quote_unclosed.bin` is the file that kills `drop_unclosed`.

## What this demonstrates

- A layered parser with typed success and failure, and a handler that runs only after acceptance.
- A regression corpus of original bytes plus sidecars, including compatibility breaks with a removal condition.
- Defensive UTF-8 decoding that keeps comma and quote bytes available to the scanner in replacement mode.
- An unparser round trip for every strict success.
- A mutant kill matrix and a seeded differential campaign that classifies witnesses and writes nothing until `admit` is given a decision id and a bucket.
- Location logs, and a field limit that the package enforces on decoded text.

## Limitations

- Labels other than the UTF-8 labels are recognized and rejected. The gb18030 end-of-queue step is not implemented.
- The two profiles can share a bug. A `c_` file records agreement, and the differential runner will not treat that agreement as proof the bytes are conformant.
- Generation is one seed and a small budget. The first seven candidates are fixed seed inputs (limit boundary, comment, bare LF, illegal byte before a comma, BOM, header); random layers fill the rest of the budget. The mutants are hand-placed.
- A parse that exceeds the timeout (1 second by default) is reported as a timeout witness. Crash and timeout witnesses have no decision id, so they count as unclassified and make `differential` exit 1. The worker is a daemon thread and is abandoned. This lab does not kill a stuck interpreter.
- Strict mode keeps a leading U+FEFF as field text (`D-bom`). The emitter writes it back as `EF BB BF`, which is byte-identical to a BOM, so a reader that strips BOMs will drop that character. The strict round trip is exact; interoperability with BOM-stripping readers is not claimed.
- The package scanner is the recognizer. One test uses `csv.reader` only to show that a bare LF is a different outcome there. This project does not claim Excel behavior, and it does not claim that the stdlib reader implements this decision table.
