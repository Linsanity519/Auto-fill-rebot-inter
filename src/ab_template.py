"""生成「指定实验ID 续期」的 Excel 模板。

⚠ 只服务 mode: ab_extension。别的配置类型各有各的 template。
样式活儿（表头上色/批注/列宽、填写说明页、存盘）走 src/xlsx_kit.py，
这里只管「这个配置类型有哪几列、说明写什么」。
"""
from __future__ import annotations

from openpyxl import Workbook

from . import xlsx_kit as X

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
        # 「延期至」按文本存，避免 Excel 把 2026-11-11 转成日期序列号后读出一串数字
        X.header_cell(ws, i, name, "req" if required else "opt", width, note,
                      number_format="@" if name == "延期至" else "")

    X.freeze_header(ws)
    _doc_sheet(wb)
    return X.save(wb, f"{form_name}_实验清单模板.xlsx")


def _doc_sheet(wb):
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

    X.doc_sheet(wb, rows, widths=(20, 10, 88), bold_prefixes=("⚠",),
                bold_exact=("字段",))
