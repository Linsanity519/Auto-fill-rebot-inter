"""生成「花火稿件授权」的 Excel 模板。

⚠ 只服务 mode: hh。样式活儿走 src/xlsx_kit.py，这里只管有哪几列、说明写什么。
COLS 同时是 registry 里 template_columns 的来源 —— 列定义只此一份，别再抄。
"""
from __future__ import annotations

from openpyxl import Workbook

from . import xlsx_kit as X
from .hh_data import RANGE_OPTIONS, TIME_OPTIONS

COLS = [
    ("稿件bvid", True, 20,
     "必填。视频链接里 BV 开头的那一段，一行一个，比如 BV1JHYd6GEKj。\n"
     "⚠ 只有「花火稿件」能在这一页授权。非花火稿件点「载入并检查」时会标红，\n"
     "  那些要走隔壁的「普通内容授权」。"),
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


def build(form_name: str = "花火稿件授权") -> str:
    wb = Workbook()
    ws = wb.active
    ws.title = "稿件清单"

    for i, (name, required, width, note) in enumerate(COLS, 1):
        # bvid 按文本存：Excel 不会动 BV 号，但列宽/对齐统一走文本更稳
        X.header_cell(ws, i, name, "req" if required else "opt", width, note,
                      number_format="@" if name == "稿件bvid" else "")

    X.freeze_header(ws)
    _doc_sheet(wb)
    return X.save(wb, f"{form_name}_稿件清单模板.xlsx")


def _doc_sheet(wb):
    rows = [
        ("表单", "花火稿件授权", "资产 → 账号及视频授权 → 花火内容授权 → 添加稿件授权"),
        ("", "", ""),
        ("怎么填", "", "「稿件清单」页签里一行一个稿件，只有「稿件bvid」是必填的。"),
        ("", "", "授权时间 / 共享范围 留空就按页面默认值（不限 / " + RANGE_OPTIONS[0] + "）。"),
        ("", "", "黄色列 = 必填，灰色列 = 选填。"),
        ("", "", ""),
        ("⚠ 跑之前", "", "先点「载入并检查」：每个 bvid 会先打一次平台的校验接口，"),
        ("", "", "「不是花火稿件」的会当场标红，不用等跑到一半才发现。"),
        ("", "", ""),
        ("⚠ 三次上限", "", "同一个视频 7 天内只有 3 次申请机会，清单里重复的 bvid 会被拦下来。"),
        ("", "", "已经申请过、还在等 UP 主确认的稿件，别重复跑。"),
        ("", "", ""),
        ("⚠ 发出去之后", "", "程序只负责发申请，还要联系 UP 主 / MCN 到花火平台确认，授权才算成。"),
        ("", "", ""),
        ("字段", "是否必填", "说明"),
    ]
    for name, required, _w, note in COLS:
        rows.append((name, "必填" if required else "选填", note.replace("\n", " ")))

    X.doc_sheet(wb, rows, widths=(20, 10, 88), bold_prefixes=("⚠",),
                bold_exact=("字段",))
