/* Local task history. Values are rendered as text, never executable markup. */
(() => {
  const dialog = document.createElement("dialog");
  dialog.style.cssText = "width:min(1100px,94vw);max-height:85vh;overflow:auto;border:1px solid #ccc;border-radius:12px;padding:20px";
  const bar = document.createElement("div");
  const title = document.createElement("h2");
  title.textContent = "任务与完整档案";
  const note = document.createElement("p");
  note.textContent = "执行结果与归集进度分别记录。价格任务由本机宿主持有；旧功能仍需保持助手打开。导入档案只用于查阅。";
  const message = document.createElement("p");
  const list = document.createElement("div");
  const details = document.createElement("pre");
  const readable = document.createElement("div");
  const raw = document.createElement("details");
  const rawTitle = document.createElement("summary");
  rawTitle.textContent = "技术数据（排查或核对完整内容）";
  raw.append(rawTitle, details);
  details.style.cssText = "white-space:pre-wrap;overflow-wrap:anywhere;max-height:45vh;overflow:auto;background:#f4f5f7;padding:12px";
  dialog.append(title, note, bar, message, list, readable, raw);
  document.body.append(dialog);
  async function api(method, ...args) {
    if (!window.pywebview?.api?.[method]) throw Error("任务档案需要桌面助手后端");
    const result = await window.pywebview.api[method](...args);
    if (result?.ok === false) throw Error(result.error);
    return result;
  }
  function button(parent, label, fn) {
    const b = document.createElement("button");
    b.textContent = label;
    b.style.cssText = "margin:4px;padding:6px 10px;cursor:pointer";
    b.onclick = async () => {
      b.disabled = true;
      try { await fn(); } catch (e) { message.textContent = e.message; }
      finally { b.disabled = false; }
    };
    parent.append(b);
  }
  const names = {queued:"已排队", running:"正在执行", awaiting_confirmation:"等待确认",
    completed:"本次执行结束", blocked:"前置条件变化", unknown:"结果待核查",
    cancelled:"已停止后续操作", interrupted:"宿主中断", reconciled:"已完成回读核验",
    failed:"任务失败", verified_success:"已回读确认", reported_success:"旧执行器报告成功",
    skipped:"已跳过", submitted:"已记录提交意图", accepted:"接口已接收，待完整核验",
    verified_complete:"完整归集已核验", retry:"待补传", rejected:"平台拒收", pending:"等待归集"};
  function text(parent, tag, value) {
    const node = document.createElement(tag); node.textContent = value; parent.append(node); return node;
  }
  function showDetail(data) {
    readable.replaceChildren();
    text(readable, "h3", "本次任务");
    text(readable, "p", "执行：" + (names[data.task?.status] || data.task?.status || "历史档案"));
    const plan = data.plan || {};
    if (plan.context) text(readable, "p", "环境：" + plan.context.environment + "；身份依据：" + plan.context.identity_label);
    const results = new Map((data.items || []).map(x => [x.item_id, x]));
    for (const item of plan.items || []) {
      const row = text(readable, "p", item.name + "　" + item.before + " → " + item.after +
        "　" + (names[results.get(item.item_id)?.status] || item.issue || "尚未执行"));
      row.style.cssText = "border-bottom:1px solid #ddd;padding:8px 0;overflow-wrap:anywhere";
    }
    if (Array.isArray(plan.inputs)) text(readable, "p", "已保存 " + plan.inputs.length + " 条原始配置，完整内容见下方技术数据。");
    const states = [...new Set((data.collection || []).map(x => names[x.status] || x.status))];
    text(readable, "p", "归集：" + (states.join("；") || "已在本机保存，尚无完整收集回执"));
    if (data.task?.limitations) text(readable, "p", data.task.limitations.join("；"));
    details.textContent = JSON.stringify(data, null, 2);
  }
  async function refresh() {
    const data = await api("task_history");
    list.replaceChildren();
    message.textContent = "本机档案位置：" + data.root + "；" + (data.collection?.message || "完整归集状态以核验回执为准");
    if (!data.tasks.length) list.textContent = "尚无任务，完成一次预检并执行后会显示在这里。";
    for (const task of data.tasks) {
      const row = document.createElement("div");
      row.style.cssText = "border-bottom:1px solid #ddd;padding:10px 0";
      const label = document.createElement("div");
      label.textContent = task.created_at + "　" + (names[task.status] || task.status) + "　" + task.task_id;
      row.append(label);
      button(row, "查看计划、记录与结果", async () => {
        showDetail(await api("task_detail", task.task_id));
      });
      button(row, "导出档案", async () => {
        const result = await api("task_export", task.task_id);
        message.textContent = result.path ? "已导出：" + result.path : "已取消";
      });
      if (task.approval) {
        if (["queued", "running", "awaiting_confirmation"].includes(task.status)) {
          button(row, "连接执行进度", async () => {
            await api("task_attach", task.task_id); dialog.close();
          });
          button(row, "停止后续操作", async () => {
            await api("task_cancel", task.task_id); message.textContent = "已请求停止；已完成的操作不会撤销";
          });
        }
        if (["unknown", "interrupted"].includes(task.status)) {
          button(row, "只读核验", async () => {
            details.textContent = JSON.stringify(await api("task_reconcile", task.task_id), null, 2);
            await refresh();
          });
        }
      }
      list.append(row);
    }
  }
  button(bar, "刷新", refresh);
  button(bar, "补传", async () => {
    const result = await api("task_sync"); message.textContent = JSON.stringify(result);
  });
  button(bar, "导入档案", async () => {
    details.textContent = JSON.stringify(await api("task_import"), null, 2);
  });
  button(bar, "查看已导入档案", async () => {
    details.textContent = JSON.stringify(await api("task_imported"), null, 2);
  });
  button(bar, "关闭", () => dialog.close());
  document.getElementById("btnTaskHistory").onclick = async () => {
    dialog.showModal();
    try { await refresh(); } catch (e) { message.textContent = e.message; }
  };
})();
