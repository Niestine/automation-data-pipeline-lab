# Research application

Case 006 is a partner-note interchange. Foreign text is read as bytes and checked as strict UTF-8. The CSV that leaves the lab is one byte profile: UTF-8, NFC, CRLF, no leading BOM, no C1 controls. Five public sources shaped the checker, the two newline policies, the file replace, and the tests. The suite runs offline on synthetic fixtures.

## Techniques used

### 1. Locale-independent UTF-8, with the charmap signature kept visible

Sources:

- Silva, Farahat, and d'Amorim, *An Empirical Analysis of Cross-OS Portability Issues in Python Projects* (MSR 2026). <https://arxiv.org/abs/2609.25531>
- Eghbali and Pradel, *No Strings Attached: An Empirical Study of String-related Software Bugs* (ASE 2020). <https://software-lab.org/publications/ase2020.pdf>
- RFC 3629, *UTF-8, a transformation format of ISO 10646*. <https://www.rfc-editor.org/rfc/rfc3629>

`policy.py` holds `ENCODING = "utf-8"`, `ERRORS = "strict"`, and `OPEN_NEWLINE = ""`. Ingest reads with `Path.read_bytes()` and never opens text in the process locale. Emit passes those three names into the text `open` that the csv writer uses.

`tests/test_ingest.py` opens byte `0x81` with `cp1252` and expects `UnicodeDecodeError` text containing `charmap` and `0x81`. The same file through `read_foreign_text` raises `StrictUtf8Error` whose message is `strict-utf8 rejected byte 0x81 at offset 2`. The log line carries `boundary=foreign-ingest`, `codec=strict-utf8`, and `byte=0x81`, and it does not carry the surrounding payload.

The same byte string `C3 A9` is U+00E9 on the production path. Decoded as cp1252 it is U+00C3 U+00A9. Decoded as cp932 it is a different two-character string. Those contrasts are wrong-output oracles: the cp932 reading does not raise. `tests/test_locale.py` repeats the production read and the golden emit in a child process with `PYTHONUTF8=0` and `PYTHONUTF8=1`, and with `PYTHONIOENCODING` set to `cp1252` or `utf-8`. The child digest is the checked-in golden sha256 in every cell. `PYTHONUTF8=0` only reaches a legacy codec on a non-UTF-8 locale host (cp932 on the Windows host where this ran), and `PYTHONIOENCODING` only touches stdio. So `tests/test_emit.py` also patches emit's codec to `cp1252` in-process. The golden rows then raise `UnicodeEncodeError` on `Ω`, `café` alone produces bytes the strict checker rejects, and no file is left behind. That is the "remove `encoding=`" mutation, reproduced on any OS.

### 2. Foreign newlines in, CRLF only out

Sources:

- RFC 5198, *Unicode Format for Network Interchange*. <https://www.rfc-editor.org/rfc/rfc5198>
- Silva, Farahat, and d'Amorim, MSR 2026 (newline signature and normalization repair). <https://arxiv.org/abs/2609.25531>
- Eghbali and Pradel, ASE 2020 (regexes written against a single newline byte). <https://software-lab.org/publications/ase2020.pdf>

`ingest.logical_lines` splits on CRLF, then LF, then CR. It does not call `str.splitlines()`, so U+0085, U+2028, and U+2029 stay inside the line. The logical text is joined with `\n`. `tests/test_ingest.py` feeds `61 0D 0A 62 0A 63 0D 64` and expects `a`, `b`, `c`, `d`. A pattern `re.fullmatch(r"row1", line)` succeeds on the first logical line of a CRLF payload. The unfixed `split("\n")` leaves a trailing CR and the same pattern fails.

Emit uses the csv excel dialect, which already writes CRLF, and opens the temp file with `newline=""`. `tests/golden/rows.csv` is the byte oracle for `[["café", "Ω"], ["x", "y"]]`: UTF-8, no BOM, two CRLF record breaks, zero `0D 0D 0A`, sha256 `6eba8588c5f2f573229b74003b682de76fb6b18baa473d3096bcae9d4efab653`. `legacy.double_translate_newlines` is the second-translation mutant. Its output contains `0D 0D 0A` and `assert_interchange_bytes` rejects it. On Windows, a text `open` that omits `newline=""` is the same mutant against a real file and is covered by `test_text_open_without_newline_doubles_cr_on_windows`. `test_forced_windows_newline_translation_is_rejected_on_any_os` patches emit's newline to `"\r\n"`, which is what that Windows translation does. Emit then raises `ProfileError` on `CR CR LF`, logs `byte=0x0d`, and publishes nothing, so a Linux runner also catches the regression.

### 3. Strict UTF-8 before any tool-facing use

Source: RFC 3629. <https://www.rfc-editor.org/rfc/rfc3629>

`utf8strict.check_strict_utf8` walks the byte string with the section 4 ranges. Leads C0, C1, and F5–FF fail. E0, ED, F0, and F4 use the second-byte bounds that reject overlong forms and UTF-8-encoded surrogates. Truncated sequences fail on the lead that is present. Every accepted sequence must encode back to the same bytes. The walker reports the byte where a sequence stops being valid. For `ED A1 ...` that is `A1` at offset 1, not the lead. A seeded differential test feeds 5,000 short strings built from boundary bytes and requires the walker to accept exactly the strings CPython's strict `utf-8` decoder accepts.

`tests/test_utf8.py` rejects the memo's vectors: `C0 80` is not U+0000, `C0 AF` is not U+002F, and `ED A1 8C ED BE B4` is not U+233B4. A one-purpose `naive_forbidden_scalar` returns those three scalars so the test can show the unfixed reading. The checker also rejects a truncated sequence, a five-octet lead, and `F4 90 80 80`. Boundary scalars from U+0000 through U+10FFFF that are legal UTF-8 round-trip, including U+0080, which is well-formed UTF-8 and still banned later as a C1 control. A leading `EF BB BF` is well-formed U+FEFF to the checker. `assert_interchange_bytes` rejects it, and foreign ingest raises `LeadingBomError` instead of stripping it. Error handlers that would carry undecodable bytes through are absent from the library sources (`tests/test_gate.py`).

### 4. NFC emit tied to this interpreter's Unicode data

Source: RFC 5198, sections 2 and 3. <https://www.rfc-editor.org/rfc/rfc5198>

`prepare_field` rejects U+0080..U+009F, rejects text that `utf-8` / `strict` cannot encode (lone surrogates fail here), then applies `unicodedata.normalize("NFC", field)`, then rejects category `Cn`. CR and LF inside a field are rejected so the record separator stays the only line break. On an assignment failure the log line includes `codec=nfc` and `unidata_version=` plus `unicodedata.unidata_version`.

The two pairs from the RFC are fixtures. U+2126 and U+03A9 emit one file, and the record body is the NFC encoding, not the ohm-sign bytes. U+0061 U+0300 and U+00E0 do the same, and the combining grave byte `CC 80` is absent. U+0085 raises before a destination file exists. U+0378 is the Cn fixture; if a later Unicode version assigns it, that test fails on purpose and the log already names the version that made the decision.

### 5. Close the handle, then replace; casefold before create

Source: Silva, Farahat, and d'Amorim, MSR 2026. <https://arxiv.org/abs/2609.25531>

`atomic_write_bytes` builds the path with `pathlib`, rejects a casefold collision with any other directory entry, writes a sibling temp file inside a `with` block, then calls `os.replace`. The test wraps the module's `open` and `os.replace`. When replace runs, every handle that was opened must already report `closed`. Moving the replace inside the `with` block fails this test on any OS. A simulated replace failure leaves the old destination bytes and no temp file. On Windows, `legacy.replace_while_open` raises `OSError` with `winerror == 32`, and the production helper still publishes the bytes.

A directory that already contains `Readme.txt` rejects `readme.TXT`. The error names both spellings. The stored bytes stay `keep`, and the directory listing stays `Readme.txt`. The same guard sits on `emit_interchange_csv`. The check does not ask whether the current disk is case-sensitive.

### 6. Unspecified-encoding gate beside the byte oracle

Sources: Silva, Farahat, and d'Amorim (only Ruff's unspecified-encoding rule touched the encoding subcategory among the six tools they compared) and Eghbali and Pradel (a static analyzer found 1 of 204 string bugs). <https://arxiv.org/abs/2609.25531>, <https://software-lab.org/publications/ase2020.pdf>

`tests/test_gate.py` parses the library and flags a text `open` / `fdopen` or a `read_text` / `write_text` that omits `encoding`. Binary mode is allowed. The same test requires the emit text `open` to pass `encoding`, `newline`, and `errors`. The rule Silva cites is Ruff's unspecified-encoding check (<https://docs.astral.sh/ruff/rules/unspecified-encoding>). This suite does not shell out to Ruff. The byte fixtures remain the oracle: a clean gate is not treated as proof that the golden file is CRLF UTF-8.

### 7. Diagnose-only mojibake count

Source: ftfy, *Heuristics for detecting mojibake*. <https://ftfy.readthedocs.io/en/latest/heuristic.html>

`diagnose_mojibake` counts adjacent pairs. The documented shape, a lowercase accented letter immediately followed by a currency symbol, scores 1 for `é$` and for `à` plus U+20AC. A Latin-1 reading of a UTF-8 lead in C2–F4 followed by a continuation-range character scores as well, which is why the cp1252 reading of `C3 A9` is nonzero. A `?` or U+FFFD next to that lead or continuation range is reported and not turned back into a letter. The function refuses `bytes`. It has no encode step and no partial repair.

Emit's source does not name the counter, and a trace of `emit_interchange_csv` does not call it. A legal field such as `é$` is flagged and still written unchanged. A 400-line ASCII log scores 0. The suite does not fail because a long string merely might contain a hit.

## What was deliberately left out

No UTF-8 mode switch lives in the library. The child-process matrix only proves the library ignores the mode. No filesystem surrogate round-trip, no WHATWG replacement decoder, and no NFKC/NFKD path. The mojibake counter is not a charset guesser. Context-dependent failures (timing, leftover files) are not classified as encoding defects.
