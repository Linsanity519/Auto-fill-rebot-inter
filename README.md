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

产出**两个**包：

| 产物 | 大小 | 什么时候用 |
|---|---|---|
| `dist\ConfigAssistant-<ver>.zip` | ~300KB | **日常发版**。只含 `src/` + `assets/` + `main.py` |
| `dist\ConfigAssistant-Setup-<ver>.exe` | ~45MB | 首次安装；以及动了 `requirements.txt` 时 |

### 为什么拆成两个

整个程序 98MB，其中真正每次会变的只有 `src/` + `assets/`，1.6MB，压缩后 300KB；
剩下 98% 是 playwright / Pillow / CPython 这些几乎从不变的运行时。
而 GitHub Release 在内网实测只有 **20~40KB/s** —— 300KB 约 8 秒，98MB 要 40 分钟。

所以打包时把两者拆开（`build_app.spec` 用 onedir，`src/`、`assets/` 以普通文件放在
exe 旁边而不冻进包里），日常更新只换代码。运行时代号记在仓库根的 `RUNTIME_ID`：
**只有改了 `requirements.txt` 才手动 +1**。代码包声明 `min_runtime`，本机
`runtime.txt` 达不到就自动改走完整安装包，不会出现「新代码 import 到本机没有的库」。

### 发布

推一个 `vX.Y.Z` 标签，GitHub Actions 会自动打包并把三个文件（代码包、安装包、
`latest.json`）发布成 Release，不需要本机装 Inno Setup 或 GitHub CLI。

手动发版：

```bash
python tools\make_update_manifest.py --version X.Y.Z --runtime 1 --payload dist\ConfigAssistant-X.Y.Z.zip --installer dist\ConfigAssistant-Setup-X.Y.Z.exe --base-url https://github.com/Linsanity519/Auto-fill-rebot-inter/releases/download/vX.Y.Z
python tools\publish_github_release.py --version X.Y.Z --notes "本次更新说明"
```

`latest.json` 里每个包可以给**多个下载地址**，程序按顺序试到通为止。想加速就在
仓库变量 `MIRROR_BASE_URL` 里填一个国内镜像（如 Gitee Releases），CI 会把它排在
GitHub 前面。

程序启动后后台检查更新，发现新版时由用户点「更新并重启」，界面上会写明这次要下多大。

### 升级会动什么、不会动什么

**会覆盖**：程序本体、`src/`、`assets/`、`config/forms/`、`config/team.json`、
`config/webhook.txt`。

**一律保留**：`data/`、`output/`、`.chrome-profile/`、`config/settings.yaml`、
`config/strategies/`、`config/prep/`。

`settings.yaml` 不被覆盖，但新版本新增的配置项会由 `src/settings.py` 用
`assets/settings.default.yaml` 自动兜底补上 —— 否则从老版本升上来的人会因为
配置里没有 `update:` 段而**永远收不到下一次更新**。

### 从老的绿色版迁移

老同事手上是解压出来的 `配置助手分发包_vX.Y.Z\` 文件夹，而安装包装到
`%LOCALAPPDATA%\配置助手` —— 是个全新的空目录。所以安装向导里有一步「从旧版本迁移」，
会自动探测常见位置，把旧文件夹里的 `config/`（策略、准备参数、settings）、`data/`
和使用统计搬过来。不迁移的话，第一次升级在同事眼里就是「恢复出厂设置」。

浏览器登录态（`.chrome-profile`）不迁移，装好后需要重新登录一次。

### 统计回传地址

仓库是公开的，企微群机器人的 key 不能进 git。它放在 `config/webhook.txt`（已 gitignore），
打包时由 `tools/inject_release_config.py` 从环境变量 `USAGE_WEBHOOK_URL` 注入；
CI 从 Secret `USAGE_WEBHOOK_URL` 取。安装包用 `ignoreversion` 发它，
所以每次升级都会刷新 —— 这点是必需的：升级不覆盖 `settings.yaml`，
如果地址只存在那里，老用户升上来后统计就静默失效了。

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
