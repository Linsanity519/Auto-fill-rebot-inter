# 给 Claude 的项目地图

**这个文件的作用：让一个全新的会话不用把 src/ 翻一遍就能动手。**
每次改完架构级的东西（加 mode、拆模块、换约定），回来更新这里。

---

## 一句话

Python + Playwright，挂到用户**已登录**的 Chrome，读 Excel / 界面上填的参数，
在内网后台批量填表，填完停下等人确认再提交。不处理账号密码。

---

## 入口

| 怎么起 | 走哪儿 | 说明 |
|---|---|---|
| `python main.py` | `src/webapp.py` + `assets/webui/` | **默认界面**，pywebview 壳 + HTML/JS。日常改界面改这里 |
| `python main.py --cli` | `src/ui.py` ConsoleUI | 命令行跑 |
| 打包后的 exe | `launcher.py` | **故意不 import src**，src/ 要留在磁盘上才能被 300KB 代码包更新掉 |

> 老的 `--tk` tkinter 界面（`src/gui.py` / `src/theme.py`）1.1.2 起删掉了：它配不了策略中心 /
> 「准备」页 / 活动选择，三套 UI 并存维护不划算。要退回旧界面就看 git 历史。

---

## 核心抽象：一个「配置类型」= 一个 mode

用户在界面上选的每一项（资源位投放 / 价格面板配置 / DMP延期 …）是一个**配置类型**：

```
config/forms/<配置类型名>.yaml    ← 声明 mode: xxx，以及这个页面的全部字段/选择器
        ↓
src/registry.py  MODES["xxx"]     ← 唯一的分发表：执行器 / 模板生成器 / 延期范围
        ↓
src/<前缀>_runner.py              ← 这个 mode 的主流程
```

**`src/registry.py` 是读代码的入口，先读它。** 新增 mode 主要就是往这张表里加一条。

### mode ↔ 文件家族

| mode | 配置类型 | 前缀 | runner | 数据 | 模板 | 填控件 |
|---|---|---|---|---|---|---|
| `wizard` | 资源位投放 | `wizard_` | `wizard_runner` | `wizard_data` | `wizard_template` | `wizard_filler` |
| `price_panel` | 价格面板配置 | `pp_` | `pp_runner` | `pp_data` | `pp_template` | `pp_filler` + `pp_creative` |
| `ad_native` | 原生商广 | `ad_` | `ad_runner` | `ad_data` | `ad_template` | `ad_filler` + `ad_image` |
| `dmp_extension` | DMP延期 | `dmp_` | `dmp_runner` | `dmp_data` | `dmp_template` | 直接操作，无独立 filler |
| `ab_extension` | AB实验延期 | `ab_` | `ab_runner` | `ab_data` | `ab_template` | 同上 |
| `meeting_reserve` | 预定会议室 | `meeting_` | `meeting_runner` | `meeting_data` | 不吃 Excel | 走接口 `meeting_api` |
| （无 mode） | 价格配置、DMP人群新建 | — | `runner` | `datasource` | `template` | `filler` |

**每套家族之间互不调用，改一套不会影响另一套。** 这是刻意的：各家后台的 DOM
栈完全不同（Formily / Vue+tw- / iView / Arco / antd），共用选择器只会互相踩。

### 跨 mode 共用的东西（改这些要想清楚影响面）

| 模块 | 谁在用 | 干什么 |
|---|---|---|
| `wizard_strategy.py` | **wizard + price_panel** | 策略中心：「配一次全批套用」的字段，支持多套方案 + 按单元名关键词切 |
| `wizard_schema.py` | **wizard + price_panel** | yaml 字段结构的读取/展开 |
| `ad_prep.py` | **ad_native + price_panel** | 「准备」页上填一次全批共用的参数，存 `config/prep/*.json` |
| `browser.py` / `chrome.py` | 全部 | 挂到 9222 的 CDP、把标签页拨到前台（`front()`，不拨会被 Chrome 降频 15 倍） |
| `formcfg.py` | 全部 | **form yaml 的唯一入口**：读 + 按 mtime 缓存 + 校验顶层键名。不要再写 `yaml.safe_load(config/forms/...)` |
| `runstate.py` | 六个执行器 | 断点（哪些已跑成功）。`StateMixin` 混进去就有 `clear_state` |
| `paths.py` | 全部 | `user_path()` 可写目录 / `resource()` 只读资源。**不要拼相对路径** |
| `settings.py` | 全部 | 读 `config/settings.yaml`，缺的字段用 `assets/settings.default.yaml` 兜底 |
| `preview.py` `ui.py` `validate.py` `images.py` `usage.py` `report.py` | 全部 | 预检行 / 界面回调 / 离线校验 / 图片 / 埋点 / 结果 csv |
| `fill_core.py` | **新写的 filler 用它** | 和 DOM 无关的填表原语，见下 |
| `xlsx_kit.py` | **全部 6 份 template** | 生成 Excel 时的样式活儿：表头上色/批注/列宽、填写说明页、存盘。各家 template 只管「有哪几列、说明写什么」 |

### `src/fill_core.py`：新 filler 从这儿起步，别再抄第五遍

四套 filler 的选择器确实一行都不能互抄（DOM 栈完全不同），**这个隔离是对的，
不要去合并它们**。但「等到条件成立 / 按文字挑一条 / 等渲染稳定 / 报错怎么措辞」
和 DOM 无关，各写了一遍 —— 光「盯着 cond 轮询到超时」这个循环就有 4 份相同拷贝
（`pp_filler` / `wizard_filler` / `wizard_runner` / `pp_creative`），
外加一批套着自己判据的 `_wait_rows` / `_wait_gone` / `_wait_list_changed`。

`fill_core` 把这部分抽了出来：`wait_until` `wait_stable` `norm` `pick` `pick_all`
`value_matches` `opt_regex` `js_click` `note`，外加统一的报错构造
（`option_error` / `missing_error` / `field_error` / `verify_error`）。

- **存量四套不改**（都在线上跑着，动它们不划算），只在写新 filler 时用。
- 新 filler 自己只负责三件 DOM 特有的事：怎么找到字段块、怎么把浮层点开、
  怎么读出选项文字。剩下的从 `fill_core` 拿。
- 改它之后跑 `python tools\test_fill_core.py`（51 项，不联网不开浏览器）。

### 界面能力一律声明化，不许按 mode 名判断

「这个配置类型有没有策略中心 / 要不要勾资源位 / 吃不吃 Excel」，
**唯一的判据是 `webapp.Api._caps(cfg)` 按 yaml 算出来的那几个布尔**，
随 `list_forms()` 一起发给前端：

| caps | 含义 | yaml 判据 |
|---|---|---|
| `strategy` | 有策略中心 | `strategy_groups` / `scheme_groups`（转调 `wizard_strategy.has_strategy`）|
| `prep` | 有「准备」页共用参数表 | `prep_fields` / `prep_from_unit`（转调 `ad_prep.has_prep`）|
| `positions` | 要勾「本次投哪些资源位」 | `positions` 多于 1 项 |
| `activity` | 本批共用一个活动 | 有 `activity` 或 `steps` |
| `task_list` | 抢占任务清单那张卡 | 有 `grab` |
| `excel` | 吃 Excel 数据文件 | `data_source` 不是 `none` |

配套的还有 `ui:` 段（`deliver_label` / `deliver_hint` / `strategy_hint` / `run_kind`），
界面上跟着类型变的几句话写在 yaml 里，不写用默认。

⚠ **`_caps()` 函数体里一个 mode 名都不该出现**；`app.js` 里也不许再写
  `modeIs("xxx")`。要一个新开关，就往 `_caps()` 加一项、在 yaml 里声明。

为什么定这条：原来 `app.js` 写的是 `hasStrategy(){ return modeIs("wizard")
|| modeIs("price_panel") }`，而 Python 那边早就改成看 yaml 了 —— **同一个判断
两套实现**，接一个新类型两边都要改，漏改还是静默的（卡片不显示，一句报错都没有）。

改了 `_caps()` / `_ui_text()` 或加了配置类型之后，跑一次
`python tools\gen_stub_forms.py` 把 `app.js` 里那份假数据同步上
（它是"不启动 pywebview 也能核对布局"的依据，走样了就白搭）。

---

## 硬约定（违反了一定出事，都是踩出来的）

0. **往 form yaml 加新顶层键，回 `src/formcfg.py` 的 `BY_MODE` 登记一下。**
   不登记 `tools\check_mode.py` 会提示「不认识这个键」—— 那是它该干的事，
   别关提示。打错一个字母是完全静默的（`strategy_groups` → `strategy_group`，
   yaml 照样解析、策略字段从 24 悄悄变 6），这张词汇表就是为了防它。
   纯 YAML 锚点用 `_` 开头，一律放行。
1. **不写死 `sleep(n)`。** 一律 `wait_until(cond)` + `settings.timeout` 上限。
2. **不用编译哈希类名**（`tw-xxxxxx` / `css-1a75fj6` / emotion 类）。发版即失效。
   定位一律 **label 文字 → 字段块 → 块内按选项文字**。
3. **同名 label 会出现多次**，要指定第几个。人群那段一个 label 出现 5 次。
4. **改 `config/forms/*.yaml` 之前先查 `docs/README.md` 有没有对应的抓取记录。**
   凭截图或口径表猜字段名/选项，已经翻车过（见 docs/README.md 的「教训」）。
5. **提交前一定停下来给人看截图**，除非用户明确选了全自动。
6. **发版必须先写 `CHANGELOG.md` 的 `## X.Y.Z` 一节**，写"用户会感觉到什么变化"。
   没写 CI 会直接失败（这是故意的：notes 缺失是静默的）。
7. `RUNTIME_ID` **只有改了 `requirements.txt` 才 +1**。
8. 打包不 `--add-data assets`：assets/ 和 src/ 要留在 exe 旁边当普通文件。

---

## 新增一个配置类型：落地清单

需求侧要哪些信息 → 见 [docs/新增配置类型-需求单.md](docs/新增配置类型-需求单.md)。
代码侧要动的文件，按顺序：

跑之前先过一遍离线自检（不开浏览器，几秒钟）：

```bash
python tools\check_mode.py <配置类型名>
```

它查的是**接线**：yaml 必备键、name 和文件名一致、registry 有没有这条 mode、
界面能力（caps）算出来对不对、策略/准备页的字段定义解析得了没、抓取记录在不在。
写新 yaml 时加 `--typos` 顺带查键名打错（启发式，有已知误报）。
给了 `--data xxx.xlsx` 还会走一遍和「载入并检查」同一条路。
⚠ **全过 ≠ 能跑通**：选择器准不准只有实跑能验。

**先跑脚手架，接线那部分不用手写**：

```bash
python tools\new_mode.py 新配置类型名 --prefix xx
```

它铺好 yaml 骨架、抓取记录占位、runner/filler 骨架，并往 `registry.py` 的 MODES、
`formcfg.py` 的 BY_MODE、`docs/README.md` 索引各插一条。跑完立刻 `check_mode` 应该全绿。
剩下的是真业务：抓页面、填字段、写控件填法。

| # | 文件 | 干什么 | 能省吗 |
|---|---|---|---|
| 0 | `python tools\capture.py --out docs\<配置类型>-配置项抓取.md` | 先自动 dump 一份草稿，别从零手抓 | — |
| 1 | `docs/<配置类型>-配置项抓取.md` | 在草稿上人工核对补全：控件真实类型 + 选项全集 + 联动 + 坑 | **不能**，跳过它后面全在返工 |
| 2 | `docs/README.md` | 加一行索引 | 不能 |
| 3 | `config/forms/<配置类型>.yaml` | `name` / `description` / `nav` / `mode` / url / `ready_selector` / 字段 | 不能 |
| 4 | `src/<前缀>_data.py` | 读 Excel + 策略 → 每个单元最终要填的值；`validate()` 离线查错 | 能，简单表单直接复用 `datasource` |
| 5 | `src/<前缀>_template.py` | 生成 Excel 模板（只出「每条都不一样」的列） | 同上，复用 `template` |
| 6 | `src/<前缀>_filler.py` | 这套 DOM 的控件填法。**建在 `fill_core` 上** | **能且应该先试** 复用现有 filler，DOM 栈相同就别新写 |
| 7 | `src/<前缀>_runner.py` | 主流程 + `preview()` + 截图 + 确认 + 结果记录 | 能，单弹窗表单复用 `runner` |
| 8 | `src/registry.py` | `MODES` 加一条 + 两个 lazy 工厂函数 | 不能 |
| 9 | `src/webapp.py` | 只有真需要「准备页 / 策略中心 / 活动选择」时才动 | 尽量能 |
| 10 | `assets/webui/app.js` + `index.html` + `style.css` | 同上 | 尽量能 |
| 11 | `CHANGELOG.md` + `src/__init__.py` 版本号 | 发版 | 不能 |

**不用动**：`build.bat` / `build_app.spec`（hiddenimports 由 spec 扫 `src/` 自动生成）。

---

## 不要通读的文件（很贵，按需 grep 就行）

`src/pp_filler.py`(1129) `src/dmp_runner.py`(926)
`src/ab_runner.py`(908) `src/usage.py`(897) `assets/webui/app.js`(3123)
`config/forms/价格面板配置.yaml`(700+) `config/forms/资源位投放.yaml`(600+)

需要它们里的某个能力时，先 `grep -n "^\s*def \|^class "` 看函数列表，再定点读。

---

## 抓页面

**先跑 `tools/capture.py`，别一开始就手动翻 DOM。**

```bash
python tools\capture.py --out docs\XX-配置项抓取.md
```

它把当前页 dump 成抓取记录草稿：字段全表、控件类型（推断）、必填、
**同名 label 计数**、当前不可见的字段。三个模式：

| 命令 | 干什么 |
|---|---|
| `--out FILE` | 出 markdown 草稿 |
| `--options "字段名"` | 把某个下拉点开、读出全部选项（草稿里读不到的那部分） |
| `--snap a` → 人工改一个值 → `--snap b --diff a` | **抓联动**：哪些字段出现/消失/变必填 |

`--diff` 是最值钱的一个：它直接产出「选了这个值之后表单换成哪一套」，
而这决定 **Excel 模板出哪些列** —— 模板列跑之前就定死，漏一条整个模板重做。

⚠ 草稿**不是结论**。控件类型是推断的（多选下拉 vs 勾选框组一定要人眼确认），
远程搜索的下拉读回来是空（正常，得打字才拉数据）。核对完再落进 `docs/`。

手动抓（capture 覆盖不到的部分，完整说明见 `docs/README.md` 末尾）：

```python
from src import chrome
from src.paths import app_dir
chrome.launch("http://127.0.0.1:9222", app_dir() / ".chrome-profile", "要抓的页面URL")
# 人工在弹出窗口里扫码登录，然后：
from src.browser import Browser
with Browser("http://127.0.0.1:9222", 30000) as b:
    p = b.page      # 标准 Playwright Page
```

**能拿接口就别翻 DOM。**（价格面板那套的 `config_materials` 一个接口顶几百行选项抓取）

---

## 发版

```bash
build.bat
```

产出两个包：`ConfigAssistant-<ver>.zip`(~300KB，日常发版) 和
`ConfigAssistant-Setup-<ver>.exe`(~45MB，首次安装 / 动了 requirements.txt)。
推 `vX.Y.Z` 标签，GitHub Actions 自动发布。细节见 `README.md`。
