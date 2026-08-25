# -*- coding: utf-8 -*-
"""只调创意层：连到一个已存在单元的创意页，把创意字段填一遍，不保存、不建任何东西。

用法：
    python tools/试创意.py <资源位> <unitId> [position] [--save] [--times N]

例：
    python tools/试创意.py PC会员中心banner 40493
    python tools/试创意.py PC会员中心banner 40493 15 --save
    python tools/试创意.py 播放页催费条 40699 --times 8

不加 --save 就只填不保存，可以反复跑同一个单元，不会产生新数据。

--times N 连着填 N 条创意（自动「+添加创意」再切页签），每条报耗时。
用来量「多条创意慢不慢」：单元层和创意页加载只算一次，一眼看出单条成本。
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8")

import yaml
from playwright.sync_api import sync_playwright

from src import wizard_data as D
from src import wizard_schema as W
from src.wizard_filler import WizardFiller
from src.wizard_runner import WizardRunner

CDP = "http://127.0.0.1:9222"
ROOT = Path(__file__).resolve().parent.parent

# 三套创意页的直连地址
URLS = {
    "v1": "https://manager.bilibili.co/v3/#/vip/resource-delivery/originality/detail"
          "?unitId={uid}&selectedUnitId={uid}&position={pos}&back=%2Fvip%2Fresource-delivery%2Foriginality%2F",
    "v2": "https://manager.bilibili.co/v3/#/vip/resource-delivery/originality/detail-v2"
          "?unitId={uid}&selectedUnitId={uid}&position={pos}&back=%2Fvip%2Fresource-delivery%2Foriginality%2F",
    "新版": "https://rich-vip.bilibili.co/delivery/creativity?unitId={uid}&position={pos}&back=%2Funit%2F0",
}


def main():
    argv = sys.argv[1:]
    times = 1
    if "--times" in argv:
        i = argv.index("--times")
        times = max(int(argv[i + 1]), 1)
        argv = argv[:i] + argv[i + 2:]
    args = [a for a in argv if not a.startswith("--")]
    save = "--save" in sys.argv
    if not args:
        print(__doc__)
        return
    position = args[0]
    uid = args[1] if len(args) > 1 else None
    if not uid:
        print("要给 unitId")
        return

    cfg = yaml.safe_load((ROOT / "config/forms/资源位投放.yaml").read_text(encoding="utf-8"))
    meta = W.position_meta(cfg, position)
    pos_id = args[2] if len(args) > 2 else meta["position_id"]
    sysname = meta["system"]
    url = URLS[sysname].format(uid=uid, pos=pos_id)

    # 从模板里取这个资源位的创意数据
    data_file = ROOT / f"data/资源位投放_{position}.xlsx"
    if not data_file.exists():
        print(f"没找到数据文件 {data_file}")
        return
    # 这里只关心创意层，活动层这个工具根本不用 —— 就当「挂到已有活动」，
    # 免得非要模板里有「活动」sheet 才能试创意。活动ID 是占位，不会拿去用。
    data = D.load(str(data_file), cfg,
                  {"wizard_activity": {"existing": True, "activity_id": "708"}})
    creative = None
    for u in data["units"]:
        if u["position"] == position and u["creatives"]:
            creative = u["creatives"][0]
            break
    if creative is None:
        print("模板里没有这个资源位的创意数据")
        return

    fields = W.creative_fields(cfg, position)
    print(f"资源位 {position}（{sysname}，pos {pos_id}）  单元 {uid}")
    print(f"要填 {len(fields)} 个字段：{[f['name'] for f in fields]}")
    print(f"数据：{ {k: v for k, v in creative.items() if k != '_row'} }")
    print()

    with sync_playwright() as pw:
        b = pw.chromium.connect_over_cdp(CDP)
        pg = b.contexts[0].pages[0]
        pg.bring_to_front()      # 后台标签会被 Chrome 降频，实测慢 15 倍，见 Browser.front
        pg.goto(url, wait_until="domcontentloaded")
        pg.wait_for_timeout(3000)
        if sysname != "新版":
            pg.reload()          # hash 路由换 position 必须整页刷新才重渲染
            pg.wait_for_timeout(6000)
        else:
            pg.wait_for_timeout(3000)

        body = pg.inner_text("body")
        if "创意模板获取失败" in body:
            print("✗ 页面报「创意模板获取失败」——这个 unitId 和 position 对不上")
            return
        print("页面已就绪：", body[-160:].replace("\n", "|"))
        print()

        wf = WizardFiller(pg, 15000)
        runner = WizardRunner({"timeout": 15000,
                               "screenshot_dir": str(ROOT / "output/screenshots"),
                               "result_file": str(ROOT / "output/result.csv")}, cfg)
        try:
            spent = []
            for j in range(1, times + 1):
                t0 = time.monotonic()
                if j > 1:
                    runner._add_creative(pg, position)
                runner._pick_creative_tab(pg, j)
                wf.fill(fields, creative, scope=f"创意{j} ")
                spent.append(time.monotonic() - t0)
                if times > 1:
                    print(f"  创意{j:>2}  {spent[-1]:5.2f}s")
            if times > 1:
                print(f"✓ {times} 条创意共 {sum(spent):.1f}s，"
                      f"平均 {sum(spent) / times:.2f}s/条")
            else:
                print("✓ 创意层全部字段填写成功")
        except Exception as e:
            print(f"✗ 失败：{e}")
            pg.screenshot(path=str(ROOT / "output/screenshots/试创意_失败.png"), full_page=True)
            print("   截图：output/screenshots/试创意_失败.png")
            return

        if save:
            text = {"v1": "保存创意", "v2": "保存创意", "新版": "保 存"}[sysname]
            pg.locator("button").filter(has_text=text).first.click()
            pg.wait_for_timeout(2500)
            print(f"已点「{text}」")
        else:
            print("（只填不保存；要保存加 --save）")


if __name__ == "__main__":
    main()
