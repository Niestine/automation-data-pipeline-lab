# Data Quality ETL Lab

Harbor Ledger is a weekly intake for a synthetic wholesale apparel catalog. A supplier drop arrives as CSV that may declare the wrong encoding, mix missing tokens with failed casts, break a style-to-department rule, repeat a listing under a reordered title, or carry a wholesale price that sits far outside the rest of the week. The pipeline types the file, seals a read-only anomaly report, and writes one canonical catalog. The same bytes, schema, and profile reproduce the same `clean.csv` and the same report digest.

The fixture in `examples/supplier_drop.csv` is invented. Nothing here calls a supplier site, a network API, or a live cleaning service.

## What it demonstrates

The week drop is a comment, a blank line, a header, and sixteen listings. The seal does the following:

| Listings | Fixture | Seal |
| --- | --- | --- |
| HL001, HL002 | Same style, color, and price; titles are a token swap | Link weight `13.047209`. HL002 is dropped. |
| HL003, HL004 | Style DN200 is both denim and shirting | Functional-dependency findings on both cells. Department is not a match field, so the pair also links and HL004 is dropped. |
| HL006 | Supplier EAST with department shirting | Constant-rule findings. The row stays. |
| HL007 | Wholesale `910.00` | Range finding and a modified-z outlier. The row stays. |
| HL008 | Fullwidth style `ＫＴ３１０` | Pattern finding. NFKC `KT310` is an annotation. The published cell stays fullwidth. |
| HL009 | Wholesale `NA` | Required value is missing. The row is excluded. |
| HL010 | Date `01/02/2020` | Cast error under `YYYY-MM-DD`. The row is excluded. |
| HL011 | Three fields | Ragged row. Later columns are not taken from the next line. |
| HL012 | Title `Coat, Hooded` | Quoted in `clean.csv`. |

Pattern, functional-dependency, constant-rule, range, and outlier findings stay in the clean file and in the report. Rows are excluded for a ragged record, a cast error, a null required or primary-key cell, a later member of a link cluster, or a repeated primary key. The survivor of a cluster or of a repeated key is the structurally sound member with the earliest `source_row`. Two rows that repeat a key are a primary-key finding and are never scored as a record link.

Clean listing ids for this fixture are HL001, HL003, HL005, HL006, HL007, HL008, HL012, HL013, HL014, HL015, and HL016.

## Architecture

`run_lab.py` loads a Table Schema file and a frozen profile, then runs eight stages:

1. **Read.** `read_with_retry` retries `OSError` for the profile's attempt count. Tests inject the sleeper and record the delay schedule. The command-line entry point uses a no-op sleeper, so a shell run retries without waiting.
2. **Decode.** Supplier bytes use an allow-list (`utf-8`, `windows-1252`, `shift_jis`). A BOM wins over the declared label. `ascii` and `latin1` select windows-1252. Replacement characters above the threshold quarantine the drop. Owned files (`--owned`) are strict UTF-8, and a BOM there is fatal. Output encoding is UTF-8.
3. **Parse.** The closed CSV profile writes a header, CRLF, a trailing break, and equal-width records. Quotes wrap a field that contains a comma, quote, or break. Trim is off unless the schema asks for it. `source_row` is the physical record number. `output_row` counts emitted data rows.
4. **Cast.** `missingValues` are applied before the type parser. Numbers honor `groupChar`, `decimalChar`, and listed affixes when `bareNumber` is false. Dates are `YYYY-MM-DD` or one `strptime` pattern. Booleans use the schema's token lists. A failed cast is stored separately from a missing token. Strings are stored in NFC.
5. **Annotate.** Identifier fields (`listing_id`, `style_code`) record an NFKC fold when it differs from the stored text. The published value is the NFC text.
6. **Detect.** Four pure functions run on one snapshot, in profile order: pattern and type, constraints, modified-z outliers, and duplicate links. The sealed finding list is their union. The profile sets `repair_between_detectors` to false, and the log line is `detector union sealed`.
7. **Decide duplicates.** Standardized strings, a sorted neighborhood of width `w`, and Fellegi-Sunter weights produce `link`, `possible`, or `non-link`. Union-find clusters link edges only. m, u, and the outlier threshold stay at the profile values.
8. **Publish.** Structurally sound survivors are sorted by primary key, then `source_row`. `clean.csv` is the canonical catalog. `report.jcs` is the RFC 8785 form, and its SHA-256 is the digest. `report.json` is a pretty view of the same object. `run_manifest.json` records the input hash, encoding, seed, window, thresholds, normalization columns, and digest.

Money is a decimal string (`86.00`, `910.00`). The report records `unicodedata.unidata_version` from the interpreter that ran the seal.

## Run

From the repository root, with the standard-library Python that can import this package (developed on Python 3.10):

```text
python projects/data-quality-etl-lab/run_lab.py --input projects/data-quality-etl-lab/examples/supplier_drop.csv --schema projects/data-quality-etl-lab/examples/schema.json --profile projects/data-quality-etl-lab/examples/profile.json --out-dir projects/data-quality-etl-lab/examples/out
```

`--dry-run` computes the digest and writes nothing. `--owned` treats the input as a pipeline-owned UTF-8 file. `--oracle` points at a JSON cell oracle (`{"cells": [...]}` or a list). Each cell needs `row_id`, `column`, and `class`. Optional `slice` is `eval` or `train`. The report then includes per-class precision, recall, and F for the evaluation slice and the training slice. Scores count cells, and every parsed row the oracle does not mark `train` is in the evaluation slice, so a finding on a row the oracle calls clean is a false positive.

Exit code 0 covers a sealed drop and a quarantine. Exit code 2 covers a fatal decode, a missing or ragged header, a schema or profile error (including an unknown detector name or a malformed threshold), and an unreadable path. Quarantine writes `report.jcs`, `report.json`, and `run_manifest.json`, and does not write `clean.csv`. A fatal decode writes nothing.

Verify from the repository root:

```text
python -m unittest discover -s projects/data-quality-etl-lab/tests -v
```

The tests are offline and use `unittest` only. There are no third-party dependencies.

## Design decisions

- **The published catalog still contains explained errors.** A range break, a pattern miss, a functional-dependency conflict, and an outlier are visible on the row a buyer would receive. The report cites them. Rows that cannot be typed, or that repeat a listing, are the ones removed.
- **NFKC is an annotation.** Folding a fullwidth style code in the published cell would hide the bytes the supplier sent. Match keys use the fold. `clean.csv` does not.
- **Thresholds are data, not a fit.** `examples/profile.json` freezes the modified-z cutoff, the neighborhood width, the agreement cutoffs, and the Fellegi-Sunter m and u values. `suggest_outlier_threshold`, `estimate_m`, and `estimate_u` are available for a training slice. The sealed path does not call them. `dedup.refit` is false.
- **One change cannot repair an earlier injected error.** The cell injector, used by tests, keeps a finite proposal list, changes a cell once, skips immutable columns, and refuses a candidate that heals an earlier detectable denial. An unfilled quota is a shortfall and exit code 0. A fixture that is already dirty exits 2. The command-line seal does not inject errors.
- **Possible-links stay in the review list.** They do not merge clusters. A one-character style-code difference (`KT300` / `KT310`) is a non-link under the demo cutoffs. A profile that allows edit distance 1 links it. That second profile is a test, not the week-drop profile.
- **The digest covers a canonical encoding of one parse.** Object keys sort by UTF-16 code units. Non-integer numbers are rejected, so prices cannot drift through binary floats. Two processes writing the same drop produce the same `clean.csv` bytes and the same digest.

Idempotency has three checked forms:

- The same supplier bytes produce the same `clean.csv` and the same digest.
- A permutation of clean, unique-key rows with an empty finding list keeps the digest. Clean rows carry no `source_row`, and they are sorted by primary key.
- Feeding `clean.csv` back through an owned read reproduces `clean.csv` byte for byte. The digest changes. Five findings remain, and they cite the new physical line numbers: both cells of the EAST constant rule, the `910.00` range, the outlier, and the fullwidth pattern. Moving HL006 past HL007 in the original drop also changes the digest and leaves `clean.csv` unchanged.

## Limitations

- The denial rules used for detectability are not-null, unique, primary key, one binary functional dependency, one constant conditional rule, and a numeric range. Enum and length findings are reported by the constraint detector and are outside that repairability score.
- The sorted neighborhood compares a row with the previous `w - 1` rows on each blocking key. A true pair that never shares a window is absent from the link list.
- Modified z-scores need at least `minimum_rows` non-null decimals in the column. The demo profile uses 5. A smaller week does not flag a price outlier.
- Per-class scores describe this synthetic oracle. They are not a measured recall on warehouse data, and this README does not repeat recall figures from the papers.
- The command-line retry loop does not wait. A caller that wants the profile delays passes a sleeper; tests do.
- The package is the Python standard library. It does not talk to a database, a message bus, or a supplier endpoint.
- JCS here rejects every float, including `2.0`. Integer counts that fit in an IEEE-754 double are emitted as numbers. Prices stay strings.
- Match keys keep Unicode letters and digits, but the abbreviation table and the `title_prefix` blocking key are tuned to this English-language fixture.

## Layout

```text
projects/data-quality-etl-lab/
  run_lab.py                 command-line seal
  examples/schema.json       harbor-ledger-week-drop schema
  examples/profile.json      frozen seed, thresholds, and retry schedule
  examples/supplier_drop.csv synthetic week
  src/harbor_ledger/         decode, CSV, cast, rules, detectors, dedup, JCS, injector
  tests/                     offline unittest modules
  RESEARCH_APPLICATION.md    techniques and the public sources they come from
  portfolio_manifest.json    title, skills, and the verification command
```
