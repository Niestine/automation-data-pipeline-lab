import contextlib
import io
import tempfile
import unittest
from pathlib import Path

import helpers  # noqa: F401

import run_lab
from edition_gate.model import column, schema
from edition_gate.pipeline import ingest_bytes, read_with_retry, run_demo
from edition_gate.registry import Registry


def _register(columns, operators=None):
    registry = Registry()
    result = registry.register(schema(1, columns), operators or [])
    if not result["ok"]:
        raise AssertionError(result["reasons"])
    return registry


class PipelineTests(unittest.TestCase):
    def test_demo_is_deterministic_and_keeps_the_staged_failures(self):
        first = run_demo(Path("unused-dry"), dry_run=True)
        second = run_demo(Path("unused-dry"), dry_run=True)
        self.assertEqual(first["manifest"], second["manifest"])
        self.assertEqual(first["manifest"]["manifest_sha256"], second["manifest"]["manifest_sha256"])
        batches = {item["name"]: item for item in first["manifest"]["batches"]}
        self.assertEqual(batches["vendor_v1"]["status"], "accepted_with_errors")
        self.assertEqual(batches["vendor_v1"]["quarantined_rows"], 1)
        self.assertEqual(batches["vendor_v2"]["status"], "accepted")
        self.assertEqual(batches["stale_v1"]["reason"], "schema_too_old")
        self.assertEqual(batches["stale_v1"]["rows"], 0)
        probe = first["manifest"]["dialect_probe"]
        self.assertEqual(probe["delimiter"], ";")
        self.assertEqual(probe["sniffer_delimiter"], ",")
        rewrite = first["manifest"]["rewrite_organization"]
        self.assertEqual(rewrite["form"], "UNION ALL")
        self.assertEqual(rewrite["sources"][0], "organization")
        self.assertIn("org_name", rewrite["sources"])
        loaded = first["batches"]["vendor_v1"]
        self.assertIn("organization", loaded["rows"][0]["cells"])
        self.assertNotIn("org_name", loaded["rows"][0]["cells"])
        links = {item["row_number"]: item for item in loaded["links"]}
        self.assertEqual(links[1]["method"], "exact")
        self.assertEqual(links[1]["canonical_id"], "P-100")
        self.assertEqual(links[2]["method"], "block")
        self.assertEqual(links[2]["canonical_id"], "P-100")
        self.assertIn("compatibility_fold", loaded["rows"][2]["flags"])
        self.assertEqual(loaded["rows"][2]["cells"]["title"]["raw_length"], 6)
        self.assertEqual(loaded["rows"][2]["cells"]["title"]["nfkc_length"], 7)
        self.assertEqual(links[3]["method"], "new")
        self.assertIn("type_error", loaded["rows"][3]["cells"]["pack_qty"]["errors"])
        self.assertEqual(loaded["rows"][3]["cells"]["pack_qty"]["value"], "one")
        self.assertEqual(loaded["quarantined_rows"][0]["reasons"], ["ragged_row"])
        self.assertEqual(first["batches"]["vendor_v2"]["rows"][0]["cells"]["organization"]["value"], "Fabrikam")

    def test_replay_is_idempotent_and_dry_run_does_not_write(self):
        registry = _register([column("sku", required=True, null_tokens=())])
        payload = b"sku\nA\n"
        first = ingest_bytes(registry, payload, writer_version=1)
        self.assertEqual(first["links"][0]["canonical_id"], "P-NA-A")
        self.assertEqual(list(registry.store.rows), ["P-NA-A"])
        again = ingest_bytes(registry, payload, writer_version=1)
        self.assertTrue(again["idempotent_replay"])
        self.assertEqual(again["rows"], first["rows"])
        self.assertEqual(list(registry.store.rows), ["P-NA-A"])
        before = registry.store.sorted_rows()
        dry = ingest_bytes(registry, b"sku\nB\n", writer_version=1, dry_run=True)
        self.assertEqual(dry["status"], "accepted")
        self.assertEqual(registry.store.sorted_rows(), before)
        self.assertNotIn("P-NA-B", registry.store.rows)

    def test_cache_key_covers_charset_and_link_settings(self):
        registry = _register([column("sku", required=True, null_tokens=())])
        payload = "sku\nÉ\n".encode("windows-1252")
        legacy = ingest_bytes(registry, payload, writer_version=1, declared_charset="windows-1252", link=False)
        self.assertEqual(legacy["status"], "accepted")
        self.assertEqual(legacy["links"], [])
        relabeled = ingest_bytes(registry, payload, writer_version=1, declared_charset="utf-8", link=False)
        self.assertFalse(relabeled.get("idempotent_replay", False))
        self.assertEqual(relabeled["reason"], "decode_fatal")
        self.assertEqual(registry.store.rows, {})
        linked = ingest_bytes(registry, payload, writer_version=1, declared_charset="windows-1252")
        self.assertFalse(linked["idempotent_replay"])
        self.assertEqual(linked["links"][0]["canonical_id"], "P-NA-É")
        self.assertIn("P-NA-É", registry.store.rows)

    def test_compatibility_fold_review_is_not_stored_as_a_new_part(self):
        registry = _register(
            [
                column("supplier_id", required=True, null_tokens=()),
                column("sku", null_tokens=()),
                column("brand", null_tokens=()),
                column("size", null_tokens=()),
                column("title", null_tokens=()),
            ]
        )
        registry.store.upsert(
            {"canonical_id": "C1", "supplier_id": "S", "sku": "", "brand": "Acme", "size": "M10", "title": "fitting"}
        )
        payload = "supplier_id,sku,brand,size,title\nS,,Acme,M10,ﬁtting\n".encode("utf-8")
        result = ingest_bytes(registry, payload, writer_version=1)
        self.assertEqual(result["links"], [])
        self.assertEqual(len(result["review"]), 1)
        self.assertEqual(result["review"][0]["kind"], "compatibility_fold")
        self.assertEqual(result["review"][0]["canonical_id"], "C1")
        self.assertEqual(list(registry.store.rows), ["C1"])

    def test_old_writer_may_omit_a_new_column_and_the_current_writer_may_not(self):
        registry = Registry()
        self.assertTrue(registry.register(schema(1, [column("name", required=True, null_tokens=())]), [])["ok"])
        self.assertTrue(
            registry.register(
                schema(2, [column("name", required=True, null_tokens=()), column("color", publication="delete_only")]),
                [{"op": "add_column", "name": "color"}],
            )["ok"]
        )
        self.assertTrue(
            registry.register(
                schema(
                    3,
                    [
                        column("name", required=True, null_tokens=()),
                        column("color", has_default=True, default="green", null_tokens=()),
                    ],
                ),
                [{"op": "set_default", "name": "color"}],
            )["ok"]
        )
        historical = ingest_bytes(registry, b"name\nAcme\n", writer_version=2, link=False)
        self.assertEqual(historical["status"], "accepted", historical["reason"])
        self.assertEqual(historical["rows"][0]["cells"]["color"]["value"], "green")
        self.assertTrue(historical["rows"][0]["cells"]["color"]["omitted"])
        painted = ingest_bytes(registry, b"name,color\nAcme,blue\n", writer_version=3, link=False)
        self.assertEqual(painted["rows"][0]["cells"]["color"]["value"], "blue")
        unnamed = ingest_bytes(registry, b"color\nblue\n", writer_version=3, link=False)
        self.assertEqual(unnamed["reason"], "schema_resolution")
        self.assertEqual(unnamed["missing_columns"], ["name"])
        self.assertEqual(unnamed["rows"], [])
        required = Registry()
        self.assertTrue(required.register(schema(1, [column("name", required=True, null_tokens=())]), [])["ok"])
        denied_jump = required.register(
            schema(
                2,
                [
                    column("name", required=True, null_tokens=()),
                    column("color", required=True, has_default=True, default="green", null_tokens=()),
                ],
            ),
            [{"op": "add_column", "name": "color"}],
        )
        self.assertIn("publication_jump", denied_jump["reasons"])

    def test_long_to_double_records_precision_loss(self):
        registry = Registry()
        self.assertTrue(registry.register(schema(1, [column("serial", datatype="long", required=True, null_tokens=())]), [])["ok"])
        widened = registry.register(
            schema(2, [column("serial", datatype="double", required=True, null_tokens=())]),
            [{"op": "set_type", "name": "serial"}],
        )
        self.assertTrue(widened["ok"], widened["reasons"])
        loaded = ingest_bytes(
            registry,
            b"serial\n9007199254740993\n9007199254740995\n",
            writer_version=1,
            link=False,
        )
        self.assertEqual(loaded["status"], "accepted", loaded["reason"])
        first, second = loaded["rows"]
        self.assertTrue(first["cells"]["serial"]["precision_loss"])
        self.assertEqual(first["cells"]["serial"]["source_integer"], "9007199254740993")
        self.assertEqual(first["cells"]["serial"]["value"], 9007199254740992)
        self.assertEqual(first["cells"]["serial"]["promoted_from"], "long")
        self.assertEqual(second["cells"]["serial"]["value"], 9007199254740996)
        self.assertEqual(second["cells"]["serial"]["source_integer"], "9007199254740995")

    def test_read_retry_doubles_the_delay_without_sleeping_by_default(self):
        calls = {"count": 0}
        delays = []

        def opener(_path):
            calls["count"] += 1
            if calls["count"] < 3:
                raise OSError("busy")

            class _Handle(io.BytesIO):
                def __enter__(self):
                    return self

                def __exit__(self, *_args):
                    return False

            return _Handle(b"sku\nA\n")

        payload = read_with_retry("parts.csv", attempts=3, sleeper=delays.append, opener=opener)
        self.assertEqual(payload, b"sku\nA\n")
        self.assertEqual(delays, [0.01, 0.02])
        with self.assertRaises(ValueError):
            read_with_retry("parts.csv", attempts=0, opener=opener)

        def always_busy(_path):
            raise OSError("busy")

        waits = []
        with self.assertRaises(OSError):
            read_with_retry("parts.csv", attempts=2, sleeper=waits.append, opener=always_busy)
        self.assertEqual(waits, [0.01])

    def test_cli_writes_the_demo_and_dry_run_creates_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            destination = Path(tmp) / "out"
            with contextlib.redirect_stdout(io.StringIO()):
                code = run_lab.main(["--out-dir", str(destination)])
            self.assertEqual(code, 0)
            self.assertTrue((destination / "manifest.json").is_file())
            self.assertTrue((destination / "vendor-v1.json").is_file())
            self.assertTrue((destination / "stale-v1.json").is_file())
            nested = Path(tmp) / "absent"
            with contextlib.redirect_stdout(io.StringIO()):
                dry_code = run_lab.main(["--out-dir", str(nested), "--dry-run"])
            self.assertEqual(dry_code, 0)
            self.assertFalse(nested.exists())
            with contextlib.redirect_stderr(io.StringIO()) as err:
                bad_code = run_lab.main(["--out-dir", str(nested), "--attempts", "0"])
            self.assertEqual(bad_code, 2)
            self.assertIn("attempts must be at least 1", err.getvalue())
            self.assertFalse(nested.exists())


if __name__ == "__main__":
    unittest.main()
