"""Count mojibake-shaped pairs. This module does not rewrite text."""

from __future__ import annotations

import unicodedata


def diagnose_mojibake(text: str) -> int:
    """Return the number of unlikely adjacent pairs in ``text``.

    A count above zero flags a short field. It is not proof of mojibake,
    and the caller decides whether a long log is worth a human look.
    Bytes are refused: this counter runs on text that is already Unicode.
    """

    if not isinstance(text, str):
        raise TypeError("diagnose_mojibake expects str")
    count = 0
    for index in range(len(text) - 1):
        left = text[index]
        right = text[index + 1]
        if _accented_lower(left) and _currency(right):
            count += 1
        elif _mojibake_lead(left) and _continuation(right):
            count += 1
        elif _lossy_span(left, right):
            count += 1
    return count


def _accented_lower(char: str) -> bool:
    if unicodedata.category(char) != "Ll" or ord(char) < 0x80:
        return False
    if unicodedata.decomposition(char):
        return True
    return 0x00C0 <= ord(char) <= 0x024F


def _currency(char: str) -> bool:
    return unicodedata.category(char) == "Sc"


def _mojibake_lead(char: str) -> bool:
    """Latin-1 reading of a UTF-8 lead byte in the range C2-F4."""

    return 0xC2 <= ord(char) <= 0xF4


def _continuation(char: str) -> bool:
    return 0x80 <= ord(char) <= 0xBF


def _lossy_marker(char: str) -> bool:
    return char in ("?", "\ufffd")


def _lossy_span(left: str, right: str) -> bool:
    if _lossy_marker(left) and (_mojibake_lead(right) or _continuation(right)):
        return True
    if _lossy_marker(right) and (_mojibake_lead(left) or _continuation(left)):
        return True
    return False
