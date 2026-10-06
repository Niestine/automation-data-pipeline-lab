# Research application

Edition Gate uses ten public sources. Each one below changes a decision the tests lock in. Titles and URLs are the public documents. Archived local copies are not part of this repository.

## CSV records

**Common Format and MIME Type for Comma-Separated Values (CSV) Files.** Y. Shafranovich, IETF, RFC 4180. https://www.rfc-editor.org/rfc/rfc4180

The record parser keeps spaces, accepts a quoted delimiter and an embedded break, and treats a doubled quote as one quote. A trailing delimiter quarantines that row instead of appending an empty field, and parsing continues. A quote inside an unquoted field, or text between a closing quote and the next delimiter, is `bad_quote` for that row. Charset and header presence live on the writer dialect because the bytes do not carry them. `tests/test_records.py` covers spaces, quotes, the embedded break, text after a closing quote, the trailing delimiter, two headers that land on one column, and a header-absent file that does not consume the first data row.

## Typed tabular cells

**Model for Tabular Data and Metadata on the Web.** W3C Recommendation, 17 December 2015. https://www.w3.org/TR/tabular-data-model/

Each loaded cell keeps `string_value`, a typed `value`, and a list of errors. Headers match a column name, an alias, or a title, including two header rows after `skip_rows`. Decimal and group characters and a date pattern are column metadata, so `12,50` and `10/18/2010` parse without becoming dialect evidence. Null tokens apply before the type parse. A string column with `max_length` records `length_error` on a longer value and keeps the string, which is the W3C length-constraint behavior.

The W3C model also fills a default from an empty cell. This lab follows the Avro rule instead: a default fills a column the writer omitted, and a present empty string stays empty. `tests/test_cells.py` and `tests/test_records.py` cover both the typed cell and the header mapping. The omission rule is in `tests/test_pipeline.py`.

## Reader and writer resolution

**Apache Avro 1.12.0 Specification.** Apache Software Foundation. https://avro.apache.org/docs/1.12.0/specification/

Resolution matches a reader field to a writer field by name, then by reader alias, then by a rename equivalence class. Writer fields with no reader field are ignored. A reader field with no writer field uses its default or fails. Promotions in this catalog are `integer` to `long`, `double`, or `decimal`, and `long` to `double`. The first three and `long` to `double` are on the Avro list; `integer` to `decimal` is a lab addition. `decimal` to `integer` is rejected at registration.

The parse fingerprint is the SHA-256 of a canonical form that keeps name, type, and size. Aliases, defaults, and publication stay in a second resolution fingerprint, so a `set_default` does not look like a structural change. `tests/test_registry.py` checks the two fingerprints, the alias load of `org_name` into `organization`, and the rejected narrowing. `tests/test_pipeline.py` checks that `long` value `9007199254740993` becomes `9007199254740992` with `precision_loss`, and that `9007199254740995` becomes `9007199254740996`, while the original digits are retained.

## Transitive compatibility

**Schema Evolution and Compatibility for Schema Registry on Confluent Platform.** Confluent documentation. https://docs.confluent.io/platform/current/schema-registry/fundamentals/schema-evolution.html

Registration requires full compatibility with every retained edition, which is the FULL_TRANSITIVE reading. A required `email` with no default, then the same column with a default, then a drop, is accepted as a neighbor pair on each step and rejected against the first edition. The registry therefore refuses the drop and leaves the current edition unchanged. The forward half of this check departs from Avro and Confluent: an older reader with the narrower type is allowed to read a widened writer, so `integer` to `long` registers. Avro FULL would reject that change. The lab accepts it because every CSV field is text, and an out-of-range value would be a `type_error` cell in the older reader rather than an unreadable file. Narrowing still fails. The email fixture, the widening fixture, and the narrowing fixture are in `tests/test_registry.py`.

## Publication ladder

**Online, Asynchronous Schema Change in F1.** Ian Rae, Eric Rollins, Jeff Shute, Sukhdeep Sodhi, and Radek Vingralek. PVLDB 6. https://www.vldb.org/pvldb/vol6/p1045-rae.pdf

A required column is added `absent` → `delete_only` → `write_only` → backfill → `public`, one state per edition. The drop runs that ladder backwards. Stored values are deleted only by `reorganize_drop` while the column is `delete_only`, and the absent edition is refused until that reorg has run. A reader more than one edition ahead of the writer returns `schema_too_old` and no rows. An optional column uses `absent` → `delete_only` → `public` and does not need a backfill.

The lab copies that state machine into one process. It does not implement F1's distributed leases. `tests/test_registry.py` walks the required chain, including the rejected jump to `public` and the rejected reorg during `write_only`. The demo's `bin_code` add stops at `delete_only`, which is why the next edition-1 ingest is too old.

## Schema operators

**Graceful Database Schema Evolution: the PRISM Workbench.** Carlo A. Curino, Hyun J. Moon, and Carlo Zaniolo. PVLDB 1. https://www.vldb.org/pvldb/vol1/1453939.pdf

The operator log is `rename_column`, `add_column`, `drop_column`, `set_default`, `set_type`, `merge_columns`, and `split_column`. Rename is not drop plus add. Drop plus add without a transform, a merge without provenance, and a split without a shared key or provenance are `not_information_preserving`. A pure drop is information-preserving only for the publication step `delete_only` → `absent`. `rewrite_names` returns the current name followed by the historical names a `UNION ALL` over older batches would have to read. It is a name list for that rewrite; the lab does not generate or execute SQL. `tests/test_registry.py` covers those operator results and the rewrite of `organization`.

A missed public name that is neither a live column, nor a reader alias, nor an explicit drop is `missed_alias`. The rename equivalence class lets old files resolve. The alias is still required at registration, so a default on the new name cannot hide the lost header.

## Normalization

**UAX #15: Unicode Normalization Forms.** Unicode Consortium. https://www.unicode.org/reports/tr15/

Stored strings use NFC. Header match keys and brand block keys use NFKC. When NFC and NFKC differ, the cell is flagged `compatibility_fold`, and when the lengths also differ the raw length is reported beside the NFKC length. The linker does not auto-link a pair that is only that fold, and the pipeline does not store that row as a new part. Casefold is a separate linker step and is not part of UAX #15.

The implementation calls `unicodedata` in the standard library. On the development interpreter that data is Unicode 13.0.0, so this is not a claim that Unicode 18.0.0 `NormalizationTest.txt` was executed. The folds the tests pin down are stable across that gap: U+FB01 to `fi`, U+3300 to `アパート`, U+FF21 staying fullwidth under NFC and becoming `A` under NFKC, and U+FF71 becoming U+30A2. See `tests/test_normalize.py` and the ligature row in `tests/test_pipeline.py`.

## Charset

**Encoding Standard.** WHATWG. https://encoding.spec.whatwg.org/

BOM sniffing follows the standard order: UTF-8 (`EF BB BF`), then UTF-16BE (`FE FF`), then UTF-16LE (`FF FE`). A BOM overrides the declared label. The lab does not implement the whole encoding specification. It decodes a closed label set with strict errors, refuses to guess `windows-1252` when the charset is missing, accepts that label when it is declared, and treats `utf-16` without a BOM as fatal because the endianness is unknown. Replacement decoding is what the tests show the standard library would do, and what `decode_bytes` refuses. `tests/test_decode.py` covers the BOM, the fatal byte, CP932, and both `windows-1252` cases.

## Dialect search

**Wrangling Messy CSV Files by Detecting Row and Type Patterns.** Gerrit J. J. van den Burg, Alfredo Nazábal, and Charles Sutton. arXiv:1811.11242. https://arxiv.org/abs/1811.11242

The search score is Q = P · T. P is the average, over distinct row lengths, of `(count × max(α, length − 1) / length)` with α = 10⁻³. T is the fraction of cells that match a small type grammar, floored at β = 10⁻¹⁰. A candidate whose pattern score is already below the best Q is not typed. Ties with the same cell matrix collapse to one dialect. Ties with different matrices abstain. The sniffer result is stored on the decision and is not a vote.

`tests/test_dialect.py` checks P = 10/3 for five rows of length 3, P = 7/4 for lengths {4, 4, 3, 3, 3}, and P = 3/1000 for three single-cell rows. It also checks the messy semicolon, pipe, and tab files (3 correct for the search, 0 for the sniffer), the clean files where both agree, the preamble that selects `skip_rows = 1` and a quote, the grouped-thousands file where the type score selects the quote, the `12,50` file that abstains, and the two known misses (space over comma, empty quote over a single quoted column).

## Duplicate decisions

**Duplicate Record Detection: A Survey.** Ahmed K. Elmagarmid, Panagiotis G. Ipeirotis, and Vassilios S. Verykios. IEEE Transactions on Knowledge and Data Engineering, 2007. https://archive.nyu.edu/handle/2451/27823

Exact identity is the supplier and SKU. Approximate comparison runs only after blocking, which is the survey's practical answer to comparing every pair. The title score averages Jaro, Jaro-Winkler, and Dice on token bigrams. Standardization is applied before that score and is limited to a fixed abbreviation table. Soundex is not implemented: the survey's surname figures do not transfer to part titles, and `smith` / `smyth` stays under the non-match threshold.

Thresholds are 0.92 to link and 0.78 to reject. `44 West Fourth Street` and `44 W. 4th St.` score 1. `44 West Fifth Street` scores about 0.72 and stays a non-match. `Hex bolt znc` against `Hex bolt zinc` scores about 0.82 and is `review_band`. A window of 2 on brand alone misses `Acme` / `Axme` when `Adme` sits between them; the title key still finds that pair. On the labeled sample the linker has precision 1 and recall 1, and the exact-key baseline has recall 0.25. `tests/test_link.py` records those outcomes.
