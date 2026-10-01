/* 商场扶梯启停巡检与封控复开平台 —— 前端逻辑（原生 JS） */
"use strict";

let S = null;                 // /api/state 全量快照
let me = "op1";
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
const esc = id => S?.escalators.find(e => e.id === id);
const person = id => S?.people.find(p => p.id === id);
const pname = id => person(id)?.name || id;
const roleOf = id => person(id)?.role;
const escName = id => esc(id)?.name || id;

const REASON_TXT = { fault: "故障封控", crowd: "客流封控", estop: "急停失败封控" };
const TICKET_TXT = {
  reported: "待派单", dispatched: "已派单", arrived: "已到场", repairing: "返修中",
  awaiting_review: "待复检", approved: "复检通过", rejected: "审批退回", closed: "已结案",
};

// ---------------------------------------------------------------- API
async function api(path, body) {
  const res = await fetch(path, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: body ? JSON.stringify(body) : "{}",
  });
  const data = await res.json();
  if (!data.ok) {
    toast(data.error || "操作失败", false);
    throw new Error(data.code);
  }
  return data;
}
async function uploadPhoto(file) {
  const buf = await file.arrayBuffer();
  const res = await fetch("/api/photo", {
    method: "POST", headers: { "X-Filename": file.name }, body: buf,
  });
  const data = await res.json();
  if (!data.ok) throw new Error(data.error);
  return data.filename;
}
async function refresh() {
  const res = await fetch("/api/state");
  S = await res.json();
  renderAll();
}
function toast(msg, ok = true) {
  const t = $("#toast");
  t.textContent = msg;
  t.className = "toast " + (ok ? "ok" : "err");
  t.hidden = false;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => (t.hidden = true), 2600);
}
const h = (tag, cls, html) => {
  const el = document.createElement(tag);
  if (cls) el.className = cls;
  if (html !== undefined) el.innerHTML = html;
  return el;
};

// ---------------------------------------------------------------- 身份 & 标签
function initIdentity() {
  const sel = $("#identity");
  S.people.forEach(p => {
    const o = h("option", null, `${p.role_name} · ${p.name}`);
    o.value = p.id;
    sel.appendChild(o);
  });
  sel.value = me;
  sel.addEventListener("change", () => { me = sel.value; renderAll(); });
}
$$("#tabs button").forEach(b => b.addEventListener("click", () => {
  $$("#tabs button").forEach(x => x.classList.toggle("active", x === b));
  $$(".tab").forEach(x => x.classList.toggle("active", x.id === "tab-" + b.dataset.tab));
}));

// ================================================================ 楼层设备图
function renderMap() {
  const root = $("#tab-map");
  root.innerHTML = "";
  const tip = h("div", "small-note",
    "↑ / ↓ 直接标注扶梯运行方向：绿色为运行方向，灰色为停梯时的计划方向；" +
    "<b style='color:var(--red)'>斜纹边界</b>表示停梯围挡（非整块红色）。点击设备可查看详情与操作。");
  root.appendChild(tip);
  const floors = [...S.floors].reverse(); // F4 在上，F1 在下
  floors.forEach((f, idx) => {
    const band = h("div", "floor-band");
    const label = h("div", "floor-label", `${f}<small>${floorTitle(f)}</small>`);
    const lane = h("div", "floor-lane");
    // 以该层为高端的设备（设备主体位于本层入口）
    S.escalators.filter(e => e.high_floor === f).forEach(e => lane.appendChild(escNode(e)));
    if (!lane.children.length) lane.appendChild(h("span", "muted", "本层无上行接入设备"));
    band.append(label, lane);
    root.appendChild(band);
    if (idx < floors.length - 1) {
      const lower = floors[idx + 1];
      const gap = h("div", "segment-gap");
      gap.appendChild(h("span", "segment-tag", `${lower} ⇄ ${f}`));
      root.appendChild(gap);
    }
  });
}
function floorTitle(f) {
  return { F1: "一层 美妆珠宝", F2: "二层 女装", F3: "三层 男装运动", F4: "四层 餐饮" }[f] || "";
}
function escNode(e) {
  const node = h("div", `esc-node ${e.state}`);
  const arrowDir = e.state === "running" ? e.direction : e.planned_direction;
  const arrow = h("div", `arrow ${arrowDir}`);
  const meta = h("div", "meta",
    `<div class="name">${e.name}</div>` +
    `<div class="sub">${e.low_floor}→${e.high_floor} · ${stateText(e)}</div>` +
    `<div class="sub">${e.state === "running"
      ? `运行方向 ${arrowDir === "up" ? "上行 ↑" : "下行 ↓"}`
      : `计划方向 ${e.planned_direction === "up" ? "上行 ↑" : "下行 ↓"}`}</div>`);
  node.append(arrow, meta);
  if (e.state === "locked") {
    node.appendChild(h("span", "barrier-tag",
      e.barrier_erected ? "围挡中" : "围挡待设"));
  }
  node.addEventListener("click", () => openEscModal(e.id));
  return node;
}
function stateText(e) {
  if (e.state === "running") return `<span class="badge running"><span class="dot running"></span>运行</span>`;
  if (e.state === "locked") return `<span class="badge locked"><span class="dot locked"></span>${REASON_TXT[e.lock_reason] || "封控"}</span>`;
  return `<span class="badge stopped"><span class="dot stopped"></span>停梯</span>`;
}

// ---------------------------------------------------------------- 设备详情弹窗
function openEscModal(escId) {
  const e = esc(escId);
  const m = $("#modal");
  m.innerHTML = "";
  m.appendChild(h("h3", null, `${e.name}（${e.id}）`));
  const kv = h("dl", "kv");
  const rows = [
    ["位置", `${e.low_floor} ⇄ ${e.high_floor}`],
    ["状态", TICKET_TXT ? "" : ""],
    ["计划方向", e.planned_direction === "up" ? "上行 ↑" : "下行 ↓"],
    ["运行方向", e.direction ? (e.direction === "up" ? "上行 ↑" : "下行 ↓") : "—"],
    ["版本号", `v${e.version}`],
    ["今日跳停", `${e.stop_count} 次`],
    ["客流密度", `${Math.round(e.density * 100)}%`],
    ["控制柜", cabinetText(e.cabinet_state)],
  ];
  rows.forEach(([k, v]) => {
    kv.appendChild(h("dt", null, k));
    kv.appendChild(h("dd", null, k === "状态" ? stateText(e) : v));
  });
  m.appendChild(kv);

  const actions = h("div", "card");
  actions.style.marginTop = "14px";
  actions.appendChild(h("div", "card-title", "操作"));
  const btnRow = h("div", "row");

  if (e.state === "stopped") {
    btnRow.appendChild(actionBtn("启动开梯", "primary", () => doStart(e.id, true)));
    btnRow.appendChild(actionBtn("改方向(主管)", "", () => askDirection(e.id)));
    btnRow.appendChild(actionBtn("急停测试", "", () => doEstop(e.id)));
    btnRow.appendChild(actionBtn("故障报修", "ghost-danger", () => openReportModal(e.id)));
  }
  if (e.state === "running") {
    btnRow.appendChild(actionBtn("停梯", "danger", () => doStop(e.id)));
    btnRow.appendChild(actionBtn("故障报修", "ghost-danger", () => openReportModal(e.id)));
  }
  if (e.state === "locked") {
    if (e.barrier_required && !e.barrier_erected)
      btnRow.appendChild(actionBtn("搭设围挡", "danger", () => api("/api/barrier/erect", { esc_id: e.id, user: me }).then(refresh)));
    if (e.barrier_erected && !e.barrier_removed)
      btnRow.appendChild(actionBtn("撤除围挡", "", () => api("/api/barrier/remove", { esc_id: e.id, user: me }).then(refresh)));
    btnRow.appendChild(actionBtn("查看工单/复检", "primary", () => { closeModal(); switchTab("tickets"); }));
  }
  actions.appendChild(btnRow);
  m.appendChild(actions);

  // 模拟注入（演示）
  const sim = h("div", "card");
  sim.appendChild(h("div", "card-title", "模拟适配器注入"));
  const simRow = h("div", "row");
  simRow.appendChild(actionBtn("注入跳停", "ghost-danger", () =>
    api("/api/sim/trip", { esc_id: e.id, code: "CHAIN_FAULT" }).then(refresh)));
  simRow.appendChild(actionBtn("急停回路置故障", "ghost-danger", () =>
    api("/api/sim/estop-fault", { esc_id: e.id, faulty: true }).then(() => toast("已置为急停回路故障"))));
  simRow.appendChild(actionBtn("急停回路恢复", "", () =>
    api("/api/sim/estop-fault", { esc_id: e.id, faulty: false }).then(() => toast("急停回路恢复正常"))));
  sim.appendChild(simRow);
  const drow = h("div", "row");
  drow.style.marginTop = "10px";
  drow.appendChild(h("span", "muted", "客流密度"));
  const range = h("input");
  range.type = "range"; range.min = 0; range.max = 100; range.value = Math.round(e.density * 100);
  const val = h("span", "chip", range.value + "%");
  range.addEventListener("input", () => (val.textContent = range.value + "%"));
  range.addEventListener("change", () =>
    api("/api/sim/crowd", { esc_id: e.id, density: range.value / 100 }).then(refresh));
  drow.append(range, val);
  sim.appendChild(drow);
  m.appendChild(sim);

  const close = h("div", "modal-actions");
  close.appendChild(h("button", "btn", "关闭"));
  close.lastChild.addEventListener("click", closeModal);
  m.appendChild(close);
  $("#modalMask").hidden = false;
}
function cabinetText(s) {
  return { stopped: "停梯待机", running: "运行中", tripped: "跳停未复位" }[s] || s;
}
function actionBtn(text, cls, fn) {
  const b = h("button", "btn " + cls, text);
  b.addEventListener("click", () => { try { fn(); } catch (_) {} });
  return b;
}
function closeModal() { $("#modalMask").hidden = true; }
$("#modalMask").addEventListener("click", ev => { if (ev.target.id === "modalMask") closeModal(); });
function switchTab(name) {
  $(`#tabs button[data-tab="${name}"]`).click();
}
async function askDirection(escId) {
  const d = prompt("输入新的运行方向：up（上行）或 down（下行）", esc(escId).planned_direction);
  if (!d) return;
  await api("/api/direction", { esc_id: escId, user: me, direction: d.trim() });
  toast("方向设定已更新（版本号已变更）"); refresh();
}
async function doStart(escId, fromModal) {
  const e = esc(escId);
  try {
    await api("/api/start", { esc_id: escId, user: me, version: e.version });
    toast("启动成功"); refresh();
    if (fromModal) closeModal();
  } catch (_) {}
}
async function doStop(escId) {
  await api("/api/stop", { esc_id: escId, user: me, reason: "值班员手动停梯" });
  toast("已停梯"); refresh();
}
async function doEstop(escId) {
  try {
    const r = await api("/api/estop-test", { esc_id: escId, user: me });
    toast(r.ok === false ? "急停测试失败，已封控报修" : "急停测试合格");
  } catch (_) {}
  refresh();
}

// ================================================================ 开梯清单
const checklistSel = { esc: "EA", tri: {} };
function renderChecklist() {
  const root = $("#tab-checklist");
  root.innerHTML = "";
  root.appendChild(h("div", "small-note",
    "每日开梯前必须逐项检查 <b>梯级、梳齿板、扶手带、照明、急停装置</b>，" +
    "每项均有明确的 <b style='color:var(--green)'>通过</b> / " +
    "<b style='color:var(--red)'>失败</b> / <b style='color:var(--silver-700)'>不适用</b> 选择；" +
    "全部合格且急停测试通过才允许启动。"));
  const filters = h("div", "filters");
  const sel = h("select");
  S.escalators.forEach(e => sel.appendChild(h("option", null, `${e.id} · ${e.name}`)));
  sel.value = checklistSel.esc;
  sel.addEventListener("change", () => { checklistSel.esc = sel.value; renderChecklist(); });
  filters.appendChild(sel);
  const gotoMap = h("button", "btn small", "楼层图查看");
  gotoMap.addEventListener("click", () => switchTab("map"));
  filters.append(h("span", "spacer"), gotoMap);
  root.appendChild(filters);

  const e = esc(checklistSel.esc);
  const chk = e.check;
  const card = h("div", "card");
  card.appendChild(h("div", "card-title", `${e.name} · 班前检查（${chk.date}）`));
  S.check_items.forEach(ci => {
    const cur = chk.items.find(i => i.key === ci.key);
    const row = h("div", "check-row");
    row.appendChild(h("div", "check-name", ci.name));
    const wrap = h("div", "check-status");
    const tri = h("div", "tri " + (cur?.result || ""));
    [["pass", "通过"], ["fail", "失败"], ["na", "不适用"]].forEach(([v, label]) => {
      const b = h("button", cur?.result === v ? "sel-" + v : "", label);
      b.addEventListener("click", async () => {
        await api("/api/check", { esc_id: e.id, user: me, item: ci.key, result: v });
        refresh();
      });
      tri.appendChild(b);
    });
    wrap.appendChild(tri);
    row.appendChild(wrap);
    row.appendChild(h("div", "check-note",
      cur ? `${cur.result === "pass" ? "✓ 合格" : cur.result === "fail" ? "✗ 不合格" : "— 不适用"} · ${pname(cur.inspector)} · ${cur.ts?.slice(11)}`
          : "<span style='color:var(--red)'>待检查（漏项将阻止启动）</span>"));
    card.appendChild(row);
  });

  // 急停测试独立按钮
  const estopRow = h("div", "check-row");
  estopRow.append(
    h("div", "check-name", "急停功能测试"),
    (() => {
      const w = h("div", "check-status");
      const b = h("button", "btn small " + (chk.estop_ok ? "" : "danger"),
        chk.estop_ok ? "重新测试" : "执行急停测试");
      b.addEventListener("click", () => doEstop(e.id));
      w.appendChild(b); return w;
    })(),
    h("div", "check-note", chk.estop_ok
      ? "<span style='color:var(--green)'>✓ 今日急停回路测试合格</span>"
      : "<span style='color:var(--red)'>未通过/未测试：测试失败将立即封控报修</span>"));
  card.appendChild(estopRow);

  // 就绪结论 + 启动
  const sum = h("div", "row");
  sum.style.marginTop = "14px";
  if (chk.ready) {
    sum.appendChild(h("span", "chip green", "✓ 检查全部合格，具备开梯条件"));
  } else {
    const miss = [...chk.missing.map(k => S.check_items.find(i => i.key === k).name),
                  ...chk.failed.map(k => S.check_items.find(i => i.key === k).name + "(不合格)")];
    if (!chk.estop_ok) miss.push("急停测试未通过");
    sum.appendChild(h("span", "chip red", "✗ 禁止启动：" + (miss.join("、") || "条件未满足")));
  }
  sum.appendChild(h("span", "spacer"));
  sum.appendChild(h("span", "muted", `当前版本 v${e.version} · 启动命令须携带此版本`));
  const startBtn = h("button", "btn primary", "按当前版本启动");
  startBtn.disabled = e.state !== "stopped";
  startBtn.addEventListener("click", () => doStart(e.id));
  sum.appendChild(startBtn);
  card.appendChild(sum);
  root.appendChild(card);

  // 方向核对
  const dirCard = h("div", "card");
  dirCard.appendChild(h("div", "card-title", "运行方向核对"));
  const drow = h("div", "row");
  drow.appendChild(h("span", null,
    `当前设定：<b>${e.planned_direction === "up" ? "上行 ↑" : "下行 ↓"}</b>` +
    `（计划 ${e.schedule_start}–${e.schedule_stop}）`));
  drow.appendChild(h("span", "spacer"));
  const db1 = actionBtn("改为上行 ↑", "btn small", () =>
    api("/api/direction", { esc_id: e.id, user: me, direction: "up" }).then(() => { toast("方向已更新"); refresh(); }));
  const db2 = actionBtn("改为下行 ↓", "btn small", () =>
    api("/api/direction", { esc_id: e.id, user: me, direction: "down" }).then(() => { toast("方向已更新"); refresh(); }));
  drow.append(db1, db2);
  dirCard.appendChild(drow);
  root.appendChild(dirCard);
}

// ================================================================ 运行状态
function renderStatus() {
  const root = $("#tab-status");
  root.innerHTML = "";
  root.appendChild(h("div", "small-note",
    "实时运行状态与控制柜读数；两名值班员<b>不能同时启动</b>同一扶梯（启动互斥），" +
    "重复跳停自动升级派单。"));
  S.escalators.forEach(e => {
    const card = h("div", "card");
    const top = h("div", "row");
    top.appendChild(h("div", "card-title", `${e.name}（${e.id}）`));
    top.appendChild(stateText(e));
    top.appendChild(h("span", "spacer"));
    top.appendChild(h("span", "chip", `版本 v${e.version}`));
    top.appendChild(h("span", "chip", e.state === "running"
      ? (e.direction === "up" ? "上行 ↑" : "下行 ↓") : "方向 —"));
    if (e.stop_count) top.appendChild(h("span", "chip amber", `今日跳停 ${e.stop_count}`));
    top.appendChild(h("span", "chip", `控制柜：${cabinetText(e.cabinet_state)}`));
    const dens = Math.round(e.density * 100);
    top.appendChild(h("span", "chip " + (dens >= 85 ? "red" : dens >= 60 ? "amber" : "green"),
      `客流 ${dens}%`));
    card.appendChild(top);
    const ops = h("div", "row");
    ops.style.marginTop = "10px";
    if (e.state === "running") ops.appendChild(actionBtn("停梯", "btn small danger", () => doStop(e.id)));
    if (e.state === "stopped")
      ops.appendChild(actionBtn("启动", "btn small primary", () => doStart(e.id)));
    ops.appendChild(actionBtn("详情/注入", "btn small", () => openEscModal(e.id)));
    ops.appendChild(h("span", "spacer"));
    ops.appendChild(h("span", "muted",
      `计划 ${e.schedule_start}–${e.schedule_stop} · ${e.low_floor}⇄${e.high_floor}`));
    card.appendChild(ops);
    root.appendChild(card);
  });
}

// ================================================================ 封控计时
function dur(sec) {
  sec = Math.max(0, sec | 0);
  const hh = String(Math.floor(sec / 3600)).padStart(2, "0");
  const mm = String(Math.floor((sec % 3600) / 60)).padStart(2, "0");
  const ss = String(sec % 60).padStart(2, "0");
  return `${hh}:${mm}:${ss}`;
}
function renderLockdown() {
  const root = $("#tab-lockdown");
  root.innerHTML = "";
  const locked = S.escalators.filter(e => e.state === "locked");
  root.appendChild(h("div", "small-note",
    "封控从停梯时刻起计时；围挡须在 5 分钟内搭设（缺失独立告警）。" +
    "<b>复开确认区</b>与普通操作保持明显距离，条件不全时复开被阻断并记为提前复开事件。"));
  if (!locked.length) { root.appendChild(h("div", "empty", "当前无封控设备")); return; }
  locked.forEach(e => {
    const card = h("div", "card");
    const top = h("div", "row");
    top.appendChild(h("div", "card-title", `${e.name}（${e.id}）`));
    top.appendChild(h("span", "chip red", REASON_TXT[e.lock_reason] || "封控"));
    top.appendChild(h("span", "spacer"));
    top.appendChild(h("span", "muted", `封控始于 ${e.locked_at?.replace("T", " ")}`));
    card.appendChild(top);
    const t = h("div", "timer-big", dur(e.locked_seconds));
    card.appendChild(t);
    card.appendChild(h("div", "timer-bar", "<i style='width:100%'></i>"));

    const br = h("div", "row");
    br.style.marginTop = "14px";
    br.appendChild(h("span", "chip " + (e.barrier_erected ? "red" : "amber"),
      e.barrier_erected ? (e.barrier_removed ? "围挡已撤除" : "围挡已搭设（斜纹禁行）")
                        : "⚠ 围挡缺失：立即搭设"));
    br.appendChild(h("span", "spacer"));
    if (e.barrier_required && !e.barrier_erected)
      br.appendChild(actionBtn("搭设围挡", "btn small danger",
        () => api("/api/barrier/erect", { esc_id: e.id, user: me }).then(refresh)));
    if (e.barrier_erected && !e.barrier_removed)
      br.appendChild(actionBtn("申请撤除围挡", "btn small",
        () => api("/api/barrier/remove", { esc_id: e.id, user: me }).then(refresh).catch(() => {})));
    card.appendChild(br);

    // 复开区：明显分隔
    const g = e.reopen;
    const zone = h("div", "reopen-zone");
    zone.appendChild(h("h4", null, "🚧 复开确认区（须全部满足）"));
    const ul = h("ul", "guard-list");
    const guards = [
      ["repair_approved", "维修复检已审批通过", e.lock_reason === "crowd"],
      ["barrier_removed", "围挡已撤除", false],
      ["crowd_resumed", "客流恢复正常", e.lock_reason !== "crowd"],
      ["detour_cleared", "绕行提示已撤销", false],
    ];
    guards.forEach(([k, label, skip]) => {
      const ok = skip || g[k];
      ul.appendChild(h("li", null,
        `<span class="${ok ? "yes" : "no"}">${ok ? "✓" : "✗"}</span>${label}${skip ? "（本场景免）" : ""}`));
    });
    zone.appendChild(ul);
    const rrow = h("div", "row");
    rrow.style.marginTop = "8px";
    rrow.appendChild(h("span", "muted",
      `携带当前版本 v${e.version} 提交 · 复开人：${pname(me)}（${person(me).role_name}）`));
    rrow.appendChild(h("span", "spacer"));
    const rb = h("button", "btn danger", "确认围挡撤除并复开");
    rb.disabled = !g.eligible;
    rb.addEventListener("click", async () => {
      try {
        await api("/api/reopen", { esc_id: e.id, user: me, version: e.version });
        toast("复开成功，设备恢复运行"); refresh();
      } catch (_) {}
    });
    rrow.appendChild(rb);
    zone.appendChild(rrow);
    card.appendChild(zone);
    root.appendChild(card);
  });
}

// ================================================================ 报修进度
function renderTickets() {
  const root = $("#tab-tickets");
  root.innerHTML = "";
  const filters = h("div", "filters");
  const openOnly = h("input");
  openOnly.type = "checkbox"; openOnly.checked = true; openOnly.id = "openOnly";
  const lab = h("label", "muted", "仅看未结案");
  lab.style.display = "flex"; lab.style.alignItems = "center"; lab.style.gap = "5px";
  lab.insertBefore(openOnly, lab.firstChild);
  openOnly.addEventListener("change", loadTickets);
  filters.appendChild(lab);
  filters.appendChild(h("span", "spacer"));
  const rep = h("button", "btn small ghost-danger", "+ 新故障报修");
  rep.addEventListener("click", () => {
    const sel = h("select");
    S.escalators.forEach(e => sel.appendChild(h("option", null, `${e.id} · ${e.name}`)));
    openReportModal(sel.value, sel);
  });
  filters.appendChild(rep);
  root.appendChild(filters);
  root.appendChild(h("div", "id-tickets"));
  loadTickets();
}
async function loadTickets() {
  const wrap = $("#id-tickets");
  const openOnly = $("#openOnly")?.checked ? 1 : "";
  const res = await fetch(`/api/tickets?open_only=${openOnly}`);
  const { tickets } = await res.json();
  wrap.innerHTML = "";
  if (!tickets.length) { wrap.appendChild(h("div", "empty", "暂无工单")); return; }
  tickets.forEach(t => wrap.appendChild(ticketCard(t)));
}

const PIPE = [
  ["reported", "报修"], ["dispatched", "派单"], ["arrived", "到场"],
  ["awaiting_review", "维修完成/待复检"], ["approved", "复检通过"], ["closed", "复开结案"],
];
function ticketCard(t) {
  const card = h("div", "card");
  const top = h("div", "row");
  top.appendChild(h("div", "card-title", `工单 #${t.id} · ${t.title}`));
  top.appendChild(h("span", "chip", t.fault_code));
  if (t.overdue && t.status !== "closed")
    top.appendChild(h("span", "chip red", "⚠ 已超 SLA"));
  top.appendChild(h("span", "chip " + (t.status === "closed" ? "green" : "amber"),
    TICKET_TXT[t.status]));
  top.appendChild(h("span", "spacer"));
  top.appendChild(h("span", "muted",
    `${escName(t.esc_id)} · 报修 ${t.reported_at?.replace("T", " ")}`));
  card.appendChild(top);

  // 流水线
  const pipe = h("div", "pipeline");
  let curIdx = PIPE.findIndex(([s]) => s === t.status);
  if (t.status === "repairing" || t.status === "rejected") curIdx = 3;
  PIPE.forEach(([s, label], i) => {
    const cls = t.status === "closed" || i < curIdx || (t.status === "approved" && i === 4)
      ? "done"
      : (t.status === "repairing" || t.status === "rejected") && i === 3 ? "bad current"
      : i === curIdx ? "current" : "";
    const step = h("div", "step " + cls,
      `<div class="ball">${i < curIdx || t.status === "closed" ? "✓" : i + 1}</div><div class="lbl">${label}</div>`);
    pipe.appendChild(step);
    if (i < PIPE.length - 1)
      pipe.appendChild(h("div", "line " + (i < curIdx || t.status === "closed" ? "done" : "")));
  });
  card.appendChild(pipe);

  const detail = h("div", "muted");
  detail.style.margin = "6px 0";
  detail.textContent = `报修人：${pname(t.reporter)}｜时限：${t.due_at?.replace("T", " ")}` +
    (t.arrived_at ? `｜到场：${t.arrived_at?.slice(11)}` : "") +
    (t.repaired_at ? `｜修复：${t.repaired_at?.slice(11)}（${pname(t.repair_by)}）` : "");
  card.appendChild(detail);
  if (t.detail) card.appendChild(h("div", "muted", "说明：" + t.detail));
  if (t.extensions?.length)
    card.appendChild(h("div", "muted", "延期记录：" +
      t.extensions.map(x => `${x.minutes}分钟(${pname(x.by)})`).join("、")));
  if (t.photos?.length) {
    const pw = h("div", "photos");
    t.photos.forEach(f => {
      const im = h("img"); im.src = `/photos/${f}`; im.title = f;
      im.addEventListener("click", () => window.open(im.src, "_blank"));
      pw.appendChild(im);
    });
    card.appendChild(pw);
  }

  const ops = h("div", "row");
  ops.style.marginTop = "10px";
  const add = (label, cls, fn) => ops.appendChild(actionBtn(label, "btn small " + cls, fn));
  if (t.status === "reported")
    add("派单", "primary", () => pickRepairer(t.id));
  if (t.status === "dispatched")
    add("维修到场", "primary", () => api("/api/arrive", { ticket_id: t.id, user: me }).then(() => { toast("已记录到场"); loadTickets(); }));
  if (["arrived", "dispatched", "repairing"].includes(t.status)) {
    add("维修完成，提交复检", "primary", () => askRepairNote(t.id));
    add("延期", "", () => askExtend(t.id));
  }
  if (["awaiting_review", "approved", "rejected"].includes(t.status))
    add("去复检/审批", "", () => switchTab("recheck"));
  if (ops.children.length) card.appendChild(ops);
  return card;
}
function pickRepairer(tid) {
  const reps = S.people.filter(p => p.role === "repairer");
  const sel = h("select");
  reps.forEach(r => sel.appendChild(h("option", null, `${r.name}`)));
  sel.value = "rep1";
  modalForm("派单", [["select", "维修人员", sel]], async () => {
    await api("/api/dispatch", { ticket_id: tid, user: me, repair_by: sel.value });
    toast("派单成功"); closeModal(); loadTickets();
  });
}
function askRepairNote(tid) {
  const ta = h("textarea");
  ta.placeholder = "维修措施、更换部件…";
  modalForm("维修完成", [["textarea", "维修说明", ta]], async () => {
    await api("/api/repair", { ticket_id: tid, user: me, note: ta.value });
    toast("已提交，等待巡检员复检"); closeModal(); loadTickets();
  });
}
function askExtend(tid) {
  const inp = h("input"); inp.type = "number"; inp.value = "30";
  const why = h("input"); why.placeholder = "延期原因（如配件调拨）";
  modalForm("维修延期", [["input", "延长分钟数", inp], ["input", "原因", why]], async () => {
    await api("/api/extend", { ticket_id: tid, user: me, minutes: +inp.value, reason: why.value });
    toast("延期已记录"); closeModal(); loadTickets();
  });
}

// ================================================================ 复检审批
async function renderRecheck() {
  const root = $("#tab-recheck");
  root.innerHTML = "";
  root.appendChild(h("div", "small-note",
    "复检由巡检员执行五项核验（每项必须选 通过/失败/不适用）；" +
    "<b>复检人不能审批自己的复检单</b>，须由另一名巡检员或主管审批；审批退回即返修。"));
  const [{ tickets }, { rechecks }] = await Promise.all([
    fetch("/api/tickets?open_only=1").then(r => r.json()),
    fetch("/api/rechecks").then(r => r.json()),
  ]);

  const todo = tickets.filter(t => t.status === "awaiting_review");
  const card1 = h("div", "card");
  card1.appendChild(h("div", "card-title", "待复检工单"));
  if (!todo.length) card1.appendChild(h("div", "empty", "暂无待复检工单"));
  todo.forEach(t => card1.appendChild(recheckForm(t)));
  root.appendChild(card1);

  const card2 = h("div", "card");
  card2.appendChild(h("div", "card-title", "复检审批队列"));
  const pending = rechecks.filter(r => r.status === "submitted");
  if (!pending.length) card2.appendChild(h("div", "empty", "暂无待审批复检单"));
  pending.forEach(r => card2.appendChild(recheckRow(r)));
  root.appendChild(card2);

  const card3 = h("div", "card");
  card3.appendChild(h("div", "card-title", "近期复检记录"));
  rechecks.slice(0, 8).forEach(r => {
    const cls = r.status === "approved" ? "green" : r.status === "rejected" ? "red" : "amber";
    card3.appendChild(h("div", "check-row",
      `<div class="check-name">#${r.id}</div>` +
      `<div class="check-note">工单 #${r.ticket_id} · ${escName(r.esc_id)} · 复检 ${pname(r.inspector)}` +
      ` → 审批 ${r.approver ? pname(r.approver) : "—"}</div>` +
      `<span class="chip ${cls}">${{ submitted: "待审批", approved: "已通过", rejected: "已退回" }[r.status]}</span>`));
  });
  root.appendChild(card3);
}
function recheckForm(t) {
  const box = h("div");
  box.style.border = "1px solid var(--silver-200)";
  box.style.borderRadius = "10px"; box.style.padding = "12px 14px"; box.style.marginBottom = "12px";
  box.appendChild(h("div", "row",
    `<b>工单 #${t.id} · ${escName(t.esc_id)}</b><span class='spacer'></span>` +
    `<span class='muted'>维修：${pname(t.repair_by)} · ${t.repaired_at?.replace("T", " ")}</span>`));
  const vals = {};
  S.check_items.forEach(ci => {
    const row = h("div", "check-row");
    row.appendChild(h("div", "check-name", ci.name));
    const tri = h("div", "tri");
    [["pass", "通过"], ["fail", "失败"], ["na", "不适用"]].forEach(([v, label]) => {
      const b = h("button", "", label);
      b.addEventListener("click", () => {
        vals[ci.key] = v;
        $$("button", tri).forEach(x => x.className = "");
        b.className = "sel-" + v;
        tri.className = "tri " + v;
      });
      tri.appendChild(b);
    });
    row.appendChild(h("div", "check-status", tri));
    box.appendChild(row);
  });
  const rrow = h("div", "row"); rrow.style.marginTop = "8px";
  rrow.appendChild(h("span", "muted", `复检人：${pname(me)}（${person(me).role_name}）`));
  rrow.appendChild(h("span", "spacer"));
  const submit = h("button", "btn primary small", "提交复检");
  submit.addEventListener("click", async () => {
    try {
      await api("/api/recheck", { ticket_id: t.id, user: me, results: vals });
      toast("复检已提交，等待他人审批"); renderRecheck();
    } catch (_) {}
  });
  rrow.appendChild(submit);
  box.appendChild(rrow);
  return box;
}
function recheckRow(r) {
  const row = h("div", "check-row");
  const detail = Object.entries(r.results).map(([k, v]) =>
    `${S.check_items.find(i => i.key === k).name}:${{ pass: "合格", fail: "失败", na: "不适用" }[v]}`).join("　");
  row.appendChild(h("div", "check-name", `#${r.id}`));
  row.appendChild(h("div", "check-note",
    `工单 #${r.ticket_id} · ${escName(r.esc_id)} · 复检人 <b>${pname(r.inspector)}</b>` +
    (r.inspector === me ? " <span style='color:var(--red)'>（与你是同一人，禁止自审）</span>" : "") +
    `<br><span class='muted'>${detail}</span>${r.note ? "　备注：" + r.note : ""}`));
  const btns = h("div", "row");
  const ap = h("button", "btn small primary", "批准");
  ap.disabled = r.inspector === me;
  ap.addEventListener("click", () => api("/api/review",
    { recheck_id: r.id, user: me, approve: true }).then(() => { toast("已批准，可撤围挡复开"); renderRecheck(); }));
  const rj = h("button", "btn small ghost-danger", "退回返修");
  rj.disabled = r.inspector === me;
  rj.addEventListener("click", () => {
    const why = prompt("退回原因：", "现场仍有异常");
    if (why === null) return;
    api("/api/review", { recheck_id: r.id, user: me, approve: false, note: why })
      .then(() => { toast("已退回返修"); renderRecheck(); });
  });
  btns.append(ap, rj);
  row.appendChild(btns);
  return row;
}

// ================================================================ 客流绕行
async function renderDetour() {
  const root = $("#tab-detour");
  root.innerHTML = "";
  root.appendChild(h("div", "small-note",
    "客服仅能发布/撤销<b>客流绕行提示</b>，无设备操作权限；" +
    "客流密度 ≥85% 自动停梯封控并提示发布绕行，回落到 60% 以下方可复开。"));
  const { detours } = await fetch("/api/detours").then(r => r.json());
  const active = detours.filter(d => d.active);
  const card1 = h("div", "card");
  card1.appendChild(h("div", "card-title", "生效中的绕行提示"));
  if (!active.length) card1.appendChild(h("div", "empty", "当前无绕行提示"));
  active.forEach(d => {
    const b = h("div", "detour-banner",
      `<b>🚧 ${escName(d.esc_id)}</b>　${d.message}<br>` +
      `<span class='muted'>渠道：${d.channel} · 发布：${pname(d.published_by)} · ${d.ts?.replace("T", " ")}</span>`);
    const x = h("button", "btn small", "撤销提示");
    x.style.marginTop = "6px";
    x.addEventListener("click", () => api("/api/detour/cancel",
      { detour_id: d.id, user: me }).then(() => { toast("已撤销"); renderDetour(); }));
    b.appendChild(x);
    card1.appendChild(b);
  });
  root.appendChild(card1);

  const card2 = h("div", "card");
  card2.appendChild(h("div", "card-title", "发布绕行提示"));
  const sel = h("select");
  S.escalators.forEach(e => sel.appendChild(h("option", null, `${e.id} · ${e.name}`)));
  const msg = h("textarea");
  msg.placeholder = "例：东翼 1 号扶梯检修，请前往中庭扶梯或乘 1 号直梯";
  const ch = h("select");
  ["广播+指引牌", "商场广播", "现场指引牌", "公众号推送"].forEach(c => ch.appendChild(h("option", null, c)));
  modalLike(card2, [["select", "设备", sel], ["textarea", "绕行内容", msg], ["select", "发布渠道", ch]]);
  const pub = h("button", "btn danger", "发布绕行提示");
  pub.style.marginTop = "10px";
  pub.addEventListener("click", async () => {
    try {
      await api("/api/detour/publish",
        { esc_id: sel.value, user: me, message: msg.value, channel: ch.value });
      toast("绕行提示已发布"); renderDetour();
    } catch (_) {}
  });
  card2.appendChild(pub);
  root.appendChild(card2);
}

// ================================================================ 事件查询
const evFilter = { esc: "", type: "" };
const EV_TYPES = ["", "CHECK", "ESTOP_TEST_PASS", "ESTOP_TEST_FAIL", "START", "STOP",
  "LOCK", "TRIP", "REPEAT_TRIP", "BARRIER_REQUIRED", "BARRIER_ERECTED", "BARRIER_OVERDUE",
  "BARRIER_REMOVED", "DISPATCH", "ARRIVE", "REPAIR_DONE", "REPAIR_OVERDUE", "REPAIR_EXTEND",
  "RECHECK_SUBMIT", "RECHECK_FAILED", "RECHECK_APPROVED", "RECHECK_REJECTED",
  "EARLY_REOPEN_BLOCKED", "REOPEN", "DETOUR_PUBLISH", "DETOUR_CANCEL", "CROWD_RESUMED",
  "DIRECTION_SET", "SCHEDULED_STOP", "SCHEDULED_START_DUE", "CABINET_RESET"];
function renderEvents() {
  const root = $("#tab-events");
  root.innerHTML = "";
  const f = h("div", "filters");
  const es = h("select");
  es.appendChild(h("option", null, "全部设备"));
  S.escalators.forEach(e => es.appendChild(h("option", null, `${e.id} · ${e.name}`)));
  es.value = evFilter.esc;
  const ts = h("select");
  EV_TYPES.forEach(t => ts.appendChild(h("option", null, t || "全部事件类型")));
  ts.value = evFilter.type;
  es.addEventListener("change", () => { evFilter.esc = es.value.startsWith("全部") ? "" : es.value.slice(0, 2); loadEvents(); });
  ts.addEventListener("change", () => { evFilter.type = ts.value.startsWith("全部") ? "" : ts.value; loadEvents(); });
  f.append(h("span", "muted", "筛选："), es, ts, h("span", "spacer"));
  root.appendChild(f);
  root.appendChild(h("div", "card", "<div id='evList'></div>"));
  loadEvents();
}
async function loadEvents() {
  const q = new URLSearchParams();
  if (evFilter.esc) q.set("esc_id", evFilter.esc);
  if (evFilter.type) q.set("type", evFilter.type);
  q.set("limit", "300");
  const { events } = await fetch("/api/events?" + q).then(r => r.json());
  const wrap = $("#evList");
  wrap.innerHTML = "";
  if (!events.length) { wrap.appendChild(h("div", "empty", "无事件")); return; }
  const tl = h("div", "timeline");
  const redTypes = new Set(["LOCK", "TRIP", "ESTOP_TEST_FAIL", "BARRIER_OVERDUE",
    "REPAIR_OVERDUE", "REPEAT_TRIP", "RECHECK_FAILED", "RECHECK_REJECTED", "EARLY_REOPEN_BLOCKED"]);
  const greenTypes = new Set(["START", "REOPEN", "RECHECK_APPROVED", "ESTOP_TEST_PASS",
    "REPAIR_DONE", "BARRIER_ERECTED", "BARRIER_REMOVED", "CROWD_RESUMED"]);
  events.forEach(e => {
    const cls = redTypes.has(e.type) ? "red" : greenTypes.has(e.type) ? "green" : "amber";
    tl.appendChild(h("div", "ev " + cls,
      `<span class="ev-time">${e.ts?.replace("T", " ")}</span>` +
      `<span class="ev-type">${e.type}</span>` +
      (e.esc_id ? `<b>[${e.esc_id}]</b> ` : "") +
      `${e.detail || ""} ` +
      `<span class="ev-actor">${e.actor ? "— " + pname(e.actor) : ""}</span>`));
  });
  wrap.appendChild(tl);
}

// ---------------------------------------------------------------- 通用表单弹窗
function modalForm(title, fields, onOk) {
  const m = $("#modal");
  m.innerHTML = "";
  m.appendChild(h("h3", null, title));
  fields.forEach(([kind, label, el]) => {
    const fr = h("div", "form-row");
    fr.appendChild(h("label", null, label));
    fr.appendChild(el);
    m.appendChild(fr);
  });
  const acts = h("div", "modal-actions");
  const cancel = h("button", "btn", "取消");
  cancel.addEventListener("click", closeModal);
  const ok = h("button", "btn primary", "确定");
  ok.addEventListener("click", onOk);
  acts.append(cancel, ok);
  m.appendChild(acts);
  $("#modalMask").hidden = false;
}
function modalLike(card, fields) {
  fields.forEach(([kind, label, el]) => {
    const fr = h("div", "form-row");
    fr.appendChild(h("label", null, label));
    fr.appendChild(el);
    card.appendChild(fr);
  });
}

// ---------------------------------------------------------------- 报修弹窗（含照片）
function openReportModal(escId, escSelect) {
  const code = h("select");
  [["CHAIN_FAULT", "链条异响"], ["COMB_DAMAGE", "梳齿板缺损"], ["HANDRAIL_STOP", "扶手带停转"],
   ["STEPS_JAM", "梯级卡滞"], ["OIL_LEAK", "漏油"], ["OTHER", "其他"]].forEach(([v, l]) =>
    code.appendChild(h("option", null, l)));
  const title = h("input");
  title.value = "链条异响";
  const detail = h("textarea");
  detail.placeholder = "现场情况描述…";
  const file = h("input"); file.type = "file"; file.accept = "image/*"; file.multiple = true;
  const fields = [];
  if (escSelect) fields.push(["select", "设备", escSelect]);
  fields.push(["select", "故障类型", code], ["input", "标题", title],
              ["textarea", "详情", detail], ["input", "故障照片（存入本地目录）", file]);
  modalForm("故障报修（将停梯封控）", fields, async () => {
    const target = escSelect ? escSelect.value.slice(0, 2) : escId;
    let photos = [];
    if (file.files.length) {
      for (const f of file.files) photos.push(await uploadPhoto(f));
    }
    await api("/api/report", {
      esc_id: target, user: me,
      fault_code: code.value, title: title.value || code.selectedOptions[0].text,
      detail: detail.value, photos,
    });
    toast("已报修并封控"); closeModal(); refresh();
  });
}

// ---------------------------------------------------------------- 渲染调度
function renderAll() {
  if (!S) return;
  renderMap();
  renderChecklist();
  renderStatus();
  renderLockdown();
  renderTickets();
  renderRecheck();
  renderDetour();
  renderEvents();
}
setInterval(() => {
  const d = new Date();
  $("#clock").textContent = d.toLocaleTimeString("zh-CN", { hour12: false });
}, 1000);

(async function init() {
  const res = await fetch("/api/state");
  S = await res.json();
  initIdentity();
  renderAll();
  setInterval(refresh, 3000);
})();
