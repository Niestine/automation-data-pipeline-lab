"""Historical defects locked in by regression tests.

Each entry is a real failure mode found in weekly catalog maintenance. The
pipeline logs ``bug_guard`` with the id when it handles the dangerous input
shape. ``regression_test`` names the unittest method that fails if the fix
regresses.
"""

from __future__ import annotations

from .models import Bug

BUGS: tuple[Bug, ...] = (
    Bug(
        id="BUG-001",
        title="Shift-JIS supplier CSV decoded as UTF-8",
        layer="decode",
        symptom="Japanese titles became mojibake or the whole feed failed to decode.",
        fix="Sniff BOM, UTF-8, then cp932 when the payload looks Japanese; never guess cp932 for latin-1 bytes.",
        regression_test="test_bug_001_shift_jis_not_utf8",
    ),
    Bug(
        id="BUG-002",
        title="Naive split on comma broke quoted titles",
        layer="parse",
        symptom="A product named 'Widget, Gamma' became two columns and shifted price/stock.",
        fix="Parse CSV with csv.reader and a sniffed delimiter; reject rows with more cells than the header.",
        regression_test="test_bug_002_quoted_comma_in_title",
    ),
    Bug(
        id="BUG-003",
        title="IEEE-754 float cents",
        layer="money",
        symptom="float('19.99') * 100 stored 1998 cents and weekly price diffs fired on unchanged goods.",
        fix="Parse with Decimal and store integer minor units.",
        regression_test="test_bug_003_float_price_rounding",
    ),
    Bug(
        id="BUG-004",
        title="Week window used local time and an inclusive end",
        layer="window",
        symptom="Sunday 16:00 UTC became Monday in JST and landed in the next ISO week; Monday 00:00 was counted twice.",
        fix="ISO-8601 weeks in UTC, half-open [start, end).",
        regression_test="test_bug_004_utc_iso_week_not_local",
    ),
    Bug(
        id="BUG-005",
        title="Empty qty became stock 0",
        layer="compat",
        symptom="Suppliers omitting the quantity column zeroed on-hand inventory.",
        fix="Empty/missing qty means unchanged; explicit 0 is out of stock. Inserts default to 0 without touching existing rows.",
        regression_test="test_bug_005_empty_qty_does_not_zero_stock",
    ),
    Bug(
        id="BUG-006",
        title="SKU identity was case-sensitive",
        layer="compat",
        symptom="abc-1 and ABC-1 were two catalog rows for one supplier SKU.",
        fix="Canonicalize SKUs to uppercase before identity checks.",
        regression_test="test_bug_006_sku_case_fold",
    ),
    Bug(
        id="BUG-007",
        title="String 'false' was truthy",
        layer="compat",
        symptom="bool('false') is True, so discontinued rows stayed active.",
        fix="Parse true/false/1/0/yes/no tokens; JSON booleans stay booleans.",
        regression_test="test_bug_007_string_false_is_inactive",
    ),
    Bug(
        id="BUG-008",
        title="SKU truncated at 12 characters",
        layer="compat",
        symptom="WIDGET-LONG-A and WIDGET-LONG-B collided as WIDGET-LONG.",
        fix="Allow 3-32 characters and reject longer SKUs instead of slicing.",
        regression_test="test_bug_008_sku_not_truncated",
    ),
    Bug(
        id="BUG-009",
        title="Older snapshot overwrote a newer catalog row",
        layer="apply",
        symptom="A delayed v1 dump replaced a newer v2 price because last-write-wins ignored updated_at.",
        fix="Skip stale timestamps; same timestamp + different body is a conflict.",
        regression_test="test_bug_009_stale_snapshot_not_applied",
    ),
    Bug(
        id="BUG-010",
        title="Dry-run wrote the catalog",
        layer="apply",
        symptom="Operators previewing a feed mutated durable state.",
        fix="Dry-run mutates an overlay and discards it; ledger and checkpoints stay untouched.",
        regression_test="test_bug_010_dry_run_does_not_persist",
    ),
    Bug(
        id="BUG-011",
        title="Quoted CRLF split a single CSV record",
        layer="parse",
        symptom="A quoted title containing a newline absorbed the next product row.",
        fix="Let csv.reader consume quoted newlines and collapse them in the title.",
        regression_test="test_bug_011_quoted_newline_in_title",
    ),
    Bug(
        id="BUG-012",
        title="EU decimal comma parsed as thousands",
        layer="money",
        symptom="'14,90' on a semicolon CSV became 1490 euros instead of 14.90.",
        fix="When the delimiter is semicolon, treat comma as the decimal mark.",
        regression_test="test_bug_012_eu_decimal_comma",
    ),
    Bug(
        id="BUG-013",
        title="Relative image paths stored verbatim",
        layer="validate",
        symptom="Downstream tools fetched './img.jpg' and failed; whitespace around URLs survived.",
        fix="Strip, require absolute http(s), and reject relative paths.",
        regression_test="test_bug_013_relative_image_rejected",
    ),
    Bug(
        id="BUG-014",
        title="Re-importing a weekly feed added stock",
        layer="apply",
        symptom="The same dump applied twice doubled on-hand units.",
        fix="Stock is an absolute snapshot. Feed sha256 + week id is an idempotency key.",
        regression_test="test_bug_014_reimport_does_not_double_stock",
    ),
    Bug(
        id="BUG-015",
        title="Slash dates were parsed as US order",
        layer="window",
        symptom="'07/01/2026' from an EU supplier became July 1.",
        fix="Accept only ISO-8601 dates; slash forms are rejected.",
        regression_test="test_bug_015_slash_date_rejected",
    ),
    Bug(
        id="BUG-016",
        title="Latin-1 0xE9 decoded as cp932",
        layer="decode",
        symptom="Café in a Western-European dump became a CJK character after the UTF-8 fallback.",
        fix="Use cp932 only when the decoded text contains Hiragana, Katakana, or CJK.",
        regression_test="test_bug_016_latin1_not_cp932",
    ),
)


def bug_ids() -> tuple[str, ...]:
    return tuple(item.id for item in BUGS)


def bug_by_id(bug_id: str) -> Bug:
    for item in BUGS:
        if item.id == bug_id:
            return item
    raise KeyError(bug_id)
