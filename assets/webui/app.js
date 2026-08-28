/* 阶段2：三步面板接上真实 Runner。
 *
 * Python 端（src/webapp.py）：
 *   - load_and_check / row_detail / make_template / pick_file / clear_state / open_output_dir
 *     是一问一答的普通调用
 *   - start_run 之后，Runner 在后台线程跑，通过 window.app.onLog/onProgress/onConfirm/
 *     onAskContinue/onFinished/onRunDone these 几个入口把事件推过来（WebUI._push 调的
 *     就是这几个），不是本文件主动去问
 *
 * hasBackend()=false（比如直接拿普通浏览器打开这个文件核对样式）时，
 * callApi() 走本地假数据，方便不启动 pywebview 也能看外壳。
 */
(function () {
  "use strict";

  // ⚠ 不能缓存成常量：pywebview 的 window.pywebview.api 是异步注入的，
  //   脚本刚执行的这一刻它大概率还没就绪。缓存下来的话第一次判断落到
  //   false 就永远走假数据分支了（实测：browser_status 轮询一直卡在
  //   "未连接"，即使真的连上了）。改成每次调用现查。
  function hasBackend() { return !!(window.pywebview && window.pywebview.api); }

  // 只有网址上带 ?demo 才允许拿假的统计数据充数，见 callApi 里的说明
  const DEMO = String(location.search || "").indexOf("demo") >= 0;

  // group / label / *_order 对应 config/forms/*.yaml 里的 nav 段，
  // 真实值来自 list_forms，这里只是没有后端时的样子货
  // ⚠ 这份假数据要和 list_forms 的真实返回**同构**（含 caps / ui），
  //   否则拿普通浏览器打开 index.html 核对样式时，卡片的显隐会和真机不一样。
  //   加了配置类型或改了 _caps 之后，重新生成一遍：
  //     python tools\gen_stub_forms.py
  const STUB_FORMS = [
    {"name": "DMP延期", "mode": "dmp_extension", "caps": {"strategy": false, "prep": false, "positions": false, "activity": false, "task_list": false, "excel": true, "toggle": false, "flow": false}, "ui": {"deliver_label": "投放配置", "deliver_hint": "配好策略 → 生成模板 → 填好 Excel → 载入并检查 → 跑", "strategy_hint": "配在这里的字段，模板里就不用逐个单元填了", "run_kind": "fill", "params_label": "名称关键词", "params_placeholder": "一行一个关键词，命中即算。留空 = 整页所有行", "strategy_label": "策略", "strategy_placeholder": "留空 = 当前打开的策略页。跨策略：一行一个，编辑页URL / 路由ID / 业务ID", "toggle_hint": ""}, "group": "DMP人群包", "group_order": 1, "label": "DMP人群延期", "order": 1, "desc": "大会员 DMP 人群管理 - 批量把人群有效期延长", "scopes": [["全部生效中 → 最晚日期", "active"], ["我创建的 → 最晚日期", "mine"], ["按清单指定人群ID", "id_list"]]},
    {"name": "DMP人群新建", "mode": null, "caps": {"strategy": false, "prep": false, "positions": false, "activity": false, "task_list": false, "excel": true, "toggle": false, "flow": false}, "ui": {"deliver_label": "投放配置", "deliver_hint": "配好策略 → 生成模板 → 填好 Excel → 载入并检查 → 跑", "strategy_hint": "配在这里的字段，模板里就不用逐个单元填了", "run_kind": "fill", "params_label": "名称关键词", "params_placeholder": "一行一个关键词，命中即算。留空 = 整页所有行", "strategy_label": "策略", "strategy_placeholder": "留空 = 当前打开的策略页。跨策略：一行一个，编辑页URL / 路由ID / 业务ID", "toggle_hint": ""}, "group": "DMP人群包", "group_order": 1, "label": "DMP人群新建", "order": 2, "desc": "大会员 DMP 人群管理 - 按 Excel 批量用「临时表创建」新建人群包", "scopes": []},
    {"name": "AB实验延期", "mode": "ab_extension", "caps": {"strategy": false, "prep": false, "positions": false, "activity": false, "task_list": false, "excel": true, "toggle": false, "flow": false}, "ui": {"deliver_label": "投放配置", "deliver_hint": "配好策略 → 生成模板 → 填好 Excel → 载入并检查 → 跑", "strategy_hint": "配在这里的字段，模板里就不用逐个单元填了", "run_kind": "fill", "params_label": "名称关键词", "params_placeholder": "一行一个关键词，命中即算。留空 = 整页所有行", "strategy_label": "策略", "strategy_placeholder": "留空 = 当前打开的策略页。跨策略：一行一个，编辑页URL / 路由ID / 业务ID", "toggle_hint": ""}, "group": "AB实验", "group_order": 2, "label": "AB实验延期", "order": 1, "desc": "AB 实验平台 - 把「我的实验」里所有「实验中」的实验续期到平台允许的最晚日期", "scopes": [["我的实验 → 最晚日期", "mine"], ["按清单指定实验ID", "id_list"]]},
    {"name": "价格配置", "mode": null, "caps": {"strategy": false, "prep": false, "positions": false, "activity": false, "task_list": false, "excel": true, "toggle": false, "flow": false}, "ui": {"deliver_label": "投放配置", "deliver_hint": "配好策略 → 生成模板 → 填好 Excel → 载入并检查 → 跑", "strategy_hint": "配在这里的字段，模板里就不用逐个单元填了", "run_kind": "fill", "params_label": "名称关键词", "params_placeholder": "一行一个关键词，命中即算。留空 = 整页所有行", "strategy_label": "策略", "strategy_placeholder": "留空 = 当前打开的策略页。跨策略：一行一个，编辑页URL / 路由ID / 业务ID", "toggle_hint": ""}, "group": "价格", "group_order": 3, "label": "价格策略配置", "order": 1, "desc": "策略中心 - 算法价格人群配置", "scopes": []},
    {"name": "价格面板配置", "mode": "price_panel", "caps": {"strategy": true, "prep": true, "positions": false, "activity": true, "task_list": false, "excel": true, "toggle": false, "flow": false}, "ui": {"deliver_label": "投放配置", "deliver_hint": "配好策略 → 生成模板 → 填好 Excel → 载入并检查 → 跑", "strategy_hint": "生效平台、流量池、收银台类型、面板设置、每个 SKU 的搭售…… 配在这里，Excel 里就只剩活动和这个面板放哪几个 SKU", "run_kind": "fill", "params_label": "名称关键词", "params_placeholder": "一行一个关键词，命中即算。留空 = 整页所有行", "strategy_label": "策略", "strategy_placeholder": "留空 = 当前打开的策略页。跨策略：一行一个，编辑页URL / 路由ID / 业务ID", "toggle_hint": ""}, "group": "价格", "group_order": 3, "label": "价格面板配置", "order": 2, "desc": "大会员投放系统（老后台）- 收银台价格面板单元配置", "scopes": []},
    {"name": "价格策略批量开关", "mode": "pt_toggle", "caps": {"strategy": false, "prep": false, "positions": false, "activity": false, "task_list": false, "excel": false, "toggle": true, "flow": false}, "ui": {"deliver_label": "投放配置", "deliver_hint": "配好策略 → 生成模板 → 填好 Excel → 载入并检查 → 跑", "strategy_hint": "配在这里的字段，模板里就不用逐个单元填了", "run_kind": "fill", "params_label": "名称关键词", "params_placeholder": "一行一个关键词，命中即算。留空 = 整页所有行", "strategy_label": "策略", "strategy_placeholder": "留空 = 当前打开的策略页。跨策略：一行一个，编辑页URL / 路由ID / 业务ID", "toggle_hint": "只点「操作」列的开启/关闭，一键直接生效、没有二次确认。已是目标状态的、以及开启方向下人群选组=不限的，自动跳过。跨策略是尽力而为——最稳的用法是自己在浏览器里打开那条策略页，用「当前打开的策略页」"}, "group": "价格", "group_order": 3, "label": "价格策略批量开关", "order": 3, "desc": "策略中心 - 把「价格配置」表里已配好的行批量开启 / 关闭（界面上切方向）", "scopes": [["按名称关键词", "keyword"], ["本工具配置过的", "ledger"], ["按清单", "list"]]},
    {"name": "资源位投放", "mode": "wizard", "caps": {"strategy": true, "prep": false, "positions": true, "activity": true, "task_list": false, "excel": true, "toggle": false, "flow": false}, "ui": {"deliver_label": "资源位投放配置", "deliver_hint": "选资源位 → 生成模板 → 填好 Excel → 载入并检查 → 跑", "strategy_hint": "生效平台、流量池、频次、人群、内容限制…… 配在这里，模板里就不用逐个单元填了", "run_kind": "fill", "params_label": "名称关键词", "params_placeholder": "一行一个关键词，命中即算。留空 = 整页所有行", "strategy_label": "策略", "strategy_placeholder": "留空 = 当前打开的策略页。跨策略：一行一个，编辑页URL / 路由ID / 业务ID", "toggle_hint": ""}, "group": "大会员资源位", "group_order": 4, "label": "常规资源位配置", "order": 1, "desc": "大会员投放系统 - 活动 / 单元 / 创意 三步配置", "scopes": []},
    {"name": "原生商广", "mode": "ad_native", "caps": {"strategy": false, "prep": true, "positions": false, "activity": false, "task_list": false, "excel": true, "toggle": false, "flow": false}, "ui": {"deliver_label": "投放配置", "deliver_hint": "配好策略 → 生成模板 → 填好 Excel → 载入并检查 → 跑", "strategy_hint": "配在这里的字段，模板里就不用逐个单元填了", "run_kind": "fill", "params_label": "名称关键词", "params_placeholder": "一行一个关键词，命中即算。留空 = 整页所有行", "strategy_label": "策略", "strategy_placeholder": "留空 = 当前打开的策略页。跨策略：一行一个，编辑页URL / 路由ID / 业务ID", "toggle_hint": ""}, "group": "商业化广告", "group_order": 5, "label": "原生商广", "order": 1, "desc": "商广投放系统 - 一个内容一个单元，每单元最多 10 条创意", "scopes": []},
    {"name": "预定会议室", "mode": "meeting_reserve", "caps": {"strategy": false, "prep": false, "positions": false, "activity": false, "task_list": true, "excel": false, "toggle": false, "flow": false}, "ui": {"deliver_label": "投放配置", "deliver_hint": "配好策略 → 生成模板 → 填好 Excel → 载入并检查 → 跑", "strategy_hint": "配在这里的字段，模板里就不用逐个单元填了", "run_kind": "grab", "params_label": "名称关键词", "params_placeholder": "一行一个关键词，命中即算。留空 = 整页所有行", "strategy_label": "策略", "strategy_placeholder": "留空 = 当前打开的策略页。跨策略：一行一个，编辑页URL / 路由ID / 业务ID", "toggle_hint": ""}, "group": "日常办公", "group_order": 6, "label": "预定会议室", "order": 1, "desc": "哔哩哔哩行政管理平台 - 掐着开放时刻抢会议室", "scopes": []},
  ];

  // 没有后端时的假抢占任务数据：只够看清任务行的排版，真实楼栋清单来自
  // config/forms/预定会议室.yaml 的 buildings，不在这里维护第二份
  const STUB_MEETING = {
    meeting: true,
    buildings: ["国正中心/2号楼", "国正中心/1号楼", "国正中心/3号楼"],
    default_task: {
      enabled: true, repeat_weekly: false, date: "", weekday: 1,
      start: "14:00", end: "15:00", min_capacity: 6,
      building: "国正中心/2号楼", building_only: false, room: "", subject: "会议", remarks: "",
    },
    weekday_names: ["周一", "周二", "周三", "周四", "周五", "周六", "周日"],
    rule_text: "10点之前只能预定5个工作日之内的会议室，10点之后才可预定第6个工作日的会议室",
    open_time: "10:00",
    tasks: [],
    issues: [],
  };

  // 没有后端时的假 wizard 数据：只够看清资源位 chip、策略中心两栏排版对不对，
  // 真实清单来自 config/forms/*.yaml，不在这里维护第二份
  const STUB_WIZARD = {
    wizard: true,
    positions: [
      { name: "播放页催费条", scene: "OGV播放页", system: "v1", strategy_fields: ["生效平台", "展示不超过", "人群选组"] },
      { name: "会员中心弹窗", scene: "会员中心", system: "v1", strategy_fields: ["生效平台", "限频规则", "人群选组"] },
      { name: "端外PUSH", scene: "消息渠道", system: "新版", strategy_fields: ["投放流量池"] },
    ],
    strategy_fields: [
      { name: "生效平台", kind: "multi", options: ["Android", "iPhone", "PC"], required: true, positions: ["播放页催费条", "会员中心弹窗"], scheme_group: "", when: null, group: "定向投放" },
      { name: "展示不超过", kind: "text", options: [], required: false, positions: ["播放页催费条"], scheme_group: "", when: null, group: "投放设置" },
      { name: "限频规则", kind: "single", options: ["达到频次限制后不再投放", "未达到频次限制时投放被点击，不再投放"], required: true, positions: ["会员中心弹窗"], scheme_group: "", when: null, group: "投放设置" },
      { name: "投放流量池", kind: "single", options: ["日常池", "特殊最优池"], required: true, positions: ["端外PUSH"], scheme_group: "", when: null, group: "投放设置" },
      { name: "人群选组", kind: "single", options: ["不限", "指定人群"], required: true, positions: ["播放页催费条", "会员中心弹窗"], scheme_group: "audience", when: null, group: "人群" },
      { name: "我想投放", kind: "multi", options: ["未登录", "在期大会员", "过期大会员"], required: true, positions: ["播放页催费条", "会员中心弹窗"], scheme_group: "audience", when: ["人群选组", "指定人群"], group: "人群" },
      { name: "过期大会员天数", kind: "range", options: [], required: true, positions: ["播放页催费条", "会员中心弹窗"], scheme_group: "audience", when: ["我想投放", "过期大会员"], group: "人群" },
      { name: "生效内容", kind: "single", options: ["全部", "部分分区"], required: true, positions: ["播放页催费条", "会员中心弹窗"], scheme_group: "content", when: null, group: "内容限制" },
    ],
    groups: ["定向投放", "投放设置"],
    scheme_groups: [
      { key: "audience", name: "人群", exception_field: "人群方案", fields: ["人群选组", "我想投放", "过期大会员天数"] },
      { key: "content", name: "内容限制", exception_field: "内容限制方案", fields: ["生效内容"] },
    ],
  };
  const STUB_STRATEGY = {
    active: "默认策略",
    items: {
      默认策略: {
        rules: {},
        groups: {
          audience: {
            mode: "fixed", scheme: "新客", rules: [{ keywords: ["新客", "拉新"], schemes: ["新客"] }],
            fallback: [], schemes: { 新客: { 人群选组: "指定人群" }, 即期: { 人群选组: "指定人群" } },
          },
          content: {
            mode: "fixed", scheme: "常规", rules: [], fallback: [],
            schemes: { 常规: { 生效内容: "全部" } },
          },
        },
        exceptions: [], updated_at: "",
      },
    },
  };

  // 原生商广准备参数的假数据，同样只为了在普通浏览器里核对样式
  const STUB_AD = {
    ad: true,
    fields: [
      { name: "计划名称", type: "text", required: true, ph: "如【26年8月】原生内容素材-整合版" },
      { name: "已有计划ID", type: "text", ph: "留空 = 本次新建计划" },
      { name: "转化目标", type: "select", required: true, options: ["表单提交"] },
      { name: "出价", type: "number", required: true, unit: "元/转化" },
      { name: "投放时间", type: "segmented", required: true, options: ["长期投放", "设置起止时间"] },
      { name: "投放起止时间", type: "text", when: ["投放时间", "设置起止时间"] },
      { name: "指定人群", type: "text" },
      { name: "排除人群", type: "text" },
    ],
    values: { 转化目标: "表单提交", 出价: "200", 投放时间: "设置起止时间" },
    grouping: {},
  };

  // 首页/数据统计的假数据，同样只为了在普通浏览器里核对样式
  const STUB_USAGE = {
    since: "2026-07-01", people: 3, people_opened: 5, shared: false, me: "abc12345",
    opens: 12, retries: 1, dry_runs: 2,
    saving: { mode: "baseline", multiplier: 3, default_seconds: 120, per_item_seconds: {} },
    report: { on: true, pending: 2, snapshot_at: "", error: "连不上网", syncing: false },
    snapshot_at: "2026-08-21T13:00:00+08:00",
    totals: { runs: 9, items: 143, failed: 4, skipped: 6, attempted: 153,
              seconds: 5400, human: 47000, saved: 41600, ok_rate: 0.972 },
    week: { items: 21, seconds: 900, saved: 7000 },
    longest: { seconds: 2400, items: 40, form: "资源位投放", ts: "2026-08-18T10:00:00+08:00" },
    // ⚠ 至少铺三个配置类型：「用在哪儿了」那张卡要的就是"条数最长的那行和
    //   省时最长的那行不是同一行"，只有一行的话这个效果根本看不出来
    forms: [{ name: "资源位投放", runs: 5, ok: 100, failed: 2, skipped: 0, total: 102,
              seconds: 4000, human: 48000, saved: 44000, last: "2026-08-20T09:00:00+08:00" },
            { name: "DMP延期", runs: 3, ok: 38, failed: 2, skipped: 6, total: 46,
              seconds: 900, human: 9120, saved: 8220, last: "2026-08-19T15:20:00+08:00" },
            { name: "预定会议室", runs: 4, ok: 3, failed: 0, skipped: 0, total: 7,
              seconds: 500, human: 0, saved: 0, last: "2026-08-25T09:30:00+08:00" }],
    recent: [{ ts: "2026-08-20T09:00:00+08:00", form: "资源位投放", mode: "auto",
               uid: "abc12345", ok: 20, total: 20, seconds: 900 },
             { ts: "2026-08-19T15:20:00+08:00", form: "DMP延期", mode: "confirm",
               uid: "abc12345", ok: 12, total: 14, seconds: 300 }],
  };

  function callApi(name, ...args) {
    if (hasBackend() && window.pywebview.api[name]) {
      return window.pywebview.api[name](...args);
    }
    // 没有 Python 后端时的假数据，只为了能在普通浏览器里核对样式
    if (name === "list_forms") return Promise.resolve(STUB_FORMS);
    if (name === "browser_status") return Promise.resolve(false);
    if (name === "wizard_meta") return Promise.resolve(STUB_WIZARD);
    if (name === "ad_meta") return Promise.resolve(STUB_AD);
    if (name === "meeting_meta") return Promise.resolve(STUB_MEETING);
    if (name === "meeting_save") return Promise.resolve({ ok: true, tasks: args[1] || [], issues: [] });
    if (name === "pt_ledger_view") return Promise.resolve({ ok: true, strategies: [], recent: [], path: "" });
    if (name === "submit_feedback") return Promise.resolve({ ok: true });
    if (name === "flow_list") return Promise.resolve([]);
    if (name === "flow_get") return Promise.resolve({ ok: true, flow: { name: args[0], status: "draft", steps: [], data: { source: "none", columns: [] } }, issues: [], columns: [] });
    if (name === "flow_new" || name === "flow_save" || name === "flow_mark_tested" || name === "flow_delete") return Promise.resolve({ ok: true, issues: [], columns: [] });
    if (name === "flow_start_record" || name === "flow_stop_record") return Promise.resolve({ ok: true, flow: { name: args[0], status: "draft", steps: [], data: {} }, issues: [] });
    if (name === "flow_record_status") return Promise.resolve({ running: false, done: true, steps: 0 });
    if (name === "flow_submit") return Promise.resolve({ ok: true, where: "wecom", url: "" });
    if (name === "prep_save") return Promise.resolve({ ok: true, values: {}, issues: [] });
    if (name === "strategy_get") return Promise.resolve({ ok: true, path: "config/strategies/…json", doc: STUB_STRATEGY });
    // ⚠ 统计的样子货只在网址带 ?demo 时给。别的样子货最多让界面长得不对，
    //   这一份不一样 —— 它会变成首页上一串**看起来像真的**的数字。
    //   宁可首页空着，也不能让人对着编出来的「省下 12 小时」做判断。
    if (name === "usage_summary") return Promise.resolve(DEMO ? STUB_USAGE : null);
    return Promise.resolve(null);
  }

  const state = {
    forms: [],
    activeForm: null,
    view: "home",           // home = 首页（统计/导航） / form = 某个配置类型的三步流程
    version: "",            // app_info 返回的版本号，显示在侧栏底部
    usage: null,            // usage_summary 返回的聚合结果
    step: "prepare",
    logOpen: false,
    logCount: 0,
    logErrors: 0,
    runMode: "confirm",
    scopeValue: null,
    toggleDir: "on",       // 价格策略批量开关：on=开启 / off=关闭
    tgScope: "keyword",    // 选哪些行：keyword / ledger / list
    tgStrategyMode: "current",  // 策略范围：current=当前页 / list=指定策略
    dataFile: "",
    loaded: false,          // 当前配置类型是否已经成功载入过一次
    previewRows: [],        // load_and_check 返回的行摘要（不含 payload）
    reviewFilter: "all",    // all | bad | done
    flow: null,             // 当前选中的自制工作流（flow_get 的结果）
    browserConnected: false,
    running: false,

    // wizard（资源位投放）专用
    wizardMeta: null,       // wizard_meta 返回的资源位 / 策略字段定义
    positions: [],          // 本次勾选的资源位，和「生成模板」解耦，是独立的一步
    activityMode: "new",    // new = 本次新建活动 / existing = 挂到已有活动
    activityId: "",
    strategyDoc: null,      // 策略中心整份文档 {active, items}
    wizardTab: "deliver",   // 资源位投放下面的二级 Tab：deliver=投放配置 / strategy=策略中心

    // 原生商广专用
    adMeta: null,           // ad_meta 返回的准备阶段字段定义
    prepValues: {},         // 准备阶段当前填的值，存盘走 prep_save

    // 预定会议室专用
    meetingMeta: null,      // meeting_meta 返回的楼栋清单 / 默认值 / 提前天数
    meetingTasks: [],       // 抢占任务清单，存盘走 meeting_save
  };

  // ============================================================ 能力判断
  //
  // ⚠ 界面上「这个配置类型有没有 xxx」**一律看后端发来的 caps，不看 mode 名**。
  //   caps 由 src/webapp.py 的 Api._caps() 按 yaml 算出来（strategy_groups /
  //   prep_fields / positions / activity / grab / data_source），那边一个 mode 名
  //   都没有。
  //
  //   为什么定这条规矩：原来这里写的是
  //       hasStrategy() { return modeIs("wizard") || modeIs("price_panel"); }
  //   而 Python 那边早就改成看 yaml 了 —— 同一个判断两套实现，接一个新配置类型
  //   要两边都记得改，漏改还是**静默的**（卡片不显示，一句报错都没有）。
  //
  //   所以：往下写新功能时，**不要再引入 modeIs("xxx")**。需要一个新的开关，
  //   去 _caps() 里加一项、在 yaml 里声明，这里只管读。
  function formMeta() {
    return state.forms.find((f) => f.name === state.activeForm) || null;
  }
  function caps() { return (formMeta() || {}).caps || {}; }
  function uiText() { return (formMeta() || {}).ui || {}; }

  // 策略中心（配一次全批套用，可建多套方案、按单元名关键词切）
  function hasStrategy() { return !!caps().strategy; }
  // 「准备」页那张共用参数平表（原生商广的投放参数、价格面板的生效渠道）
  function hasPrepCard() { return !!caps().prep; }
  // 要不要勾「本次投哪些资源位」。只有一个资源位的配置类型没这回事
  function hasPositions() { return !!caps().positions; }
  // 本批共用一个活动：本次新建 or 挂到已有
  function hasActivity() { return !!caps().activity; }
  // 抢占任务清单那张卡（会议室）
  function hasTaskList() { return !!caps().task_list; }
  // 吃不吃 Excel 数据文件
  function needsExcel() { return caps().excel !== false; }
  // 「批量开关」类型（价格策略批量开启/关闭）：藏数据文件行，露「名称关键词」文本框
  function hasToggle() { return !!caps().toggle; }
  // 自制配置类型（录制生成的工作流）：准备页显示步骤卡
  function hasFlow() { return !!caps().flow; }
  // 跑法：grab=抢占（只找不订／开抢），fill=填表（空跑／逐条确认／全自动）
  function isGrabRun() { return uiText().run_kind === "grab"; }

  // 资源位勾选 / 活动设置记在本地，换个配置类型再切回来不用重勾。
  // ⚠ 按配置类型分开存（key 里带 activeForm）：资源位投放和价格面板配置各记各的活动设置。
  function prefsKey() { return `formbot.wizard.${state.activeForm}`; }
  function savePrefs() {
    if (!hasPositions() && !hasActivity()) return;
    try {
      localStorage.setItem(prefsKey(), JSON.stringify({
        positions: state.positions, activityMode: state.activityMode,
        activityId: state.activityId,
      }));
    } catch (e) { /* 忽略：localStorage 不可用不影响主流程 */ }
  }
  function loadPrefs() {
    let saved = null;
    try { saved = JSON.parse(localStorage.getItem(prefsKey()) || "null"); } catch (e) { saved = null; }
    state.positions = (saved && Array.isArray(saved.positions)) ? saved.positions : [];
    state.activityMode = (saved && saved.activityMode === "existing") ? "existing" : "new";
    state.activityId = (saved && saved.activityId) || "";
  }

  const STEP_ORDER = ["prepare", "review", "execute"];
  const STEP_LABEL = { prepare: "准备", review: "核对", execute: "执行" };

  const $ = (sel) => document.querySelector(sel);
  const el = (tag, cls, text) => {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  };
  function escapeHtml(s) {
    const d = document.createElement("div");
    d.textContent = String(s == null ? "" : s);
    return d.innerHTML;
  }

  // ---------------- 日志抽屉 ----------------
  function appendLog(msg, level) {
    level = level || "info";
    const console_ = $("#logConsole");
    const line = el("div", "line");
    const ts = new Date().toTimeString().slice(0, 8);
    const span = el("span", `lvl-${level}`, msg);
    line.appendChild(el("span", "ts", `${ts}  `));
    line.appendChild(span);
    console_.appendChild(line);
    console_.scrollTop = console_.scrollHeight;

    state.logCount++;
    $("#logCountPill").textContent = String(state.logCount);
    if (level === "error") {
      state.logErrors++;
      const p = $("#logErrorPill");
      p.textContent = `${state.logErrors} 报错`;
      p.classList.remove("hidden");
    }
  }

  function setLogOpen(open) {
    state.logOpen = open;
    $("#logDrawer").classList.toggle("open", open);
    $("#logToggleHint").textContent = open ? "收起 ⌃" : "展开 ⌄";
  }

  // ---------------- 主题 ----------------
  function applyTheme(dark) {
    document.documentElement.setAttribute("data-theme", dark ? "dark" : "light");
    const t = $("#themeToggle");
    if (t) t.textContent = dark ? "☀" : "☾";   // 点了切到浅色显示☀，反之☾
    try { localStorage.setItem("formbot.theme", dark ? "dark" : "light"); } catch (e) { /* 忽略 */ }
  }

  // 版本号来自 Python 端的 src/__init__.py，全项目只有那一个
  function renderVersion() {
    callApi("app_info").then((info) => {
      state.version = (info && info.version) || "";
      $("#appVersion").textContent = state.version ? `版本 ${state.version}` : "";
    });
  }

  // 更新检查不影响主流程：服务器暂不可用时只在侧栏给出「可重试」提示，
  // 绝不能让用户因为更新服务故障而不能配置业务。
  function fmtSize(info) {
    if (!info.size) return "";
    const big = info.kind !== "payload";
    const size = info.size >= 1048576
      ? `${(info.size / 1048576).toFixed(0)}MB`
      : `${Math.round(info.size / 1024)}KB`;
    return big ? `（完整安装包 ${size}，用时较久）` : `（${size}）`;
  }

  // 平时 footer 只有一行「版本 X · 检查更新」。只有真有新版本，才把那个带
  // 「更新并重启」的框展开出来；没更新时给文字链接一个短暂的「已是最新」反馈。
  function setCheckLink(text, dim) {
    const link = $("#btnCheckUpdate");
    if (!link) return;
    link.disabled = false;
    link.textContent = text;
    link.classList.toggle("dim", !!dim);
  }

  function renderUpdate(info) {
    const box = $("#updateBox");
    const label = $("#updateLabel");
    const install = $("#btnInstallUpdate");
    const link = $("#btnCheckUpdate");

    if (info && info.state === "available") {
      box.classList.remove("hidden");
      install.classList.remove("hidden");
      link.classList.add("has-update");
      setCheckLink("有新版");
    } else {
      box.classList.add("hidden");
      install.classList.add("hidden");
      link.classList.remove("has-update");
      link.classList.toggle("hidden", !!(info && info.state === "disabled"));
      if (info && info.state === "current") {
        setCheckLink("已是最新", true);
        setTimeout(() => { const l = $("#btnCheckUpdate");
          if (l && !l.classList.contains("has-update")) setCheckLink("检查更新"); }, 2500);
      } else if (info && info.state === "error") {
        setCheckLink("检查失败", true);
        setTimeout(() => { const l = $("#btnCheckUpdate");
          if (l && !l.classList.contains("has-update")) setCheckLink("检查更新"); }, 2500);
      } else {
        setCheckLink("检查更新");
      }
    }
    if (info && info.state === "available") {
      // ⚠ 侧栏那个框只报「有新版本 + 多大」，改动说明一个字都不放 —— notes 是多行的
      //   更新日志，塞进这一行会被压成一长条（1.0.11 实测很难看），也和紧接着的弹窗重复。
      label.textContent = `发现新版本 ${info.version}${fmtSize(info)}`;
      maybeAnnounceUpdate(info);
    }
  }

  // ⚠ 光靠侧栏那行小字没人会看见 —— 实测就是这样，同事一直用着老版本也不知道。
  //   所以发现新版时弹一次模态框。但**同一个版本只弹一次**：每次开程序都糊人一脸
  //   会让人条件反射去点「稍后」，反而更不会更新。记在 localStorage 里。
  const UPDATE_SEEN_KEY = "formbot.update.seen";
  function maybeAnnounceUpdate(info) {
    if (!info || !info.version) return;
    let seen = "";
    try { seen = localStorage.getItem(UPDATE_SEEN_KEY) || ""; } catch (e) {}
    if (seen === info.version) return;
    try { localStorage.setItem(UPDATE_SEEN_KEY, info.version); } catch (e) {}

    const size = fmtSize(info).replace(/^（|）$/g, "");
    // notes 是多行的更新日志。弹窗的 CSS 是 white-space: pre-line，换行有效，
    // 所以这里原样给；和下面那句下载量之间空一行，别粘成一坨。
    // ⚠ 以前这儿还有一句「更新时程序会自动关闭并重新打开，配置和数据都不会动」。
    //   去掉了：每次弹窗都念一遍同样的免责声明，只会把真正要看的改动说明挤下去。
    const lines = [
      info.notes ? String(info.notes).trim() + "\n" : "",
      size ? `这次需要下载 ${size}。` : "",
    ].filter(Boolean);

    showModal({
      title: `有新版本 ${info.version}`,
      desc: lines.join("\n"),
      buttons: [
        { label: "立即更新", primary: true, onClick: () => downloadAndInstallUpdate() },
        { label: "以后再说" },
      ],
    });
  }

  function checkForUpdate(force) {
    if (force) {
      const link = $("#btnCheckUpdate");
      if (link) { link.textContent = "检查中…"; link.disabled = true; }
      // 手动点检查 = 明确想看结果，把「这版已提醒过」的记录清掉
      try { localStorage.removeItem(UPDATE_SEEN_KEY); } catch (e) {}
    }
    return callApi("check_update", !!force).then(renderUpdate);
  }

  // ---------------- 反馈 ----------------
  function logTail(n) {
    return [...$("#logConsole").querySelectorAll(".line")]
      .slice(-n).map((l) => l.textContent.replace(/\s+/g, " ").trim()).join("\n");
  }

  const FB_COPY = {
    issue: {
      desc: "遇到的问题写下面，勾上日志能帮我们更快定位。",
      ph: "哪个配置类型、点了什么、期望怎样、实际怎样 —— 越具体越好",
    },
    idea: {
      desc: "想要的功能写下面，说清用在什么场景、现在只能怎么绕。",
      ph: "想要什么功能、解决什么场景、现在是怎么手动做的",
    },
  };

  function openFeedback(kind) {
    kind = kind === "idea" ? "idea" : "issue";
    showModal({
      title: "反馈",
      desc: FB_COPY[kind].desc,
      extraHtml:
        '<div style="padding:12px;display:flex;flex-direction:column;gap:10px">' +
        '<div class="segmented" id="fbKindSeg">' +
        `<div class="seg-item${kind === "issue" ? " active" : ""}" data-k="issue">报告问题</div>` +
        `<div class="seg-item${kind === "idea" ? " active" : ""}" data-k="idea">功能建议</div>` +
        "</div>" +
        '<textarea id="fbText" rows="5" class="field" style="resize:vertical;font-family:inherit"></textarea>' +
        '<label class="row" id="fbLogRow" style="gap:6px;color:var(--sub);font-size:12px;cursor:pointer">' +
        '<input type="checkbox" id="fbLog" checked> 附上最近的运行日志（会一起发出去）</label>' +
        "</div>",
      buttons: [
        { label: "发送", primary: true, onClick: sendFeedback },
        { label: "取消" },
      ],
    });
    const seg = $("#fbKindSeg");
    const syncKind = () => {
      const active = seg.querySelector(".seg-item.active");
      const k = active && active.dataset.k === "idea" ? "idea" : "issue";
      $("#modalDesc").textContent = FB_COPY[k].desc;
      $("#fbText").placeholder = FB_COPY[k].ph;
      $("#fbLogRow").style.display = k === "issue" ? "" : "none";
    };
    seg.querySelectorAll(".seg-item").forEach((it) => {
      it.onclick = () => {
        seg.querySelectorAll(".seg-item").forEach((n) => n.classList.remove("active"));
        it.classList.add("active");
        syncKind();
      };
    });
    syncKind();
    $("#fbText").focus();
  }

  function sendFeedback() {
    // showModal 的按钮回调里 overlay 已 hide，但 #modalExtra 的 DOM 还在，能读到值
    const active = $("#fbKindSeg .seg-item.active");
    const kind = active && active.dataset.k === "idea" ? "idea" : "issue";
    const text = ($("#fbText") && $("#fbText").value || "").trim();
    if (!text) { appendLog("反馈内容是空的，没发送", "warn"); return; }
    const withLog = kind === "issue" && $("#fbLog") && $("#fbLog").checked;
    appendLog("正在发送反馈…", "info");
    callApi("submit_feedback", { kind, text, log: withLog ? logTail(60) : "" }).then((r) => {
      if (r && r.ok) appendLog("反馈已发送，谢谢 🙏", "ok");
      else appendLog(`反馈没发出去：${r ? r.error : "无法连接后端"}`, "error");
    });
  }

  function downloadAndInstallUpdate() {
    const install = $("#btnInstallUpdate");
    const check = $("#btnCheckUpdate");
    install.disabled = true;
    check.disabled = true;
    install.textContent = "正在下载…";
    $("#updateLabel").textContent = "正在下载更新，请稍候…";
    callApi("download_update").then((download) => {
      if (!download || !download.ok) throw new Error((download && download.error) || "下载失败");
      $("#updateLabel").textContent = `已下载 ${download.version}，正在安装并重启…`;
      install.textContent = "正在安装…";
      return callApi("install_update", download.path);
    }).then((installed) => {
      if (!installed || !installed.ok) throw new Error((installed && installed.error) || "启动安装失败");
      // Python 端会在本次桥接调用返回后主动退出窗口，独立更新器再启动安装包。
    }).catch((err) => {
      $("#updateLabel").textContent = `更新失败：${err.message || err}`;
      install.disabled = false;
      install.textContent = "重新下载";
      check.disabled = false;
    });
  }

  function initTheme() {
    let dark = false;
    try {
      const saved = localStorage.getItem("formbot.theme");
      if (saved) dark = saved === "dark";
    } catch (e) { /* 忽略 */ }
    applyTheme(dark);
    $("#themeToggle").addEventListener("click", () => {
      applyTheme(document.documentElement.getAttribute("data-theme") !== "dark");
    });
  }

  // ---------------- 通用弹窗 ----------------
  function showModal({ title, desc, buttons, extraHtml }) {
    $("#modalTitle").textContent = title || "";
    $("#modalDesc").textContent = desc || "";
    const extra = $("#modalExtra");
    if (extraHtml) {
      extra.innerHTML = extraHtml;
      extra.classList.remove("hidden");
    } else {
      extra.innerHTML = "";
      extra.classList.add("hidden");
    }
    const actions = $("#modalActions");
    actions.innerHTML = "";
    (buttons || []).forEach((b) => {
      const btn = el("button", "btn" + (b.primary ? " btn-primary" : ""), b.label);
      btn.addEventListener("click", () => {
        hideModal();
        if (b.onClick) b.onClick();
      });
      actions.appendChild(btn);
    });
    $("#modalOverlay").classList.remove("hidden");
  }
  function hideModal() { $("#modalOverlay").classList.add("hidden"); }

  // ---------------- 侧栏 ----------------
  // 侧栏是两层：主 Tab（yaml 的 nav.group）+ 它下面的分 Tab（nav.label）。
  // 只有分 Tab 能点选，主 Tab 只负责收起 / 展开。
  function formLabel(name) {
    const f = state.forms.find((x) => x.name === name);
    return (f && f.label) || name || "";
  }
  function formGroup(name) {
    const f = state.forms.find((x) => x.name === name);
    return (f && f.group) || "";
  }

  // 归类 + 排序。组的次序取组内最小的 group_order，这样同组的几份 yaml
  // 只要写一样的 group_order 就行，写歪了也不会把组拆成两半。
  function groupedForms() {
    const groups = [];
    const index = {};
    state.forms.forEach((f) => {
      const gname = f.group || "其他";
      let g = index[gname];
      if (!g) {
        g = { name: gname, order: 99, items: [] };
        index[gname] = g;
        groups.push(g);
      }
      const go = (f.group_order == null) ? 99 : f.group_order;
      if (go < g.order) g.order = go;
      g.items.push(f);
    });
    const num = (v) => (v == null ? 99 : v);
    groups.forEach((g) => g.items.sort(
      (a, b) => num(a.order) - num(b.order) || String(a.label || a.name).localeCompare(String(b.label || b.name), "zh")));
    groups.sort((a, b) => a.order - b.order || a.name.localeCompare(b.name, "zh"));
    return groups;
  }

  // 收起哪些组记在本地，下次打开还是这个样子。
  // ⚠ 默认（从没手动展开过）是全部收起 —— 平时侧栏只剩「首页」+ 六个大类，
  //   要用哪个自己点开。所以这里要区分「存过一个空数组」（= 全展开）和
  //   「压根没存过」（= 还没表过态，按默认全收起），不能用 || "[]" 把两者抹平。
  function collapsedGroups() {
    try {
      const raw = localStorage.getItem("formbot.nav.collapsed");
      if (raw == null) return null;                    // 没表过态
      const saved = JSON.parse(raw);
      return Array.isArray(saved) ? saved : null;
    } catch (e) { return null; }
  }
  function collapsedNow() {
    const saved = collapsedGroups();
    return saved || groupedForms().map((g) => g.name);
  }
  function saveCollapsedGroups(names) {
    try { localStorage.setItem("formbot.nav.collapsed", JSON.stringify(names)); } catch (e) { /* 忽略 */ }
  }

  function renderSidebar() {
    const list = $("#sidebarList");
    const collapsed = collapsedNow();
    list.innerHTML = "";
    groupedForms().forEach((g) => {
      const box = el("div", "nav-group");
      box.dataset.group = g.name;
      if (collapsed.indexOf(g.name) >= 0) box.classList.add("collapsed");

      const head = el("div", "nav-group-head");
      head.appendChild(el("span", "nav-group-name", g.name));
      head.appendChild(el("span", "nav-group-dot"));   // 收起时用它提示「当前那项在这组里」
      head.appendChild(el("span", "caret", "⌄"));
      head.addEventListener("click", () => toggleGroup(g.name));
      box.appendChild(head);

      const items = el("div", "nav-group-items");
      g.items.forEach((f) => {
        const item = el("div", "sidebar-item");
        item.dataset.name = f.name;
        item.appendChild(el("span", null, f.label || f.name));
        item.appendChild(el("span", "badge", ""));
        item.addEventListener("click", () => selectForm(f.name));
        items.appendChild(item);
      });
      box.appendChild(items);
      list.appendChild(box);
    });

    // 常驻：录一个自己的助手
    const make = el("div", "sidebar-item");
    make.style.marginTop = "6px";
    make.style.color = "var(--pink)";
    make.appendChild(el("span", null, "＋ 自己录一个助手"));
    make.appendChild(el("span", "badge", ""));   // updateSidebarActive 每个 .sidebar-item 都读 .badge
    make.addEventListener("click", () => openFlowRecord(null));
    list.appendChild(make);

    updateSidebarActive();
  }

  function groupBox(name) {
    let hit = null;
    document.querySelectorAll(".nav-group").forEach((n) => {
      if (n.dataset.group === name) hit = n;
    });
    return hit;
  }

  function toggleGroup(name) {
    const box = groupBox(name);
    if (!box) return;
    box.classList.toggle("collapsed");
    const collapsed = collapsedNow().filter((n) => n !== name);
    if (box.classList.contains("collapsed")) collapsed.push(name);
    saveCollapsedGroups(collapsed);
    updateSidebarActive();
  }

  function updateSidebarActive() {
    const onForm = state.view === "form";
    document.querySelectorAll(".sidebar-item").forEach((n) => {
      if (n.id === "navHome" || n.id === "navStats") return;   // 钉住的两项单独处理
      n.classList.toggle("active", onForm && n.dataset.name === state.activeForm);
      const badge = n.querySelector(".badge");
      badge.textContent = (onForm && n.dataset.name === state.activeForm && state.loaded)
        ? String(state.previewRows.length) : "";
    });
    $("#navHome").classList.toggle("active", state.view === "home");
    $("#navStats").classList.toggle("active", state.view === "stats");
    const activeGroup = onForm ? formGroup(state.activeForm) : null;
    document.querySelectorAll(".nav-group").forEach((box) => {
      box.classList.toggle("has-active", box.dataset.group === activeGroup);
    });
  }

  // 点分 Tab 时若它所在的组是收起的（比如从别处跳过来），把组展开，
  // 不然选中了却看不见
  function expandGroupOf(name) {
    const g = formGroup(name);
    if (!g) return;
    const box = groupBox(g);
    if (!box || !box.classList.contains("collapsed")) return;
    box.classList.remove("collapsed");
    saveCollapsedGroups(collapsedNow().filter((n) => n !== g));
  }

  function currentFormMeta() {
    return state.forms.find((f) => f.name === state.activeForm) || null;
  }

  function selectForm(name, opts) {
    if (state.running) {
      appendLog("正在跑，先停止再切换配置类型", "warn");
      return;
    }
    state.view = "form";
    state.activeForm = name;
    state.dataFile = "";
    state.loaded = false;
    state.previewRows = [];
    state.reviewFilter = "all";
    $("#dataFileInput").value = "";
    $("#failedSection").classList.add("hidden");
    // 从首页跳过来时要把它所在的组打开，不然选中了却看不见；
    // 启动时那次「铺状态」不展开（expand:false）—— 人还停在首页
    if (!opts || opts.expand !== false) expandGroupOf(name);
    updateSidebarActive();
    renderTopbar();
    renderScopeRow();
    renderWizardCard();
    renderAdCard();
    renderMeetingCard();
    renderToggleCard();
    renderFlowCard();
    syncModeSegmented();
    renderReviewTable();
    goToStep("prepare");
  }

  // ---------------- 自制配置类型（mode: flow）----------------
  function renderFlowCard() {
    const on = hasFlow();
    $("#flowCard").classList.toggle("hidden", !on);
    if (!on) { state.flow = null; return; }
    $("#btnMakeTemplate").classList.add("hidden");   // flow 有自己的「生成模板」按钮
    callApi("flow_get", state.activeForm).then((r) => {
      if (!r || !r.ok) { $("#flowSteps").textContent = "读不到这个工作流"; return; }
      state.flow = r.flow;
      paintFlow(r);
    });
  }

  const FLOW_STATUS = {
    draft: ["草稿", "var(--mu)"], tested: ["本地已跑通 · 待审核", "var(--pink)"],
    submitted: ["已提交审核", "var(--ok)"], adopted: ["已采纳到正式配置", "var(--ok)"],
  };

  function paintFlow(r) {
    const f = r.flow;
    const [txt, col] = FLOW_STATUS[f.status] || FLOW_STATUS.draft;
    $("#flowStatus").textContent = txt;
    $("#flowStatus").style.color = col;
    $("#flowCols").value = (f.data && f.data.columns || []).join("、");
    $("#flowLoop").checked = (f.steps || []).some((s) => s.op === "loop_rows");
    $("#flowIssues").innerHTML = (r.issues && r.issues.length)
      ? r.issues.map((x) => "· " + escapeHtml(x)).join("<br>")
      : '<span style="color:var(--ok)">结构没问题，可以本地跑一遍了</span>';
    $("#btnFlowSubmit").disabled = !!(r.issues && r.issues.length);

    const box = $("#flowSteps");
    box.innerHTML = "";
    const rows = flattenSteps(f.steps || []);
    if (!rows.length) {
      box.innerHTML = '<div style="color:var(--mu);font-size:12px">还没录 —— 点「重新录制」去浏览器里操作一遍</div>';
      return;
    }
    rows.forEach((it) => box.appendChild(flowStepRow(it)));
  }

  // 把 loop_rows 摊平成带缩进的一列，方便展示
  function flattenSteps(steps, depth, out) {
    out = out || []; depth = depth || 0;
    steps.forEach((s, i) => {
      out.push({ s, depth, ref: s });
      if (s.op === "loop_rows") flattenSteps(s.body || [], depth + 1, out);
    });
    return out;
  }

  function pickSummary(pick) {
    if (!pick || !pick.length) return { text: "（无选择器）", warn: true };
    const c = pick[0];
    const kind = Object.keys(c)[0];
    const kinds = pick.map((p) => Object.keys(p)[0]);
    const cssOnly = kinds.every((k) => k === "css");
    const label = { text: "文字", role: "角色", label: "label", attr: "属性", css: "css" }[kind] || kind;
    return { text: `${label}：${c[kind]}`, warn: cssOnly };
  }

  function flowStepRow(it) {
    const s = it.s;
    const row = el("div", "row");
    row.style.cssText = `gap:8px;align-items:center;font-size:12px;padding:5px 8px;border:1px solid var(--bd);border-radius:8px;margin-left:${it.depth * 16}px`;
    const op = el("span", null, s.op + (s.submit ? " ·提交" : ""));
    op.style.cssText = "font-family:monospace;font-size:11px;color:var(--pink-text-on-light);background:var(--pink-light);border-radius:5px;padding:1px 6px;flex:none";
    row.appendChild(op);

    if (s.op === "loop_rows") {
      row.appendChild(el("span", null, `按 Excel 行循环（${(s.body || []).length} 步）`));
      return row;
    }
    if (["click", "fill", "select", "wait_for"].includes(s.op)) {
      const ps = pickSummary(s.pick);
      const tag = el("span", null, ps.text);
      tag.style.color = ps.warn ? "var(--bad)" : "var(--sub)";
      tag.title = ps.warn ? "只有 css 兜底，页面一变就会失效" : (s.seen || "");
      row.appendChild(tag);
    }
    if (["fill", "select"].includes(s.op)) {
      const inp = el("input", "field");
      inp.value = s.value || "";
      inp.style.cssText = "height:24px;flex:1;min-width:60px;font-size:12px";
      inp.title = "可改成 {{列名}} 绑 Excel";
      inp.addEventListener("change", () => { it.ref.value = inp.value; });
      row.appendChild(inp);
    } else if (s.op === "wait_text" || s.op === "assert") {
      row.appendChild(el("span", null, s.text || JSON.stringify(s.gone || s.url_matches || "")));
    } else if (s.op === "goto") {
      const u = el("span", null, s.url || "");
      u.style.cssText = "color:var(--mu);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1";
      row.appendChild(u);
    } else if (s.op === "confirm") {
      row.appendChild(el("span", null, s.note || "核对一眼"));
    } else if (s.op === "press") {
      row.appendChild(el("span", null, "按 " + (s.key || "Enter")));
    }

    const del = el("button", "btn btn-sm btn-ghost", "×");
    del.style.cssText = "margin-left:auto;flex:none;padding:0 8px";
    del.title = "删掉这步";
    del.addEventListener("click", () => { removeStep(state.flow.steps, it.ref); paintFlow({ flow: state.flow, issues: [] }); });
    row.appendChild(del);
    return row;
  }

  function removeStep(steps, ref) {
    for (let i = 0; i < steps.length; i++) {
      if (steps[i] === ref) { steps.splice(i, 1); return true; }
      if (steps[i].op === "loop_rows" && removeStep(steps[i].body || [], ref)) return true;
    }
    return false;
  }

  function collectFlow() {
    const f = JSON.parse(JSON.stringify(state.flow || {}));
    const cols = ($("#flowCols").value || "").split(/[,，、\s]+/).map((s) => s.trim()).filter(Boolean);
    f.data = f.data || {};
    f.data.columns = cols;
    f.data.source = (cols.length || $("#flowLoop").checked) ? "excel" : "none";
    // loop 开关：把 goto 之后的步骤包进 / 解出 loop_rows
    const want = $("#flowLoop").checked;
    const has = (f.steps || []).some((s) => s.op === "loop_rows");
    if (want && !has) {
      const lead = [], body = [];
      (f.steps || []).forEach((s) => (s.op === "goto" && !body.length ? lead : body).push(s));
      f.steps = lead.concat(body.length ? [{ op: "loop_rows", body }] : []);
    } else if (!want && has) {
      const flat = [];
      (f.steps || []).forEach((s) => s.op === "loop_rows" ? flat.push(...(s.body || [])) : flat.push(s));
      f.steps = flat;
    }
    return f;
  }

  function flowSave() {
    const f = collectFlow();
    return callApi("flow_save", state.activeForm, f).then((r) => {
      if (!r || !r.ok) { appendLog(`保存失败：${r ? r.error : "无法连接后端"}`, "error"); return r; }
      appendLog("已保存", "ok");
      state.flow = f;
      state.loaded = false; state.previewRows = []; renderReviewTable();
      paintFlow({ flow: f, issues: r.issues || [] });
      return r;
    });
  }

  function openFlowRecord(existingName) {
    const isNew = !existingName;
    showModal({
      title: isNew ? "录一个自己的助手" : "重新录制",
      desc: isNew ? "起个名字、填要操作的页面网址。点开始后去浏览器里正常操作，"
        + "完了点浮条上的「完成」。" : "重新录一遍会覆盖现在的步骤。",
      extraHtml: isNew
        ? '<div style="padding:12px;display:flex;flex-direction:column;gap:10px">'
        + '<input class="field" id="fnName" placeholder="给这个助手起个名字，比如「收银台加时」">'
        + '<input class="field" id="fnUrl" placeholder="要操作的页面网址（https://…）">'
        + "</div>"
        : "",
      buttons: [
        { label: isNew ? "开始录制" : "开始重录", primary: true, onClick: () => startRecording(isNew, existingName) },
        { label: "取消" },
      ],
    });
  }

  function startRecording(isNew, existingName) {
    const go = (name) => {
      callApi("flow_start_record", name).then((r) => {
        if (!r || !r.ok) { appendLog(`录制没起来：${r ? r.error : "无法连接后端"}`, "error"); return; }
        recordingModal(name);
      });
    };
    if (isNew) {
      const name = ($("#fnName") && $("#fnName").value || "").trim();
      const url = ($("#fnUrl") && $("#fnUrl").value || "").trim();
      if (!name) { appendLog("先给它起个名字", "warn"); return; }
      callApi("flow_new", name, url).then((r) => {
        if (!r || !r.ok) { appendLog(`建不了：${r ? r.error : "无法连接后端"}`, "error"); return; }
        go(name);
      });
    } else {
      go(existingName);
    }
  }

  function recordingModal(name) {
    let timer = null;
    const stop = () => {
      if (timer) clearInterval(timer);
      callApi("flow_stop_record", name).then((r) => {
        hideModal();
        if (!r || !r.ok) { appendLog(`收尾出错：${r ? r.error : ""}`, "error"); return; }
        appendLog(`录完了，共 ${(r.flow && r.flow.steps || []).length} 段步骤`, "ok");
        callApi("list_forms").then((fs) => {
          state.forms = fs || []; renderSidebar(); selectForm(name);
        });
      });
    };
    showModal({
      title: "录制中",
      desc: "去浏览器里操作。步骤会实时记下来 —— 完了点浮条上的「完成」，或点这里的「结束录制」。",
      extraHtml: '<div id="recStat" style="padding:12px;color:var(--sub)">已记 0 步…</div>',
      buttons: [{ label: "结束录制", primary: true, onClick: stop }],
    });
    timer = setInterval(() => {
      callApi("flow_record_status").then((st) => {
        const box = $("#recStat");
        if (box) box.textContent = `已记 ${st.steps || 0} 步…`;
        if (st && st.done) stop();
      });
    }, 1500);
  }

  function initFlowCard() {
    $("#btnFlowSave").addEventListener("click", flowSave);
    $("#btnFlowReRecord").addEventListener("click", () => openFlowRecord(state.activeForm));
    $("#btnFlowTemplate").addEventListener("click", () => {
      flowSave().then((r) => {
        if (r && r.ok) callApi("make_template", state.activeForm, null).then(handleTemplateResult);
      });
    });
    $("#btnFlowSubmit").addEventListener("click", () => {
      flowSave().then((r) => {
        if (!r || !r.ok) return;
        if (r.issues && r.issues.length) { appendLog("还有问题没解决，先看上面的提示", "warn"); return; }
        callApi("flow_mark_tested", state.activeForm).then(() => {
          appendLog("正在送审…", "info");
          callApi("flow_submit", state.activeForm).then((s) => {
            if (s && s.ok) {
              appendLog(`已送审（${s.where === "github" ? "GitHub 分支" : "企微群"}）`
                + (s.url ? "：" + s.url : ""), "ok");
              renderFlowCard();
            } else {
              appendLog(`送审没成功：${s ? s.error : "无法连接后端"}`, "error");
            }
          });
        });
      });
    });
  }

  // ---------------- 首页 ----------------
  // 两个受众：用的人要一个「值得用」的理由，维护的人要成就感和「下一步修哪儿」。
  // 数据来自 usage_summary（口径见 src/usage.py 的 summarize）。
  // ⚠ 一个字的业务内容都没有 —— 埋点里就没记，这里也变不出来。
  function showHome() { showOverview("home"); }
  function showStats() { showOverview("stats"); }

  function showOverview(view) {
    if (state.running) {
      appendLog("正在跑，先停止再看这些", "warn");
      return;
    }
    state.view = view;
    $("#wizardTabs").classList.add("hidden");
    $(".stepbar").classList.add("hidden");
    $(".footer-bar").classList.add("hidden");
    $("#filterPills").classList.add("hidden");
    document.querySelectorAll(".step-panel").forEach((n) => {
      n.classList.toggle("active", n.dataset.panel === view);
    });
    $("#topTitle").textContent = view === "home" ? "首页" : "数据统计";
    $("#topSubtitle").textContent = view === "home"
      ? "这工具能帮你干什么，以及替大家干了多少" : "全部明细";
    updateSidebarActive();
    loadUsage().then((sum) => (view === "home" ? paintHome(sum) : paintStats(sum)));
    refreshTeamThenRepaint(view);
  }

  // 团队快照（首页那个「N 人在用 / 共省了多少」）是从 GitHub 拉的，不再等发版。
  // ⚠ 顺序是「先用本地那份把界面点亮，拉到新的再重绘」——反过来的话，网络慢时
  //   人要对着空白页干等。拉失败什么都不做，继续用本地那份。
  let teamRefreshed = false;
  function refreshTeamThenRepaint(view) {
    if (teamRefreshed) return;          // 一次启动只拉一次，别每次切页都请求
    teamRefreshed = true;
    callApi("refresh_team")
      .then((r) => {
        if (!r || !r.changed) return;
        return loadUsage(true).then((sum) => {
          if (state.view !== view) return;
          view === "home" ? paintHome(sum) : paintStats(sum);
        });
      })
      .catch(() => {});
  }

  // 统计读一次就够，不用每次点都去捞：数据源（本机 jsonl / 以后的企微表格）都不是
  // 实时变的，跑完一轮会把缓存置空，「数据统计」页上还有「刷新」按钮兜底。
  function loadUsage(force) {
    if (state.usage && !force) return Promise.resolve(state.usage);
    return callApi("usage_summary").then((sum) => {
      state.usage = sum || null;
      return state.usage;
    });
  }

  function fmtDuration(sec) {
    sec = Math.max(0, Number(sec) || 0);
    if (sec < 90) return `${Math.round(sec)} 秒`;
    if (sec < 3600) return `${Math.round(sec / 60)} 分钟`;
    const h = sec / 3600;
    return `${h < 10 ? h.toFixed(1) : Math.round(h)} 小时`;
  }
  function fmtWorkdays(sec) {
    const d = (Number(sec) || 0) / 3600 / 8;
    if (d < 0.1) return "";
    return `≈ ${d < 10 ? d.toFixed(1) : Math.round(d)} 个工作日`;
  }
  function fmtWhen(ts) {
    if (!ts) return "";
    const t = new Date(ts);
    if (isNaN(t)) return String(ts).slice(0, 10);
    const mins = (Date.now() - t.getTime()) / 60000;
    if (mins < 1) return "刚刚";
    if (mins < 60) return `${Math.round(mins)} 分钟前`;
    if (mins < 60 * 24) return `${Math.round(mins / 60)} 小时前`;
    if (mins < 60 * 24 * 7) return `${Math.round(mins / 60 / 24)} 天前`;
    return ts.slice(5, 10);
  }

  // 首页 = 能干什么（主角）+ 关键数据一屏（配角，详情去「数据统计」看）
  function paintHome(sum) {
    const box = $("#homeBody");
    box.innerHTML = "";
    const t = (sum && sum.totals) || {};
    const used = new Set(((sum && sum.forms) || []).map((f) => f.name));
    const hasData = (t.runs || 0) > 0;

    if (!hasData) box.appendChild(homeWelcome());
    box.appendChild(homeCatalog(used, hasData));
    if (hasData) box.appendChild(homeHero(sum, t, true));
    box.appendChild(homeFootnote(sum));
  }

  // 数据统计页 = 首页放不下的全部明细
  function paintStats(sum) {
    const box = $("#statsBody");
    box.innerHTML = "";
    const t = (sum && sum.totals) || {};
    if (!sum || sum.error || !(t.runs || 0)) {
      const tip = el("div", "card col home-card");
      tip.style.padding = "24px 18px";
      tip.appendChild(el("b", null, "还没有统计数据"));
      tip.appendChild(el("div", "home-note",
        (sum && sum.error) || "跑一次配置，这里就有数了"));
      box.appendChild(tip);
      if (sum) box.appendChild(homeFootnote(sum));
      return;
    }
    box.appendChild(homeHero(sum, t, false));
    box.appendChild(homeLedger(sum, t));
    box.appendChild(homeForms(sum));
    box.appendChild(homeRecent(sum));
    box.appendChild(homeFootnote(sum));
  }

  function homeCard(title, hint) {
    const c = el("div", "card col home-card");
    if (title) {
      const sect = el("div", "sect");
      sect.appendChild(el("span", "bar"));
      sect.appendChild(el("b", null, title));
      if (hint) sect.appendChild(el("span", null, hint));
      c.appendChild(sect);
    }
    return c;
  }

  // ① 顶部四宫格
  function homeHero(sum, t, brief) {
    const c = homeCard();
    c.classList.add("home-hero");
    const grid = el("div", "kpi-grid");
    const rate = (t.ok_rate == null) ? "—" : `${(t.ok_rate * 100).toFixed(1)}%`;
    const many = (sum.people || 1) > 1;      // 接上汇总、真有别人在用时才显示人数
    const saved = (t.saved == null) ? t.seconds : t.saved;
    const items = [
      ["累计处理", `${t.items || 0}`, "条", (sum.week && sum.week.items)
        ? `本周 +${sum.week.items}` : "本周还没动过"],
      // 「省下工时」= 人工基准 × 条数 − 机器实跑。口径见 src/usage.py 的 saved_seconds
      ["省下工时", fmtDuration(saved), "", fmtWorkdays(saved) || "还没攒够"],
      ["一次做对", rate, "", (t.failed ? `失败 ${t.failed} 条` : "还没失败过")],
      many
        ? ["在用人数", `${sum.people}`, "人", `另有 ${(sum.people_opened || 0) - sum.people} 人只打开过`]
        : ["跑过", `${t.runs || 0}`, "次", (sum.retries ? `另有 ${sum.retries} 次返工` : "没返过工")],
    ];
    items.forEach(([label, value, unit, sub]) => {
      const k = el("div", "kpi");
      k.appendChild(el("div", "kpi-label", label));
      const v = el("div", "kpi-value", value);
      if (unit) v.appendChild(el("span", "kpi-unit", unit));
      k.appendChild(v);
      k.appendChild(el("div", "kpi-sub", sub));
      grid.appendChild(k);
    });
    c.appendChild(grid);
    const mine = sum.mine && sum.mine.totals;
    if (many && mine) {
      c.appendChild(el("div", "home-note",
        `其中你自己跑了 ${mine.items || 0} 条，省下 ${fmtDuration(mine.saved == null ? mine.seconds : mine.saved)}。`));
    }
    c.appendChild(el("div", "home-note", savingNote(sum, t)));
    if (brief) {
      // 首页只给这一屏关键数字，剩下的去「数据统计」看
      const more = el("div", "row");
      more.style.cssText = "margin-top:2px";
      const link = el("button", "btn btn-sm", "查看详情 →");
      link.addEventListener("click", showStats);
      more.appendChild(link);
      c.appendChild(more);
    }
    return c;
  }

  /** 顶部那行口径小字。⚠ 必须说清哪半截是实测、哪半截是估的 —— 这行字是这个数字
   *  可信不可信的全部区别。 */
  function savingNote(sum, t) {
    const machine = fmtDuration(t.seconds);
    const cfg = sum.saving || {};
    if (cfg.mode === "multiplier") {
      return `「省下工时」= 机器实跑 ${machine}（实测，已扣掉等你点确认的时间）`
        + ` × ${cfg.multiplier} 倍 − 机器实跑。倍数在 settings.yaml 的 usage.saving 里改。`;
    }
    return `「省下工时」= 同样这些条数人工要花的时间 ${fmtDuration(t.human)}（按每种配置一条`
      + `多少分钟估的，见 settings.yaml 的 usage.saving）− 机器实跑 ${machine}`
      + `（实测，已扣掉等你点确认的时间）。`;
  }

  // ② 两本账：条数落在哪儿（环形）+ 时间去哪儿了（对比条）
  //
  // ⚠ 这两本账的**分母不一样** —— 条数账的分母是「这次要处理多少条」，时间账的
  //   分母是「人工要花多久」。所以是两张图，不能凑成一张。
  // ⚠ 时间账画成上下两条**各自独立**的条、不叠成一条：saved 是逐次运行
  //   max(0, 人工 − 机器) 累加出来的，机器比人工还慢的那几次贡献 0，所以
  //   「机器实跑 + 省下」并不恒等于「人工要花」。叠成一条等于宣称它们相加，那是假的。
  function homeLedger(sum, t) {
    const c = homeCard("这些数字怎么来的", "左边是条数落在哪儿，右边是时间去哪儿了");
    const duo = el("div", "ledger-duo");

    // 左：成败环形。中心那个数和 hero 上的「一次做对」是同一个，
    // 这里补的是它的分母长什么样 —— 失败几条、跳过几条
    const ok = statNum(t.items), bad = statNum(t.failed), skip = statNum(t.skipped);
    const rate = (t.ok_rate == null) ? "—" : `${(t.ok_rate * 100).toFixed(1)}%`;
    const left = el("div", "ledger-cell");
    left.appendChild(donut([
      { name: "成功", value: ok, cls: "ok" },
      { name: "失败", value: bad, cls: "bad" },
      { name: "跳过", value: skip, cls: "skip" },
    ], rate, "一次做对"));
    const lg = el("div", "legend-row");
    lg.appendChild(legendItem("ok", "成功", ok));
    lg.appendChild(legendItem("bad", "失败", bad));
    lg.appendChild(legendItem("skip", "跳过", skip));
    left.appendChild(lg);
    left.appendChild(el("div", "home-note", "跳过的不算错，不进「一次做对」的分母"));
    duo.appendChild(left);

    // 右：时间账。两条同尺归一（都除以两者里大的那个），所以「机器实跑」那条
    // 有多短是看得出来的 —— 各归各的最大值就全都顶格，什么也说明不了
    const human = statNum(t.human), machine = statNum(t.seconds);
    const saved = statNum(t.saved == null ? t.seconds : t.saved);
    const scale = Math.max(human, machine, 1);
    const right = el("div", "ledger-cell ledger-time");
    right.appendChild(meterRow("人工要花", human / scale, fmtDuration(human), "估"));
    right.appendChild(meterRow("机器实跑", machine / scale, fmtDuration(machine), "实测", "blue"));
    const big = el("div", "ledger-saved");
    big.appendChild(el("b", null, fmtDuration(saved)));
    big.appendChild(el("span", null, "省下的差额"
      + (fmtWorkdays(saved) ? `　${fmtWorkdays(saved)}` : "")));
    right.appendChild(big);
    right.appendChild(el("div", "home-note",
      "上面那条是估的，下面那条是实测的 —— 差额才是省下的。口径见页脚那行小字。"));
    duo.appendChild(right);

    c.appendChild(duo);
    return c;
  }

  const SVG_NS = "http://www.w3.org/2000/svg";
  const statNum = (v) => Math.max(0, Number(v) || 0);

  /** 环形图。内联 SVG + CSS 变量上色。
   *
   *  ⚠ 为什么不引图表库：assets/webui/ 里一个外部依赖都不能有（离线内网工具，
   *    而且 assets/ 整个进那个 300KB 的代码包）。另外 canvas 画的图在深浅色
   *    切换时得手动重绘，SVG 吃 CSS 变量，主题一变自己就跟着变。
   *  ⚠ 起点在 12 点方向：整个 svg 转了 -90°（见 style.css 的 .donut）。
   *    中心文字因此不能放进 svg —— 会跟着一起歪 —— 用 HTML 浮在上面。
   *
   *  segs: [{name, value, cls}]，cls 决定描边颜色（见 style.css 的 .donut-seg.xx）。 */
  function donut(segs, bigText, smallText) {
    const R = 52, C = 2 * Math.PI * R;      // 半径 + 周长：dasharray 全靠这两个数
    const total = segs.reduce((s, x) => s + Math.max(0, x.value || 0), 0);
    const wrap = el("div", "donut-wrap");
    const svg = document.createElementNS(SVG_NS, "svg");
    svg.setAttribute("viewBox", "0 0 132 132");
    svg.setAttribute("class", "donut");
    // 底环：一条数据都没有时也得看得见个圈，不然那块地方是纯空白
    svg.appendChild(donutArc("donut-track", C, C, 0));
    let at = 0;
    segs.forEach((s) => {
      const v = Math.max(0, s.value || 0);
      if (!v || !total) return;
      const len = (v / total) * C;
      const a = donutArc(`donut-seg ${s.cls || ""}`, C, len, at);
      const tip = document.createElementNS(SVG_NS, "title");
      tip.textContent = `${s.name}：${v} 条（${((v / total) * 100).toFixed(1)}%）`;
      a.appendChild(tip);
      svg.appendChild(a);
      at += len;
    });
    wrap.appendChild(svg);
    const center = el("div", "donut-center");
    center.appendChild(el("div", "donut-big", bigText));
    if (smallText) center.appendChild(el("div", "donut-small", smallText));
    wrap.appendChild(center);
    return wrap;
  }

  /** ⚠ dasharray 一次给到终值，进场动画完全交给 CSS（见 style.css 的 donut-in）。
   *  原来是「先画 0、rAF 里再拨到真值」，那样在**窗口没显示时 rAF 根本不触发**，
   *  统计页会停在一个空心圈上。终值给死就没有这种时序依赖了。 */
  function donutArc(cls, C, len, offset) {
    const c = document.createElementNS(SVG_NS, "circle");
    c.setAttribute("cx", "66");
    c.setAttribute("cy", "66");
    c.setAttribute("r", "52");
    c.setAttribute("class", cls);
    c.setAttribute("stroke-dasharray", `${len} ${C - len}`);
    c.setAttribute("stroke-dashoffset", String(-offset));
    return c;
  }

  /** 一条带标签的横条。tone 是描边/填色的类名（留空 = 主色）。 */
  function meterRow(label, ratio, value, tag, tone) {
    const row = el("div", "meter-row");
    row.appendChild(el("div", "meter-label", label));
    const track = el("div", "meter-track");
    const fill = el("i", tone || "");
    fill.style.width = `${Math.round(Math.min(1, Math.max(0, ratio || 0)) * 100)}%`;
    track.appendChild(fill);
    row.appendChild(track);
    row.appendChild(el("div", "meter-value", value));
    row.appendChild(el("div", "meter-tag", tag || ""));
    return row;
  }

  function legendItem(cls, name, n) {
    const s = el("span", "legend");
    s.appendChild(el("i", cls));
    s.appendChild(el("span", null, (n === "" || n == null) ? name : `${name} ${n}`));
    return s;
  }

  function barSeg(cls, ratio) {
    const i = el("i", cls);
    const pct = Math.min(100, Math.max(0, (ratio || 0) * 100));
    // 有值就至少给 1.5%，不然「失败 1 条」那一段细到看不见，等于没画
    i.style.width = pct ? `${Math.max(1.5, pct)}%` : "0";
    return i;
  }

  // ③ 用在哪儿了：一行两条 —— 上条是条数（成功/失败分段），下条是省下的时间。
  //    两条各按自己那一列的最大值归一，所以最长的条数条和最长的时间条常常**不是
  //    同一行** —— 这张卡要说的就是这件事，副标题那句话原来没有图能对着看。
  function homeForms(sum) {
    const c = homeCard("用在哪儿了", "上面一条是条数，下面一条是省下的时间");
    const rows = (sum.forms || []).slice();
    // ⚠ 判据是「这个配置类型的跑法是不是抢占」，不是名字叫不叫预定会议室。
    //   跑法来自后端 ui.run_kind（见 webapp.Api._ui_text），不在这里列名单。
    const grabNames = state.forms
      .filter((f) => f.ui && f.ui.run_kind === "grab").map((f) => f.name);
    const savedOf = (r) => statNum(r.saved == null ? r.seconds : r.saved);
    const maxItems = Math.max(1, ...rows.map((r) => statNum(r.ok) + statNum(r.failed)));
    const maxSaved = Math.max(1, ...rows.map(savedOf));
    // ⚠ 分母只算非抢占型：抢占型那行显示的是「抢中 3/7」而不是占比，
    //   把它的条数算进分母的话，屏幕上那几个百分比加起来永远差几个点
    const allItems = rows.reduce(
      (s, r) => s + (grabNames.includes(r.name) ? 0 : statNum(r.ok)), 0);

    const lg = el("div", "legend-row");
    lg.appendChild(legendItem("ok", "成功", ""));
    lg.appendChild(legendItem("bad", "失败", ""));
    lg.appendChild(legendItem("time", "省下的时间", ""));
    c.appendChild(lg);

    rows.forEach((r) => {
      // 抢占型按「抢中率」讲才有意义，按条数/耗时讲等于没有价值
      const isGrab = grabNames.includes(r.name);
      const ok = statNum(r.ok), bad = statNum(r.failed), total = statNum(r.total);
      const row = el("div", "bar-row");
      row.appendChild(el("div", "bar-name", formLabel(r.name)));

      const tracks = el("div", "bar-tracks");
      const top = el("div", "bar-track");
      // 抢占型的分母是「一共抢了几次」，条形读作抢中率；其余按全场最大值归一
      const denom = isGrab ? Math.max(1, total) : maxItems;
      top.appendChild(barSeg("ok", ok / denom));
      top.appendChild(barSeg("bad", (isGrab ? Math.max(0, total - ok) : bad) / denom));
      tracks.appendChild(top);
      const bottom = el("div", "bar-track thin");
      bottom.appendChild(barSeg("time", isGrab ? 0 : savedOf(r) / maxSaved));
      tracks.appendChild(bottom);
      row.appendChild(tracks);

      const numBox = el("div", "bar-num");
      numBox.appendChild(el("b", null, isGrab ? `抢中 ${ok}/${total}` : `${ok} 条`));
      if (!isGrab && allItems) {
        numBox.appendChild(el("span", "bar-pct", `占 ${Math.round((ok / allItems) * 100)}%`));
      }
      row.appendChild(numBox);

      row.appendChild(el("div", "bar-side", isGrab
        ? `不按时长算价值 · ${fmtWhen(r.last)}`
        : `省下 ${fmtDuration(savedOf(r))} · 机器实跑 ${fmtDuration(r.seconds)}`));
      row.title = `跑了 ${r.runs} 次，最近一次 ${fmtWhen(r.last)}`
        + (r.failed ? `，失败 ${r.failed} 条` : "");
      row.addEventListener("click", () => selectForm(r.name));
      c.appendChild(row);
    });
    return c;
  }

  // ④ 最近几次 + 最长的一次
  function homeRecent(sum) {
    const c = homeCard("最近跑的", "中间那条是这一次的完成度");
    (sum.recent || []).forEach((r) => {
      const ok = statNum(r.ok), total = statNum(r.total);
      const okAll = total > 0 && ok >= total;
      const line = el("div", "feed-row");
      line.appendChild(el("span", "feed-when", fmtWhen(r.ts)));
      line.appendChild(el("span", "feed-form", formLabel(r.form)));
      const mini = el("div", "feed-bar");
      const fill = el("i", okAll ? "" : "bad");
      fill.style.width = total ? `${Math.max(3, Math.round((ok / total) * 100))}%` : "0";
      mini.appendChild(fill);
      mini.title = total ? `${ok}/${total} 条` : "";
      line.appendChild(mini);
      line.appendChild(el("span", okAll ? "feed-stat" : "feed-stat bad",
        okAll ? `${ok} 条全成` : `${ok}/${total} 条`));
      line.appendChild(el("span", "feed-cost", fmtDuration(r.seconds)));
      c.appendChild(line);
    });
    const L = sum.longest;
    if (L && L.seconds > 60) {
      c.appendChild(el("div", "home-note",
        `最长的一次：${formLabel(L.form)} 连着跑了 ${fmtDuration(L.seconds)}，一口气 ${L.items} 条。`));
    }
    return c;
  }

  // 没数据时的开场白
  function homeWelcome() {
    const c = homeCard();
    c.classList.add("home-hero");
    c.appendChild(el("div", "home-welcome-title", "还没用它跑过东西"));
    c.appendChild(el("div", "home-welcome-sub",
      "它能替你干下面这些活 —— 挑一个，按「准备 → 核对 → 执行」走一遍就行。"
      + "跑完这里会记下替你干了多少、花了多久。"));
    return c;
  }

  // 功能导航 / 覆盖度：没用过的那些才是这一块的重点
  function homeCatalog(used, hasData) {
    const groups = groupedForms();
    const total = state.forms.length;
    const c = homeCard(hasData ? "还能干什么" : "能干什么",
      hasData ? `${total} 个配置类型，你用过 ${used.size} 个` : "");
    const wrap = el("div", "catalog");
    groups.forEach((g) => {
      g.items.forEach((f) => {
        const it = el("div", "catalog-item" + (used.has(f.name) ? " used" : ""));
        const head = el("div", "catalog-head");
        head.appendChild(el("span", "catalog-group", g.name));
        head.appendChild(el("span", "catalog-name", f.label || f.name));
        if (used.has(f.name)) head.appendChild(el("span", "catalog-tag", "用过"));
        it.appendChild(head);
        // description 前半截是系统名（「大会员 DMP 人群管理 - …」），和上面的主 Tab 重复，
        // 砍掉只留真正说事的后半句；完整原文留在 tooltip 里
        const parts = String(f.desc || "").split(" - ");
        it.appendChild(el("div", "catalog-desc",
          parts.length > 1 ? parts.slice(1).join(" - ") : parts[0]));
        it.title = f.desc || "";
        it.addEventListener("click", () => selectForm(f.name));
        wrap.appendChild(it);
      });
    });
    c.appendChild(wrap);
    return c;
  }

  function homeFootnote(sum) {
    sum = sum || {};        // 没后端时 usage_summary 是 null，别让页脚把整个首页带崩
    const wrap = el("div", "col");
    wrap.style.cssText = "gap:6px;padding:2px 4px 6px";
    const line = el("div", "row");
    line.style.cssText = "gap:10px;align-items:center";
    const n = el("div", "home-foot");
    n.style.padding = "0";
    const bits = [];
    if (sum.since) bits.push(`统计自 ${sum.since}`);
    const rep = sum.report || {};
    // ⚠ 全团队那份是随包分发的快照，不是实时的 —— 这句话必须说出来，
    //   不然人会拿一个上周的数字当今天的用
    if (sum.snapshot_at) {
      bits.push(`全团队数字截至 ${String(sum.snapshot_at).slice(0, 10)}（随版本更新）`);
    } else if (rep.on) {
      bits.push("全团队数字要等下个版本带过来");
    } else {
      bits.push("只统计这台机器");
    }
    bits.push("只记条数和耗时，不记任何业务内容");
    if (state.version) bits.push(`版本 ${state.version}`);
    n.textContent = bits.join("　·　");
    line.appendChild(n);
    wrap.appendChild(line);

    // 欠着没上报的，必须说出来 —— 这类失败原来是完全静默的，人根本不知道数据没上去
    if (rep.pending) {
      const warn = el("div", "home-foot");
      warn.style.cssText = "padding:0;color:var(--bad)";
      warn.textContent = `还有 ${rep.pending} 周的使用统计没回传成功`
        + (rep.error ? `（${rep.error}）` : "")
        + "。数据没丢，都在本机记着，下次开程序会自动补 —— 一直是这句话就找开发看看。";
      wrap.appendChild(warn);
    }
    return wrap;
  }

  // ---------------- 准备页：wizard 卡片（资源位 / 活动 / 策略）----------------
  function renderWizardCard() {
    const card = $("#wizardCard");
    if (!hasStrategy()) {
      card.classList.add("hidden");
      $("#wizardTabs").classList.add("hidden");
      $(".stepbar").classList.remove("hidden");
      $(".footer-bar").classList.remove("hidden");
      state.wizardMeta = null;
      state.positions = [];
      strategyUI.draft = null;
      return;
    }
    card.classList.remove("hidden");
    $("#wizardTabs").classList.remove("hidden");
    // 资源位选择是资源位投放独有的（价格面板配置只有一个资源位）。
    // 活动那一行两边都有：都是「本批共用一个活动」，要么本次新建、要么挂到已有。
    const multi = hasPositions();
    $("#wizardPosRow").classList.toggle("hidden", !multi);
    $("#wizardActivityRow").classList.toggle("hidden", !hasActivity());
    $("#strategyScopeWrap").classList.toggle("hidden", !multi);
    $("#wizardDeliverTab").textContent = uiText().deliver_label || "投放配置";
    loadPrefs();
    renderActivityRow();

    state.wizardTab = "deliver";
    strategyUI.draft = null;
    setWizardTab("deliver");

    callApi("wizard_meta", state.activeForm).then((meta) => {
      // meta 到位之后策略中心才渲染得出来；没配过策略的直接把人带到策略中心那一页
      // （规则没配就生成模板，模板是对的但跑起来会卡在「策略中心没配」）
      if (!meta || !meta.wizard) return;
      state.wizardMeta = meta;
      const known = meta.positions.map((p) => p.name);
      state.positions = state.positions.filter((p) => known.includes(p));
      renderPosChips();
    });
    refreshStrategy();
  }

  // ---------------- 准备页：原生商广卡片（本批共用的投放参数）----------------
  // 字段清单来自 yaml 的 prep_fields，这里只按 type 决定长什么样，
  // 所以往 yaml 里加一项（比如再来个「投放时段」）不用改这段代码。
  function renderAdCard() {
    const card = $("#adCard");
    if (!hasPrepCard()) {
      card.classList.add("hidden");
      state.adMeta = null;
      state.prepValues = {};
      return;
    }
    card.classList.remove("hidden");
    $("#adPrepFields").innerHTML = "";
    $("#adPrepHint").textContent = "读取中…";

    callApi("ad_meta", state.activeForm).then((meta) => {
      if (!meta || !meta.ad) return;
      state.adMeta = meta;
      state.prepValues = Object.assign({}, meta.values || {});
      renderPrepFields();
    });

    $("#btnSavePrep").onclick = savePrep;
  }

  function renderPrepFields() {
    const box = $("#adPrepFields");
    box.innerHTML = "";
    const fields = (state.adMeta && state.adMeta.fields) || [];
    let lastGroup = null;
    fields.forEach((f) => {
      // when: [字段名, 值] 或 [字段名, [值1, 值2]] —— 依赖字段是这些值之一时才出现。
      // 用列表是因为「价格面板pid」在搭售类型 = 买赠 和 买赠+0元购 时都要填。
      if (!prepShown(f)) return;

      // 同一个 group 的字段归到一个小标题下（26 个 SKU 各自一组，不分组根本没法看）
      if (f.group && f.group !== lastGroup) {
        lastGroup = f.group;
        const h = el("div", "sect");
        h.style.cssText = "margin-top:6px";
        h.appendChild(el("span", "bar"));
        h.appendChild(el("b", null, f.group));
        box.appendChild(h);
      }

      const row = el("div", "row");
      row.style.cssText = "gap:10px;align-items:flex-start";
      const label = el("span", "form-label", f.name + (f.required ? " *" : ""));
      row.appendChild(label);

      const right = el("div", "col");
      right.style.cssText = "flex:1;gap:4px;min-width:0";
      right.appendChild(prepControl(f));
      if (f.note) {
        const note = el("div", null, f.note);
        note.style.cssText = "color:var(--mu);font-size:11px";
        right.appendChild(note);
      }
      row.appendChild(right);
      box.appendChild(row);
    });
    renderPrepHint();
  }

  // when 的求值：[字段名, 值] 和 [字段名, [值1, 值2]] 两种写法都认。
  // ⚠ 必须和 Python 端 ad_prep.shown() 保持一致，否则会出现
  //   「界面上没有这一项、却一直提示没填」，人完全没法处理。
  function prepShown(f) {
    if (!f.when) return true;
    const cur = String(state.prepValues[f.when[0]] || "");
    const want = f.when[1];
    return Array.isArray(want) ? want.map(String).includes(cur) : cur === String(want);
  }

  function prepControl(f) {
    const cur = String(state.prepValues[f.name] == null ? "" : state.prepValues[f.name]);
    // ⚠ 长什么样由 Python 端算好（webapp._prep_kind），不要在这儿看 type：
    //   原生商广的 type 写的就是长相，价格面板配置的 type 是「填写方式」
    //   （pp_radio / pp_checkbox…，给 pp_filler 用的），只认 type 会全渲染成文本框。
    const kind = f.kind || f.type;

    // 文件路径：给个「浏览」按钮，省得人去粘路径
    if (kind === "file") {
      const wrap = el("div", "row");
      wrap.style.cssText = "gap:6px;align-items:center;flex:1;min-width:0";
      const inp = el("input", "field");
      inp.value = cur;
      inp.placeholder = f.ph || "选一个 Excel，或直接粘路径";
      inp.addEventListener("input", () => {
        state.prepValues[f.name] = inp.value;
        renderPrepHint();
      });
      const btn = el("button", "btn", "浏览…");
      btn.addEventListener("click", () => {
        callApi("pick_file").then((path) => {
          if (!path) return;
          state.prepValues[f.name] = path;
          renderPrepFields();
        });
      });
      wrap.appendChild(inp);
      wrap.appendChild(btn);
      return wrap;
    }

    if (kind === "segmented" || (kind === "select" && (f.options || []).length <= 3)) {
      const seg = el("div", "segmented");
      (f.options || []).forEach((opt) => {
        const it = el("div", "seg-item" + (opt === cur ? " active" : ""), opt);
        it.addEventListener("click", () => {
          state.prepValues[f.name] = opt;
          renderPrepFields();      // 可能有 when 依赖它，整块重画
        });
        seg.appendChild(it);
      });
      return seg;
    }

    if (kind === "select") {
      const sel = el("select", "field");
      sel.style.cssText = "width:220px;flex:none";
      (f.options || []).forEach((opt) => {
        const o = el("option", null, opt);
        o.value = opt;
        if (opt === cur) o.selected = true;
        sel.appendChild(o);
      });
      sel.addEventListener("change", () => {
        state.prepValues[f.name] = sel.value;
        renderPrepFields();
      });
      return sel;
    }

    const wrap = el("div", "row");
    wrap.style.cssText = "gap:6px;align-items:center";
    const inp = el("input", "field");
    inp.value = cur;
    inp.placeholder = f.ph || "";
    if (kind === "number") inp.style.cssText = "width:120px;flex:none";
    inp.addEventListener("input", () => {
      state.prepValues[f.name] = inp.value;
      renderPrepHint();
    });
    wrap.appendChild(inp);
    if (f.unit) {
      const u = el("span", null, f.unit);
      u.style.cssText = "color:var(--sub);font-size:12px";
      wrap.appendChild(u);
    }
    return wrap;
  }

  function renderPrepHint() {
    const fields = (state.adMeta && state.adMeta.fields) || [];
    const missing = fields
      .filter((f) => f.required && !String(state.prepValues[f.name] || "").trim())
      .filter(prepShown)
      .map((f) => f.name);
    $("#adPrepHint").textContent = missing.length
      ? "还没填：" + missing.join("、")
      : "改完记得点保存；生成模板和载入检查都读保存后的值";
  }

  // ⚠ 必须显式保存：生成模板和「载入并检查」都是 Python 端重新读盘上的 json，
  //   不保存就会拿到上一次的值 —— renderPrepHint 里那句提示说的就是这件事。
  function savePrep() {
    if (!hasPrepCard()) return;
    callApi("prep_save", state.activeForm, state.prepValues).then((r) => {
      if (!r) return;
      if (!r.ok) {
        appendLog("准备参数保存失败：" + (r.error || ""), "error");
        return;
      }
      state.prepValues = Object.assign({}, r.values || state.prepValues);
      renderPrepFields();
      const issues = r.issues || [];
      appendLog(issues.length ? "准备参数已保存，但还有问题：" + issues.join("；") : "准备参数已保存",
                issues.length ? "warn" : "ok");
      $("#adPrepHint").textContent = issues.length ? issues.join("；") : "已保存";
    });
  }

  // ---------------- 准备页：预定会议室卡片（抢占任务清单）----------------
  // ⚠ 这个 mode 不吃 Excel。每条任务的日期/时段/人数都不一样，还要支持「每周循环」，
  //   在界面上加行比来回导 Excel 顺手。清单存 config/prep/预定会议室.json，
  //   「载入并检查」时 Python 端重新读盘 —— 所以改完必须点保存，和原生商广那张卡一个规矩。
  function renderMeetingCard() {
    const card = $("#meetingCard");
    // 「数据文件」那一行跟着 caps.excel 走（yaml 里 data_source: none 就藏掉），
    // 不跟着「是不是会议室」走 —— 以后再来个不吃 Excel 的类型不用改这儿
    $("#dataFileRow").classList.toggle("hidden", !needsExcel());
    if (!hasTaskList()) {
      card.classList.add("hidden");
      $("#dataSourceTitle").textContent = "配置来源";
      $("#dataSourceHint").textContent = "选择数据文件，勾选延期范围（如果有）";
      state.meetingMeta = null;
      state.meetingTasks = [];
      return;
    }
    card.classList.remove("hidden");
    $("#dataSourceTitle").textContent = "开抢";
    $("#dataSourceHint").textContent = "任务在上面填，这里点「载入并检查」核对开抢时刻";

    $("#meetingTasks").innerHTML = "";
    $("#meetingHint").textContent = "读取中…";

    callApi("meeting_meta", state.activeForm).then((meta) => {
      if (!meta || !meta.meeting) return;
      state.meetingMeta = meta;
      state.meetingTasks = (meta.tasks || []).map((t) => Object.assign({}, t));
      $("#meetingWindowHint").textContent =
        `${meta.rule_text || ""}　→　机器人会掐着开放那天的 ${meta.open_time || "10:00"} 抢`;
      renderMeetingTasks();
    });

    $("#btnAddMeetingTask").onclick = addMeetingTask;
    $("#btnSaveMeeting").onclick = saveMeetingTasks;
  }

  // 「价格策略批量开关」专用：方向 + 名称关键词 + 策略 三个控件。
  // ⚠ 判据是 caps.toggle（yaml 里 toggle: true），不看 mode 名。
  //   数据文件那一行由 renderMeetingCard 里那句 needsExcel() 统一藏掉（这类 data_source: none）。
  //   结构对齐最初的设计稿：方向 / 选哪些行（keyword·ledger·list）/ 策略范围。
  function tgInvalidate() {
    state.loaded = false;
    state.previewRows = [];
    renderReviewTable();
    updateNextButtonState();
  }

  function tgWireSeg(sel, key, active, onPick) {
    const seg = $(sel);
    seg.querySelectorAll(".seg-item").forEach((it) => {
      it.classList.toggle("active", it.dataset[key] === active);
      it.onclick = () => {
        seg.querySelectorAll(".seg-item").forEach((n) => n.classList.remove("active"));
        it.classList.add("active");
        onPick(it.dataset[key]);
      };
    });
  }

  function renderToggleCard() {
    const on = hasToggle();
    $("#toggleParamRow").classList.toggle("hidden", !on);
    $("#btnMakeTemplate").classList.toggle("hidden", on);
    if (on) $("#scopeRow").classList.add("hidden");   // 这类不用共用的「延期范围」那一行
    if (!on) return;

    $("#dataSourceTitle").textContent = "批量开关";
    $("#dataSourceHint").textContent = "把「价格配置」表里已配好的行，批量开 / 关";
    $("#tgHint").textContent = uiText().toggle_hint || "";

    tgWireSeg("#tgDirSeg", "dir", state.toggleDir, (v) => {
      state.toggleDir = v; tgRefreshDirHint(); tgInvalidate();
    });
    tgRefreshDirHint();

    tgWireSeg("#tgScopeSeg", "scope", state.tgScope, (v) => {
      state.tgScope = v; state.scopeValue = v; tgSyncBody(); tgInvalidate();
    });
    tgWireSeg("#tgStrategyModeSeg", "sm", state.tgStrategyMode, (v) => {
      state.tgStrategyMode = v; tgSyncBody(); tgInvalidate();
    });

    state.scopeValue = state.tgScope;   // 后端按它走（keyword / ledger / list）
    ["#tgKeywordInput", "#tgListInput", "#tgStrategyInput", "#tgLedgerFrom", "#tgLedgerTo"]
      .forEach((s) => { $(s).oninput = tgInvalidate; });
    $("#tgLedgerStrategy").onchange = tgInvalidate;
    tgSyncBody();
  }

  function tgRefreshDirHint() {
    $("#tgDirHint").textContent = state.toggleDir === "on"
      ? "把还没开的行开起来" : "把已开启的行关掉";
  }

  function tgSyncBody() {
    const sc = state.tgScope;
    $("#tgKeyword").classList.toggle("hidden", sc !== "keyword");
    $("#tgLedger").classList.toggle("hidden", sc !== "ledger");
    $("#tgList").classList.toggle("hidden", sc !== "list");
    // ledger 用它自己的「策略」下拉；keyword / list 才用上面的「策略范围」
    $("#tgStrategyScopeRow").classList.toggle("hidden", sc === "ledger");
    $("#tgStrategyInput").classList.toggle("hidden", state.tgStrategyMode !== "list");
    if (sc === "ledger") tgLoadLedger();
  }

  function tgLoadLedger() {
    const box = $("#tgLedgerRecords");
    const sel = $("#tgLedgerStrategy");
    box.innerHTML = '<div style="padding:8px;color:var(--mu)">读取中…</div>';
    callApi("pt_ledger_view", state.activeForm).then((r) => {
      if (!r || !r.ok) {
        box.innerHTML = '<div style="padding:8px;color:var(--mu)">读不到台账</div>';
        return;
      }
      const prev = sel.value;
      sel.innerHTML = '<option value="">全部策略</option>' +
        (r.strategies || []).map((s) =>
          `<option value="${escapeHtml(s.id)}">${escapeHtml(s.name || ("策略" + s.id))}（${s.batches}批）</option>`
        ).join("");
      sel.value = prev;
      if (!(r.recent || []).length) {
        box.innerHTML =
          '<div style="padding:10px;color:var(--mu)">还没有记录 —— 用「价格策略配置」跑过一轮后，' +
          '这里会列出它配了哪些人群、在哪条策略下</div>';
        return;
      }
      box.innerHTML = r.recent.map((b) =>
        `<div style="padding:6px 10px;border-bottom:1px solid var(--bd)">` +
        `<span style="color:var(--sub)">${escapeHtml(b.at)}</span> · ` +
        `<b>${escapeHtml(b.strategy)}</b> · ${b.count} 条` +
        `<div style="color:var(--mu);margin-top:2px">${(b.names || []).map(escapeHtml).join("、")}` +
        `${b.count > (b.names || []).length ? " …" : ""}</div></div>`
      ).join("");
    });
  }

  /** 「运行模式」三选一按 mode 调整。
   *
   * ⚠ 抢会议室没有「逐条确认」这一档：窗口开的那一瞬间没有等人点确认的余地
   *   （执行器里也是直接忽略的）。留着这个按钮只会让人以为会弹窗核对，
   *   所以这里直接把它藏掉，并且把「空跑」的文案改成这个 mode 下的实际含义。
   */
  function syncModeSegmented() {
    const seg = $("#modeSegmented");
    const item = (m) => seg.querySelector(`.seg-item[data-mode="${m}"]`);
    const grab = isGrabRun();
    item("dry").textContent = grab ? "空跑（只找不订）" : "空跑（只填不提交）";
    item("auto").textContent = grab ? "开抢" : "全自动";
    item("confirm").classList.toggle("hidden", grab);
    if (grab && state.runMode === "confirm") {
      item("confirm").classList.remove("active");
      item("auto").classList.add("active");
      state.runMode = "auto";
    }
  }

  function addMeetingTask() {
    const base = (state.meetingMeta && state.meetingMeta.default_task) || {};
    const last = state.meetingTasks[state.meetingTasks.length - 1];
    // 从上一条拷贝：连着排几场会时，通常只有日期不一样
    state.meetingTasks.push(Object.assign({}, base, last ? Object.assign({}, last, { date: "" }) : {}));
    renderMeetingTasks();
  }

  function renderMeetingTasks() {
    const box = $("#meetingTasks");
    box.innerHTML = "";
    if (!state.meetingTasks.length) {
      const empty = el("div", null, "还没有任务。点「+ 添加一条」，填上日期、时间段和人数。");
      empty.style.cssText = "color:var(--mu);font-size:12px;padding:8px 0";
      box.appendChild(empty);
    }
    state.meetingTasks.forEach((task, i) => box.appendChild(meetingTaskRow(task, i)));
    renderMeetingHint();
  }

  function meetingTaskRow(task, i) {
    const wrap = el("div", "mt-task col");
    wrap.style.cssText =
      "gap:8px;padding:10px 12px;border:1px solid var(--bd);border-radius:8px" +
      (task.enabled ? "" : ";opacity:.5");

    const mkInput = (val, ph, width, type) => {
      const inp = el("input", "field");
      if (type) inp.type = type;
      inp.value = val == null ? "" : val;
      inp.placeholder = ph || "";
      inp.style.cssText = width ? `width:${width};flex:none` : "";
      return inp;
    };
    const lab = (text) => {
      const s = el("span", null, text);
      s.style.cssText = "color:var(--sub);font-size:12px;flex:none";
      return s;
    };
    // 文本类改动只更新数据和提示，不重画 —— 重画会把正在输入的框换成新节点，光标就丢了
    const bindText = (inp, key, cast) => {
      inp.addEventListener("input", () => {
        task[key] = cast ? cast(inp.value) : inp.value;
        renderMeetingHint();
      });
    };

    // ---- 第一行：启用 / 日期方式 / 日期 / 时段 / 删除 ----
    const r1 = el("div", "row");
    r1.style.cssText = "gap:10px;flex-wrap:wrap;align-items:center";

    const on = el("input");
    on.type = "checkbox";
    on.checked = !!task.enabled;
    on.style.cssText = "accent-color:var(--pink)";
    on.addEventListener("change", () => { task.enabled = on.checked; renderMeetingTasks(); });
    const onWrap = el("label", "row");
    onWrap.style.cssText = "gap:5px;color:var(--sub);font-size:12px;cursor:pointer;flex:none";
    onWrap.appendChild(on);
    onWrap.appendChild(el("span", null, "启用"));
    r1.appendChild(onWrap);

    const seg = el("div", "segmented");
    [["指定日期", false], ["每周循环", true]].forEach(([text, v]) => {
      const it = el("div", "seg-item" + (!!task.repeat_weekly === v ? " active" : ""), text);
      it.addEventListener("click", () => { task.repeat_weekly = v; renderMeetingTasks(); });
      seg.appendChild(it);
    });
    r1.appendChild(seg);

    if (task.repeat_weekly) {
      const names = (state.meetingMeta && state.meetingMeta.weekday_names) ||
        ["周一", "周二", "周三", "周四", "周五", "周六", "周日"];
      const sel = el("select", "field");
      sel.style.cssText = "width:96px;flex:none";
      names.forEach((n, k) => {
        const o = el("option", null, "每" + n);
        o.value = String(k + 1);
        if (Number(task.weekday) === k + 1) o.selected = true;
        sel.appendChild(o);
      });
      sel.addEventListener("change", () => { task.weekday = Number(sel.value); renderMeetingHint(); });
      r1.appendChild(sel);
    } else {
      const d = mkInput(task.date, "", "150px", "date");
      bindText(d, "date");
      r1.appendChild(d);
    }

    const st = mkInput(task.start, "14:00", "110px", "time");
    st.step = 1800;
    bindText(st, "start");
    const en = mkInput(task.end, "15:00", "110px", "time");
    en.step = 1800;
    bindText(en, "end");
    r1.appendChild(st);
    r1.appendChild(lab("~"));
    r1.appendChild(en);

    const del = el("button", "btn", "删除");
    del.style.cssText = "margin-left:auto;flex:none";
    del.addEventListener("click", () => {
      state.meetingTasks.splice(i, 1);
      renderMeetingTasks();
    });
    r1.appendChild(del);
    wrap.appendChild(r1);

    // ---- 第二行：人数 / 楼栋 / 指定会议室 / 主题 ----
    const r2 = el("div", "row");
    r2.style.cssText = "gap:10px;flex-wrap:wrap;align-items:center";

    r2.appendChild(lab("可容纳"));
    const cap = mkInput(task.min_capacity, "6", "70px", "number");
    cap.min = 1;
    bindText(cap, "min_capacity", (v) => Number(v) || 1);
    r2.appendChild(cap);
    r2.appendChild(lab("人及以上"));

    const bsel = el("select", "field");
    bsel.style.cssText = "width:190px;flex:none";
    const blank = el("option", null, "不限楼栋");
    blank.value = "";
    bsel.appendChild(blank);
    ((state.meetingMeta && state.meetingMeta.buildings) || []).forEach((b) => {
      const o = el("option", null, b);
      o.value = b;
      if (b === task.building) o.selected = true;
      bsel.appendChild(o);
    });
    bsel.addEventListener("change", () => { task.building = bsel.value; renderMeetingTasks(); });
    r2.appendChild(bsel);

    // 「刚需」= 只在这栋楼抢；不勾就是这栋楼优先、抢不到退到其它楼
    const only = el("input");
    only.type = "checkbox";
    only.checked = !!task.building_only;
    only.disabled = !task.building;
    only.style.cssText = "accent-color:var(--pink)";
    only.addEventListener("change", () => { task.building_only = only.checked; renderMeetingHint(); });
    const onlyWrap = el("label", "row");
    onlyWrap.style.cssText = "gap:5px;color:var(--sub);font-size:12px;cursor:pointer;flex:none" +
      (task.building ? "" : ";opacity:.45;cursor:default");
    onlyWrap.appendChild(only);
    onlyWrap.appendChild(el("span", null, "只要这栋"));
    onlyWrap.title = task.building
      ? "勾上=刚需，只在这栋楼抢；不勾=这栋楼优先，抢不到退到其它楼"
      : "先选一个楼栋";
    r2.appendChild(onlyWrap);

    const room = mkInput(task.room, "指定会议室（可空）", "180px");
    bindText(room, "room");
    room.title = "填了就只盯这一间，人数和楼栋条件不再起作用";
    r2.appendChild(room);
    wrap.appendChild(r2);

    // ---- 第三行：主题 / 备注 ----
    // ⚠ 主题必须带标签。挤在第二行末尾时它只是个没头没脑的输入框，
    //   而且填了默认值「会议」之后连 placeholder 都看不见，没人认得出那是什么。
    const r3 = el("div", "row");
    r3.style.cssText = "gap:10px;flex-wrap:wrap;align-items:center";
    r3.appendChild(lab("主题 *"));
    const subj = mkInput(task.subject, "会议主题", "190px");
    bindText(subj, "subject");
    subj.title = "后台必填项";
    r3.appendChild(subj);
    r3.appendChild(lab("备注"));
    const rem = mkInput(task.remarks, "可空", "220px");
    bindText(rem, "remarks");
    r3.appendChild(rem);
    wrap.appendChild(r3);

    const issues = meetingIssues(task);
    if (issues.length) {
      const bad = el("div", null, issues.join("；"));
      bad.style.cssText = "color:var(--bad);font-size:11px";
      wrap.appendChild(bad);
    }
    return wrap;
  }

  /** 前端的即时校验，只为了边填边给红字。真正拦人的是 Python 端 meeting_data.validate。 */
  function meetingIssues(task) {
    const out = [];
    const hhmm = (v) => /^\d{1,2}:(00|30)$/.test(String(v || "").trim());
    if (!String(task.subject || "").trim()) out.push("会议主题没填");
    if (!task.repeat_weekly && !String(task.date || "").trim()) out.push("日期没填");
    if (!hhmm(task.start)) out.push("开始时间要是整点或半点");
    if (!hhmm(task.end)) out.push("结束时间要是整点或半点");
    if (hhmm(task.start) && hhmm(task.end) && task.end <= task.start) out.push("结束时间要晚于开始时间");
    if (task.building_only && !task.building && !String(task.room || "").trim()) {
      out.push("勾了「只要这栋」但没选楼栋");
    }
    return out;
  }

  function renderMeetingHint() {
    const active = state.meetingTasks.filter((t) => t.enabled);
    const bad = active.filter((t) => meetingIssues(t).length).length;
    $("#meetingHint").textContent = bad
      ? `${bad} 条还有问题，标红的那几行`
      : `启用 ${active.length} 条 · 改完记得点保存，「载入并检查」读的是保存后的清单`;
  }

  function saveMeetingTasks() {
    if (!hasTaskList()) return;
    callApi("meeting_save", state.activeForm, state.meetingTasks).then((r) => {
      if (!r) return;
      if (!r.ok) {
        appendLog("抢占任务保存失败：" + (r.error || ""), "error");
        return;
      }
      state.meetingTasks = (r.tasks || state.meetingTasks).map((t) => Object.assign({}, t));
      renderMeetingTasks();
      const bad = (r.issues || []).filter((x) => (x.items || []).length);
      appendLog(bad.length
        ? `抢占任务已保存，但有 ${bad.length} 条有问题：` +
          bad.map((x) => `第${x.index}条 ${x.items.join("；")}`).join(" / ")
        : `抢占任务已保存（${state.meetingTasks.length} 条）`,
        bad.length ? "warn" : "ok");
    });
  }

  /** 资源位多选下拉：关着的时候是已选 chip，打开是按场景分组的勾选列表。 */
  function renderPosChips() {
    renderPosField();
    renderPosList();
  }

  // ⚠ 勾一项只重画 field，不重画 list：list 一重画，勾选框就是新节点，
  //   列表滚动位置会跳回顶部——18 个资源位滚到下面勾几个时特别难受。
  function renderPosField() {
    const meta = state.wizardMeta;
    const field = $("#posField");
    field.innerHTML = "";
    if (!meta) return;

    if (!state.positions.length) {
      field.appendChild(el("span", null, "点这里选本次要投的资源位…"));
    } else {
      state.positions.forEach((name) => {
        const chip = el("span", "chip on");
        chip.appendChild(el("b", null, name));
        const x = el("span", "x", "×");
        x.addEventListener("click", (e) => {
          e.stopPropagation();               // 别把下拉一起点开
          togglePosition(name, false);
        });
        chip.appendChild(x);
        field.appendChild(chip);
      });
    }

    const n = state.positions.length;
    $("#posCount").textContent = `已选 ${n} / ${meta.positions.length}`;
    $("#posSummary").textContent = n
      ? `已选 ${n} 个：${state.positions.join("、")}`
      : "还没选。选了哪些，模板就只出哪些资源位的表";
    updateNextButtonState();
  }

  function renderPosList() {
    const list = $("#posList");
    const meta = state.wizardMeta;
    list.innerHTML = "";
    if (!meta) return;
    const kw = ($("#posSearch").value || "").trim().toLowerCase();

    const groups = new Map();            // 场景 → 资源位[]，保持 yaml 里的顺序
    meta.positions.forEach((p) => {
      const hay = `${p.name} ${p.scene || ""} ${p.real_name || ""}`.toLowerCase();
      if (kw && !hay.includes(kw)) return;
      const key = p.scene || "其他";
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(p);
    });

    if (!groups.size) {
      list.appendChild(el("div", "ms-group", `没有匹配「${kw}」的资源位`));
      return;
    }
    groups.forEach((items, scene) => {
      list.appendChild(el("div", "ms-group", scene));
      items.forEach((p) => {
        const lb = el("label", "ms-opt");
        const cb = document.createElement("input");
        cb.type = "checkbox";
        cb.checked = state.positions.includes(p.name);
        cb.addEventListener("change", () => togglePosition(p.name, cb.checked));
        lb.appendChild(cb);
        lb.appendChild(el("span", null, p.name));
        lb.appendChild(el("span", "tag", p.real_name ? `后台名：${p.real_name}` : `创意 ${p.system || ""}`));
        list.appendChild(lb);
      });
    });
  }

  function togglePosition(name, on) {
    const has = state.positions.includes(name);
    if (on && !has) {
      // 按 yaml 里的顺序排，界面上和模板里的 sheet 顺序对得上
      const order = state.wizardMeta.positions.map((p) => p.name);
      state.positions = order.filter((n) => n === name || state.positions.includes(n));
    } else if (!on && has) {
      state.positions = state.positions.filter((x) => x !== name);
    }
    savePrefs();
    renderPosField();
  }

  function setPosPanelOpen(open) {
    $("#posPanel").classList.toggle("hidden", !open);
    if (open) {
      $("#posSearch").value = "";
      renderPosList();
      $("#posSearch").focus();
    }
  }

  function renderActivityRow() {
    const seg = $("#activitySegmented");
    seg.querySelectorAll(".seg-item").forEach((n) => {
      n.classList.toggle("active", n.dataset.activity === state.activityMode);
    });
    const existing = state.activityMode === "existing";
    $("#activityIdWrap").classList.toggle("hidden", !existing);
    $("#activityIdInput").value = state.activityId;
    $("#activityHint").textContent = existing
      ? "单元直接挂到这个活动下，模板不带「活动」sheet"
      : "模板会多一张「活动」sheet，填一行，本次所有单元都挂在它下面";
  }

  function initWizardActions() {
    $("#activitySegmented").querySelectorAll(".seg-item").forEach((n) => {
      n.addEventListener("click", () => {
        state.activityMode = n.dataset.activity;
        savePrefs();
        renderActivityRow();
      });
    });
    $("#activityIdInput").addEventListener("input", (e) => {
      state.activityId = e.target.value.trim(); savePrefs();
    });
    $("#btnPosAll").addEventListener("click", () => {
      if (!state.wizardMeta) return;
      state.positions = state.wizardMeta.positions.map((p) => p.name);
      savePrefs(); renderPosChips();
    });
    $("#btnPosNone").addEventListener("click", () => {
      state.positions = []; savePrefs(); renderPosChips();
    });
    $("#posField").addEventListener("click", () => {
      setPosPanelOpen($("#posPanel").classList.contains("hidden"));
    });
    $("#posSearch").addEventListener("input", renderPosList);
    $("#btnPosDone").addEventListener("click", () => setPosPanelOpen(false));
    document.addEventListener("click", (e) => {
      if (!$("#posSelect").contains(e.target)) setPosPanelOpen(false);
      // 点到别处就收起搭售角标的菜单（角标自己的 click 已经 stopPropagation）
      if (!e.target.closest || !e.target.closest(".stie-wrap")) {
        document.querySelectorAll(".stie-menu").forEach((m) => m.remove());
      }
    });
    $("#btnOpenStrategy").addEventListener("click", openStrategy);
    $("#strategySelect").addEventListener("change", (e) => {
      if (!state.strategyDoc) return;
      state.strategyDoc.active = e.target.value;
      // 执行时后端读的是存盘里的 active，所以切一下就得落盘，不能只记在界面上
      callApi("strategy_save", state.activeForm, state.strategyDoc).then((res) => {
        if (res && res.ok) {
          state.strategyDoc = res.doc;
          appendLog(`当前策略切到「${res.doc.active}」`, "ok");
          renderStrategyRow();
        }
      });
    });
    initStrategyPanel();
  }

  function refreshStrategy() {
    callApi("strategy_get", state.activeForm).then((res) => {
      if (!res || !res.ok) return;
      state.strategyDoc = res.doc;
      state.strategyPath = res.path;
      renderStrategyRow();
      const item = res.doc.items[res.doc.active] || {};
      if (!item.updated_at && state.wizardTab === "deliver") {
        setWizardTab("strategy");
        appendLog("这个配置类型还没配过策略，先在策略中心把规则定下来", "warn");
      }
    });
  }

  function renderStrategyRow() {
    const sel = $("#strategySelect");
    const doc = state.strategyDoc;
    sel.innerHTML = "";
    if (!doc) return;
    Object.keys(doc.items).forEach((name) => {
      const o = el("option", null, name);
      o.value = name;
      if (name === doc.active) o.selected = true;
      sel.appendChild(o);
    });
    const item = doc.items[doc.active] || { rules: {}, groups: {}, exceptions: [] };
    const rules = item.rules || {};
    const groups = item.groups || {};
    const exc = item.exceptions || [];
    const n = Object.keys(rules).length;
    $("#strategySummary").textContent = n
      ? `已配 ${n} 项规则` + (exc.length ? `，${exc.length} 条例外` : "") +
        (item.updated_at ? `　（${item.updated_at} 更新）` : "")
      : "还没配。生成模板前先去策略中心配一次，这些字段不会出现在 Excel 里";

    // 「本次按什么跑」的一句话摘要：开跑前扫一眼就能核对，不用切到策略中心
    const brief = [];
    const pick = (k) => (rules[k] ? `${k} ${rules[k]}` : null);
    ["生效平台", "投放流量池", "展示不超过", "创意赛马"].forEach((k) => {
      const s = pick(k);
      if (s) brief.push(s);
    });
    // 方案组各来一句：「人群 新客」/「内容限制 按单元名称匹配」
    (state.wizardMeta && state.wizardMeta.scheme_groups ? state.wizardMeta.scheme_groups
      : [{ key: "audience", name: "人群" }]).forEach((g) => {
      const gv = groups[g.key] || {};
      brief.push(gv.mode === "keyword" ? `${g.name} 按单元名称匹配`
                                       : `${g.name} ${gv.scheme || "未选"}`);
    });
    if (exc.length) brief.push(`${exc.length} 条例外`);
    $("#strategyBrief").textContent = n || exc.length ? brief.join("　·　") : "—";
  }

  function renderTopbar() {
    const meta = currentFormMeta();
    // 标题按侧栏那套「主 Tab · 分 Tab」写，和左边对得上
    const label = state.activeForm ? formLabel(state.activeForm) : "";
    const group = state.activeForm ? formGroup(state.activeForm) : "";
    $("#topTitle").textContent = label ? (group ? `${group} · ${label}` : label) : "—";
    $("#topSubtitle").textContent = (meta && meta.scopes && meta.scopes.length) ? meta.scopes[0][0] : "";
    $("#reviewTitle").textContent = `核对 · ${label}`;
  }

  // ---------------- 准备页：延期范围 ----------------
  function renderScopeRow() {
    const meta = currentFormMeta();
    const row = $("#scopeRow");
    const seg = $("#scopeSegmented");
    seg.innerHTML = "";
    if (!meta || !meta.scopes || !meta.scopes.length) {
      row.classList.add("hidden");
      state.scopeValue = null;
      return;
    }
    row.classList.remove("hidden");
    meta.scopes.forEach(([label, value], i) => {
      const item = el("div", "seg-item" + (i === 0 ? " active" : ""), label);
      item.dataset.value = value;
      item.addEventListener("click", () => {
        seg.querySelectorAll(".seg-item").forEach((n) => n.classList.remove("active"));
        item.classList.add("active");
        state.scopeValue = value;
        state.loaded = false;   // 换范围了，之前载入的数据不再对得上
        state.previewRows = [];
        renderReviewTable();
        updateNextButtonState();
        updateScopeHint(value);
      });
      seg.appendChild(item);
    });
    state.scopeValue = meta.scopes[0][1];
    updateScopeHint(state.scopeValue);
  }

  function updateScopeHint(value) {
    // 批量开关自己有一套控件（见 renderToggleCard），共用的「延期范围」行是藏着的
    if (hasToggle()) return;
    const needsExcel = value === "id_list";
    $("#scopeHint").textContent = needsExcel
      ? "这个范围要 Excel 清单：先点「生成 Excel 模板」，填好后用「浏览…」选它"
      : "这个范围直接读网页，不用选数据文件，点「载入并检查」即可";
  }

  // ---------------- 资源位投放的二级 Tab ----------------
  // 策略中心是这个配置类型下和「投放配置」并列的一块，不是弹窗：
  // 规则在这里统一管，投放配置那边只管选资源位、填 Excel、跑。
  function setWizardTab(tab) {
    if (!hasStrategy()) tab = "deliver";
    if (tab === "strategy" && (!state.wizardMeta || !state.strategyDoc)) {
      appendLog("策略还没载入好，稍等一下再点", "warn");
      return;
    }
    state.wizardTab = tab;
    document.querySelectorAll("[data-wtab]").forEach((n) => {
      n.classList.toggle("active", n.dataset.wtab === tab);
    });
    const onStrategy = tab === "strategy";
    $(".stepbar").classList.toggle("hidden", onStrategy);
    $(".footer-bar").classList.toggle("hidden", onStrategy);
    // 提示语按配置类型说人话：两边的字段和步骤都不一样
    $("#wizardTabHint").textContent = onStrategy
      ? (uiText().strategy_hint || "配在这里的字段，模板里就不用逐个单元填了")
      : (uiText().deliver_hint || "生成模板 → 填好 Excel → 载入并检查 → 跑");

    if (onStrategy) {
      if (!strategyUI.draft) strategyUI.draft = JSON.parse(JSON.stringify(state.strategyDoc));
      strategyUI.adding = null;
      $("#strategyPath").textContent = state.strategyPath || "";
      document.querySelectorAll(".step-panel").forEach((n) => {
        n.classList.toggle("active", n.dataset.panel === "strategy");
      });
      renderStrategyPanel();
    } else {
      if (strategyDirty()) appendLog("策略中心里有改动还没保存", "warn");
      goToStep(state.step);
    }
  }

  function strategyDirty() {
    if (!strategyUI.draft || !state.strategyDoc) return false;
    return JSON.stringify(strategyUI.draft) !== JSON.stringify(state.strategyDoc);
  }

  function initWizardTabs() {
    document.querySelectorAll("[data-wtab]").forEach((n) => {
      n.addEventListener("click", () => setWizardTab(n.dataset.wtab));
    });
  }

  // ---------------- 步骤条 ----------------
  function goToStep(step) {
    state.step = step;
    document.querySelectorAll(".seg-item[data-step]").forEach((n) => {
      n.classList.toggle("active", n.dataset.step === step);
    });
    document.querySelectorAll(".step-panel").forEach((n) => {
      n.classList.toggle("active", n.dataset.panel === step);
    });
    $("#filterPills").classList.toggle("hidden", step !== "review");
    if (step === "review") renderFilterPills();
    updateNextButtonState();
  }

  function updateNextButtonState() {
    const idx = STEP_ORDER.indexOf(state.step);
    const nextBtn = $("#btnNextStep");
    if (idx === STEP_ORDER.length - 1) {
      nextBtn.textContent = "开始配置";
      nextBtn.disabled = state.running;
    } else {
      nextBtn.textContent = `下一步 · ${STEP_LABEL[STEP_ORDER[idx + 1]]}`;
      nextBtn.disabled = false; // 可以点；点了不满足条件会提示原因，而不是先灰掉猜用户想干嘛
    }
    $("#btnPrevStep").disabled = idx === 0 || state.running;
  }

  function blockedReason(targetStep) {
    if (targetStep === "review" && hasPositions() && !state.positions.length) {
      return "先在「准备」页勾选本次要投的资源位";
    }
    if (targetStep === "review" && !state.loaded) return "请先在「准备」页选好数据文件，点「载入并检查」";
    if (targetStep === "execute" && !state.browserConnected) return "浏览器没连上，请先点右上角「启动浏览器并登录」";
    return null;
  }

  function tryGoToStep(target) {
    const curIdx = STEP_ORDER.indexOf(state.step);
    const targetIdx = STEP_ORDER.indexOf(target);
    if (targetIdx > curIdx) {
      for (let i = curIdx + 1; i <= targetIdx; i++) {
        const reason = blockedReason(STEP_ORDER[i]);
        if (reason) { appendLog(reason, "warn"); return; }
      }
    }
    goToStep(target);
  }

  function initStepNav() {
    document.querySelectorAll(".seg-item[data-step]").forEach((n) => {
      n.addEventListener("click", () => tryGoToStep(n.dataset.step));
    });
    $("#btnPrevStep").addEventListener("click", () => {
      const idx = STEP_ORDER.indexOf(state.step);
      if (idx > 0) goToStep(STEP_ORDER[idx - 1]);
    });
    $("#btnNextStep").addEventListener("click", () => {
      const idx = STEP_ORDER.indexOf(state.step);
      if (idx === STEP_ORDER.length - 1) { startRun(); return; }
      tryGoToStep(STEP_ORDER[idx + 1]);
    });
  }

  // ---------------- 运行模式分段控件 ----------------
  function initModeSegmented() {
    const seg = $("#modeSegmented");
    seg.querySelectorAll(".seg-item").forEach((n) => {
      n.addEventListener("click", () => {
        seg.querySelectorAll(".seg-item").forEach((x) => x.classList.remove("active"));
        n.classList.add("active");
        state.runMode = n.dataset.mode;
      });
    });
  }

  // ---------------- 浏览器连接状态 ----------------
  function setBrowserStatus(ok) {
    ok = !!ok;
    const changed = state.browserConnected !== ok;
    state.browserConnected = ok;
    $("#browserDot").classList.toggle("ok", ok);
    const label = $("#browserLabel");
    label.textContent = ok ? "浏览器已连接" : "浏览器未连接";
    label.className = ok ? "label-ok" : "label-off";
    if (changed) updateNextButtonState();
  }

  function pollBrowserStatus() {
    callApi("browser_status").then((ok) => setBrowserStatus(!!ok));
  }

  function initLaunchBrowser() {
    $("#btnLaunchBrowser").addEventListener("click", () => {
      appendLog("正在启动浏览器…", "info");
      callApi("launch_browser", state.activeForm).then((res) => {
        if (!res) return;
        appendLog(res.message, res.ok ? "ok" : "error");
        pollBrowserStatus();
      }).catch((err) => appendLog(`启动失败：${err}`, "error"));
    });
  }

  // ---------------- 准备页：浏览 / 生成模板 / 载入 ----------------
  function initPrepareActions() {
    $("#btnBrowseFile").addEventListener("click", () => {
      callApi("pick_file").then((path) => {
        if (!path) return;
        state.dataFile = path;
        $("#dataFileInput").value = path;
        doLoadCheck();   // 选完文件顺手载入一次，和旧版一致
      });
    });

    // 生成模板不再顺带问「要哪些资源位」——资源位在上面的卡片里已经选好了，
    // 这里只负责按已选好的东西出一份 Excel
    $("#btnMakeTemplate").addEventListener("click", () => {
      if (!state.activeForm) return;
      if (hasPositions()) {
        if (!state.positions.length) {
          appendLog("先在上面勾选本次要投的资源位，再生成模板", "warn");
          return;
        }
        callApi("make_template", state.activeForm, null, state.positions,
                { existing_activity: state.activityMode === "existing" })
          .then(handleTemplateResult);
        return;
      }
      if (hasActivity()) {   // 没有资源位可选、但要挂活动的类型（价格面板配置）
        callApi("make_template", state.activeForm, null, null,
                { existing_activity: state.activityMode === "existing" })
          .then(handleTemplateResult);
        return;
      }
      callApi("make_template", state.activeForm, state.scopeValue).then(handleTemplateResult);
    });

    $("#btnLoadCheck").addEventListener("click", doLoadCheck);
  }

  function handleTemplateResult(res) {
    if (!res || !res.ok) {
      appendLog(`生成模板失败：${res ? res.error : "无法连接后端"}`, "error");
      return;
    }
    appendLog(`模板已生成：${res.path}`, "ok");
    showModal({
      title: "生成成功",
      desc: `模板已生成：\n${res.path}\n\n现在打开吗？`,
      buttons: [
        { label: "打开", primary: true, onClick: () => callApi("open_path", res.path) },
        { label: "关闭" },
      ],
    });
  }

  function wizardOptions() {
    if (!hasPositions() && !hasActivity()) return null;
    return {
      positions: state.positions,
      activity: {
        existing: state.activityMode === "existing",
        activity_id: state.activityId,
      },
    };
  }

  function doLoadCheck() {
    if (!state.activeForm) return;
    if (hasActivity() && state.activityMode === "existing" && !state.activityId) {
      appendLog("选了「挂到已有活动」，先把活动ID填上", "warn");
      return;
    }
    const btn = $("#btnLoadCheck");
    btn.disabled = true;
    appendLog("正在载入并检查…", "info");
    const opts = wizardOptions() || {};
    if (hasToggle()) {
      const sc = state.tgScope;
      opts.toggle_direction = state.toggleDir;
      opts.toggle_params = sc === "list" ? $("#tgListInput").value
        : sc === "keyword" ? $("#tgKeywordInput").value : "";
      if (sc === "ledger") {
        opts.toggle_date_from = $("#tgLedgerFrom").value || "";
        opts.toggle_date_to = $("#tgLedgerTo").value || "";
        opts.toggle_strategies = $("#tgLedgerStrategy").value || "";
      } else {
        opts.toggle_strategies = state.tgStrategyMode === "list" ? $("#tgStrategyInput").value : "";
      }
    }
    callApi("load_and_check", state.activeForm, state.dataFile, state.scopeValue,
            Object.keys(opts).length ? opts : null)
      .then((res) => {
        btn.disabled = false;
        if (!res || !res.ok) {
          appendLog(`载入失败：${res ? res.error : "无法连接后端"}`, "error");
          return;
        }
        state.previewRows = res.rows;
        state.loaded = true;
        state.reviewFilter = "all";
        appendLog(
          `载入 ${res.total} 条配置，${res.total - res.bad} 条通过校验` + (res.bad ? `，${res.bad} 条有问题` : ""),
          res.bad ? "warn" : "ok");
        updateSidebarActive();
        renderReviewTable();
        updateNextButtonState();
      })
      .catch((err) => { btn.disabled = false; appendLog(`载入失败：${err}`, "error"); });
  }

  // ---------------- 核对页 ----------------
  function renderFilterPills() {
    const wrap = $("#filterPills");
    wrap.innerHTML = "";
    if (state.step !== "review" || !state.previewRows.length) return;
    const rows = state.previewRows;
    const bad = rows.filter((r) => r.issues.length).length;
    const done = rows.filter((r) => r.done).length;
    [["all", `全部 ${rows.length}`], ["bad", `有问题 ${bad}`], ["done", `已完成 ${done}`]].forEach(([key, label]) => {
      const p = el("span", "pill clickable" + (state.reviewFilter === key ? " on" : ""), label);
      p.addEventListener("click", () => { state.reviewFilter = key; renderReviewTable(); renderFilterPills(); });
      wrap.appendChild(p);
    });
  }

  function renderReviewTable() {
    const body = $("#reviewBody");
    if (!state.loaded) {
      body.innerHTML = '<div class="empty-state"><b>还没有数据</b><span>先在「准备」页选好数据文件，点「载入并检查」</span></div>';
      return;
    }
    if (!state.previewRows.length) {
      body.innerHTML = '<div class="empty-state"><b>没有数据</b><span>这个范围目前没有匹配的记录</span></div>';
      return;
    }
    const filtered = state.previewRows.filter((r) => {
      if (state.reviewFilter === "bad") return r.issues.length > 0;
      if (state.reviewFilter === "done") return r.done;
      return true;
    });
    let html = '<div style="border:1px solid var(--bd);border-radius:9px;flex:1;min-height:0;overflow:auto">' +
      '<table class="data"><thead><tr>' +
      '<th class="num" style="width:52px">序号</th><th>名称</th><th style="width:120px">类型</th>' +
      '<th class="num" style="width:56px">明细</th><th style="width:260px">校验结果</th>' +
      '</tr></thead><tbody>';
    filtered.forEach((r) => {
      let cls = "", verdict;
      if (r.issues.length) {
        cls = "bad";
        verdict = "✗ " + r.issues.slice(0, 2).join("；") + (r.issues.length > 2 ? "…" : "");
      } else if (r.done) {
        cls = "skip";
        verdict = "— 已完成，本次跳过";
      } else {
        verdict = "✓ 校验通过";
      }
      html += `<tr class="${cls}" data-index="${r.index}"><td class="num">${r.index}</td>` +
        `<td>${escapeHtml(r.name)}</td><td>${escapeHtml(r.kind)}</td><td class="num">${r.detail_count}</td>` +
        `<td>${escapeHtml(verdict)}</td></tr>`;
    });
    html += "</tbody></table></div>";
    if (!filtered.length) {
      html = '<div class="empty-state"><b>这个筛选下没有行</b></div>';
    }
    body.innerHTML = html;
    body.querySelectorAll("tr[data-index]").forEach((tr) => {
      tr.addEventListener("dblclick", () => showDetail(parseInt(tr.dataset.index, 10)));
    });
  }

  function showDetail(index) {
    callApi("row_detail", index).then((d) => {
      if (!d) return;
      $("#detailTitle").textContent = `第 ${d.index} 条 · ${d.name}`;
      $("#detailIssues").textContent = d.issues.length ? "问题：" + d.issues.join("；") : "校验通过，没有发现问题。";
      let html = "<div style='margin-bottom:6px;color:var(--sub)'>主表</div>";
      Object.entries(d.header || {}).forEach(([k, v]) => {
        if (String(v).trim()) html += `${escapeHtml(k)}：${escapeHtml(v)}<br>`;
      });
      if (d.items && d.items.length) {
        html += "<div style='margin:12px 0 6px;color:var(--sub)'>明细</div>";
        d.items.forEach((it, i) => {
          const parts = Object.entries(it).filter(([, v]) => String(v).trim()).map(([k, v]) => `${k}=${v}`).join("，");
          html += `第${i + 1}项：${escapeHtml(parts)}<br>`;
        });
      }
      $("#detailBody").innerHTML = html;
      $("#detailModal").classList.remove("hidden");
    });
  }

  // ---------------- 执行页 ----------------
  function setRunButtons(running) {
    $("#btnStart").disabled = running;
    $("#btnPause").disabled = !running;
    $("#btnStop").disabled = !running;
    if (!running) $("#btnPause").textContent = "暂停";
    updateNextButtonState();
  }

  function startRun() {
    if (!state.loaded) {
      appendLog("还没有载入数据，请先在「准备」页载入并检查", "warn");
      goToStep("prepare");
      return;
    }
    if (!state.browserConnected) {
      appendLog("浏览器没连上，请先点右上角「启动浏览器并登录」", "warn");
      return;
    }
    if (state.runMode === "auto") {
      showModal({
        title: "确认全自动",
        desc: "全自动模式会连续提交，中途不再询问。\n\n建议先用「逐条确认」跑通前几条。确定继续？",
        buttons: [
          { label: "确定", primary: true, onClick: doStartRun },
          { label: "取消" },
        ],
      });
      return;
    }
    doStartRun();
  }

  function doStartRun() {
    const skip = $("#skipDoneCheck").checked;
    $("#failedSection").classList.add("hidden");
    callApi("start_run", state.runMode, skip).then((res) => {
      if (!res || !res.ok) {
        appendLog(`没法开始：${res ? res.error : "无法连接后端"}`, "error");
        return;
      }
      state.running = true;
      setRunButtons(true);
      $("#runProgressBar").style.width = "0%";
      $("#runStat").textContent = `0/${res.total}`;
      appendLog(`开始配置，共 ${res.total} 条`, "ok");
      goToStep("execute");
    });
  }

  // ---------------- 失败清单 / 重跑 ----------------
  function renderFailedList(failed) {
    const section = $("#failedSection");
    const list = $("#failedList");
    list.innerHTML = "";
    if (!failed || !failed.length) {
      section.classList.add("hidden");
      return;
    }
    section.classList.remove("hidden");
    $("#failedCount").textContent = `失败 ${failed.length} 条`;
    failed.forEach((f) => {
      const row = el("div", "row");
      row.style.cssText = "padding:8px 12px;gap:10px;border-bottom:1px solid var(--bd)";
      const info = el("div");
      info.style.cssText = "flex:1;min-width:0";
      info.innerHTML = `<div>${escapeHtml(f.name)}</div>` +
        `<div style="color:var(--bad);font-size:11px">${escapeHtml(f.error)}</div>`;
      const btn = el("button", "btn btn-sm", "重跑");
      btn.addEventListener("click", () => retryRows([f.index]));
      row.appendChild(info);
      row.appendChild(btn);
      list.appendChild(row);
    });
    $("#btnRetryAll").onclick = () => retryRows(failed.map((f) => f.index));
  }

  function retryRows(indices) {
    if (state.running) {
      appendLog("正在跑，等这一轮结束再重跑", "warn");
      return;
    }
    callApi("retry_rows", indices, state.runMode).then((res) => {
      if (!res || !res.ok) {
        appendLog(`没法重跑：${res ? res.error : "无法连接后端"}`, "error");
        return;
      }
      $("#failedSection").classList.add("hidden");
      state.running = true;
      setRunButtons(true);
      $("#runProgressBar").style.width = "0%";
      $("#runStat").textContent = `0/${res.total}`;
      appendLog(`重跑 ${res.total} 条`, "ok");
    });
  }

  function initExecuteActions() {
    $("#btnStart").addEventListener("click", startRun);
    $("#btnPause").addEventListener("click", () => {
      callApi("pause_run").then((res) => {
        if (!res) return;
        $("#btnPause").textContent = res.paused ? "继续" : "暂停";
        appendLog(res.paused ? "已暂停（当前这条会填完再停）" : "已继续", res.paused ? "warn" : "info");
      });
    });
    $("#btnStop").addEventListener("click", () => {
      callApi("stop_run");
      appendLog("正在停止…", "warn");
    });
  }

  // ---------------- 侧栏底部：打开结果目录 / 清除断点 ----------------
  function initSidebarFooterActions() {
    $("#btnOpenOutput").addEventListener("click", () => callApi("open_output_dir"));
    $("#btnCheckUpdate").addEventListener("click", () => checkForUpdate(true));
    $("#btnInstallUpdate").addEventListener("click", downloadAndInstallUpdate);
    $("#btnClearState").addEventListener("click", () => {
      if (!state.activeForm) return;
      showModal({
        title: "清除断点",
        desc: "清除后会从第一条重新开始（已提交的不会撤销）。确定？",
        buttons: [
          {
            label: "确定", primary: true, onClick: () => {
              callApi("clear_state", state.activeForm).then((res) => {
                if (res && res.ok) {
                  appendLog("断点已清除", "ok");
                  if (state.loaded) doLoadCheck();
                } else {
                  appendLog(`清除失败：${res ? res.error : "无法连接后端"}`, "error");
                }
              });
            },
          },
          { label: "取消" },
        ],
      });
    });
  }

  // ---------------- 策略中心 ----------------
  // 一套策略 = 通用规则（按组分卡）+ 方案组（人群 / 内容限制，各一个方案库）+ 例外清单。
  // 折叠态是一份可核对的清单，点开才变成表单：这个页面读的次数远多于改的次数。
  // 取值逻辑全在 Python 端（wizard_strategy.resolve），这里只负责编辑那份 JSON。
  const strategyUI = {
    draft: null,
    open: {},              // 哪几张卡是展开的
    openScheme: {},        // 每个方案组里展开的是哪一套 {组key: 方案名}
    scopeToSelection: true,
    adding: null,          // 正在加的那条例外 {positions, field, value}
  };

  // 方案组只剩两种用法 —— 2026-08-21 去掉了「Excel 里逐单元填」，
  // 这些字段列多到看不过来，逐行填纯属重复劳动（Python 端同步去掉了）
  const MODE_LABEL = { fixed: "全部用同一套", keyword: "按单元名称匹配" };

  function openStrategy() { setWizardTab("strategy"); }

  /** 方案组清单（人群 / 内容限制），来自后端 wizard_meta */
  function schemeGroups() {
    return (state.wizardMeta && state.wizardMeta.scheme_groups) || [];
  }

  function draftItem() {
    const d = strategyUI.draft;
    if (!d.items[d.active]) d.items[d.active] = { rules: {}, groups: {}, exceptions: [] };
    const it = d.items[d.active];
    it.rules = it.rules || {};
    it.groups = it.groups || {};
    schemeGroups().forEach((g) => {
      const gv = it.groups[g.key] || (it.groups[g.key] = {});
      gv.schemes = gv.schemes || {};
      gv.rules = gv.rules || [];
      gv.fallback = gv.fallback || [];
      if (!MODE_LABEL[gv.mode]) gv.mode = "fixed";
    });
    it.exceptions = it.exceptions || [];
    return it;
  }

  /** 某一组的草稿数据 */
  function draftGroup(key) { return draftItem().groups[key]; }

  function strategyDirty() {
    if (!strategyUI.draft || !state.strategyDoc) return false;
    return JSON.stringify(strategyUI.draft) !== JSON.stringify(state.strategyDoc);
  }

  /** 这个字段这次用不用得上：本次选中的资源位里有没有它 */
  function fieldInScope(f) {
    if (!hasPositions()) return true;      // 单资源位的配置类型，没有「本次投哪些位」这回事
    if (!strategyUI.scopeToSelection || !state.positions.length) return true;
    return (f.positions || []).some((p) => state.positions.includes(p));
  }

  /** 级联：父字段没选到触发值，这个字段就不该出现
   *
   *  when = [父字段, [触发值...]]。**触发值是个数组**：同一个字段常常挂在父字段的
   *  好几个取值下（「搭售类型」选「买赠」和「买赠+0元购」都要填 价格面板pid）。
   *  ⚠ 要和 Python 端 wizard_schema.when_active 保持同一套判断，
   *    否则会出现「界面上没这一项、跑起来却说它必填」。 */
  function fieldRevealed(f, values) {
    if (!f.when) return true;
    // ⚠ 兜底：父字段压根不在策略中心里（比如它被放进了 Excel）——这儿永远取不到
    //   它的值，照级联判就是整组恒定不显示，那几项再也配不了，而且界面上一点
    //   报错都没有。父字段不在这份清单里就不做级联，触发条件由 field_defs_for_ui
    //   写进字段说明给人看。
    //   （价格面板的「内容限制」曾经就是这么整组空掉的：父字段收银台类型在 Excel 里。
    //     2026-08-26 已经把收银台类型搬进策略中心，这条兜底现在不该再被触发 ——
    //     留着是防同样的配法再出现一次。）
    if (!strategyFieldExists(f.when[0])) return true;
    const cur = String(values[f.when[0]] || "");
    // 多选父字段（我想投放 =「在期大会员,未登录」）按成员判断
    const members = cur.split(/[,，]/).map((s) => s.trim());
    const want = Array.isArray(f.when[1]) ? f.when[1] : [f.when[1]];
    return want.some((v) => cur === v || members.includes(v) || (cur && cur.startsWith(v)));
  }

  function visibleFields(list, values, scoped) {
    // ui: sku_tie_badge 的字段不单独占一行 —— 它们是套餐卡片右边那个小角标读写的
    //（买赠SKU / 0元购SKU）。「这个 SKU 上不上面板」和「它搭什么」是一个决定，
    // 拆成两处填必然对不上。
    return list.filter((f) => f.ui !== "sku_tie_badge"
      && (!scoped || fieldInScope(f)) && fieldRevealed(f, values));
  }

  // 搭售角标：四选一，两个存盘字段拼出来（和后台的 sale_strategy / add_type 同构）
  const TIE_FIELDS = ["买赠SKU", "0元购SKU"];
  const TIE_CHOICES = [
    { label: "无", bits: [] },
    { label: "买赠", bits: ["买赠SKU"] },
    { label: "0元购", bits: ["0元购SKU"] },
    { label: "买赠+0元购", bits: ["买赠SKU", "0元购SKU"] },
  ];

  function tieHasBadge(values) {
    return TIE_FIELDS.some((n) => strategyFieldExists(n));
  }
  function tieListOf(values, name) {
    return String(values[name] || "").split(/[,，]/).map((s) => s.trim()).filter(Boolean);
  }
  function tieOf(values, sku) {
    const bits = TIE_FIELDS.filter((n) => tieListOf(values, n).includes(sku));
    return TIE_CHOICES.find((c) => c.bits.length === bits.length
      && c.bits.every((b) => bits.includes(b))) || TIE_CHOICES[0];
  }
  function setTie(values, sku, choice) {
    TIE_FIELDS.forEach((n) => {
      const cur = tieListOf(values, n).filter((x) => x !== sku);
      if (choice.bits.includes(n)) cur.push(sku);
      values[n] = cur.join(",");
    });
  }
  /** 从三段面板里移出一个 SKU 时，它的搭售标记也要跟着清掉，
   *  否则面板里没有的 SKU 还挂着「买赠」，跑起来谁都找不到那张卡片。 */
  function dropTie(values, sku) { setTie(values, sku, TIE_CHOICES[0]); }

  /** 套餐卡片右边那个角标：点开是四选一，不是循环切换 ——
   *  循环点法看不出「一共有哪几种」，也不知道自己点到第几下了。 */
  function tieBadge(sku, values, fire) {
    const cur = tieOf(values, sku);
    const wrap = el("span", "stie-wrap");
    const btn = el("b", "stie" + (cur.bits.length ? " on" : ""),
                   (cur.bits.length ? cur.label : "搭售") + " ⌄");
    btn.title = `搭售类型：${cur.label}（点一下改）`;
    wrap.appendChild(btn);

    const close = () => { const m = wrap.querySelector(".stie-menu"); if (m) m.remove(); };
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      if (wrap.querySelector(".stie-menu")) { close(); return; }
      document.querySelectorAll(".stie-menu").forEach((m) => m.remove());
      const menu = el("div", "stie-menu");
      TIE_CHOICES.forEach((c) => {
        const it = el("div", "stie-item" + (c.label === cur.label ? " sel" : ""), c.label);
        it.addEventListener("click", (ev) => {
          ev.stopPropagation();
          setTie(values, sku, c);
          close();
          fire();
        });
        menu.appendChild(it);
      });
      wrap.appendChild(menu);
    });
    return wrap;
  }

  function strategyFieldExists(name) {
    return (state.wizardMeta.strategy_fields || []).some((f) => f.name === name);
  }

  /** 这个字段在级联里的第几层：人群选组 0 → 人群类型 1 → 人群ID / 人群标签 2
   *  ⚠ 父字段不在策略中心里的（见 fieldRevealed）算第 0 层 —— 它在这儿是顶层项，
   *    缩进成子项反而让人以为上面还有一行没填。 */
  function fieldDepth(f) {
    const all = state.wizardMeta.strategy_fields || [];
    let depth = 0, cur = f;
    while (cur && cur.when && depth < 6) {
      const parent = all.find((x) => x.name === cur.when[0]);
      if (!parent) return 0;
      cur = parent;
      depth++;
    }
    return depth;
  }

  function hasDescendants(name) {
    return (state.wizardMeta.strategy_fields || []).some((f) => f.when && f.when[0] === name);
  }

  /** 父字段改了值，子孙字段留着的旧值就没意义了（还会让它们错误地显示出来），清掉 */
  function clearDescendants(values, name) {
    (state.wizardMeta.strategy_fields || []).forEach((f) => {
      if (f.when && f.when[0] === name && values[f.name] !== undefined) {
        delete values[f.name];
        clearDescendants(values, f.name);
      }
    });
  }

  function fieldsOfGroup(group) {
    return (state.wizardMeta.strategy_fields || []).filter((f) => f.group === group);
  }

  // ---------------- 卡片外壳 ----------------
  function card(key, title, summary, body, extraHead) {
    const open = !!strategyUI.open[key];
    const box = el("div", "scard" + (open ? " open" : ""));
    const head = el("div", "scard-head");
    head.appendChild(el("span", "scard-title", title));
    if (open) {
      if (extraHead) head.appendChild(extraHead);
    } else {
      head.appendChild(el("span", "scard-sum", summary));
    }
    head.appendChild(el("span", "scard-act", open ? "收起" : "编辑"));
    head.addEventListener("click", (e) => {
      if (e.target.closest(".scard-body") || e.target.closest("input, select, button")) return;
      strategyUI.open[key] = !open;
      renderStrategyPanel();
    });
    box.appendChild(head);
    if (open && body) {
      const wrap = el("div", "scard-body");
      wrap.appendChild(body);
      box.appendChild(wrap);
    }
    return box;
  }

  /** 折叠态的值清单：字段 / 值 两列，和例外一样的读法 */
  function valueList(pairs) {
    const wrap = el("div", "svals");
    pairs.forEach(([k, v, muted]) => {
      const row = el("div", "svals-row");
      row.appendChild(el("span", "k", k));
      row.appendChild(el("span", muted ? "v mu" : "v", v));
      wrap.appendChild(row);
    });
    return wrap;
  }

  // ---------------- 通用规则卡 ----------------
  function ruleCard(group) {
    const item = draftItem();
    const all = fieldsOfGroup(group);
    const shown = visibleFields(all, item.rules, true);
    const hidden = all.length - shown.length;

    const filled = shown.filter((f) => String(item.rules[f.name] || "").trim());
    const summary = filled.length
      ? filled.map((f) => item.rules[f.name]).join(" · ")
      : "未配置";

    const body = el("div", "col");
    body.style.gap = "12px";
    const grid = el("div", "strategy-grid");
    shown.forEach((f) => grid.appendChild(strategyField(f, item.rules, null, () => renderStrategyPanel())));
    body.appendChild(grid);
    if (hidden > 0) {
      body.appendChild(el("div", "snote", `另有 ${hidden} 项本次用不上或未触发，已隐藏`));
    }
    return card(`g:${group}`, group, summary, body);
  }

  // ---------------- 方案组卡（人群 / 内容限制）----------------
  // 两组用法完全一样，所以只有这一套渲染代码，按 g（组定义）参数化。
  function schemeCard(g) {
    const grp = draftGroup(g.key);
    const mode = grp.mode || "fixed";
    const schemes = Object.keys(grp.schemes);

    let summary = MODE_LABEL[mode];
    if (mode === "fixed") summary += grp.scheme ? ` · ${grp.scheme}` : " · 未选方案";
    if (mode === "keyword") {
      const multi = grp.rules.filter((r) => (r.schemes || []).length > 1).length;
      summary += ` · ${grp.rules.length} 条规则，${schemes.length} 套方案`;
      if (multi) summary += `，其中 ${multi} 条用多套`;
    }

    const body = el("div", "col");
    body.style.gap = "14px";

    // —— 怎么来 ——
    const modeRow = el("div", "col");
    modeRow.style.gap = "6px";
    modeRow.appendChild(el("div", "snote", `这批单元的${g.name}怎么来`));
    const seg = el("div", "segmented");
    Object.keys(MODE_LABEL).forEach((m) => {
      const it = el("div", "seg-item" + (m === mode ? " active" : ""), MODE_LABEL[m]);
      it.addEventListener("click", () => { grp.mode = m; renderStrategyPanel(); renderStrategyRow(); });
      seg.appendChild(it);
    });
    modeRow.appendChild(seg);
    body.appendChild(modeRow);

    if (mode === "fixed") {
      const wrap = el("div", "sfield");
      wrap.appendChild(el("div", "sname", "用哪一套"));
      wrap.appendChild(schemePicker(g, grp.scheme ? [grp.scheme] : [],
                                    (v) => { grp.scheme = v[0] || ""; }, "点一下选中"));
      body.appendChild(wrap);
    } else {
      body.appendChild(keywordTable(g, grp));
    }

    // —— 方案库 ——
    const lib = el("div", "col");
    lib.style.gap = "6px";
    lib.appendChild(el("div", "snote", `${g.name}方案库`));
    schemes.forEach((name) => lib.appendChild(schemeRow(g, grp, name)));
    const add = el("div", "sadd", "＋ 新建方案");
    add.addEventListener("click", () => askName(`新建${g.name}方案`, "", (n) => {
      grp.schemes[n] = {};
      strategyUI.openScheme[g.key] = n;
      renderStrategyPanel();
    }));
    lib.appendChild(add);
    body.appendChild(lib);

    return card(`sg:${g.key}`, g.name, summary, body);
  }

  /** 方案选择器。多套 = 页面上「添加人群配置」那种多组，多选字段之间取并集。 */
  function schemePicker(g, picked, onChange, blankText) {
    const grp = draftGroup(g.key);
    const names = Object.keys(grp.schemes);
    const cur = Array.isArray(picked) ? picked.slice() : (picked ? [picked] : []);

    const box = el("div", "spick");
    names.forEach((n) => {
      const on = cur.includes(n);
      const chip = el("span", "chip" + (on ? " on" : ""), n);
      chip.addEventListener("click", () => {
        const next = on ? cur.filter((x) => x !== n) : cur.concat([n]);
        // 按方案库里的顺序排，和页面上加组的先后一致
        onChange(names.filter((x) => next.includes(x)));
        renderStrategyPanel();
      });
      box.appendChild(chip);
    });
    if (!cur.length && blankText) box.appendChild(el("span", "snote", blankText));
    return box;
  }

  function keywordTable(g, grp) {
    const wrap = el("div", "col");
    wrap.style.gap = "6px";
    wrap.appendChild(el("div", "snote", "从上往下，第一条命中的生效"));
    wrap.appendChild(el("div", "snote",
      "一行可以选多套 —— 多选字段会合并到一起（并集）；单选字段几套配得不一样就合不到一起，会在核对页点出来"));
    grp.rules.forEach((r, i) => {
      const row = el("div", "krow");
      const kw = document.createElement("input");
      kw.className = "field";
      kw.value = (r.keywords || []).join("、");
      kw.placeholder = "单元名称里出现的词，用、分隔";
      kw.addEventListener("input", () => {
        r.keywords = kw.value.split(/[、,，]/).map((x) => x.trim()).filter(Boolean);
      });
      row.appendChild(kw);
      row.appendChild(el("span", "karrow", "→"));
      row.appendChild(schemePicker(g, r.schemes || (r.scheme ? [r.scheme] : []),
                                   (v) => { r.schemes = v; delete r.scheme; }, "点一下选方案"));
      const del = el("span", "kdel", "×");
      del.title = "删掉这条";
      del.addEventListener("click", () => { grp.rules.splice(i, 1); renderStrategyPanel(); });
      row.appendChild(del);
      wrap.appendChild(row);
    });

    const foot = el("div", "row");
    foot.style.gap = "12px";
    const add = el("span", "slink", "＋ 加一行");
    add.addEventListener("click", () => {
      grp.rules.push({ keywords: [], schemes: [Object.keys(grp.schemes)[0]].filter(Boolean) });
      renderStrategyPanel();
    });
    foot.appendChild(add);
    const fb = el("span", "row");
    fb.style.cssText = "gap:6px;margin-left:auto";
    fb.appendChild(el("span", "snote", "都没命中时用"));
    fb.appendChild(schemePicker(g, grp.fallback || [], (v) => { grp.fallback = v; }, "（不设兜底）"));
    foot.appendChild(fb);
    wrap.appendChild(foot);
    return wrap;
  }

  function schemeRow(g, grp, name) {
    const open = strategyUI.openScheme[g.key] === name;
    const vals = grp.schemes[name];
    const box = el("div", "srow" + (open ? " open" : ""));

    const head = el("div", "srow-head");
    head.appendChild(el("b", null, name));
    head.appendChild(el("span", "srow-sum", open ? "" : describeScheme(g, vals)));
    const act = el("span", "scard-act", open ? "收起" : "编辑");
    act.addEventListener("click", () => {
      strategyUI.openScheme[g.key] = open ? "" : name;
      renderStrategyPanel();
    });
    head.appendChild(act);
    box.appendChild(head);

    if (open) {
      const body = el("div", "srow-body");
      const fields = (state.wizardMeta.strategy_fields || []).filter((f) => f.scheme_group === g.key);
      visibleFields(fields, vals, false).forEach((f) => {
        const line = el("div", "scas");
        line.style.paddingLeft = fieldDepth(f) * 20 + "px";
        if (f.when && strategyFieldExists(f.when[0])) line.appendChild(el("span", "scas-arrow", "└"));
        const fw = strategyField(f, vals, null, () => renderStrategyPanel());
        fw.style.flex = "1";
        line.appendChild(fw);
        body.appendChild(line);
      });
      const bar = el("div", "row");
      bar.style.cssText = "gap:10px;margin-top:10px";
      const ren = el("span", "slink", "重命名");
      ren.addEventListener("click", () => askName(`重命名${g.name}方案`, name, (n) => {
        grp.schemes[n] = grp.schemes[name];
        if (n !== name) {
          delete grp.schemes[name];
          (grp.rules || []).forEach((r) => {
            r.schemes = (r.schemes || []).map((x) => (x === name ? n : x));
          });
          if (grp.scheme === name) grp.scheme = n;
          grp.fallback = (grp.fallback || []).map((x) => (x === name ? n : x));
          (draftItem().exceptions || []).forEach((e) => {
            if (e.field === g.exception_field && e.value === name) e.value = n;
          });
        }
        strategyUI.openScheme[g.key] = n;
        renderStrategyPanel();
      }));
      bar.appendChild(ren);
      const del = el("span", "slink bad", "删除这套");
      del.addEventListener("click", () => {
        if (Object.keys(grp.schemes).length <= 1) { appendLog(`至少要留一套${g.name}方案`, "warn"); return; }
        const used = (grp.rules || []).filter((r) => (r.schemes || []).includes(name)).length;
        delete grp.schemes[name];
        grp.rules = (grp.rules || [])
          .map((r) => Object.assign(r, { schemes: (r.schemes || []).filter((x) => x !== name) }))
          .filter((r) => (r.schemes || []).length);
        if (grp.scheme === name) grp.scheme = Object.keys(grp.schemes)[0];
        grp.fallback = (grp.fallback || []).filter((x) => x !== name);
        strategyUI.openScheme[g.key] = "";
        renderStrategyPanel();
        appendLog(`已删除${g.name}方案「${name}」` + (used ? `，连带 ${used} 条匹配规则` : ""), "warn");
      });
      bar.appendChild(del);
      body.appendChild(bar);
      box.appendChild(body);
    }
    return box;
  }

  /** 折叠态那行摘要：挑几个能一眼认出这套方案的字段 */
  function describeScheme(g, vals) {
    const bits = [];
    ["人群类型", "人群标签", "我想投放", "ep付费状态", "生效内容", "版本限制"].forEach((k) => {
      if (vals[k] && bits.length < 2) bits.push(vals[k]);
    });
    if (vals["人群ID"]) bits.push("人群ID " + vals["人群ID"]);
    if (!bits.length) {
      const first = (g.fields || []).find((n) => vals[n]);
      bits.push(first ? vals[first] : "未配置");
    }
    return bits.join(" · ");
  }

  // ---------------- 例外卡 ----------------
  function exceptionCard() {
    const item = draftItem();
    const list = item.exceptions;
    const summary = list.length ? `${list.length} 条` : "无，全部按通用规则";

    const body = el("div", "col");
    body.style.gap = "8px";

    list.forEach((e, i) => {
      const row = el("div", "erow");
      const head = e.positions.slice(0, 2).join("、");
      row.appendChild(el("span", "epos", head));
      if (e.positions.length > 2) row.appendChild(el("span", "pill muted", `+${e.positions.length - 2}`));
      row.appendChild(el("span", "esep", "·"));
      row.appendChild(el("span", "efield", e.field));
      row.appendChild(el("span", "esep", "="));
      row.appendChild(el("span", "eval", e.value));
      const del = el("span", "kdel", "×");
      del.addEventListener("click", () => { list.splice(i, 1); renderStrategyPanel(); });
      row.appendChild(del);
      body.appendChild(row);
    });

    if (strategyUI.adding) {
      body.appendChild(exceptionForm());
    } else {
      const add = el("div", "sadd", "＋ 加一条例外");
      add.addEventListener("click", () => {
        strategyUI.adding = { positions: [], field: "", value: "" };
        renderStrategyPanel();
      });
      body.appendChild(add);
    }
    body.appendChild(el("div", "snote",
      "push / 短信这些资源位本来就没有版本限制、生效内容这类字段，系统自动跳过，不用在这里配。"));

    return card("exc", "例外", summary, body);
  }

  function exceptionForm() {
    const draft = strategyUI.adding;
    const meta = state.wizardMeta;
    const box = el("div", "eform");
    box.appendChild(el("div", "snote", "哪些资源位（可多选）"));

    const chips = el("div", "chip-wrap");
    meta.positions.forEach((p) => {
      const on = draft.positions.includes(p.name);
      const c = el("span", "chip" + (on ? " on" : ""), p.name);
      c.addEventListener("click", () => {
        draft.positions = on ? draft.positions.filter((x) => x !== p.name)
                             : draft.positions.concat([p.name]);
        if (draft.field && !exceptionFieldOptions(draft.positions).some((f) => f.name === draft.field)) {
          draft.field = ""; draft.value = "";
        }
        renderStrategyPanel();
      });
      chips.appendChild(c);
    });
    box.appendChild(chips);

    if (draft.positions.length) {
      const opts = exceptionFieldOptions(draft.positions);
      const row = el("div", "row");
      row.style.cssText = "gap:10px;margin-top:10px;align-items:flex-start;flex-wrap:wrap";

      const fw = el("div", "sfield");
      fw.style.width = "190px";
      fw.appendChild(el("div", "sname", "改哪个字段"));
      const sel = el("select", "field");
      const blank = el("option", null, "选一个…");
      blank.value = "";
      sel.appendChild(blank);
      opts.forEach((f) => {
        const o = el("option", null, f.name);
        o.value = f.name;
        if (f.name === draft.field) o.selected = true;
        sel.appendChild(o);
      });
      sel.addEventListener("change", () => { draft.field = sel.value; draft.value = ""; renderStrategyPanel(); });
      fw.appendChild(sel);
      row.appendChild(fw);

      if (draft.field) {
        const vw = el("div", "sfield");
        vw.style.cssText = "flex:1;min-width:220px";
        vw.appendChild(el("div", "sname", "改成什么"));
        const swap = schemeGroups().find((g) => g.exception_field === draft.field);
        if (swap) {
          // 例外里换整组只指一套（push 那种「只吃 DMP 包」的场景），要多套就用关键词规则
          vw.appendChild(schemePicker(swap, draft.value ? [draft.value] : [],
                                      (v) => { draft.value = v[0] || ""; }, "点一下选一套"));
          if (!draft.value) draft.value = Object.keys(draftGroup(swap.key).schemes)[0] || "";
        } else {
          const def = opts.find((f) => f.name === draft.field);
          vw.appendChild(strategyField(Object.assign({}, def, { name: "" }), draft, null,
                                       null, "value"));
        }
        row.appendChild(vw);
      }
      box.appendChild(row);

      const bar = el("div", "row");
      bar.style.cssText = "gap:8px;margin-top:12px";
      const ok = el("button", "btn btn-sm btn-primary", "加上");
      ok.addEventListener("click", () => {
        if (!draft.field || !String(draft.value).trim()) { appendLog("例外要选字段、填值", "warn"); return; }
        const item = draftItem();
        item.exceptions = item.exceptions.filter(
          (e) => !(e.field === draft.field && e.positions.join() === draft.positions.join()));
        item.exceptions.push({ positions: draft.positions.slice(), field: draft.field, value: String(draft.value) });
        strategyUI.adding = null;
        renderStrategyPanel();
      });
      bar.appendChild(ok);
      const cancel = el("button", "btn btn-sm", "取消");
      cancel.addEventListener("click", () => { strategyUI.adding = null; renderStrategyPanel(); });
      bar.appendChild(cancel);
      box.appendChild(bar);
    }
    return box;
  }

  /** 例外能改哪些字段：选中的资源位共同拥有的那些 + 每组「换整套方案」 */
  function exceptionFieldOptions(positions) {
    const meta = state.wizardMeta;
    const mine = meta.positions.filter((p) => positions.includes(p.name));
    const out = (meta.strategy_fields || []).filter(
      (f) => !f.scheme_group && mine.every((p) => (p.strategy_fields || []).includes(f.name)));
    // 方案组的单个字段不进例外 —— 换就整套换，不然一个资源位里半套新半套旧，没人看得懂
    schemeGroups().forEach((g) => {
      out.push({ name: g.exception_field, kind: "single", options: [], scheme_group: g.key });
    });
    return out;
  }

  // ---------------- 主渲染 ----------------
  function renderStrategyPanel() {
    const d = strategyUI.draft;
    if (!d || !state.wizardMeta) return;

    const sel = $("#strategyPickSelect");
    sel.innerHTML = "";
    Object.keys(d.items).forEach((name) => {
      const o = el("option", null, name);
      o.value = name;
      if (name === d.active) o.selected = true;
      sel.appendChild(o);
    });

    const wrap = $("#strategyCards");
    wrap.innerHTML = "";
    (state.wizardMeta.groups || []).forEach((g) => wrap.appendChild(ruleCard(g)));
    schemeGroups().forEach((g) => wrap.appendChild(schemeCard(g)));
    wrap.appendChild(exceptionCard());

    const item = draftItem();
    const need = (state.wizardMeta.strategy_fields || [])
      .filter((f) => !f.scheme_group && f.required && fieldInScope(f) && fieldRevealed(f, item.rules));
    const miss = need.filter((f) => !String(item.rules[f.name] || "").trim());
    const hint = $("#strategyFootHint");
    hint.textContent = miss.length
      ? `还差 ${miss.length} 项必填：${miss.map((f) => f.name).join("、")}`
      : `「${d.active}」必填项齐了` + (strategyDirty() ? "，有改动没保存" : "");
    hint.style.color = miss.length ? "var(--bad)" : "var(--mu)";
    $("#strategyScopeCheck").checked = strategyUI.scopeToSelection;
    renderStrategyRow();
  }

  /** 一个策略字段的编辑控件。values[key] 是它写回的地方。 */
  function strategyField(f, values, fallback, onChange, key) {
    key = key || f.name;
    const box = el("div", "sfield");
    if (f.name) {
      const name = el("div", "sname", f.name);
      if (f.required && !fallback) name.appendChild(el("i", null, "*"));
      box.appendChild(name);
    }
    const cur = String(values[key] || "");
    const fire = () => { if (onChange) onChange(); };

    if (f.kind === "ordered_multi") {
      // 按点选顺序的多选：**顺序本身有意义**（= 页面上从左到右怎么摆），
      // 所以不能用勾选框（勾选框只说得出「选了哪些」）。
      // 互斥：同一个 SKU 只能落在一个面板里，别的面板选过的这里就不出现。
      const picked = cur.split(",").map((x) => x.trim()).filter(Boolean);
      const taken = new Set();
      (f.exclusive_with || []).forEach((other) => {
        String(values[other] || "").split(",").map((x) => x.trim())
          .filter(Boolean).forEach((x) => taken.add(x));
      });
      const write = (arr) => {
        values[key] = arr.join(",");
        if (key === f.name) clearDescendants(values, f.name);
        fire();
      };

      const chosen = el("div", "spicked");
      if (!picked.length) chosen.appendChild(el("span", "spick-empty", "还没选 —— 一个都不选就是这一段不要"));
      const badgeOn = tieHasBadge(values);
      picked.forEach((o, i) => {
        const chip = el("div", "spick");
        chip.appendChild(el("i", "spick-no", String(i + 1)));
        chip.appendChild(el("span", null, o));
        if (badgeOn) chip.appendChild(tieBadge(o, values, fire));
        const x = el("b", "spick-x", "×");
        x.title = "移出这一段";
        x.addEventListener("click", () => {
          dropTie(values, o);
          write(picked.filter((v) => v !== o));
        });
        chip.appendChild(x);
        chosen.appendChild(chip);
      });
      box.appendChild(chosen);

      const pool = el("div", "spool");
      const rest = (f.options || []).filter((o) => !picked.includes(o) && !taken.has(o));
      if (!rest.length) chosen.appendChild(el("span", "spick-empty", "（没有别的可选了）"));
      rest.forEach((o) => {
        const b = el("div", "spool-item", o);
        b.title = "点一下加到末尾";
        b.addEventListener("click", () => write(picked.concat([o])));
        pool.appendChild(b);
      });
      box.appendChild(pool);
    } else if (f.kind === "file") {
      const row = el("div", "row");
      row.style.cssText = "gap:6px;align-items:center";
      const inp = document.createElement("input");
      inp.className = "field";
      inp.value = cur;
      inp.placeholder = "选一个 Excel，或直接粘路径";
      inp.addEventListener("input", () => { values[key] = inp.value.trim(); });
      const btn = el("button", "btn btn-sm", "浏览…");
      btn.addEventListener("click", () => {
        callApi("pick_file").then((path) => {
          if (!path) return;
          values[key] = path;
          fire();
        });
      });
      row.appendChild(inp);
      row.appendChild(btn);
      box.appendChild(row);
    } else if (f.kind === "multi") {
      const picked = cur.split(",").map((s) => s.trim()).filter(Boolean);
      const opts = el("div", "sopts");
      (f.options || []).forEach((o) => {
        const lb = el("label");
        const cb = document.createElement("input");
        cb.type = "checkbox";
        cb.value = o;
        cb.checked = picked.includes(o);
        cb.addEventListener("change", () => {
          values[key] = Array.from(opts.querySelectorAll("input:checked")).map((x) => x.value).join(",");
          if (key === f.name && hasDescendants(f.name)) {
            clearDescendants(values, f.name);
            fire();
          }
        });
        lb.appendChild(cb);
        lb.appendChild(el("span", null, o));
        opts.appendChild(lb);
      });
      box.appendChild(opts);
    } else if (f.kind === "range") {
      // 后台就是「n 天至 m 天(从小到大)」两个数字框，这里对齐，别让人手写「1-365」
      const parts = cur.split("-");
      const row = el("div", "srange");
      const mk = (i, ph) => {
        const n = document.createElement("input");
        n.type = "number";
        n.className = "field";
        n.value = (parts[i] || "").trim();
        n.placeholder = ph;
        n.addEventListener("input", () => {
          const a = row.querySelectorAll("input")[0].value.trim();
          const b = row.querySelectorAll("input")[1].value.trim();
          values[key] = (a || b) ? `${a}-${b}` : "";
        });
        return n;
      };
      row.appendChild(mk(0, "小"));
      row.appendChild(el("span", "sunit", "天至"));
      row.appendChild(mk(1, "大"));
      row.appendChild(el("span", "sunit", "天（从小到大，填 -1 = 不限）"));
      box.appendChild(row);
    } else if (f.kind === "single") {
      const sel = el("select", "field");
      const blank = el("option", null, "（不填）");
      blank.value = "";
      sel.appendChild(blank);
      (f.options || []).forEach((o) => {
        const op = el("option", null, o);
        op.value = o;
        if (o === cur) op.selected = true;
        sel.appendChild(op);
      });
      sel.addEventListener("change", () => {
        values[key] = sel.value;
        if (key === f.name) clearDescendants(values, f.name);
        fire();
      });
      box.appendChild(sel);
    } else {
      const inp = document.createElement("input");
      inp.className = "field";
      inp.value = cur;
      inp.placeholder = f.note && f.note.length < 20 ? f.note : "";
      inp.addEventListener("input", () => { values[key] = inp.value.trim(); });
      box.appendChild(inp);
    }
    return box;
  }

  function initStrategyPanel() {
    $("#btnStrategyReset").addEventListener("click", () => {
      strategyUI.draft = JSON.parse(JSON.stringify(state.strategyDoc));
      strategyUI.adding = null;
      renderStrategyPanel();
      appendLog("策略改动已撤销，回到上次保存的样子", "warn");
    });
    $("#strategyScopeCheck").addEventListener("change", (e) => {
      strategyUI.scopeToSelection = e.target.checked;
      renderStrategyPanel();
    });
    $("#strategyPickSelect").addEventListener("change", (e) => {
      strategyUI.draft.active = e.target.value;
      strategyUI.adding = null;
      renderStrategyPanel();
    });
    $("#btnStrategyNew").addEventListener("click", () => askName("新建一套策略", "", (name) => {
      strategyUI.draft.items[name] = JSON.parse(JSON.stringify(draftItem()));
      strategyUI.draft.items[name].updated_at = "";
      strategyUI.draft.active = name;
      renderStrategyPanel();
      appendLog(`新策略「${name}」是从当前这套复制出来的，改完记得保存`, "ok");
    }));
    $("#btnStrategyRename").addEventListener("click", () => {
      const old = strategyUI.draft.active;
      askName("重命名当前策略", old, (name) => {
        strategyUI.draft.items[name] = strategyUI.draft.items[old];
        if (name !== old) delete strategyUI.draft.items[old];
        strategyUI.draft.active = name;
        renderStrategyPanel();
      });
    });
    $("#btnStrategyDelete").addEventListener("click", () => {
      const d = strategyUI.draft;
      if (Object.keys(d.items).length <= 1) { appendLog("至少要留一套策略", "warn"); return; }
      const gone = d.active;
      delete d.items[gone];
      d.active = Object.keys(d.items)[0];
      renderStrategyPanel();
      appendLog(`已删除策略「${gone}」，保存后生效`, "warn");
    });
    $("#btnStrategySave").addEventListener("click", () => {
      callApi("strategy_save", state.activeForm, strategyUI.draft).then((res) => {
        if (!res || !res.ok) {
          appendLog("策略保存失败：" + (res ? res.error : "无法连接后端"), "error");
          return;
        }
        state.strategyDoc = res.doc;
        strategyUI.draft = JSON.parse(JSON.stringify(res.doc));
        appendLog("策略「" + res.doc.active + "」已保存：" + res.path, "ok");
        renderStrategyPanel();
      });
    });
  }

  /** 小输入弹窗。pywebview 里 window.prompt() 不一定可用，用自己的弹窗代替。 */
  function askName(title, initial, onOk) {
    strategyUI.pendingName = initial;
    showModal({
      title: title,
      desc: "起个一眼能认出来的名字",
      extraHtml: '<div style="padding:12px"><input id="askNameInput" class="field" style="width:100%" value="' +
                 escapeHtml(initial) + '" placeholder="名称"></div>',
      buttons: [
        {
          label: "确定", primary: true, onClick: () => {
            const v = String(strategyUI.pendingName || "").trim();
            if (!v) { appendLog("名字不能为空", "warn"); return; }
            onOk(v);
          },
        },
        { label: "取消" },
      ],
    });
    const inp = $("#askNameInput");
    inp.addEventListener("input", () => { strategyUI.pendingName = inp.value; });
    inp.focus();
  }

  function initLogDrawer() {
    $("#logHead").addEventListener("click", () => setLogOpen(!state.logOpen));
    $("#btnFeedbackLog").addEventListener("click", (e) => { e.stopPropagation(); openFeedback("issue"); });
    $("#btnFeedback").addEventListener("click", () => openFeedback("idea"));
    $("#btnDetailClose").addEventListener("click", () => $("#detailModal").classList.add("hidden"));
  }

  // ---------------- 暴露给 Python 端 push 的接口 ----------------
  window.app = {
    onLog: appendLog,
    setBrowserStatus: setBrowserStatus,

    onProgress(done, total, stats) {
      const pct = total ? Math.round((done / total) * 100) : 0;
      $("#runProgressBar").style.width = pct + "%";
      const extra = stats.dry ? ` · 空跑 ${stats.dry}` : "";
      $("#runStat").textContent = `${done}/${total}　成功 ${stats.ok} · 失败 ${stats.failed} · 跳过 ${stats.skipped}${extra}`;
    },

    onConfirm(label, summary) {
      showModal({
        title: `${label}　${summary}`,
        desc: "已在浏览器里填好，请切到 Chrome 核对内容后选择",
        buttons: [
          { label: "提交这条", primary: true, onClick: () => callApi("answer", "submit") },
          { label: "跳过", onClick: () => callApi("answer", "skip") },
          { label: "以后全部自动", onClick: () => callApi("answer", "auto") },
          { label: "停止", onClick: () => callApi("answer", "stop") },
        ],
      });
    },

    onAskContinue(error) {
      showModal({
        title: "这条失败了",
        desc: `${error}\n\n继续跑下一条吗？`,
        buttons: [
          { label: "继续", primary: true, onClick: () => callApi("answer", true) },
          { label: "停止", onClick: () => callApi("answer", false) },
        ],
      });
    },

    onFinished(title, body, ok) {
      appendLog(`${title}：${String(body).replace(/\n+/g, " ")}`, ok ? "ok" : "error");
      showModal({ title, desc: body, buttons: [
        { label: "打开结果目录", onClick: () => callApi("open_output_dir") },
        { label: "知道了", primary: true },
      ] });
    },

    onRunDone(summary) {
      state.usage = null;      // 跑完有新数据了，统计缓存作废
      state.running = false;
      setRunButtons(false);
      if (!summary) return;
      if (summary.misaligned) {
        appendLog("这次跑的范围和结果对不上号（常见原因：没勾「跳过已成功的」），失败清单这次没法逐行列出，请看运行日志里的报错", "warn");
        $("#failedSection").classList.add("hidden");
      } else {
        renderFailedList(summary.failed);
      }
    },
  };

  // ---------------- 启动 ----------------
  function init() {
    initTheme();
    renderVersion();
    initStepNav();
    initModeSegmented();
    initPrepareActions();
    initWizardTabs();
    initWizardActions();
    initExecuteActions();
    initLaunchBrowser();
    initSidebarFooterActions();
    initLogDrawer();
    initFlowCard();

    // 等界面和首屏都开始渲染后再检查，网络波动不影响程序启动速度。
    setTimeout(() => checkForUpdate(false), 800);

    $("#navHome").addEventListener("click", showHome);
    $("#navStats").addEventListener("click", showStats);
    $(".sidebar-brand").addEventListener("click", showHome);

    callApi("list_forms").then((forms) => {
      state.forms = forms || [];
      renderSidebar();
      // 先把第一个配置类型的状态铺好（按归类后的次序，不是文件名次序），
      // 再落到首页 —— 打开程序先看到「干了多少活」，而不是一上来就催你选文件
      const groups = groupedForms();
      if (groups.length && groups[0].items.length) selectForm(groups[0].items[0].name, { expand: false });
      showHome();
      appendLog(`已加载 ${state.forms.length} 个配置类型`, "ok");
    });

    pollBrowserStatus();
    setInterval(pollBrowserStatus, 3000);
  }

  // ⚠ 必须等 pywebview 把 api 挂上来再 init。
  //   window.pywebview 是异步注入的，DOMContentLoaded 那一刻它经常还不存在 ——
  //   这时 callApi 会走「没有后端」那条样子货分支，**首页会显示出假数字**
  //   （踩过：用户在 exe 里看到 143 条 / 3 个人，全是 STUB 里编的）。
  //   普通浏览器里没有 pywebviewready 这个事件，等 3 秒就按「没后端」走样式预览。
  function whenBackendReady(fn) {
    if (window.pywebview && window.pywebview.api) return fn();
    let fired = false;
    const go = () => { if (!fired) { fired = true; fn(); } };
    window.addEventListener("pywebviewready", go, { once: true });
    setTimeout(go, 3000);
  }

  document.addEventListener("DOMContentLoaded", () => whenBackendReady(init));
})();
