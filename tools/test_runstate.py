"""src/runstate.py 的场景测试。改那个文件之后跑一遍：

    python tools\\test_runstate.py

全在临时目录里跑完就删，不碰用户的 output/。
"""
import json
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
if sys.stdout:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

from src.runstate import RunState, StateMixin   # noqa: E402

# 「文件坏了」那几条会 log.warning(exc_info=True)，那是预期行为
import logging  # noqa: E402
logging.disable(logging.CRITICAL)

PASS, FAIL = [], []


def ok(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  ✓ " if cond else "  ✗ ") + name + (("　" + detail) if detail and not cond else ""))


def tmp():
    d = Path(tempfile.mkdtemp(prefix="runstate_"))
    return d, d / "state.json"


def test_basic():
    print("\n[基本]")
    d, p = tmp()
    try:
        st = RunState(p, "资源位投放")
        ok("新建时是空的", st.done == [] and st.failed == [])
        ok("没跑过的不算 done", not st.is_done("a"))

        st.mark_done("a")
        ok("标记之后立刻写盘（不攒着）", p.exists())
        ok("标了就认得", st.is_done("a"))

        st.mark_done("a")
        ok("重复标记不会出现两条", st.done == ["a"], f"实际 {st.done}")

        st.mark_failed("b", "第二条", "页面上找不到字段")
        ok("失败记了 key/name/error",
           st.failed == [{"key": "b", "name": "第二条", "error": "页面上找不到字段"}])
        ok("失败的不算 done", not st.is_done("b"))
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_reload():
    print("\n[重开]")
    d, p = tmp()
    try:
        st = RunState(p, "资源位投放")
        st.mark_done("单元A")
        st.mark_done("单元B")

        again = RunState(p, "资源位投放")
        ok("重开还认得已完成的", again.is_done("单元A") and again.is_done("单元B"))

        off = RunState(p, "资源位投放", resume=False)
        ok("resume=False 当作从头开始（但不删盘上的）", off.done == [])
        ok("　　盘上的还在", RunState(p, "资源位投放").done == ["单元A", "单元B"])
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_isolation():
    print("\n[按配置类型隔离]")
    d, p = tmp()
    try:
        a = RunState(p, "资源位投放")
        a.mark_done("x")
        b = RunState(p, "价格面板配置")
        ok("另一个配置类型看不到它的断点", not b.is_done("x"))
        b.mark_done("y")

        ok("⚠ 存自己的不能冲掉别人的", RunState(p, "资源位投放").is_done("x"))
        raw = json.loads(p.read_text(encoding="utf-8"))
        ok("盘上按配置类型名分区", set(raw) == {"资源位投放", "价格面板配置"}, f"实际 {set(raw)}")

        b.clear()
        ok("清一个不影响另一个",
           RunState(p, "资源位投放").is_done("x") and not RunState(p, "价格面板配置").done)
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_key_types():
    print("\n[key 的类型]")
    d, p = tmp()
    try:
        st = RunState(p, "价格配置")
        st.mark_done(0)
        st.mark_done(3)
        ok("int 型 key（价格配置用行号）过一遍 JSON 还认得",
           RunState(p, "价格配置").is_done(3))
        ok("　　没标过的不认", not RunState(p, "价格配置").is_done(1))

        st2 = RunState(p, "资源位投放")
        st2.mark_done("开通提示条/新客单元_1")
        ok("带斜杠的字符串 key 也行",
           RunState(p, "资源位投放").is_done("开通提示条/新客单元_1"))
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_broken_file():
    print("\n[文件坏了]")
    d, p = tmp()
    try:
        p.write_text("{这不是 json", encoding="utf-8")
        st = RunState(p, "资源位投放")
        ok("⚠ 断点读不了也不能挡住跑，当作从头开始", st.done == [])
        st.mark_done("a")
        ok("　　还能正常写回去", RunState(p, "资源位投放").is_done("a"))

        p.write_text('["不是字典"]', encoding="utf-8")
        st2 = RunState(p, "资源位投放")
        st2.mark_done("b")
        ok("盘上是个列表也不炸", RunState(p, "资源位投放").is_done("b"))
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_old_format():
    print("\n[老断点文件]")
    d, p = tmp()
    try:
        # 1.0.18 之前 runner/dmp/ab 写的就是这个格式，升上来必须还认得
        p.write_text(json.dumps({
            "价格配置": {"done": [0, 1], "failed": [{"record": 2, "name": "x", "error": "y"}]},
            "DMP延期": {"done": ["12345"], "failed": []},
        }, ensure_ascii=False), encoding="utf-8")
        ok("老格式的 done 直接能用", RunState(p, "价格配置").is_done(1))
        ok("老格式的 DMP 断点也能用", RunState(p, "DMP延期").is_done("12345"))
        ok("老的 failed 条目原样留着（字段名不一样也不丢）",
           RunState(p, "价格配置").failed[0].get("record") == 2)
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_mixin():
    print("\n[StateMixin]")
    d, p = tmp()
    try:
        class FakeRunner(StateMixin):
            def __init__(self):
                self.s = {"state_file": str(p), "resume": True}
                self.f = {"name": "原生商广"}
                self._init_state()

        r = FakeRunner()
        r.state.mark_done("unit-1")
        ok("混进去就有 state", r.state.is_done("unit-1"))
        ok("⚠ clear_state 必须存在（webapp 的「清除断点」无条件调它）",
           callable(getattr(r, "clear_state", None)))
        r.clear_state()
        ok("clear_state 清得掉", not r.state.done and not RunState(p, "原生商广").done)
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_all_runners_have_it():
    print("\n[六个执行器都得有断点]")
    import yaml

    from src import registry
    from src.paths import user_path

    d, p = tmp()
    try:
        s = {"state_file": str(p), "resume": True, "timeout": 15000,
             "screenshot_dir": str(d), "result_file": str(d / "r.csv"),
             "cdp_url": "http://127.0.0.1:9222", "data_file": "", "dry_run": True}
        for q in sorted(user_path("config", "forms").glob("*.yaml")):
            cfg = yaml.safe_load(q.read_text(encoding="utf-8")) or {}
            runner = registry.spec_for(cfg.get("mode")).make_runner(dict(s), cfg, None)
            ok(f"{q.stem}　有 clear_state", callable(getattr(runner, "clear_state", None)))
    finally:
        shutil.rmtree(d, ignore_errors=True)


def main():
    print("=" * 56)
    print("runstate 场景测试")
    print("=" * 56)
    for fn in (test_basic, test_reload, test_isolation, test_key_types,
               test_broken_file, test_old_format, test_mixin, test_all_runners_have_it):
        fn()
    print("\n" + "=" * 56)
    print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    for f in FAIL:
        print("  ✗ " + f)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
