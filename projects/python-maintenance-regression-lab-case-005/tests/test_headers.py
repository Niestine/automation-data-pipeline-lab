"""Deprecation and Sunset use different grammars and different clocks."""

from __future__ import annotations

import unittest
from datetime import datetime, timezone

import helpers  # noqa: F401

from contract_lab.errors import HeaderGrammarError
from contract_lab.http_lifecycle import (
    clock_order_fault,
    deprecation_phase,
    interpret_headers,
    parse_deprecation,
    parse_link,
    parse_sunset,
    sunset_phase,
)

RFC_DEPRECATION = "@1688169599"
RFC_SUNSET_GMT = "Sat, 31 Dec 2018 23:59:59 GMT"
RFC_SUNSET_UTC = "Sun, 30 Jun 2024 23:59:59 UTC"
NOW = datetime(2024, 6, 1, tzinfo=timezone.utc)


class HeaderGrammarTests(unittest.TestCase):
    def test_deprecation_oracle(self) -> None:
        parsed = parse_deprecation(RFC_DEPRECATION)
        self.assertEqual(parsed, datetime(2023, 6, 30, 23, 59, 59, tzinfo=timezone.utc))
        self.assertEqual(deprecation_phase(parsed, NOW), "already_deprecated")

    def test_sunset_oracles_including_the_mismatched_weekday(self) -> None:
        # 31 December 2018 was a Monday. The RFC 8594 example says Saturday.
        gmt = parse_sunset(RFC_SUNSET_GMT)
        utc = parse_sunset(RFC_SUNSET_UTC)
        self.assertEqual(gmt, datetime(2018, 12, 31, 23, 59, 59, tzinfo=timezone.utc))
        self.assertEqual(utc, datetime(2024, 6, 30, 23, 59, 59, tzinfo=timezone.utc))
        self.assertEqual(sunset_phase(gmt, NOW), "elapsed")
        self.assertEqual(sunset_phase(utc, NOW), "announced")

    def test_each_grammar_rejects_the_other(self) -> None:
        with self.assertRaises(HeaderGrammarError):
            parse_deprecation(RFC_SUNSET_UTC)
        with self.assertRaises(HeaderGrammarError):
            parse_deprecation(RFC_SUNSET_GMT)
        with self.assertRaises(HeaderGrammarError):
            parse_sunset(RFC_DEPRECATION)

    def test_rfc_pair_is_ordered_and_equal_clocks_are_clean(self) -> None:
        deprecation = parse_deprecation(RFC_DEPRECATION)
        sunset = parse_sunset(RFC_SUNSET_UTC)
        self.assertFalse(clock_order_fault(deprecation, sunset))
        same = parse_sunset("Fri, 30 Jun 2023 23:59:59 GMT")
        self.assertEqual(deprecation, same)
        self.assertFalse(clock_order_fault(deprecation, same))

    def test_reversed_clocks_are_a_distinct_fault(self) -> None:
        later = parse_deprecation("@1719791999")
        earlier = parse_sunset("Fri, 30 Jun 2023 23:59:59 GMT")
        self.assertEqual(later, datetime(2024, 6, 30, 23, 59, 59, tzinfo=timezone.utc))
        self.assertTrue(clock_order_fault(later, earlier))

    def test_future_deprecation_is_scheduled(self) -> None:
        instant = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.assertEqual(deprecation_phase(instant, NOW), "scheduled")

    def test_link_rel_is_recorded_and_not_fetched(self) -> None:
        links = parse_link(
            '<https://developer.example.test/deprecation>; rel="deprecation"; type="text/html"'
        )
        self.assertEqual(links[0]["target"], "https://developer.example.test/deprecation")
        self.assertEqual(links[0]["rel"], "deprecation")
        facts = interpret_headers(
            {"Link": '<https://developer.example.test/deprecation>; rel="deprecation"'},
            NOW,
        )
        self.assertEqual(facts.phase, "not_deprecated")
        self.assertEqual(facts.policy_targets, ["https://developer.example.test/deprecation"])
        self.assertIsNone(facts.deprecation_at)

    def test_illegal_present_header_fails_closed(self) -> None:
        with self.assertRaises(HeaderGrammarError) as raised:
            interpret_headers({"Deprecation": "tomorrow"}, NOW)
        self.assertEqual(raised.exception.header, "Deprecation")
        with self.assertRaises(HeaderGrammarError):
            interpret_headers({"Sunset": "@1688169599"}, NOW)
        with self.assertRaises(HeaderGrammarError):
            parse_link("https://developer.example.test/deprecation")

    def test_deprecation_integer_range_fails_as_grammar_not_overflow(self) -> None:
        self.assertEqual(
            parse_deprecation("@-1"),
            datetime(1969, 12, 31, 23, 59, 59, tzinfo=timezone.utc),
        )
        with self.assertRaises(HeaderGrammarError):
            parse_deprecation("@999999999999999")
        with self.assertRaises(HeaderGrammarError):
            parse_deprecation("@1234567890123456")

    def test_quote_inside_link_target_does_not_swallow_the_separator(self) -> None:
        links = parse_link('<https://a.example.test/x"y>; rel="deprecation", <https://b.example.test/>; rel="alternate"')
        self.assertEqual([link["rel"] for link in links], ["deprecation", "alternate"])
        self.assertEqual(links[0]["target"], 'https://a.example.test/x"y')
