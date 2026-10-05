from pathlib import Path
from contextlib import redirect_stdout
import io
import json
import tempfile
import unittest

from tools.check_architecture import check, load_legacy, main


class ArchitectureTests(unittest.TestCase):
    def inspect(self, files, legacy=()):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "src").mkdir()
            for name, content in files.items():
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding="utf-8")
            return check(root, set(legacy))

    def codes(self, files, legacy=()):
        return {row.code for row in self.inspect(files, legacy)}

    def test_accepts_pure_domain(self):
        self.assertEqual(self.inspect({"src/domain/price.py":
            "from decimal import Decimal\nfrom dataclasses import dataclass\n"}), [])

    def test_blocks_new_flat_module(self):
        self.assertIn("A001", self.codes({"src/another_runner.py": "x = 1"}))

    def test_layer_names_cannot_hide_new_flat_modules(self):
        self.assertIn("A001", self.codes({"src/domain.py": "x = 1"}))

    def test_allows_legacy_but_does_not_certify_it(self):
        self.assertEqual(self.inspect({"src/old.py": "import playwright"},
                                      ["src/old.py"]), [])

    def test_syntax_checked_even_in_legacy(self):
        self.assertIn("A000", self.codes({"src/old.py": "def ("}, ["src/old.py"]))

    def test_blocks_domain_browser_dependency(self):
        self.assertIn("A003", self.codes({"src/domain/rules.py":
            "from playwright.sync_api import Page"}))

    def test_blocks_application_sqlite(self):
        self.assertIn("A003", self.codes({"src/application/jobs.py":
            "import sqlite3 as db"}))

    def test_blocks_relative_import_across_layers(self):
        self.assertIn("A002", self.codes({"src/domain/rules.py":
            "from ..adapters.browser import Browser"}))

    def test_blocks_from_package_import(self):
        self.assertIn("A002", self.codes({"src/application/usecase.py":
            "from src import browser"}))

    def test_blocks_direct_file_io(self):
        self.assertIn("A004", self.codes({"src/domain/rules.py":
            "text = open('file').read()"}))

    def test_blocks_dynamic_import(self):
        self.assertIn("A004", self.codes({"src/application/jobs.py":
            "__import__('sqlite3')"}))

    def test_accepts_application_port_and_adapter(self):
        self.assertEqual(self.inspect({
            "src/application/ports.py": "from typing import Protocol",
            "src/adapters/store.py": "import sqlite3\nfrom ..application.ports import Protocol",
        }), [])

    def test_entrypoint_cannot_use_adapter(self):
        self.assertIn("A002", self.codes({"src/entrypoints/mcp.py":
            "from ..adapters.store import Store"}))

    def test_entrypoint_cannot_use_http_directly(self):
        self.assertIn("A003", self.codes({"src/entrypoints/mcp.py": "import httpx"}))

    def test_entrypoint_can_use_bootstrap(self):
        self.assertEqual(self.inspect({
            "src/entrypoints/cli.py": "from ..bootstrap import make_app",
            "src/bootstrap.py": "def make_app(): pass",
        }), [])

    def test_legacy_requires_named_bridge_directory(self):
        for path, expected in [
            ("src/adapters/legacy/price.py", set()),
            ("src/adapters/price.py", {"A002"}),
        ]:
            self.assertEqual(self.codes({path: "from src import old",
                                         "src/old.py": ""}, ["src/old.py"]), expected)

    def test_cycle_is_detected(self):
        self.assertIn("A005", self.codes({
            "src/domain/a.py": "from . import b",
            "src/domain/b.py": "from . import a",
        }))

    def test_cycle_crossing_legacy_is_detected(self):
        self.assertIn("A005", self.codes({
            "src/adapters/legacy/new.py": "from src import old",
            "src/old.py": "from src.adapters.legacy import new",
        }, ["src/old.py"]))

    def test_legacy_only_cycle_is_grandfathered(self):
        files = {"src/a.py": "from . import b", "src/b.py": "from . import a"}
        self.assertEqual(self.inspect(files, list(files)), [])

    def test_package_init_relative_import(self):
        self.assertIn("A002", self.codes({"src/domain/__init__.py":
            "from .. import browser"}))

    def test_wildcard_legacy_exemption_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "tools").mkdir()
            (root / "tools/architecture_legacy.json").write_text(
                json.dumps({"version": 1, "legacy_files": ["src/**/*.py"]}),
                encoding="utf-8")
            with self.assertRaises(ValueError):
                load_legacy(root)

    def test_flat_wildcard_legacy_exemption_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "tools").mkdir()
            (root / "tools/architecture_legacy.json").write_text(
                json.dumps({"version": 1, "legacy_files": ["src/*.py"]}),
                encoding="utf-8")
            with self.assertRaises(ValueError):
                load_legacy(root)

    def test_command_returns_failure_for_real_violation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "tools").mkdir()
            (root / "src/domain").mkdir(parents=True)
            (root / "tools/architecture_legacy.json").write_text(
                json.dumps({"version": 1, "legacy_files": []}), encoding="utf-8")
            (root / "src/domain/rules.py").write_text("import sqlite3", encoding="utf-8")
            output = io.StringIO()
            with redirect_stdout(output):
                code = main(["--root", str(root)])
            self.assertEqual(code, 1)
            self.assertIn("A003", output.getvalue())


if __name__ == "__main__":
    unittest.main()
