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

const OP_LABEL = { scheduled: "Agendada", arrived: "Chegou", in_progress: "Em curso", done: "Concluída" };
const EST_LABEL = { confirmed: "Confirmada", pending: "Pendente", cancelled: "Cancelada", completed: "Concluída", no_show: "Não compareceu" };
function statusBadge(estado, op, bloqueiaHorario) {
  const e = (estado || "").toLowerCase();
  if (e === "cancelled")
    return h("span", { class: "badge badge--danger" }, bloqueiaHorario ? "Cancelada · horário bloqueado" : "Cancelada");
  if (e === "no_show") return h("span", { class: "badge badge--danger" }, EST_LABEL.no_show);
  if (op === "done" || e === "completed") return h("span", { class: "badge badge--success" }, "Concluída");
  if (op === "in_progress") return h("span", { class: "badge badge--warning" }, "Em curso");
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
    statusBadge(ag.estado, op, ag.bloqueia_horario),
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
    if (op === "scheduled") {
      foot.append(h("button", { class: "btn", onclick: () => act(() => opTransition(ag.id, "arrived")) }, icon("user-check"), "Chegou"));
      foot.append(h("button", { class: "btn btn--danger", onclick: () => naoCompareceuPrompt(ag) }, "Não compareceu"));
    }
    if (op === "arrived")
      foot.append(h("button", { class: "btn btn--primary", onclick: () => act(() => opTransition(ag.id, "in_progress")) }, icon("play"), "Iniciar"));
    if (op === "in_progress" || op === "arrived")
      foot.append(h("button", { class: "btn btn--primary", onclick: () => act(() => concluirEFaturar(ag)) }, icon("check"), "Concluir"));
    foot.append(h("button", { class: "btn", onclick: () => openEditAppointment(ag) }, "Editar"));
    foot.append(h("button", { class: "btn btn--danger", onclick: () => cancelarPrompt(ag) }, "Cancelar"));
  } else if (op === "done" && !ag.fatura) {
    foot.append(h("button", { class: "btn btn--primary", onclick: () => act(() => gerarFatura(ag)) }, icon("receipt"), "Gerar fatura"));
  } else if (est === "cancelled") {
    // Uma cancelada nunca reabre (não há "Chegou"/"Iniciar"/"Concluir"
    // aqui): "Remarcar" e "Marcar novamente" criam sempre uma marcação
    // NOVA (POST /api/agendamentos) — a cancelada em si fica intacta.
    foot.append(h("button", { class: "btn btn--primary",
      onclick: () => openCreateAppointment({ nome: ag.nome, telefone: ag.telefone, servico_id: ag.servico_id }) },
      icon("calendar"), "Remarcar"));
    foot.append(h("button", { class: "btn",
      onclick: () => openCreateAppointment({ nome: ag.nome, telefone: ag.telefone }) },
      "Marcar novamente"));
    if (ag.customer_id) {
      foot.append(h("button", { class: "btn", onclick: () => openCliente(ag.customer_id, "whatsapp") }, icon("sparkles"), "WhatsApp"));
      foot.append(h("button", { class: "btn", onclick: () => openCliente(ag.customer_id) }, "Abrir cliente"));
    }
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
async function cancelarPrompt(ag) {
  if (!confirm(`Cancelar a marcação de ${ag.nome || "cliente"}?`)) return;
  try { await jpost(`/api/agendamentos/${ag.id}/cancelar`, {}); toast("Cancelada."); Drawer.close(); Router.reload(); }
  catch (e) { toast(e.message, "err"); }
}
async function naoCompareceuPrompt(ag) {
  if (!confirm(`Marcar ${ag.nome || "cliente"} como Não compareceu?`)) return;
  const tentar = async (confirmar) => {
    try {
      await jpost(`/api/agendamentos/${ag.id}/estado`, { estado: "no_show", confirmar });
      toast("Marcada como Não compareceu.");
      Drawer.close(); Router.reload();
    } catch (e) {
      if (e.status === 409 && e.body && e.body.precisa_confirmacao && confirm(e.body.erro + "\n\nConfirmar mesmo assim?"))
        return tentar(true);
      toast(e.message, "err");
    }
  };
  await tentar(false);
}

/* ---------- caches partilhadas (evitam pedidos redundantes) ---------- */
let _servicosCache = null;
async function getServicos() {
  if (!_servicosCache) _servicosCache = await api("/api/servicos");
  return _servicosCache;
}
let _clientesCache = null;
async function getClientesCache() {
  if (!_clientesCache) _clientesCache = await api("/api/clientes");
  return _clientesCache;
}
function invalidateClientesCache() { _clientesCache = null; }
const HORA_RE = /^([01]\d|2[0-3]):[0-5]\d$/;

/* ===================================================================
   NOVA MARCAÇÃO — POST /api/agendamentos (reutiliza guardar_agendamento)
   =================================================================== */
async function openCreateAppointment(prefill = {}) {
  let servicos, clientes;
  try { [servicos, clientes] = await Promise.all([getServicos(), getClientesCache()]); }
  catch (e) { toast(e.message, "err"); return; }
  servicos = servicos.filter((s) => s.ativo);

  const f = (label, node) => h("div", { class: "field" }, h("label", {}, label), node);
  const dlId = "dl-clientes-nova";
  const iNome = h("input", { class: "inp", placeholder: "Nome do cliente", list: dlId, autocomplete: "off",
    value: prefill.nome || "" });
  const iTelefone = h("input", { class: "inp", placeholder: "+41 79 000 00 00", type: "tel",
    value: prefill.telefone || "" });
  const datalist = h("datalist", { id: dlId }, clientes.map((c) => h("option", { value: c.name })));
  iNome.addEventListener("input", () => {
    const match = clientes.find((c) => (c.name || "").toLowerCase() === iNome.value.trim().toLowerCase());
    if (match && match.phone) iTelefone.value = match.phone;
  });

  const selServico = h("select", { class: "inp" },
    h("option", { value: "", disabled: true, selected: !prefill.servico_id }, "Escolher serviço…"),
    servicos.map((s) => h("option", { value: s.id, selected: s.id === prefill.servico_id || undefined },
      `${s.nome_pt} · ${fmtMin(s.duracao_min)}` + (s.preco_cents == null ? "" : ` · ${chf(s.preco_cents)}`))));

  const iData = h("input", { class: "inp", type: "date", value: prefill.data || agendaDia });
  const iHora = h("input", { class: "inp", type: "time", value: prefill.hora || "" });
  const iDuracao = h("input", { class: "inp", type: "number", min: 5, max: 600, placeholder: "do serviço" });
  const iPreco = h("input", { class: "inp", type: "number", min: 0, step: "0.05", placeholder: "a confirmar" });
  const iNotas = h("textarea", { class: "inp", rows: 3, style: "resize:vertical" });

  selServico.addEventListener("change", () => {
    const s = servicos.find((x) => x.id === selServico.value);
    if (!s) return;
    iPreco.placeholder = s.preco_cents == null ? "a confirmar" : (s.preco_cents / 100).toFixed(2);
    iDuracao.placeholder = String(s.duracao_min) + " min";
  });

  const body = h("div", { class: "drawer-body" },
    datalist,
    f("Cliente", iNome), f("Telefone", iTelefone),
    f("Serviço", selServico),
    h("div", { style: "display:flex;gap:12px" }, f("Data", iData), f("Hora", iHora)),
    h("div", { style: "display:flex;gap:12px" },
      f("Duração (min)", iDuracao), f("Preço (CHF)", iPreco)),
    f("Notas (opcional)", iNotas));

  const btnGuardar = h("button", { class: "btn btn--primary" }, "Criar marcação");
  const foot = h("div", { class: "drawer-foot" },
    h("button", { class: "btn", onclick: () => Drawer.close() }, "Cancelar"), btnGuardar);

  btnGuardar.addEventListener("click", async () => {
    const nome = iNome.value.trim(), telefone = iTelefone.value.trim();
    if (!nome) { toast("Indica o nome do cliente.", "err"); return; }
    if (!telefone) { toast("Indica o telefone do cliente.", "err"); return; }
    if (!selServico.value) { toast("Escolhe um serviço.", "err"); return; }
    if (!iData.value) { toast("Escolhe uma data.", "err"); return; }
    if (!HORA_RE.test(iHora.value)) { toast("Indica uma hora válida.", "err"); return; }

    const payload = { nome, telefone, servico_id: selServico.value, data: iData.value, hora: iHora.value };
    if (iDuracao.value) payload.duracao_min = Number(iDuracao.value);
    if (iPreco.value.trim() !== "") {
      const cents = Math.round(parseFloat(iPreco.value.replace(",", ".")) * 100);
      if (!Number.isFinite(cents) || cents < 0) { toast("Preço inválido.", "err"); return; }
      payload.preco_cents = cents;
    }
    if (iNotas.value.trim()) payload.notas = iNotas.value.trim();

    btnGuardar.disabled = true;
    try {
      await jpost("/api/agendamentos", payload);
      toast("Marcação criada.");
      invalidateClientesCache();
      Drawer.close();
      Router.reload();
    } catch (e) {
      toast(e.message, "err");
      btnGuardar.disabled = false;
    }
  });

  Drawer.open(h("div", { style: "display:flex;flex-direction:column;height:100%" },
    h("div", { class: "drawer-head" }, icon("calendar"), h("h3", {}, "Nova marcação"),
      h("span", { style: "flex:1" }), h("button", { class: "icon-btn", onclick: () => Drawer.close() }, icon("x"))),
    body, foot));
}

/* ===================================================================
   EDITAR MARCAÇÃO — /editar (serviço/duração/preço/notas) + /reagendar
   (data/hora), na mesma marcação. Cancelar/concluir/estado operacional/
   cliente/fatura mantêm-se nas suas próprias ações.
   =================================================================== */
async function openEditAppointment(ag) {
  let servicos;
  try { servicos = await getServicos(); }
  catch (e) { toast(e.message, "err"); return; }
  servicos = servicos.filter((s) => s.ativo || s.id === ag.servico_id);

  const f = (label, node) => h("div", { class: "field" }, h("label", {}, label), node);
  const iData = h("input", { class: "inp", type: "date", value: ag.data_iso || "" });
  const iHora = h("input", { class: "inp", type: "time", value: ag.hora_hhmm || "" });
  const selServico = h("select", { class: "inp" },
    servicos.map((s) => h("option", { value: s.id, selected: s.id === ag.servico_id || undefined }, s.nome_pt)));
  const iDuracao = h("input", { class: "inp", type: "number", min: 5, max: 600, value: ag.duracao_min || "" });
  const iPreco = h("input", { class: "inp", type: "number", min: 0, step: "0.05", placeholder: "a confirmar",
    value: ag.preco_cents == null ? "" : (ag.preco_cents / 100).toFixed(2) });
  const iNotas = h("textarea", { class: "inp", rows: 3, style: "resize:vertical" }, ag.extra || "");

  const body = h("div", { class: "drawer-body" },
    h("div", { style: "display:flex;gap:12px" }, f("Data", iData), f("Hora", iHora)),
    f("Serviço", selServico),
    h("div", { style: "display:flex;gap:12px" },
      f("Duração (min)", iDuracao), f("Preço (CHF, vazio = a confirmar)", iPreco)),
    f("Notas", iNotas));

  const btnGuardar = h("button", { class: "btn btn--primary" }, "Guardar alterações");
  const foot = h("div", { class: "drawer-foot" },
    h("button", { class: "btn", onclick: () => renderAppointment(ag) }, "Voltar"), btnGuardar);

  btnGuardar.addEventListener("click", async () => {
    if (!iData.value || !HORA_RE.test(iHora.value)) { toast("Data e hora são obrigatórias.", "err"); return; }
    const patch = {};
    if (selServico.value !== ag.servico_id) patch.servico_id = selServico.value;
    const dur = Number(iDuracao.value);
    if (Number.isFinite(dur) && dur > 0 && dur !== ag.duracao_min) patch.duracao_min = dur;
    const precoTxt = iPreco.value.trim();
    if (precoTxt === "") { if (ag.preco_cents != null) patch.preco_cents = null; }
    else {
      const cents = Math.round(parseFloat(precoTxt.replace(",", ".")) * 100);
      if (!Number.isFinite(cents) || cents < 0) { toast("Preço inválido.", "err"); return; }
      if (cents !== ag.preco_cents) patch.preco_cents = cents;
    }
    const notasTxt = iNotas.value.trim();
    if (notasTxt !== (ag.extra || "")) patch.notas = notasTxt;

    btnGuardar.disabled = true;
    try {
      if (Object.keys(patch).length) await jpost(`/api/agendamentos/${ag.id}/editar`, patch);
      if (iData.value !== ag.data_iso || iHora.value !== ag.hora_hhmm)
        await jpost(`/api/agendamentos/${ag.id}/reagendar`, { data: iData.value, hora: iHora.value });
      toast("Marcação atualizada.");
      Drawer.close(); Router.reload();
    } catch (e) { toast(e.message, "err"); btnGuardar.disabled = false; }
  });

  Drawer.open(h("div", { style: "display:flex;flex-direction:column;height:100%" },
    h("div", { class: "drawer-head" }, icon("calendar"), h("h3", {}, `Editar marcação #${ag.id}`),
      h("span", { style: "flex:1" }), h("button", { class: "icon-btn", onclick: () => Drawer.close() }, icon("x"))),
    body, foot));
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

  // 2 — atenção (agregada)
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
      // Aggregate by tipo within each nivel
      const byTipo = {};
      groups[key].forEach((a) => {
        const t = a.tipo || "_single";
        if (!byTipo[t]) byTipo[t] = [];
        byTipo[t].push(a);
      });
      for (const [tipo, items] of Object.entries(byTipo)) {
        if (items.length >= 2 && tipo !== "_single") {
          list.append(attAggregatedRow(tipo, items));
        } else {
          items.forEach((a) => list.append(attRow(a)));
        }
      }
      mount.append(h("div", { class: "att-group" }, h("div", { class: "att-group-lbl" }, label), list));
    }
  }

  // 3 — agenda de hoje (timeline)
  mount.append(h("div", { style: "display:flex;align-items:center;justify-content:space-between;gap:8px;margin:32px 2px 12px" },
    h("div", { class: "eyebrow", style: "margin:0" }, "Agenda de hoje"),
    h("button", { class: "btn btn--sm", onclick: () => openCreateAppointment({ data: ymdOf(new Date()) }) }, icon("calendar"), "Nova marcação")));
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
  if (op === "in_progress") {
    wrap.append(h("button", { class: "btn btn--primary", onclick: () => run(() => concluirEFaturar(m)) }, icon("check"), "Concluir"));
    wrap.append(h("button", { class: "btn btn--secondary", disabled: true, title: "Funcionalidade pendente de endpoint backend" }, icon("clock"), "+15 min"));
    if (m.customer_id)
      wrap.append(h("button", { class: "btn btn--secondary", onclick: () => openCliente(m.customer_id) }, icon("user-check"), "Cliente"));
  }
  wrap.append(h("button", { class: "btn btn--subtle", onclick: () => openAppointment(m.id) }, "Marcação"));
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

const ATT_TYPE_LABELS = {
  preco_pendente: (n) => `${n} preços por confirmar`,
  automacao_falhou: (n) => `${n} notificações por enviar`,
  needs_human: (n) => `${n} cliente(s) pediram ajuda`,
  risco_no_show: (n) => `${n} cliente(s) em risco`,
};
function attAggregatedRow(tipo, items) {
  const sev = items[0].nivel === "agora" ? "sev-agora" : items[0].nivel === "hoje" ? "sev-hoje" : "sev-info";
  const labelFn = ATT_TYPE_LABELS[tipo];
  const title = labelFn ? labelFn(items.length) : `${items.length} itens`;
  // Extract client names from titles — strip prefix like "Preço por definir — "
  const names = items.map((a) => {
    const raw = a.titulo || "";
    const dash = raw.indexOf(" — ");
    return dash >= 0 ? raw.slice(dash + 3).split(" · ")[0] : raw;
  });
  const row = h("div", { class: "att " + sev },
    h("div", { class: "a-body" },
      h("div", { class: "a-title" }, title),
      h("div", { class: "a-desc" }, names.join(" · "))));
  if (items[0].appointment_id)
    row.append(h("button", { class: "btn btn--sm", onclick: () => openAppointment(items[0].appointment_id) }, "Ver"));
  return row;
}

function metric(val, label, accent) {
  return h("div", { class: "metric " + (accent ? "metric--accent" : "") },
    h("div", { class: "m-val tnum" }, String(val)),
    h("div", { class: "m-lbl" }, label));
}

/* ===================================================================
   VIEW: AGENDA  (dia · semana, navegável)
   A vista semanal reutiliza as REGRAS da grelha semanal legada:
   semana começa à segunda-feira, grelha horária vertical com bandas de
   intervalo_min, posicionamento top = (minuto-inicio)*(px/intervalo),
   altura proporcional à duração, repartição de sobrepostos em colunas
   e linha "Agora" só na coluna de hoje. Renderizado no design system
   V2 (tokens, badges, drawer partilhado). Uma única chamada a
   /api/calendario?inicio=&fim= cobre a semana — sem endpoint novo.
   =================================================================== */
let agendaVista = "dia";                                 // "dia" | "semana"
let agendaDia = ymdOf(new Date());   // âncora (YYYY-MM-DD)
let agGrelha = { hora_inicio: 8, hora_fim: 19, intervalo_min: 30 };  // vem do servidor
let agendaFiltro = "all";
let agendaBusca = "";

const AG_FILTROS = [
  ["all", "Todas"], ["confirmed", "Confirmadas"], ["pending", "Pendentes"],
  ["in_progress", "Em curso"], ["completed", "Concluídas"], ["cancelled", "Canceladas"],
  ["no_show", "Não compareceu"],
];
function matchFiltroAg(e, filtro) {
  const estado = (e.estado_chave || "").toLowerCase();
  if (filtro === "all") {
    // Cancelada que já libertou o horário é ruído na Agenda operacional —
    // só entra em "Todas" quando ainda BLOQUEIA o horário (continua a
    // ocupar o slot, por isso continua a precisar de ser vista). A que
    // libertou só aparece com o filtro "Canceladas" ligado, ou no
    // histórico do cliente.
    return estado !== "cancelled" || !!e.bloqueia_horario;
  }
  if (filtro === "in_progress") return (e.op_status || "") === "in_progress";
  return estado === filtro;
}
function matchBuscaAg(e, q) {
  if (!q) return true;
  return `${e.nome || ""} ${e.telefone || ""} ${e.servico || ""}`.toLowerCase().includes(q);
}

const DAY_SHORT = ["seg", "ter", "qua", "qui", "sex", "sáb", "dom"];
function ymdOf(d) { return d.getFullYear() + "-" + String(d.getMonth() + 1).padStart(2, "0") + "-" + String(d.getDate()).padStart(2, "0"); }
function dateOf(s) {
  if (s instanceof Date) return new Date(s);
  return new Date(String(s).trim() + "T00:00:00");
}
function addDays(d, n) { const x = new Date(d); x.setDate(x.getDate() + n); return x; }
// A semana começa sempre à SEGUNDA-feira (mesma regra do legado).
function mondayOf(anchor) {
  const d = dateOf(anchor);
  const off = (d.getDay() + 6) % 7;   // segunda = 0 … domingo = 6
  return addDays(d, -off);
}
function weekDays(anchor) { const mon = mondayOf(anchor); return Array.from({ length: 7 }, (_, i) => addDays(mon, i)); }

async function viewAgenda(mount) {
  setTitle("Agenda");
  const isWeek = agendaVista === "semana";
  const anchor = isWeek ? ymdOf(mondayOf(agendaDia)) : agendaDia;
  const fim = isWeek ? ymdOf(addDays(dateOf(anchor), 6)) : anchor;

  const nav = h("div", { class: "ag-toolbar" },
    h("div", { class: "ag-nav" },
      h("button", { class: "btn btn--sm", "aria-label": "Anterior", onclick: () => shiftAgenda(-1) }, "‹"),
      h("button", { class: "btn btn--sm", onclick: () => { agendaDia = ymdOf(new Date()); Router.reload(); } }, "Hoje"),
      h("button", { class: "btn btn--sm", "aria-label": "Seguinte", onclick: () => shiftAgenda(1) }, "›")),
    h("div", { class: "seg", role: "tablist", "aria-label": "Vista da agenda" },
      h("button", { class: (isWeek ? "" : "is-active"), role: "tab", "aria-selected": String(!isWeek), onclick: () => setAgendaVista("dia") }, "Dia"),
      h("button", { class: (isWeek ? "is-active" : ""), role: "tab", "aria-selected": String(isWeek), onclick: () => setAgendaVista("semana") }, "Semana")),
    h("strong", { class: "ag-range tnum" }, agRangeLabel()),
    h("span", { class: "nav-spacer" }),
    h("button", { class: "btn btn--sm btn--primary", onclick: () => openCreateAppointment({ data: agendaDia }) }, icon("calendar"), "Nova marcação"));

  const chipsWrap = h("div", { class: "ag-filtros" });
  function renderChips() {
    chipsWrap.innerHTML = "";
    AG_FILTROS.forEach(([k, l]) => chipsWrap.append(h("button", {
      class: "chip" + (agendaFiltro === k ? " is-active" : ""),
      onclick: () => { agendaFiltro = k; renderChips(); renderList(); },
    }, l)));
  }
  renderChips();

  const searchInp = h("input", { class: "inp ag-search", type: "search",
    placeholder: "Procurar por nome, telefone ou serviço…", value: agendaBusca });
  searchInp.addEventListener("input", () => { agendaBusca = searchInp.value; renderList(); });

  mount.append(nav, chipsWrap, searchInp, skeletonCard());

  let d;
  try { d = await api(`/api/calendario?inicio=${anchor}&fim=${fim}`); }
  catch (e) { toast(e.message, "err"); return; }
  mount.lastChild.remove();
  if (d.grelha) agGrelha = { hora_inicio: d.grelha.hora_inicio ?? 8, hora_fim: d.grelha.hora_fim ?? 19, intervalo_min: d.grelha.intervalo_min ?? 30 };

  const listMount = h("div", {});
  mount.append(listMount);

  function renderList() {
    listMount.innerHTML = "";
    const q = agendaBusca.trim().toLowerCase();
    const filtrados = (d.eventos || []).filter((e) => matchFiltroAg(e, agendaFiltro) && matchBuscaAg(e, q));
    if (isWeek) {
      const evs = filtrados.filter((e) => e.dia >= anchor && e.dia <= fim);
      listMount.append(renderSemana(evs));
    } else {
      const evs = filtrados.filter((e) => e.dia === anchor).sort((a, b) => (a.hora_hhmm || "").localeCompare(b.hora_hhmm || ""));
      listMount.append(renderDiaGrid(evs, anchor));
    }
  }
  renderList();
}

function setAgendaVista(v) {
  if (agendaVista === v) return;
  agendaVista = v;
  Router.reload();
}
function shiftAgenda(n) {
  const step = agendaVista === "semana" ? 7 : 1;
  const d = dateOf(agendaDia); d.setDate(d.getDate() + step * n);
  agendaDia = ymdOf(d);
  Router.reload();
}

function agRangeLabel() {
  if (agendaVista === "dia") {
    return dateOf(agendaDia).toLocaleDateString("pt-PT", { weekday: "long", day: "numeric", month: "long", year: "numeric" });
  }
  const seg = mondayOf(agendaDia), dom = addDays(seg, 6);
  const iniPt = seg.toLocaleDateString("pt-PT", { day: "numeric", month: "long" });
  const fimPt = dom.toLocaleDateString("pt-PT", { day: "numeric", month: "long", year: "numeric" });
  return iniPt + " — " + fimPt;
}

/* ---- clique em espaço vazio + drag & drop (reagendar via /reagendar) ---- */
function faixaHora(i) {
  const min = agGrelha.hora_inicio * 60 + i * (agGrelha.intervalo_min || 30);
  return String(Math.floor(min / 60)).padStart(2, "0") + ":" + String(min % 60).padStart(2, "0");
}
let _dragEv = null;
async function reagendarDrag(id, data, hora) {
  try { await jpost(`/api/agendamentos/${id}/reagendar`, { data, hora }); toast("Marcação reagendada."); Router.reload(); }
  catch (e) { toast(e.message, "err"); }
}
// Uma faixa vazia é uma célula de fundo: um clique cria uma marcação pré-
// preenchida; um drop reagenda a marcação arrastada para esta hora/dia.
// Cartões de marcação (.ag-ev) ficam por cima e absorvem o próprio clique,
// por isso um clique numa marcação existente NUNCA chega a esta faixa.
function makeFaixa(i, dia) {
  const min = agGrelha.hora_inicio * 60 + i * (agGrelha.intervalo_min || 30);
  const el = h("div", { class: "ag-faixa" + (min % 60 === 0 ? " hora-cheia" : ""),
    onclick: () => openCreateAppointment({ data: dia, hora: faixaHora(i) }) });
  el.addEventListener("dragover", (e) => { e.preventDefault(); el.classList.add("drop-target"); });
  el.addEventListener("dragleave", () => el.classList.remove("drop-target"));
  el.addEventListener("drop", (e) => {
    e.preventDefault();
    el.classList.remove("drop-target");
    if (!_dragEv) return;
    const novaHora = faixaHora(i);
    if (dia !== _dragEv.dia || novaHora !== _dragEv.hora) reagendarDrag(_dragEv.id, dia, novaHora);
    _dragEv = null;
  });
  return el;
}

/* ---- vista DIA (grelha horária de uma coluna — clique/drag como a semana) ---- */
function renderDiaGrid(evs, anchor) {
  const bandas = agBands();
  const hojeYmd = ymdOf(new Date());
  const eHoje = anchor === hojeYmd;
  const ePassado = anchor < hojeYmd;
  const agoraMin = new Date().getHours() * 60 + new Date().getMinutes();
  const dentro = (min) => min >= agGrelha.hora_inicio * 60 && min <= agGrelha.hora_fim * 60;

  const horas = h("div", { class: "ag-hcol" });
  for (let i = 0; i < bandas; i++) {
    const min = agGrelha.hora_inicio * 60 + i * (agGrelha.intervalo_min || 30);
    horas.append(h("div", { class: "ag-hora" }, (min % 60 === 0 ? String(Math.floor(min / 60)).padStart(2, "0") + ":00" : "")));
  }
  // is-past: cabeçalho/coluna ligeiramente mais apagados — só distingue o
  // DIA, nunca as marcações em si (o histórico continua legível). O futuro
  // fica no estilo base (neutro), já claramente distinto do passado.
  const body = h("div", { class: "ag-col" + (eHoje ? " is-today" : ePassado ? " is-past" : ""), "data-dia": anchor });
  for (let i = 0; i < bandas; i++) body.append(makeFaixa(i, anchor));
  agDispor(evs).forEach((p) => body.append(agEventCard(p, eHoje)));
  if (eHoje && dentro(agoraMin)) {
    const agoraTxt = String(new Date().getHours()).padStart(2, "0") + ":" + String(new Date().getMinutes()).padStart(2, "0");
    body.append(h("div", { class: "ag-now", style: `top:${agTop(agoraMin).toFixed(1)}px`, "aria-hidden": "true" },
      h("span", {}, "Agora · " + agoraTxt)));
  }
  const bodyRow = h("div", { class: "ag-bd ag-bd--dia" }, horas, body);
  const grid = h("div", { class: "ag-grid ag-grid--dia" }, bodyRow);
  if (!evs.length) grid.append(h("div", { class: "empty", style: "padding:14px" }, "Nada agendado neste dia."));
  return h("div", { class: "ag-week-scroll" }, grid);
}

/* ---- vista SEMANA (grelha horária com 7 dias) ---- */
const AG_FAIXA = 34;   // altura (px) de uma banda de intervalo
function agBands() {
  const { hora_inicio, hora_fim, intervalo_min } = agGrelha;
  return Math.max(1, Math.round(((hora_fim - hora_inicio) * 60) / (intervalo_min || 30)));
}
function agStartMin(ev) {
  const [hh, mm] = (ev.hora_hhmm || "00:00").split(":").map(Number);
  return (hh * 60 + mm) - agGrelha.hora_inicio * 60;
}
function agEndMin(ev) {
  if (ev.fim) {
    const [hh, mm] = String(ev.fim).split("T")[1].slice(0, 5).split(":").map(Number);
    return (hh * 60 + mm) - agGrelha.hora_inicio * 60;
  }
  return agStartMin(ev) + (ev.duracao_minutos || 60);
}
const agTop = (min) => Math.max(0, min) / (agGrelha.intervalo_min || 30) * AG_FAIXA;
const agH = (dur) => Math.max((dur / (agGrelha.intervalo_min || 30)) * AG_FAIXA, 44); // altura mínima p/ caber título+serviço+estado sem cortar

// Reparte eventos sobrepostos do mesmo dia por colunas (regra do legado).
function agDispor(evs) {
  const ordenados = evs.slice().sort((a, b) => agStartMin(a) - agStartMin(b));
  const colunas = [], postos = [];
  let grupo = [], grupoFim = -1;
  const fechar = () => {
    const total = Math.max(1, colunas.length);
    grupo.forEach((p) => { p.total = total; });
    grupo = []; colunas.length = 0; grupoFim = -1;
  };
  ordenados.forEach((ev) => {
    const ini = agStartMin(ev);
    const fim = Math.max(ini + 15, agEndMin(ev));
    if (grupo.length && ini >= grupoFim) fechar();
    let col = colunas.findIndex((f) => f <= ini);
    if (col === -1) { colunas.push(fim); col = colunas.length - 1; } else { colunas[col] = fim; }
    const p = { ev, ini, fim, coluna: col, total: 1 };
    postos.push(p); grupo.push(p); grupoFim = Math.max(grupoFim, fim);
  });
  if (grupo.length) fechar();
  return postos;
}

function agEventCard(p, todayCol) {
  const ev = p.ev;
  const op = ev.op_status || "scheduled";
  const estadoKey = (ev.estado_chave || ev.estado || "").toLowerCase();
  const top = agTop(p.ini), altura = agH(p.fim - p.ini);
  const larg = (100 / p.total) - 0.6;
  const left = (100 / p.total) * p.coluna + 3;
  const txt = `${ev.hora_hhmm || "—"} · ${ev.nome || ev.primeiro_nome || "Cliente"}`;
  const sub = [ev.servico, fmtMin(ev.duracao_minutos || 0)].filter(Boolean).join(" · ");
  // Reagendar por drag & drop exige uma marcação ainda ativa e não concluída
  // — a mesma regra da API de /reagendar. A duração nunca é enviada aqui:
  // o drop só muda dia/hora, o backend mantém a duração existente.
  const podeArrastar = !["cancelled", "no_show"].includes(estadoKey) && op !== "done" && estadoKey !== "completed";
  const el = h("button", { class: `ag-ev st-${op} st-${estadoKey}` + (todayCol ? " on-today" : ""),
    style: `top:${top.toFixed(1)}px;height:${altura.toFixed(1)}px;left:${left.toFixed(2)}%;width:${larg.toFixed(2)}%`,
    title: `${txt} · ${sub}`, draggable: podeArrastar || undefined, onclick: () => openAppointment(ev.id),
    "aria-label": `${sub} às ${ev.hora_hhmm || "—"}` },
    h("span", { class: "ag-ev-t" }, txt),
    h("span", { class: "ag-ev-s" }, sub),
    statusBadge(ev.estado, op, ev.bloqueia_horario));
  if (podeArrastar) {
    el.addEventListener("dragstart", (e) => {
      _dragEv = { id: ev.id, dia: ev.dia, hora: ev.hora_hhmm };
      e.dataTransfer.effectAllowed = "move";
      e.dataTransfer.setData("text/plain", String(ev.id));
      el.classList.add("dragging");
    });
    el.addEventListener("dragend", () => { el.classList.remove("dragging"); _dragEv = null; });
  }
  return el;
}

function renderSemana(eventos) {
  const seg = mondayOf(agendaDia);
  const bandas = agBands();
  const hojeYmd = ymdOf(new Date());
  const agoraMin = new Date().getHours() * 60 + new Date().getMinutes();
  const dentro = (min) => min >= agGrelha.hora_inicio * 60 && min <= agGrelha.hora_fim * 60;

  const horas = h("div", { class: "ag-hcol" });
  for (let i = 0; i < bandas; i++) {
    const min = agGrelha.hora_inicio * 60 + i * (agGrelha.intervalo_min || 30);
    horas.append(h("div", { class: "ag-hora" }, (min % 60 === 0 ? String(Math.floor(min / 60)).padStart(2, "0") + ":00" : "")));
  }

  const cols = weekDays(seg).map((dia) => {
    const chave = ymdOf(dia);
    const eHoje = chave === hojeYmd;
    const ePassado = chave < hojeYmd;
    const doDia = eventos.filter((e) => e.dia === chave);
    // is-past: cabeçalho/coluna ligeiramente mais apagados — só distingue o
    // DIA, nunca as marcações em si (o histórico continua legível). O
    // futuro fica no estilo base (neutro), já claramente distinto do passado.
    const body = h("div", { class: "ag-col" + (eHoje ? " is-today" : ePassado ? " is-past" : ""), "data-dia": chave });
    for (let i = 0; i < bandas; i++) body.append(makeFaixa(i, chave));
    agDispor(doDia).forEach((p) => body.append(agEventCard(p, eHoje)));
    // Linha "Agora": só na coluna de hoje, e só quando hoje está na semana e dentro do horário.
    if (eHoje && dentro(agoraMin)) {
      const agoraTxt = String(new Date().getHours()).padStart(2, "0") + ":" + String(new Date().getMinutes()).padStart(2, "0");
      body.append(h("div", { class: "ag-now", style: `top:${agTop(agoraMin).toFixed(1)}px`, "aria-hidden": "true" },
        h("span", {}, "Agora · " + agoraTxt)));
    }
    return { chave, eHoje, ePassado, body };
  });

  const headerRow = h("div", { class: "ag-hd" },
    h("div", { class: "ag-hd-corner" }),
    cols.map(({ chave, eHoje, ePassado }) =>
      h("div", { class: "ag-hd-dia" + (eHoje ? " is-today" : ePassado ? " is-past" : ""), "data-dia": chave },
        h("span", { class: "ag-hd-wd" }, DAY_SHORT[chave.split("-")[0] && (new Date(chave + "T00:00:00").getDay() + 6) % 7]),
        h("span", { class: "ag-hd-dt tnum" }, chave.slice(8, 10) + "/" + chave.slice(5, 7)),
        eHoje ? h("span", { class: "ag-hd-hoje" }, "hoje") : null)));

  const bodyRow = h("div", { class: "ag-bd" }, horas, cols.map((c) => c.body));
  const grid = h("div", { class: "ag-grid" }, headerRow, bodyRow);
  if (!eventos.length) grid.append(h("div", { class: "empty", style: "padding:14px" }, "Sem marcações nesta semana."));

  return h("div", { class: "ag-week-scroll" }, grid);
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

/* ---------- Client Manager: helpers ---------- */
function fmtDataHoraPt(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  if (isNaN(d)) return "—";
  return d.toLocaleDateString("pt-PT", { day: "2-digit", month: "short" }) + " · " +
    d.toLocaleTimeString("pt-PT", { hour: "2-digit", minute: "2-digit" });
}

function clientTagsRow(c) {
  const tags = [];
  if (c.vip) tags.push(h("span", { class: "badge badge--brand" }, "VIP"));
  const visitas = c.visits_count ?? 0;
  if (visitas === 0) tags.push(h("span", { class: "badge" }, "Nova cliente"));
  else if (visitas >= 2) tags.push(h("span", { class: "badge badge--info" }, "Recorrente"));
  (c.tags || []).forEach((t) => tags.push(h("span", { class: "badge" }, t)));
  return tags.length ? h("div", { style: "display:flex;gap:6px;flex-wrap:wrap;margin-top:6px" }, tags) : null;
}

// Marcação ativa mais próxima (hoje ou futuro, não cancelada/faltou) — usada
// para gating das ações rápidas e para pré-preencher o composer.
function proximaMarcacaoDoCliente(historico) {
  const hoje = ymdOf(new Date());
  const ativas = (historico || []).filter((m) =>
    m.data_iso && m.data_iso >= hoje && m.op_status !== "done" &&
    !["cancelled", "no_show"].includes((m.estado || "").toLowerCase()));
  ativas.sort((a, b) => (a.data_iso + (a.hora_hhmm || "")).localeCompare(b.data_iso + (b.hora_hhmm || "")));
  return ativas[0] || null;
}

const EVENT_LABEL = { "customer.created": "Cliente registado", "message.manual_sent": "Mensagem enviada" };

// Timeline compacta: junta o histórico de marcações (já persistido) com os
// eventos da outbox sobre este cliente (registo/mensagens) — nunca inventa
// um transcript de conversa, só o que já está guardado.
function timelineDoCliente(historico, eventos) {
  const itens = [];
  (historico || []).forEach((m) => itens.push({
    chave: (m.data_iso || "0000-00-00") + "T" + (m.hora_hhmm || "00:00"),
    node: h("div", { class: "tl-row", style: "grid-template-columns:70px 1fr auto", onclick: () => openAppointment(m.id) },
      h("span", { class: "tl-time" }, fmtDataPt(m.data_iso)),
      h("div", {}, h("div", { class: "tl-name" }, m.servico), h("div", { class: "tl-svc" }, m.hora || "")),
      statusBadge(m.estado, m.op_status)),
  }));
  (eventos || []).forEach((e) => {
    const label = EVENT_LABEL[e.type];
    if (!label) return;
    const desc = e.type === "message.manual_sent" ? ((e.payload && e.payload.texto) || "") : "";
    itens.push({
      chave: e.created_at || "0000-00-00",
      node: h("div", { class: "tl-row", style: "grid-template-columns:70px 1fr auto" },
        h("span", { class: "tl-time" }, fmtDataHoraPt(e.created_at)),
        h("div", {}, h("div", { class: "tl-name" }, label), desc ? h("div", { class: "tl-svc" }, desc.slice(0, 60)) : null),
        icon(e.type === "message.manual_sent" ? "sparkles" : "users")),
    });
  });
  itens.sort((a, b) => b.chave.localeCompare(a.chave));
  return itens.map((i) => i.node);
}

function clientComposer(c, proxima) {
  const textarea = h("textarea", { class: "inp", rows: 3, style: "resize:vertical",
    placeholder: "Escrever mensagem…" });
  const semProxima = !proxima;
  const preencher = (txt) => { textarea.value = txt; textarea.focus(); };
  const rapidas = h("div", { style: "display:flex;gap:8px;flex-wrap:wrap;margin:10px 0" },
    h("button", { class: "btn btn--sm", disabled: semProxima,
      onclick: () => preencher(`Confirmo a sua marcação: ${proxima.servico}, ${fmtDataPt(proxima.data_iso)} às ${proxima.hora_hhmm || proxima.hora}. ✨`) },
      "Confirmar horário"),
    h("button", { class: "btn btn--sm", disabled: semProxima,
      onclick: () => preencher(`Só a lembrar a sua marcação: ${proxima.servico}, ${fmtDataPt(proxima.data_iso)} às ${proxima.hora_hhmm || proxima.hora}. 📅`) },
      "Relembrar marcação"),
    h("button", { class: "btn btn--sm",
      onclick: () => preencher("Vamos atrasar-nos alguns minutos — obrigada pela paciência!") },
      "Avisar atraso"),
    h("button", { class: "btn btn--sm", onclick: () => preencher("") }, "Escrever outra"));
  const btnEnviar = h("button", { class: "btn btn--primary btn--sm" }, "Enviar mensagem");
  btnEnviar.addEventListener("click", async () => {
    const texto = textarea.value.trim();
    if (!texto) { toast("Escreva uma mensagem antes de enviar.", "err"); return; }
    btnEnviar.disabled = true;
    try {
      const r = await jpost(`/api/clientes/${c.id}/mensagem`, { texto });
      if (r.demo) toast("Cliente demo — mensagem registada, envio real ao WhatsApp desativado.");
      else toast("Mensagem enviada.");
      textarea.value = "";
    } catch (e) { toast(e.message, "err"); }
    finally { btnEnviar.disabled = false; }
  });
  return h("div", { class: "card card--pad", style: "margin-top:8px;display:none" },
    h("div", { class: "eyebrow", style: "margin:0 0 8px" }, "WhatsApp"),
    textarea, rapidas, btnEnviar);
}

function clientNotesSection(c, refresh) {
  const view = h("div", { style: "white-space:pre-wrap;color:var(--text-2);font-size:13px" },
    c.notes_internal || "Sem notas.");
  const btnEditar = h("button", { class: "btn btn--sm" }, "Editar");
  const head = h("div", { style: "display:flex;justify-content:space-between;align-items:center;margin-bottom:8px" },
    h("div", { class: "eyebrow", style: "margin:0" }, "Notas internas"), btnEditar);
  const box = h("div", { class: "card card--pad", style: "margin-top:8px" }, head, view);
  btnEditar.addEventListener("click", () => {
    const textarea = h("textarea", { class: "inp", rows: 4, style: "resize:vertical" }, c.notes_internal || "");
    const btnGuardar = h("button", { class: "btn btn--primary btn--sm" }, "Guardar");
    const btnCancelar = h("button", { class: "btn btn--sm" }, "Cancelar");
    view.replaceWith(h("div", {}, textarea, h("div", { style: "display:flex;gap:8px;margin-top:8px" }, btnGuardar, btnCancelar)));
    head.replaceChildren(h("div", { class: "eyebrow", style: "margin:0" }, "Notas internas"));
    btnGuardar.addEventListener("click", async () => {
      btnGuardar.disabled = true;
      try {
        await jpatch(`/api/clientes/${c.id}`, { notes_internal: textarea.value.trim() });
        toast("Notas guardadas.");
        refresh();
      } catch (e) { toast(e.message, "err"); btnGuardar.disabled = false; }
    });
    btnCancelar.addEventListener("click", () => refresh());
  });
  return box;
}

async function reagendarClientePrompt(ag, id) {
  const novaData = prompt(`Nova data para ${ag.servico} (AAAA-MM-DD):`, ag.data_iso || "");
  if (!novaData) return;
  const novaHora = prompt("Nova hora (HH:MM):", ag.hora_hhmm || "");
  if (!novaHora) return;
  try {
    await jpost(`/api/agendamentos/${ag.id}/reagendar`, { data: novaData, hora: novaHora });
    toast("Marcação reagendada.");
    invalidateClientesCache();
    Drawer.close();
    Router.reload();
  } catch (e) { toast(e.message, "err"); }
}

function clientQuickActions(c, proxima, composerNode) {
  const foot = h("div", { class: "drawer-foot" });
  const act = async (fn) => { try { await fn(); invalidateClientesCache(); Drawer.close(); Router.reload(); } catch (e) { handleActionError(e); } };

  foot.append(h("button", { class: "btn", onclick: () => {
    const visivel = composerNode.style.display !== "none";
    composerNode.style.display = visivel ? "none" : "";
    if (!visivel) { composerNode.scrollIntoView({ behavior: "smooth", block: "nearest" }); $("textarea.inp", composerNode).focus(); }
  } }, icon("sparkles"), "WhatsApp"));
  foot.append(h("button", { class: "btn", onclick: () => openCreateAppointment({ nome: c.name, telefone: c.phone }) }, icon("calendar"), "Nova marcação"));

  if (proxima) {
    const op = proxima.op_status || "scheduled";
    foot.append(h("button", { class: "btn", onclick: () => reagendarClientePrompt(proxima, c.id) }, icon("clock"), "Reagendar"));
    foot.append(h("button", { class: "btn btn--danger",
      onclick: () => act(() => jpost(`/api/agendamentos/${proxima.id}/cancelar`, {})) }, icon("x"), "Cancelar"));
    if (op === "scheduled")
      foot.append(h("button", { class: "btn btn--primary", onclick: () => act(() => opTransition(proxima.id, "arrived")) }, icon("user-check"), "Chegou"));
    if (op === "arrived")
      foot.append(h("button", { class: "btn btn--primary", onclick: () => act(() => opTransition(proxima.id, "in_progress")) }, icon("play"), "Iniciar"));
    if (op === "in_progress" || op === "arrived")
      foot.append(h("button", { class: "btn btn--primary", onclick: () => act(() => jpost(`/api/agendamentos/${proxima.id}/estado`, { estado: "completed", confirmar: true })) }, icon("check"), "Concluir"));
  }
  return foot;
}

async function openCliente(id, foco) {
  Drawer.open(h("div", { class: "drawer-body" }, h("div", { class: "skel", style: "height:140px" })));
  let d;
  try { d = await api(`/api/clientes/${id}`); }
  catch (e) { toast(e.message, "err"); Drawer.close(); return; }
  const c = d.cliente;
  const historico = d.historico || [];
  const proxima = proximaMarcacaoDoCliente(historico);
  const refresh = () => openCliente(id);
  const composerNode = clientComposer(c, proxima);
  if (foco === "whatsapp") composerNode.style.display = "";

  const head = h("div", { class: "drawer-head" },
    h("span", { class: "avatar", style: "width:38px;height:38px;font-size:14px" }, initials(c.name)),
    h("div", {},
      h("h3", { style: "margin:0" }, c.name || "Cliente"),
      h("div", { style: "color:var(--text-3);font-size:12.5px" }, c.phone || "")),
    h("span", { style: "flex:1" }),
    h("button", { class: "icon-btn", onclick: () => Drawer.close(), "aria-label": "Fechar" }, icon("x")));

  const body = h("div", { class: "drawer-body" },
    clientTagsRow(c),
    h("div", { class: "metrics", style: "margin-top:14px" },
      metric(c.visits_count ?? 0, "Visitas"),
      metric(chf(c.spend_cents), "Gasto total"),
      metric(c.last_visit ? fmtDataPt(c.last_visit) : "—", "Última visita"),
      metric(c.next_visit ? fmtDataPt(c.next_visit) : "—", "Próxima")),

    proxima ? h("div", { class: "att sev-info", style: "margin-top:14px" },
      icon("calendar"),
      h("div", { class: "a-body" },
        h("div", { class: "a-title" }, proxima.servico),
        h("div", { class: "a-desc" }, `${fmtDataPt(proxima.data_iso)} · ${proxima.hora_hhmm || proxima.hora} · ${OP_LABEL[proxima.op_status || "scheduled"]}`)),
      h("button", { class: "btn btn--sm", onclick: () => openAppointment(proxima.id) }, "Abrir")) : null,

    historico[0] && historico[0].servico_id ? h("button", {
      class: "btn btn--sm", style: "margin-top:12px",
      onclick: () => openCreateAppointment({ nome: c.name, telefone: c.phone, servico_id: historico[0].servico_id }),
    }, "Repetir último serviço") : null,

    composerNode,
    clientNotesSection(c, refresh),

    h("div", { class: "eyebrow" }, "Faturas"),
    (d.faturas && d.faturas.length)
      ? h("div", {}, d.faturas.map((f) => h("div", { class: "att sev-info" },
        icon("receipt"),
        h("div", { class: "a-body" },
          h("div", { class: "a-title" }, f.invoice_number || "Rascunho"),
          h("div", { class: "a-desc" }, `${fmtDataPt(f.issue_date || (f.created_at || "").slice(0, 10))} · ${chf(f.total_cents)} · ${EST_LABEL[f.status] || f.status}`)))))
      : h("div", { class: "empty", style: "padding:16px" }, "Sem faturas."),

    h("div", { class: "eyebrow" }, "Histórico"));

  const timelineTodas = timelineDoCliente(historico, d.eventos);
  const timelineEl = h("div", { class: "timeline" }, timelineTodas.slice(0, 5));
  body.append(timelineEl);
  if (timelineTodas.length > 5) {
    const btnVerTudo = h("button", { class: "btn btn--sm", style: "margin-top:10px" }, "Ver histórico completo");
    btnVerTudo.addEventListener("click", () => {
      timelineEl.innerHTML = "";
      timelineEl.append(...timelineTodas);
      btnVerTudo.remove();
    });
    body.append(btnVerTudo);
  }
  if (!timelineTodas.length) body.append(h("div", { class: "empty", style: "padding:16px" }, "Sem atividade registada."));

  Drawer.open(h("div", { style: "display:flex;flex-direction:column;height:100%" },
    head, body, clientQuickActions(c, proxima, composerNode)));
  if (foco === "whatsapp") {
    composerNode.scrollIntoView({ behavior: "smooth", block: "nearest" });
    $("textarea.inp", composerNode).focus();
  }
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
  if (status === "issued" && due && due < ymdOf(new Date()))
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
function initNovoMenu() {
  const wrap = $("#novo-wrap"), btn = $("#novo-btn"), menu = $("#novo-menu");
  const closeMenu = () => { menu.hidden = true; btn.setAttribute("aria-expanded", "false"); };
  const items = [
    { label: "Nova marcação", icon: "calendar", run: () => openCreateAppointment() },
    { label: "Novo cliente", icon: "users", soon: "Funcionalidade pendente de endpoint backend" },
    { label: "Bloquear horário", icon: "clock", soon: "Backend só suporta bloqueio do dia inteiro — em breve" },
    { label: "Criar fatura", icon: "receipt", soon: "Fatura tem de estar ligada a uma marcação concluída — em breve" },
  ];
  items.forEach((it) => {
    const el = h("button", { class: "novo-item", role: "menuitem", disabled: !!it.soon, title: it.soon || "",
      onclick: it.run ? () => { closeMenu(); it.run(); } : null },
      icon(it.icon), h("span", {}, it.label), it.soon ? h("span", { class: "novo-soon" }, "Em breve") : null);
    menu.append(el);
  });
  btn.addEventListener("click", (e) => {
    e.stopPropagation();
    const open = menu.hidden;
    menu.hidden = !open;
    btn.setAttribute("aria-expanded", String(open));
  });
  document.addEventListener("click", (e) => { if (!wrap.contains(e.target)) closeMenu(); });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeMenu(); });
}

document.addEventListener("DOMContentLoaded", () => {
  Drawer.init();
  initNovoMenu();
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
  requestAnimationFrame(() => document.body.classList.add("theme-ready"));
  themeBtn.addEventListener("click", () => {
    const cur = document.documentElement.dataset.theme || (prefersDark() ? "dark" : "light");
    const next = cur === "light" ? "dark" : "light";
    applyTheme(next);
    try { localStorage.setItem(THEME_KEY, next); } catch (e) {}
  });
  if (!location.hash) location.hash = "#/hoje";
  Router.render(parseHash());
});
