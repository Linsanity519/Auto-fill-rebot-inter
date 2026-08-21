"""四个执行器共用的预览行契约。

在这之前每个 runner.preview() 各自拼一个中文 key 的 dict（"问题"/"已完成"/"序号"...），
主表单/DMP/AB 三个还用 "_record"，wizard 用的是 "_unit" —— gui.py 里两处写死取
"_record"，wizard 模式一进界面就 KeyError（那会儿资源位投放还标着"开发中勿用"，所以一直没人发现）。
这里统一成一个 dataclass，"_record"/"_unit" 都改叫 payload，把这个坑连带修掉。

payload 内部的形状仍然按 mode 不同：单弹窗表单（runner/dmp/ab）是
{"header": {...}, "items": [...]}，wizard 是 {"position", "header", "creatives"} ——
这是业务本身的差异（wizard 要连着填活动/单元/创意三层，字段名对应到 run() 里
消费它的代码），不强行拉平成同一形状。
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class PreviewRow:
    index: int              # 原「序号」
    name: str                # 原「名称」
    kind: str                 # 原「类型」
    detail_count: int         # 原「明细」
    issues: list = field(default_factory=list)   # 原「问题」
    done: bool = False         # 原「已完成」
    payload: dict = field(default_factory=dict)  # 原 "_record" / "_unit"
