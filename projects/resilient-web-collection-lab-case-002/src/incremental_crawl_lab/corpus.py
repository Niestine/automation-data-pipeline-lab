"""Synthetic catalog pages. Tokens are stable inputs for simhash calibration."""

from __future__ import annotations

_PREFIX = ("harborline", "indigo", "wool", "coat", "ledger", "stockroom", "selvage")
_TOKENS = tuple(f"token{i:03d}" for i in range(1, 61))
_BASE_WORDS = _PREFIX + _TOKENS
_REPLACEMENT = ("harborline", "replacement", "copy") + tuple(
    f"zeta{i:03d}" for i in range(1, 61)
)


def _page(words: tuple[str, ...]) -> str:
    return "<html><body><p>" + " ".join(words) + "</p></body></html>"


BASE = _page(_BASE_WORDS)
TIMESTAMP_EDIT = _page(_BASE_WORDS + ("stamp", "20261005", "1800"))
AD_EDIT = _page(_BASE_WORDS + ("sponsored", "banner", "click", "offer"))
PARAGRAPH_EDIT = _page(_REPLACEMENT)

NOT_FOUND = (
    "<html><body><p>not found the requested catalog page is missing "
    "from this fixture</p></body></html>"
)

INDEX = """<html><body>
<a href="/catalog/wool-coat">wool coat</a>
<a href="/catalog/linen-shirt">linen shirt</a>
<a href="/catalog/field-notes">field notes</a>
<a href="/private/cost">private cost</a>
<a href="/secret">secret</a>
<a href="https://evil.example/phish">phish</a>
</body></html>
"""

LINEN = "<html><body><p>Caf\u00e9 linen shirt aisle</p></body></html>"

ROLES = {
    "index": INDEX,
    "base": BASE,
    "timestamp": TIMESTAMP_EDIT,
    "ad": AD_EDIT,
    "paragraph": PARAGRAPH_EDIT,
    "linen": LINEN,
    "not_found": NOT_FOUND,
}
