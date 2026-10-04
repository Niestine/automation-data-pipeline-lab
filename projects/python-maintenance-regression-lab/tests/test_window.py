import unittest
from datetime import datetime, timezone

import helpers  # noqa: F401

from maintenance_lab.errors import ValidationError
from maintenance_lab.models import LAB_EPOCH_MS, LAB_NOW_MS
from maintenance_lab.window import (
    contains,
    iso_week_bounds,
    iso_week_id,
    ms_from_utc,
    parse_iso_datetime,
    utc_from_ms,
)


class WindowTests(unittest.TestCase):
    def test_lab_epoch_is_2026_01_01(self):
        self.assertEqual(utc_from_ms(LAB_EPOCH_MS).isoformat(), "2026-01-01T00:00:00+00:00")

    def test_lab_now_is_sunday_noon(self):
        self.assertEqual(utc_from_ms(LAB_NOW_MS).isoformat(), "2026-01-04T12:00:00+00:00")
        self.assertEqual(iso_week_id(LAB_NOW_MS), "2026-W01")

    def test_week_is_half_open(self):
        start, end = iso_week_bounds("2026-W01")
        self.assertTrue(contains("2026-W01", start))
        self.assertFalse(contains("2026-W01", end))
        self.assertEqual(iso_week_id(end), "2026-W02")

    def test_sunday_utc_stays_in_w01(self):
        sunday_late = ms_from_utc(datetime(2026, 1, 4, 16, 0, 0, tzinfo=timezone.utc))
        monday = ms_from_utc(datetime(2026, 1, 5, 0, 0, 0, tzinfo=timezone.utc))
        self.assertEqual(iso_week_id(sunday_late), "2026-W01")
        self.assertEqual(iso_week_id(monday), "2026-W02")

    def test_parse_iso_date_and_z(self):
        self.assertEqual(parse_iso_datetime("2026-01-02"), parse_iso_datetime("2026-01-02T00:00:00Z"))
        self.assertGreater(parse_iso_datetime("2026-01-02T12:00:00Z"), parse_iso_datetime("2026-01-02"))

    def test_slash_date_rejected(self):
        with self.assertRaises(ValidationError) as ctx:
            parse_iso_datetime("01/07/2026")
        self.assertIn("ISO-8601", ctx.exception.message)

    def test_naive_datetime_rejected(self):
        with self.assertRaises(ValidationError):
            ms_from_utc(datetime(2026, 1, 1))

    def test_invalid_week_id(self):
        with self.assertRaises(ValidationError):
            iso_week_bounds("2026-W1")

    def test_ms_roundtrip(self):
        self.assertEqual(ms_from_utc(utc_from_ms(LAB_NOW_MS)), LAB_NOW_MS)


if __name__ == "__main__":
    unittest.main()
