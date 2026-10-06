"""NFC for stored text, NFKC for match keys.

Case folding is not part of UAX #15. Callers that compare titles apply
casefold as a separate linker step.
"""

from __future__ import annotations

import unicodedata


def nfc(value: str) -> str:
    return unicodedata.normalize("NFC", value)


def nfkc(value: str) -> str:
    return unicodedata.normalize("NFKC", value)


def match_key(value: str) -> str:
    """Header and dedup key. Compatibility fold only; case is unchanged."""
    return nfkc(value)


def stored_form(value: str) -> str:
    return nfc(value)


def length_report(value: str) -> dict:
    folded = nfkc(value)
    raw_length = len(value)
    folded_length = len(folded)
    return {
        "raw_length": raw_length,
        "nfkc_length": folded_length,
        "lengths_differ": raw_length != folded_length,
        "compatibility_fold": nfc(value) != folded,
        "nfc": nfc(value),
        "nfkc": folded,
    }


def unidata_version() -> str:
    return unicodedata.unidata_version
