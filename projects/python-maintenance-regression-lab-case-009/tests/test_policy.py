"""Locks the status map, the deprecation gate, and the single exit site."""

import ast
import io
import logging
import unittest
import warnings
from pathlib import Path

import helpers
from batchnote import __version__
from batchnote import problems
from batchnote import status
from batchnote.run import HELP_EPILOG, LOGGER, run


def _package_dir():
    return Path(status.__file__).resolve().parent


class ExitVisitor(ast.NodeVisitor):
    def __init__(self):
        self.hits = []

    def visit_Name(self, node):
        if node.id == "SystemExit":
            self.hits.append("SystemExit:%s" % node.lineno)
        self.generic_visit(node)

    def visit_Attribute(self, node):
        if node.attr == "SystemExit":
            self.hits.append("SystemExit:%s" % node.lineno)
        owner = node.value
        if isinstance(owner, ast.Name) and owner.id == "sys" and node.attr == "exit":
            self.hits.append("sys.exit:%s" % node.lineno)
        if isinstance(owner, ast.Name) and owner.id == "os" and node.attr == "_exit":
            self.hits.append("os._exit:%s" % node.lineno)
        self.generic_visit(node)

    def visit_Call(self, node):
        func = node.func
        if isinstance(func, ast.Name) and func.id == "exit":
            self.hits.append("exit():%s" % node.lineno)
        self.generic_visit(node)


class PolicyTests(unittest.TestCase):
    def test_public_codes_are_frozen(self):
        self.assertEqual(status.OK, 0)
        self.assertEqual(status.FAILURE, 1)
        self.assertEqual(status.USAGE, 2)
        self.assertIsNone(status.USAGE_REMOVAL)
        self.assertNotEqual(status.USAGE, status.HISTORIC_SYSEXITS["EX_USAGE"])
        self.assertTrue(
            set(status.HISTORIC_SYSEXITS.values()).isdisjoint(
                {status.OK, status.FAILURE, status.USAGE}
            )
        )

    def test_catalog_uses_only_public_codes(self):
        for kind, (type_urn, title, code) in problems.CATALOG.items():
            self.assertIn(code, {status.OK, status.FAILURE, status.USAGE})
            self.assertNotIn(code, status.HISTORIC_SYSEXITS.values())
            self.assertTrue(type_urn.startswith("urn:batchnote:problem:"))
            self.assertNotEqual(title, "")
            if kind == "usage":
                self.assertEqual(code, status.USAGE)
            else:
                self.assertEqual(code, status.FAILURE)

    def test_finalize_collapses_the_count(self):
        self.assertEqual(status.finalize(0), 0)
        self.assertEqual(status.finalize(1), 1)
        self.assertEqual(status.finalize(256), 1)
        self.assertEqual(status.finalize(255), 1)
        with self.assertRaises(ValueError):
            status.finalize(-1)
        with self.assertRaises(ValueError):
            status.finalize(True)

    def test_usage_status_warns_only_after_a_removal_is_declared(self):
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            self.assertEqual(status.usage_status(), 2)
        self.assertEqual(caught, [])

        original = status.USAGE_REMOVAL
        status.USAGE_REMOVAL = "2.0.0"
        try:
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                code = status.usage_status()
            self.assertEqual(code, status.USAGE)
            self.assertEqual(status.USAGE, 2)
            self.assertTrue(caught)
            self.assertTrue(all(item.category is DeprecationWarning for item in caught))
            self.assertTrue(any("2.0.0" in str(item.message) for item in caught))
        finally:
            status.USAGE_REMOVAL = original

    def test_declared_removal_keeps_process_status_2(self):
        original = status.USAGE_REMOVAL
        status.USAGE_REMOVAL = "2.0.0"
        out = io.StringIO()
        err = io.StringIO()
        try:
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                code = run(["--bogus"], stdout=out, stderr=err)
        finally:
            status.USAGE_REMOVAL = original
        self.assertEqual(code, 2)
        self.assertEqual(out.getvalue(), "")
        self.assertIn("usage:", err.getvalue())
        self.assertTrue(any("2.0.0" in str(item.message) for item in caught))

    def test_human_line_is_soft_deprecated_without_a_warning(self):
        out = io.StringIO()
        err = io.StringIO()

        def opener(path):
            raise FileNotFoundError(2, "No such file or directory", path)

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            code = run(["missing.txt"], stdout=out, stderr=err, opener=opener)
        self.assertEqual(code, 1)
        self.assertEqual(out.getvalue(), "")
        text = err.getvalue()
        self.assertIn("batchnote: missing.txt: cannot read missing.txt: No such file or directory", text)
        self.assertNotIn("DeprecationWarning", text)
        self.assertFalse(any(item.category is DeprecationWarning for item in caught))

    def test_readme_matches_the_code_contract(self):
        readme = (helpers.PROJECT / "README.md").read_text(encoding="utf-8")
        self.assertIn(HELP_EPILOG, readme)
        self.assertIn("USAGE_REMOVAL is None.", readme)
        self.assertIn(
            "The preferred removal window is five years, and the minimum is two minor releases.",
            readme,
        )
        self.assertIn("batchnote %s" % __version__, readme)
        rows = {}
        for line in readme.splitlines():
            parts = [part.strip() for part in line.strip().strip("|").split("|")]
            if len(parts) == 2 and parts[0].startswith("EX_"):
                rows[parts[0]] = int(parts[1])
        self.assertEqual(rows, status.HISTORIC_SYSEXITS)

    def test_help_publishes_the_same_epilog(self):
        out = io.StringIO()
        err = io.StringIO()
        code = run(["--help"], stdout=out, stderr=err)
        self.assertEqual(code, 0)
        self.assertEqual(err.getvalue(), "")
        self.assertIn(HELP_EPILOG, out.getvalue())

    def test_only_main_mentions_system_exit(self):
        package = _package_dir()
        main = (package / "__main__.py").read_text(encoding="utf-8")
        self.assertIn("raise SystemExit(run(sys.argv[1:]))", main)
        for path in sorted(package.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            visitor = ExitVisitor()
            visitor.visit(tree)
            hits = visitor.hits
            if path.name == "__main__.py":
                self.assertTrue(any(item.startswith("SystemExit") for item in hits))
                self.assertFalse(any(item.startswith("sys.exit") for item in hits))
                self.assertFalse(any(item.startswith("os._exit") for item in hits))
            else:
                self.assertEqual(hits, [], path.name)
            source = path.read_text(encoding="utf-8")
            self.assertNotIn("logging.exception", source)
            self.assertNotIn(".exception(", source)

    def test_logger_is_quiet_unless_verbose_attaches_a_handler(self):
        self.assertFalse(LOGGER.propagate)
        self.assertTrue(any(isinstance(handler, logging.NullHandler) for handler in LOGGER.handlers))
        self.assertEqual(LOGGER.name, "batchnote")


if __name__ == "__main__":
    unittest.main()
