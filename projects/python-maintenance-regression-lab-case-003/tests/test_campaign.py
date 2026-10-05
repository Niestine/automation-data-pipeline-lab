"""Campaign cap, trivial screen, soundness tripwire, and the GIL stamp."""

from __future__ import annotations

import json
import os
import sys
import sysconfig
import tempfile
import helpers  # noqa: F401
import unittest
from fractions import Fraction
from pathlib import Path

import time
import random
from time import sleep

from schedlab.campaign import (
    SCREEN_SEEDS,
    exact_failure_rate,
    failure_count,
    fit_atomicity_pad,
    run_campaign,
    trivial_screen,
)
from schedlab.errors import SoundnessError
from schedlab.machine import Op
from schedlab.native import native_smoke
from schedlab.oracles import is_subject_failure
from schedlab.subjects import Subject, atomicity_d2, get_subject


def _sleepy(_machine: object) -> bool:
    time.sleep(0)
    return True


def _quiet_helper() -> None:
    sleep(0)


def _indirect(_machine: object) -> bool:
    _quiet_helper()
    return True


def _nested_lambda(_machine: object) -> bool:
    return (lambda: time.monotonic())() > 0


def _make_random_predicate():
    def predicate(_machine: object) -> bool:
        random.random()
        return False

    return predicate


class CampaignTests(unittest.TestCase):
    def test_cap_is_not_a_subject_failure(self) -> None:
        capped = run_campaign(
            "ordering_d1",
            "fixed",
            "pct",
            seeds=range(10),
            d=1,
            n_max=2,
            schedule_cap=3,
            console_stream=helpers.silence(),
        )
        self.assertEqual(capped.terminal, "cap")
        self.assertEqual(capped.schedules_used, 3)
        self.assertFalse(is_subject_failure(capped.terminal))

        dfs_cap = run_campaign(
            "ordering_d1",
            "buggy",
            "dfs",
            n_max=2,
            schedule_cap=1,
            console_stream=helpers.silence(),
        )
        self.assertEqual(dfs_cap.terminal, "cap")
        self.assertEqual(dfs_cap.schedules_used, 1)
        self.assertFalse(is_subject_failure(dfs_cap.terminal))

    def test_pct_rows_stay_split_by_depth(self) -> None:
        seeds = list(range(30))
        depth1 = run_campaign(
            "atomicity_d2",
            "buggy",
            "pct",
            seeds=seeds,
            d=1,
            n_max=2,
            schedule_cap=30,
            console_stream=helpers.silence(),
        )
        depth2 = run_campaign(
            "atomicity_d2",
            "buggy",
            "pct",
            seeds=seeds,
            d=2,
            n_max=2,
            schedule_cap=30,
            console_stream=helpers.silence(),
        )
        depth3 = run_campaign(
            "atomicity_d2",
            "buggy",
            "pct",
            seeds=seeds,
            d=3,
            n_max=2,
            schedule_cap=30,
            console_stream=helpers.silence(),
        )
        self.assertEqual(depth1.terminal, "pass")
        self.assertEqual(depth1.schedules_used, 30)
        self.assertEqual(depth1.d, 1)
        self.assertEqual(depth2.terminal, "fail")
        self.assertEqual(depth2.d, 2)
        self.assertGreaterEqual(depth2.schedules_used, 1)
        self.assertLessEqual(depth2.schedules_used, 30)
        self.assertEqual(depth3.d, 3)
        self.assertNotEqual(depth1.d, depth3.d)
        again = run_campaign(
            "atomicity_d2",
            "buggy",
            "pct",
            seeds=seeds,
            d=2,
            n_max=2,
            schedule_cap=30,
            console_stream=helpers.silence(),
        )
        self.assertEqual(again.terminal, depth2.terminal)
        self.assertEqual(again.schedules_used, depth2.schedules_used)
        self.assertEqual(again.schedule, depth2.schedule)

    def test_screen_uses_the_fixed_seeds(self) -> None:
        seeds = json.loads((helpers.EXAMPLES / "screen_seeds.json").read_text(encoding="utf-8"))
        self.assertEqual(seeds, list(SCREEN_SEEDS))
        screen = trivial_screen(seeds)
        rows = screen["rows"]
        assert isinstance(rows, dict)
        for name in ("ordering_d1", "atomicity_d2", "deadlock_d2"):
            row = rows[name]
            assert isinstance(row, dict)
            pad = int(row["pad"])
            hits = failure_count(name, "buggy", seeds, pad=pad)
            self.assertEqual(row["hits"], hits)
            exact = Fraction(str(row["exact_rate"]))
            self.assertEqual(exact, exact_failure_rate(get_subject(name, "buggy", pad=pad)))
            self.assertEqual(row["trivial"], exact * 2 >= 1)
        # Every unpadded planted bug fails exactly half of controlled-random runs.
        self.assertEqual(rows["ordering_d1"]["exact_rate"], "1/2")
        self.assertTrue(rows["ordering_d1"]["trivial"])
        self.assertEqual(rows["deadlock_d2"]["exact_rate"], "1/2")
        self.assertTrue(rows["deadlock_d2"]["trivial"])
        self.assertEqual(screen["pad"], 1)
        self.assertEqual(rows["atomicity_d2"]["pad"], 1)
        self.assertEqual(rows["atomicity_d2"]["exact_rate"], "3/8")
        self.assertFalse(rows["atomicity_d2"]["trivial"])
        self.assertEqual(screen["headline"], "atomicity_d2")
        again = trivial_screen(seeds)
        self.assertEqual(again["rows"], screen["rows"])
        self.assertEqual(again["headline"], screen["headline"])

    def test_padding_sits_before_the_snapshot_and_makes_the_bug_rarer(self) -> None:
        padded = atomicity_d2("buggy", pad=2)
        fixed = atomicity_d2("fixed", pad=2)
        self.assertEqual(padded.step_budget(), 8)
        self.assertEqual(
            [(op.kind, op.name) for op in padded.ops[1]],
            [("read", "pad"), ("read", "pad"), ("read", "x"), ("write_add", "x")],
        )
        self.assertEqual(
            [(op.kind, op.name) for op in fixed.ops[1]],
            [("read", "pad"), ("read", "pad"), ("read", "x"), ("rmw_add", "x")],
        )
        self.assertEqual(padded.ops[1], padded.ops[2])
        rates = [exact_failure_rate(atomicity_d2("buggy", pad=pad)) for pad in range(4)]
        self.assertEqual(rates[:3], [Fraction(1, 2), Fraction(3, 8), Fraction(5, 16)])
        self.assertTrue(all(later < earlier for earlier, later in zip(rates, rates[1:])))
        self.assertEqual(fit_atomicity_pad(), 1)
        # The same read placed between the snapshot and the store widens the window.
        between = (
            Op("read", name="x", local="snap"),
            Op("read", name="pad", local="pad0"),
            Op("write_add", name="x", local="snap"),
        )
        wider = Subject(
            name="atomicity_between",
            revision="buggy",
            ops={1: between, 2: between},
            cells={"x": 0, "pad": 0},
            locks=(),
            predicate=atomicity_d2("buggy").predicate,
        )
        self.assertEqual(exact_failure_rate(wider), Fraction(3, 4))

    def test_exact_rate_matches_a_long_seeded_sample(self) -> None:
        seeds = range(2000)
        for name, pad in (("ordering_d1", 0), ("atomicity_d2", 1), ("deadlock_d2", 0)):
            exact = exact_failure_rate(get_subject(name, "buggy", pad=pad))
            sampled = failure_count(name, "buggy", seeds, pad=pad) / 2000
            self.assertAlmostEqual(sampled, float(exact), delta=0.04, msg=name)
        self.assertEqual(exact_failure_rate(get_subject("atomicity_d2", "fixed")), 0)
        self.assertEqual(exact_failure_rate(get_subject("deadlock_d2", "fixed")), 0)

    def test_first_failure_table_matches_the_fixture(self) -> None:
        table = json.loads(
            (helpers.EXAMPLES / "first_failure_table.json").read_text(encoding="utf-8")
        )
        for row in table["rows"]:
            report = run_campaign(
                table["subject"],
                table["revision"],
                row["policy"],
                seeds=table["seeds"],
                d=row["d"],
                n_max=table["n_max"],
                schedule_cap=table["schedule_cap"],
                pad=table["pad"],
                console_stream=helpers.silence(),
            )
            self.assertEqual(report.pad, table["pad"])
            self.assertEqual(report.terminal, row["terminal"], msg=row["policy"])
            self.assertEqual(report.schedules_used, row["schedules_used"], msg=row)
            self.assertEqual(report.d, row["d"])
            self.assertEqual(report.schedule, row["schedule"])
            self.assertEqual(report.seed, row["seed"])
            if row["terminal"] == "pass":
                self.assertFalse(is_subject_failure(report.terminal))
                self.assertIn("no failure", report.coverage)


def _always_true(_machine: object) -> bool:
    return True


class SoundnessTests(unittest.TestCase):
    def test_sleep_and_random_raise_before_an_artifact(self) -> None:
        sleepy = Subject(
            name="sleepy",
            revision="buggy",
            ops={1: (Op("read", name="c", local="v"),)},
            cells={"c": 0},
            locks=(),
            predicate=_sleepy,
        )
        drawn = Subject(
            name="drawn",
            revision="buggy",
            ops={1: (Op("read", name="c", local="v"),)},
            cells={"c": 0},
            locks=(),
            predicate=_make_random_predicate(),
        )
        indirect = Subject(
            name="indirect",
            revision="buggy",
            ops={1: (Op("read", name="c", local="v"),)},
            cells={"c": 0},
            locks=(),
            predicate=_always_true,
            audit_fns=(_indirect,),
        )
        nested = Subject(
            name="nested",
            revision="buggy",
            ops={1: (Op("read", name="c", local="v"),)},
            cells={"c": 0},
            locks=(),
            predicate=_nested_lambda,
        )
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            for subject in (sleepy, drawn, indirect, nested):
                before = list(directory.iterdir())
                with self.assertRaises(SoundnessError):
                    run_campaign(
                        subject,
                        "buggy",
                        "fair",
                        artifact_dir=directory,
                        console_stream=helpers.silence(),
                    )
                self.assertEqual(list(directory.iterdir()), before)


class GilAndSmokeTests(unittest.TestCase):
    def test_artifact_stamp_matches_this_process(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            run_campaign(
                "ordering_d1",
                "fixed",
                "fair",
                n_max=2,
                artifact_dir=directory,
                console_stream=helpers.silence(),
            )
            written = list(directory.glob("*.json"))
            self.assertEqual(len(written), 1)
            payload = json.loads(written[0].read_text(encoding="utf-8"))
        gil = payload["gil"]
        self.assertEqual(gil["Py_GIL_DISABLED"], sysconfig.get_config_var("Py_GIL_DISABLED"))
        self.assertEqual(gil["PYTHON_GIL"], os.environ.get("PYTHON_GIL"))
        self.assertEqual(payload["python"], sys.version)
        probe = getattr(sys, "_is_gil_enabled", None)
        if probe is None:
            self.assertIsNone(gil["gil_enabled"])
            self.assertEqual(gil["gil_probe"], "unavailable")
        else:
            self.assertEqual(gil["gil_enabled"], bool(probe()))
            self.assertNotIn("gil_probe", gil)

    def test_native_smoke_writes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            result = native_smoke(iterations=2, artifact_dir=directory)
            self.assertEqual(list(directory.iterdir()), [])
        self.assertTrue(result["informational"])
        self.assertFalse(result["artifact_written"])
        self.assertIn("gil", result)
        # Informational only: the smoke reports ok but never gates CI.
        self.assertIsInstance(result["ok"], bool)


if __name__ == "__main__":
    unittest.main()
