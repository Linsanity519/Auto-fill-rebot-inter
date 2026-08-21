"""生成「指定实验ID 续期」的 Excel 模板。

新增文件。src/template.py（价格配置）、src/wizard_template.py（资源位投放）、
src/dmp_template.py（DMP延期）都不受影响。
"""
from __future__ import annotations

from openpyxl import Workbook
from openpyxl.comments import Comment
from openpyxl.styles import Alignment, Font, PatternFill

from .paths import user_path

REQ_FILL = PatternFill("solid", fgColor="FFF2CC")   # 必填 - 浅黄
OPT_FILL = PatternFill("solid", fgColor="F2F2F2")   # 选填 - 浅灰

COLS = [
    ("实验ID", True, 18, "必填。实验列表里实验名下面那个 ID:12345 的数字部分，一行一个。"),
    ("延期至", False, 18,
     "选填。写成 2026-11-11。留空 = 延到平台允许的最晚日期；\n"
     "填的日期超过平台上限时，也会自动改成平台上限。"),
    ("实验名称", False, 40, "选填。只用来核对，程序按实验ID定位，不看这一列。"),
]


def build(form_name: str = "AB实验延期") -> str:
    wb = Workbook()
    ws = wb.active
    ws.title = "实验清单"

    for i, (name, required, width, note) in enumerate(COLS, 1):
        cell = ws.cell(row=1, column=i, value=name)
        cell.font = Font(bold=True)
        cell.alignment = Alignment(horizontal="center")
        cell.fill = REQ_FILL if required else OPT_FILL
        cell.comment = Comment(note, "配置助手")
        ws.column_dimensions[cell.column_letter].width = width

    # 「延期至」按文本存，避免 Excel 把 2026-11-11 转成日期序列号后读出一串数字
    for r in range(2, 501):
        ws.cell(row=r, column=2).number_format = "@"

    ws.freeze_panes = "A2"
    _doc_sheet(wb)

    out = user_path("data", f"{form_name}_实验清单模板.xlsx")
    out.parent.mkdir(parents=True, exist_ok=True)
    wb.save(out)
    return str(out)


def _doc_sheet(wb):
    doc = wb.create_sheet("填写说明")
    for col, w in (("A", 20), ("B", 10), ("C", 88)):
        doc.column_dimensions[col].width = w

    rows = [
        ("表单", "AB实验延期", "指定实验ID 批量续期"),
        ("", "", ""),
        ("怎么填", "", "「实验清单」页签里一行一个实验，只有「实验ID」是必填的。"),
        ("", "", "「延期至」留空，就把这个实验延到平台允许的最晚日期。"),
        ("", "", "「延期至」填的日期超过平台上限时，自动改成平台上限，不会报错。"),
        ("", "", "黄色列 = 必填，灰色列 = 选填。"),
        ("", "", ""),
        ("⚠ 跑之前", "", "先点「载入并检查」：清单里的 ID 会用页面搜索框逐个核对一遍，"),
        ("", "", "搜不到的 ID 会标红，不会等跑到一半才发现。"),
        ("", "", ""),
        ("⚠ 续不了的", "", "已经跑满最长实验时长的实验，平台不给任何可选日期，"),
        ("", "", "这种会标成「不能再续期」跳过，不算失败，也不会中断后面的实验。"),
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
