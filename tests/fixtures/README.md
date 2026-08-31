# DOM 定位回归 fixture

`tools/test_filler_locate.py` 用这里的**真实页面快照**，离线（headless Chromium +
`page.set_content`）跑一遍「yaml 里的定位目标在不在」，抓后台改版导致的定位失效 ——
不用连内网、不用登录。

## 目录约定

```
tests/fixtures/<配置类型名>/<任意名>.html          页面快照（存 DOM 那一刻的 outerHTML）
tests/fixtures/<配置类型名>/<同名>.expect.json     期望
```

`*.expect.json`：

```json
{
  "form": "价格配置",
  "note": "新建人群弹窗打开、限制类型=常规均价 时抓的",
  "expect_missing": ["价格"]        // 这一屏本来就没渲染的字段，允许 missing（可选）
}
```

跑法：`health.probe(formcfg.load(form), page)` —— 命中 0 且不在 `expect_missing`
里的字段 = 失败。

## 怎么加一份新快照

1. `python tools\capture.py --open "<页面URL>"`，登录、把页面点到要测的那一屏。
2. 浏览器 DevTools Console：`copy(document.documentElement.outerHTML)`，粘进
   `tests/fixtures/<配置类型>/xxx.html`。
3. 写一份同名 `.expect.json`（至少 `{"form": "<配置类型名>"}`）。
4. `python tools\test_filler_locate.py` 应当全绿。以后后台改版、这份快照过期了，
   它会红 —— 那就是提醒你重抓一份、并同步改 yaml。

## 能测什么、不能测什么

- **能**：label 文字 / css 选择器 还定位得到几个元素（`src/health.py` 那套通用探针）。
- **不能**：下拉点开后选项对不对、填进去生不生效、Formily/Arco 各自的联动 ——
  那些要各 filler 的私有逻辑 + 完整交互，只有实跑能验。
