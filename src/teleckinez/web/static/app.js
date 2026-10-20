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
  const [data, status, faults] = await Promise.all([
    api("/api/devices"), api("/api/status"), api("/api/sim/faults").catch(() => ({ faults: {} })),
  ]);
  faultsState.faults = faults.faults || {};
  state.rooms = data.rooms;
  state.devices = new Map(data.devices.map((d) => [d.id, d]));
  state.status = status;
  renderDashboard();
  renderStatus();
  renderVoicePanel();
  // views that list devices may have been opened (restored tab) before the data arrived
  const active = document.querySelector(".view.active")?.id;
  if (active === "view-faults") renderFaults();
  if (active === "view-journal") { renderJournalShell(); renderJournal(); }
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
  device_error: "внутрішня помилка пристрою",
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
const COLOR_UK = {
  white: "білий", warm: "теплий", red: "червоний", orange: "помаранчевий", yellow: "жовтий",
  green: "зелений", cyan: "блакитний", blue: "синій", purple: "фіолетовий", pink: "рожевий",
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
  const glow = (d.type === "light" || d.type === "rgb_light") && r.on
    ? (d.type === "rgb_light" ? COLORS[r.color] || COLORS.warm : COLORS.warm) : null;
  const card = el("article", {
    class: ["card", `type-${d.type}`, on ? "is-on" : "", glow ? "glow" : "", pending ? "pending" : "",
      d.availability === "offline" ? "offline" : ""].join(" "),
    title: `${d.id} · ${AVAIL[d.availability] || d.availability}`,
    style: glow ? `--glow:${glow}; --glow-strength:${r.brightness ?? 100}%` : null,
  });
  const icon = d.type === "door_lock" ? (r.locked === false ? "🔓" : "🔒") : ICONS[d.type] || "🔧";
  card.append(el("div", { class: "card-head" },
    el("div", { class: "icon" }, icon),
    el("div", {}, el("div", { class: "card-title" }, d.title), el("div", { class: "card-sub" }, describeAge(d))),
    el("i", { class: `dot ${d.availability}`, title: AVAIL[d.availability] })));
  card.append(el("div", { class: "state-line" }, ...stateLine(d, r, pending)));
  const badges = faultBadges(d.id);
  if (badges.length) card.append(el("div", { class: "fault-line" }, badges));
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
    if ("color" in r && d.type === "rgb_light") parts.push(COLOR_UK[r.color] || r.color);
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
  if ("temperature" in r) parts.push("температура · вологість");
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

/// ---------------------------------------------------------------- voice panel
const PIPELINE = {
  idle: ["Очікування", "скажіть «Телекінез» або натисніть кнопку"],
  listening: ["Слухаю…", "говоріть"],
  recognizing: ["Розпізнаю…", "перетворюю мову на текст"],
  processing: ["Обробляю…", "розбір наміру і виконання команди"],
  speaking: ["Відповідаю…", ""],
};
const REASONS_UK = {
  not_understood: "не зрозумів", low_confidence: "низька впевненість", unknown_device: "невідомий пристрій",
  ambiguous_target: "неоднозначно: назвіть кімнату", ambiguous_device: "неоднозначно: кілька пристроїв",
  not_in_room: "немає в цій кімнаті", unsupported_action: "дія не підтримується",
};
const voice = { state: "idle", history: [], idleTimer: null };

function setPipeline(next) {
  voice.state = next;
  clearTimeout(voice.idleTimer);
  if (next === "speaking" && !state.status.voice_available) {
    voice.idleTimer = setTimeout(() => setPipeline("idle"), 1200); // text mode: no TTS playback
  }
  renderPipeline();
}

function renderVoicePanel() {
  const panel = $("#voice-panel");
  const pttDisabled = !state.status.voice_available;
  const ptt = el("button", {
    class: "ptt", id: "ptt", disabled: pttDisabled,
    title: pttDisabled ? "Голосовий конвеєр не запущено (етап 2 ROADMAP)" : "Тримайте, поки говорите",
  }, el("span", { class: "ptt-icon" }, "🎙"), el("span", {}, "Натисни й говори"));
  const press = (pressed) => {
    if (ptt.disabled || ptt.classList.contains("held") === pressed) return;
    ptt.classList.toggle("held", pressed);
    api("/api/voice/ptt", { method: "POST", body: { pressed } }).catch((e) => toast(e.message, "bad"));
  };
  ptt.addEventListener("pointerdown", () => press(true));
  ptt.addEventListener("pointerup", () => press(false));
  ptt.addEventListener("pointerleave", () => press(false));

  const input = el("input", {
    type: "text", id: "text-input", autocomplete: "off",
    placeholder: "Напишіть команду, напр. «увімкни світло на кухні»",
  });
  const form = el("form", {
    class: "text-form",
    onsubmit: async (e) => {
      e.preventDefault();
      const text = input.value.trim();
      if (!text) return;
      input.value = "";
      try { await api("/api/text-command", { method: "POST", body: { text } }); }
      catch (err) { toast(`Помилка: ${err.message}`, "bad"); }
    },
  }, input, el("button", { class: "btn primary", type: "submit" }, "Надіслати"));

  const hint = pttDisabled
    ? el("p", { class: "hint" }, "Голосовий режим ще не підключено — працює текстовий ввід, той самий шлях, що й голос.")
    : null;
  panel.replaceChildren(
    el("div", { class: "panel-card" }, el("div", { class: "pipeline", id: "pipeline" }), ptt, hint, form),
    el("div", { class: "panel-card" }, el("h2", {}, "Останні запити"), el("div", { id: "history", class: "history" })),
  );
  renderPipeline();
  renderHistory();
}

function renderPipeline() {
  const box = $("#pipeline");
  if (!box) return;
  const [title, sub] = PIPELINE[voice.state];
  const steps = ["listening", "recognizing", "processing", "speaking"];
  box.className = `pipeline state-${voice.state}`;
  box.replaceChildren(
    el("div", { class: "pulse" }),
    el("div", {}, el("div", { class: "pipeline-title" }, title), el("div", { class: "pipeline-sub" }, sub)),
    el("ol", { class: "steps" }, steps.map((s) =>
      el("li", { class: s === voice.state ? "on" : "" }, PIPELINE[s][0].replace("…", "")))),
  );
}

function renderHistory() {
  const box = $("#history");
  if (!box) return;
  if (!voice.history.length) {
    box.replaceChildren(el("p", { class: "muted" }, "Ще нічого не сказано."));
    return;
  }
  box.replaceChildren(...voice.history.map((h) => {
    if (h.notification) {
      return el("div", { class: "exchange notification" },
        el("span", { class: "who" }, "сповіщення"), el("div", {}, h.notification));
    }
    const conf = h.confidence != null ? `впевненість ${Math.round(h.confidence * 100)} %` : null;
    const meta = [h.engine === "text" ? "текстом" : h.engine, conf].filter(Boolean).join(" · ");
    return el("div", { class: "exchange" },
      el("div", { class: "said" }, el("span", { class: "who" }, "ви"), h.text, el("div", { class: "meta" }, meta)),
      h.intent ? el("div", { class: "intent" }, intentChips(h.intent)) : null,
      h.failed ? el("div", { class: "intent failed" }, REASONS_UK[h.failed] || h.failed) : null,
      h.progress.map((p) => el("div", { class: "reply progress" }, p)),
      h.response
        ? el("div", { class: "reply" }, el("span", { class: "who" }, "асистент"), h.response)
        : el("div", { class: "reply muted" }, "…"),
    );
  }));
}

function intentChips(i) {
  const room = i.room && (state.rooms.find((r) => r.id === i.room)?.name || i.room);
  const chips = [
    i.kind === "query" ? "запит стану" : `дія: ${i.action || "за замовчуванням"}`,
    i.device_name && `пристрій: ${i.device_name}`,
    !i.device_name && i.device_type && `тип: ${TYPE_UK[i.device_type] || i.device_type}`,
    room && `кімната: ${room}`,
    i.group && `група: ${i.group === "all" ? "усі" : i.group}`,
    Object.keys(i.params || {}).length && `параметри: ${JSON.stringify(i.params)}`,
    `${i.parser} · ${Math.round(i.confidence * 100)} %`,
  ].filter(Boolean);
  return chips.map((c) => el("span", { class: "ichip" }, c));
}

const exchange = (sessionId) => voice.history.find((h) => h.session === sessionId);

state.listeners.push(({ type, data }) => {
  switch (type) {
    case "WakeDetected":
    case "ListeningStarted": setPipeline("listening"); break;
    case "SpeechEnded": setPipeline("recognizing"); break;
    case "TranscriptReady":
      voice.history.unshift({
        session: data.session_id, text: data.text, engine: data.engine,
        confidence: data.engine === "text" ? null : data.confidence, progress: [],
      });
      voice.history = voice.history.slice(0, 8);
      setPipeline("processing");
      break;
    case "IntentParsed": { const h = exchange(data.session_id); if (h) h.intent = data.intent; break; }
    case "IntentFailed": { const h = exchange(data.session_id); if (h) h.failed = data.reason; break; }
    case "SpeakRequested": {
      if (data.kind === "notification") {
        voice.history.unshift({ notification: data.text, progress: [] });
        voice.history = voice.history.slice(0, 8);
        toast(`🔔 ${data.text}`, "bad");
        break;
      }
      const h = exchange(data.session_id);
      if (h) { if (data.final) h.response = data.text; else h.progress.push(data.text); }
      setPipeline("speaking");
      break;
    }
    case "SpeakFinished": setPipeline("idle"); break;
    default: return;
  }
  renderHistory();
});

// -------------------------------------------------------------------- journal
const journal = { rows: [], device: "", outcome: "", timer: null, loaded: false };
const OUTCOME = {
  "": "усі",
  ok: "виконані",
  failed: "збої",
  running: "у процесі",
};
const RUNNING = new Set(["created", "published", "retrying", "accepted"]);

function renderJournalShell() {
  const view = $("#view-journal");
  const devSelect = el("select", { class: "btn", onchange: (e) => { journal.device = e.target.value; loadJournal(); } },
    el("option", { value: "" }, "усі пристрої"),
    [...state.devices.values()].map((d) => el("option", { value: d.id, selected: journal.device === d.id }, d.title)));
  const outSelect = el("select", { class: "btn", onchange: (e) => { journal.outcome = e.target.value; renderJournal(); } },
    Object.entries(OUTCOME).map(([k, v]) => el("option", { value: k, selected: journal.outcome === k }, v)));
  view.replaceChildren(
    el("div", { class: "toolbar" }, el("h1", {}, "Журнал команд"),
      el("div", { class: "filters" }, devSelect, outSelect,
        el("button", { class: "btn", onclick: loadJournal }, "Оновити"))),
    el("div", { id: "journal-stats", class: "stats" }),
    el("div", { class: "table-wrap" }, el("table", { class: "journal" },
      el("thead", {}, el("tr", {},
        ["Час", "Пристрій", "Команда", "Статус", "Спроби", "→ publish", "→ ack", "Разом", "Помилка"].map((h) => el("th", {}, h)))),
      el("tbody", { id: "journal-body" }))),
  );
}

async function loadJournal() {
  const query = new URLSearchParams({ limit: "200" });
  if (journal.device) query.set("device_id", journal.device);
  try {
    journal.rows = await api(`/api/commands?${query}`);
    journal.loaded = true;
    renderJournal();
  } catch (err) {
    toast(`Журнал: ${err.message}`, "bad");
  }
}

const ms = (a, b) => (a != null && b != null ? Math.round((b - a) * 1000) : null);
const clock = (ts) => new Date(ts * 1000).toLocaleTimeString("uk-UA", { hour12: false });
const median = (xs) => {
  if (!xs.length) return null;
  const s = [...xs].sort((a, b) => a - b);
  return s.length % 2 ? s[(s.length - 1) / 2] : (s[s.length / 2 - 1] + s[s.length / 2]) / 2;
};

function rowOutcome(r) {
  if (RUNNING.has(r.status)) return "running";
  return r.status === "completed" ? "ok" : "failed";
}

function renderJournal() {
  const body = $("#journal-body");
  if (!body) return;
  const rows = journal.rows.filter((r) => !journal.outcome || rowOutcome(r) === journal.outcome);
  body.replaceChildren(...rows.map((r) => {
    const dev = state.devices.get(r.device_id);
    const shown = Object.keys(r.desired || {}).length ? r.desired : r.params || {};
    const what = Object.keys(shown).length ? describeDesired(shown) : r.action;
    const cells = [
      clock(r.t_created),
      dev ? dev.title : r.device_id,
      el("span", { title: `${r.action} ${JSON.stringify(r.params)}` }, what),
      el("span", { class: `badge ${rowOutcome(r)}` }, STATUS_UK[r.status] || r.status),
      r.attempts,
      fmtMs(ms(r.t_created, r.t_published)),
      fmtMs(ms(r.t_published, r.t_ack)),
      fmtMs(ms(r.t_created, r.t_finished)),
      r.error && !r.error.startsWith("superseded_by") ? ERROR_UK[r.error] || r.error : "",
    ];
    return el("tr", { class: rowOutcome(r) }, cells.map((c, i) => el("td", { class: i >= 4 && i <= 7 ? "num" : "" }, c)));
  }));
  if (!rows.length) body.append(el("tr", {}, el("td", { colspan: 9, class: "muted" }, "Команд ще немає.")));

  const finished = journal.rows.filter((r) => !RUNNING.has(r.status));
  const ok = finished.filter((r) => r.status === "completed");
  const acks = journal.rows.map((r) => ms(r.t_published, r.t_ack)).filter((x) => x != null);
  const totals = ok.map((r) => ms(r.t_created, r.t_finished)).filter((x) => x != null);
  const stat = (label, value) => el("div", { class: "stat" }, el("div", { class: "stat-value" }, value), el("div", { class: "stat-label" }, label));
  $("#journal-stats").replaceChildren(
    stat("команд", journal.rows.length),
    stat("виконано", finished.length ? `${Math.round((ok.length / finished.length) * 100)} %` : "—"),
    stat("збоїв", finished.length - ok.length),
    stat("медіана ack", acks.length ? `${Math.round(median(acks))} мс` : "—"),
    stat("медіана виконання", totals.length ? `${Math.round(median(totals))} мс` : "—"),
  );
}

const fmtMs = (v) => (v == null ? "—" : `${v} мс`);

state.listeners.push(({ type }) => {
  if (!["CommandCreated", "CommandStatusChanged", "CommandFinished"].includes(type)) return;
  if (!$("#view-journal").classList.contains("active")) { journal.loaded = false; return; }
  clearTimeout(journal.timer);
  journal.timer = setTimeout(loadJournal, 250); // a group command produces many events at once
});

// ------------------------------------------------------------------- settings
// [section, field, label, kind, options/hint]; kind: select | number | bool | list | range
const SETTINGS_FORM = [
  ["Контроль виконання", "застосовується одразу", [
    ["dispatcher", "ack_timeout_s", "Таймаут підтвердження (ack), с", "number", { step: 0.1, min: 0.1 }],
    ["dispatcher", "max_attempts", "Спроб відправки", "number", { step: 1, min: 1 }],
    ["dispatcher", "retry_backoff_s", "Паузи перед повторами, с", "list", { hint: "через кому, напр. 0.5, 1" }],
    ["dispatcher", "cmd_ttl_s", "Термін дії команди (TTL), с", "number", { step: 0.5, min: 0.5 }],
    ["dispatcher", "state_margin_s", "Запас часу для рухомих пристроїв, с", "number", { step: 0.5, min: 0 }],
    ["dispatcher", "progress_messages", "Проміжні повідомлення («Відкриваю…»)", "bool"],
    ["dispatcher", "progress_min_s", "Вважати дію довгою від, с", "number", { step: 0.5, min: 0 }],
  ]],
  ["Доступність пристроїв", "застосовується одразу", [
    ["health", "heartbeat_s", "Період heartbeat за замовчуванням, с", "number", { step: 1, min: 1 }],
    ["health", "stale_factor", "«Мовчить» після N × heartbeat", "number", { step: 0.5, min: 1 }],
  ]],
  ["Сповіщення", "асистент сам повідомляє про проблеми; застосовується одразу", [
    ["notifications", "enabled", "Сповіщення увімкнено", "bool"],
    ["notifications", "offline", "Пристрій не в мережі", "bool"],
    ["notifications", "stale", "Пристрій мовчить (немає heartbeat)", "bool"],
    ["notifications", "recovered", "Пристрій знову на зв'язку", "bool"],
    ["notifications", "drift_critical", "Критичний пристрій змінився без команди", "bool"],
    ["notifications", "cooldown_s", "Не повторювати для пристрою частіше, с", "number", { step: 5, min: 0 }],
  ]],
  ["Розбір команд і відповіді", "застосовується одразу", [
    ["nlu", "mode", "Режим NLU", "select", { options: { rule: "правила + fuzzy", llm: "LLM (етап 4)", hybrid: "гібрид (етап 4)" }, disabled: ["llm", "hybrid"] }],
    ["nlu", "fuzzy_threshold", "Поріг нечіткого збігу", "range", { min: 50, max: 100, step: 1 }],
    ["nlu", "min_confidence", "Мінімальна впевненість", "range", { min: 0, max: 1, step: 0.05 }],
    ["", "language", "Мова відповідей панелі", "select", { options: { uk: "українська", en: "English" } }],
  ]],
  ["Голос", "збережеться; діє, коли запущено голосовий конвеєр", [
    ["voice", "trigger", "Тригер", "select", { options: { vosk_kws: "слово «Телекінез» (Vosk)", openwakeword: "hey jarvis (openWakeWord)", push_to_talk: "кнопка (push-to-talk)" } }],
    ["voice", "stt", "Розпізнавання мовлення", "select", { options: { vosk: "Vosk", whisper: "faster-whisper" } }],
    ["voice", "stt_lang", "Мова розпізнавання", "select", { options: { uk: "українська", en: "English", auto: "авто" } }],
    ["voice", "tts", "Відповідь", "select", { options: { piper: "голос + текст (Piper)", sapi: "голос + текст (Windows)", text: "лише текст" } }],
    ["voice", "vad_end_silence_ms", "Кінець фрази після тиші, мс", "number", { step: 10, min: 90 }],
    ["voice", "max_utterance_s", "Найдовша фраза, с", "number", { step: 1, min: 1 }],
  ]],
  ["Інфраструктура", "лише для перегляду: зміни потребують перезапуску (config/settings.yaml)", [
    ["mqtt", "host", "MQTT-брокер", "readonly"],
    ["mqtt", "port", "Порт MQTT", "readonly"],
    ["mqtt", "prefix", "Префікс топіків", "readonly"],
    ["web", "port", "Порт веб-панелі", "readonly"],
    ["storage", "path", "База даних", "readonly"],
  ]],
];

const settingsState = { values: null };
const getValue = (section, field) => (section ? settingsState.values[section][field] : settingsState.values[field]);

async function loadSettings() {
  const body = await api("/api/settings");
  settingsState.values = body.settings;
  renderSettings();
}

function settingInput(section, field, kind, opts = {}) {
  const value = getValue(section, field);
  const name = section ? `${section}.${field}` : field;
  if (kind === "readonly") return el("code", { class: "ro" }, String(value));
  if (kind === "bool") {
    const input = el("input", { type: "checkbox", name });
    input.checked = !!value;
    return el("label", { class: "switch" }, input, el("span"));
  }
  if (kind === "select") {
    return el("select", { class: "btn", name }, Object.entries(opts.options).map(([k, v]) =>
      el("option", { value: k, selected: value === k, disabled: (opts.disabled || []).includes(k) }, v)));
  }
  if (kind === "range") {
    const out = el("output", {}, String(value));
    const input = el("input", { type: "range", name, min: opts.min, max: opts.max, step: opts.step, value,
      oninput: (e) => { out.textContent = e.target.value; } });
    return el("span", { class: "range" }, input, out);
  }
  if (kind === "list") {
    return el("input", { class: "field", name, value: value.join(", "), title: opts.hint || "" });
  }
  return el("input", { class: "field", type: "number", name, value, step: opts.step, min: opts.min });
}

function renderSettings() {
  const view = $("#view-settings");
  const form = el("form", { class: "settings", novalidate: true, onsubmit: saveSettings }); // the server validates
  for (const [title, note, fields] of SETTINGS_FORM) {
    form.append(el("fieldset", {},
      el("legend", {}, title, el("span", { class: "note" }, note)),
      fields.map(([section, field, label, kind, opts]) =>
        el("div", { class: "setting" }, el("label", {}, label), settingInput(section, field, kind, opts)))));
  }
  form.append(el("div", { class: "actions" },
    el("button", { class: "btn primary", type: "submit" }, "Застосувати"),
    el("button", { class: "btn", type: "button", onclick: loadSettings }, "Скасувати зміни"),
    el("span", { class: "muted" }, "Зміни діють до перезапуску асистента.")));
  view.replaceChildren(el("div", { class: "toolbar" }, el("h1", {}, "Налаштування")), form);
}

async function saveSettings(e) {
  e.preventDefault();
  const patch = {};
  for (const [, , fields] of SETTINGS_FORM) {
    for (const [section, field, , kind] of fields) {
      if (kind === "readonly") continue;
      const input = e.target.elements[section ? `${section}.${field}` : field];
      let value;
      if (kind === "bool") value = input.checked;
      else if (kind === "list") value = input.value.split(",").map((x) => Number(x.trim())).filter((x) => !Number.isNaN(x));
      else if (kind === "number" || kind === "range") value = Number(input.value);
      else value = input.value;
      if (JSON.stringify(value) === JSON.stringify(getValue(section, field))) continue;
      if (section) (patch[section] ||= {})[field] = value;
      else patch[field] = value;
    }
  }
  if (!Object.keys(patch).length) { toast("Змін немає."); return; }
  try {
    const result = await api("/api/settings", { method: "PUT", body: patch });
    settingsState.values = result.settings;
    renderSettings();
    const labels = Object.fromEntries(SETTINGS_FORM.flatMap(([, , fs]) => fs.map(([s, f, l]) => [s ? `${s}.${f}` : f, l])));
    const names = (list) => list.map((n) => `«${labels[n] || n}»`).join(", ");
    const later = result.restart_required.length ? ` Після перезапуску: ${names(result.restart_required)}.` : "";
    toast(`Застосовано: ${names(result.applied) || "нічого"}.${later}`, "ok");
    if (patch.language) refresh();
  } catch (err) {
    toast(`Не застосовано: ${err.message}`, "bad");
  }
}

// ---------------------------------------------------------------- theme, view
function storage(key, value) {
  try {
    if (value === undefined) return localStorage.getItem(key);
    localStorage.setItem(key, value);
  } catch { /* private mode / blocked storage: the panel works without it */ }
  return null;
}

function applyTheme(theme) {
  document.documentElement.dataset.theme = theme;
  $("#theme-toggle").textContent = theme === "dark" ? "☀" : "☾";
  $("#theme-toggle").title = theme === "dark" ? "Світла тема" : "Темна тема";
}
const systemDark = window.matchMedia?.("(prefers-color-scheme: dark)").matches;
applyTheme(storage("tkz-theme") || (systemDark ? "dark" : "light"));
$("#theme-toggle").addEventListener("click", () => {
  const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
  applyTheme(next);
  storage("tkz-theme", next);
});


// --------------------------------------------------------------------- faults
const FAULTS_UK = {
  offline: ["Не в мережі", "обрив зв'язку без disconnect → брокер шле LWT"],
  freeze: ["Завис", "з'єднання є, але ні ack, ні heartbeat"],
  drop_rate: ["Втрачає команди", "частка p команд губиться (повтор рятує)"],
  stuck: ["Не виконує", "ack приходить, стан не змінюється"],
  slow: ["Повільний", "затримка ms перед відповіддю"],
  reject: ["Відхиляє", "ack rejected з помилкою"],
};
const faultsState = { faults: {} };

function faultBadges(deviceId) {
  const active = faultsState.faults[deviceId] || {};
  return Object.keys(active).map((f) => el("span", { class: "fault-badge", title: FAULTS_UK[f]?.[1] || f },
    `⚠ ${FAULTS_UK[f]?.[0] || f}`));
}

async function sendFault(deviceId, fault, params = {}, enabled = true) {
  try {
    await api("/api/sim/faults", { method: "POST", body: { device_id: deviceId, fault, params, enabled } });
  } catch (err) {
    toast(`Збій не застосовано: ${err.message}`, "bad");
  }
}

function faultControl(d) {
  const active = faultsState.faults[d.id] || {};
  const select = el("select", { class: "btn" },
    el("option", { value: "" }, "без збою"),
    Object.entries(FAULTS_UK).map(([k, [label]]) => el("option", { value: k, selected: k in active }, label)));
  const param = el("input", { class: "field small", type: "number", placeholder: "параметр" });
  const syncParam = () => {
    const f = select.value;
    param.hidden = !["drop_rate", "slow"].includes(f);
    param.step = f === "drop_rate" ? "0.1" : "100";
    param.min = "0";
    param.max = f === "drop_rate" ? "1" : "";
    param.placeholder = f === "drop_rate" ? "p, 0..1" : "ms";
    param.value = f === "drop_rate" ? active.drop_rate?.p ?? 0.5 : f === "slow" ? active.slow?.ms ?? 3000 : "";
  };
  select.addEventListener("change", syncParam);
  syncParam();
  const apply = el("button", { class: "btn primary", onclick: () => {
    const f = select.value;
    if (!f) return sendFault(d.id, "clear");
    const params = f === "drop_rate" ? { p: Number(param.value) } : f === "slow" ? { ms: Number(param.value) } : {};
    return sendFault(d.id, f, params);
  } }, "Застосувати");
  const drift = el("button", { class: "btn", title: "Стан зміниться сам, ніби хтось натиснув вимикач",
    onclick: () => sendFault(d.id, "drift") }, "Drift");
  return el("tr", { class: Object.keys(active).length ? "has-fault" : "" },
    el("td", {}, el("div", {}, d.title), el("div", { class: "card-sub" }, d.id)),
    el("td", {}, el("i", { class: `dot ${d.availability}` }), " ", AVAIL[d.availability] || d.availability),
    el("td", {}, faultBadges(d.id).length ? faultBadges(d.id) : el("span", { class: "muted" }, "—")),
    el("td", {}, el("div", { class: "fault-controls" }, select, param, apply, drift)));
}

function renderFaults() {
  const view = $("#view-faults");
  const devices = [...state.devices.values()];
  const count = Object.keys(faultsState.faults).length;
  view.replaceChildren(
    el("div", { class: "toolbar" }, el("h1", {}, "Збої (симулятор)"),
      el("div", { class: "filters" },
        el("span", { class: "muted" }, count ? `активних збоїв: ${count}` : "збоїв немає"),
        el("button", { class: "btn", disabled: !count, onclick: () => sendFault("*", "clear") }, "Скинути всі"))),
    el("p", { class: "muted intro" }, "Збої вмикаються в симуляторі через MQTT (tkz/sim/faults). "
      + "Дайте команду пристрою зі збоєм — на дашборді й у журналі видно, як асистент його виявляє."),
    el("div", { class: "table-wrap" }, el("table", { class: "journal faults" },
      el("thead", {}, el("tr", {}, ["Пристрій", "Доступність", "Активні збої", "Керування"].map((h) => el("th", {}, h)))),
      el("tbody", {}, devices.map(faultControl)))),
  );
}

async function loadFaults() {
  try {
    faultsState.faults = (await api("/api/sim/faults")).faults || {};
  } catch { faultsState.faults = {}; }
  renderFaults();
}

state.listeners.push(({ type, data }) => {
  if (type === "SimFaultsChanged") {
    faultsState.faults = data.faults || {};
    for (const id of state.devices.keys()) updateCard(id);
    if ($("#view-faults").classList.contains("active")) renderFaults();
  } else if (type === "DeviceAvailabilityChanged" && $("#view-faults").classList.contains("active")) {
    renderFaults();
  }
});

// ----------------------------------------------------------------------- tabs
function showView(name) {
  document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t.dataset.view === name));
  document.querySelectorAll(".view").forEach((v) => v.classList.toggle("active", v.id === `view-${name}`));
  storage("tkz-view", name);
  if (name === "journal") { renderJournalShell(); loadJournal(); }
  if (name === "faults") loadFaults();
  if (name === "settings") loadSettings().catch((e) => toast(e.message, "bad"));
}
document.querySelectorAll(".tab").forEach((t) => t.addEventListener("click", () => showView(t.dataset.view)));

// ages tick locally between heartbeats
setInterval(() => { for (const d of state.devices.values()) if (d.age_s != null) d.age_s += 1; }, 1000);

const lastView = storage("tkz-view");
if (lastView && $(`#view-${lastView}`)) showView(lastView);
connect();
