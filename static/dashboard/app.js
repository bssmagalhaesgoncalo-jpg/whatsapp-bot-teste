/* =====================================================================
   Daniela Beauty — dashboard client
   Router por hash + views que consomem as APIs JSON de bot.py.
   Sem framework, sem build. ES2020.
   ===================================================================== */
"use strict";

/* ---------- core helpers ---------- */
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];

function h(tag, attrs = {}, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k === "class") e.className = v;
    else if (k === "html") e.innerHTML = v;
    else if (k === "dataset") Object.assign(e.dataset, v);
    else if (k.startsWith("on") && typeof v === "function") e.addEventListener(k.slice(2), v);
    else e.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat()) {
    if (kid == null || kid === false) continue;
    e.append(kid.nodeType ? kid : document.createTextNode(kid));
  }
  return e;
}

function icon(name, cls = "") {
  const ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("fill", "none");
  svg.setAttribute("stroke", "currentColor");
  svg.setAttribute("stroke-width", "1.7");
  svg.setAttribute("stroke-linecap", "round");
  svg.setAttribute("stroke-linejoin", "round");
  svg.setAttribute("class", ("ico " + (cls || "")).trim());
  svg.setAttribute("aria-hidden", "true");
  const use = document.createElementNS(ns, "use");
  use.setAttribute("href", "#i-" + name);
  svg.append(use);
  return svg;
}

async function api(url, opts) {
  const r = await fetch(url, opts);
  const ct = r.headers.get("content-type") || "";
  const body = ct.includes("json") ? await r.json().catch(() => ({})) : {};
  if (!r.ok) {
    const err = new Error(body.erro || `Erro ${r.status}`);
    err.status = r.status;
    err.body = body;
    throw err;
  }
  return body;
}
const jpost = (url, data) =>
  api(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(data || {}) });
const jput = (url, data) =>
  api(url, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(data || {}) });
const jpatch = (url, data) =>
  api(url, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify(data || {}) });

function toast(msg, kind = "") {
  const t = h("div", { class: "toast " + (kind === "err" ? "err" : "") }, msg);
  $("#toasts").append(t);
  setTimeout(() => t.remove(), 3600);
}

/* ---------- formatters ---------- */
const chf = (cents) =>
  cents == null ? "—" : "CHF " + (cents / 100).toFixed(2).replace(".", ",");
function fmtMin(m) {
  m = Math.round(m || 0);
  if (m < 60) return m + " min";
  const hh = Math.floor(m / 60), r = m % 60;
  return r ? `${hh}h${String(r).padStart(2, "0")}` : `${hh}h`;
}
function fmtDataPt(iso) {
  if (!iso) return "—";
  const d = new Date(iso + "T00:00:00");
  return d.toLocaleDateString("pt-PT", { day: "2-digit", month: "short" });
}
const initials = (name) =>
  (name || "?").split(/\s+/).slice(0, 2).map((s) => s[0] || "").join("").toUpperCase();

const OP_LABEL = { scheduled: "Agendada", arrived: "Chegou", in_progress: "A decorrer", done: "Concluída" };
const EST_LABEL = { confirmed: "Confirmada", pending: "A aprovar", cancelled: "Cancelada", completed: "Concluída", no_show: "Não compareceu" };
function statusBadge(estado, op) {
  const e = (estado || "").toLowerCase();
  if (e === "cancelled") return h("span", { class: "badge badge--danger" }, "Cancelada");
  if (e === "no_show") return h("span", { class: "badge badge--danger" }, "Não veio");
  if (op === "done" || e === "completed") return h("span", { class: "badge badge--success" }, "Concluída");
  if (op === "in_progress") return h("span", { class: "badge badge--warning" }, "A decorrer");
  if (op === "arrived") return h("span", { class: "badge badge--info" }, "Chegou");
  return h("span", { class: "badge" }, EST_LABEL[e] || "Agendada");
}

function skeletonCard() { return h("div", { class: "card card--pad" }, h("div", { class: "skel", style: "height:64px" })); }
function emptyState(txt) {
  return h("div", { class: "empty" }, icon("today"), h("div", {}, txt));
}

/* ---------- drawer ---------- */
const Drawer = {
  el: null, scrim: null,
  init() {
    this.el = $("#drawer"); this.scrim = $("#scrim");
    this.scrim.addEventListener("click", () => this.close());
    document.addEventListener("keydown", (e) => { if (e.key === "Escape") this.close(); });
  },
  open(node) {
    if (!this.el.classList.contains("open")) this._returnFocus = document.activeElement;
    this.el.innerHTML = "";
    this.el.append(node);
    this.el.classList.add("open");
    this.scrim.classList.add("open");
    this.el.setAttribute("aria-hidden", "false");
    this.el.setAttribute("role", "dialog");
    this.el.setAttribute("aria-modal", "true");
    this.el.setAttribute("tabindex", "-1");
    const first = this.el.querySelector("button, a, input, select, [tabindex]");
    (first || this.el).focus();
  },
  close() {
    if (!this.el.classList.contains("open")) return;
    this.el.classList.remove("open");
    this.scrim.classList.remove("open");
    this.el.setAttribute("aria-hidden", "true");
    if (this._returnFocus && this._returnFocus.focus) this._returnFocus.focus();
    this._returnFocus = null;
  },
};

/* ===================================================================
   APPOINTMENT DRAWER  (partilhado por Hoje e Agenda)
   =================================================================== */
async function openAppointment(id) {
  Drawer.open(h("div", { class: "drawer-body" }, h("div", { class: "skel", style: "height:120px" })));
  let ag;
  try { ag = await api(`/api/agendamentos/${id}`); }
  catch (e) { toast(e.message, "err"); Drawer.close(); return; }
  renderAppointment(ag);
}

function renderAppointment(ag) {
  const op = ag.op_status || "scheduled";
  const est = (ag.estado || "").toLowerCase();
  const encerrada = ["cancelled", "no_show"].includes(est);
  const totalLabel = ag.preco_por_confirmar ? "Preço a confirmar" : chf(ag.total_centimos);

  const head = h("div", { class: "drawer-head" },
    icon("calendar"),
    h("h3", {}, `Marcação #${ag.id}`),
    h("span", { class: "nav-spacer", style: "flex:1" }),
    h("button", { class: "icon-btn", onclick: () => Drawer.close(), "aria-label": "Fechar" }, icon("x")));

  const cli = ag.cliente_resumo;
  const body = h("div", { class: "drawer-body" },
    h("div", { style: "display:flex;gap:12px;align-items:center;margin-bottom:16px" },
      h("span", { class: "avatar", style: "width:38px;height:38px;font-size:14px" }, initials(ag.nome)),
      h("div", {},
        h("div", { style: "font-weight:600;font-size:15px" }, ag.nome || "Cliente"),
        h("div", { style: "color:var(--text-3);font-size:12.5px" }, ag.telefone || ""))),
    statusBadge(ag.estado, op),
    h("dl", { class: "dl", style: "margin:16px 0" },
      h("dt", {}, "Serviço"), h("dd", {}, ag.servico || "—"),
      h("dt", {}, "Data"), h("dd", {}, ag.data || "—"),
      h("dt", {}, "Hora"), h("dd", { class: "tnum" }, ag.hora || ag.hora_hhmm || "—"),
      h("dt", {}, "Duração"), h("dd", {}, ag.duracao_min ? fmtMin(ag.duracao_min) : (ag.duracao || "—")),
      h("dt", {}, "Preço"), h("dd", {}, totalLabel),
      h("dt", {}, "Estado operacional"), h("dd", {}, OP_LABEL[op] || op)),
    cli ? h("div", { class: "card card--pad", style: "margin-top:8px" },
      h("div", { class: "eyebrow", style: "margin:0 0 8px" }, "Cliente"),
      h("dl", { class: "dl" },
        h("dt", {}, "Visitas"), h("dd", { class: "tnum" }, String(cli.visits_count ?? 0)),
        h("dt", {}, "Gasto total"), h("dd", { class: "tnum" }, chf(cli.spend_cents)),
        h("dt", {}, "Última visita"), h("dd", {}, cli.last_visit ? fmtDataPt(cli.last_visit) : "—"),
        cli.no_show_count ? h("dt", {}, "Faltas") : null,
        cli.no_show_count ? h("dd", { class: "tnum", style: "color:var(--danger)" }, String(cli.no_show_count)) : null)) : null,
    ag.fatura ? h("div", { class: "att sev-info", style: "margin-top:14px" },
      icon("receipt"),
      h("div", { class: "a-body" },
        h("div", { class: "a-title" }, ag.fatura.invoice_number || "Rascunho de fatura"),
        h("div", { class: "a-desc" }, `${chf(ag.fatura.total_cents)} · ${EST_LABEL[ag.fatura.status] || ag.fatura.status}`)),
      h("button", { class: "btn btn--sm", onclick: () => location.hash = "#/faturas" }, "Abrir")) : null);

  // actions
  const foot = h("div", { class: "drawer-foot" });
  const act = async (fn) => { try { await fn(); Drawer.close(); Router.reload(); } catch (e) { handleActionError(e); } };

  if (!encerrada && op !== "done") {
    if (op === "scheduled")
      foot.append(h("button", { class: "btn", onclick: () => act(() => opTransition(ag.id, "arrived")) }, icon("user-check"), "Chegou"));
    if (op === "arrived")
      foot.append(h("button", { class: "btn btn--primary", onclick: () => act(() => opTransition(ag.id, "in_progress")) }, icon("play"), "Iniciar"));
    if (op === "in_progress" || op === "arrived")
      foot.append(h("button", { class: "btn btn--primary", onclick: () => act(() => concluirEFaturar(ag)) }, icon("check"), "Concluir"));
    foot.append(h("button", { class: "btn", onclick: () => reagendarPrompt(ag) }, "Reagendar"));
    foot.append(h("button", { class: "btn btn--danger", onclick: () => cancelarPrompt(ag) }, "Cancelar"));
  } else if (op === "done" && !ag.fatura) {
    foot.append(h("button", { class: "btn btn--primary", onclick: () => act(() => gerarFatura(ag)) }, icon("receipt"), "Gerar fatura"));
  }

  Drawer.open(h("div", { style: "display:flex;flex-direction:column;height:100%" }, head, body, foot));
}

async function opTransition(id, op, confirmar) {
  return jpost(`/api/agendamentos/${id}/op`, { op, confirmar });
}
async function handleActionError(e) {
  if (e.status === 409 && e.body && e.body.precisa_confirmacao) {
    if (confirm(e.body.erro + "\n\nConfirmar mesmo assim?")) return; // caller re-tries with confirmar
  }
  toast(e.message, "err");
}

async function concluirEFaturar(ag) {
  await jpost(`/api/agendamentos/${ag.id}/estado`, { estado: "completed", confirmar: true });
}
async function gerarFatura(ag) {
  let preco = null;
  if (ag.preco_por_confirmar || ag.total_centimos == null) {
    const v = prompt(`Preço deste atendimento (${ag.servico}) em CHF:`, "");
    if (v == null) throw new Error("cancelado");
    const cents = Math.round(parseFloat(v.replace(",", ".")) * 100);
    if (!Number.isFinite(cents) || cents < 0) throw new Error("Preço inválido.");
    preco = cents;
  }
  const inv = await jpost(`/api/agendamentos/${ag.id}/fatura`, preco == null ? {} : { preco_cents: preco });
  toast(`Fatura criada (${chf(inv.total_cents)}) — rascunho`);
}
async function reagendarPrompt(ag) {
  const data = prompt("Nova data (AAAA-MM-DD):", "");
  if (!data) return;
  const hora = prompt("Nova hora (HH:MM):", ag.hora_hhmm || "");
  if (!hora) return;
  try { await jpost(`/api/agendamentos/${ag.id}/reagendar`, { data, hora }); toast("Reagendada."); Drawer.close(); Router.reload(); }
  catch (e) { toast(e.message, "err"); }
}
async function cancelarPrompt(ag) {
  if (!confirm(`Cancelar a marcação de ${ag.nome || "cliente"}?`)) return;
  try { await jpost(`/api/agendamentos/${ag.id}/cancelar`, {}); toast("Cancelada."); Drawer.close(); Router.reload(); }
  catch (e) { toast(e.message, "err"); }
}

/* ===================================================================
   VIEW: HOJE
   =================================================================== */
async function viewHoje(mount) {
  setTitle("Hoje", new Date().toLocaleDateString("pt-PT", { weekday: "long", day: "numeric", month: "long" }));
  mount.append(skeletonCard());
  let d;
  try { d = await api("/api/painel/hoje"); }
  catch (e) { mount.innerHTML = ""; mount.append(emptyState("Não foi possível carregar: " + e.message)); return; }
  mount.innerHTML = "";

  // 1 — operação atual
  mount.append(cockpitCard(d.cartao));

  // 2 — atenção
  const att = d.atencao || [];
  $("#nav-att").hidden = !att.length;
  $("#nav-att").textContent = att.length;
  mount.append(h("div", { class: "eyebrow" }, "Precisa da tua atenção"));
  if (!att.length) {
    mount.append(h("div", { class: "att-list" },
      h("div", { class: "att sev-info" }, icon("check"), h("div", { class: "a-body" }, h("div", { class: "a-title" }, "Tudo em dia.")))));
  } else {
    const groups = { agora: [], hoje: [], quando_puder: [] };
    att.forEach((a) => (groups[a.nivel] || groups.quando_puder).push(a));
    for (const [key, label] of [["agora", "Agora"], ["hoje", "Hoje"], ["quando_puder", "Mais tarde"]]) {
      if (!groups[key].length) continue;
      const list = h("div", { class: "att-list" });
      groups[key].forEach((a) => list.append(attRow(a)));
      mount.append(h("div", { class: "att-group" }, h("div", { class: "att-group-lbl" }, label), list));
    }
  }

  // 3 — agenda de hoje (timeline)
  mount.append(h("div", { class: "eyebrow" }, "Agenda de hoje"));
  const ag = d.agenda || [];
  if (!ag.length) mount.append(h("div", { class: "card card--pad" }, emptyState("Sem marcações hoje.")));
  else {
    const tl = h("div", { class: "card", style: "padding:6px 10px" });
    ag.forEach((m) => {
      const op = m.op_status || "scheduled";
      tl.append(h("div", { class: `tl-row st-${op} st-${(m.estado || "").toLowerCase()}`, onclick: () => openAppointment(m.id) },
        h("span", { class: "tl-time tnum" }, m.hora),
        h("span", { class: "tl-rail" }),
        h("div", {}, h("div", { class: "tl-name" }, m.cliente),
          h("div", { class: "tl-svc" }, `${m.servico} · ${m.preco_por_confirmar ? "a confirmar" : m.preco_label || ""}`)),
        statusBadge(m.estado, op)));
    });
    mount.append(tl);
  }

  // 4 — resumo do dia
  const r = d.resumo || {};
  mount.append(h("div", { class: "eyebrow" }, "Resumo do dia"));
  mount.append(h("div", { class: "metrics" },
    metric(r.marcacoes ?? 0, "Marcações"),
    metric(r.concluidas ?? 0, "Concluídas"),
    metric(r.receita_por_confirmar ? chf(r.receita_cents) + "+" : chf(r.receita_cents), "Receita", true),
    metric(r.novos_clientes ?? 0, "Novos clientes")));
}

function cockpitCard(ck) {
  ck = ck || { kind: "done" };
  const kind = ck.kind === "in_progress" ? "in_progress" : ck.kind === "next" ? "next" : "idle";
  const card = h("div", { class: `cockpit k-${kind}` });
  const m = ck.marcacao;

  if (ck.kind === "done" || !m) {
    card.append(
      h("span", { class: "badge badge--brand k-badge" }, "Dia terminado"),
      h("h2", {}, "Sem mais marcações hoje"),
      h("div", { class: "k-svc" }, `${ck.marcacoes_hoje || 0} marcação(ões) no total`));
    return card;
  }

  if (ck.kind === "in_progress") {
    const total = (ck.decorrido_min || 0) + (ck.restante_min || 0) || 1;
    const pct = Math.min(100, Math.round(((ck.decorrido_min || 0) / total) * 100));
    card.append(
      h("span", { class: "badge k-badge", html: '<span class="dot dot--pulse"></span>' + (ck.atrasado ? "Em curso · a passar da hora" : "Em curso") }),
      h("h2", {}, m.cliente),
      h("div", { class: "k-svc" }, m.servico),
      h("div", { class: "k-track" }, h("i", { style: `width:${pct}%` })),
      h("div", { class: "k-meta" },
        h("span", {}, `${ck.inicio} → ${ck.fim_previsto}`),
        h("span", {}, "Decorrido ", h("b", {}, fmtMin(ck.decorrido_min))),
        h("span", {}, ck.atrasado ? "Atrasado " : "Faltam ", h("b", {}, fmtMin(ck.restante_min)))),
      cockpitCta(m, "in_progress"));
    return card;
  }

  // next
  card.append(
    h("span", { class: "badge badge--warning k-badge" }, ck.chegou ? "Próxima · já chegou" : "Próxima cliente"),
    h("h2", {}, m.cliente),
    h("div", { class: "k-svc" }, m.servico),
    h("div", { class: "k-meta", style: "margin-top:10px" },
      h("span", { class: "tnum" }, ck.hora),
      h("span", {}, ck.faltam_min > 0 ? `daqui a ${fmtMin(ck.faltam_min)}` : "agora"),
      h("span", {}, fmtMin(m.duracao_min)),
      h("span", {}, m.preco_por_confirmar ? "Preço a confirmar" : m.preco_label)),
    (m.cliente_visitas != null) ? h("div", { class: "k-client" },
      h("span", {}, `${m.cliente_visitas} visita(s)`),
      m.cliente_ultima_visita ? h("span", {}, `Última ${fmtDataPt(m.cliente_ultima_visita)}`) : null,
      m.cliente_no_shows ? h("span", { style: "color:var(--danger)" }, `${m.cliente_no_shows} falta(s)`) : null) : null,
    cockpitCta(m, ck.chegou ? "arrived" : "scheduled"));
  return card;
}

function cockpitCta(m, op) {
  const wrap = h("div", { class: "k-cta" });
  const run = async (fn) => { try { await fn(); Router.reload(); } catch (e) { toast(e.message, "err"); } };
  if (op === "scheduled")
    wrap.append(h("button", { class: "btn btn--primary", onclick: () => run(() => opTransition(m.id, "arrived")) }, icon("user-check"), "Chegou"));
  if (op === "arrived")
    wrap.append(h("button", { class: "btn btn--primary", onclick: () => run(() => opTransition(m.id, "in_progress")) }, icon("play"), "Iniciar"));
  if (op === "in_progress")
    wrap.append(h("button", { class: "btn btn--primary", onclick: () => run(() => concluirEFaturar(m)) }, icon("check"), "Concluir"));
  wrap.append(h("button", { class: "btn", onclick: () => openAppointment(m.id) }, "Marcação"));
  return wrap;
}

function attRow(a) {
  const sev = a.nivel === "agora" ? "sev-agora" : a.nivel === "hoje" ? "sev-hoje" : "sev-info";
  const row = h("div", { class: "att " + sev },
    h("div", { class: "a-body" },
      h("div", { class: "a-title" }, a.titulo),
      a.detalhe ? h("div", { class: "a-desc" }, a.detalhe) : null));
  if (a.appointment_id)
    row.append(h("button", { class: "btn btn--sm", onclick: () => openAppointment(a.appointment_id) }, "Abrir"));
  return row;
}

function metric(val, label, accent) {
  return h("div", { class: "metric " + (accent ? "metric--accent" : "") },
    h("div", { class: "m-val tnum" }, String(val)),
    h("div", { class: "m-lbl" }, label));
}

/* ===================================================================
   VIEW: AGENDA  (dia, navegável)
   =================================================================== */
let agendaDia = new Date().toISOString().slice(0, 10);
async function viewAgenda(mount) {
  setTitle("Agenda");
  const nav = h("div", { style: "display:flex;gap:8px;align-items:center;margin-bottom:16px" },
    h("button", { class: "btn btn--sm", onclick: () => shiftDia(-1) }, "‹"),
    h("button", { class: "btn btn--sm", onclick: () => { agendaDia = new Date().toISOString().slice(0, 10); Router.reload(); } }, "Hoje"),
    h("button", { class: "btn btn--sm", onclick: () => shiftDia(1) }, "›"),
    h("strong", { style: "margin-left:6px" }, new Date(agendaDia + "T00:00:00").toLocaleDateString("pt-PT", { weekday: "long", day: "numeric", month: "long" })));
  mount.append(nav, skeletonCard());
  let d;
  try { d = await api(`/api/calendario?inicio=${agendaDia}&fim=${agendaDia}`); }
  catch (e) { toast(e.message, "err"); return; }
  mount.lastChild.remove();
  const evs = (d.eventos || []).filter((e) => e.dia === agendaDia).sort((a, b) => (a.hora_hhmm || "").localeCompare(b.hora_hhmm || ""));
  if (!evs.length) { mount.append(h("div", { class: "card card--pad" }, emptyState("Nada agendado neste dia."))); return; }
  const tl = h("div", { class: "card", style: "padding:6px 10px" });
  evs.forEach((e) => {
    const op = e.op_status || "scheduled";
    tl.append(h("div", { class: `tl-row st-${op} st-${e.estado_chave}`, onclick: () => openAppointment(e.id) },
      h("span", { class: "tl-time tnum" }, e.hora_hhmm || "—"),
      h("span", { class: "tl-rail" }),
      h("div", {}, h("div", { class: "tl-name" }, e.nome || "Cliente"),
        h("div", { class: "tl-svc" }, `${e.servico || ""} · ${e.preco_por_confirmar ? "a confirmar" : chf(e.total_centimos)}`)),
      statusBadge(e.estado, op)));
  });
  mount.append(tl);
}
function shiftDia(n) {
  const d = new Date(agendaDia + "T00:00:00"); d.setDate(d.getDate() + n);
  agendaDia = d.toISOString().slice(0, 10); Router.reload();
}

/* ===================================================================
   VIEW: CLIENTES
   =================================================================== */
async function viewClientes(mount) {
  setTitle("Clientes");
  const search = h("input", { class: "inp", placeholder: "Procurar cliente…", style: "max-width:280px;margin-bottom:16px" });
  mount.append(search, skeletonCard());
  let rows;
  try { rows = await api("/api/clientes"); }
  catch (e) { toast(e.message, "err"); return; }
  mount.lastChild.remove();
  const wrap = h("div", { class: "tbl-wrap" });
  const render = (list) => {
    wrap.innerHTML = "";
    const t = h("table", { class: "tbl" },
      h("thead", {}, h("tr", {},
        h("th", {}, "Cliente"), h("th", {}, "Última visita"), h("th", {}, "Próxima"),
        h("th", { class: "num" }, "Visitas"), h("th", { class: "num" }, "Gasto"))));
    const tb = h("tbody", {});
    list.forEach((c) => tb.append(h("tr", { onclick: () => openCliente(c.id) },
      h("td", {}, h("div", { style: "display:flex;gap:10px;align-items:center" },
        h("span", { class: "avatar" }, initials(c.name)), h("span", {}, c.name || "—"))),
      h("td", {}, c.last_visit ? fmtDataPt(c.last_visit) : "—"),
      h("td", {}, c.next_visit ? fmtDataPt(c.next_visit) : "—"),
      h("td", { class: "num tnum" }, String(c.visits_count ?? 0)),
      h("td", { class: "num tnum" }, chf(c.spend_cents)))));
    t.append(tb); wrap.append(t);
    if (!list.length) wrap.append(emptyState("Nenhum cliente."));
  };
  render(rows);
  mount.append(wrap);
  search.addEventListener("input", () => {
    const q = search.value.trim().toLowerCase();
    render(rows.filter((c) => (c.name || "").toLowerCase().includes(q)));
  });
}

async function openCliente(id) {
  Drawer.open(h("div", { class: "drawer-body" }, h("div", { class: "skel", style: "height:140px" })));
  let d;
  try { d = await api(`/api/clientes/${id}`); }
  catch (e) { toast(e.message, "err"); Drawer.close(); return; }
  const c = d.cliente;
  Drawer.open(h("div", { style: "display:flex;flex-direction:column;height:100%" },
    h("div", { class: "drawer-head" }, icon("users"), h("h3", {}, c.name || "Cliente"),
      h("span", { style: "flex:1" }), h("button", { class: "icon-btn", onclick: () => Drawer.close() }, icon("x"))),
    h("div", { class: "drawer-body" },
      h("div", { class: "eyebrow", style: "margin-top:0" }, "Resumo"),
      h("dl", { class: "dl" },
        h("dt", {}, "Telefone"), h("dd", {}, c.phone || "—"),
        h("dt", {}, "Visitas"), h("dd", { class: "tnum" }, String(c.visits_count ?? 0)),
        h("dt", {}, "Gasto total"), h("dd", { class: "tnum" }, chf(c.spend_cents)),
        h("dt", {}, "Última visita"), h("dd", {}, c.last_visit ? fmtDataPt(c.last_visit) : "—"),
        h("dt", {}, "Próxima"), h("dd", {}, c.next_visit ? fmtDataPt(c.next_visit) : "—"),
        c.no_show_count ? h("dt", {}, "Faltas") : null,
        c.no_show_count ? h("dd", { style: "color:var(--danger)" }, String(c.no_show_count)) : null),
      h("div", { class: "eyebrow" }, "Faturas"),
      (d.faturas && d.faturas.length)
        ? h("div", {}, d.faturas.map((f) => h("div", { class: "att sev-info" },
          icon("receipt"),
          h("div", { class: "a-body" },
            h("div", { class: "a-title" }, f.invoice_number || "Rascunho"),
            h("div", { class: "a-desc" }, `${fmtDataPt(f.issue_date || (f.created_at || "").slice(0, 10))} · ${chf(f.total_cents)} · ${EST_LABEL[f.status] || f.status}`)))))
        : h("div", { class: "empty", style: "padding:16px" }, "Sem faturas."),
      h("div", { class: "eyebrow" }, "Histórico"),
      h("div", { class: "timeline" }, (d.historico || []).slice(0, 20).map((m) => h("div", { class: "tl-row", style: "grid-template-columns:70px 1fr auto", onclick: () => openAppointment(m.id) },
        h("span", { class: "tl-time" }, fmtDataPt(m.data_iso)),
        h("div", {}, h("div", { class: "tl-name" }, m.servico), h("div", { class: "tl-svc" }, m.hora || "")),
        statusBadge(m.estado, m.op_status)))))));
}

/* ===================================================================
   VIEW: SERVIÇOS
   =================================================================== */
async function viewServicos(mount) {
  setTitle("Serviços");
  mount.append(skeletonCard());
  let list;
  try { list = await api("/api/servicos"); }
  catch (e) { toast(e.message, "err"); return; }
  mount.lastChild.remove();
  const box = h("div", { class: "card", style: "padding:4px 6px" });
  list.forEach((s) => {
    box.append(h("div", { class: "tl-row", style: "grid-template-columns:1fr auto auto", onclick: () => editServico(s) },
      h("div", {}, h("div", { class: "tl-name" }, s.nome_pt),
        h("div", { class: "tl-svc" }, fmtMin(s.duracao_min))),
      h("span", { class: "tnum", style: "color:var(--text-2)" }, s.preco_cents == null ? "Preço a confirmar" : chf(s.preco_cents)),
      h("span", { class: "badge " + (s.ativo ? "badge--success" : "") }, s.ativo ? "Ativo" : "Inativo")));
  });
  mount.append(box);
}
function editServico(s) {
  const f = (name, val, attrs = {}) => h("div", { class: "field" }, h("label", {}, name),
    h("input", Object.assign({ class: "inp", value: val ?? "", "data-k": attrs.k }, attrs)));
  const body = h("div", { class: "drawer-body" },
    f("Nome (PT)", s.nome_pt, { k: "nome_pt" }),
    f("Duração (min)", s.duracao_min, { k: "duracao_min", type: "number", min: 5, max: 600 }),
    f("Preço (cêntimos, vazio = a confirmar)", s.preco_cents ?? "", { k: "preco_cents", type: "number", min: 0 }),
    f("Buffer antes (min)", s.buffer_before_min ?? 0, { k: "buffer_before_min", type: "number", min: 0 }),
    f("Buffer depois (min)", s.buffer_after_min ?? 0, { k: "buffer_after_min", type: "number", min: 0 }),
    f("Reagendar após (dias)", s.rebook_days ?? "", { k: "rebook_days", type: "number", min: 0 }));
  const foot = h("div", { class: "drawer-foot" },
    h("button", { class: "btn btn--primary", onclick: async (ev) => {
      const patch = {};
      $$("input[data-k]", body).forEach((i) => { if (i.value !== "") patch[i.dataset.k] = i.type === "number" ? Number(i.value) : i.value; });
      if ($("input[data-k=preco_cents]", body).value === "") patch.preco_cents = null;
      try { await jpatch(`/api/servicos/${s.id}`, patch); toast("Serviço atualizado."); Drawer.close(); Router.reload(); }
      catch (e) { toast(e.message, "err"); }
    } }, "Guardar"));
  Drawer.open(h("div", { style: "display:flex;flex-direction:column;height:100%" },
    h("div", { class: "drawer-head" }, icon("sparkles"), h("h3", {}, s.nome_pt),
      h("span", { style: "flex:1" }), h("button", { class: "icon-btn", onclick: () => Drawer.close() }, icon("x"))),
    body, foot));
}

/* ===================================================================
   VIEW: HORÁRIOS
   =================================================================== */
const DIAS = ["Segunda", "Terça", "Quarta", "Quinta", "Sexta", "Sábado", "Domingo"];
async function viewHorarios(mount) {
  setTitle("Horários");
  mount.append(skeletonCard());
  let d;
  try { d = await api("/api/horarios"); }
  catch (e) { toast(e.message, "err"); return; }
  mount.lastChild.remove();
  const grelha = d.grelha || [];
  const box = h("div", { class: "card", style: "padding:4px 6px" });
  DIAS.forEach((nome, wd) => {
    const dia = grelha.find((g) => g.weekday === wd) || {};
    const aberto = dia.opens && dia.closes;
    box.append(h("div", { class: "tl-row", style: "grid-template-columns:110px 1fr" },
      h("div", { class: "tl-name" }, nome),
      aberto
        ? h("div", { class: "tnum", style: "color:var(--text-2)" }, `${dia.opens} – ${dia.closes}` + (dia.break_start ? `  ·  pausa ${dia.break_start}–${dia.break_end}` : ""))
        : h("span", { class: "badge" }, "Fechado")));
  });
  mount.append(box);
  const exc = d.excecoes || [];
  mount.append(h("div", { class: "eyebrow" }, "Exceções / ausências"));
  if (!exc.length) mount.append(h("div", { class: "empty", style: "padding:14px" }, "Sem exceções."));
  else mount.append(h("div", { class: "card", style: "padding:4px 6px" }, exc.map((e) => h("div", { class: "tl-row", style: "grid-template-columns:1fr auto" },
    h("div", {}, h("div", { class: "tl-name" }, fmtDataPt(e.day || e.data_inicio)), h("div", { class: "tl-svc" }, e.reason || "")),
    h("span", { class: "badge " + (e.closed ? "badge--danger" : "") }, e.closed ? "Fechado" : "Horário especial")))));
  mount.append(h("p", { class: "empty", style: "text-align:left;padding:16px 2px" },
    "Edição de horários e exceções: próxima iteração."));
}

/* ===================================================================
   VIEW: FATURAS
   =================================================================== */
let faturaFiltro = "all";
async function viewFaturas(mount) {
  setTitle("Faturas");
  const filtros = h("div", { class: "tabs" });
  [["all", "Todas"], ["draft", "Rascunho"], ["issued", "Emitidas"], ["paid", "Pagas"], ["overdue", "Vencidas"], ["cancelled", "Anuladas"]]
    .forEach(([k, l]) => filtros.append(h("button", { class: "tab " + (faturaFiltro === k ? "is-active" : ""), onclick: () => { faturaFiltro = k; Router.reload(); } }, l)));
  mount.append(filtros, skeletonCard());
  let list;
  try { list = await api("/api/faturas?estado=" + faturaFiltro); }
  catch (e) { toast(e.message, "err"); return; }
  mount.lastChild.remove();
  if (!list.length) { mount.append(h("div", { class: "card card--pad" }, emptyState("Nenhuma fatura."))); return; }
  const wrap = h("div", { class: "tbl-wrap" },
    h("table", { class: "tbl" },
      h("thead", {}, h("tr", {}, h("th", {}, "#"), h("th", {}, "Cliente"), h("th", {}, "Data"), h("th", { class: "num" }, "Total"), h("th", {}, "Estado"))),
      h("tbody", {}, list.map((f) => h("tr", { onclick: () => openFatura(f.id) },
        h("td", { class: "tnum" }, f.invoice_number || "—"),
        h("td", {}, f.customer_name_snapshot || "—"),
        h("td", {}, fmtDataPt(f.issue_date || (f.created_at || "").slice(0, 10))),
        h("td", { class: "num tnum" }, chf(f.total_cents)),
        h("td", {}, invoiceBadge(f.status, f.due_date)))))));
  mount.append(wrap);
}
function invoiceBadge(status, due) {
  if (status === "issued" && due && due < new Date().toISOString().slice(0, 10))
    return h("span", { class: "badge badge--danger" }, "Vencida");
  const map = { draft: ["", "Rascunho"], issued: ["badge--info", "Emitida"], paid: ["badge--success", "Paga"], cancelled: ["", "Anulada"] };
  const [c, l] = map[status] || ["", status];
  return h("span", { class: "badge " + c }, l);
}
async function openFatura(id) {
  Drawer.open(h("div", { class: "drawer-body" }, h("div", { class: "skel", style: "height:160px" })));
  let f;
  try { f = await api(`/api/faturas/${id}`); }
  catch (e) { toast(e.message, "err"); Drawer.close(); return; }
  const foot = h("div", { class: "drawer-foot" });
  const run = async (fn) => { try { await fn(); toast("Feito."); Drawer.close(); Router.reload(); } catch (e) { toast(e.message, "err"); } };
  if (f.status === "draft") {
    foot.append(h("button", { class: "btn btn--primary", onclick: () => run(() => jpost(`/api/faturas/${id}/emitir`)) }, "Emitir"));
    foot.append(h("button", { class: "btn btn--danger", onclick: () => run(() => jpost(`/api/faturas/${id}/anular`)) }, "Anular"));
  } else if (f.status === "issued") {
    foot.append(h("button", { class: "btn btn--primary", onclick: () => run(() => jpost(`/api/faturas/${id}/pagar`)) }, "Marcar paga"));
    foot.append(h("button", { class: "btn btn--danger", onclick: () => run(() => jpost(`/api/faturas/${id}/anular`)) }, "Anular"));
  }
  foot.append(h("button", { class: "btn", disabled: true, title: "Próxima iteração" }, "PDF"));

  Drawer.open(h("div", { style: "display:flex;flex-direction:column;height:100%" },
    h("div", { class: "drawer-head" }, icon("receipt"), h("h3", {}, f.invoice_number || "Rascunho"),
      h("span", { style: "flex:1" }), h("button", { class: "icon-btn", onclick: () => Drawer.close() }, icon("x"))),
    h("div", { class: "drawer-body" },
      invoiceBadge(f.status, f.due_date),
      h("dl", { class: "dl", style: "margin:14px 0" },
        h("dt", {}, "Cliente"), h("dd", {}, f.customer_name_snapshot || "—"),
        h("dt", {}, "Emitida"), h("dd", {}, f.issue_date ? fmtDataPt(f.issue_date) : "—"),
        h("dt", {}, "Vencimento"), h("dd", {}, f.due_date ? fmtDataPt(f.due_date) : "—")),
      h("div", { class: "tbl-wrap" }, h("table", { class: "tbl" },
        h("thead", {}, h("tr", {}, h("th", {}, "Descrição"), h("th", { class: "num" }, "Qtd"), h("th", { class: "num" }, "Preço"), h("th", { class: "num" }, "Total"))),
        h("tbody", {}, (f.lines || []).map((l) => h("tr", {},
          h("td", {}, l.description), h("td", { class: "num tnum" }, String(l.quantity)),
          h("td", { class: "num tnum" }, chf(l.unit_price_cents)), h("td", { class: "num tnum" }, chf(l.line_total_cents))))))),
      h("dl", { class: "dl", style: "margin-top:14px" },
        h("dt", {}, "Subtotal"), h("dd", { class: "tnum" }, chf(f.subtotal_cents)),
        f.discount_cents ? h("dt", {}, "Desconto") : null,
        f.discount_cents ? h("dd", { class: "tnum" }, "− " + chf(f.discount_cents)) : null,
        h("dt", {}, f.tax_rate_bps ? `IVA (${(f.tax_rate_bps / 100).toFixed(2)}%)` : "IVA"),
        h("dd", { class: "tnum" }, f.tax_rate_bps ? chf(f.tax_cents) : "—"),
        h("dt", { style: "font-weight:600;color:var(--text)" }, "Total"),
        h("dd", { class: "tnum", style: "font-weight:700" }, chf(f.total_cents)))),
    foot));
}

/* ===================================================================
   VIEW: DEFINIÇÕES  (faturação)
   =================================================================== */
async function viewDefinicoes(mount) {
  setTitle("Definições", "Faturação");
  mount.append(skeletonCard());
  let cfg;
  try { cfg = await api("/api/definicoes/faturacao"); }
  catch (e) { toast(e.message, "err"); return; }
  mount.lastChild.remove();
  const f = (name, k, val, attrs = {}) => h("div", { class: "field" }, h("label", {}, name),
    h("input", Object.assign({ class: "inp", value: val ?? "", "data-k": k }, attrs)));
  const form = h("div", { class: "card card--pad", style: "max-width:520px" },
    h("div", { class: "eyebrow", style: "margin-top:0" }, "Dados do negócio (aparecem na fatura)"),
    f("Nome legal", "legal_name", cfg.legal_name),
    f("Morada", "address", cfg.address),
    h("div", { style: "display:flex;gap:12px" },
      f("Código postal", "postal_code", cfg.postal_code, { style: "max-width:140px" }),
      f("Localidade", "city", cfg.city)),
    f("Email", "email", cfg.email, { type: "email" }),
    f("Telefone", "phone", cfg.phone),
    f("IBAN", "iban", cfg.iban, { placeholder: "só se quiseres que apareça na fatura" }),
    h("div", { class: "eyebrow" }, "IVA / MWST"),
    h("label", { class: "field", style: "display:flex;gap:8px;align-items:center" },
      h("input", { type: "checkbox", "data-k": "vat_enabled", checked: cfg.vat_enabled || undefined }),
      h("span", {}, "Cobrar IVA nas faturas")),
    f("Taxa de IVA (basis points, ex.: 810 = 8,10%)", "vat_rate_bps", cfg.vat_rate_bps || "", { type: "number", min: 0 }),
    f("Número de IVA (UID)", "vat_number", cfg.vat_number),
    h("div", { class: "eyebrow" }, "Faturação"),
    f("Prefixo do número", "invoice_prefix", cfg.invoice_prefix, { placeholder: "vazio → 2026-0001" }),
    f("Prazo de pagamento (dias)", "payment_terms_days", cfg.payment_terms_days ?? 30, { type: "number", min: 0 }),
    f("Rodapé da fatura", "invoice_footer", cfg.invoice_footer),
    h("button", { class: "btn btn--primary", style: "margin-top:8px", onclick: async () => {
      const patch = {};
      $$("[data-k]", form).forEach((i) => {
        if (i.type === "checkbox") patch[i.dataset.k] = i.checked;
        else if (i.value !== "") patch[i.dataset.k] = i.type === "number" ? Number(i.value) : i.value;
        else patch[i.dataset.k] = null;
      });
      try { await jput("/api/definicoes/faturacao", patch); toast("Definições guardadas."); }
      catch (e) { toast(e.message, "err"); }
    } }, "Guardar"));
  mount.append(form);
  mount.append(h("p", { class: "empty", style: "text-align:left;padding:16px 2px;max-width:520px" },
    "Nada é preenchido automaticamente — IBAN, número de IVA e taxa só aparecem na fatura depois de os introduzires aqui."));
}

/* ===================================================================
   ROUTER
   =================================================================== */
const ROUTES = {
  hoje: viewHoje, agenda: viewAgenda, clientes: viewClientes,
  servicos: viewServicos, horarios: viewHorarios, faturas: viewFaturas, definicoes: viewDefinicoes,
};
const Router = {
  current: "hoje",
  reload() { this.render(this.current); },
  render(name) {
    this.current = name;
    const view = $("#view");
    view.innerHTML = "";
    view.classList.toggle("view-narrow", ["definicoes"].includes(name));
    $$(".nav-item").forEach((a) => a.classList.toggle("is-active", a.getAttribute("href") === "#/" + name));
    (ROUTES[name] || viewHoje)(view).catch((e) => { view.innerHTML = ""; view.append(emptyState(e.message)); });
    $("#sidebar").classList.remove("open");
    $("#scrim").classList.remove("open");
  },
};
function setTitle(t, sub = "") { $("#page-title").textContent = t; $("#page-sub").textContent = sub; }

function parseHash() {
  const m = (location.hash || "#/hoje").replace(/^#\//, "").split("/")[0];
  return ROUTES[m] ? m : "hoje";
}
window.addEventListener("hashchange", () => Router.render(parseHash()));

/* ---------- boot ---------- */
document.addEventListener("DOMContentLoaded", () => {
  Drawer.init();
  $("#menu-btn").addEventListener("click", () => { $("#sidebar").classList.toggle("open"); $("#scrim").classList.toggle("open"); });
  $("#scrim").addEventListener("click", () => { $("#sidebar").classList.remove("open"); });
  const THEME_KEY = "db_theme";
  const prefersDark = () => window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches;
  const themeBtn = $("#theme-btn");
  const themeBtnIcon = $("#theme-btn-icon");
  const applyTheme = (t) => {
    document.documentElement.dataset.theme = t;
    themeBtnIcon.setAttribute("href", t === "dark" ? "#i-sun" : "#i-moon");
    themeBtn.setAttribute("aria-label", t === "dark" ? "Ativar modo claro" : "Ativar modo escuro");
  };
  applyTheme(prefersDark() ? "dark" : "light");
  try { const saved = localStorage.getItem(THEME_KEY); if (saved) applyTheme(saved); } catch (e) {}
  themeBtn.addEventListener("click", () => {
    const cur = document.documentElement.dataset.theme || (prefersDark() ? "dark" : "light");
    const next = cur === "light" ? "dark" : "light";
    applyTheme(next);
    try { localStorage.setItem(THEME_KEY, next); } catch (e) {}
  });
  if (!location.hash) location.hash = "#/hoje";
  Router.render(parseHash());
});
