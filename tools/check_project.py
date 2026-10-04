"""Run reproducible check selection, with no live platform calls or publishing."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import platform
import runpy
import subprocess
import sys

ROOT = Path(__file__).resolve().parent.parent
OFFLINE_TESTS = (
    "test_ab_runner", "test_collect", "test_dates", "test_dl_ledger",
    "test_dl_runner", "test_dmp_runner", "test_fill_core", "test_flow_engine",
    "test_formcfg", "test_health", "test_manifest", "test_meeting",
    "test_pt_ledger", "test_pt_runner", "test_pt_strategy", "test_runstate",
    "test_usage", "test_xlsx_diff",
)
ENVIRONMENT_TESTS = {
    "test_task_host_process": "isolated fake-platform subprocess acceptance; run separately for host changes",
    "test_update": "starts a local HTTP server; run separately when update code changes",
    "test_updater": "launches updater processes; requires an isolated update fixture",
    "test_filler_locate": "requires a browser executable and page fixtures",
    "test_ad_reg_titles": "requires a browser executable",
}


def child(target: str) -> int:
    platform.system()  # Cache Windows version lookup before blocking processes.
    sys.path.insert(0, str(ROOT))
    blocked = {"socket.connect", "socket.bind", "subprocess.Popen",
               "os.system", "os.startfile", "os.posix_spawn"}

    def audit(event: str, _args: tuple) -> None:
        if event in blocked:
            raise RuntimeError(f"Offline check blocked an external effect: {event}")

    sys.addaudithook(audit)
    if target in {"unittest", "governance"}:
        start = "tests/governance" if target == "governance" else "tests"
        sys.argv = ["unittest", "discover", "-s", start, "-p", "test_*.py", "-v"]
        runpy.run_module("unittest", run_name="__main__")
    else:
        known = {"tools/check_architecture.py", "tools/check_mode.py"}
        known |= {f"tools/{name}.py" for name in OFFLINE_TESTS}
        if target not in known:
            raise ValueError("unknown check target")
        sys.argv = [str(ROOT / target)]
        runpy.run_path(str(ROOT / target), run_name="__main__")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--static-only", action="store_true")
    parser.add_argument("--child", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.child:
        return child(args.child)
    expected = set(OFFLINE_TESTS) | set(ENVIRONMENT_TESTS)
    actual = {p.stem for p in (ROOT / "tools").glob("test_*.py")}
    if actual != expected:
        print("Check inventory mismatch. Unregistered: " + ", ".join(sorted(actual - expected)))
        print("Missing: " + ", ".join(sorted(expected - actual)))
        return 1
    logs = ROOT / "output" / "quality"
    temporary = logs / "tmp"
    temporary.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8",
               TEMP=str(temporary), TMP=str(temporary),
               CONFIG_ASSISTANT_HOME=str(temporary / "runtime"),
               CONFIG_ASSISTANT_LOCK_HOME=str(temporary / "locks"))
    targets = ["tools/check_architecture.py", "governance" if args.static_only else "unittest"]
    if not args.static_only:
        targets += ["tools/check_mode.py"] + [f"tools/{name}.py" for name in OFFLINE_TESTS]
    results = []
    for target in targets:
        command = [sys.executable, "-X", "utf8", str(Path(__file__).resolve()),
                   "--child", target]
        try:
            result = subprocess.run(command, cwd=ROOT, env=env, timeout=90,
                                    capture_output=True, text=True, encoding="utf-8",
                                    errors="replace")
            code, output = result.returncode, result.stdout + result.stderr
        except subprocess.TimeoutExpired:
            code, output = 124, "Check timed out after 90 seconds."
        log = logs / (Path(target).stem + ".log")
        log.write_text(output, encoding="utf-8")
        results.append({"check": target, "exit_code": code, "log": log.relative_to(ROOT).as_posix()})
        print(f"{'PASS' if code == 0 else 'FAIL'} {target}", flush=True)
        if code:
            print("\n".join(output.splitlines()[-25:]), flush=True)
    report = {"mode": "static" if args.static_only else "offline",
              "checks": results, "not_run_environment_tests": ENVIRONMENT_TESTS}
    (logs / "summary.json").write_text(json.dumps(report, ensure_ascii=False, indent=2),
                                       encoding="utf-8")
    failed = sum(row["exit_code"] != 0 for row in results)
    print(f"Result: {len(results) - failed}/{len(results)} checks passed; "
          f"{len(ENVIRONMENT_TESTS)} environment-dependent tests NOT run.")
    return int(bool(failed))


if __name__ == "__main__":
    raise SystemExit(main())
