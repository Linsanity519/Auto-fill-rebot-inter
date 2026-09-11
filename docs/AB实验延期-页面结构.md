# AB 实验延期 — 页面结构 / 接口抓取

抓取来源：`http://abtest.bilibili.co/#/abtest/list?space=abtest`（prod），2026-09-11
在 CDP Chrome 里**新开一个标签页**抓的（Element Plus 页面）。
试跑（打开弹窗、选日期、取消）跑过两种范围；同日真实提交过 1 次
（15866：2026-11-29 → 2026-12-10，二次确认气泡 → 弹窗关闭，接口复查 expirationTime 已变）。

对应配置：[config/forms/AB实验延期.yaml](../config/forms/AB实验延期.yaml)　执行器：[src/ab_runner.py](../src/ab_runner.py)

---

## 一、最容易踩的坑

1. **列表刷新要盯接口，不能按秒数等。** 点「我的实验」、翻页、搜索都会重新拉
   `/ab/v3/experiment/list`，实测 0.3~0.6 秒回来（内网卡的时候会久得多）。
   点完到接口回来之间，表格上摆的还是**上一屏**，而且它是静止的 ——「看起来安静了」一判就过。
2. **搜索会连发两个请求**：先按「当前页码 + 新搜索词」发一个，紧接着按「第 1 页 + 新搜索词」
   再发一个（实测 `currentSize=2&queryParam=123` → `currentSize=1&queryParam=123`，间隔 0.3 秒）。
   第一个回来时第二个可能还没发出去 —— 只看「请求都回来了」会读到旧页码那一个。
   执行器按「最后发出去的那个」读，并要求它是 `currentSize=1&queryParam=<词>`。
3. **搜索框里已经是这个词时再按回车，页面不发请求。** 要逼它重新请求得先清空再搜。
4. **URL 是 # 路由。** 已经停在这个站点上时 `goto` 同一个地址只改 # 后面，页面不重新加载、
   列表也不重新请求 —— 执行器先跳 `about:blank` 再回来。
5. **行上没有 `data-row-key`。** 实验 ID 只在「实验名称」那一格的文字里（`ID:15937`）。
   定位一行按这段文字过滤（`ID[:：]\s*15937(?!\d)`），不用 `nth(i)` 按下标。
6. **页面里有三张 `.el-table`**（实验管理 / 联调实验 / 发布管理），只有一张可见；
   `.my-test`（我的实验）和 `.el-pagination` 也各有三份。一律只认 `:visible` 的。
7. **每一行都有自己的下拉菜单挂在 DOM 里**（实测 17 个 `.el-dropdown-menu`），只有刚展开的那个可见。

---

## 二、列表接口

```
GET http://abtest.bilibili.co/ab/v3/experiment/list
    ?name=&platform=&layerId=&departmentName=&bizName=
    &userId=55798          ← 点了「我的实验」才有（当前登录人的 userId）
    &currentSize=1         ← 页码，从 1 开始
    &perPageSize=15
    &queryParam=           ← 搜索框里的词（实验名称 / 创建人 / 实验ID）
```

同域请求，没有 OPTIONS 预检（执行器仍然只认 GET）。响应：

```json
{
  "code": 200,                       ← 成功是 200，不是 0
  "msg": "success",
  "items": [
    {"id": 15937, "name": "【子实验】EP券拉新实验_26年H2", "runStatus": 2,
     "expirationTime": "2026-11-28 23:59:59", "runStartTime": 1788508360000,
     "userId": 55798, "userName": "linzifan01", "nickName": "子凡2.0",
     "layerId": 8192, "layerName": "…", "parentId": 15753, "flowPercent": 50, …}
  ],
  "pageVO": {"currentSize": 1, "perPageSize": 15, "totalPageSize": 18, "totalSize": 267}
}
```

- `items[].id` 的顺序 = 表格行的顺序（实测逐行对得上）→ 「表格渲染成这一批了」的判据
- `pageVO.totalPageSize` → 「最后一页」的判据（不靠「下一页点不动」猜）
- `runStatus`：2 = 实验中，3 = 已结束（「我的实验」第 2 页起全是 3），4 在搜索结果里见过（老实验）
- 搜索时 `userId` 照带，但**结果不受它限制**（搜 `123` 搜到了别人的老实验 123）
- 同批还会发 `/ab/v3/experiment/statistics`、`/ab/v3/layer/list` —— 路径不同，别算进列表请求

「我的实验」按实验状态分组排：实验中全部排在最前（第 1 页 15 条全是 2，第 2 页前 5 条是 2、
后面全是 3）。所以扫到第一个非「实验中」就可以停。

---

## 三、表格列（0 起，可见那张表）

| # | 列名 | 例 |
|---|---|---|
| 0 | 实验名称 | `【子实验】EP券拉新实验_26年H2\nID:15937\n父子实验` —— 第一行是名称，`ID:` 那行是实验ID |
| 1 | 状态 | `实验中`（精确匹配，名称里带「实验中」的不能算） |
| 2 | 流量 | `50 %` |
| 3 | 开始/结束时间 | `2026-09-04\n2026-11-28`（取最后一行；搜索结果页偶尔渲染不全，执行器优先用接口的 `expirationTime`） |
| 4 | 创建人 | `@linzifan01(子凡2.0)` |
| 5 | 部门 | |
| 6 | 授权人数 | |
| 7 | 已运行/总天数 | `8/86天` |
| 8 | 创建/更新时间 | |
| 9 | 平台 | `APP` |
| 10 | 实验层 | |
| 11 | 操作 | `分流配置 / 实验数据 / 复制 / 其他` |

分页器：`.el-pagination .btn-next` / `.btn-prev`（`button`，到头时带 `disabled`），
当前页 `.el-pager li.is-active`。每页 15 条。

---

## 四、「其他 → 续期」

- 操作列最后是 `div.el-dropdown > span[role=button]`「其他」，hover 展开（点击也行，Playwright 点之前会先把鼠标挪上去）
- 菜单 `ul.el-dropdown-menu > li.el-dropdown-menu__item`：实验基础配置 / 分流配置 / 实验数据 / 分流命中查询 / … / 续期
- 点「续期」→ **页面马上发 `GET /ab/v3/experiment/15937`（详情）** —— 弹窗上不显示实验名称，
  执行器拿这个请求里的 ID 核对「弹窗是不是目标实验的」

## 五、续期弹窗

```
div.el-overlay-dialog[aria-label=实验续期]
  div.el-dialog
    header  .el-dialog__title「实验续期」
    body    label「实验到期日期」 + input.el-input__inner[placeholder=选择日期]   ← 值：2026-11-28
    footer  button「取消」  button.el-button--primary「 续期 」（文字两边带空格）
```

- DOM 里只有一个 `.el-dialog`，关掉后隐藏（`:visible` 判断开/关）
- 输入框的值是详情接口回来之后才填的；打开日期面板前要等它填好
- 点「续期」后还有一个 el-popconfirm「确定续期吗?」，确定按钮 `.el-popconfirm__action .el-button--primary`
  （2026-09-11 真实提交验证过：点确定后弹窗关闭，整个提交约 1.3 秒）

## 六、日期面板

- `div.el-picker-panel.el-date-picker`（可见那个），标题是**两个** `span.el-date-picker__header-label`：「2026 年」「11 月」
- 翻月：`.el-picker-panel__icon-btn.arrow-left` / `.arrow-right`（旁边还有 `d-arrow-left` 是翻年，别选错）
- 日期格 `.el-date-table td`，本月可选 = `td.available:not(.disabled):not(.prev-month):not(.next-month)`
- 实测：到期日 2026-11-28 的实验（开始 09-04，总 86 天），11 月只有 1~28 可选 —— 上限就是现在的到期日，
  执行器会判「已是最晚可选日期」跳过

## 七、关弹窗（Esc）

实测：日期面板开着时按 Esc，**第一下只收掉面板**（300ms 内），弹窗还在；**第二下才关弹窗**（300ms 内）。
执行器按「面板开着就先收面板 → 再收弹窗 → 都不行才点取消」来，每一步等它真的消失。
