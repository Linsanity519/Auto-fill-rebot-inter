# DMP 人群新建（临时表创建）

按 Excel 批量在 DMP 人群管理里用「临时表创建」新建人群包。

页面：<https://bangumi-mng.bilibili.co/marin/s/full-ogv-data/user-assets/crowd-manage/crowd-list>

配置：`config/forms/DMP人群新建.yaml`，走通用 Runner（不是 `DMP延期` 那套专用执行器）。

## 怎么用

1. 生成模板 → 得到 `data/DMP人群新建_模板.xlsx`

   ```bash
   python main.py --make-template --form DMP人群新建
   ```

2. 一行填一个人群包，列见下表。
3. 先空跑（只填不提交，每条填完截图后自动取消）：

   ```bash
   python main.py --cli --form DMP人群新建 --dry-run --data data/你的表.xlsx
   ```

4. 核对 `output/screenshots/` 里的截图无误后正式跑。默认逐条确认，`y` 提交 / `n` 跳过 / `a` 之后全自动 / `q` 退出：

   ```bash
   python main.py --cli --form DMP人群新建 --data data/你的表.xlsx
   ```

图形界面里选「DMP人群新建」是同样的流程。

## Excel 列

| 列 | 必填 | 说明 |
|---|---|---|
| 人群名称 | ✅ | 字母、数字、汉字、下划线和括号 |
| 表名 | ✅ | **必须是你自己有权限的表**，否则页面会在点「确定」时报错 |
| 匹配类型 | | 页面目前只有 `mid` 一个选项且默认选中，留空即可 |
| 人群时效性 | ✅ | `天级` / `小时级` |
| key值 | ✅ | 表里作为人群 key 的字段名 |
| 人群有效期 | ✅ | `7天` / `15天` / `30天` |
| 推广形式 | | `大会员后台` / `天马（智能投放平台）`，多选用英文逗号分隔。留空 = 只保留页面默认的 titan |

`天级` / `7天` 这类固定选项在模板里做成了 Excel 下拉，填错在录入时就会被拦下；
真跑起来 Filler 也会再校验一次，不会拿错值去撞页面。

## 页面上的坑

- **antd class 前缀不是 `ant-`**：这套前端打包成了 `full_ogv_data_antd-`。
  配置里的 `antd_prefix` 就是干这个用的，Filler 内部所有 antd 选择器都按它拼。
- **要点两下才到表单**：列表页「新建人群」→ 方式弹窗里选「临时表创建」。
  5 个方式的按钮文字都是「创 建」（antd 给双字中文按钮插了空格），
  只能靠所在 `list-item` 的标题区分，所以 `open_steps` 第二步用的是
  `li:has(.list-item-meta-title:text-is("临时表创建")) button`。
- **两个弹窗会同时存在**：方式弹窗不会因为打开了临时表弹窗而关掉。
  所以判断弹窗开/关一律用临时表弹窗独有的 `#table_name`，不要用 `.modal`。
- **推广形式里的 titan 是禁用且默认勾选的**：`checkbox_sync` 会跳过禁用项。
  早期版本会去点它取消勾选，而 Playwright 点不动禁用元素，只会一路等到超时。
- **每条都回列表页重开弹窗**（`reset_between_rows: true`），
  慢几秒，但不会把上一条的残留状态带到下一条。

## 判成功的依据

点「确定」之后等 `#table_name` 消失才算成功，最多等 `config/settings.yaml` 里的 `timeout`（默认 15 秒）。
要是实际用下来发现建人群的接口比这慢，会被误判成「点了确定但弹窗没关闭」，把 `timeout` 调大即可。


点了没关说明后端校验没过（最常见的是表名没权限），
这时会把页面上的报错文字抓出来写进 `output/result.csv` 的「错误」列，并存一张失败现场截图。
