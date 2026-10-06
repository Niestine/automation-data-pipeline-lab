# Research application

Case 010 hardens a synthetic wharf intake sheet. The ten sources below are the ones the package and the tests actually use. Titles and URLs are the public documents. Figures quoted from a paper describe that paper's systems, and they are not a score this lab is trying to hit.

## Recognizer, then handler

Sergey Bratus, Lars Hermerschmidt, Sven M. Hallberg, Michael E. Locasto, Falcon D. Momot, Meredith L. Patterson, and Anna Shubina, "Curing the Vulnerable Parser: Design Patterns for Secure Input Handling", USENIX ;login:, 2017.  
https://www.usenix.org/system/files/login/articles/login_spring17_08_bratus.pdf

The hardened path is a recognizer. `hardened_parse` accepts the whole byte string or returns a typed failure. The optional handler receives the data-record tuple only after that acceptance. A quoted field that contains a comma and a CRLF is one record. An unclosed quote never calls the handler. `unparse` is the only writer, and a strict success parsed again after unparsing keeps the same header and records with an empty event tuple. The closed decision ids are the error taxonomy for this window.

Tests: `tests/test_corpus.py`, `tests/test_roundtrip.py`, `tests/test_decode.py`.

## RFC 4180 grammar, with the empty-field prose frozen

Yakov Shafranovich, "Common Format and MIME Type for Comma-Separated Values (CSV) Files", RFC 4180.  
https://www.rfc-editor.org/rfc/rfc4180.html

The strict profile uses comma, CRLF, optional final CRLF, doubled quotes as the only escape, and spaces as data. A quoted field may contain a comma, CR, or LF. The header is caller policy (`present` or `absent`), because a local file has no MIME header parameter. `a,b,` is three fields: the ABNF allows an empty field, and that reading is frozen as `D-empty-field` even though the RFC's prose example differs. Unicode scalar values are an explicit extension past the RFC's printable-ASCII TEXTDATA, stored as `D-unicode`. The strict profile is also deliberately narrower than the RFC in three places: a leading `'`, a backslash before a separator, quote, or backslash, and a record that starts with `#` are all legal TEXTDATA in RFC 4180, but strict mode rejects them because the legacy reader gave those bytes a different meaning. A bare CR and a bare LF between records are rejected with separate codes.

Tests: `tests/test_contract.py` (quoted newline uses the logical record index), corpus files `y_quote_comma_crlf.bin` (a quoted field holding both a comma and a CRLF), `i_field_empty_trailing.bin`, `y_field_spaces.bin`, `i_text_unicode.bin`, `n_record_bare_lf.bin`, `n_record_bare_cr.bin`.

## Temporary workarounds with an end state

Martin Thomson and David Schinazi, "Maintaining Robust Protocols", RFC 9413.  
https://www.rfc-editor.org/rfc/rfc9413.html

Each legacy repair is a row in `decisions.py` with `end_state`, `removal_condition`, and `break_date`. The strict emitter does not consult the legacy column. A `b_` sidecar records both outcomes and must copy that removal text. Silent repair is not allowed: a legacy success that changed the input carries a `RepairEvent`, and each repeated header name gets its own event. An event that names an id outside the decision table is a corpus problem, whether it is stored in a sidecar or emitted by a parser.

Tests: `tests/test_corpus.py` checks that every `b_` file still disagrees in the recorded way and that removal text matches the row, and shows the drift and unknown-id gates firing on a deliberately broken copy of the corpus. `tests/test_contract.py` checks one event per repeated header name.

## Bucketed regression corpus

Nicolas Seriot, "Parsing JSON is a Minefield".  
https://seriot.ch/parsing_json.php

The corpus uses Seriot's three buckets plus two maintenance buckets. `y_` must accept, `n_` must reject, `i_` is a frozen local choice, `c_` means the two profiles return the same records and is marked `conformance_oracle: false`, and `b_` is a dated break. One file is one decision. Two files that share a result signature must not have one byte string as a proper prefix of the other; every pair with the same signature is compared, not just each file against the first one seen. Every error code the recognizer or decoder can return must appear in a strict corpus outcome. Agreement between the two parsers is compatibility, and the runner does not rewrite a sidecar from whichever parser happened to run.

Tests: `tests/test_corpus.py`.

## UTF-8 decode hooks and the closed label set

WHATWG, "Encoding Standard".  
https://encoding.spec.whatwg.org/

`decode.py` keeps the Encoding Standard label table. ASCII whitespace is stripped and the match is ASCII-case-insensitive: a label with a non-ASCII character never matches, so `str.lower` folding KELVIN SIGN to `k` cannot turn it into `koi8-r`. An unknown label such as `utf8-bom` is `E-charset`. A known label that is not a UTF-8 label, including `windows-1252` and the labels the standard maps onto it (`ascii`, `latin1`), is `E-not-utf8`. Strict mode follows "UTF-8 decode without BOM or fail": the first ill-formed sequence fails the input and the recognizer is not called. Legacy mode follows "UTF-8 decode": one leading `EF BB BF` is removed and `bom_stripped` is set; replacement emits U+FFFD and puts a following ASCII byte back, so `0x2C` and `0x22` stay delimiters. An incomplete `EF BB` is not a BOM. The gb18030 end-of-queue step is outside this window.

Tests: `tests/test_decode.py`, corpus files `b_decode_ff_comma.bin`, `b_decode_efbb_comma.bin`, `b_bom_leading.bin`, `n_charset_utf8_bom.bin`, `n_charset_windows_1252.bin`.

## Both parsers stay callable, and the accept set is pinned

Guido van Rossum, Pablo Galindo Salgado, and Lysandros Nikolaou, "PEP 617 – New PEG parser for CPython".  
https://peps.python.org/pep-0617/

The migration shape is the same idea at package scale. `legacy_parse` and `hardened_parse` are both callable. The public name `parse` is the hardened function object. The strict accept set does not grow inside the compatibility window. `freeze_pairs()` is duplicated as a literal in `tests/test_contract.py`, so editing a decision row fails until the pin is edited in the same change. A code change that widens acceptance is caught by the `n_` and `b_` files rather than by the pin, and only for patterns those files cover. Legacy removal waits until every `c_` file passes on the hardened parser and the open `b_` rows are retired.

Tests: `tests/test_contract.py`.

## Same bytes, then minimize

William M. McKeeman, "Differential Testing for Software", Digital Technical Journal, 1998.  
https://www.cs.tufts.edu/comp/150FP/archive/bill-mckeeman/DifferentailTesting.pdf

`campaign` feeds one byte string to both parsers. A crash, a timeout, or a difference in acceptance or canonical records is a witness. The generator is one seed and a capped budget, in four layers: raw bytes, delimiter lexemes, files from the strict grammar, and those files with one byte inserted or deleted. Minimization deletes a byte, a field, or a record while the disagreement class stays the same, and it stops when a full cycle changes nothing (there is no round cap; every accepted trial is shorter). A comment seed must shrink and stay classified as `D-comments`. The campaign has no write path. `admit` refuses a write that has no known decision id and bucket, a name that is not lower-case `bucket_component_condition` for that bucket, and a name that already exists, so a sidecar is never overwritten.

Tests: `tests/test_differential.py`.

## Normalize spelling, keep field text

Jonas Möller, Felix Weißberg, Lukas Pirch, Thorsten Eisenhofer, and Konrad Rieck, "Cross-Language Differential Testing of JSON Parsers", ACM ASIA CCS, 2024. Open copy:  
https://depositonce.tu-berlin.de/items/20715750-2c32-4330-8695-15f903d11ee4

Comparison uses records, header, and failure codes. The only spelling folds are record-separator spelling and quote-style spelling. Each fold is recorded on the witness and written as an INFO line (`normalization separator`, `normalization quote-style`) on the `wharf_sheet` logger, without the bytes. Field text is unchanged, so a 17-digit field stays that digit string. Either parser is not used as the sole normalizer of its own output. Two-parser agreement can share a bug, so a `c_` sidecar is a compatibility note.

Tests: `tests/test_differential.py` (`spelling_notes`, digit-string fold, normalization log lines), corpus file `c_field_digits.bin`.

## Kill rate separate from statement coverage

René Just, Darioush Jalali, Laura Inozemtseva, Michael D. Ernst, Reid Holmes, and Gordon Fraser, "Are Mutants a Valid Substitute for Real Faults in Software Testing?", ACM SIGSOFT FSE, 2014.  
https://homes.cs.washington.edu/~mernst/pubs/mutation-effectiveness-fse2014-abstract.html

Six hand-placed mutants cover a relational swap, a boundary swap, two statement deletions, and two parser-specific gaps (semicolon treated as comma, ASCII restore dropped). None is marked equivalent. Every unmarked mutant must fail at least one corpus oracle. On the five Java systems in that study, 73% of 357 real faults coupled to mutants from commonly used operators. Of the 95 that did not, 25 needed a stronger operator, 7 needed a new operator, and 63 were not coupled to any mutant. Those counts describe those systems. This lab reports `kill_rate` and `statement_coverage` as two numbers and does not drop a case only because coverage did not move. Removing `n_quote_unclosed.bin` must leave `drop_unclosed` alive. Each mutant has one named corpus file that kills it, and the unmutated parser must pass every oracle the kill is measured against.

Tests: `tests/test_mutants.py`.

## The csv module is a contrast, not the scanner

Python Software Foundation, "csv — CSV File Reading and Writing".  
https://docs.python.org/3/library/csv.html

The recognizer does not call `csv.reader`, `Sniffer`, or `field_size_limit`. `write_sheet` opens the text file with `encoding="utf-8"` and `newline=""` so a quoted line feed is not translated again. The field limit is a count of decoded characters: length equal to the limit succeeds, and one character over fails, both in a fresh process at the stdlib default and after the stdlib limit has been raised. One test shows `csv.reader` returning two rows for a bare LF while `hardened_parse` returns `E-bare-lf`. `reader.line_num` is not used; a quoted newline that makes the next record ragged is reported at logical record index 1.

Tests: `tests/test_contract.py`, `tests/test_limit.py`, `tests/test_roundtrip.py`.
