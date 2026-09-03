# 三连竞价推广（auto-v2）— 配置项抓取

抓取来源：`https://ad.bilibili.co/#/promote/auto-v2`，账户「大会员中心-营销活动推广」ID 4094。
抓取时间：2026-09-02，驱动页面实地点开抓的。
本页**取代**老的 `#/promote/auto`（原生商广 1.x 用的页）。

> 覆盖度：项目层字段 + 选项 + 推广目的联动 + 两个抽屉的框架 = 已确认。
> **素材行字段（标题/描述/封面上限）、提交判据、入口 URL 参数** = 待实跑确认（下方 §5）。

---

## 0. 和老页面的根本差异

| | 老 `#/promote/auto` | 新 `#/promote/auto-v2`（本页） |
|---|---|---|
| 前端框架（主表单） | iView `ivu-` | **B 站自研 `bd-`**：`bd-form-item` / `bd-radio-button` / `bd-select` / `bd-date-editor` / `bd-checkbox` / `bd-drawer` |
| 层级 | 计划 → 单元 → 创意（三层） | **项目 → 素材（两层）** |
| 单选选中态 | `.radio-item` 看 `active` | `.bd-radio-button` 看 **`is-active`** |
| 单元层的定向/出价/预算/时段/监测 | 每个单元各填一遍 | **全部上移到项目层，一个项目填一次** |
| 一批的产物 | 1 计划 + N 单元（按「内容」分组），每单元 ≤10 创意 | **1 项目 + N 素材（平表），≤200 素材** |
| 提交按钮 | 页面有 3 个隐藏的「保存」，要挑可见的 | 只有 1 个可见「保存」（`bd-button--primary`），旁边一个「取消」 |

左侧步骤条 `.ad-auto-v2-sidebar`：

```
1 项目信息 ── 基础信息 / 推广标的 / 目标与出价 / 排期与预算 / 定向
2 素材信息 ── AI素材衍生 / 基础素材
```

**两个抽屉没有跟着换框架，还是老的 iView**，老 `ad_filler` 里对应的代码基本能复用：
- 「编辑定向」抽屉：`.ivu-drawer-body` / `.ivu-form-item` / `li.radio-item.active`
- 「添加稿件/视频」抽屉：`.product-select-drawer`，搜索框 `input.ivu-input`，footer `.drawer-footer`

---

## 1. 项目层字段（`bd-form-item`，默认 18~19 个，随推广目的增减）

选中态 = class 含 `is-active`。容器 `.bd-form-item`，label 在 `.bd-form-item__label`，
控件在 `.bd-form-item__content`，必填 = 含 `is-required` / `asterisk-right`。

### 基础信息

| label | 必填 | 控件 | 选项 / placeholder | 值来源（新模型） | 备注 |
|---|---|---|---|---|---|
| 项目名称 | ✔ | 输入框 | `请输入项目名称`，上限 90 字 | 准备页（本批 1 个项目） | 老「计划名称」 |
| 广告类型 | ✔ | `bd-radio-button` | **所有广告**(默认) / 搜索广告 | 固定 所有广告 | 选「搜索广告/所有广告」会多出「搜索快投」块（见下），OGV推广下不出现 |

### 推广标的

| label | 必填 | 控件 | 选项 | 值来源 | 备注 |
|---|---|---|---|---|---|
| 推广目的 | ✔ | `.ppt-new-item` 卡片（`.ppt-title` + `active`） | 交易经营 / 内容种草 / **销售线索收集** / 应用推广 | 固定 销售线索收集 | ⚠ 联动总开关，见 §2 |
| 推广内容 | ✔ | `bd-radio-button` | 随推广目的变；**销售线索收集**下：线索(默认) / 直播间 / 直播预约 / **OGV推广** / 小程序/小游戏 | 固定 OGV推广 | 老 yaml 也是这个组合 |
| APP包 | ✔（条件） | 下拉 | — | — | **仅推广内容=线索时出现**，OGV推广下消失 |
| 关联产品 | ✔ | `bd-radio-button` | **关闭**(默认) / 开启 | 固定 关闭 | 老「关联产品=不启用」 |
| 监测链接 | ✔ | `bd-radio-button` | 自定义 | 固定 自定义 | |
| 展示监控 | ✔ | 输入框 | `请输入https链接开头的URL` | 固定（default 里那串 callback URL） | 老页面靠 placeholder 顺序取，新页面有独立 label |
| 点击和播放3秒监控 | ✔ | 输入框 | `请输入https链接开头的URL` | 固定（同上 type=click） | 老 yaml 叫「点击监控」 |

### 目标与出价

| label | 必填 | 控件 | 选项 / placeholder | 值来源 | 备注 |
|---|---|---|---|---|---|
| 竞价策略 | ✔ | 卡片（`.bd-form-item__content` 为空，卡片渲染在别处，页面文案确认为）**稳定成本投放**（控制成本，尽量消耗预算）/ 最大转化投放（匀速花完预算，获取更多转化） | 固定 稳定成本投放 | 选择器待实跑定（老页面是 `.launch-type-item-new`） |
| 付费方式 | ✔ | `bd-radio-button` | oCPM | 固定 oCPM | |
| 转化目标及出价 | ✔ | `bd-select`（`.bd-select__wrapper` → 浮层 `.bd-select-dropdown__list`）+ 出价输入框 | 下拉 `请选择优化目标`，**销售线索收集+OGV推广下只有「表单提交」**；出价框 `请输入出价(元)`，单位 `元/转化` | 下拉固定 表单提交；出价 准备页 | 老页面是「转化目标」「出价」两项，新页面合成一项 |
| 搜索快投 | 条件 | 复合块（出价系数/首位出价系数/关键词/否词/定向拓展/搜索AIGC/智能选词 + 「编辑搜索快投」） | — | 不填，用默认 | **OGV推广下不出现**，原生商广用不到 |

### 排期与预算

| label | 必填 | 控件 | 选项 / placeholder | 值来源 | 备注 |
|---|---|---|---|---|---|
| 项目预算 | ✔ | `bd-radio-button` + 金额框 | 不限预算 / **日预算**(默认)；金额框 `请输入不小于500，且只有2位小数`，单位 `元` | 准备页（固定或填值） | ⚠ 老页面「单元预算=不限预算」，新页面默认日预算且下限 500。原生商广要用「不限预算」的话得显式点 |
| 投放日期 | ✔ | `bd-radio-button` + `bd-date-editor` | **长期投放**(默认) / 设置起止时间；日期框 placeholder `投放开始时间` | 准备页（同老 yaml：投放时间 segmented + 起止时间） | ⚠ 老页面默认「设置起止时间」，新页面默认「长期投放」 |
| 投放时段 | ✔ | 周×时段网格 + 「编辑投放时段」 | 默认周一~周日 00-23 全选 | 不动，用默认 | 同老 yaml |

### 定向

| label | 必填 | 控件 | 选项 / placeholder | 值来源 | 备注 |
|---|---|---|---|---|---|
| 选择定向 | ✔ | 定向包下拉（`请选择定向包`）+ 「编辑定向」抽屉 | 抽屉 14 项：年龄/性别/地域/设备/设备品牌/操作系统版本/人群包/兴趣定向/视频分区/粉丝关系/网络/已安装用户/已转化用户过滤/手机价格/客户端版本，默认全「不限」 | 准备页（只动人群包） | 见 §3 |
| 广告投放位置 | ✔ | `bd-radio-button` | **全部**(默认) / 移动 / PC | 固定 全部 | 同老 yaml |

### 项目层里**消失 / 改名**的老字段

- **是否唤起外部应用**（老 plan：无需唤起）→ 新页面 18 项里没有，已并入推广目的联动
- **计划预算=不限预算**（老 plan）→ 并进「项目预算」
- **单元名称** → 两层模型没有「单元」，素材不再有单元名（素材行是否有自己的名字见 §4）
- 老「转化目标」「出价」两项 → 合成「转化目标及出价」

---

## 2. 联动：推广目的 → 推广内容（实地 diff）

推广目的默认「交易经营」。切到各值后「推广内容」整组替换：

| 推广目的 | 推广内容选项 | 附带变化 |
|---|---|---|
| 交易经营（默认） | 带货内容(默认) / 电商链接 / 直播间 / 直播预约 | —— |
| 销售线索收集 | 线索(默认) / 直播间 / 直播预约 / **OGV推广** / 小程序/小游戏 | 选「线索」时多一个 **APP包** 下拉；选 OGV推广 时 APP包 消失、**搜索快投块也消失** |
| 内容种草 | 待抓 | 待抓 |
| 应用推广 | 待抓 | 待抓 |

> **原生商广 = 推广目的「销售线索收集」+ 推广内容「OGV推广」**（用户确认，与老 yaml 一致）。
> 常规商广投放走哪个组合，用户后续给。

---

## 3. 「编辑定向」抽屉（还是 iView，人群包结构同老页面）

- 抽屉容器 `.ivu-drawer-body`，字段 `.ivu-form-item`（label 在 `.ivu-form-item-label`）
- 人群包块：`.pinpoint-crowd-packs-selector` > `.pinpoint-radio-group` > `ul.radio-list` >
  `li.radio-item`，三项：**不限**(默认 active) / 指定人群包 / 排除人群包，选中 = `active`
- 指定/排除点开后在 `.extra-radio-content` 里出人群包列表（老 `_pick_audience` 逻辑可复用，
  列表项 `.list-item`、勾选框 `.ivu-checkbox-wrapper`、排除「全部」行 `.checkbox-all` —— 待抓确认还在不在）
- 底部有确认按钮（`bd-button`），点完等抽屉 hidden

老 `ad_filler._audience` / `_pick_audience` 基本能搬过来，只是「编辑定向」按钮文字/外层摘要在 `bd-` 页面上，要重新定位打开入口。

---

## 4. 素材层（基础素材 = 聚合配置） ⚠ 模型变了

滚到项目层下方 or 点侧栏「基础素材」锚点才挂载。容器 `.media-editor.aggregated`。
**只有「聚合配置」一种模式**（`.media-header__mode-tag` 是死标签，没有「独立配置」开关）。

### ⚠ 和老页面的根本差异：从「一稿件一创意」变成「三个池子」

老页面：一个 avid = 一个 `.single-creative-wrapper`，各配**自己的** 1 标题 / 1 描述 / 1 封面。

新页面「聚合配置」：整个项目就 **3 个池子** + 1 个描述，后台自己交叉组合成程序化创意：

| 池子 | 上限 | 控件 | 说明 |
|---|---|---|---|
| 稿件 | **200** | 「添加稿件/视频」抽屉，多次添加累加；`.material-card`，单个删 `.material-card__remove`，「一键清空」清全部 | 顶部 tab「稿件 (n)」计数 |
| 封面 | **100** | 「封面」段：`自定义封面` / `原始封面` 切换（`.cover-mode-switch` → `.custom-toggle-item`，选中 `active`）；「添加封面」`+` 卡；有 `input[type=file]`；计数「已添加 n/100 个封面」 | 自定义封面走上传；原始封面 = 用稿件自带封面 |
| 标题 | **50** | 「广告文案 · 标题」段：每个标题一个输入框 `请输入2~40个字（移动场景建议18字以内）`，计数 `0/40`；「新增标题」加行；「批量添加」批量；「一键清空」；「AI填充」；「关键词匹配」 | **2~40 字** |
| 描述 | **1 个**（不是池子！） | 「广告文案 · 描述」段：单个输入框 `请输入2 ~ 10个字，即客户端广告卡片中UP主名称位置的外显文案`，计数 `0/10` | **2~10 字，整个项目只有一条** |

其他信息：补充资质（手动选择 / 选择资质包）—— 选填，不填。
右侧「素材预览」实时预览组合效果。底部「取消 / 保存」。

### 「添加稿件/视频」抽屉（还是 iView）

- 容器 `.promote-custom-drawer.product-select-drawer.bd-drawer`（open 时加 `.open`）
- tab：bilibili账号稿件 / 花火内容稿件 / 普通内容稿件 / 我的视频 / 从素材标签选择；子 tab 品牌银行 / 三连账户
- 搜索框 `input.ivu-input`，placeholder `请输入稿件bvid或avid搜索`（同老页面）——
  填 avid + 回车即出结果卡（实测 avid `112868888414555` 秒出「赘婿 第二季」）
- 结果卡：缩略图（标 `NNNN × 1080 px` + 时长）+ 绿色「原生」标 + 标题。**点卡片即选中**
  （没有独立 checkbox，点标题文字也行）；footer `.drawer-footer` 显示 `已选 n/200` + 取消 / 确定
- 点「确定」后卡片进「稿件」池，抽屉关闭

### ⏳ 还要实跑确认
- [ ] 「批量添加」标题的输入形态（一行一个？逗号分隔？）—— 29 个标题用它比点 29 次「新增标题」快
- [ ] 「自定义封面」上传的抽屉/入口 DOM（老页面是 `input[type=file]` + 「已选 1/1」轮询）
- [ ] 封面和稿件要不要一一对应（聚合模式下大概率不对应，是整池混合）
- [ ] 素材层的必填校验：稿件≥1 + 标题≥1 + 描述，够不够保存

---

## 5. 实跑状态（2026-09-02 dry-run 联调，src/adv2_*，配置类型「原生商广新」）

✅ **一趟 dry-run 全绿**（只填不保存）：项目层 18 项 + 素材层 3 池 + 描述，实测数据
`原生商广_素材数据.xlsx`（先 2 条、后跑全量）。填出来的项目层截图逐项核对无误。

已确认可用的填法（都在 `src/adv2_filler.py`）：
- 项目名称 / 出价 / 监测链接：`bd_fill`，按 placeholder 定位（出价框在 `.bd-input` 里，
  `input[placeholder*="请输入出价(元)"]` 命中）
- 广告类型 / 推广内容 / 关联产品 / 监测链接方式 / 付费方式 / 项目预算方式 /
  投放日期方式 / 广告投放位置：`bd_radio` —— `label.bd-radio-button`，选中 `is-active`。
  ⚠ 推广目的一变，推广内容整组重渲染，`_bd_radio` 带 4 次重试
- 推广目的：`ppt_card`，`.ppt-new-item` / `.ppt-title` / `active`
- **竞价策略：`card_by_text` 命中**（卡片是 `.launch-type-item-new`，全页按「文字以
  『稳定成本投放』开头」找）
- 转化目标：`bd_select`，`.bd-select__wrapper` 点开 → `.bd-select-dropdown__list li`。
  实测「销售线索收集+OGV推广」下就 `表单提交` 一项，页面会自动选中
- **投放日期：纯日历面板**（`.bd-picker-panel.bd-date-range-picker`，range-input 只读）。
  `_pick_day` 读 `.bd-date-range-picker__header`（"2026 年 9 月"）判断月份，
  `[aria-label="上个月"/"下个月"]` 翻页，点 `table.bd-date-table` 里
  `td:not(.prev-month):not(.next-month):not(.disabled)` 的日子格
- 定向人群：`audience`，iView 抽屉（`.ivu-drawer`），人群包 `.pinpoint-radio-group` /
  `li.radio-item`，同「原生商广老」

素材层（聚合，`.media-editor`）：
- 稿件池：`add_archives` 开一次 `.product-select-drawer` 抽屉，逐个搜 avid。
  ⚠ **结果卡点标题 `.video-name` 才是选中**；点卡片本体 `.video-select-item` 会弹视频预览
  大弹窗（`.ivu-modal-wrap.fullmodal`）—— `_picker_result_card` 用 `result_item` yaml 里
  写的 `.video-select-item .video-name`，并在点错时 Escape 关预览重试
- 标题池：`add_titles`，首行已在，之后每个先点「新增标题」（`.add-button`，被 `.see-more`
  浮层挡时走 DOM click 兜底）；填值用 `_type_into`（不走 mouse click，避开浮层拦截）
- 封面池：`add_covers`，先切「自定义封面」，逐张 `input[type=file]` + 轮询「已添加 n/100」。
  超 700KB 走 `ad_image.shrink` 压（实测 683KB→67KB / 953KB→147KB）
- 描述：`set_description`，单个 `input[placeholder*="即客户端广告卡片中UP主名称位置"]`，
  先 `_dismiss_overlays()` 再 `_type_into`

⏳ 还没实跑到的（dry-run 不点保存）：
- [ ] **提交判据**：点「保存」后 URL 里 `promote/auto-v2` 是否消失 /
  失败红字是不是 `.bd-form-item__error` / `.bd-form-item.is-error`（`adv2_runner._submit` 先按这个写）
- [ ] 挂到已有项目的 URL 参数（新模型 1 项目/批，暂不需要）
- [ ] 标题「批量添加」的输入形态（>50 条时用它比点 50 次「新增标题」快 —— 现在是逐个点）
- [ ] 转化目标 / 定向包能不能走接口拿

## 6. 定位速查（已确认）

| 东西 | 选择器 |
|---|---|
| 表单项 | `.bd-form-item`（label `.bd-form-item__label`，内容 `.bd-form-item__content`） |
| 卡片单选 | `label.bd-radio-button`，选中 = `is-active` |
| 推广目的卡片 | `.ppt-new-item`（标题 `.ppt-title`，选中 = `active`） |
| 下拉 | `.bd-select__wrapper` 点开 → 浮层 `.bd-select-dropdown__list` 里 `li` |
| 日期 | `.bd-date-editor` |
| 必填 | `.bd-form-item.is-required` / 子节点 `.asterisk-right` |
| 提交 | 可见的 `button` / `.bd-button--primary`，文字「保存」 |
| 定向抽屉 | `.ivu-drawer-body`（iView，同老页面） |
| 稿件抽屉 | `.product-select-drawer`，搜索 `input.ivu-input[placeholder*="稿件bvid或avid"]` |
