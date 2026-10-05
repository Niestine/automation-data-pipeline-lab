"""Case-fold collisions and close-then-replace."""

from __future__ import annotations

import os
import sys
import unittest
from unittest import mock

import helpers  # noqa: F401
from helpers import LabTest

from netunicode_lab import atomic_write_bytes, emit_interchange_csv
from netunicode_lab.errors import CasefoldCollisionError
from netunicode_lab.legacy import replace_while_open


class FileTests(LabTest):
    def test_casefold_collision_names_both_entries_and_does_not_write(self):
        existing = self.root / "Readme.txt"
        existing.write_bytes(b"keep")
        with self.assertRaises(CasefoldCollisionError) as caught:
            atomic_write_bytes(self.root / "readme.TXT", b"new")
        self.assertEqual(caught.exception.candidate, "readme.TXT")
        self.assertEqual(caught.exception.existing, "Readme.txt")
        self.assertIn("Readme.txt", str(caught.exception))
        self.assertIn("readme.TXT", str(caught.exception))
        # Windows reports the other spelling as the same file, so the listing is the oracle.
        self.assertEqual(sorted(path.name for path in self.root.iterdir()), ["Readme.txt"])
        self.assertEqual(existing.read_bytes(), b"keep")

    def test_emit_uses_the_same_casefold_guard(self):
        existing = self.root / "Readme.txt"
        existing.write_bytes(b"keep")
        with self.assertRaises(CasefoldCollisionError):
            emit_interchange_csv(self.root / "readme.TXT", [["x"]])
        self.assertEqual(existing.read_bytes(), b"keep")
        self.assertEqual(sorted(path.name for path in self.root.iterdir()), ["Readme.txt"])

    def test_same_spelling_can_be_replaced(self):
        destination = self.root / "notes.bin"
        atomic_write_bytes(destination, b"one")
        atomic_write_bytes(destination, b"two")
        self.assertEqual(destination.read_bytes(), b"two")
        self.assertEqual(sorted(path.name for path in self.root.iterdir()), ["notes.bin"])

    def test_replace_runs_only_after_the_temp_handle_is_closed(self):
        events = []
        handles = []
        real_open = open
        real_replace = os.replace

        def recording_open(*args, **kwargs):
            handle = real_open(*args, **kwargs)
            handles.append(handle)
            events.append("open")
            return handle

        def recording_replace(source, destination):
            events.append(("replace", [handle.closed for handle in handles]))
            return real_replace(source, destination)

        destination = self.root / "out.bin"
        with mock.patch("netunicode_lab.files.open", recording_open, create=True), mock.patch(
            "netunicode_lab.files.os.replace", recording_replace
        ):
            atomic_write_bytes(destination, b"abc")
        self.assertEqual(events, ["open", ("replace", [True])])
        self.assertEqual(destination.read_bytes(), b"abc")
        self.assertEqual(sorted(path.name for path in self.root.iterdir()), ["out.bin"])

    def test_failed_replace_leaves_no_temp_file(self):
        destination = self.root / "out.bin"
        destination.write_bytes(b"old")

        def failing_replace(source, destination):
            raise PermissionError(13, "simulated sharing violation")

        with mock.patch("netunicode_lab.files.os.replace", failing_replace):
            with self.assertRaises(PermissionError):
                atomic_write_bytes(destination, b"new")
        self.assertEqual(destination.read_bytes(), b"old")
        self.assertEqual(sorted(path.name for path in self.root.iterdir()), ["out.bin"])

    @unittest.skipUnless(sys.platform == "win32", "WinError 32 is the Windows sharing violation")
    def test_replace_inside_with_raises_winerror_32(self):
        source = self.root / "open.tmp"
        destination = self.root / "dest.bin"
        with self.assertRaises(OSError) as caught:
            replace_while_open(source, destination, b"abc")
        self.assertEqual(caught.exception.winerror, 32)
        self.assertFalse(destination.exists())
        atomic_write_bytes(destination, b"abc")
        self.assertEqual(destination.read_bytes(), b"abc")

    def test_missing_parent_and_directory_destination_fail_closed(self):
        missing = self.root / "missing" / "rows.csv"
        with self.assertRaises(FileNotFoundError):
            emit_interchange_csv(missing, [["x"]])
        self.assertFalse(missing.exists())
        with self.assertRaises(IsADirectoryError):
            atomic_write_bytes(self.root, b"nope")


if __name__ == "__main__":
    unittest.main()
