# 规范如何执行

## 三个层次

| 层次 | 已提供 | 实际保证 |
|---|---|---|
| AI 指引 | 根 AGENTS.md；CLAUDE.md 引用；开发/架构/业务契约 | 为读取这些文件的 AI 提供统一约定，不保证所有宿主都自动加载 |
| 自动验收 | check_architecture.py、check_project.py、检查器测试 | 对确定的静态违规返回非零；统一运行已登记离线检查 |
| 合并与发版 | quality.yml；release.yml 依赖 quality；build.bat 前置检查 | 上传代码后 CI 才运行；阻止不合格合并还需仓库端启用必需状态检查 |

本次仅编辑本地仓库并执行本地检查。没有推送、创建发布、改变远端仓库规则或声称 GitHub 已启用保护。

## 自动检查覆盖范围

- A000：src Python 语法、重复模块与源文件读取问题。
- A001：新增运行模块不能进入原 src 平铺结构或未知目录。
- A002：新代码的项目依赖方向；旧模块只通过 adapters/legacy 接入新层。
- A003：domain/application 非允许库导入；入口直接访问常见浏览器、数据库或 HTTP 客户端。
- A004：核心直接调用 open/input/eval/exec/__import__/compile。
- A005：涉及新模块的静态导入循环，包括经过旧模块的循环。
- 验收脚本核对 tools/test_*.py 登记，未登记或意外删除测试会失败。

存量清单只豁免旧模块的布局和依赖，不豁免语法。纯旧模块循环暂不阻断。新规则落在首个新模块上就会检查；没有新领域包不代表旧项目已经达到目标架构。

静态检查不能证明无全部 I/O、反射、动态依赖或业务越权，也不验证前端代码中是否出现业务重复。它是工程检查，不是对不可信代码的安全沙箱。业务合同、授权、事务和恢复由行为测试及评审保障。不得将“检查绿了”解释成“所有架构规则均已自动证明”。

## 使用命令

```text
python tools/check_project.py --static-only
python tools/check_project.py
```

静态模式运行架构检查和 tests/governance 下的标准库 unittest。完整模式发现 tests 下所有 unittest，并运行 check_mode 和 18 个已登记离线脚本。输出位于 output/quality/summary.json 及各脚本日志，属于本机产物。

执行子进程会阻断常见网络连接、绑定端口与再次启动进程的 Python 审计事件。阻断只是避免意外访问，不能代替系统级隔离。

以下四个旧脚本没有计入离线通过：test_update、test_updater、test_filler_locate、test_ad_reg_titles。更改对应业务时，根据环境另行运行并说明结果。新增普通 unittest 子目录需要 __init__.py，避免发现器漏收。

## GitHub 合并保护的生效步骤

1. 将本次工程文件随正常开发分支提交到仓库。
2. 让 Project quality 工作流在一次 PR 上完成，确认实际产生的 quality 检查名称与来源。
3. 在实际默认分支的 Rulesets 或 Branch protection 中，将该检查设为必需，来源指定 GitHub Actions，并要求合并前分支保持最新或采用相应合并策略。
4. 若维护者希望任何人都不能绕过，移除不必要的绕过权限；单人项目不强制增加必须由第二个人审批的门槛。
5. 规范、检查脚本和工作流改动也要审阅。仅把检查器与业务代码放在同一个 PR 内，不能防止有权限的人恶意同时删除规则；若未来需要这种强度，可使用组织级外部规则/独立检查器。

仓库规则需要维护权限；本地文件无法自行启用它。不要把“写好了工作流”报告为“远端合并已被强制保护”。[GitHub 必需状态检查](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-protected-branches/about-protected-branches)

release 工作流在同次发布运行中依赖质量作业，本地 build.bat 也在改版本号前运行验收，手工发布脚本 publish_github_release.py 在调用 gh 发布前运行同一检查。手工在 GitHub 网页上传其他安装包仍然不受本地脚本控制；程序测试与产物出处不能由这些文件单独保证。

## AI 客户端与启动方式

在配置助手仓库根目录开始开发。支持 AGENTS.md 的宿主按自身规则读取；Claude Code 的 CLAUDE.md 用 @AGENTS.md 引用同一份规范。[OpenAI 指令文件说明](https://learn.chatgpt.com/docs/agent-configuration/agents-md)、[Claude Code 引用说明](https://code.claude.com/docs/en/memory)

对不能自动发现指令文件的客户端，可直接发送：

> 请先读取仓库根 AGENTS.md 和其中要求的工程规范。确认本次目标属于哪些层，遵守本地部署、统一业务契约和数据恢复要求。在现有授权范围内完成实现，执行规定检查，报告实际结果与未验证事项；不要为了通过检查修改豁免或弱化规则。

存在更近的覆盖文件、客户端禁用项目指令或指令长度限制时，加载行为可能不同。使用前让客户端列出读取的规范可以排查，但自然语言自述仍不能代替自动验收。

## 规范演进与遗留债务

调整架构、业务状态、默认数据去向等原则时写 ADR，明确维护者采用依据。清单只登记真实旧路径，不能把新模块伪装为旧模块。不要提供“一键重新生成全部豁免”作为日常通过方式。

本轮断点/持久化/重复启动已有针对性修复与离线回归；其余业务迁移、依赖锁定、真实平台验证和远端保护仍未完成，详见 IMPLEMENTATION_STATUS.md。规则和 CI 不能证明未知的平台事实。
