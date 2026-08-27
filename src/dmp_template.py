"""生成「指定人群ID 延期」的 Excel 模板。

⚠ 只服务 mode: dmp_extension。别的配置类型各有各的 template。
样式活儿（表头上色/批注/列宽、填写说明页、存盘）走 src/xlsx_kit.py，
这里只管「这个配置类型有哪几列、说明写什么」。
"""
from __future__ import annotations

from openpyxl import Workbook

from . import xlsx_kit as X

COLS = [
    ("人群ID", True, 18, "必填。页面人群列表里的那个 ID，一行一个。"),
    ("延期至", False, 18,
     "选填。写成 2026-08-20。留空 = 延到系统允许的最晚日期；\n"
     "填的日期超过系统上限时，也会自动改成系统上限。"),
    ("人群名称", False, 34, "选填。只用来核对，程序按人群ID定位，不看这一列。"),
]


def build(form_name: str = "DMP延期") -> str:
    wb = Workbook()
    ws = wb.active
    ws.title = "人群清单"

    for i, (name, required, width, note) in enumerate(COLS, 1):
        # 「延期至」列按文本存，避免 Excel 把 2026-08-20 转成日期序列号后读出一串数字
        X.header_cell(ws, i, name, "req" if required else "opt", width, note,
                      number_format="@" if name == "延期至" else "")

    X.freeze_header(ws)
    _doc_sheet(wb)
    return X.save(wb, f"{form_name}_人群清单模板.xlsx")


def _doc_sheet(wb):
    rows = [
        ("表单", "DMP延期", "指定人群ID 批量延期"),
        ("", "", ""),
        ("怎么填", "", "「人群清单」页签里一行一个人群，只有「人群ID」是必填的。"),
        ("", "", "「延期至」留空，就把这个人群延到系统允许的最晚日期。"),
        ("", "", "「延期至」填的日期超过系统上限时，自动改成系统上限，不会报错。"),
        ("", "", "黄色列 = 必填，灰色列 = 选填。"),
        ("", "", ""),
        ("⚠ 跑之前", "", "先点「载入并检查」：清单里的 ID 会和页面上的人群核对一遍，"),
        ("", "", "页面上找不到的 ID 会标红，不会去撞墙。"),
        ("", "", ""),
        ("字段", "是否必填", "说明"),
    ]
    for name, required, _w, note in COLS:
        rows.append((name, "必填" if required else "选填", note.replace("\n", " ")))

    X.doc_sheet(wb, rows, widths=(20, 10, 88), bold_prefixes=("⚠",),
                bold_exact=("字段",))
