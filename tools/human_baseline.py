"""人工基准估算：一条配置，人自己在页面上填要多久。

    python tools\\human_baseline.py

主页那个「省下工时」= 人工基准 × 条数 − 机器实跑。人工那半截是估的，
这个脚本就是「怎么估的」—— 把估的过程摊开写在这儿，而不是拍一个数写进
settings.yaml 了事。跑一遍会打出每个配置类型的构成明细和建议值，
核对完手动抄进 config/settings.yaml 的 usage.saving.per_item_seconds。

⚠ 为什么不在运行时实时算：那样 yaml 一改，历史统计数字就会跟着跳，
  没法解释「上周报的 12 小时今天怎么变 14 小时了」。所以基准是**静态数字**，
  这个脚本只是让它可复算、可质疑。

估法：一条的时间 = Σ(每个要填的字段 × 按控件类型的单价) + 每条固定开销

单价是按「知道自己要填什么的人，在陌生度中等的后台页面上操作一次」估的
（见 UNIT_SECONDS 每一行后面的理由）。它们不是实测值 ——
**真要校准，找人拿秒表做一条，对一下这里算出来的数**，差得多就改单价，
别改结果。

⚠ 改完单价、把新数字抄进 settings.yaml 时，在 commit / 注释里写清**改的是哪个单价、
  为什么** —— 这个数会出现在给所有人看的主页上，「它怎么来的」必须一路可追。

带条件的字段（reveals 展开出来的，「人群类型=DMP包 才要填人群ID」）按半个算：
一条单元只会走中其中一个分支，全算就高估了。
"""
from __future__ import annotations

import io
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
if sys.stdout:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

from src import wizard_schema as W  # noqa: E402

FORMS_DIR = Path(__file__).resolve().parent.parent / "config" / "forms"

# 一个字段填一次要多久（秒）。理由写在后面，觉得不对就在这儿改。
UNIT_SECONDS = {
    "cover": 90,     # 原生商广的封面：找图 + **手动压到 700KB 以内**（剧集截图动辄好几 MB，
                     #   后台直接打回）+ 上传。压图那一步机器是自动做的（src/ad_image.py），
                     #   人工得开个工具导出、不够小再来一遍 —— 这一个字段比整个单元层都费时间
    "upload": 45,    # 翻素材目录、选文件、等上传、核对尺寸对不对
    "date_range": 35,  # 起止两个日期，各翻一次月份
    "date": 20,      # 日期控件要翻月份点格子，还得把「年-月-日 时:分」看两遍
    "picker": 30,    # 人群包这类要开弹窗搜、勾、确定
    "range": 12,     # 「n 天至 m 天」两个数字框
    "multi": 15,     # 多选要展开列表逐个勾，选项动辄十几个
    "single": 10,    # 下拉/单选：展开、在长列表里找到那一项、点
    "text": 8,       # 定位到输入框、把值敲进去
}

# 每条的固定开销：不填字段也躲不掉的那些动作
FIXED_SECONDS = {
    "资源位投放": 90,      # 单元页「保存并下一步」→ 等后台跳创意页 → 创意页保存 → 等回列表 → 扫一眼有没有标红
    "原生商广": 90,        # 计划/单元/创意三段页面 + 等素材上传
    "DMP人群新建": 40,     # 开弹窗、提交、等结果
    "价格配置": 30,
    # 老后台的单元页：进页面、在 164 行的资源位表格里翻到「收银台价格面板」、
    # 保存并等跳创意页、回头扫一眼有没有标红
    "价格面板配置": 60,
}
DEFAULT_FIXED = 30

# 没有字段清单的那两个：它们不是「填表」而是「翻列表找到那一行再点几下」，
# 只能按动作数算。动作序列直接照着 runner 走的步骤列，别凭印象。
ACTION_FLOWS = {
    "DMP延期": [
        ("在列表里搜到这个人群包（翻页/搜索）", 20),
        ("点操作菜单 → 延长有效期", 8),
        ("打开日期面板", 5),
        ("翻月份找到平台允许的最晚一天 —— 人得一个月一个月试，这是最费神的一步", 30),
        ("点确认、等保存、核对列表上的日期变了", 15),
    ],
    "AB实验延期": [
        ("在「我的实验」里筛出这一条", 20),
        ("点更多 → 实验延期", 8),
        ("打开日期面板、翻到最晚可选日", 30),
        ("确认 + 二次确认弹窗 + 等保存", 15),
    ],
    # 抢会议室的价值不在时长上（一次运行几百毫秒），在「00:00 那一刻抢没抢到」。
    # 基准给 0 = 不按时长算它的价值，主页另有「抢中率」的说法。
    "预定会议室": [],
}


def kind_of(f: dict) -> str:
    """按 yaml 里的控件类型归到哪一档单价。

    ⚠ 按类型名判，别只看有没有 options —— 「封面」「投放日期」这些没有 options，
      按 text 8 秒算就离谱了（实际是这一条里最费时间的两项）。
    """
    t = str(f.get("type") or "")
    if t == "replace_cover":
        return "cover"
    if t.startswith("upload"):
        return "upload"
    if t == "date_range":
        return "date_range"
    if t in W.DATE_TYPES or t.startswith("date"):
        return "date"
    if t == "number_range_by_label":
        return "range"
    if t == "audience":
        return "picker"
    if t in W.MULTI_TYPES:
        return "multi"
    if (f.get("options") or t.startswith(("select", "radio", "card"))
            or t.endswith("_radio")):
        return "single"
    return "text"


def weigh(fields: list[dict]) -> tuple[float, dict]:
    """一组字段要多久，外加按控件类型的构成。"""
    total, mix = 0.0, {}
    for f in fields:
        k = kind_of(f)
        # 条件字段按半个算：一条单元只会走中 reveals 里的一个分支
        w = 0.5 if f.get("_when") else 1.0
        total += UNIT_SECONDS[k] * w
        mix[k] = mix.get(k, 0) + w
    return total, mix


def wizard_form(cfg: dict) -> tuple[int, list[str]]:
    """资源位投放：一条 = 一个单元 + 一条创意。各资源位字段数差很多，取平均。

    ⚠ 人群和内容限制那些字段也要算进去 —— 现在是策略中心统一供的，
      但人工做的话，**每一个单元都得把它们重填一遍**，这正是这工具省掉的部分。
    """
    lines, per_pos = [], []
    for pos in W.position_names(cfg):
        unit = W.columns_for(cfg, pos, W.STEP_UNIT)
        crea = W.columns_for(cfg, pos, W.STEP_CREATIVE)
        t_u, mix_u = weigh(unit)
        t_c, mix_c = weigh(crea)
        per_pos.append((pos, t_u + t_c, len(unit), len(crea)))
    per_pos.sort(key=lambda x: x[1])
    avg = sum(p[1] for p in per_pos) / len(per_pos)
    lines.append(f"  {len(per_pos)} 个资源位，单元层+创意层字段填写时间平均 {avg:.0f} 秒")
    lines.append(f"    最省的：{per_pos[0][0]}　{per_pos[0][1]:.0f} 秒"
                 f"（单元 {per_pos[0][2]} 列 + 创意 {per_pos[0][3]} 列）")
    lines.append(f"    最费的：{per_pos[-1][0]}　{per_pos[-1][1]:.0f} 秒"
                 f"（单元 {per_pos[-1][2]} 列 + 创意 {per_pos[-1][3]} 列）")
    return round(avg), lines


def flat_form(cfg: dict, keys: tuple) -> tuple[int, list[str]]:
    """普通配置：把几段字段清单拍平了一起算。"""
    fields = []
    for k in keys:
        v = cfg.get(k)
        if isinstance(v, list):
            fields += W.flatten(v)
    total, mix = weigh(fields)
    desc = "、".join(f"{k}×{v:g}" for k, v in mix.items())
    return round(total), [f"  {len(fields)} 个字段（{desc}）＝ {total:.0f} 秒"]


# 原生商广的「一条」= 一个单元（runner 就是按单元记成功/失败的），
# 一个单元下面挂若干条创意，所以创意那部分要乘上「平均每单元几条创意」。
CREATIVES_PER_UNIT = 3          # ⚠ 拍的：yaml 里上限 10，实际投放常见 2~4 条


def ad_form(cfg: dict) -> tuple[int, list[str]]:
    unit = W.flatten(list(cfg.get("unit_fields") or []))
    crea = W.flatten(list((cfg.get("creative") or {}).get("fields") or []))
    t_u, _ = weigh(unit)
    t_c, _ = weigh(crea)
    return round(t_u + t_c * CREATIVES_PER_UNIT), [
        "  一条 = 一个单元（runner 按单元记成败）",
        f"    单元层 {len(unit)} 个字段 ＝ {t_u:.0f} 秒",
        f"    创意层 {len(crea)} 个字段 × 平均 {CREATIVES_PER_UNIT} 条创意 "
        f"＝ {t_c * CREATIVES_PER_UNIT:.0f} 秒（⚠ 每单元几条创意是拍的，上限 10）",
    ]


# 价格面板配置：一条 = 一个单元。时间大头不在「字段多」，而在**每个 SKU 都要单独配一遍**：
# 点中卡片 → 选搭售类型 → 在一两百条的下拉里找到那个 pid → 再选买赠商品的类型和 id。
# 外加「套餐排列」是纯手工拖出来的。机器省掉的正是这两块。
# ⚠ 人工做的时候 pid 一样要一个个去下拉里找 —— 机器人是从 PID 映射表读，
#   但那张表本来就是人抄一次、之后一直复用，摊到每个单元上可以忽略。
SKUS_PER_UNIT = 4               # ⚠ 拍的：面板最多 2+2，常见 3~5 个 SKU
DRAG_SECONDS = 12               # 拖一张卡片到位（拖歪了还得再来一次）


def price_panel_form(cfg) -> tuple[int, list[str]]:
    """price_panel：Excel 只出几列，其余走策略中心，所以要把两边都算上。"""
    pos = W.position_names(cfg)[0]
    unit = [f for f in (cfg.get("unit_fields") or []) if f.get("type")]
    # ⚠ 策略中心供的字段也要算：机器人是配一次全批套用，人工做的话
    #   **每个单元都得重填一遍** —— 这正是这工具省掉的部分（同 wizard_form 的口径）。
    common = [f for f in W.flatten(W.unit_fields(cfg, pos)) if f.get("scope") != "manual"]
    t_unit, _ = weigh(unit)
    t_common, _ = weigh(common)

    per_sku = UNIT_SECONDS["single"] + UNIT_SECONDS["picker"] * 2
    t_sku = per_sku * SKUS_PER_UNIT
    t_drag = DRAG_SECONDS * SKUS_PER_UNIT

    return round(t_unit + t_common + t_sku + t_drag), [
        f"  单元级字段 {len(unit)} 个 ＝ {t_unit:.0f} 秒",
        f"  策略中心供的字段 {len(common)} 个 ＝ {t_common:.0f} 秒"
        f"（人工做则每个单元都要重填一遍）",
        f"  每个 SKU 单独配搭售 {per_sku} 秒 × {SKUS_PER_UNIT} 个 ＝ {t_sku:.0f} 秒"
        f"（⚠ 每单元几个 SKU 是拍的）",
        f"  套餐排列拖拽 {DRAG_SECONDS} 秒 × {SKUS_PER_UNIT} ＝ {t_drag:.0f} 秒",
    ]


def main():
    print("=" * 62)
    print("人工基准估算　—— 一条配置，人自己在页面上填要多久")
    print("=" * 62)
    print("单价：" + "、".join(f"{k} {v}秒" for k, v in UNIT_SECONDS.items()))
    print("条件字段（reveals 出来的）按半个算\n")

    out = {}
    for path in sorted(FORMS_DIR.glob("*.yaml")):
        cfg = yaml.safe_load(io.open(path, encoding="utf-8").read())
        name = cfg["name"]
        print(f"■ {name}")

        if name in ACTION_FLOWS:
            steps = ACTION_FLOWS[name]
            if not steps:
                print("  不按时长算价值（看抢中率），基准 = 0\n")
                out[name] = 0
                continue
            for text, sec in steps:
                print(f"    {sec:>3} 秒　{text}")
            body = sum(sec for _, sec in steps)
            fixed = 0
        elif W.is_wizard(cfg):
            body, lines = wizard_form(cfg)
            print("\n".join(lines))
            fixed = FIXED_SECONDS.get(name, DEFAULT_FIXED)
        elif cfg.get("mode") == "ad_native":
            body, lines = ad_form(cfg)
            print("\n".join(lines))
            fixed = FIXED_SECONDS.get(name, DEFAULT_FIXED)
        elif cfg.get("mode") == "price_panel":
            body, lines = price_panel_form(cfg)
            print("\n".join(lines))
            fixed = FIXED_SECONDS.get(name, DEFAULT_FIXED)
        else:
            body, lines = flat_form(cfg, ("fields",))
            print("\n".join(lines))
            fixed = FIXED_SECONDS.get(name, DEFAULT_FIXED)

        total = body + fixed
        # 取整到半分钟：这是个估计值，写成 487 秒会显得比它实际上精确
        rounded = int(round(total / 30.0) * 30)
        print(f"  字段/动作 {body} 秒 + 每条固定开销 {fixed} 秒 = {total} 秒"
              f"　→ 建议基准 {rounded} 秒（{rounded / 60:.1f} 分钟）\n")
        out[name] = rounded

    print("=" * 62)
    print("抄进 config/settings.yaml 的 usage.saving.per_item_seconds：\n")
    for k, v in out.items():
        print(f"      {k}: {v}")
    print("\n⚠ 这些是算出来的，不是测出来的。找人拿秒表做一条对一下，")
    print("  差得多就回来改 UNIT_SECONDS / FIXED_SECONDS 的单价，别直接改结果。")


if __name__ == "__main__":
    main()
