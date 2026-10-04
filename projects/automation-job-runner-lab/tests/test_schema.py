import unittest

import helpers  # noqa: F401
from helpers import catalog_dict, job_dict

from automation_job_lab.errors import SchemaError
from automation_job_lab.schema import (
    check_schema,
    load_catalog,
    parse_json_text,
    topo_sort,
    validate_record,
)
from automation_job_lab.seed import build_catalog, build_inbox


class SchemaTests(unittest.TestCase):
    def test_default_catalog_loads_and_is_acyclic(self):
        catalog = load_catalog(build_catalog())
        ordered = [job.id for job in topo_sort(catalog.jobs)]
        self.assertEqual(
            ordered,
            [
                "heartbeat-log",
                "ingest-inbox",
                "transform-records",
                "export-report",
                "cleanup-inbox",
                "notify-ops",
            ],
        )

    def test_extra_catalog_field_is_rejected(self):
        data = build_catalog()
        data["owner"] = "ops-team"
        with self.assertRaises(SchemaError) as ctx:
            load_catalog(data)
        self.assertIn("additional property", ctx.exception.message)

    def test_unknown_handler_is_rejected(self):
        data = catalog_dict(job_dict(handler="shell"))
        with self.assertRaises(SchemaError):
            load_catalog(data)

    def test_unknown_dependency_is_rejected(self):
        data = catalog_dict(job_dict(id="job-a", depends_on=["missing"]))
        with self.assertRaises(SchemaError) as ctx:
            load_catalog(data)
        self.assertIn("unknown missing", ctx.exception.message)

    def test_duplicate_job_id_is_rejected(self):
        data = catalog_dict(job_dict(), job_dict())
        with self.assertRaises(SchemaError) as ctx:
            load_catalog(data)
        self.assertIn("duplicate job id", ctx.exception.message)

    def test_cycle_is_rejected(self):
        a = job_dict(id="job-a", depends_on=["job-b"])
        b = job_dict(id="job-b", depends_on=["job-a"])
        with self.assertRaises(SchemaError) as ctx:
            load_catalog(catalog_dict(a, b))
        self.assertIn("cycle", ctx.exception.message)

    def test_self_dependency_is_rejected(self):
        with self.assertRaises(SchemaError):
            load_catalog(catalog_dict(job_dict(id="job-a", depends_on=["job-a"])))

    def test_timeout_out_of_range(self):
        with self.assertRaises(SchemaError):
            load_catalog(catalog_dict(job_dict(timeout_ms=0)))

    def test_cron_minute_out_of_range(self):
        with self.assertRaises(SchemaError):
            load_catalog(catalog_dict(job_dict(schedule={"cron_minute": 60})))

    def test_bool_is_not_an_integer(self):
        errors = check_schema(True, {"type": "integer"}, "$")
        self.assertTrue(errors)

    def test_nan_json_is_rejected(self):
        with self.assertRaises(SchemaError) as ctx:
            parse_json_text('{"window_ms": NaN}')
        self.assertIn("non-finite", ctx.exception.message)
        with self.assertRaises(SchemaError):
            parse_json_text('[-Infinity]')

    def test_nan_text_inside_a_string_is_accepted(self):
        self.assertEqual(parse_json_text('{"summary": "NaN rows and Infinity loops"}')["summary"], "NaN rows and Infinity loops")

    def test_unknown_bucket_param_is_rejected_at_load_time(self):
        data = catalog_dict(
            job_dict(id="ingest-inbox", handler="ingest", params={"source": "inbox", "dest": "../etc"})
        )
        with self.assertRaises(SchemaError) as ctx:
            load_catalog(data)
        self.assertIn("dest", ctx.exception.message)

    def test_valid_inbox_records_pass(self):
        for row in build_inbox(window=0)[:10]:
            self.assertEqual(validate_record(row), [])

    def test_poison_extra_field_is_rejected(self):
        row = next(item for item in build_inbox() if item["id"] == "REC-1098")
        errors = validate_record(row)
        self.assertTrue(any("additional property" in item for item in errors))

    def test_poison_amount_mismatch_is_rejected(self):
        row = next(item for item in build_inbox() if item["id"] == "REC-1099")
        errors = validate_record(row)
        self.assertTrue(any("does not match" in item for item in errors))

    def test_ingest_params_require_source_and_dest(self):
        data = catalog_dict(
            job_dict(id="ingest-inbox", handler="ingest", params={"source": "inbox"})
        )
        with self.assertRaises(SchemaError) as ctx:
            load_catalog(data)
        self.assertIn("dest", ctx.exception.message)
