// BookingAgent admin page. Everything is inserted with textContent.
"use strict";

const $ = (id) => document.getElementById(id);
const store = {
  get(k, d) { try { return localStorage.getItem(k) ?? d; } catch { return d; } },
  set(k, v) { try { localStorage.setItem(k, v); } catch { /* storage unavailable */ } },
};
function el(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined && text !== null) n.textContent = String(text);
  return n;
}
const pill = (v) => el("span", "pill " + (v || ""), v || "—");
const time = (iso) => iso ? new Date(iso).toLocaleString("en-IN", { dateStyle: "short", timeStyle: "medium" }) : "";

async function api(path, opts = {}) {
  const res = await fetch("/admin/api" + path, {
    ...opts, headers: { "Content-Type": "application/json", "X-Admin-Code": store.get("ba.admin", ""), ...(opts.headers || {}) },
  });
  if (res.status === 401) { $("gate").showModal(); throw new Error("admin code required"); }
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body.detail ? (typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail)) : res.status);
  return body;
}

// ---------------------------------------------------------------- tabs
const loaders = { activity: loadActivity, bookings: loadBookings, jobs: loadJobs, settings: loadSettings, demo: loadDemo, users: () => {} };
function showTab(name) {
  document.querySelectorAll(".tabs button").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.tab === name)));
  document.querySelectorAll(".tab").forEach((t) => { t.hidden = t.id !== "tab-" + name; });
  store.set("ba.admin.tab", name);
  loaders[name]().catch(showError);
}
function showError(e) { console.error(e); }

// ---------------------------------------------------------------- activity and traces
async function loadActivity() {
  const { counts, activity } = await api("/overview");
  const c = $("counts");
  c.replaceChildren();
  const add = (label, value) => { const d = el("div", "count"); d.append(el("b", "", value), el("span", "", label)); c.append(d); };
  add("conversations", counts.conversations);
  add("searches", counts.searches);
  add("quotes shown", counts.quotes.shown || 0);
  add("quotes confirmed", counts.quotes.confirmed || 0);
  add("quotes done", counts.quotes.done || 0);
  add("bookings booked", counts.bookings.booked || 0);
  add("jobs pending", counts.jobs.pending || 0);
  add("jobs running", counts.jobs.running || 0);
  add("jobs failed", counts.jobs.failed || 0);
  add("jobs dead", counts.jobs.dead || 0);
  const tb = $("activity").querySelector("tbody");
  tb.replaceChildren();
  if (!activity.length) { const tr = el("tr"); const td = el("td", "", "No turns since the last restart."); td.colSpan = 9; tr.append(td); tb.append(tr); }
  for (const a of activity) {
    const tr = el("tr");
    tr.tabIndex = 0;
    const cells = [time(a.start), a.user_id, a.message || (a.name !== "turn" ? a.name : ""), null, a.answered_by, a.tool_calls, a.tokens, a.cost_usd ? "$" + a.cost_usd.toFixed(4) : "", Math.round(a.duration_ms)];
    cells.forEach((v, i) => {
      const td = el("td", i === 2 ? "msg-cell" : "");
      if (i === 3) td.append(pill(a.exit || a.status)); else td.textContent = v ?? "";
      if (i === 2) td.title = a.message || "";
      tr.append(td);
    });
    const open = () => { tb.querySelectorAll("tr").forEach((r) => r.removeAttribute("aria-selected")); tr.setAttribute("aria-selected", "true"); loadTrace(a.trace_id).catch(showError); };
    tr.addEventListener("click", open);
    tr.addEventListener("keydown", (e) => { if (e.key === "Enter") open(); });
    tb.append(tr);
  }
}

const SUMMARY = {
  tool_call: (l) => `${l.tool} → ${l.outcome}${l.error ? " (" + l.error + ")" : ""}`,
  model_call: (l) => `${l["gen_ai.request.model"] || "model"}${l.ok === false ? " failed: " + (l.error || "") : ""}`,
  effect_issued: (l) => `${l.action} sent with key ${l.idem_key}`,
  effect_result: (l) => `verified: ${l.status}${l.booking_id ? " " + l.booking_id : ""}`,
  approval_granted: (l) => `approval ${l.approval_id} for ${l.action}`,
  approval_refused: (l) => `approval refused: ${l.reason}`,
  write_result: (l) => `write result: ${l.kind}${l.booking_id ? " " + l.booking_id : ""}`,
  run_finished: (l) => `exit ${l.exit && l.exit.kind}, ${l.tool_calls} tool calls, ${l.elapsed_s}s`,
  run_started: (l) => `user ${l.user_id}, conversation ${l.conversation_id || l.session_id}`,
};

async function loadTrace(traceId) {
  const { timeline } = await api("/traces/" + traceId);
  $("trace-hint").textContent = "Trace " + traceId;
  const ol = $("timeline");
  ol.replaceChildren();
  for (const l of timeline) {
    const li = el("li", l.type);
    li.append(el("span", "t", l.type.replace(/_/g, " ")));
    const s = SUMMARY[l.type] ? SUMMARY[l.type](l) : "";
    if (s) li.append(el("span", "m", s));
    if (l.latency_ms !== undefined) li.append(el("span", "m", Math.round(l.latency_ms) + " ms"));
    const d = el("details");
    d.append(el("summary", "", "details"));
    const body = { ...l };
    delete body.trace_id;
    d.append(el("pre", "", JSON.stringify(body, null, 2)));
    li.append(d);
    ol.append(li);
  }
}

// ---------------------------------------------------------------- bookings and quotes
const rupees = (amount) => amount == null ? "" : "₹" + amount.toLocaleString("en-IN");
async function loadBookings() {
  const { bookings, quotes } = await api("/bookings");
  const tb = $("bookings-table").querySelector("tbody");
  tb.replaceChildren();
  for (const b of bookings) {
    const tr = el("tr");
    [b.id, b.user_id, b.provider_name, time(b.start)].forEach((v) => tr.append(el("td", "", v)));
    const st = el("td"); st.append(pill(b.status)); tr.append(st);
    tr.append(el("td", "", time(b.created_at)));
    const act = el("td");
    if (b.status === "booked") {
      const btn = el("button", "", "Cancel as provider");
      btn.type = "button";
      btn.addEventListener("click", async () => { await providerCancel(b.id); loadBookings(); });
      act.append(btn);
    }
    tr.append(act);
    tb.append(tr);
  }
  const tq = $("quotes-table").querySelector("tbody");
  tq.replaceChildren();
  for (const q of quotes) {
    const tr = el("tr");
    [q.id, q.user_id, q.action, q.provider_name, time(q.start), rupees(q.price), q.version].forEach((v) => tr.append(el("td", "", v)));
    const st = el("td"); st.append(pill(q.status)); tr.append(st);
    tr.append(el("td", "", time(q.updated_at)));
    tq.append(tr);
  }
}

// ---------------------------------------------------------------- jobs
async function loadJobs() {
  const { jobs } = await api("/jobs?limit=50");
  const tb = $("jobs-table").querySelector("tbody");
  tb.replaceChildren();
  if (!jobs.length) { const tr = el("tr"); const td = el("td", "", "No jobs yet."); td.colSpan = 5; tr.append(td); tb.append(tr); }
  for (const j of jobs) {
    const tr = el("tr");
    tr.append(el("td", "", j.kind));
    const st = el("td"); st.append(pill(j.status)); tr.append(st);
    tr.append(el("td", "", `${j.attempts}/${j.max_attempts}`));
    tr.append(el("td", "", time(j.run_at)));
    const err = el("td", "msg-cell", j.last_error || "");
    err.title = j.last_error || "";
    tr.append(err);
    tb.append(tr);
  }
}

// ---------------------------------------------------------------- users
$("user-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const id = $("user-q").value.trim();
  const out = $("user-out");
  out.replaceChildren(el("p", "hint", "Loading…"));
  try {
    const u = await api("/users/" + encodeURIComponent(id));
    out.replaceChildren();
    const section = (title, headers, rows) => {
      out.append(el("h2", "", title));
      if (!rows.length) { out.append(el("p", "hint", "None.")); return; }
      const t = el("table");
      t.append(row(headers, "th"));
      rows.forEach((r) => t.append(row(r)));
      out.append(t);
    };
    const p = u.profile;
    section("Profile", ["Name", "Phones", "Addresses"], [[p.name || "", p.phones.join(", "), p.addresses.map((a) => `${a.label}: ${a.address}`).join("; ")]]);
    out.append(el("h2", "", "Memory"));
    out.append(el("pre", "", JSON.stringify(u.memory, null, 2)));
    section("Conversations", ["Conversation", "Title", "Updated"], u.conversations.map((c) => [c.id, c.title || "", time(c.updated_at)]));
    section("Our bookings", ["Booking", "Service", "Provider", "Slot", "Price", "Status"], u.bookings.map((b) => [b.id, b.service, b.provider_name, time(b.start), rupees(b.price), b.status]));
    section("Provider bookings", ["Booking", "Service", "Provider", "Slot", "Status"], (u.provider_bookings || []).map((b) => [b.booking_id, b.service, b.provider_name, time(b.slot_start), b.status]));
  } catch (err) { out.replaceChildren(el("p", "error", String(err.message))); }
});
function row(values, cell = "td") {
  const tr = el("tr");
  values.forEach((v) => tr.append(el(cell, "", v)));
  return tr;
}

// ---------------------------------------------------------------- settings
async function loadSettings() {
  const { settings, info } = await api("/settings");
  const f = $("settings-form");
  for (const [k, v] of Object.entries(settings)) {
    const input = f.elements.namedItem(k);
    if (!input) continue;
    if (input.type === "checkbox") input.checked = Boolean(v); else input.value = v;
  }
  $("settings-info").textContent = `LLM key: ${info.llm_key} (${info.llm_base_url}) · database: ${info.database} · user access code: ${info.access_code}. Keys and the database are set in the environment, not here.`;
}
$("settings-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = e.target;
  const body = {};
  for (const input of f.querySelectorAll("input[name], select[name], textarea[name]")) {
    if (input.type === "checkbox") body[input.name] = input.checked;
    else if (input.type === "number") body[input.name] = Number(input.value);
    else body[input.name] = input.value;
  }
  const status = $("settings-status");
  status.textContent = "Saving…";
  status.className = "";
  try {
    await api("/settings", { method: "PUT", body: JSON.stringify(body) });
    status.textContent = "Saved. New turns use these settings.";
  } catch (err) { status.textContent = String(err.message); status.className = "error"; }
});

// ---------------------------------------------------------------- demo controls
async function loadDemo() {
  const { demo } = await api("/overview");
  $("demo-state").textContent = `Queued book outcomes: ${demo.forced_book.join(", ") || "none"} · status checks set to time out: ${demo.forced_status_timeouts}`;
}
document.querySelectorAll("[data-next]").forEach((b) => b.addEventListener("click", async () => {
  await api("/demo/next-book", { method: "POST", body: JSON.stringify({ outcome: b.dataset.next }) });
  loadDemo();
}));
$("status-timeouts").addEventListener("click", async () => { await api("/demo/status-timeouts", { method: "POST", body: JSON.stringify({ count: 3 }) }); loadDemo(); });
$("demo-clear").addEventListener("click", async () => { await api("/demo/clear", { method: "POST" }); loadDemo(); });
async function providerCancel(id) {
  try {
    const r = await api("/demo/provider-cancel", { method: "POST", body: JSON.stringify({ booking_id: id }) });
    $("cancel-status").textContent = `${r.cancelled_by_provider} cancelled by provider; ${r.replacements_queued} replacement search(es) queued.`;
  } catch (err) { $("cancel-status").textContent = String(err.message); }
}
$("cancel-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  await providerCancel($("cancel-id").value.trim());
});

// ---------------------------------------------------------------- start
$("gate-form").addEventListener("submit", (e) => {
  e.preventDefault();
  store.set("ba.admin", $("admin-code").value.trim());
  $("gate").close();
  showTab(store.get("ba.admin.tab", "activity"));
});
document.querySelectorAll(".tabs button").forEach((b) => b.addEventListener("click", () => showTab(b.dataset.tab)));
showTab(store.get("ba.admin.tab", "activity"));
setInterval(() => {
  if (!$("tab-activity").hidden) loadActivity().catch(showError);
  if (!$("tab-jobs").hidden) loadJobs().catch(showError);
}, 15000);
