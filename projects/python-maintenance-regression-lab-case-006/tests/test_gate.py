"""Static gate modeled on Ruff's unspecified-encoding rule, plus the call-site pins."""

from __future__ import annotations

import ast
import unittest

import helpers  # noqa: F401
from helpers import LIBRARY, library_sources

TEXT_CALLS = {"open", "fdopen", "read_text", "write_text"}


def _called_name(node: ast.Call) -> str | None:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _literal_mode(node: ast.Call) -> str | None:
    """Return a literal mode string. Path.open puts it in args[0]; open/fdopen use args[1]."""

    name = _called_name(node)
    index = 0 if name == "open" and isinstance(node.func, ast.Attribute) else 1
    if len(node.args) > index and isinstance(node.args[index], ast.Constant) and isinstance(node.args[index].value, str):
        return node.args[index].value
    for keyword in node.keywords:
        if keyword.arg == "mode" and isinstance(keyword.value, ast.Constant) and isinstance(keyword.value.value, str):
            return keyword.value.value
    return None


def unspecified_encoding_findings(source: str) -> list[str]:
    """Return text-mode opens and read_text/write_text calls that omit encoding."""

    tree = ast.parse(source)
    findings = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _called_name(node)
        if name not in TEXT_CALLS:
            continue
        keywords = {keyword.arg for keyword in node.keywords}
        if name in {"read_text", "write_text"}:
            if "encoding" not in keywords:
                findings.append(f"line {node.lineno}: {name}() without encoding")
            continue
        mode = _literal_mode(node)
        if mode is not None and "b" in mode:
            continue
        if "encoding" not in keywords:
            findings.append(f"line {node.lineno}: {name}() without encoding")
    return findings


class GateTests(unittest.TestCase):
    def test_gate_flags_text_open_and_read_text(self):
        self.assertEqual(
            unspecified_encoding_findings("def f(p):\n    open(p, 'w')\n"),
            ["line 2: open() without encoding"],
        )
        self.assertEqual(
            unspecified_encoding_findings("def f(p):\n    p.open('w')\n"),
            ["line 2: open() without encoding"],
        )
        self.assertEqual(
            unspecified_encoding_findings("def f(p):\n    p.read_text()\n"),
            ["line 2: read_text() without encoding"],
        )
        self.assertEqual(
            unspecified_encoding_findings("def f(p):\n    p.write_text('x')\n"),
            ["line 2: write_text() without encoding"],
        )

    def test_gate_allows_explicit_encoding_and_binary(self):
        source = (
            "def f(p):\n"
            "    open(p, 'w', encoding='utf-8')\n"
            "    open(p, 'wb')\n"
            "    p.open('wb')\n"
            "    p.read_text(encoding='utf-8')\n"
            "    p.read_bytes()\n"
        )
        self.assertEqual(unspecified_encoding_findings(source), [])

    def test_library_has_no_unspecified_encoding(self):
        findings = []
        for path in library_sources():
            text = path.read_text(encoding="utf-8")
            for item in unspecified_encoding_findings(text):
                findings.append(f"{path.name}: {item}")
        self.assertEqual(findings, [])

    def test_emit_text_open_passes_encoding_and_newline(self):
        tree = ast.parse((LIBRARY / "emit.py").read_text(encoding="utf-8"))
        saw_text_open = False
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or _called_name(node) != "open":
                continue
            mode = _literal_mode(node)
            if mode is not None and "b" in mode:
                continue
            keywords = {keyword.arg for keyword in node.keywords}
            self.assertIn("encoding", keywords)
            self.assertIn("newline", keywords)
            self.assertIn("errors", keywords)
            saw_text_open = True
        self.assertTrue(saw_text_open)

    def test_ingest_reads_bytes(self):
        source = (LIBRARY / "ingest.py").read_text(encoding="utf-8")
        self.assertIn("read_bytes", source)
        self.assertNotIn("read_text", source)

    def test_library_avoids_smuggled_bytes_splitlines_and_separator_rewrites(self):
        forbidden_tokens = ("surrogateescape", "surrogatepass", "splitlines")
        for path in library_sources():
            text = path.read_text(encoding="utf-8")
            for token in forbidden_tokens:
                self.assertNotIn(token, text, path.name)
            tree = ast.parse(text)
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                    continue
                if node.func.attr != "replace" or not node.args:
                    continue
                argument = node.args[0]
                if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
                    if set(argument.value) <= {"/", "\\"} and argument.value:
                        self.fail(f"{path.name}:{node.lineno} rewrites a path separator")

    def test_production_modules_do_not_import_legacy(self):
        for name in ("__init__.py", "emit.py", "ingest.py", "files.py", "utf8strict.py", "policy.py", "__main__.py"):
            text = (LIBRARY / name).read_text(encoding="utf-8")
            self.assertNotIn("legacy", text, name)


if __name__ == "__main__":
    unittest.main()
