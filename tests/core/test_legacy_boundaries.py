import importlib
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from src.adapters.data_paths import browser_lock
from src.adapters.locking import ResourceBusy
from src.domain.identity import identify_records
from src.ui import BaseUI


class UI(BaseUI):
    wait_seconds = 0
    sample_n = 0
    def log(self, *args): pass
    def _push(self, *args): pass


class LegacyTests(unittest.TestCase):
    def test_all_baseline_capabilities_still_registered(self):
        import json
        from src import registry, formcfg
        baseline = json.loads((Path(__file__).parent / "fixtures/capabilities.json").read_text(encoding="utf-8"))
        self.assertEqual(len(baseline["forms"]), 14)
        for item in baseline["forms"]:
            cfg = formcfg.load(item["name"])
            self.assertEqual(cfg.get("mode", "default"), item["mode"])
            self.assertTrue(callable(registry.spec_for(item["mode"]).make_runner))
        self.assertTrue(callable(registry.spec_for("flow").make_runner))

    def test_readonly_runner_paths_do_not_connect_or_fill(self):
        modules = ("runner", "wizard_runner", "ad_runner", "adv2_runner", "ad_reg_runner",
                   "pp_runner", "pt_runner", "dl_runner", "dmp_runner", "ab_runner",
                   "hh_runner", "nc_runner", "flow_runner")
        from src.runstate import StateMixin
        for module_name in modules:
            with self.subTest(module=module_name):
                module = importlib.import_module("src." + module_name)
                cls = next(c for c in vars(module).values()
                           if isinstance(c, type) and c is not StateMixin and StateMixin in c.__bases__)
                runner = cls.__new__(cls)
                runner.s, runner.f, runner.ui = {"dry_run": True}, {"name": "fixture"}, UI()
                runner._write_results = Mock()
                with patch.object(module, "Browser", side_effect=AssertionError("readonly must not connect")):
                    result = runner.run([{"header": {"名称": "示例"}, "items": []}])
                self.assertEqual(result[0]["状态"], "dry_run")

    def test_aliases_and_distinct_data_directories_share_browser_lock(self):
        with tempfile.TemporaryDirectory() as temp:
            first = {"task_data_dir": temp + "/a", "task_lock_dir": temp,
                     "cdp_url": "http://127.0.0.1:9222"}
            second = {**first, "task_data_dir": temp + "/b", "cdp_url": "http://localhost:9222/"}
            with browser_lock(first):
                with self.assertRaises(ResourceBusy):
                    browser_lock(second).acquire()
            with browser_lock(second):
                pass

    def test_gui_repeated_start_load_retry_are_rejected_while_running(self):
        from src.webapp import Api
        api = Api.__new__(Api)
        api.settings = {}
        api._command_lock = threading.RLock()
        api.worker = None
        api._window = None
        api.form_name, api.scope = "fixture", None
        api._record_run = Mock()
        api._sync_sheet_async = Mock()
        api._failed_summary = lambda: {}
        started, release = threading.Event(), threading.Event()
        calls = []
        def run(records):
            calls.append(records)
            started.set()
            release.wait(3)
            return []
        api.runner = SimpleNamespace(s={}, managed_host=True, run=run)
        rows = [SimpleNamespace(payload={"header": {}, "items": []})]
        with patch("src.webapp.WebUI", return_value=UI()):
            try:
                self.assertTrue(api._run(rows, "dry")["ok"])
                self.assertTrue(started.wait(1))
                self.assertFalse(api._run(rows, "dry")["ok"])
                self.assertFalse(api.load_and_check("other", None, None)["ok"])
                self.assertFalse(api.retry_rows([1], "auto")["ok"])
            finally:
                release.set()
                api.worker.join(3)
        self.assertEqual(len(calls), 1)

    def test_authorization_preflight_failure_is_not_eligibility(self):
        from src.hh_runner import HhRunner
        from src.nc_runner import NcRunner
        from src.runstate import RunState
        with tempfile.TemporaryDirectory() as temp:
            for cls in (HhRunner, NcRunner):
                with self.subTest(runner=cls.__name__):
                    runner = cls.__new__(cls)
                    runner.s, runner.f, runner.ui = {}, {}, UI()
                    runner.state = RunState(Path(temp) / (cls.__name__ + ".json"), "test")
                    item = {"bvid": "BVfixture", "issues": [], "time": "2030-01-01", "range": "示例"}
                    runner._check_status = Mock(side_effect=TimeoutError())
                    with patch("src.hh_data.load", return_value=[item]):
                        rows = runner.preview()
                    self.assertIn("资格未知", rows[0].issues[0])

    def test_scaffold_only_adds_layered_unregistered_modules(self):
        from tools.new_mode import scaffold
        files = scaffold("优惠券配置", "coupon")
        self.assertTrue(all(not p.startswith("src/") or len(Path(p).parts) >= 3 for p in files))
        self.assertNotIn("src/registry.py", files)
        for path, content in files.items():
            if path.endswith(".py"):
                compile(content, path, "exec")
        with self.assertRaises(ValueError):
            scaffold("bad", "../escape")
