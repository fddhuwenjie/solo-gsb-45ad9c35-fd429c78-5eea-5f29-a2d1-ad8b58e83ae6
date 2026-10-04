"use strict";

/* 口述史发布许可校验台 —— 前端交互（原生 JavaScript） */

const MEDIA = [
  { value: "audio", label: "音频" },
  { value: "text", label: "文字" },
];

const state = {
  interviews: [],
  currentId: null,
  detail: null, // {id, title, duration_seconds, speakers, segments, licenses}
  remBatch: [], // 补件批次草稿 [{speaker_id, start, end, allow, deny, embargo, note}]
  remResult: null, // 最近一次补件试算 / 写入结果
};

const $ = (id) => document.getElementById(id);

/* ---------- 工具 ---------- */

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

function fmt(sec) {
  sec = Math.max(0, Math.round(Number(sec) || 0));
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  const s = sec % 60;
  return [h, m, s].map((n) => String(n).padStart(2, "0")).join(":");
}

function parseTimecode(str) {
  str = String(str ?? "").trim();
  if (!str) return NaN;
  const parts = str.split(":");
  if (parts.length > 3) return NaN;
  let sec = 0;
  for (const p of parts) {
    if (!/^\d+$/.test(p)) return NaN;
    sec = sec * 60 + parseInt(p, 10);
  }
  return sec;
}

function setStatus(msg, isError = false) {
  const el = $("status-line");
  el.textContent = msg || "";
  el.className = isError ? "error" : "";
}

async function api(path, method = "GET", body) {
  let resp;
  try {
    resp = await fetch(path, {
      method,
      headers: body !== undefined ? { "Content-Type": "application/json" } : undefined,
      body: body !== undefined ? JSON.stringify(body) : undefined,
    });
  } catch (e) {
    throw new Error("无法连接服务器");
  }
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) throw new Error(data.error || `请求失败（HTTP ${resp.status}）`);
  return data;
}

/* 包装异步操作，统一错误提示 */
async function run(fn) {
  try {
    await fn();
    setStatus("");
  } catch (e) {
    setStatus(e.message, true);
  }
}

/* ---------- 数据加载 ---------- */

async function loadInterviews() {
  state.interviews = await api("/api/interviews");
  if (!state.interviews.some((i) => i.id === state.currentId)) {
    state.currentId = state.interviews.length ? state.interviews[0].id : null;
  }
  await refreshDetail();
  render();
}

async function refreshDetail() {
  state.detail = state.currentId
    ? await api(`/api/interviews/${state.currentId}`)
    : null;
}

/* ---------- 渲染 ---------- */

function render() {
  const sel = $("interview-select");
  sel.innerHTML = state.interviews.length
    ? state.interviews.map((i) =>
        `<option value="${i.id}" ${i.id === state.currentId ? "selected" : ""}>`
        + `#${i.id} ${esc(i.title)}（${fmt(i.duration_seconds)}）</option>`).join("")
    : `<option value="">（暂无访谈，请先新建）</option>`;

  const has = !!state.detail;
  $("detail-panels").classList.toggle("hidden", !has);
  $("interview-edit").classList.toggle("hidden", !has);
  if (!has) return;

  $("edit-title").value = state.detail.title;
  $("edit-duration").value = fmt(state.detail.duration_seconds);
  renderSpeakers();
  renderSegments();
  renderLicenses();
  renderRemediate();
  renderRemResult();
}

function speakerOptions(selected) {
  return state.detail.speakers.map((s) =>
    `<option value="${s.id}" ${s.id === selected ? "selected" : ""}>${esc(s.name)}</option>`
  ).join("");
}

function speakerName(id) {
  const sp = state.detail.speakers.find((s) => s.id === id);
  return sp ? sp.name : `#${id}`;
}

function renderSpeakers() {
  const rows = state.detail.speakers.map((s) => `
    <tr>
      <td>#${s.id}</td>
      <td><input id="sp-name-${s.id}" value="${esc(s.name)}"></td>
      <td>
        <button onclick="run(() => saveSpeaker(${s.id}))">保存</button>
        <button class="danger" onclick="run(() => delSpeaker(${s.id}))">删除</button>
      </td>
    </tr>`).join("");
  $("speakers-body").innerHTML =
    rows || `<tr><td colspan="3" class="empty">暂无说话人</td></tr>`;
}

function renderSegments() {
  const rows = state.detail.segments.map((g) => `
    <tr>
      <td>#${g.id}</td>
      <td><select id="seg-${g.id}-sp">${speakerOptions(g.speaker_id)}</select></td>
      <td><input id="seg-${g.id}-start" class="tc-input" value="${fmt(g.start_seconds)}"></td>
      <td><input id="seg-${g.id}-end" class="tc-input" value="${fmt(g.end_seconds)}"></td>
      <td>
        <button onclick="run(() => saveSegment(${g.id}))">保存</button>
        <button class="danger" onclick="run(() => delSegment(${g.id}))">删除</button>
      </td>
    </tr>`).join("");
  $("segments-body").innerHTML =
    rows || `<tr><td colspan="5" class="empty">暂无发言区间</td></tr>`;
  $("new-seg-speaker").innerHTML = speakerOptions(null);
}

function mediaCheckboxes(prefix, picked) {
  return MEDIA.map((m) =>
    `<label><input type="checkbox" id="${prefix}-${m.value}"`
    + `${picked.includes(m.value) ? " checked" : ""}>${m.label}</label>`
  ).join("");
}

function readMediaCheckboxes(prefix) {
  return MEDIA.filter((m) => $(`${prefix}-${m.value}`).checked)
    .map((m) => m.value);
}

function renderLicenses() {
  const rows = state.detail.licenses.map((l) => `
    <tr>
      <td>#${l.id}</td>
      <td>${esc(speakerName(l.speaker_id))}</td>
      <td><input id="lic-${l.id}-start" class="tc-input" value="${fmt(l.start_seconds)}"></td>
      <td><input id="lic-${l.id}-end" class="tc-input" value="${fmt(l.end_seconds)}"></td>
      <td>${mediaCheckboxes(`lic-${l.id}-allow`, l.allow_media)}</td>
      <td>${mediaCheckboxes(`lic-${l.id}-deny`, l.deny_media)}</td>
      <td><input type="date" id="lic-${l.id}-emb" value="${esc(l.embargo_until || "")}"></td>
      <td><input id="lic-${l.id}-note" value="${esc(l.note)}"></td>
      <td>
        <button onclick="run(() => saveLicense(${l.id}))">保存</button>
        <button class="danger" onclick="run(() => delLicense(${l.id}))">删除</button>
      </td>
    </tr>`).join("");
  $("licenses-body").innerHTML =
    rows || `<tr><td colspan="9" class="empty">暂无授权记录</td></tr>`;
  $("new-lic-speaker").innerHTML = speakerOptions(null);
}

function clearResults() {
  $("results-summary").textContent = "尚未校验。";
  $("timeline").innerHTML = "";
  $("results-body").innerHTML = "";
}

function intervalRowsHtml(intervals) {
  return intervals.map((it) => `
    <tr class="${it.status}">
      <td class="tc">${it.start_timecode} – ${it.end_timecode}</td>
      <td>${fmt(it.end_seconds - it.start_seconds)}</td>
      <td><span class="badge ${it.status}">${it.status_label}</span></td>
      <td>${it.speakers.length ? esc(it.speakers.join("、")) : "—"}</td>
      <td>${it.reasons.length
        ? it.reasons.map(esc).join("<br>")
        : (it.status === "no_speech" ? "无发言，无需授权" : "全部发言人授权有效")}</td>
    </tr>`).join("");
}

function renderResults(data) {
  const { intervals, summary } = data;
  const mediumLabel = data.medium === "audio" ? "音频" : "文字";
  $("results-summary").innerHTML =
    `发布日期 <b>${esc(data.publication_date)}</b> · 媒介 <b>${mediumLabel}</b> · `
    + `总时长 <b>${fmt(summary.duration_seconds)}</b>，`
    + `可发布 <b class="ok-text">${fmt(summary.publishable_seconds)}</b>，`
    + `不可发布 <b class="bad-text">${fmt(summary.blocked_seconds)}</b>`;

  $("timeline").innerHTML = intervals.map((it) => {
    const width = (it.end_seconds - it.start_seconds)
      / summary.duration_seconds * 100;
    const tip = `${it.start_timecode}–${it.end_timecode} ${it.status_label}`
      + (it.reasons.length ? "\n" + it.reasons.join("\n") : "");
    return `<div class="seg ${it.status}" style="width:${width}%"`
      + ` title="${esc(tip)}"></div>`;
  }).join("");

  $("results-body").innerHTML = intervalRowsHtml(intervals);
}

/* ---------- 访谈操作 ---------- */

async function createInterview() {
  const title = $("new-title").value.trim();
  const duration_seconds = parseTimecode($("new-duration").value);
  if (!title) throw new Error("请填写访谈标题");
  if (!Number.isInteger(duration_seconds) || duration_seconds <= 0) {
    throw new Error("时长格式应为 HH:MM:SS");
  }
  const created = await api("/api/interviews", "POST", { title, duration_seconds });
  state.currentId = created.id;
  $("new-title").value = "";
  await loadInterviews();
}

async function saveInterview() {
  const title = $("edit-title").value.trim();
  const duration_seconds = parseTimecode($("edit-duration").value);
  if (!title) throw new Error("请填写访谈标题");
  if (!Number.isInteger(duration_seconds) || duration_seconds <= 0) {
    throw new Error("时长格式应为 HH:MM:SS");
  }
  await api(`/api/interviews/${state.currentId}`, "PUT", { title, duration_seconds });
  await loadInterviews();
}

async function deleteInterview() {
  if (!confirm("确定删除该访谈及其全部说话人、发言与授权记录？")) return;
  await api(`/api/interviews/${state.currentId}`, "DELETE");
  state.currentId = null;
  state.remBatch = [];
  state.remResult = null;
  clearResults();
  await loadInterviews();
}

/* ---------- 说话人操作 ---------- */

async function addSpeaker() {
  const name = $("new-speaker-name").value.trim();
  if (!name) throw new Error("请填写说话人姓名");
  await api(`/api/interviews/${state.currentId}/speakers`, "POST", { name });
  $("new-speaker-name").value = "";
  await refreshDetail();
  render();
}

async function saveSpeaker(id) {
  const name = $(`sp-name-${id}`).value.trim();
  if (!name) throw new Error("说话人姓名不能为空");
  await api(`/api/speakers/${id}`, "PUT", { name });
  await refreshDetail();
  render();
}

async function delSpeaker(id) {
  if (!confirm("删除该说话人及其发言、授权记录？")) return;
  await api(`/api/speakers/${id}`, "DELETE");
  await refreshDetail();
  render();
}

/* ---------- 发言区间操作 ---------- */

function readSegmentInputs(prefix) {
  const start_seconds = parseTimecode($(`${prefix}-start`).value);
  const end_seconds = parseTimecode($(`${prefix}-end`).value);
  if (!Number.isInteger(start_seconds) || !Number.isInteger(end_seconds)) {
    throw new Error("时间码格式应为 HH:MM:SS");
  }
  if (end_seconds <= start_seconds) throw new Error("结束时间必须大于开始时间");
  return { start_seconds, end_seconds };
}

async function addSegment() {
  const speaker_id = Number($("new-seg-speaker").value);
  if (!speaker_id) throw new Error("请先添加说话人");
  const range = readSegmentInputs("new-seg");
  await api(`/api/interviews/${state.currentId}/segments`, "POST",
    { speaker_id, ...range });
  await refreshDetail();
  render();
}

async function saveSegment(id) {
  const speaker_id = Number($(`seg-${id}-sp`).value);
  const range = readSegmentInputs(`seg-${id}`);
  await api(`/api/segments/${id}`, "PUT", { speaker_id, ...range });
  await refreshDetail();
  render();
}

async function delSegment(id) {
  await api(`/api/segments/${id}`, "DELETE");
  await refreshDetail();
  render();
}

/* ---------- 授权区间操作 ---------- */

function readLicenseInputs(prefix) {
  const range = readSegmentInputs(prefix);
  return {
    ...range,
    allow_media: readMediaCheckboxes(`${prefix}-allow`),
    deny_media: readMediaCheckboxes(`${prefix}-deny`),
    embargo_until: $(`${prefix}-emb`).value || null,
    note: $(`${prefix}-note`).value.trim(),
  };
}

async function addLicense() {
  const speaker_id = Number($("new-lic-speaker").value);
  if (!speaker_id) throw new Error("请先添加说话人");
  const values = readLicenseInputs("new-lic");
  await api(`/api/speakers/${speaker_id}/licenses`, "POST", values);
  await refreshDetail();
  render();
}

async function saveLicense(id) {
  const values = readLicenseInputs(`lic-${id}`);
  await api(`/api/licenses/${id}`, "PUT", values);
  await refreshDetail();
  render();
}

async function delLicense(id) {
  await api(`/api/licenses/${id}`, "DELETE");
  await refreshDetail();
  render();
}

/* ---------- 校验 ---------- */

async function evaluate() {
  const publication_date = $("pub-date").value;
  const medium = $("medium").value;
  if (!publication_date) throw new Error("请选择发布日期");
  const data = await api(`/api/interviews/${state.currentId}/evaluate`, "POST",
    { publication_date, medium });
  renderResults(data);
}

/* ---------- 补件并复核 ---------- */

function renderRemediate() {
  const rows = state.remBatch.map((row, i) => `
    <tr>
      <td>${i + 1}</td>
      <td><select id="rem-${i}-sp">${speakerOptions(row.speaker_id)}</select></td>
      <td><input id="rem-${i}-start" class="tc-input" placeholder="00:00:00" value="${esc(row.start)}"></td>
      <td><input id="rem-${i}-end" class="tc-input" placeholder="00:10:00" value="${esc(row.end)}"></td>
      <td>${mediaCheckboxes(`rem-${i}-allow`, row.allow)}</td>
      <td>${mediaCheckboxes(`rem-${i}-deny`, row.deny)}</td>
      <td><input type="date" id="rem-${i}-emb" value="${esc(row.embargo || "")}"></td>
      <td><input id="rem-${i}-note" value="${esc(row.note || "")}"></td>
      <td><button class="danger" onclick="run(() => removeRemRow(${i}))">删除</button></td>
    </tr>`).join("");
  $("remediate-body").innerHTML =
    rows || `<tr><td colspan="9" class="empty">暂无补件，点击「添加一行」</td></tr>`;
}

/* 重渲染前把页面上的编辑同步回 state.remBatch */
function syncRemBatch() {
  state.remBatch = state.remBatch.map((row, i) => {
    if (!$(`rem-${i}-sp`)) return row;
    return {
      speaker_id: Number($(`rem-${i}-sp`).value) || null,
      start: $(`rem-${i}-start`).value,
      end: $(`rem-${i}-end`).value,
      allow: readMediaCheckboxes(`rem-${i}-allow`),
      deny: readMediaCheckboxes(`rem-${i}-deny`),
      embargo: $(`rem-${i}-emb`).value,
      note: $(`rem-${i}-note`).value,
    };
  });
}

async function addRemRow() {
  syncRemBatch();
  state.remBatch.push({
    speaker_id: state.detail.speakers.length ? state.detail.speakers[0].id : null,
    start: "", end: "", allow: ["audio"], deny: [], embargo: "", note: "",
  });
  renderRemediate();
}

async function removeRemRow(i) {
  syncRemBatch();
  state.remBatch.splice(i, 1);
  renderRemediate();
}

function renderRemResult() {
  const data = state.remResult;
  const box = $("remediate-result");
  if (!data) {
    box.innerHTML = "";
    return;
  }
  const mediumLabel = data.medium === "audio" ? "音频" : "文字";
  let headline;
  if (data.committed) {
    headline = `<p class="ok-text">✔ 整条时间轴均可发布，已一次性写入 `
      + `${data.inserted_license_ids.length} 条补充授权`
      + `（许可 ${data.inserted_license_ids.map((id) => "#" + id).join("、")}），`
      + `以下为变更后的区间。</p>`;
  } else if (data.blocked && data.blocked.length) {
    headline = `<p class="bad-text">✘ 合并补件后仍有 ${data.blocked.length} 个`
      + `不可发布区间，未写入任何记录。</p>`;
  } else {
    headline = `<p class="ok-text">✔ 试算通过：合并补件后整条时间轴均可发布`
      + `（尚未写入，确认无误请点击「补件并写入」）。</p>`;
  }
  const sum = (s) =>
    `可发布 ${fmt(s.publishable_seconds)} / 共 ${fmt(s.duration_seconds)}`
    + `（阻断 ${fmt(s.blocked_seconds)}）`;
  box.innerHTML = `
    <div class="summary">发布日期 <b>${esc(data.publication_date)}</b> · 媒介 <b>${mediumLabel}</b></div>
    ${headline}
    <p class="hint">试算前：${sum(data.before.summary)}<br>`
    + `${data.committed ? "写入后" : "合并补件后"}：${sum(data.after.summary)}</p>
    <table>
      <thead>
        <tr><th>区间（时间码）</th><th>时长</th><th>结果</th><th>说话人</th><th>原因 / 说明</th></tr>
      </thead>
      <tbody>${intervalRowsHtml(data.after.intervals)}</tbody>
    </table>`;
}

async function submitRemediate(commit) {
  syncRemBatch();
  if (!state.remBatch.length) throw new Error("请先添加补件行");
  const publication_date = $("pub-date").value;
  const medium = $("medium").value;
  if (!publication_date) throw new Error("请选择发布日期");
  const supplements = state.remBatch.map((row, i) => {
    if (!row.speaker_id) throw new Error(`第 ${i + 1} 行请选择说话人`);
    const start_seconds = parseTimecode(row.start);
    const end_seconds = parseTimecode(row.end);
    if (!Number.isInteger(start_seconds) || !Number.isInteger(end_seconds)) {
      throw new Error(`第 ${i + 1} 行时间码格式应为 HH:MM:SS`);
    }
    return {
      speaker_id: row.speaker_id,
      start_seconds,
      end_seconds,
      allow_media: row.allow,
      deny_media: row.deny,
      embargo_until: row.embargo || null,
      note: String(row.note || "").trim(),
    };
  });
  let resp;
  try {
    resp = await fetch(`/api/interviews/${state.currentId}/remediate`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ publication_date, medium, commit, supplements }),
    });
  } catch (e) {
    throw new Error("无法连接服务器");
  }
  const data = await resp.json().catch(() => ({}));
  if (resp.status === 409) {
    // 仍有阻断区间：展示试算结果与原因，再提示未写入
    state.remResult = data;
    renderRemResult();
    throw new Error(data.error || "仍存在不可发布区间，未写入任何补件");
  }
  if (!resp.ok) throw new Error(data.error || `请求失败（HTTP ${resp.status}）`);
  state.remResult = data;
  if (data.committed) {
    state.remBatch = [];
    await refreshDetail();
    render();
  } else {
    renderRemResult();
  }
}

/* ---------- 启动 ---------- */

window.addEventListener("DOMContentLoaded", () => {
  $("pub-date").value = new Date().toISOString().slice(0, 10);
  $("btn-create-interview").addEventListener("click", () => run(createInterview));
  $("btn-save-interview").addEventListener("click", () => run(saveInterview));
  $("btn-delete-interview").addEventListener("click", () => run(deleteInterview));
  $("btn-refresh").addEventListener("click", () => run(loadInterviews));
  $("btn-add-speaker").addEventListener("click", () => run(addSpeaker));
  $("btn-add-segment").addEventListener("click", () => run(addSegment));
  $("btn-add-license").addEventListener("click", () => run(addLicense));
  $("btn-evaluate").addEventListener("click", () => run(evaluate));
  $("btn-rem-add").addEventListener("click", () => run(addRemRow));
  $("btn-rem-dry").addEventListener("click", () => run(() => submitRemediate(false)));
  $("btn-rem-commit").addEventListener("click", () => run(() => submitRemediate(true)));
  $("interview-select").addEventListener("change", (e) => run(async () => {
    state.currentId = Number(e.target.value) || null;
    state.remBatch = [];
    state.remResult = null;
    await refreshDetail();
    render();
    clearResults();
  }));
  run(loadInterviews);
});
