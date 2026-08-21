# 内网表单自动配置机器人

Python + Playwright，挂载到你**已登录**的 Chrome，读 Excel 批量填表，填完停下等你确认再提交。

**脚本不处理账号密码，登录始终由你本人在浏览器里完成。**

## 一次性准备

```bash
pip install -r requirements.txt
```

不需要 `playwright install`——我们挂载用户自己的 Chrome，不用 Playwright 自带的浏览器内核。

## 打包、首次安装与后续更新

```bash
build.bat
```

产出 `dist\配置助手-Setup-X.Y.Z.exe`。首次把这个安装包发给同事；它会安装到当前用户的
`%LOCALAPPDATA%\配置助手`，对方只需要 Chrome，不需要 Python 或管理员权限。

从带更新功能的首版开始，在 `config/settings.yaml` 填入 `update.manifest_url` 并启用更新。
每次发新版后，执行：

```bash
python tools\make_update_manifest.py --version X.Y.Z --installer dist\配置助手-Setup-X.Y.Z.exe --base-url https://你的发布目录 --notes "本次更新说明"
```

将安装包和生成的 `dist\latest.json` 发布到同一个目录。程序启动后会后台检查，发现新版时由
用户点击「更新并重启」完成下载、校验、安装与重启。

升级只替换程序、表单映射和团队统计快照；`data/`、`output/`、`.chrome-profile/`、本地策略和
准备参数都会保留。`config/settings.yaml` 也不会被覆盖，新增配置项应由版本迁移或手动补充。

**`.chrome-profile` 目录含登录凭据，不要从旧分发文件夹复制或发送给他人。**

## 每次使用

1. 双击 `start_chrome.bat`，在弹出的 Chrome 里登录内网系统（登录态会存在 `.chrome-profile`，之后免登）
2. 把数据填进 `data/input.xlsx`，**表头必须和 `config/fields.yaml` 里的 `name` 一致**
3. 先空跑验证：

```bash
python main.py --dry-run --row 1
```

4. 确认无误后正式跑：

```bash
python main.py
```

每条填完会截图并问你 `y/n/a/q`：提交 / 跳过 / 之后全自动 / 退出。

## 目录

## 多套配置

每个表单一个 profile，放在 `config/forms/` 下：

```bash
python main.py --form 价格配置
```

不加 `--form` 会列出所有可选表单让你挑。断点 (`state.json`) 按表单名隔离，互不干扰。

新增一个表单 = 在 `config/forms/` 里加一个 yaml，不用改代码。

### profile 支持的表达能力

| 能力 | 写法 | 解决的问题 |
|---|---|---|
| 条件字段 | `reveals:` | 选了某个值才出现的字段（人群选组 → 人群选择） |
| 明细行变体 | `list.variants_by` + `variants` | 明细字段随主表某字段变化（限制类型 → 均价/兜底两套字段） |
| 虚拟滚动下拉 | `virtual_scroll: true` | antd 只渲染可视区选项，需滚动查找（会员卡种 25 项） |
| 默认全选的复选组 | `type: checkbox_sync` | 双向同步，取消不该勾的（平台默认全选） |
| 可搜索下拉 | `search: true` | 选项极多时打字过滤（dmp 人群包） |
| 多步打开弹窗 | `open_steps:` | 要点两下才到表单（DMP 新建人群 → 临时表创建） |
| 非默认 antd 前缀 | `antd_prefix:` | 前端改了 antd 的 class 前缀（DMP 是 `full_ogv_data_antd-`） |

| 路径 | 作用 |
|---|---|
| `config/forms/*.yaml` | **各表单的字段映射，唯一需要按页面改的文件** |
| `config/settings.yaml` | 运行参数（超时、是否确认、断点续跑） |
| `src/filler.py` | 各类控件的填写逻辑，新增控件类型在这里加 |
| `src/runner.py` | 主流程、截图、确认、状态记录 |
| `output/state.json` | 断点，中断后重跑会跳过已成功的行 |
| `output/result.csv` | 每行的成功/失败明细 |
| `output/screenshots/` | 每条的填写截图 + 失败现场 |

## 常用命令

```bash
python main.py --row 3 --dry-run
```

```bash
python main.py --no-resume
```
