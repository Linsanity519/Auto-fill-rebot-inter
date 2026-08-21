"""企业微信文档 MCP 客户端 —— 只用标准库，四十行。

⚠ **只在收集端（你自己的机器）用，绝不要 import 进 src/**。
  这把 apikey 没法限权：一把覆盖持有人名下所有文档，能新建、能全量覆盖、能删子表
  （见 docs/界面方案/主页-使用统计调研.md §2.5.2）。打进发给一百个人的 exe 里，
  等于把「以我的身份删改我所有文档」发出去 —— exe 里的字符串谁都能翻出来。
  分发出去的只有群机器人 webhook（泄露最多被人往统计群里发消息）。

实测结论（2026-08-21，端点 qyapi.weixin.qq.com/mcp/v2/bot/doc）：
  · 返回的是**普通 JSON**，不是 SSE —— 比 MCP 协议文档说的简单，不用解析事件流
  · **不需要 Mcp-Session-Id**，也不用先 initialize 再调（但还是照规矩走一遍，
    免得哪天服务端收紧）
  · tools/list 62 个工具；写用 smartsheet_records_update，读用 smartsheet_records_list

key 从哪来（按顺序找，都没有就报错）：
  1. 环境变量 WXDOC_MCP_KEY
  2. tools/.mcp_key 文件（已在 .gitignore 里，不会进仓库）
"""
from __future__ import annotations

import json
import os
import urllib.request
from pathlib import Path

ENDPOINT = "https://qyapi.weixin.qq.com/mcp/v2/bot/doc"
TIMEOUT = 30
KEY_FILE = Path(__file__).resolve().parent / ".mcp_key"


class McpError(RuntimeError):
    pass


def load_key() -> str:
    key = (os.environ.get("WXDOC_MCP_KEY") or "").strip()
    if not key and KEY_FILE.exists():
        key = KEY_FILE.read_text(encoding="utf-8").strip()
    if not key:
        raise McpError(
            "没找到企微文档机器人的 apikey。二选一：\n"
            f"  · 把 key 写进 {KEY_FILE}（这个文件不会进 git）\n"
            "  · 或者设环境变量 WXDOC_MCP_KEY\n"
            "key 在企微「文档机器人 → 可使用权限」弹窗里，"
            "URL 的 ?apikey= 后面那一串。")
    return key


class Doc:
    """一个会话。用法：`with Doc() as d: d.call("smartsheet_get", {...})`"""

    def __init__(self, key: str | None = None):
        self.url = f"{ENDPOINT}?apikey={key or load_key()}"
        self._id = 0
        self._ready = False

    # ---------------- 底层 ----------------
    def _rpc(self, method: str, params=None, notify: bool = False):
        body = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            body["params"] = params
        if not notify:
            self._id += 1
            body["id"] = self._id
        req = urllib.request.Request(
            self.url, data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     "Accept": "application/json, text/event-stream"})
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            text = r.read().decode("utf-8", "replace")
        if notify:
            return None
        return self._parse(text)

    @staticmethod
    def _parse(text: str):
        """普通 JSON 和 SSE 两种都认 —— 现在返回的是前者，但别赌它永远是。"""
        t = (text or "").strip()
        if t.startswith("{"):
            return json.loads(t)
        for line in t.splitlines():
            if line.startswith("data:"):
                return json.loads(line[5:].strip())
        raise McpError(f"看不懂的返回：{t[:200]}")

    def __enter__(self):
        self._rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                 "clientInfo": {"name": "配置助手-统计收集", "version": "1"}})
        self._rpc("notifications/initialized", {}, notify=True)
        self._ready = True
        return self

    def __exit__(self, *a):
        return False

    # ---------------- 对外 ----------------
    def call(self, tool: str, args: dict):
        """调一个工具，返回它的结果（已经从 content 里把 JSON 抠出来）。"""
        res = self._rpc("tools/call", {"name": tool, "arguments": args})
        if "error" in res:
            raise McpError(f"{tool} 失败：{res['error']}")
        result = res.get("result") or {}
        if result.get("isError"):
            raise McpError(f"{tool} 返回错误：{_text_of(result)[:300]}")
        text = _text_of(result)
        try:
            return json.loads(text) if text.strip().startswith(("{", "[")) else text
        except json.JSONDecodeError:
            return text

    def tools(self) -> list[str]:
        return [t["name"] for t in (self._rpc("tools/list", {})
                                    .get("result", {}).get("tools", []))]


def _text_of(result: dict) -> str:
    return "".join(c.get("text", "") for c in (result.get("content") or [])
                   if c.get("type") == "text")
