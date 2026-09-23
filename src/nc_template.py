"""生成「普通内容授权」的 Excel 模板。

⚠ 只服务 mode: nc。列和「花火稿件授权」一样（同一份清单能两边跑），
  但**填写说明不一样** —— 上限口径、跑错 tab 的提示都不同，所以没有合成一份。
  读 Excel 复用 src/hh_data.py（列名/校验完全相同，没必要抄第二遍）。
"""
from __future__ import annotations

from openpyxl import Workbook

from . import xlsx_kit as X
from .hh_data import RANGE_OPTIONS, TIME_OPTIONS

COLS = [
    ("稿件bvid", True, 20,
     "必填。视频链接里 BV 开头的那一段，一行一个，比如 BV1Nibn63EPm。\n"
     "⚠ 这里只收**非花火**稿件。花火商单稿件会标红，那些要走「花火稿件授权」。"),
    ("授权时间", False, 14,
     "选填。" + " / ".join(TIME_OPTIONS) + "。留空 = 不限。\n"
     "（页面上还有「自定义」，程序不支持，要自定义日期请手工操作。）"),
    ("共享范围", False, 26,
     "选填。" + " / ".join(RANGE_OPTIONS) + "。\n"
     "留空 = " + RANGE_OPTIONS[0] + "（页面默认值）。"),
    ("UP主", False, 20, "选填。只用来在界面上认人，程序按 bvid 定位，不看这一列。"),
]


def columns(cfg: dict | None = None, opts: dict | None = None) -> dict:
    return {"稿件清单": [c[0] for c in COLS]}


def build(form_name: str = "普通内容授权") -> str:
    wb = Workbook()
    ws = wb.active
    ws.title = "稿件清单"

    for i, (name, required, width, note) in enumerate(COLS, 1):
        X.header_cell(ws, i, name, "req" if required else "opt", width, note,
                      number_format="@" if name == "稿件bvid" else "")

    X.freeze_header(ws)
    _doc_sheet(wb)
    return X.save(wb, f"{form_name}_稿件清单模板.xlsx")


def _doc_sheet(wb):
    rows = [
        ("表单", "普通内容授权", "资产 → 账号及视频授权 → 普通内容授权 → 添加稿件授权"),
        ("", "", ""),
        ("怎么填", "", "「稿件清单」页签里一行一个稿件，只有「稿件bvid」是必填的。"),
        ("", "", "授权时间 / 共享范围 留空就按页面默认值（不限 / " + RANGE_OPTIONS[0] + "）。"),
        ("", "", "黄色列 = 必填，灰色列 = 选填。"),
        ("", "", ""),
        ("⚠ 跑之前", "", "先点「载入并检查」：每个 bvid 会先打一次平台的校验接口，"),
        ("", "", "花火商单稿件会当场标红 —— 那些要去「花火稿件授权」那个配置类型跑。"),
        ("", "", "已经授权过的稿件也会标出来，不会重复申请。"),
        ("", "", ""),
        ("⚠ 3 次上限", "", "这一页的上限是**按 UP 主算的**：7 日内对同一账号只有 3 次申请机会。"),
        ("", "", "所以同一个 UP 主在清单里超过 3 条时，第 4 条起会被标红拦下 ——"),
        ("", "", "不是程序小气，是平台发出去也会失败。分两周跑，或者挑重点的 3 条。"),
        ("", "", ""),
        ("⚠ 发出去之后", "", "程序只负责发申请，还要 UP 主到 bilibili 消息列表里确认，授权才算成。"),
        ("", "", ""),
        ("字段", "是否必填", "说明"),
    ]
    for name, required, _w, note in COLS:
        rows.append((name, "必填" if required else "选填", note.replace("\n", " ")))

    X.doc_sheet(wb, rows, widths=(20, 10, 88), bold_prefixes=("⚠",),
                bold_exact=("字段",))
