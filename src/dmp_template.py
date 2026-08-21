"""生成「指定人群ID 延期」的 Excel 模板。

新增文件。老的 src/template.py（价格配置）和 src/wizard_template.py（资源位投放）
都不受影响。
"""
from __future__ import annotations

from openpyxl import Workbook
from openpyxl.comments import Comment
from openpyxl.styles import Alignment, Font, PatternFill

from .paths import user_path

REQ_FILL = PatternFill("solid", fgColor="FFF2CC")   # 必填 - 浅黄
OPT_FILL = PatternFill("solid", fgColor="F2F2F2")   # 选填 - 浅灰

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
        cell = ws.cell(row=1, column=i, value=name)
        cell.font = Font(bold=True)
        cell.alignment = Alignment(horizontal="center")
        cell.fill = REQ_FILL if required else OPT_FILL
        cell.comment = Comment(note, "配置助手")
        ws.column_dimensions[cell.column_letter].width = width

    # 「延期至」列按文本存，避免 Excel 把 2026-08-20 转成日期序列号后读出一串数字
    for r in range(2, 501):
        ws.cell(row=r, column=2).number_format = "@"

    ws.freeze_panes = "A2"
    _doc_sheet(wb)

    out = user_path("data", f"{form_name}_人群清单模板.xlsx")
    out.parent.mkdir(parents=True, exist_ok=True)
    wb.save(out)
    return str(out)


def _doc_sheet(wb):
    doc = wb.create_sheet("填写说明")
    for col, w in (("A", 20), ("B", 10), ("C", 88)):
        doc.column_dimensions[col].width = w

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

    for r, row in enumerate(rows, 1):
        for c, v in enumerate(row, 1):
            cell = doc.cell(row=r, column=c, value=v)
            cell.alignment = Alignment(vertical="top", wrap_text=(c == 3))
            if str(v).startswith("⚠") or v in ("字段",):
                cell.font = Font(bold=True)
