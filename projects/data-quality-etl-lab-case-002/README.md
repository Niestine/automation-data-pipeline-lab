# Data Quality ETL Lab — Case 002

Edition Gate is a storeroom parts-catalog ingest. A supplier CSV is admitted only when a schema registry can show that the current reader still understands that writer, including writers from earlier editions. The lab uses synthetic rows only.

## Problem

Parts files keep their column names, encodings, and quoting habits after the catalog schema has moved on. A rename that forgets the old header, a required column added in one jump, or a file from two editions ago will load the wrong field or invent a value. Edition Gate separates those failures: fatal decode, dialect abstention, row quarantine, cell errors that stay on the row, and schema rejection before any row is built.

## Architecture

The package is `edition_gate`. Six gates run in order.

1. **Decode.** A BOM selects UTF-8, UTF-16BE, or UTF-16LE and overrides a conflicting declared charset. With no BOM, the declared label is required. `utf-16` without a BOM is fatal. Illegal sequences are fatal. A NUL after a successful decode is `binary_payload`. `windows-1252` is used only when the file declares it.
2. **Dialect.** A writer contract is parsed as written. A search scores candidate delimiters, quotes, escapes, and a one-row preamble by pattern consistency times the fraction of typed cells. Equal scores with different cell matrices return `dialect_tie`. Python's `csv.Sniffer` is recorded and does not break that tie.
3. **Records.** RFC 4180 quoting, doubled quotes, and embedded breaks. Spaces stay in the field. A trailing delimiter quarantines that row and does not invent an empty cell. A stray quote in an unquoted field, or text after a closing quote, is `bad_quote` for that row. Later rows still load. Two header cells that map to the same column refuse the file as `schema_resolution` instead of letting one overwrite the other.
4. **Cells.** The raw string and the typed value are both kept. `NA` is null. A bad number, date, or boolean, or a number that overflows a double, is `type_error` and the row is still emitted. A string longer than the column's `max_length` is `length_error`. An explicit empty string stays empty.
5. **Registry.** Reader aliases and rename equivalence map old headers onto current names. Writer-only fields are ignored. A reader default fills a column the writer omitted. Widening promotions are `integer` to `long`, `double`, or `decimal`, and `long` to `double`; `integer` to `decimal` is a lab addition that is not on the Avro list. Narrowing is rejected. Publication of a required column moves one step at a time: `absent`, `delete_only`, `write_only`, backfill, `public`. A drop reverses that path, and stored values are removed only by an explicit reorg while the column is `delete_only`. Compatibility is checked in both directions against every retained version, so a default added on the middle edition does not make the oldest edition able to read a dropped column. The forward direction is relaxed for widening (see Limitations).
6. **Link.** An exact `supplier_id` + `sku` match wins. One SKU claimed by two canonical ids is `sku_collision`. Otherwise rows block on supplier, NFKC-casefolded brand, and standardized size. Inside a block the title score is the mean of Jaro, prefix-weighted Jaro-Winkler (scale 0.1, prefix 4), and token-bigram Dice. At least 0.92 links, below 0.78 is a non-match, and the open interval is `review_band`. A missing block key falls through to a sorted neighborhood on title, size, and brand. A pair whose titles differ only by a Unicode compatibility fold is a review, not a link. Rows held for any linker review are not written to the canonical table.

`ingest_bytes` is idempotent for the same payload, writer version, reader resolution fingerprint, declared charset, search flag, error budget, link flag, and link settings. A dry run parses and links without writing the store or the batch cache. `read_with_retry` retries `OSError` with delays 0.01s, 0.02s, and so on, and rejects `attempts` below 1. The default sleeper records that schedule and does not wait. Logs go to the `edition_gate` logger. JSON output is UTF-8 with sorted keys and no timestamps.

## Run

From the repository root, with Python 3.10 and the standard library:

```text
python -m unittest discover -s projects/data-quality-etl-lab-case-002/tests -v
python projects/data-quality-etl-lab-case-002/run_lab.py --out-dir projects/data-quality-etl-lab-case-002/examples/out
python projects/data-quality-etl-lab-case-002/run_lab.py --out-dir projects/data-quality-etl-lab-case-002/examples/out --dry-run
```

The demo registers three catalog editions, ingests `examples/vendor_v1.csv` and `examples/vendor_v2.csv`, then ingests the first file again after `bin_code` has been staged. `--dry-run` prints the same batch lines and does not create the output directory.

On this development interpreter (Python 3.10.11) `unicodedata.unidata_version` is `13.0.0`. The demo manifest records whatever version the running interpreter ships.

## Design decisions

- Stored text is NFC. Match keys and brand blocks use NFKC. Casefold runs only inside the linker, after standardization of a short abbreviation list (`street` to `st`, `fourth` to `4th`, `millimeter` to `mm`, and the same kind of token). `zinc` is left intact so `znc` stays a review.
- A default applies when the writer schema has no cell for that column. A quoted empty field is data.
- The parse fingerprint keeps name, type, and size. The resolution fingerprint also keeps aliases, defaults, null tokens, required flags, formats, and publication. Changing a default changes the second hash only.
- Rename is one schema operator and must leave the old name as a reader alias. Drop plus add is a different operator and needs an explicit transform. Merge needs provenance. Split needs a shared key or provenance.
- A file may be one catalog edition behind the reader. Older than that is `schema_too_old` and produces no rows. The current writer must still emit its own required public columns, even when a default exists.
- An error budget can quarantine the whole file. Below the budget, cell errors stay on the emitted row.

## What the demo and tests show

`vendor_v1.csv` loads as writer edition 1 while the reader is edition 2. The header `org_name` lands in `organization`. Row 1 matches canonical `P-100` by supplier and SKU. Row 2 has no SKU and links to the same part inside the supplier/brand/size block. The ligature in `ﬁtting` is stored as NFC, flagged `compatibility_fold`, and given a new id. `pack_qty` value `one` is a `type_error` on an emitted row. The short last row is quarantined as `ragged_row`, and the file still completes as `accepted_with_errors`. `vendor_v2.csv` loads as the current writer. After `bin_code` is `delete_only` on edition 3, the edition-1 file is `schema_too_old`.

The semicolon probe `examples/messy_semicolon.csv` selects `;`. `csv.Sniffer` selects `,` on that file. The same gap holds for the pipe and tab fixtures whose titles also contain commas. Clean comma, semicolon, and tab files agree with the sniffer.

The suite also covers BOM override, fatal UTF-8, text after a closing quote, duplicate headers, `max_length`, a cache key that changes with the declared charset and link flag, a compatibility-fold review that is not stored as a new part, declared CP932, a closed charset set, quoted European-style decimals, publication backfill and reorg, the email drop that neighbor checks accept and the transitive check rejects, `long` to `double` precision loss for `9007199254740993` and `9007199254740995`, link precision 1 and recall 1 against an exact-key baseline recall of 0.25, idempotent replay, and a dry run that leaves the store unchanged.

## Limitations

- The forward half of the compatibility check accepts a widened writer type for an older, narrower reader. Avro and Confluent FULL would reject an `int` to `long` change. The lab allows it because every CSV field is text: an out-of-range value would be a `type_error` cell in the older reader, not a failed file. Narrowing is still rejected.
- The publication states follow the F1 add/drop ladder. This process is one local registry, and it does not reproduce F1's distributed lease or anomaly argument.
- Normalization uses the interpreter's `unicodedata` (13.0.0 here). That is not a run of Unicode 18.0.0 `NormalizationTest.txt`.
- Dialect search prefers space over comma on `alpha one, beta`, and it prefers an empty quote on a single column of quoted commas such as `"red,large"`. Those files are the known misses of this score. A semicolon file of quoted `12,50` values ties, because the quote does not raise the type score, and the sniffer's semicolon guess is not allowed to settle it.
- The neighborhood compares each row with the previous `w - 1` rows of one key. A pair that is distant on title, size, and brand is not compared.
- A dropped column is not restored by the inverse operator. The reorg deletes the stored value first.
- The command-line retry path calls the no-op sleeper. Tests inject a sleeper to observe 0.01 then 0.02. The CLI does not sleep.
- There is no live database, network fetch, or external schema registry. Batch identity is the SHA-256 cache in the process.
