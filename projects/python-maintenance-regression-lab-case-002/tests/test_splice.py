import random
import unittest

import helpers  # noqa: F401

from slip_lab.dialect import is_dialect_valid
from slip_lab.differential import classify_inprocess
from slip_lab.grammar import start_symbol
from slip_lab.hardened import parse as hardened_parse
from slip_lab.splice import (
    as_splice,
    contexts_still_failing,
    depth_rate,
    generate_batch,
    mutate_add_field,
    mutate_delete_char,
    splice_field,
    splice_malformed,
)
from slip_lab.stock import SPECS


class SpliceTests(unittest.TestCase):
    def test_splice_stays_in_grammar(self):
        skeleton = b"AA|BB\n"
        spliced = splice_field(skeleton, "CC", 0)
        self.assertEqual(spliced, b"CC|BB\n")
        self.assertEqual(start_symbol(spliced), "document")
        self.assertTrue(is_dialect_valid(spliced))
        malformed = splice_malformed(skeleton, '"NOEND')
        tagged = as_splice(malformed, "malformed_probe", "quoted_field", ["CASE"], 1)
        self.assertEqual(tagged.slot, "malformed_probe")
        self.assertIsNone(start_symbol(malformed))
        self.assertFalse(is_dialect_valid(malformed))
        self.assertEqual(depth_rate([as_splice(spliced, "valid", "field", ["CASE"], 1), tagged]), 1.0)

    def test_incomplete_fix(self):
        first = splice_field(b"KEEP|1\n", "BOOM", 0)
        second = splice_field(b"OTHER|2\n", "BOOM", 0)
        self.assertEqual(first, b"BOOM|1\n")
        self.assertEqual(second, b"BOOM|2\n")

        def unpatched(blob: bytes):
            if b"BOOM" in blob:
                raise RuntimeError("boom")
            return hardened_parse(blob)

        def patched(blob: bytes):
            if blob == first:
                return ("records", None, (("BOOM", "1"),))
            if b"BOOM" in blob:
                raise RuntimeError("boom")
            return hardened_parse(blob)

        self.assertEqual(contexts_still_failing(unpatched, [first, second]), [first, second])
        self.assertEqual(contexts_still_failing(patched, [first, second]), [second])

    def test_generated_batch_uses_history_and_safe_probes(self):
        sources = [(spec["id"], spec["blob"]) for spec in SPECS]
        batch = generate_batch(sources, 1)
        self.assertEqual(batch, generate_batch(sources, 1))

        historical = [item for item in batch if item.nonterminal == "quoted_field"]
        self.assertEqual(len(historical), 1)
        spliced = historical[0]
        self.assertIn("SLIP-001", spliced.parent_ids)
        self.assertEqual(spliced.blob, b'@slip|lane|qty\n""""|1\nL2|2\n')
        self.assertEqual(start_symbol(spliced.blob), "document")
        outcome = classify_inprocess(spliced.blob)
        self.assertEqual(outcome.kind, "DISAGREE")
        self.assertEqual(outcome.hardened.value[1], ("lane", "qty"))

        for item in batch:
            self.assertEqual(item.slot == "valid", start_symbol(item.blob) == "document", item)
            self.assertEqual(item.seed, 1)
            self.assertNotIn(b"\x00", item.blob)
            self.assertNotIn(b"\r", item.blob)
            self.assertNotIn(b"__HANG__", item.blob)
        probes = [item for item in batch if item.slot == "malformed_probe"]
        self.assertEqual(len(probes), 1)
        self.assertEqual(probes[0].parent_ids, ("CASE-header", "CASE-bare"))
        self.assertFalse(is_dialect_valid(probes[0].blob))

        unsafe = [("NUL", b"A\x00\n"), ("CR", b"A\r\n"), ("HANG", b'"__HANG__\n'), ("OK", b"A|1\n")]
        fallback = [item for item in generate_batch(unsafe, 2) if item.slot == "malformed_probe"]
        self.assertEqual(fallback[0].parent_ids, ("OK", "synthetic"))
        self.assertTrue(fallback[0].blob.startswith(b'"NOEND\n'))

    def test_nearby_edits_are_deterministic(self):
        blob = b"AA|BB\n"
        self.assertEqual(mutate_delete_char(blob, random.Random(3)), mutate_delete_char(blob, random.Random(3)))
        added = mutate_add_field(blob, random.Random(3))
        self.assertTrue(is_dialect_valid(added))
        self.assertGreater(len(added), len(blob))


if __name__ == "__main__":
    unittest.main()
