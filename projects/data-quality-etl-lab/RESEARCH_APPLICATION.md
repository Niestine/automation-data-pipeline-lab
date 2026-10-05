# Research application

Harbor Ledger uses nine public sources. Each technique below is implemented in `src/harbor_ledger/` and locked by the offline unittest suite. Synthetic precision, recall, and F measure whether the seeded generator and the detectors agree. They are a soundness check on this fixture, which is the use a cell-oracle study allows, and they are not field recall.

## Closed CSV profile

RFC 4180 supplies the writer and reader rules: a header, CRLF records, a trailing break, doubled quotes inside quoted fields, and quotes only when a field contains a comma, quote, or line break. The reader also accepts LF and a missing final break. `source_row` counts every physical record, including comments and blank lines. A short record does not steal columns from the next record.

Tests write the same bytes from two processes, round-trip a quoted comma and an embedded line break, and keep spaces unquoted when the schema leaves trim off.

## Encoding allow-list

The WHATWG Encoding Standard supplies BOM-first decoding and replacement mode. A leading BOM wins over the declared label and is consumed. The labels `ascii`, `latin1`, and `iso-8859-1` select windows-1252, so byte `0x80` becomes U+20AC. An illegal Shift_JIS pair is decoded in replacement mode, which leaves a following ASCII quote visible. Supplier text is quarantined when the U+FFFD count exceeds the profile threshold; an equal count is accepted. Pipeline-owned files are strict UTF-8, and a BOM on those files is fatal. Every artifact this pipeline writes is UTF-8. The HTML error mode is unused, because numeric character references would look like supplier data.

## Normalization forms

UAX #15 separates NFC from NFKC. Parsed strings are stored in NFC. NFKC runs only on fields marked `identifier_fold`, and only as an annotation (`before` / `after`). The published cell keeps the NFC text, so a fullwidth style code remains fullwidth in `clean.csv` while the annotation records the ASCII fold. Tests cover NFC idempotence, a circled digit, the fi ligature, and that one-row case.

## Table metadata and constraints

The W3C tabular-data model supplies the dialect: header presence, comment prefix, skipped blank rows, and trim as an explicit flag. Frictionless Table Schema supplies the constraint subset actually loaded: `missingValues` before casting, `decimalChar` / `groupChar` / `bareNumber`, boolean token lists, one date format (`YYYY-MM-DD` or a single `strptime` pattern), and `pattern` as a full-string match. `format: any` is rejected at schema load. Primary-key fields are required. Minimum and maximum are integers or decimal strings.

A cast failure and a missing token stay distinct. `NA` is null only when the schema lists it. `yes` is a boolean type error under the default token lists. A grouped number such as `1,234.5` parses only when `groupChar` is declared and the CSV quotes the field.

## Detectability and repairability

Arocena, Glavic, Mecca, Miller, Papotti, and Santoro (BART, PVLDB 2015) treat error placement as a search for detectable cells. This lab keeps a finite greedy list. A cell changes at most once. A candidate that makes an earlier detectable error undetectable is rejected. Immutable columns and primary-key columns on the random path are skipped. A quota the pass cannot fill is a shortfall, and `run_injection` reports exit code 0. A fixture that already violates a denial rule makes `run_injection` report exit code 2.

The denial subset is not-null, unique, primary key, one binary functional dependency, one constant conditional rule, and a numeric range. Repairability is the share of the clean value in that rule's candidate bag. A constant rule scores 1. An open numeric range scores 0. Not-null, unique, and primary-key breaks score 0, because this subset has no context bag that holds the clean value. Several rules keep the maximum. The five-row department bag Staff, Staff, Sales, Mktg, Mktg scores `0.2` for clean Sales. A type-valid salary that stays inside the declared range is undetectable. The same seed repeats the same oracle. The injector is a library used by tests. `run_lab.py` does not call it, and the supplier manifest records `injector_shortfall: null`.

## Detector union and per-class scores

Abedjan, Chu, Deng, Fernandez, Ilyas, Ouzzani, Papotti, Stonebraker, and Tang (PVLDB 2016) compare read-only detectors and warn that a single F hides a tool that cannot see a class. This lab runs four pure detectors, in profile order:

| Detector | Classes it may cover |
| --- | --- |
| `pattern_type` | `bogus`, `typo` |
| `constraint` | `constraint_break`, `missing` |
| `outlier` | `outlier` |
| `duplicate` | `duplicated_value` |

The sealed finding list is their union. `min_k_filter` is present so a test can show a singleton pattern hit leaving a k=2 slice; the pipeline never calls it. With `repair_between_detectors` false, swapping the order leaves the finding signatures unchanged. The flagged repair-between path blanks `bogus` cells before the next detector, which turns a pattern failure into a required-value miss. The published profile leaves that path off.

Modified z-scores use `0.6745 * |value - median| / MAD`, and a column with fewer than `minimum_rows` non-null decimals is skipped. On the sample 10, 12, 11, 13, 10, 500, threshold 3.5 flags only 500 and threshold 0.1 flags every value. `suggest_outlier_threshold` is a training diagnostic. The sealed run keeps the profile threshold. `estimate_m` and `estimate_u` exist for a later refit; with `dedup.refit` false the pipeline does not call them. A test replaces all three with a function that raises, and the week drop still seals.

When an oracle is supplied, each slice (`evaluation`, `training`) stores per-detector, per-class precision, recall, F, and an upper-bound recall. Counts are per cell, so two findings from one detector on one cell are one prediction. Every parsed row is in the evaluation slice unless the oracle marks it `train`, so a finding on a row the oracle calls clean is a false positive. There is no headline F. Upper-bound recall is 1 when that detector's class list contains the class and the slice has oracle cells of that class; otherwise it equals the measured recall, so the bound stays at or above the recall.

## Duplicate decisions

Elmagarmid, Ipeirotis, and Verykios (IEEE TKDE 2007) separate standardization, blocking, and field comparison. Comparison strings are NFC, casefolded, split into runs of Unicode letters and digits, and passed through a small abbreviation map, so `44 W. 4th St.` and `44 West Fourth Street` share one form. Each blocking key is sorted, and a new row is compared only with the previous `w - 1` rows. A second key can recover a pair the first window missed. Rows are paired as physical records, and two rows that repeat a primary key are left to the primary-key check rather than scored as a link.

Short fields agree on exact match, on Jaro-Winkler at the profile cutoff (prefix scale 0.1, prefix length at most 4), or on a bounded Levenshtein distance. Token fields agree on cosine of character q-grams computed inside each atom, or on Monge-Elkan (atoms match when equal or when one is a prefix of the other, and the score divides by the average atom count). Whole-string q-grams of `navy wool coat` and `coat wool navy` score 0.5; the per-atom grams score above 0.999, and Monge-Elkan scores 1. Soundex is absent.

Field weights are Fellegi-Sunter log ratios, `log2(m/u)` on agreement and `log2((1-m)/(1-u))` on disagreement. A null skips the field. The profile's m and u stay frozen. Two thresholds produce `link`, `possible`, and `non-link`. Union-find receives link edges only, so links A-B and B-C form one cluster while a possible-link cannot bridge two clusters. The demo profile sets `jaro_agree` to 0.99 and `edit_agree` to 0, so `KT300` and `KT310` do not agree. A separate profile with edit distance 1 links that one-character typo. Published weights are quantized to six decimal places, half-even. The week-drop links publish `13.047209`.

## Canonical digest

RFC 8785 (JCS) defines the hashed form of the anomaly report. Object members sort by UTF-16 code units. The property-order vector `\r`, `1`, U+0080, U+00F6, U+20AC, U+1F600, U+FB33 places the emoji before U+FB33; Python's code-point sort does the opposite. Strings escape controls as lowercase `\u00hh`, with the short forms `\b`, `\t`, `\n`, `\f`, and `\r`. The encoder rejects lone surrogates, duplicate keys (in an object built in code and in parsed JSON text), non-finite floats, every other float including `2.0`, and integers outside the IEEE-754 exact range. Money stays a decimal string (`86.00`, `910.00`, and the rejection fixture `26000.33`). Report keys are ASCII. The digest is SHA-256 of the UTF-8 canonical bytes. `report.json` is a pretty view and is not the hashed artifact. Whitespace and key order in a parsed JSON document do not survive `canonicalize_json`.

## Sources

1. Patricia C. Arocena, Boris Glavic, Giansalvatore Mecca, Renée J. Miller, Paolo Papotti, and Donatello Santoro. "Messing Up with BART: Error Generation for Evaluating Data-Cleaning Algorithms." PVLDB 9(2), 2015. <https://www.vldb.org/pvldb/vol9/p36-arocena.pdf>
2. Ziawasch Abedjan, Xu Chu, Dong Deng, Raul Castro Fernandez, Ihab F. Ilyas, Mourad Ouzzani, Paolo Papotti, Michael Stonebraker, and Nan Tang. "Detecting Data Errors: Where are we and what needs to be done?" PVLDB 9(12), 2016. <https://www.vldb.org/pvldb/vol9/p993-abedjan.pdf>
3. Ahmed K. Elmagarmid, Panagiotis G. Ipeirotis, and Vassilios S. Verykios. "Duplicate Record Detection: A Survey." IEEE Transactions on Knowledge and Data Engineering, 2007. <https://www.cs.purdue.edu/homes/ake/pub/TKDE-0240-0605-1.pdf>
4. Y. Shafranovich. "Common Format and MIME Type for Comma-Separated Values (CSV) Files." RFC 4180. <https://www.rfc-editor.org/rfc/rfc4180>
5. W3C. "Model for Tabular Data and Metadata on the Web." <https://www.w3.org/TR/tabular-data-model/>
6. Ken Whistler. "Unicode Standard Annex #15: Unicode Normalization Forms." <https://www.unicode.org/reports/tr15/tr15-58.html>
7. WHATWG. "Encoding Standard." <https://encoding.spec.whatwg.org/>
8. Frictionless Data. "Table Schema." <https://specs.frictionlessdata.io/table-schema/>
9. A. Rundgren, B. Jordan, and S. Erdtman. "JSON Canonicalization Scheme (JCS)." RFC 8785. <https://www.rfc-editor.org/rfc/rfc8785>
