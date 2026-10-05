# 本轮源码的使用与验收

本轮提供 M0–M3 基础实现与可选试点，尚不是完成内网验收的正式安装包。M4 MCP/Skill、M5 全功能迁移、M6 打包升级继续按[迭代方案](ITERATION_PLAN.md)规划。

## 日常界面

- 默认保留现有配置类型、模板和执行入口。
- “只读预检”不填写、不创建、不提交。“真实填写演练”和“逐步填写演练”可能创建活动或单元，结束后要检查平台产物。
- 侧栏“任务与完整档案”查看计划、记录、结果和归集状态。导出包在任务目录的 exports；导入 ZIP 或旧运行清单 JSON 只供查阅，不执行配置。
- 普通内容授权预检失败时阻止提交；历史申请次数的配额口径未核实，未知项不得全自动申请。逐条模式需人明确声明已在平台核实，不把它记成程序验证了配额。
- 断点损坏或保存失败会停止。旧通用行号断点不能猜测映射，应先备份并核对平台。待核查提交不能用“清除断点”绕过。

## 价格开关试点

勾选“价格开关任务宿主试点”，然后重新载入并检查。调试浏览器中需有唯一价格策略页；多个候选时保留一个，或设置 platform_page_url 精确匹配。适配器新建自己的标签页，结束只关闭自己的标签页。

计划显示环境、会话指纹、策略、对象与前后变化。平台 row-key 重载后可能变化，当前不能承诺它是稳定业务 ID；不匹配就停止。真实验收前默认不启用试点。

自动任务由独立宿主持有，界面离开不终止宿主。逐条确认可从任务档案“连接执行进度”，30 分钟无响应后停止后续操作。中断任务“只读核验”不重提。翻回仅准备指定任务的反向计划，核对后再执行；旧清单缺证据时不能自动翻回。

命令行示例：

    python main.py --cli --form 价格策略批量开关 --task-core --pt-scope list --toggle-params 示例人群 --toggle-direction off --dry-run
    python main.py --tasks list
    python main.py --tasks show task_实际编号
    python main.py --tasks reconcile task_实际编号
    python main.py --tasks reverse task_实际编号
    python main.py --tasks export task_实际编号 --output D:\私有档案\本次配置.zip
    python main.py --tasks import D:\私有档案\本次配置.zip
    python main.py --tasks import-legacy D:\私有档案\旧运行清单.json

去掉 --dry-run 后默认逐条确认；--auto 才是不再逐条询问。reverse 返回计划，不直接执行。

## 数据与备份

默认 %LOCALAPPDATA%\ConfigAssistant，包含 tasks.sqlite3、附件、宿主配置/日志与导出包，不需要数据库服务器。CONFIG_ASSISTANT_HOME 或 settings.yaml 的 task_data_dir 可指定本机私有目录。同一目录绑定执行设置；不能靠换目录规避历史未知任务。

旧配置、台账、输入仍在安装目录，M5 逐项迁移，不要先删除。应用及宿主停止后可整体备份任务目录；在线备份用 SQLite backup 方法并保留附件。只复制运行中的主数据库可能遗漏 WAL 数据。

## 完整表格归集

原摘要回传沿用现有配置。完整档案等待真实表结构核实，默认不会向未验证列发送大段业务数据。

archive_collection 包含 enabled、schema_verified、chunk_chars，以及 fields 对 archive_id/index/count/package_sha256/content/schema_version 六项的真实列 ID 映射。不能猜列名，也不能把默认分块大小当成平台容量承诺。

字段与容量核实后：

1. 用两个隔离目录和专用测试任务验证客户端补传。
2. 维护者用自己的 wecom-cli 授权及经验证的分页 SELECT，六列设对应别名，按稳定顺序读取，以 {limit}/{offset} 占位。平台不支持 OFFSET 时需适配其真实分页，不能删掉完整性检查。
3. 执行下列收集命令，或以 --input 文件.json 代替 SQL 做本地联调。
4. 收齐并验证内容/附件后返回回执，客户端验证回执再标记完整。自动回执通道尚待权限核实。

    python tools/collect_archives.py --sql-template 私有分页.sql --database-dir 私有归集目录 --receipts-dir 私有回执目录
    python main.py --tasks receipt 回执.json

接口已接收不等于完整归集；超时补发产生的重复块会去重，同一编号不同内容报告冲突。

## 验收

    python tools/check_project.py
    python tools/test_task_host_process.py
    python tools/ui_smoke.py --executable "本机 Chrome 或 Edge 路径"

最后一项只打开本地界面夹具并阻断 HTTP/HTTPS。真实业务 ID、登录态、页面回读、表字段/容量/读取授权、附件归集与安装升级须分别实测。
