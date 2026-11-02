// BookingAgent chat. Agent text is always inserted with textContent, never as HTML.
"use strict";

const $ = (id) => document.getElementById(id);
const store = {
  get(k, d) { try { return localStorage.getItem(k) ?? d; } catch { return d; } },
  set(k, v) { try { localStorage.setItem(k, v); } catch { /* storage unavailable */ } },
};
let userId = store.get("ba.user", "u1");
let conversationId = store.get("ba.session", newConversationId());
let busy = false;
let liveStream = null; // AbortController of the open event stream
let waitingNote = null; // the "Waiting for the provider…" line, if one is shown

let SERVICE = {};
let TILES = [];
let catalogLoaded = false;
const EXAMPLES = [
  "Find a plumber for my kitchen tap this week, mornings, under ₹1,500 in Koramangala",
  "Move my AC servicing to Saturday",
  "Book the same electrician as last time, not before 10 a.m.",
];

function newConversationId() { return "web-" + Math.random().toString(36).slice(2, 10); }
function decode(text) { const t = document.createElement("textarea"); t.innerHTML = text; return t.value; }
function el(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined && text !== null) n.textContent = String(text);
  return n;
}
function button(cls, text, onClick) {
  const b = el("button", cls, text);
  b.type = "button";
  b.addEventListener("click", onClick);
  return b;
}
const rupees = (p) => "₹" + p.toLocaleString("en-IN");
let IST = { timeZone: "Asia/Kolkata" };
const dayLabel = (iso) => new Date(iso).toLocaleDateString("en-IN", { weekday: "long", day: "numeric", month: "short", ...IST });
const hm = (iso) => new Date(iso).toLocaleTimeString("en-IN", { hour: "numeric", minute: "2-digit", ...IST });

async function api(path, opts = {}) {
  const res = await fetch(path, {
    ...opts,
    headers: { "Content-Type": "application/json", "X-User-Id": userId, "X-Access-Code": store.get("ba.code", ""), ...(opts.headers || {}) },
  });
  if (res.status === 401) { askForCode(); throw new Error("access code required"); }
  if (!res.ok) {
    // The server explains refusals as JSON {detail}; keep it so the caller can show it.
    let detail = null;
    try { const j = await res.json(); if (typeof j.detail === "string") detail = j.detail; } catch { /* no JSON body */ }
    const err = new Error(`${res.status}`);
    err.status = res.status;
    err.detail = detail;
    throw err;
  }
  return res.status === 204 ? null : res.json();
}

function askForCode() {
  if (document.querySelector("dialog.gate")) return;
  const d = el("dialog", "gate");
  const f = el("form", "gate-form");
  f.append(el("h3", "", "Welcome"), el("p", "", "Enter the access code you were given to start booking."));
  const input = el("input");
  input.type = "password"; input.required = true; input.autocomplete = "current-password"; input.id = "access-code";
  input.setAttribute("aria-label", "Access code");
  const go = el("button", "btn-primary", "Continue");
  go.type = "submit";
  f.append(input, go);
  f.addEventListener("submit", async (e) => {
    e.preventDefault();
    store.set("ba.code", input.value.trim());
    d.close();
    try { await openConversation(conversationId); loadProfile(); } catch { /* the gate reopens on 401 */ }
  });
  d.append(f);
  d.addEventListener("close", () => d.remove());
  document.body.append(d);
  d.showModal();
}

// ---------------------------------------------------------------- cards
function chipsFor(groups, container) {
  const wrap = el("div", "chips");
  const finish = (text) => { wrap.classList.add("used"); send(text); };
  // One row of choices: a tap sends at once. Two or more: one pick per row, sent together.
  const choiceRows = groups.filter((g) => (g.choices || []).length).length;
  const multi = choiceRows > 1;
  const picks = new Map(); // row index -> { text, btn }
  let sendBtn = null;
  const selectedText = () => groups.map((_, gi) => picks.get(gi)).filter(Boolean).map((p) => p.text).join(", ");
  for (const [gi, g] of groups.entries()) {
    const row = el("div", "chip-group");
    if (g.label) row.append(el("span", "label", g.label));
    for (const c of g.choices || []) {
      if (!multi) { row.append(button("chip", c, () => finish(c))); continue; }
      const btn = button("chip", c, () => {
        const prev = picks.get(gi);
        if (prev) prev.btn.classList.remove("selected");
        if (prev && prev.btn === btn) picks.delete(gi);
        else { picks.set(gi, { text: c, btn }); btn.classList.add("selected"); }
        if (picks.size === choiceRows) finish(selectedText());
        else if (sendBtn) sendBtn.disabled = picks.size === 0;
      });
      row.append(btn);
    }
    (g.actions || []).forEach((a, i) => row.append(button("chip" + (i === 0 ? " primary" : ""), a.label, () => finish(a.send))));
    wrap.append(row);
  }
  if (multi) {
    sendBtn = button("chip primary", "Send", () => finish(selectedText()));
    sendBtn.disabled = true;
    const sendRow = el("div", "chip-group");
    sendRow.append(sendBtn);
    wrap.append(sendRow);
  }
  container.append(wrap);
}

// card.choices is a list of rows {label?, choices: [text]}; a plain list of strings is one row.
function questionCard(card, box) {
  let groups = Array.isArray(card.choices) ? card.choices : [];
  if (groups.length && typeof groups[0] === "string") groups = [{ choices: groups }];
  groups = groups.filter((g) => g && Array.isArray(g.choices));
  if (groups.length) chipsFor(groups, box);
}

const OFFERS_SHOWN = 5;
const slotLabel = (iso) => new Date(iso).toLocaleDateString("en-IN", { weekday: "short", day: "numeric", month: "short", ...IST }) + ", " + hm(iso);
const pickLabel = (name, iso) => `${name}, ${new Date(iso).toLocaleDateString("en-IN", { weekday: "short", ...IST })} ${new Date(iso).toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit", ...IST })}`;

function offersCard(card) {
  const box = el("section", "card offers");
  const providers = card.providers || [];
  if (!providers.length) { box.append(el("p", "card-note", "No providers had a free slot.")); return box; }
  const list = el("ul", "offer-list");
  const row = (p) => {
    const li = el("li", "offer");
    const top = el("div", "offer-head");
    top.append(el("span", "name", p.name),
      el("span", "meta", [p.rating ? `★ ${p.rating}` : null, rupees(p.price)].filter(Boolean).join(" · ")));
    const slots = el("div", "slot-chips");
    for (const s of p.slots || []) {
      slots.append(button("chip slot", slotLabel(s.start), () => {
        box.classList.add("used");
        send(pickLabel(p.name, s.start), { provider_id: p.provider_id, slot_start: s.start });
      }));
    }
    li.append(top, slots);
    return li;
  };
  providers.slice(0, OFFERS_SHOWN).forEach((p) => list.append(row(p)));
  box.append(list);
  if (providers.length > OFFERS_SHOWN) {
    const more = button("btn-text more", `Show more (${providers.length - OFFERS_SHOWN})`, () => {
      providers.slice(OFFERS_SHOWN).forEach((p) => list.append(row(p)));
      more.remove();
    });
    box.append(more);
  }
  return box;
}

const QUOTE_STATUS = {
  confirmed: "Confirmed",
  done: { book: "Booked", cancel: "Cancelled", reschedule: "Moved" },
  expired: "Expired",
  declined: "Declined",
  failed: "Couldn't be completed",
};
function quoteStatusLabel(card) {
  const v = QUOTE_STATUS[card.status];
  if (v && typeof v === "object") return v[card.action] || "Done";
  return v || String(card.status);
}

function quoteCard(card) {
  const art = el("article", "card quote");
  art.dataset.quote = card.quote_id;
  art.dataset.status = card.status || "shown";

  const head = el("div", "card-head");
  const kicker = { book: `${SERVICE[card.service] || card.service} visit`, cancel: "Cancel this booking?", reschedule: "Move this booking?" }[card.action];
  head.append(el("span", "card-kicker", kicker || ""));
  head.append(el("h3", "card-title", card.provider_name));
  art.append(head);

  const main = el("div", "card-main");
  const when = el("div", "when", dayLabel(card.start));
  when.append(el("small", "", `${hm(card.start)} – ${hm(card.end)}${card.action === "reschedule" ? " (new time)" : ""}`));
  main.append(when, el("div", "price", rupees(card.price)));
  art.append(main);

  // Short review lines from past customers, shown as plain text under the details.
  const reviews = (card.reviews || []).filter((r) => typeof r === "string" && r.trim()).slice(0, 3);
  if (reviews.length) {
    const list = el("ul", "reviews");
    reviews.forEach((r) => list.append(el("li", "review", r)));
    art.append(list);
  }

  if (card.status && card.status !== "shown") {
    art.append(el("p", "card-status " + card.status, quoteStatusLabel(card)));
    return art;
  }
  const actions = el("div", "card-actions");
  const confirmLabel = { book: "Confirm booking", cancel: "Yes, cancel it", reschedule: "Confirm new time" }[card.action] || "Confirm";
  // Card buttons send a structured action; the label is only what the user's bubble shows.
  actions.append(button("btn-primary", confirmLabel, () => send(confirmLabel, null, { type: "confirm", quote_id: card.quote_id, version: card.version })));
  actions.append(button("btn-text", "Not now", () => send("Not now", null, { type: "decline", quote_id: card.quote_id })));
  art.append(actions);
  return art;
}

function confirmationCard(card) {
  const c = el("div", "done");
  const label = { cancelled: "Cancelled", cancelled_by_provider: "Cancelled by provider" }[card.status] || { cancel: "Cancelled", reschedule: "Moved" }[card.action] || "Booked";
  c.append(el("div", "stamp", `✓ ${label}`));
  const detail = [card.service ? SERVICE[card.service] || card.service : null, card.provider_name,
    card.start ? `${dayLabel(card.start)}, ${hm(card.start)}${card.end ? " – " + hm(card.end) : ""}` : null].filter(Boolean).join(" · ");
  if (detail) c.append(el("p", "", detail));
  if (card.price != null) c.append(el("p", "", rupees(card.price)));
  c.append(el("p", "ref", `Reference ${card.booking_id} · confirmed with the provider`));
  return c;
}

// A saved or live message: {role, text, card, at}. Unknown card types show the text only.
function renderMessage(m) {
  const wrap = el("div", "msg " + m.role);
  if (m.role === "user") { wrap.append(el("div", "bubble", m.text)); return wrap; }
  const card = m.card || null;
  const type = card && card.type;
  // A confirmation card carries the details itself, so its text would repeat them.
  if (m.text && type !== "confirmation") wrap.append(el("div", type === "notice" ? "note" : "say", decode(m.text)));
  switch (type) {
    case "offers": wrap.append(offersCard(card)); break;
    case "quote": wrap.append(quoteCard(card)); break;
    case "confirmation": wrap.append(confirmationCard(card)); break;
    case "question": questionCard(card, wrap); break;
    default: break;
  }
  return wrap;
}

function clearWaiting() {
  if (waitingNote) waitingNote.remove();
  waitingNote = null;
}

// "Waiting for the provider…" while the newest quote card is confirmed, or a reply says it was queued.
function updateWaiting(messages, exit) {
  clearWaiting();
  let quoteStatus = null;
  for (let i = messages.length - 1; i >= 0 && !quoteStatus; i--) {
    const c = messages[i].card;
    if (c && c.type === "quote") quoteStatus = c.status;
  }
  if (quoteStatus === "confirmed" || (exit && exit.kind === "queued")) {
    waitingNote = el("div", "note pending", "Waiting for the provider…");
    const box = $("messages");
    const last = box.lastElementChild;
    if (last && last.classList.contains("agent")) last.append(waitingNote); else box.append(waitingNote);
  }
}

function markStale() {
  const msgs = [...document.querySelectorAll(".msg.agent")];
  const last = msgs[msgs.length - 1];
  const older = (c) => !last || !last.contains(c);
  // An open quote that is no longer the newest message was replaced; the server reports other states itself.
  document.querySelectorAll(".card.quote").forEach((c) => c.classList.toggle("stale", c.dataset.status === "shown" && older(c)));
  document.querySelectorAll(".card.offers").forEach((c) => { if (older(c)) c.classList.add("used"); });
  document.querySelectorAll(".chips").forEach((c) => { if (older(c)) c.classList.add("used"); });
}

function welcome() {
  const box = el("section", "welcome");
  box.append(el("h1", "", "What needs fixing?"),
    el("p", "", "Tell me the job, your area and when. I'll find someone, hold a slot and show you the exact price. Nothing is booked until you confirm."));
  const tiles = el("div", "tiles");
  for (const [name, sub, msg] of TILES) {
    const t = el("button", "tile");
    t.type = "button";
    t.append(el("b", "", name), el("span", "", sub));
    t.addEventListener("click", () => send(msg));
    tiles.append(t);
  }
  box.append(tiles);
  const ex = el("div", "examples");
  ex.append(el("span", "hint", "Or try:"));
  EXAMPLES.forEach((e) => ex.append(button("", e, () => send(e))));
  box.append(ex);
  return box;
}

function typing() {
  const t = el("div", "msg agent");
  const d = el("div", "typing");
  d.append(el("i"), el("i"), el("i"), el("span", "", "Checking providers…"));
  t.append(d);
  return t;
}
function scrollDown() { const c = $("conversation"); c.scrollTop = c.scrollHeight; }

// ---------------------------------------------------------------- data
async function loadConversations() {
  const { conversations } = await api("/v1/conversations");
  const ul = $("session-list");
  ul.replaceChildren();
  const list = conversations.some((c) => c.id === conversationId) ? conversations : [{ id: conversationId, title: null }, ...conversations];
  for (const c of list) {
    const li = el("li");
    const b = el("button");
    b.type = "button";
    b.setAttribute("aria-current", String(c.id === conversationId));
    b.append(el("span", "t", c.title || "New conversation"));
    if (c.updated_at) b.append(el("span", "d", new Date(c.updated_at).toLocaleString("en-IN", { dateStyle: "medium", timeStyle: "short" })));
    b.addEventListener("click", () => { closeDrawers(); openConversation(c.id); });
    li.append(b);
    ul.append(li);
  }
}

// Fetches the saved messages and redraws the thread. An unknown conversation id is an empty one.
async function renderThread(id, exit) {
  let messages = [];
  try {
    ({ messages } = await api(`/v1/conversations/${encodeURIComponent(id)}`));
  } catch (e) { if (e.status !== 404) throw e; }
  if (id !== conversationId) return;
  const box = $("messages");
  box.replaceChildren();
  waitingNote = null;
  if (!messages.length) box.append(welcome());
  messages.forEach((m) => box.append(renderMessage(m)));
  updateWaiting(messages, exit);
  markStale();
  scrollDown();
}

async function openConversation(id) {
  conversationId = id;
  store.set("ba.session", id);
  if (!catalogLoaded) {
    const metadata = await api("/v1/catalog");
    SERVICE = metadata.labels || {};
    TILES = metadata.services.map((service) => [SERVICE[service] || service, "Available service", `I need ${SERVICE[service] || service}`]);
    IST = { timeZone: metadata.timezone };
    catalogLoaded = true;
  }
  await renderThread(id);
  loadConversations().catch(() => {});
  watchConversation(id);
}

// ---------------------------------------------------------------- profile and memory
function rowInput(cls, value, label, placeholder) {
  const i = el("input", cls);
  i.value = value || "";
  i.placeholder = placeholder || "";
  i.setAttribute("aria-label", label);
  i.autocomplete = "off";
  return i;
}

async function loadProfile() {
  try {
    const { bookings } = await api("/v1/bookings");
    const ul = $("bookings");
    ul.replaceChildren();
    const rows = bookings.slice().sort((a, b) => a.start.localeCompare(b.start));
    if (!rows.length) ul.append(el("li", "s", "No bookings yet."));
    const STATUS = { booked: "Booked", cancelled: "Cancelled", cancelled_by_provider: "Cancelled by provider" };
    for (const b of rows) {
      const li = el("li");
      li.append(el("div", "", `${SERVICE[b.service] || b.service} · ${b.provider_name}`));
      li.append(el("div", "s", `${dayLabel(b.start)}, ${hm(b.start)}`));
      li.append(el("div", "s " + b.status, STATUS[b.status] || b.status));
      ul.append(li);
    }
  } catch { $("bookings").replaceChildren(el("li", "s", "Couldn't load bookings.")); }
  loadProfileForm();
  loadMemory();
}

async function loadProfileForm() {
  const form = $("profile-form");
  try {
    const p = await api("/v1/profile");
    form.replaceChildren();
    const name = rowInput("", p.name, "Name", "Your name");
    form.append(el("label", "field-label", "Name"), name);

    const phones = el("div", "rows");
    const addPhone = (v) => {
      const r = el("div", "edit-row");
      const i = rowInput("phone", v, "Phone number", "Phone number");
      i.type = "tel";
      r.append(i, button("link-btn", "Remove", () => r.remove()));
      phones.append(r);
    };
    (p.phones || []).forEach(addPhone);
    form.append(el("label", "field-label", "Phone numbers"), phones, button("link-btn add", "Add phone", () => addPhone("")));

    const addrs = el("div", "rows");
    const addAddress = (a) => {
      const r = el("div", "edit-row addr");
      r.append(rowInput("addr-label", a.label, "Address label", "Label, e.g. Home"),
        rowInput("addr-text", a.address, "Address", "Address, including the area"),
        button("link-btn", "Remove", () => r.remove()));
      addrs.append(r);
    };
    (p.addresses || []).forEach(addAddress);
    form.append(el("label", "field-label", "Addresses"), addrs, button("link-btn add", "Add address", () => addAddress({})));

    const status = el("span", "s");
    status.setAttribute("role", "status");
    const save = button("btn-primary", "Save", async () => {
      const body = {
        name: name.value.trim(),
        phones: [...phones.querySelectorAll("input.phone")].map((i) => i.value.trim()).filter(Boolean),
        addresses: [...addrs.querySelectorAll(".edit-row")].map((r) => ({
          label: r.querySelector(".addr-label").value.trim(), address: r.querySelector(".addr-text").value.trim(),
        })).filter((a) => a.address),
      };
      try {
        await api("/v1/profile", { method: "PUT", body: JSON.stringify(body) });
        status.textContent = "Saved.";
      } catch (e) { status.textContent = e.detail || "Couldn't save. Try again."; }
    });
    const foot = el("div", "form-foot");
    foot.append(save, status);
    form.append(foot);
  } catch { form.replaceChildren(el("p", "s", "Couldn't load your profile.")); }
}

const memoryValue = (v) => (typeof v === "string" ? v : JSON.stringify(v));

async function loadMemory() {
  const ul = $("memory");
  try {
    const { memory } = await api("/v1/memory");
    ul.replaceChildren();
    const keys = Object.keys(memory);
    if (!keys.length) ul.append(el("li", "s", "Nothing yet. Say things like “I prefer mornings”."));
    for (const key of keys) {
      const m = memory[key];
      const li = el("li");
      const row = el("div", "row");
      const text = el("span", "fact", `${key}: ${memoryValue(m.value)}`);
      const acts = el("span", "acts");
      const edit = button("", "Edit", () => {
        const input = rowInput("fact-edit", memoryValue(m.value), "Memory value");
        text.replaceWith(input);
        input.focus();
        acts.replaceChildren(
          button("", "Save", async () => {
            const v = input.value.trim();
            if (!v) return;
            let value = v;
            try { value = JSON.parse(v); } catch { /* plain text value */ }
            try { await api(`/v1/memory/${encodeURIComponent(key)}`, { method: "PUT", body: JSON.stringify({ value }) }); } catch { /* list reloads below */ }
            loadMemory();
          }),
          button("", "Cancel", loadMemory));
      });
      const del = button("danger", "Delete", async () => {
        try { await api(`/v1/memory/${encodeURIComponent(key)}` + (conversationId ? `?conversation_id=${encodeURIComponent(conversationId)}` : ""), { method: "DELETE" }); } catch { /* list reloads below */ }
        loadMemory();
      });
      acts.append(edit, del);
      row.append(text, acts);
      li.append(row);
      if (!m.confirmed) li.append(el("div", "s", "Not confirmed yet"));
      ul.append(li);
    }
  } catch { ul.replaceChildren(el("li", "s", "Couldn't load memory.")); }
}

// ---------------------------------------------------------------- sending
// `pickSlot` is {provider_id, slot_start}. `action` is a card button ({type: "confirm", quote_id, version}
// or {type: "decline", quote_id}). Only actions confirm or decline; typed text always goes to the model.
// `text` is what the user's bubble shows; it is sent as `message` only when no slot or action is attached.
async function send(text, pickSlot = null, action = null) {
  if (busy || !text.trim()) return;
  busy = true;
  $("send").disabled = true;
  const box = $("messages");
  box.querySelector(".welcome")?.remove();
  box.append(renderMessage({ role: "user", text }));
  const wait = typing();
  box.append(wait);
  markStale();
  scrollDown();
  const body = { conversation_id: conversationId };
  if (pickSlot) body.pick_slot = pickSlot;
  else if (action) body.action = action;
  else body.message = text;
  let reply = null;
  try {
    reply = await api("/v1/chat", { method: "POST", body: JSON.stringify(body) });
    wait.remove();
  } catch (e) {
    const m = el("div", "msg agent");
    const msg = e.message === "access code required" ? "Enter the access code to continue."
      : e.detail ? e.detail : "That didn't reach me. Check your connection and try again.";
    m.append(el("div", "note stop", msg));
    wait.replaceWith(m);
  }
  busy = false;
  $("send").disabled = false;
  if (reply) {
    // The saved thread is the source of truth (it also refreshes the status of earlier cards).
    // If it can't be loaded, show the reply itself.
    try { await renderThread(conversationId, reply.exit); } catch {
      box.append(renderMessage({ role: "agent", text: reply.text, card: reply.card || null }));
      updateWaiting([{ card: reply.card }], reply.exit);
    }
  }
  markStale();
  scrollDown();
  loadConversations().catch(() => {});
}

// ---------------------------------------------------------------- live updates
// EventSource cannot send the X-User-Id and X-Access-Code headers, so the stream is read with fetch.
function pause(ms, signal) {
  return new Promise((resolve) => {
    const timer = setTimeout(resolve, ms);
    signal.addEventListener("abort", () => { clearTimeout(timer); resolve(); }, { once: true });
  });
}

// One SSE frame: lines of "field: value", comment lines start with ":". Returns null if it has no data.
function parseFrame(frame) {
  let type = "message";
  const data = [];
  for (const line of frame.split("\n")) {
    if (line.startsWith(":")) continue;
    const colon = line.indexOf(":");
    const field = colon < 0 ? line : line.slice(0, colon);
    const value = colon < 0 ? "" : line.slice(colon + 1).replace(/^ /, "");
    if (field === "event") type = value;
    else if (field === "data") data.push(value);
  }
  if (!data.length) return null;
  try { return { type, data: JSON.parse(data.join("\n")) }; } catch { return null; }
}

async function readFrames(body, onEvent) {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { done, value } = await reader.read();
    if (done) return;
    buffer += decoder.decode(value, { stream: true }).replace(/\r\n/g, "\n");
    let end = buffer.indexOf("\n\n");
    while (end >= 0) {
      const event = parseFrame(buffer.slice(0, end));
      buffer = buffer.slice(end + 2);
      if (event) onEvent(event);
      end = buffer.indexOf("\n\n");
    }
  }
}

// Redraws the thread from the saved messages unless a send is in flight (its reply redraws it).
async function refreshThread(id) {
  if (busy || id !== conversationId) return;
  await renderThread(id);
  loadConversations().catch(() => {});
}

function onLiveEvent(id, event) {
  if (event.type === "conversation.updated" || event.type === "session.updated") refreshThread(id).catch(() => {});
}

async function streamLoop(id, signal) {
  let delay = 1000;
  let connected = false;
  while (!signal.aborted) {
    try {
      const res = await fetch(`/v1/conversations/${encodeURIComponent(id)}/events`, {
        signal,
        headers: { "X-User-Id": userId, "X-Access-Code": store.get("ba.code", "") },
      });
      if (res.status === 401) { askForCode(); return; }
      if (!res.ok || !res.body) throw new Error(`${res.status}`);
      delay = 1000;
      // Events are not replayed, so after a reconnect look for messages saved while the stream was down.
      if (connected) refreshThread(id).catch(() => {});
      connected = true;
      await readFrames(res.body, (event) => onLiveEvent(id, event));
    } catch { /* aborted, offline or server restart: reconnect below */ }
    if (signal.aborted) return;
    await pause(delay, signal);
    delay = Math.min(delay * 2, 15000);
  }
}

function watchConversation(id) {
  if (liveStream) liveStream.abort();
  liveStream = new AbortController();
  streamLoop(id, liveStream.signal);
}

// ---------------------------------------------------------------- drawers and wiring
function openDrawer(id) { $(id).hidden = false; $("scrim").hidden = false; if (id === "profile") loadProfile(); }
function closeDrawers() { $("history").hidden = true; $("profile").hidden = true; $("scrim").hidden = true; }

function init() {
  $("user-id").value = userId;
  $("user-id").addEventListener("change", (e) => {
    userId = e.target.value.trim() || "u1";
    store.set("ba.user", userId);
    closeDrawers();
    openConversation(newConversationId()).catch(() => {});
  });
  const autosize = () => { const t = $("input"); t.style.height = "auto"; t.style.height = Math.min(t.scrollHeight, 160) + "px"; };
  $("composer").addEventListener("submit", (e) => { e.preventDefault(); const v = $("input").value; $("input").value = ""; autosize(); send(v); });
  $("input").addEventListener("input", autosize);
  $("input").addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); $("composer").requestSubmit(); } });
  $("new-chat").addEventListener("click", () => openConversation(newConversationId()).catch(() => {}));
  $("open-history").addEventListener("click", () => openDrawer("history"));
  $("open-profile").addEventListener("click", () => openDrawer("profile"));
  $("scrim").addEventListener("click", closeDrawers);
  document.querySelectorAll("[data-close]").forEach((b) => b.addEventListener("click", closeDrawers));
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeDrawers(); });
  openConversation(conversationId).catch(() => {});
}
init();
