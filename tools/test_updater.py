"""更新器的离线测试：造一个假的安装目录，把各种更新场景跑一遍。

跑法：python tools\\test_updater.py

⚠ 这个文件测的是「更新会不会弄坏同事的机器」，出问题的代价是一屋子人的
  策略配置没了 —— build.bat 里那条注释记过一次同类事故。改 updater.py 之前
  先看这里。
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import updater  # noqa: E402


def check(label: str, cond: bool) -> None:
    if not cond:
        raise AssertionError(label)
    print(f"OK  {label}")


def make_app(root: Path) -> Path:
    """造一个「已装好的老版本」，含用户自己的数据。"""
    app = root / "app"
    (app / "src").mkdir(parents=True)
    (app / "src" / "__init__.py").write_text("__version__='1.1.0'", encoding="utf-8")
    (app / "src" / "goner.py").write_text("# 新版本里被删掉的模块", encoding="utf-8")
    (app / "assets" / "webui").mkdir(parents=True)
    (app / "assets" / "webui" / "index.html").write_text("OLD", encoding="utf-8")
    (app / "main.py").write_text("OLD MAIN", encoding="utf-8")
    (app / "runtime.txt").write_text("1", encoding="utf-8")

    # 随版本发布的内容
    (app / "config" / "forms").mkdir(parents=True)
    (app / "config" / "forms" / "价格配置.yaml").write_text("old: form", encoding="utf-8")
    (app / "config" / "forms" / "已删除的表单.yaml").write_text("gone", encoding="utf-8")
    (app / "config" / "team.json").write_text('{"totals":"old"}', encoding="utf-8")

    # 用户自己的东西 —— 更新绝对不能碰
    (app / "config" / "strategies").mkdir(parents=True)
    (app / "config" / "strategies" / "我的方案.json").write_text('{"mine":1}', encoding="utf-8")
    (app / "config" / "prep").mkdir(parents=True)
    (app / "config" / "prep" / "原生商广.json").write_text('{"bid":"3.5"}', encoding="utf-8")
    (app / "config" / "settings.yaml").write_text("nickname: 老王", encoding="utf-8")
    (app / "config" / "webhook.txt").write_text("https://example/hook", encoding="utf-8")
    (app / "data").mkdir()
    (app / "data" / "我的数据.xlsx").write_text("xlsx", encoding="utf-8")
    (app / "output").mkdir()
    (app / "output" / "usage.jsonl").write_text("埋点", encoding="utf-8")
    (app / ".chrome-profile").mkdir()
    (app / ".chrome-profile" / "Cookies").write_text("登录态", encoding="utf-8")
    return app


def make_payload(path: Path, *, complete: bool = True, evil: bool = False) -> None:
    with zipfile.ZipFile(path, "w") as z:
        if evil:
            z.writestr("../../evil.dll", "x")
            return
        z.writestr("main.py", "NEW MAIN")
        z.writestr("src/__init__.py", "__version__='2.0.0'")
        z.writestr("config/forms/价格配置.yaml", "new: form")
        z.writestr("config/team.json", '{"totals":"new"}')
        z.writestr("payload.json", json.dumps({"version": "2.0.0", "min_runtime": 1}))
        if complete:
            z.writestr("assets/webui/index.html", "NEW")


def user_data_intact(app: Path) -> bool:
    return (
        (app / "config" / "strategies" / "我的方案.json").read_text(encoding="utf-8") == '{"mine":1}'
        and (app / "config" / "prep" / "原生商广.json").read_text(encoding="utf-8") == '{"bid":"3.5"}'
        and (app / "config" / "settings.yaml").read_text(encoding="utf-8") == "nickname: 老王"
        and (app / "config" / "webhook.txt").read_text(encoding="utf-8") == "https://example/hook"
        and (app / "data" / "我的数据.xlsx").exists()
        and (app / "output" / "usage.jsonl").read_text(encoding="utf-8") == "埋点"
        and (app / ".chrome-profile" / "Cookies").exists()
        and (app / "runtime.txt").read_text(encoding="utf-8") == "1"
    )


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="formbot-updater-"))
    try:
        updater._launch_app = lambda *a, **k: None      # 测试里不真启动 exe
        errors: list[str] = []
        updater._error = lambda m: errors.append(m)

        app = make_app(tmp)
        log = tmp / "update-run.log"

        # ---- 1. 跨版本一步到位（1.1.0 -> 2.0.0）----
        good = tmp / "good.zip"
        make_payload(good)
        check("跨版本更新成功", updater.apply_payload(good, app, log) == 0)
        check("代码已换新", (app / "main.py").read_text(encoding="utf-8") == "NEW MAIN")
        check("被删掉的模块真的消失了", not (app / "src" / "goner.py").exists())
        check("表单定义跟着更新了",
              (app / "config" / "forms" / "价格配置.yaml").read_text(encoding="utf-8") == "new: form")
        check("被删掉的表单定义也消失了",
              not (app / "config" / "forms" / "已删除的表单.yaml").exists())
        check("团队统计快照已刷新",
              (app / "config" / "team.json").read_text(encoding="utf-8") == '{"totals":"new"}')
        check("★ 用户的策略/准备参数/数据/登录态全都没动", user_data_intact(app))
        check("成功后备份已清理", not (app / ".backup").exists())

        # ---- 2. 坏包自动回滚 ----
        bad = tmp / "bad.zip"
        make_payload(bad, complete=False)       # 缺 index.html
        errors.clear()
        check("坏包被拒绝", updater.apply_payload(bad, app, log) == 4)
        check("已回滚到上一版", (app / "main.py").read_text(encoding="utf-8") == "NEW MAIN")
        check("回滚后表单定义仍是上一版",
              (app / "config" / "forms" / "价格配置.yaml").read_text(encoding="utf-8") == "new: form")
        check("回滚后用户数据依然完好", user_data_intact(app))
        check("回滚有明确提示", errors and "已自动恢复" in errors[0])

        # ---- 3. 目录穿越 ----
        evil = tmp / "evil.zip"
        make_payload(evil, evil=True)
        errors.clear()
        check("目录穿越被拒绝", updater.apply_payload(evil, app, log) == 4)
        check("穿越后用户数据依然完好", user_data_intact(app))

        # ---- 4. 越权内容（想借代码包覆盖用户配置）----
        sneaky = tmp / "sneaky.zip"
        with zipfile.ZipFile(sneaky, "w") as z:
            z.writestr("main.py", "x")
            z.writestr("config/strategies/我的方案.json", '{"hijacked":1}')
        errors.clear()
        check("代码包想动 config/strategies 会被拒", updater.apply_payload(sneaky, app, log) == 4)
        check("★ 策略配置没被劫持",
              (app / "config" / "strategies" / "我的方案.json").read_text(encoding="utf-8") == '{"mine":1}')

        print("\n全部通过")
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
