"""Per-reader history classes, kept apart from the retirement verdict."""

from __future__ import annotations

import json
import unittest

import helpers  # noqa: F401

from contract_lab.compliance import cited_figures, review_history, score_history
from helpers import EXAMPLES, GAUGE, bulletin, document, flat_schema


def _release(before, after, left, right):
    return {
        "before": before,
        "after": after,
        "before_version": left,
        "after_version": right,
    }


class ComplianceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.full = flat_schema(
            {"gauge_id": {"type": "string"}, "height_mm": {"type": "integer"}},
            ["gauge_id"],
        )
        self.short = flat_schema({"gauge_id": {"type": "string"}}, ["gauge_id"])
        self.added = flat_schema(
            {
                "gauge_id": {"type": "string"},
                "height_mm": {"type": "integer"},
                "spare_note": {"type": "string"},
            },
            ["gauge_id"],
        )

    def test_required_history_classes(self) -> None:
        patch_break = _release(bulletin(self.full), bulletin(self.short), "1.2.0", "1.2.1")
        self.assertEqual(score_history([patch_break], "strict"), "leaking")
        self.assertEqual(score_history([patch_break], "tolerant"), "leaking")

        added_path = document(
            {
                GAUGE: bulletin(self.short)["paths"][GAUGE],
                "/stations/{station_id}": bulletin(self.short)["paths"][GAUGE],
            }
        )
        quiet = _release(bulletin(self.short), added_path, "1.0.0", "1.1.0")
        self.assertEqual(score_history([quiet], "strict"), "compatible_only")
        self.assertEqual(score_history([quiet], "tolerant"), "compatible_only")

        major = _release(bulletin(self.full), bulletin(self.short), "1.0.0", "2.0.0")
        self.assertEqual(score_history([major], "strict"), "consistent")
        self.assertEqual(score_history([major], "tolerant"), "consistent")

        nullable = _release(
            bulletin(flat_schema({"height_mm": {"type": "integer", "nullable": False}})),
            bulletin(flat_schema({"height_mm": {"type": "integer", "nullable": True}})),
            "1.0.0",
            "2.0.0",
        )
        for reader in ("strict", "tolerant"):
            verdict = score_history([nullable], reader)
            self.assertEqual(verdict, "undecidable")
            self.assertNotIn(verdict, {"consistent", "leaking", "compatible_only"})

        minor_add = _release(bulletin(self.full), bulletin(self.added), "1.2.0", "1.3.0")
        self.assertEqual(score_history([minor_add], "strict"), "leaking")
        self.assertNotEqual(score_history([minor_add], "tolerant"), "leaking")
        self.assertEqual(score_history([minor_add], "tolerant"), "compatible_only")

    def test_downgrade_label_and_unnecessary_major(self) -> None:
        downgrade = _release(bulletin(self.full), bulletin(self.short), "1.4.0", "1.2.0")
        self.assertEqual(score_history([downgrade], "strict"), "leaking")
        label = _release(bulletin(self.full), bulletin(self.short), "1.2.3-alpha", "1.2.3-rc")
        self.assertEqual(score_history([label], "tolerant"), "leaking")
        unnecessary = _release(
            bulletin(self.short),
            document(
                {
                    GAUGE: bulletin(self.short)["paths"][GAUGE],
                    "/stations/{station_id}": bulletin(self.short)["paths"][GAUGE],
                }
            ),
            "1.0.0",
            "2.0.0",
        )
        self.assertEqual(score_history([unnecessary], "strict"), "compatible_only")

    def test_major_bump_does_not_clear_an_unmanaged_removal(self) -> None:
        present = bulletin(self.short, version="1.0.0")
        gone = document({GAUGE: {}}, version="2.0.0")
        report = review_history([present, gone], ["1.0.0", "2.0.0"])
        self.assertEqual(report["compliance"]["strict"], "consistent")
        self.assertEqual(report["compliance"]["tolerant"], "consistent")
        self.assertFalse(report["protocol_ok"])
        self.assertEqual(report["removals"][0]["status"], "unmanaged")

    def test_checked_in_history_matches_the_split(self) -> None:
        payload = json.loads((EXAMPLES / "histories.json").read_text(encoding="utf-8"))
        report = review_history(payload["documents"], payload["versions"])
        self.assertEqual(report["compliance"]["strict"], "leaking")
        self.assertEqual(report["compliance"]["tolerant"], "compatible_only")
        self.assertEqual(report["releases"][0]["change_ids"], ["response_property_added"])
        self.assertEqual(report["releases"][0]["version_kind"], "minor_upgrade")

    def test_an_undecidable_release_does_not_hide_a_decided_leak(self) -> None:
        nullable = _release(
            bulletin(flat_schema({"gauge_id": {"type": "string", "nullable": False}}, ["gauge_id"])),
            bulletin(flat_schema({"gauge_id": {"type": "string", "nullable": True}}, ["gauge_id"])),
            "1.2.1",
            "1.2.2",
        )
        patch_break = _release(bulletin(self.full), bulletin(self.short), "1.2.0", "1.2.1")
        self.assertEqual(score_history([patch_break, nullable], "strict"), "leaking")
        self.assertEqual(score_history([nullable, patch_break], "tolerant"), "leaking")
        major = _release(bulletin(self.full), bulletin(self.short), "1.0.0", "2.0.0")
        self.assertEqual(score_history([major, nullable], "strict"), "undecidable")
        quiet = _release(bulletin(self.short), bulletin(self.short), "1.0.0", "1.0.1")
        self.assertEqual(score_history([quiet, nullable], "tolerant"), "undecidable")
        unparsed = _release(bulletin(self.short), bulletin(self.short), "1.0.0", "20240101")
        self.assertEqual(score_history([quiet, unparsed], "strict"), "undecidable")

    def test_cited_counts_stay_separate(self) -> None:
        cited = cited_figures()
        stored = json.loads((EXAMPLES / "measured_metrics.json").read_text(encoding="utf-8"))
        self.assertEqual(stored, cited)
        self.assertEqual(cited["best_case_consistent_apis"], 517)
        self.assertEqual(cited["leaking_apis"], 1970)
        self.assertEqual(cited["never_breaking_apis"], 927)
        self.assertEqual(cited["studied_apis"], 3075)
        self.assertNotEqual(
            cited["best_case_consistent_apis"] + cited["leaking_apis"] + cited["never_breaking_apis"],
            cited["studied_apis"],
        )
        self.assertNotEqual(cited["abstract_new_versions"], cited["dataset_section_versions"])
        self.assertEqual(cited["abstract_new_versions"], 16053)
        self.assertEqual(cited["dataset_section_versions"], 15856)
        self.assertEqual(
            cited["breaking_change_types"]
            + cited["non_breaking_change_types"]
            + cited["undecidable_change_types"],
            cited["change_type_total"],
        )
        self.assertEqual(cited["yasmin_proactive_channel_apis"], 3)
        self.assertEqual(cited["yasmin_deprecation_related_apis"], 219)
        self.assertEqual(cited["yasmin_breaking_versions"], 251)
