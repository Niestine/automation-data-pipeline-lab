import unittest
from fractions import Fraction

import helpers  # noqa: F401

from edition_gate.dialect import detect_dialect, pattern_score
from edition_gate.records import parse_text


class DialectTests(unittest.TestCase):
    def test_pattern_score_matches_the_consistency_formula(self):
        self.assertEqual(pattern_score([3, 3, 3, 3, 3]), Fraction(10, 3))
        self.assertEqual(pattern_score([4, 3, 4, 3, 3]), Fraction(7, 4))
        self.assertEqual(pattern_score([1, 1, 1]), Fraction(3, 1000))

    def test_tie_is_not_broken_by_the_sniffer(self):
        found = detect_dialect("a|b;c\nd|e;f\ng|h;i\n")
        self.assertEqual(found["status"], "dialect_tie")
        self.assertIsNone(found["dialect"])
        self.assertEqual(found["sniffer"]["delimiter"], ";")
        self.assertFalse(found["tie_broken"])

    def test_contract_dialect_is_not_a_search(self):
        text = "a;b,c\nd;e,f\n"
        found = detect_dialect(text, contract={"delimiter": ";", "quote": '"'})
        self.assertEqual(found["source"], "contract")
        self.assertEqual(found["status"], "selected")
        self.assertEqual(found["dialect"]["delimiter"], ";")
        parsed = parse_text(text, found["dialect"]["delimiter"], '"', "", "lf")
        self.assertEqual(parsed[0]["fields"], ["a", "b,c"])

    def test_known_misses_are_reproduced(self):
        spaced = detect_dialect("alpha one, beta\nalpha one, beta\nalpha one, beta\n")
        self.assertEqual(spaced["dialect"]["delimiter"], " ")
        self.assertEqual(spaced["sniffer"]["delimiter"], ",")
        quoted = detect_dialect('"red,large"\n"blue,small"\n"green,medium"\n')
        self.assertEqual(quoted["status"], "selected")
        self.assertEqual(quoted["dialect"]["delimiter"], ",")
        self.assertEqual(quoted["dialect"]["quote"], "")

    def test_messy_files_beat_the_sniffer_and_clean_files_agree(self):
        messy = {
            ";": (
                "M10;hex bolt, zinc;12\n"
                "M12;hex bolt, zinc;14\n"
                "M8;hex nut, plain;9\n"
                "M6;flat washer, wide;4\n"
            ),
            "|": (
                "M10|hex bolt, zinc|12\n"
                "M12|hex bolt, zinc|14\n"
                "M8|hex nut, plain|9\n"
            ),
            "\t": (
                "M10\thex bolt, zinc\t12\n"
                "M12\thex bolt, zinc\t14\n"
                "M8\thex nut, plain\t9\n"
            ),
        }
        clean = {
            ",": "sku,title,qty\nA1,hex bolt,12\nA2,hex nut,9\n",
            ";": "sku;title;qty\nA1;hex bolt;12\nA2;hex nut;9\n",
            "\t": "sku\ttitle\tqty\nA1\thex bolt\t12\nA2\thex nut\t9\n",
        }
        messy_ours = 0
        messy_sniffer = 0
        for delimiter, text in messy.items():
            found = detect_dialect(text)
            if found["dialect"] and found["dialect"]["delimiter"] == delimiter:
                messy_ours += 1
            if found["sniffer"] and found["sniffer"]["delimiter"] == delimiter:
                messy_sniffer += 1
        self.assertEqual(messy_ours, 3)
        self.assertEqual(messy_sniffer, 0)
        self.assertGreater(messy_ours, messy_sniffer)
        for delimiter, text in clean.items():
            found = detect_dialect(text)
            self.assertEqual(found["dialect"]["delimiter"], delimiter)
            self.assertEqual(found["sniffer"]["delimiter"], delimiter)

    def test_preamble_selects_skip_and_quote(self):
        text = (
            "Generated for storeroom\n"
            "sku;title;price\n"
            'A1;"hex bolt, zinc\n'
            'grade 8";12,50\n'
            "A2;plain nut;3,20\n"
        )
        found = detect_dialect(text)
        self.assertEqual(found["status"], "selected")
        self.assertEqual(found["dialect"]["delimiter"], ";")
        self.assertEqual(found["dialect"]["quote"], '"')
        self.assertEqual(found["dialect"]["skip_rows"], 1)

    def test_type_score_selects_the_quoting_dialect(self):
        text = 'id;amount\n1;"1,234"\n2;"2,345"\n3;"3,456"\n'
        found = detect_dialect(text)
        self.assertEqual(found["status"], "selected")
        self.assertEqual(found["dialect"]["delimiter"], ";")
        self.assertEqual(found["dialect"]["quote"], '"')

    def test_equal_scores_with_different_cells_abstain(self):
        text = 'name;price;note\nbolt;"12,50";N/A\nnut;"3,20";ok\nwasher;"1,10";N/A\n'
        found = detect_dialect(text)
        self.assertEqual(found["status"], "dialect_tie")
        self.assertIsNone(found["dialect"])
        self.assertEqual(found["sniffer"]["delimiter"], ";")


if __name__ == "__main__":
    unittest.main()
