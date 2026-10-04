# 架构规范

状态：本地优先路线已确定，价格开关分层试点已实现，生产验证与其余能力迁移待完成。规范日期：2026-10-04。

## A01：部署与依赖方向

采用模块化本地应用与轻量端口/适配器。分层不要求拆进程或服务器。未来需要集中管理时通过外部适配扩展，当前不引入微服务、分布式队列或组织级认证基础设施作为运行前提。

```text
桌面 / CLI / MCP → application → domain
                       ↑
                 adapters 实现端口
bootstrap 组装这些对象
```

运行时用例调用适配器，代码层面端口由 application 声明。domain 不 import application 或具体技术；application 不 import adapters。入口只转换数据和调用用例，不能直接操作平台。

| 层 | 允许的项目依赖 | 禁止 |
|---|---|---|
| domain | domain | UI、数据库、网络、文件读写、运行配置、其他层 |
| application | application、domain | Playwright、pywebview、SQLite/ORM、HTTP 客户端及旧 runner |
| adapters | adapters、application、domain | 入口、bootstrap；直接导入旧代码须放 adapters/legacy |
| entrypoints | entrypoints、application、domain、bootstrap | 直接导入 adapters 或旧 runner |
| bootstrap | domain、application、adapters、bootstrap | 入口；新增规则和业务分支 |

domain/application 使用检查器中列出的纯计算标准库。时钟、文件、网络、存储通过端口传入；扩充允许库需说明使用方式，不通过动态导入绕过。外层可用第三方库，但仍要遵守依赖维护规则。

## A02：迁移方式

- 原有 80 个 src Python 文件通过精确清单登记为存量区；不是这些文件已经合规的证明。
- 修复旧文件缺陷可以小改，不强制同时重写；新增可复用业务规则与新增用例进入新分层。
- 原有注册表可通过工厂接入新用例；确需复用旧填表器的适配逻辑放 adapters/legacy，以免核心依赖旧执行器。
- 对照样例、离线验证和平台核验按业务切片推进。旧代码被替换后再删除，更新存量清单并说明迁移目的。
- 运行代码继续放 src 下，符合现有代码包边界。不得顺手修改 PAYLOAD_MEMBERS；旧 updater 的兼容性需要独立方案。
- tools/new_mode.py 已改为生成分层且未注册的能力骨架；补齐契约与测试后再在 bootstrap/registry 接线，不把占位实现当作可执行功能。

## A03：组织与扩展

domain/application 内部可按 pricing、delivery、authorization 等领域分包。只有有真实复用需求时才提取公共模块。禁止建一个包含业务、页面、SQL 与 UI 的万能 utils/service/manager。

适配器表达业务能力，例如读取策略、设置目标状态、查询授权结果。Page、Locator、数据库连接、HTTP response 不得跨入业务输入输出；返回标准化业务对象与错误。

平台 API、浏览器会话 API 和 Playwright 可以并存。开始执行前确定适配器。提交结果未知时不能切换适配器重复提交。页面私有签名等依赖限于对应平台适配器，记录兼容条件。

bootstrap 负责依赖构造与配置注入，不保存业务规则。每个入口使用相同服务实例约定；多个进程仍需进程间互斥或单一任务宿主，导入同一个包不等于共享运行状态。

## A04：数据与执行

本机任务库保存输入快照、规则版本、计划、对象与事件；文件附件保存引用和校验值。数据库迁移有版本、备份与失败策略。不能将正在使用的 SQLite 文件放到网络盘上由多人写入。

外部平台提交与 SQLite 事务不是一个原子事务。提交前记录意图，提交后核验，无法确认时标为未知并停止盲目重提。账号、环境、父对象与业务 ID 构成操作上下文。

统计上传失败与执行记录写入失败分别处理。完整档案导出/import/sync 使用稳定 ID 去重，保留事件顺序与冲突记录，展示归集时间和缺失状态；同步历史不等于获得跨电脑执行协调。

## A05：架构决策

以下改动需 ADR：改变层间依赖；改变计划确认或任务恢复语义；改变默认存储/上传位置；改变认证归属；引入新的部署组件；改变安装和更新兼容边界。

ADR 至少包含背景、选择、备选、数据/用户影响、迁移回退、验证和状态。普通实现、修复和文件拆分无需逐项写 ADR。参考 [模板](decisions/TEMPLATE.md)。
