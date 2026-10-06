import inspect
import unittest

import helpers  # noqa: F401

from edition_gate import link as link_module
from edition_gate.link import evaluate, exact_baseline, link_rows, neighborhood_pairs, title_score, standardize
from edition_gate.normalize import match_key


def _row(**kwargs):
    base = {
        "supplier_id": "S",
        "sku": "",
        "brand": "Acme",
        "size": "M10",
        "title": "",
    }
    base.update(kwargs)
    return base


class LinkTests(unittest.TestCase):
    def test_standardized_titles_separate_match_review_and_non_match(self):
        same = title_score(standardize("44 West Fourth Street"), standardize("44 W. 4th St."))
        fifth = title_score(standardize("44 West Fourth Street"), standardize("44 West Fifth Street"))
        typo = title_score(standardize("Hex bolt zinc"), standardize("Hex bolt znc"))
        self.assertEqual(same, 1.0)
        self.assertLess(fifth, 0.78)
        self.assertGreater(typo, 0.78)
        self.assertLess(typo, 0.92)
        smith = title_score(standardize("smith"), standardize("smyth"))
        self.assertLess(smith, 0.78)

    def test_linker_recall_beats_the_exact_baseline(self):
        canonical = [
            _row(canonical_id="C1", sku="SKU1", title="44 W. 4th St."),
            _row(canonical_id="C2", sku="", size="M8", title="Hex bolt zinc"),
            _row(canonical_id="C3", sku="DUP", size="M8", title="Widget"),
            _row(canonical_id="C4", sku="DUP", brand="Other", size="M9", title="Widget two"),
            _row(canonical_id="C5", sku="", size="M12", title="brass flange unique"),
            _row(canonical_id="C6", sku="", size="M6", title="Hex bolt zinc"),
        ]
        incoming = [
            _row(row_id="L1", title="44 West Fourth Street"),
            _row(row_id="L2", title="44 West Fifth Street"),
            _row(row_id="L3", size="M8", title="Hex bolt zinc"),
            _row(row_id="L4", sku="DUP", size="M8", title="Widget"),
            _row(row_id="L5", brand="Axme", size="M12", title="brass flange unique"),
            _row(row_id="L6", size="M6", title="Hex bolt znc"),
            _row(row_id="L7", sku="SKU1", brand="Other", size="M99", title="unrelated gasket"),
        ]
        result = link_rows(incoming, canonical, {"window": 2})
        linked = {item["row_id"]: item for item in result["links"]}
        reviews = {item["row_id"]: item for item in result["reviews"]}
        self.assertEqual(linked["L1"]["canonical_id"], "C1")
        self.assertEqual(linked["L1"]["method"], "block")
        self.assertNotIn("L2", linked)
        self.assertNotIn("L2", reviews)
        self.assertEqual(linked["L3"]["canonical_id"], "C2")
        self.assertEqual(linked["L3"]["method"], "block")
        self.assertEqual(reviews["L4"]["kind"], "sku_collision")
        self.assertEqual(reviews["L4"]["canonical_ids"], ["C3", "C4"])
        self.assertNotIn("L4", linked)
        self.assertEqual(linked["L5"]["canonical_id"], "C5")
        self.assertEqual(linked["L5"]["method"], "neighborhood")
        self.assertEqual(reviews["L6"]["kind"], "review_band")
        self.assertNotIn("L6", linked)
        self.assertEqual(linked["L7"]["method"], "exact")
        self.assertEqual(linked["L7"]["canonical_id"], "C1")
        labels = {
            "L1": "C1",
            "L2": None,
            "L3": "C2",
            "L4": None,
            "L5": "C5",
            "L6": None,
            "L7": "C1",
        }
        measured = evaluate(labels, result["links"])
        baseline = evaluate(labels, exact_baseline(incoming, canonical))
        self.assertEqual(measured["precision"], 1.0)
        self.assertEqual(measured["recall"], 1.0)
        self.assertEqual(baseline["recall"], 0.25)
        self.assertGreater(measured["recall"], baseline["recall"])

    def test_neighborhood_window_misses_a_brand_gap(self):
        records = [
            {"row_id": "C1", "side": "canonical", "brand": "Acme", "title": "alpha bolt"},
            {"row_id": "M1", "side": "canonical", "brand": "Adme", "title": "middle piece"},
            {"row_id": "L1", "side": "incoming", "brand": "Axme", "title": "alpha bolt"},
        ]
        by_brand = neighborhood_pairs(records, [lambda item: item["brand"]], 2)
        self.assertNotIn(("L1", "C1"), {(left["row_id"], right["row_id"]) for left, right in by_brand})
        by_title = neighborhood_pairs(records, [lambda item: item["title"]], 2)
        self.assertIn(("L1", "C1"), {(left["row_id"], right["row_id"]) for left, right in by_title})

    def test_compatibility_fold_does_not_auto_link_and_soundex_is_absent(self):
        canonical = [_row(canonical_id="C1", title="fitting")]
        incoming = [_row(row_id="L1", title="\ufb01tting")]
        result = link_rows(incoming, canonical)
        self.assertEqual(result["links"], [])
        self.assertEqual(result["reviews"][0]["kind"], "compatibility_fold")
        self.assertEqual(result["reviews"][0]["canonical_id"], "C1")
        folded = link_rows(
            [_row(row_id="L1", brand="Acme", title="hex bolt")],
            [_row(canonical_id="C1", brand="\uff21cme", title="hex bolt")],
        )
        self.assertEqual(folded["links"][0]["canonical_id"], "C1")
        self.assertEqual(folded["links"][0]["method"], "block")
        smith = link_rows(
            [_row(row_id="L1", title="smyth")],
            [_row(canonical_id="C1", title="smith")],
        )
        self.assertEqual(smith["links"], [])
        source = inspect.getsource(link_module)
        self.assertNotIn("def soundex", source)
        self.assertFalse(hasattr(link_module, "soundex"))
        self.assertNotEqual(match_key("HEX"), match_key("hex"))


if __name__ == "__main__":
    unittest.main()
