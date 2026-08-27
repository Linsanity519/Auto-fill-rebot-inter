"""入口：默认开图形界面，加 --cli 走命令行。"""
import argparse
import logging
import sys
import time
from pathlib import Path

import yaml

from src import settings as settings_defaults
from src.paths import app_dir, user_path

ROOT = app_dir()
FORMS_DIR = user_path("config", "forms")


def setup_logging(log_file: str):
    Path(log_file).parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
        handlers=[logging.FileHandler(log_file, encoding="utf-8")],
    )


def load_form(name: str | None) -> dict:
    available = sorted(p.stem for p in FORMS_DIR.glob("*.yaml"))
    if not available:
        raise SystemExit(f"{FORMS_DIR} 下没有任何表单配置")

    if not name:
        if len(available) == 1:
            name = available[0]
        else:
            print("有多个配置类型，请选择：")
            for i, n in enumerate(available, 1):
                print(f"  {i}. {n}")
            choice = input("序号或名称：").strip()
            name = available[int(choice) - 1] if choice.isdigit() else choice

    if not (FORMS_DIR / f"{name}.yaml").exists():
        raise SystemExit(f"找不到配置「{name}」，可选：{available}")

    from src import formcfg
    cfg = formcfg.load(name)
    # 命令行下顺手把 yaml 的问题说出来 —— 界面版有「载入并检查」，命令行没有
    errs, warns = formcfg.validate(cfg, name)
    for m in errs:
        print("✗ " + m)
    for m in warns:
        print("⚠ " + m)
    if errs:
        raise SystemExit(f"配置有问题，先修好再跑（详细自检：python tools\\check_mode.py {name}）")
    return cfg


def main():
    ap = argparse.ArgumentParser(description="大会员业务后台 配置助手")
    ap.add_argument("--cli", action="store_true", help="走命令行，不开图形界面")
    ap.add_argument("--tk", action="store_true",
                    help="走旧版 tkinter 界面。⚠ 它没有「准备」页/策略中心/活动选择，"
                         "这几样只能先在默认界面里配好；不支持的配置类型开进去会提示")
    ap.add_argument("--form", help="配置类型（config/forms/ 下的文件名）")
    ap.add_argument("--settings", default="config/settings.yaml")
    ap.add_argument("--data", help="覆盖数据文件")
    ap.add_argument("--dry-run", action="store_true", help="只填不提交")
    ap.add_argument("--auto", action="store_true", help="不逐条确认，连续提交")
    ap.add_argument("--no-resume", action="store_true", help="忽略断点，从头跑")
    ap.add_argument("--make-template", action="store_true", help="只生成 Excel 模板然后退出")
    ap.add_argument("--positions", help="wizard 模式：要配置的资源位，逗号分隔")
    ap.add_argument("--activity-id",
                    help="wizard 模式：挂到已有活动的活动ID。给了就不新建活动，"
                         "生成模板时也不带「活动」sheet")
    ap.add_argument("--activity-type", default="5",
                    help="wizard 模式：配合 --activity-id 用的活动类型ID，默认 5（测试验收）")
    ap.add_argument("--scope", choices=["active", "mine", "id_list"],
                    help="延期范围。DMP延期：active=全部生效中(默认) / mine=我创建的 / "
                         "id_list=按清单指定人群ID；AB实验延期：mine=我的实验(默认) / "
                         "id_list=按清单指定实验ID（AB 不支持 active）")
    args = ap.parse_args()

    settings_path = Path(args.settings)
    if not settings_path.is_absolute():
        settings_path = ROOT / settings_path
    settings = settings_defaults.apply_defaults(
        yaml.safe_load(settings_path.read_text(encoding="utf-8")))
    settings.setdefault("_root", str(ROOT))
    for key in ("state_file", "result_file", "log_file", "screenshot_dir", "data_file"):
        if settings.get(key) and not Path(settings[key]).is_absolute():
            settings[key] = str(ROOT / settings[key])
    setup_logging(settings["log_file"])

    if args.make_template:
        from src import registry

        names = [args.form] if args.form else sorted(p.stem for p in FORMS_DIR.glob("*.yaml"))
        for n in names:
            cfg = load_form(n)
            if cfg.get("mode") == "wizard":
                from src import wizard_schema as W
                from src import wizard_template as WT

                avail = W.position_names(cfg)
                picked = [p.strip() for p in (args.positions or "").split(",") if p.strip()]
                if not picked:
                    print(f"「{n}」需要指定资源位。可选：")
                    for k, name in enumerate(avail, 1):
                        print(f"  {k}. {name}")
                    raw = input("填序号或名称，逗号分隔（回车=全部）：").strip()
                    if not raw:
                        picked = avail
                    else:
                        picked = [avail[int(x) - 1] if x.strip().isdigit() else x.strip()
                                  for x in raw.split(",")]
                print("已生成：" + WT.build(cfg, picked,
                                            existing_activity=bool(args.activity_id)))
                continue

            spec = registry.spec_for(cfg.get("mode"))
            if registry.scopes_for(cfg) and (args.scope or cfg.get("scope")) != "id_list":
                print(f"「{n}」{spec.no_template_hint_cli}")
                continue
            # 有的 mode 压根不吃 Excel（比如抢会议室），build_template 是 None
            if spec.build_template is None:
                print(f"「{n}」{spec.no_template_hint_cli or '不需要 Excel 模板'}")
                continue
            print("已生成：" + spec.build_template(n))
        return

    if args.tk:
        from src.gui import main as gui_main

        gui_main()
        return

    if not args.cli:
        from src.webapp import main as web_main

        web_main()
        return

    from src.ui import ConsoleUI

    form_cfg = load_form(args.form)
    if args.data:
        settings["data_file"] = args.data
    if args.dry_run:
        settings["dry_run"] = True
    if args.no_resume:
        settings["resume"] = False
    if args.scope:
        # 两个执行器各读各的键，互不干扰
        settings["dmp_scope"] = args.scope
        settings["ab_scope"] = args.scope
    if args.activity_id:
        settings["wizard_activity"] = {"existing": True, "activity_id": args.activity_id,
                                       "activity_type": args.activity_type}

    try:
        from src import registry

        runner = registry.spec_for(form_cfg.get("mode")).make_runner(
            settings, form_cfg, ConsoleUI(auto=args.auto))
        runner.auto = args.auto

        rows = runner.preview()
        bad = [r for r in rows if r.issues]
        for r in bad:
            print(f"  跳过第{r.index}条「{r.name}」：{'；'.join(r.issues)}")
        if bad:
            print(f"共 {len(bad)} 条未通过校验，已跳过")

        # 和图形界面保持一致：校验不过的不去撞墙
        records = [r.payload for r in rows if not r.issues]
        if not records:
            print("没有可执行的记录")
            return

        # 埋点：和界面版走同一套口径，见 src/usage.py
        from src import usage

        t0 = time.monotonic()
        results = []
        ui = runner.ui
        try:
            results = runner.run(records)
        finally:
            usage.record(
                settings, "run_finished",
                run_id=usage.new_run_id(), form=form_cfg.get("name", ""),
                mode=("dry" if settings.get("dry_run") else ("auto" if args.auto else "confirm")),
                scope=args.scope, total=len(records),
                seconds=round(time.monotonic() - t0, 1), entry="cli",
                wait_seconds=round(getattr(ui, "wait_seconds", 0.0), 1),
                chrome=usage.chrome_version(settings.get("cdp_url")),
                **usage.count_status(results),
                **usage.fail_detail(results),
            )
    except KeyboardInterrupt:
        print("\n已中断。下次运行会从断点继续。")
        sys.exit(1)
    except Exception as e:
        print(f"\n出错：{e}")
        logging.exception("fatal")
        sys.exit(1)


if __name__ == "__main__":
    if sys.stdout:
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass
    main()
