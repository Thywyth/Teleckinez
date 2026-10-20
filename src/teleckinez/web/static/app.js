/* Teleckinez web panel (docs/specs/web.md). Plain JS, no build step, works offline.
 * Loads /api/devices once, then lives on /ws events from the assistant's EventBus. */
"use strict";

const $ = (sel, root = document) => root.querySelector(sel);
const el = (tag, attrs = {}, ...children) => {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") node.className = v;
    else if (k === "dataset") Object.assign(node.dataset, v);
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else if (v === true) node.setAttribute(k, "");
    else if (v !== false && v != null) node.setAttribute(k, v);
  }
  for (const c of children.flat()) if (c != null) node.append(c instanceof Node ? c : String(c));
  return node;
};
const fmt = (v) => (typeof v === "number" ? String(Math.round(v * 10) / 10).replace(".", ",") : v);

const state = {
  rooms: [],
  devices: new Map(),      // id -> device from /api/devices, kept up to date by events
  pending: new Map(),      // device id -> Set(cmd_id) of running commands
  status: {},
  listeners: [],           // (event) => void, other views subscribe here
};

// ------------------------------------------------------------------------- api
async function api(path, options = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
    body: options.body ? JSON.stringify(options.body) : undefined,
  });
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch { /* not json */ }
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  return res.json();
}

function toast(text, kind = "") {
  const node = el("div", { class: `toast ${kind}` }, text);
  $("#toasts").append(node);
  setTimeout(() => node.remove(), kind === "bad" ? 6000 : 3500);
}

// ------------------------------------------------------------------- websocket
let socket = null;
function connect() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  socket = new WebSocket(`${proto}://${location.host}/ws`);
  socket.onopen = () => { setChip("#chip-ws", "панель", "ok"); refresh(); };
  socket.onclose = () => { setChip("#chip-ws", "панель: немає зв'язку", "bad"); setTimeout(connect, 1500); };
  socket.onmessage = (msg) => {
    const event = JSON.parse(msg.data);
    handleEvent(event);
    for (const fn of state.listeners) fn(event);
  };
}

function setChip(sel, text, kind) {
  const chip = $(sel);
  chip.textContent = text;
  chip.className = `chip ${kind || ""}`;
}

async function refresh() {
  const [data, status] = await Promise.all([api("/api/devices"), api("/api/status")]);
  state.rooms = data.rooms;
  state.devices = new Map(data.devices.map((d) => [d.id, d]));
  state.status = status;
  renderDashboard();
  renderStatus();
}

function renderStatus() {
  const online = [...state.devices.values()].filter((d) => d.availability === "online").length;
  const total = state.devices.size;
  setChip("#chip-online", `${online}/${total} на зв'язку`, online === total ? "ok" : online ? "warn" : "bad");
  setChip("#chip-broker", state.status.broker_connected ? "брокер" : "брокер: немає зв'язку",
    state.status.broker_connected ? "ok" : "bad");
}

function handleEvent({ type, data }) {
  const dev = data.device_id && state.devices.get(data.device_id);
  if (type === "DeviceStateChanged" && dev) {
    dev.reported = data.reported;
    dev.age_s = 0;
    updateCard(dev.id);
  } else if (type === "DeviceAvailabilityChanged" && dev) {
    dev.availability = data.availability;
    updateCard(dev.id);
    renderStatus();
  } else if (type === "CommandCreated" && dev) {
    if (!state.pending.has(dev.id)) state.pending.set(dev.id, new Set());
    state.pending.get(dev.id).add(data.cmd_id);
    dev.desired = { ...(dev.desired || {}), ...data.desired };
    updateCard(dev.id);
  } else if (type === "CommandFinished" && dev) {
    const set = state.pending.get(dev.id);
    if (set) set.delete(data.cmd_id);
    if (!set || set.size === 0) dev.desired = null;
    dev.lastError = data.status === "completed" ? null : statusText(data.status, data.error);
    updateCard(dev.id, data.status === "completed" ? "flash-ok" : "flash-bad");
  }
}

const STATUS_UK = {
  completed: "виконано", rejected: "пристрій відхилив команду",
  rejected_local: "команду не відправлено", timeout_ack: "пристрій не відповідає",
  timeout_state: "прийнято, але стан не змінився", device_offline: "пристрій не в мережі",
  superseded: "замінено новішою командою", published: "надіслано", accepted: "прийнято",
  retrying: "повтор", created: "створено",
};
const ERROR_UK = {
  value_out_of_range: "значення поза межами", unsupported_action: "дія не підтримується",
  broker_unavailable: "немає зв'язку з брокером", no_ack: "немає підтвердження",
  state_not_reached: "стан не досягнуто", device_offline: "не в мережі",
};
function statusText(status, error) {
  const base = STATUS_UK[status] || status;
  const why = error && !error.startsWith("superseded_by") ? ERROR_UK[error] || error : "";
  return why && why !== base ? `${base} (${why})` : base;
}

// ------------------------------------------------------------------- commands
async function command(id, action, params = {}) {
  const dev = state.devices.get(id);
  if (dev.critical && ["unlock", "open", "toggle"].includes(action)) {
    if (!confirm(`«${dev.title}» — критичний пристрій. Виконати?`)) return;
  }
  try {
    const result = await api(`/api/devices/${encodeURIComponent(id)}/command`, {
      method: "POST", body: { action, params, wait: true },
    });
    if (result.status !== "completed") toast(result.response || statusText(result.status, result.error), "bad");
  } catch (err) {
    toast(`Помилка: ${err.message}`, "bad");
  }
}

// ------------------------------------------------------------------ dashboard
const ICONS = {
  light: "💡", rgb_light: "🌈", curtains: "🪟", thermostat: "🌡️", air_conditioner: "❄️",
  socket: "🔌", kettle: "☕", boiler: "♨️", fan: "🌀", door_lock: "🔒", garage_gate: "🚗",
  robot_vacuum: "🧹", tv: "📺", humidifier: "💧", climate_sensor: "🌡️", motion_sensor: "🚶",
  door_sensor: "🚪",
};
const COLORS = {
  white: "#f5f5f0", warm: "#ffd9a0", red: "#ff4d4d", orange: "#ff9a3c", yellow: "#ffe14d",
  green: "#47d16c", cyan: "#4dd8e6", blue: "#4d7dff", purple: "#a35cff", pink: "#ff7ac6",
};
const MODES = { cool: "охолодження", heat: "обігрів", fan: "вентиляція", dry: "осушення", auto: "авто" };
const VACUUM = { docked: "на базі", cleaning: "прибирає", returning: "повертається" };
const TYPE_UK = {
  light: "світло", rgb_light: "кольорова лампа", curtains: "штори", thermostat: "термостат",
  air_conditioner: "кондиціонер", socket: "розетка", kettle: "чайник", boiler: "бойлер",
  fan: "вентилятор", door_lock: "замок", garage_gate: "ворота", robot_vacuum: "пилосос",
  tv: "телевізор", humidifier: "зволожувач", climate_sensor: "датчик клімату",
  motion_sensor: "датчик руху", door_sensor: "датчик дверей",
};
const ATTR_UK = {
  on: (v) => (v ? "увімкнено" : "вимкнено"), brightness: (v) => `яскравість ${v} %`,
  color: (v) => `колір ${v}`, position: (v) => `положення ${v} %`, locked: (v) => (v ? "зачинено" : "відчинено"),
  target_temp: (v) => `${fmt(v)} °C`, mode: (v) => MODES[v] || v, speed: (v) => `швидкість ${v}`,
  level: (v) => `рівень ${v}`, volume: (v) => `гучність ${v}`, status: (v) => VACUUM[v] || v,
};
const describeDesired = (desired) =>
  Object.entries(desired).map(([k, v]) => (ATTR_UK[k] ? ATTR_UK[k](v) : `${k}=${v}`)).join(", ");
const AVAIL = { online: "на зв'язку", stale: "мовчить (немає heartbeat)", offline: "не в мережі", unknown: "невідомо" };

function renderDashboard() {
  const root = $("#rooms");
  root.replaceChildren();
  for (const room of state.rooms) {
    const devices = [...state.devices.values()].filter((d) => d.room === room.id);
    const grid = el("div", { class: "grid" }, devices.map((d) => el("div", { id: cardId(d.id) })));
    root.append(el("section", { class: "room" },
      el("h2", { class: "room-title" }, room.name, el("span", { class: "count" }, `${devices.length}`)),
      grid));
    devices.forEach((d) => updateCard(d.id));
  }
}

const cardId = (id) => `card-${id.replace(/\./g, "_")}`;

function updateCard(id, flash) {
  const holder = document.getElementById(cardId(id));
  const dev = state.devices.get(id);
  if (!holder || !dev) return;
  if (holder.contains(document.activeElement) && document.activeElement.type === "range") {
    holder.dataset.dirty = "1"; // do not yank a slider the user is dragging
    return;
  }
  const card = buildCard(dev);
  if (flash) card.classList.add(flash);
  holder.replaceChildren(card);
}

function buildCard(d) {
  const r = d.reported || {};
  const pending = (state.pending.get(d.id)?.size || 0) > 0;
  const on = r.on === true || r.status === "cleaning" || (r.position > 0 && r.position !== undefined);
  const card = el("article", {
    class: ["card", on ? "is-on" : "", pending ? "pending" : "", d.availability === "offline" ? "offline" : ""].join(" "),
    title: `${d.id} · ${AVAIL[d.availability] || d.availability}`,
  });
  const icon = d.type === "door_lock" ? (r.locked === false ? "🔓" : "🔒") : ICONS[d.type] || "🔧";
  card.append(el("div", { class: "card-head" },
    el("div", { class: "icon" }, icon),
    el("div", {}, el("div", { class: "card-title" }, d.title), el("div", { class: "card-sub" }, describeAge(d))),
    el("i", { class: `dot ${d.availability}`, title: AVAIL[d.availability] })));
  card.append(el("div", { class: "state-line" }, ...stateLine(d, r, pending)));
  const controls = buildControls(d, r);
  if (controls) card.append(controls);
  if (d.lastError) card.append(el("div", { class: "card-error" }, `Остання команда: ${d.lastError}`));
  return card;
}

function describeAge(d) {
  if (d.availability === "offline") return "не в мережі";
  if (d.availability === "stale") return "немає heartbeat";
  const kind = TYPE_UK[d.type] || d.type;
  return d.critical ? `${kind} · критичний` : kind;
}

function stateLine(d, r, pending) {
  if (!Object.keys(r).length) return ["немає даних"];
  const parts = [];
  if ("on" in r) parts.push(r.on ? "увімкнено" : "вимкнено");
  if (r.on !== false) {
    if ("brightness" in r) parts.push(`${r.brightness} %`);
    if ("color" in r && d.type === "rgb_light") parts.push(r.color);
    if ("mode" in r) parts.push(MODES[r.mode] || r.mode);
    if ("speed" in r) parts.push(`швидкість ${r.speed}`);
    if ("level" in r) parts.push(`рівень ${r.level}`);
    if ("volume" in r) parts.push(`гучність ${r.volume}`);
    if ("power_w" in r && r.power_w) parts.push(`${fmt(r.power_w)} Вт`);
  }
  if ("position" in r) parts.push(r.moving ? `рухається… ${r.position} %` : r.position >= 100 ? "відкрито" : r.position <= 0 ? "закрито" : `відкрито на ${r.position} %`);
  if ("locked" in r) parts.push(r.locked ? "зачинено" : "відчинено");
  if ("status" in r) parts.push(`${VACUUM[r.status] || r.status}, заряд ${r.battery} %`);
  if ("current_temp" in r) parts.push(`у приміщенні ${fmt(r.current_temp)} °C${r.heating ? ", гріє" : ""}`);
  if ("water_temp" in r) parts.push(`вода ${fmt(r.water_temp)} °C`);
  if ("motion" in r) parts.push(r.motion ? "є рух" : "руху немає");
  const line = [el("b", {}, parts.join(" · ") || "—")];
  if (pending && d.desired) line.push(" → ", el("span", {}, `очікую: ${describeDesired(d.desired)}`));
  return line;
}

function rangeControl(label, value, min, max, unit, onCommit) {
  const output = el("output", {}, `${value}${unit}`);
  const input = el("input", {
    type: "range", min, max, value,
    oninput: (e) => { output.textContent = `${e.target.value}${unit}`; },
    onchange: (e) => onCommit(Number(e.target.value)),
  });
  return el("label", { class: "range" }, label, input, output);
}

function buildControls(d, r) {
  const box = el("div", { class: "controls" });
  const disabled = d.availability === "offline";
  const btn = (text, action, params, cls = "btn") =>
    el("button", { class: cls, disabled, onclick: () => command(d.id, action, params) }, text);
  const caps = d.capabilities;

  if (caps.on && !caps.on.readonly) {
    const input = el("input", { type: "checkbox", disabled, onchange: (e) => command(d.id, e.target.checked ? "turn_on" : "turn_off") });
    input.checked = !!r.on;
    box.append(el("label", { class: "switch", title: "Увімкнути / вимкнути" }, input, el("span")));
  }
  switch (d.type) {
    case "light":
    case "rgb_light":
      box.append(rangeControl("☀", r.brightness ?? 100, 1, 100, " %", (v) => command(d.id, "set", { brightness: v })));
      if (d.type === "rgb_light") {
        box.append(el("div", { class: "swatches" }, Object.entries(COLORS).map(([name, hex]) =>
          el("button", { class: `swatch ${r.color === name ? "active" : ""}`, style: `background:${hex}`, title: name, disabled,
            onclick: () => command(d.id, "set", { color: name }) }))));
      }
      break;
    case "curtains":
    case "garage_gate": {
      const bar = el("div", { class: `position ${r.moving ? "moving" : ""}` }, el("div", { style: `width:${r.position ?? 0}%` }));
      box.append(el("div", { style: "width:100%" }, bar));
      box.append(btn("Відкрити", "open"), btn("Закрити", "close"));
      box.append(rangeControl("", r.position ?? 0, 0, 100, " %", (v) => command(d.id, "set", { position: v })));
      break;
    }
    case "thermostat":
    case "air_conditioner": {
      const t = r.target_temp ?? 21;
      box.append(btn("−", "set", { target_temp: Math.max(16, t - 1) }),
        el("span", { class: "big-value" }, fmt(t), el("small", {}, " °C")),
        btn("+", "set", { target_temp: Math.min(30, t + 1) }));
      if (d.type === "air_conditioner") {
        const select = el("select", { class: "btn", disabled, onchange: (e) => command(d.id, "set", { mode: e.target.value }) },
          Object.entries(MODES).map(([k, v]) => el("option", { value: k, selected: r.mode === k }, v)));
        box.append(select);
      }
      break;
    }
    case "fan":
    case "humidifier": {
      const key = d.type === "fan" ? "speed" : "level";
      box.append(el("div", { class: "seg" }, [1, 2, 3].map((n) =>
        el("button", { class: `btn ${r[key] === n && r.on ? "active" : ""}`, disabled, onclick: () => command(d.id, "set", { [key]: n }) }, n))));
      break;
    }
    case "tv":
      box.append(rangeControl("🔊", r.volume ?? 20, 0, 100, "", (v) => command(d.id, "set", { volume: v })));
      break;
    case "door_lock":
      box.append(btn("Зачинити", "lock", {}, r.locked ? "btn" : "btn primary"), btn("Відчинити", "unlock"));
      break;
    case "robot_vacuum":
      box.append(btn("Прибрати", "start", {}, "btn primary"), btn("На базу", "dock"));
      break;
    case "kettle":
      box.append(el("span", { class: "big-value" }, fmt(r.water_temp ?? 20), el("small", {}, " °C")));
      box.append(btn("Закип'ятити", "turn_on", {}, "btn primary"));
      break;
    case "boiler":
      box.append(el("span", { class: "big-value" }, fmt(r.water_temp ?? 0), el("small", {}, " °C")));
      break;
    case "climate_sensor":
      box.append(el("span", { class: "big-value" }, fmt(r.temperature ?? 0), el("small", {}, " °C")),
        el("span", { class: "big-value" }, fmt(r.humidity ?? 0), el("small", {}, " %")));
      break;
    default:
      break;
  }
  return box.childElementCount ? box : null;
}

document.addEventListener("focusout", (e) => {
  const holder = e.target.closest?.("[id^='card-']");
  if (holder?.dataset.dirty) {
    delete holder.dataset.dirty;
    const id = [...state.devices.keys()].find((k) => cardId(k) === holder.id);
    if (id) updateCard(id);
  }
});

// ----------------------------------------------------------------------- tabs
function showView(name) {
  document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t.dataset.view === name));
  document.querySelectorAll(".view").forEach((v) => v.classList.toggle("active", v.id === `view-${name}`));
  try { localStorage.setItem("tkz-view", name); } catch { /* private mode */ }
}
document.querySelectorAll(".tab").forEach((t) => t.addEventListener("click", () => showView(t.dataset.view)));

// ages tick locally between heartbeats
setInterval(() => { for (const d of state.devices.values()) if (d.age_s != null) d.age_s += 1; }, 1000);

connect();
