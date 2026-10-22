// CRM-фронт: таблица лидов + карточка. Без сборки и зависимостей.
"use strict";

const PAGE = 50;
const WEBSITE_LABELS = {
  has_website: ["є сайт", "has"],
  not_found: ["немає", "none"],
  site_down: ["не відкривається", "none"],
  ddg_failed: ["пошук не вдався", "unknown"],
  null: ["не шукали", "unknown"],
};

// Подписи категорий для людей; неизвестный ключ показываем как есть
const CATEGORY_LABELS = {
  dentist: "Стоматологія",
  clinic: "Клініка",
  doctors: "Лікарі",
  lab: "Лабораторія",
  rehab: "Реабілітація",
  cosmetology: "Косметологія",
  hospital: "Лікарня",
  vet: "Ветеринарія",
  pharmacy: "Аптека",
  hairdresser: "Перукарня",
  beauty: "Салон краси",
  nails: "Манікюр",
  tattoo: "Тату",
  fitness: "Фітнес",
  spa: "SPA",
  real_estate: "Нерухомість",
  lawyer: "Юристи",
  accountant: "Бухгалтерія",
  travel: "Турагенція",
  insurance: "Страхування",
  photo: "Фото",
  driving_school: "Автошкола",
  language_school: "Мовна школа",
};
const catLabel = (key) => CATEGORY_LABELS[key] ?? key;

const state = { status: "", offset: 0, total: 0, statuses: [], outreachStatuses: {}, openId: null };
const $ = (id) => document.getElementById(id);

// --- утилиты ---------------------------------------------------------------

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v == null || v === false) continue;
    if (k === "class") node.className = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v);
  }
  node.append(...children.filter((c) => c != null));
  return node;
}

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
  });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body.detail || `HTTP ${res.status}`);
  return body;
}

let toastTimer;
function toast(text, isError = false) {
  const t = $("toast");
  t.textContent = text;
  t.classList.toggle("error", isError);
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (t.hidden = true), isError ? 5000 : 2500);
}

function debounce(fn, ms) {
  let timer;
  return (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => fn(...args), ms);
  };
}

const statusLabel = (key) => state.statuses.find((s) => s.key === key)?.label ?? key;
const fmtDate = (iso) =>
  new Date(iso).toLocaleString("uk-UA", { dateStyle: "short", timeStyle: "short" });

function statusSelect(current, onChange) {
  const sel = el("select", { class: `status-select st-${current}`, "aria-label": "Статус" });
  for (const s of state.statuses) {
    sel.append(el("option", { value: s.key, selected: s.key === current }, s.label));
  }
  sel.addEventListener("change", () => {
    sel.className = `status-select st-${sel.value}`;
    onChange(sel.value);
  });
  return sel;
}

// --- статусы и фильтры -----------------------------------------------------

async function loadMeta(first = false) {
  const meta = await api("/meta");
  state.statuses = meta.statuses;
  state.outreachStatuses = meta.outreach_statuses ?? {};
  renderTabs();
  if (!first) return;

  for (const c of meta.cities) $("f-city").append(el("option", { value: c }, c));
  for (const c of meta.categories) $("f-category").append(el("option", { value: c }, catLabel(c)));
  $("sync-btn").hidden = !meta.sheets_enabled;
  $("card-status").replaceChildren(
    ...meta.statuses.map((s) => el("option", { value: s.key }, s.label)),
  );
}

function renderTabs() {
  const total = state.statuses.reduce((sum, s) => sum + s.count, 0);
  const touched = total - (state.statuses.find((s) => s.key === "new")?.count ?? 0);
  $("subtitle").textContent = `${total} лідів у базі · ${touched} в обробці`;
  const tab = (key, label, count, hint) =>
    el(
      "button",
      {
        class: key ? `tab st-${key}` : "tab",
        "aria-pressed": String(state.status === key),
        title: hint,
        onclick: () => {
          state.status = key;
          state.offset = 0;
          renderTabs();
          loadLeads();
        },
      },
      key ? el("span", { class: "dot" }) : null,
      label,
      el("span", { class: "n" }, String(count)),
    );
  $("status-tabs").replaceChildren(
    tab("", "Усі", total),
    ...state.statuses.map((s) => tab(s.key, s.label, s.count, s.hint)),
  );
}

function query() {
  const p = new URLSearchParams({ limit: PAGE, offset: state.offset });
  const put = (k, v) => v && p.set(k, v);
  put("status", state.status);
  put("q", $("f-q").value.trim());
  put("city", $("f-city").value);
  put("category", $("f-category").value);
  put("website", $("f-website").value);
  if ($("f-phone").checked) p.set("with_phone", "true");
  return p;
}

// --- таблица ---------------------------------------------------------------

async function loadLeads() {
  let data;
  try {
    data = await api(`/leads?${query()}`);
  } catch (e) {
    toast(`Не вдалося завантажити: ${e.message}`, true);
    return;
  }
  state.total = data.total;
  $("rows").replaceChildren(...data.items.map(renderRow));
  $("empty").hidden = data.items.length > 0;

  const from = data.total ? state.offset + 1 : 0;
  const to = state.offset + data.items.length;
  $("range").textContent = `${from}–${to} з ${data.total}`;
  $("prev").disabled = state.offset === 0;
  $("next").disabled = to >= data.total;
}

function websiteCell(lead) {
  const [label, kind] = WEBSITE_LABELS[lead.website_status] ?? WEBSITE_LABELS.null;
  if (lead.website) {
    const host = lead.website.replace(/^https?:\/\/(www\.)?/, "").replace(/\/.*$/, "");
    return el("a", { class: "site has", href: lead.website, target: "_blank", rel: "noopener", title: lead.website },
      host.length > 22 ? "сайт ↗" : `${host} ↗`);
  }
  return el("span", { class: `site ${kind}` }, label);
}

const metaLine = (lead) => `${catLabel(lead.category)} · ${lead.city}`;

// --- Instagram и холодная рассылка (её шлёт внешний агент, см. /api/outreach) ---

const igHandle = (url) => url.replace(/\/+$/, "").split("/").pop();
const outreachBadge = (status) =>
  el("span", { class: `badge ob-${status}` }, state.outreachStatuses[status] ?? status);

function igLine(lead) {
  if (!lead.instagram) return null;
  return el(
    "div",
    { class: "ig-line" },
    el("a", { class: "ig", href: lead.instagram, target: "_blank", rel: "noopener",
              onclick: (e) => e.stopPropagation() }, `@${igHandle(lead.instagram)}`),
    lead.outreach_status ? outreachBadge(lead.outreach_status) : null,
  );
}

function outreachField(o) {
  const when = o.sent_at ?? o.claimed_at;
  const lines = [
    el("div", {}, outreachBadge(o.status), when ? el("span", { class: "muted" }, ` ${fmtDate(when)}`) : null),
  ];
  if (o.message) lines.push(el("p", { class: "ob-text" }, "→ ", o.message));
  if (o.reply) lines.push(el("p", { class: "ob-text" }, "← ", o.reply));
  if (o.error) lines.push(el("p", { class: "ob-text muted" }, o.error));
  return lines;
}

// Первая буква названия без кавычек/цифр — для «аватарки»
const initial = (name) => (name.match(/\p{L}/u)?.[0] ?? "•").toUpperCase();

function renderRow(lead) {
  const stop = (e) => e.stopPropagation();
  return el(
    "tr",
    { onclick: () => openCard(lead.id) },
    el(
      "td",
      {},
      el(
        "div",
        { class: "lead" },
        el("div", { class: `avatar st-${lead.crm_status}`, "aria-hidden": "true" }, initial(lead.name)),
        el(
          "div",
          { style: "min-width:0" },
          el("div", { class: "lead-name", title: lead.name }, lead.name),
          el("div", { class: "lead-meta" }, metaLine(lead)),
          igLine(lead),
          // На узком экране колонки «Телефон» нет — номер под названием
          lead.phone
            ? el("a", { class: "phone phone-m", href: `tel:${lead.phone}`, onclick: stop }, lead.phone)
            : null,
        ),
      ),
    ),
    el(
      "td",
      { class: "phone-col" },
      lead.phone
        ? el("a", { class: "phone", href: `tel:${lead.phone}`, onclick: stop }, lead.phone)
        : el("span", { class: "nophone" }, "—"),
    ),
    el("td", { class: "site-col", onclick: stop }, websiteCell(lead)),
    el(
      "td",
      { class: "status-col", onclick: stop },
      statusSelect(lead.crm_status, (status) => saveStatus(lead.id, status)),
    ),
  );
}

async function saveStatus(id, status, comment = null) {
  try {
    await api(`/leads/${id}/status`, {
      method: "PATCH",
      body: JSON.stringify({ status, comment }),
    });
  } catch (e) {
    toast(`Не збережено: ${e.message}`, true);
    loadLeads();
    return false;
  }
  toast(`Статус: ${statusLabel(status)}`);
  await loadMeta();
  // Лид мог выпасть из текущего фильтра по статусу — перечитываем страницу
  if (state.status) loadLeads();
  return true;
}

// --- карточка --------------------------------------------------------------

const CARD_FIELDS = [
  ["phone", "Телефон"],
  ["email", "Email"],
  ["contact_person", "Контактна особа"],
  ["address", "Адреса"],
  ["website", "Сайт"],
  ["instagram", "Instagram"],
  ["facebook", "Facebook"],
  ["telegram", "Telegram"],
  ["other_socials", "Інші соцмережі"],
  ["automation", "Автоматизація"],
  ["size_estimate", "Розмір"],
  ["edrpou", "ЄДРПОУ"],
  ["notes", "Нотатки"],
];

function fieldValue(key, value) {
  if (key === "phone") return el("a", { href: `tel:${value}` }, value);
  if (key === "email") return el("a", { href: `mailto:${value}` }, value);
  if (/^https?:\/\//.test(value)) {
    const short = value.replace(/^https?:\/\/(www\.)?/, "").replace(/\/$/, "");
    return el("a", { href: value, target: "_blank", rel: "noopener" }, short);
  }
  return value;
}

async function openCard(id) {
  let lead;
  try {
    lead = await api(`/leads/${id}`);
  } catch (e) {
    toast(e.message, true);
    return;
  }
  state.openId = id;
  history.replaceState(null, "", `#lead-${id}`);
  $("card-title").textContent = lead.name;
  $("card-meta").textContent = metaLine(lead);

  const fields = [];
  for (const [key, label] of CARD_FIELDS) {
    if (lead[key] === "" || lead[key] == null) continue;
    fields.push(el("dt", {}, label), el("dd", {}, fieldValue(key, String(lead[key]))));
  }
  if (!lead.website) {
    const [label] = WEBSITE_LABELS[lead.website_status] ?? WEBSITE_LABELS.null;
    fields.push(el("dt", {}, "Сайт"), el("dd", {}, label));
  }
  if (lead.outreach) {
    fields.push(el("dt", {}, "Розсилка IG"), el("dd", {}, ...outreachField(lead.outreach)));
  }
  $("card-fields").replaceChildren(...fields);

  $("card-status").value = lead.crm_status;
  $("card-status").className = `status-select st-${lead.crm_status}`;
  $("card-comment").value = "";
  renderHistory(lead.events);

  $("card").hidden = false;
  $("backdrop").hidden = false;
}

function renderHistory(events) {
  if (!events.length) {
    $("card-history").replaceChildren(el("li", { class: "muted" }, "Ще нічого не було"));
    return;
  }
  $("card-history").replaceChildren(
    ...events.map((ev) => {
      const changed = ev.old_status !== ev.new_status;
      return el(
        "li",
        {},
        el("time", { datetime: ev.created_at }, fmtDate(ev.created_at)),
        changed
          ? el(
              "span",
              {},
              el("span", { class: `badge st-${ev.old_status}` }, statusLabel(ev.old_status)),
              el("span", { class: "arrow" }, "→"),
              el("span", { class: `badge st-${ev.new_status}` }, statusLabel(ev.new_status)),
            )
          : null,
        ev.comment ? el("p", {}, ev.comment) : null,
      );
    }),
  );
}

function closeCard() {
  if (state.openId == null) return;
  state.openId = null;
  history.replaceState(null, "", location.pathname + location.search);
  $("card").hidden = true;
  $("backdrop").hidden = true;
}

$("card-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const id = state.openId;
  const ok = await saveStatus(id, $("card-status").value, $("card-comment").value);
  if (ok) {
    loadLeads();
    openCard(id);
  }
});
$("card-status").addEventListener("change", (e) => {
  e.target.className = `status-select st-${e.target.value}`;
});
$("card-close").addEventListener("click", closeCard);
$("backdrop").addEventListener("click", closeCard);
document.addEventListener("keydown", (e) => e.key === "Escape" && closeCard());

// --- синк с Google Sheets --------------------------------------------------

$("sync-btn").addEventListener("click", async () => {
  const btn = $("sync-btn");
  btn.disabled = true;
  $("sync-note").textContent = "Синхронізую…";
  try {
    const r = await api("/sync", { method: "POST" });
    $("sync-note").textContent = `Оновлено: ${r.pushed} рядків`;
  } catch (e) {
    $("sync-note").textContent = "";
    toast(e.message, true);
  } finally {
    btn.disabled = false;
  }
});

// --- старт -----------------------------------------------------------------

function resetAndLoad() {
  state.offset = 0;
  loadLeads();
}
$("f-q").addEventListener("input", debounce(resetAndLoad, 300));
for (const id of ["f-city", "f-category", "f-website", "f-phone"]) {
  $(id).addEventListener("change", resetAndLoad);
}
$("prev").addEventListener("click", () => {
  state.offset = Math.max(0, state.offset - PAGE);
  loadLeads();
});
$("next").addEventListener("click", () => {
  state.offset += PAGE;
  loadLeads();
});

// #lead-123 в адресе — сразу открыть карточку: такой ссылкой удобно делиться
const deepLink = location.hash.match(/^#lead-(\d+)$/);

loadMeta(true)
  .then(loadLeads)
  .then(() => deepLink && openCard(Number(deepLink[1])))
  .catch((e) => toast(`Сервер недоступний: ${e.message}`, true));
