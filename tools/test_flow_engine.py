"""src/flow_data.py 的离线测试（不联网、不开浏览器）。

    python tools\\test_flow_engine.py
"""
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
if sys.stdout:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

logging.disable(logging.CRITICAL)

from src import flow_data as FD          # noqa: E402
from src import registry                 # noqa: E402

PASS, FAIL = [], []


def ok(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  ✓ " if cond else "  ✗ ") + name + (("　" + str(detail)) if detail and not cond else ""))


LOOP = {
    "name": "加时", "data": {"source": "excel", "columns": ["单元名", "加时天数"]},
    "source_url": "http://host/edit",
    "steps": [
        {"op": "goto", "url": "{{source_url}}"},
        {"op": "loop_rows", "body": [
            {"op": "click", "pick": [{"text": "新建"}, {"role": "button", "name": "新建"}]},
            {"op": "fill", "pick": [{"label": "单元名称"}], "value": "{{单元名}}"},
            {"op": "fill", "pick": [{"label": "加时天数"}], "value": "{{加时天数}}"},
            {"op": "wait_text", "text": "校验通过"},
            {"op": "confirm", "note": "核对"},
            {"op": "click", "pick": [{"text": "确 定"}], "submit": True},
            {"op": "assert", "gone": {"text": "确 定"}},
        ]},
    ],
}


def test_render():
    print("\n[变量替换]")
    ok("{{列名}} 换成行里的值",
       FD.render("天 {{加时天数}}", {"加时天数": "7"}) == "天 7")
    ok("{{source_url}} 是特殊变量",
       FD.render("{{source_url}}", {}, "http://x") == "http://x")
    ok("列不存在时原样留着（好让 validate 抓到）",
       FD.render("{{没有}}", {"a": "1"}) == "{{没有}}")
    ok("没有变量的文本原样",
       FD.render("确 定", {"确": "x"}) == "确 定")


def test_columns():
    print("\n[要哪几列]")
    ok("显式 data.columns 优先", FD.columns(LOOP) == ["单元名", "加时天数"])
    scanned = {"name": "t", "data": {"source": "excel"}, "steps": [
        {"op": "loop_rows", "body": [{"op": "fill", "pick": [{"label": "x"}], "value": "{{甲}}{{乙}}"}]}]}
    ok("没写 columns 就从 {{}} 扫", FD.columns(scanned) == ["乙", "甲"], FD.columns(scanned))


def test_validate_ok():
    print("\n[校验：好的]")
    ok("结构完整的 loop 流程没问题", FD.validate(LOOP) == [], FD.validate(LOOP))
    single = {"name": "s", "data": {"source": "none"}, "steps": [
        {"op": "goto", "url": "http://x"},
        {"op": "click", "pick": [{"text": "开始"}]},
        {"op": "wait_text", "text": "完成"},
    ]}
    ok("不吃 Excel 的单段流程也行", FD.validate(single) == [], FD.validate(single))


def test_validate_catches():
    print("\n[校验：该抓到的]")

    def has(issues, frag):
        return any(frag in x for x in issues)

    ok("空流程", FD.validate({"name": "x", "steps": []}) == ["这个工作流一步都没有"])

    # 脆弱选择器：不是硬伤（不进 validate），是提醒（进 warnings）——
    # 录下来的流程本来就该能复原、能重复，改版会失效是所有前端自动化的通病
    css_only = {"name": "x", "data": {"source": "none"}, "steps": [
        {"op": "click", "pick": [{"css": "div > input"}]}]}
    ok("非唯一 css → 不算硬伤", FD.validate(css_only) == [], FD.validate(css_only))
    ok("非唯一 css → 进 warnings", has(FD.warnings(css_only), "定位不唯一"))

    anchored = {"name": "x", "data": {"source": "none"}, "steps": [
        {"op": "click", "pick": [{"css": "#bar > button:nth-child(2)", "anchored": True}]}]}
    ok("唯一 css（anchored）→ 连提醒都没有", FD.warnings(anchored) == [], FD.warnings(anchored))

    unbound = {"name": "x", "data": {"source": "excel", "columns": ["甲"]}, "steps": [
        {"op": "loop_rows", "body": [{"op": "fill", "pick": [{"label": "x"}], "value": "{{乙}}"}]}]}
    ok("用了没声明的列", has(FD.validate(unbound), "没在某一步标成「按表格」"))

    noloop = {"name": "x", "data": {"source": "excel", "columns": ["甲"]}, "steps": [
        {"op": "fill", "pick": [{"label": "x"}], "value": "{{甲}}"}]}
    ok("用了 {{}} 但没 loop_rows", has(FD.validate(noloop), "没勾"))

    nocfm = {"name": "x", "data": {"source": "none"}, "steps": [
        {"op": "click", "pick": [{"text": "a"}], "submit": True}]}
    ok("有提交但没 confirm → 硬伤里没有", FD.validate(nocfm) == [], FD.validate(nocfm))
    ok("有提交但没 confirm → 进 warnings", has(FD.warnings(nocfm), "停下核对"))

    badop = {"name": "x", "steps": [{"op": "teleport"}]}
    ok("不认识的动作", has(FD.validate(badop), "不认识的动作"))

    emptypick = {"name": "x", "data": {"source": "none"}, "steps": [{"op": "click", "pick": []}]}
    ok("click 没 pick", has(FD.validate(emptypick), "没有选择器"))

    noval = {"name": "x", "data": {"source": "none"}, "steps": [
        {"op": "fill", "pick": [{"text": "x"}]}]}
    ok("fill 没 value", has(FD.validate(noval), "没有要填的值"))

    missing_col = {"name": "x", "data": {"source": "excel", "columns": ["甲"]}, "steps": [
        {"op": "loop_rows", "body": [{"op": "fill", "pick": [{"label": "x"}], "value": "{{甲}}"}]}]}
    ok("Excel 里缺列", has(FD.validate(missing_col, rows=[{"乙": "1"}]), "Excel 里缺这几列"))


def test_synthetic_and_registry():
    print("\n[接线]")
    cfg = FD.synthetic_cfg(LOOP)
    ok("mode 恒为 flow", cfg["mode"] == "flow")
    ok("有 loop → 吃 Excel", cfg["data_source"] == "excel")
    ok("归到《自制配置类型》组", cfg["nav"]["group"] == FD.GROUP)
    ok("原件挂在 _flow 上", cfg["_flow"]["name"] == "加时")
    single = FD.synthetic_cfg({"name": "s", "data": {"source": "none"}, "steps": [{"op": "goto", "url": "x"}]})
    ok("没 loop → 不吃 Excel", single["data_source"] == "none")

    spec = registry.spec_for("flow")
    r = spec.make_runner({"screenshot_dir": ".", "state_file": "x.json", "resume": False,
                          "cdp_url": "", "timeout": 1, "result_file": "r.csv"}, cfg, None)
    ok("registry 造得出 FlowRunner", type(r).__name__ == "FlowRunner")
    ok("有 clear_state（webapp 无条件调）", callable(getattr(r, "clear_state", None)))


def test_step_mode():
    print("\n[逐步试跑]")
    from src.flow_runner import FlowRunner
    base = {"screenshot_dir": ".", "state_file": "x.json", "resume": False,
            "cdp_url": "", "timeout": 1, "result_file": "r.csv"}
    cfg = FD.synthetic_cfg(LOOP)
    r = FlowRunner(dict(base), cfg, None)
    ok("默认不是逐步", r._step_mode is False)
    r2 = FlowRunner(dict(base, flow_step=True), cfg, None)
    ok("settings.flow_step → 逐步", r2._step_mode is True)
    d = FlowRunner._step_desc({"op": "fill", "field": "单元名称", "value": "{{单元名}}"},
                              2, {"单元名": "甲乙"}, "http://x")
    ok("步骤描述里带上了行的值和字段名", "甲乙" in d and "单元名称" in d, d)
    d2 = FlowRunner._step_desc({"op": "goto", "url": "{{source_url}}"}, 1, {}, "http://host/x")
    ok("goto 描述里把 {{source_url}} 渲染开", "http://host/x" in d2, d2)
    d3 = FlowRunner._step_desc({"op": "search_pick", "field": "人群分组ID",
                               "query": "白", "value": "运营白名单251203"}, 3, {}, "")
    ok("search_pick 描述里有字段 / 搜索词 / 目标值",
       "人群分组ID" in d3 and "白" in d3 and "运营白名单251203" in d3, d3)


def test_semantic_ops():
    print("\n[语义步骤 select / search_pick / pick_item]")

    def has(issues, frag):
        return any(frag in x for x in issues)

    good = {"name": "x", "data": {"source": "none"}, "steps": [
        {"op": "goto", "url": "http://x"},
        {"op": "select", "field": "人群选组", "value": "指定人群包（离线数据）"},
        {"op": "search_pick", "field": "人群分组ID", "query": "白",
         "value": "运营白名单251203", "pick": [{"label": "人群分组ID"}]},
        {"op": "pick_item", "field": "投放展示位置", "value": "播放页催费条",
         "pick": [{"css": "table", "anchored": True}]},
        {"op": "check", "field": "生效平台", "value": "Android", "checked": True},
        {"op": "check", "field": "生效内容", "value": "全部", "checked": True},
    ]}
    ok("语义步骤都在 OPS 里、结构没硬伤", FD.validate(good) == [], FD.validate(good))
    ok("select 有 field 就不报「没有选择器」", not has(FD.validate(good), "没有选择器"))
    ok("check 靠 value 也能定位，没 pick 没 field 也不算硬伤",
       FD.validate({"name": "x", "data": {"source": "none"},
                    "steps": [{"op": "check", "value": "Android", "checked": True}]}) == [])
    ok("check 没 value → 硬伤",
       has(FD.validate({"name": "x", "data": {"source": "none"},
                        "steps": [{"op": "check", "field": "生效平台"}]}), "没有要勾的值"))
    from src.flow_runner import FlowRunner
    dc = FlowRunner._step_desc({"op": "check", "field": "生效平台", "value": "Android", "checked": True}, 5, {}, "")
    ok("check 描述：勾「Android」", "勾「Android」" in dc and "生效平台" in dc, dc)

    noval = {"name": "x", "data": {"source": "none"}, "steps": [
        {"op": "select", "field": "人群选组"}]}
    ok("select 没 value → 硬伤", has(FD.validate(noval), "没有要选的值"))

    nofield = {"name": "x", "data": {"source": "none"}, "steps": [
        {"op": "pick_item", "value": "某行"}]}
    ok("pick_item 既没 pick 又没 field → 硬伤", has(FD.validate(nofield), "既没有选择器"))

    # {{列}} 也要能从 query 里扫出来
    bound = {"name": "x", "data": {"source": "excel", "columns": ["名单"]}, "steps": [
        {"op": "loop_rows", "body": [
            {"op": "search_pick", "field": "ID", "query": "{{名单}}", "value": "{{名单}}",
             "pick": [{"label": "ID"}]}]}]}
    ok("search_pick 的 query 里的 {{列}} 认得出", FD.columns(bound) == ["名单"], FD.columns(bound))


def test_rec_session_import():
    print("\n[录制会话]")
    from src.webapp import _FlowRecSession
    s = _FlowRecSession("http://127.0.0.1:9222", 1000, "")
    snap = s.snapshot()
    ok("没 start 时 running=True 之前是啥都行，关键是 steps=0", snap["steps"] == 0)
    ok("有独立线程、默认没起", not s._thread.is_alive())
    ok("会话上有 form_state / form_url 两个字段", s.form_state == [] and s.form_url == "")


def test_snapshot_fields():
    print("\n[整表快照 → reconcile 输入]")
    doc = {"name": "复刻单元", "data": {"source": "none"},
           "steps": [{"op": "goto", "url": "http://x"},
                     {"op": "check", "field": "生效平台", "value": "Android", "checked": True}],
           "snapshot": {"captured_at": "2026-08-31 12:00:00", "url": "http://x/unit/0", "fields": [
               {"field": "投放流量池", "kind": "radio", "value": "特殊最优池(慎重使用)"},
               {"field": "生效平台", "kind": "checkbox", "value": ["Android", "iPhone"]},
               {"field": "版本限制", "kind": "select", "value": "不限"},
               {"field": "单元名称", "kind": "text", "value": "复刻v1"},
               {"field": "", "kind": "text", "value": "没有字段名的丢掉"},
               {"field": "空值字段", "kind": "text", "value": ""},
           ]}}
    got = FD.snapshot_fields(doc)
    ok("清洗后 4 条（丢掉没 field / 没 value 的）", len(got) == 4, got)
    ok("radio 值是字符串", any(g["field"] == "投放流量池" and g["value"] == "特殊最优池(慎重使用)" for g in got))
    ok("checkbox 值是列表", any(g["field"] == "生效平台" and g["value"] == ["Android", "iPhone"] for g in got))

    off = dict(doc, reconcile=False)
    ok("关了「回放后对齐整表」→ 空", FD.snapshot_fields(off) == [])

    looped = {"name": "l", "data": {"source": "excel", "columns": ["名"]},
              "steps": [{"op": "loop_rows", "body": [
                  {"op": "fill", "pick": [{"label": "x"}], "value": "{{名}}"}]}],
              "snapshot": {"fields": [{"field": "版本限制", "kind": "select", "value": "不限"}]}}
    ok("吃 Excel 的循环流程 → 不对齐（每行值本就不同）", FD.snapshot_fields(looped) == [])

    ok("describe 里带出快照字段数",
       "对齐整表 4 字段" in FD.describe(doc), FD.describe(doc))
    ok("_defaults 给 reconcile 兜底 True", FD._defaults({"name": "x"}).get("reconcile") is True)


def main():
    print("=" * 56)
    print("flow 引擎 离线测试")
    print("=" * 56)
    for fn in (test_render, test_columns, test_validate_ok, test_validate_catches,
               test_synthetic_and_registry, test_step_mode, test_semantic_ops,
               test_rec_session_import, test_snapshot_fields):
        fn()
    print("\n" + "=" * 56)
    print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    for f in FAIL:
        print("  ✗ " + f)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
