"""Closed decisions for every RFC 4180 gap this lab freezes.

A repair row is a temporary workaround: it names the end state and the
condition that removes it. The strict emitter does not consult the legacy column.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Decision:
    decision_id: str
    strict: str
    legacy: str
    end_state: str
    removal_condition: str | None
    break_date: str | None
    summary: str


def _row(
    decision_id: str,
    strict: str,
    legacy: str,
    end_state: str,
    summary: str,
    removal_condition: str | None = None,
    break_date: str | None = None,
) -> Decision:
    return Decision(
        decision_id,
        strict,
        legacy,
        end_state,
        removal_condition,
        break_date,
        summary,
    )


# legacy "repair" rows are the compatibility window. "same" means both profiles
# already share the strict outcome.
DECISIONS: dict[str, Decision] = {
    item.decision_id: item
    for item in (
        _row(
            "D-crlf",
            "crlf-only",
            "repair",
            "reject-bare-break",
            "Record separator is CRLF. A final record may omit it. A quoted field may contain CR or LF.",
            "Remove the bare-break repair when every intake file separates records with CRLF.",
            "2026-04-01",
        ),
        _row(
            "D-empty-field",
            "accept-empty-field",
            "same",
            "accept-empty-field",
            "ABNF allows an empty field, so a,b, is three fields. Section 2 item 4 of RFC 4180 disagrees; this row freezes the ABNF reading.",
        ),
        _row(
            "D-spaces",
            "keep-spaces",
            "same",
            "keep-spaces",
            "Spaces are field data. skipinitialspace stays off.",
        ),
        _row(
            "D-header",
            "caller-policy",
            "same",
            "caller-policy",
            "The caller passes header present or absent. A local file has no MIME header parameter, and sampling is not used.",
        ),
        _row(
            "D-dup-header",
            "reject",
            "repair",
            "reject",
            "RFC 4180 is silent. A repeated header name is a strict failure because one name cannot key two columns.",
            "Remove duplicate-name acceptance when every present header uses distinct names.",
            "2026-04-01",
        ),
        _row(
            "D-ragged",
            "reject",
            "repair",
            "reject",
            "Strict mode rejects a record whose field count differs from the first record. Legacy may pad or keep the extra fields only with an event.",
            "Remove padding and extra-field collection when every record has the first record's field count.",
            "2026-04-01",
        ),
        _row(
            "D-quotes",
            "doubled-quote-only",
            "repair",
            "doubled-quote-only",
            "The only quote is the double quote and the only escape is a doubled quote. RFC 4180 TEXTDATA includes the apostrophe and the backslash, so rejecting a leading apostrophe, or a backslash before a separator, quote, or backslash, is a deliberate narrowing: the legacy reader gave those bytes a different meaning.",
            "Remove single-quote and backslash repairs when intake files use only doubled quotes.",
            "2026-04-01",
        ),
        _row(
            "D-unicode",
            "accept-scalar",
            "same",
            "accept-scalar",
            "A field may contain Unicode scalar values. That is an extension past RFC 4180 TEXTDATA, which is printable ASCII. The on-disk encoding is UTF-8.",
        ),
        _row(
            "D-comments",
            "reject",
            "repair",
            "reject",
            "A record that starts with # fails. RFC 4180 has no comment production and treats # as TEXTDATA; strict rejects it because the legacy reader dropped such records. Legacy skips that record and logs the decision.",
            "Remove the comment skip when no intake file starts a record with #.",
            "2026-04-01",
        ),
        _row(
            "D-limit",
            "reject-over-limit",
            "same",
            "reject-over-limit",
            "A field longer than the configured limit fails. The recognizer counts decoded characters and rejects the input itself.",
        ),
        _row(
            "D-empty",
            "reject-zero-records",
            "same",
            "reject-zero-records",
            "Zero data records is a failure. A header alone does not count. After a stripped BOM, an empty remainder is this failure.",
        ),
        _row(
            "D-bom",
            "keep-as-text",
            "repair",
            "do-not-strip",
            "The legacy wrapper strips one leading UTF-8 BOM and records it. Strict mode does not strip, so U+FEFF stays in the first field.",
            "Remove the BOM strip when senders stop prefixing a UTF-8 BOM.",
            "2026-04-01",
        ),
        _row(
            "D-decode",
            "fatal",
            "repair",
            "fatal",
            "Strict mode fails the input on the first ill-formed UTF-8 sequence. Legacy replacement mode emits U+FFFD and puts a following ASCII byte back.",
            "Remove replacement decoding when intake files are well-formed UTF-8.",
            "2026-04-01",
        ),
        _row(
            "D-charset",
            "closed-label-set",
            "same",
            "reject-unknown-or-non-utf8",
            "Labels outside the Encoding Standard set fail. Labels in that set that are not UTF-8 fail closed, because this window only runs the UTF-8 hooks.",
        ),
        _row(
            "D-comma",
            "comma-only",
            "same",
            "comma-only",
            "The only field separator is comma. A semicolon is field text.",
        ),
        _row(
            "D-digits",
            "keep-digit-string",
            "same",
            "keep-digit-string",
            "A numeric-looking field stays that digit string. It is not passed through a float.",
        ),
    )
}


def freeze_pairs() -> tuple[tuple[str, str, str], ...]:
    """Id, strict outcome, legacy outcome. Tests pin this tuple."""

    return tuple((item.decision_id, item.strict, item.legacy) for item in DECISIONS.values())


def require_decision(decision_id: str) -> Decision:
    try:
        return DECISIONS[decision_id]
    except KeyError as exc:
        raise KeyError(decision_id) from exc
