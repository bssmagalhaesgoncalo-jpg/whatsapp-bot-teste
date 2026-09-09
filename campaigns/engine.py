"""
campaigns/engine.py — segmentação, ciclo de vida e envio de campanhas WhatsApp.

Fluxo:

    filtros (painel)
        -> elegiveis_e_excluidos()      leitura pura, nunca persiste nada
    "Guardar rascunho"
        -> criar_rascunho()             grava name/template/filtros, status=draft
    "Agendar" / "Enviar agora"
        -> agendar() / enviar_agora()   snapshot de campaign_recipients (uma
                                         vez só — nunca recalculado a meio de
                                         um envio) + UM automation_job
                                         "campaign_send" (reaproveita
                                         notifications/jobs.py, o MESMO
                                         executor do P0/P1/P2/P4.1)
    run_at vencido, /api/automacoes/correr
        -> executar_envio_campanha()    processa um LOTE de destinatários
                                         pendentes (nunca a campanha inteira
                                         de um só vez — throttling, §13 do
                                         patch); se sobrar trabalho, cria um
                                         NOVO job (chave de idempotência por
                                         offset) para o resto
    cliente toca "Marcar agora"
        -> registar_clique()            bot.py entra no fluxo de marcação
                                         NORMAL (nunca cria uma marcação
                                         diretamente)
    booking.created / booking.pending
        -> handler_evento_conversao()   fecha o funil: recipient "converted"

Idempotência (§14 do patch):
  • campaign_recipients tem um índice único (campaign_id, customer_id) — a
    MESMA campanha nunca tem duas linhas para o mesmo cliente, mesmo que
    _preparar_envio corra duas vezes (INTEGRITY ERROR ignorado de propósito).
  • agendar()/enviar_agora() só criam o snapshot se a campanha AINDA não
    tiver nenhum recipient — um duplo clique em "Enviar agora" nunca
    duplica o disparo (devolve o estado atual).
  • cada lote de envio usa a MESMA notificações.jobs (automation_jobs);
    "campaign_send:<id>:<offset>" garante que o mesmo lote nunca é
    reprocessado por um restart do worker.
  • um destinatário nunca é reenviado: só é elegível para
    executar_envio_campanha() enquanto o seu status for "pending".
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import date, timedelta

import config
import db
import tempo
from messaging import whatsapp as wa
from notifications import jobs as notif_jobs

log = logging.getLogger("campaigns")

# ---------------------------------------------------------------------------
# Vocabulário
# ---------------------------------------------------------------------------
TEMPLATE_CLIENT_REACTIVATION = "client_reactivation"
TEMPLATES_SUPORTADOS = (TEMPLATE_CLIENT_REACTIVATION,)

STATUS_DRAFT = "draft"
STATUS_SCHEDULED = "scheduled"
STATUS_RUNNING = "running"
STATUS_COMPLETED = "completed"
STATUS_CANCELLED = "cancelled"

REC_PENDING = "pending"
REC_SENT = "sent"
REC_FAILED = "failed"
REC_CLICKED = "clicked"
REC_CONVERTED = "converted"
REC_SKIPPED = "skipped"

TYPE_CAMPAIGN_SEND = "campaign_send"

_CODIGO_META_POR_IDIOMA = {"pt": "pt_PT", "de": "de", "en": "en_US"}


class CampanhaErro(Exception):
    pass


class CampanhaNaoEncontrada(CampanhaErro):
    pass


class EstadoInvalido(CampanhaErro):
    pass


# ---------------------------------------------------------------------------
# Segmentação — filtros combináveis com AND (§6 do patch, "MVP controlado",
# nunca um query builder genérico)
# ---------------------------------------------------------------------------
_VALORES_NO_VISIT_DIAS = (30, 60, 90, 180)

_CAMPOS_SEGMENTO = ("id", "name", "phone", "locale", "last_visit", "visits_count",
                    "blocked", "marketing_opt_in")


def _data_valida(texto) -> bool:
    try:
        date.fromisoformat(str(texto))
        return True
    except (TypeError, ValueError):
        return False


def _normalizar_filtros(filtros: dict | None) -> dict:
    """Nunca confia no que chega do painel — só os filtros/valores
    explicitamente suportados sobrevivem; tudo o resto é ignorado em
    silêncio (nunca um erro 500 por causa de um filtro desconhecido)."""
    filtros = filtros or {}
    out: dict = {}
    nvd = filtros.get("no_visit_days")
    try:
        nvd = int(nvd) if nvd is not None else None
    except (TypeError, ValueError):
        nvd = None
    if nvd in _VALORES_NO_VISIT_DIAS:
        out["no_visit_days"] = nvd

    sid = filtros.get("service_id")
    if sid and isinstance(sid, str) and db.obter_servico(sid):
        out["service_id"] = sid

    if filtros.get("no_future_booking"):
        out["no_future_booking"] = True
    if filtros.get("recurring"):
        out["recurring"] = True

    lv_from, lv_to = filtros.get("last_visit_from"), filtros.get("last_visit_to")
    if _data_valida(lv_from):
        out["last_visit_from"] = lv_from
    if _data_valida(lv_to):
        out["last_visit_to"] = lv_to
    return out


def _customers_do_segmento(conn, tenant_id: int, filtros: dict) -> list[dict]:
    """Só os filtros de SEGMENTO (B-F do patch) — as exclusões de segurança
    (bloqueado/sem consentimento/telefone inválido/demo) são aplicadas à
    parte, em `_motivo_exclusao`, para se poder reportar CADA motivo
    separadamente (§7/§10 do patch: "Excluídos: 5", nunca só um número
    cego)."""
    condicoes = ["c.tenant_id = ?"]
    params: list = [tenant_id]
    hoje = tempo.hoje_zurique()

    nvd = filtros.get("no_visit_days")
    if nvd:
        cutoff = (hoje - timedelta(days=int(nvd))).isoformat()
        condicoes.append("c.last_visit IS NOT NULL AND c.last_visit <= ?")
        params.append(cutoff)

    sid = filtros.get("service_id")
    if sid:
        condicoes.append(
            "EXISTS (SELECT 1 FROM agendamentos a WHERE a.customer_id = c.id "
            "AND a.servico_id = ? AND a.estado = 'completed')")
        params.append(sid)

    if filtros.get("no_future_booking"):
        condicoes.append(
            "NOT EXISTS (SELECT 1 FROM agendamentos a WHERE a.customer_id = c.id "
            "AND a.estado IN ('confirmed', 'pending') AND a.data_iso >= ?)")
        params.append(hoje.isoformat())

    if filtros.get("recurring"):
        condicoes.append("c.visits_count >= 2")

    if filtros.get("last_visit_from"):
        condicoes.append("c.last_visit >= ?")
        params.append(filtros["last_visit_from"])
    if filtros.get("last_visit_to"):
        condicoes.append("c.last_visit <= ?")
        params.append(filtros["last_visit_to"])

    sql = (f"SELECT {', '.join('c.' + f for f in _CAMPOS_SEGMENTO)} FROM customers c "
           f"WHERE {' AND '.join(condicoes)} ORDER BY c.id")
    rows = conn.execute(sql, params).fetchall()
    return [dict(zip(_CAMPOS_SEGMENTO, r)) for r in rows]


_TELEFONE_VALIDO_MIN, _TELEFONE_VALIDO_MAX = 8, 15


def _telefone_valido(tel: str | None) -> bool:
    tel = (tel or "").strip()
    return tel.isdigit() and _TELEFONE_VALIDO_MIN <= len(tel) <= _TELEFONE_VALIDO_MAX


def _e_telefone_demo(tel: str | None) -> bool:
    return bool(tel) and str(tel).startswith(config.DEMO_PHONE_PREFIX)


def _motivo_exclusao(cliente: dict) -> str | None:
    """§7 do patch — obrigatório excluir: bloqueado, sem opt-in de
    marketing, telefone inválido, cliente DEMO, fora do tenant (já garantido
    por `_customers_do_segmento`, que filtra por tenant_id). Duplicados:
    estruturalmente impossível — `customers` já é UNIQUE(tenant_id, phone) e
    esta consulta devolve UMA linha por customer.id."""
    if cliente["blocked"]:
        return "bloqueado"
    if not cliente["marketing_opt_in"]:
        return "sem_consentimento_marketing"
    if not _telefone_valido(cliente["phone"]):
        return "telefone_invalido"
    if _e_telefone_demo(cliente["phone"]):
        return "cliente_demo"
    return None


def _computar_segmento(tenant_id: int, filtros: dict) -> tuple[list[dict], dict]:
    """Lista COMPLETA de elegíveis (nunca só uma amostra) + contagem de
    excluídos por motivo. Usada tanto pelo preview em tempo real
    (`elegiveis_e_excluidos`) como pelo snapshot real (`_preparar_envio`) —
    a MESMA função, para o número mostrado no preview ser sempre o mesmo
    critério usado no envio."""
    filtros = _normalizar_filtros(filtros)
    with db.ligacao() as c:
        candidatos = _customers_do_segmento(c, tenant_id, filtros)
    elegiveis, motivos = [], {}
    for cli in candidatos:
        motivo = _motivo_exclusao(cli)
        if motivo:
            motivos[motivo] = motivos.get(motivo, 0) + 1
        else:
            elegiveis.append(cli)
    return elegiveis, motivos


def elegiveis_e_excluidos(tenant_id: int, filtros: dict | None) -> dict:
    """Preview em tempo real (POST /api/campanhas/segmentar) — leitura pura,
    nunca persiste nada. "37 clientes elegíveis" (§6 do patch)."""
    elegiveis, motivos = _computar_segmento(tenant_id, filtros)
    amostra = [{"id": e["id"], "name": e["name"] or "Cliente", "locale": e["locale"] or "pt"}
               for e in elegiveis[:5]]
    return {
        "elegiveis": len(elegiveis),
        "excluidos": sum(motivos.values()),
        "motivos_exclusao": motivos,
        "amostra": amostra,
        "filtros": _normalizar_filtros(filtros),
    }


# ---------------------------------------------------------------------------
# Preview da mensagem (§9/§10 do patch) — o TEXTO sugerido para o template
# aprovado na Meta. O envio real usa sempre messaging.whatsapp.enviar_template
# com o nome do template já aprovado (config.WHATSAPP_CAMPAIGN_TEMPLATE_*) —
# isto é só para a Daniela VER como fica antes de aprovar o template.
# ---------------------------------------------------------------------------
_TEXTO_PREVIEW = {
    "pt": "Olá, {nome} 🤍\n\nJá passou algum tempo desde a sua última visita à {negocio}.\n\n"
          "Se quiser, pode escolher já o seu próximo horário.",
    "de": "Hallo {nome} 🤍\n\nEs ist eine Weile her seit Ihrem letzten Besuch bei {negocio}.\n\n"
          "Wenn Sie möchten, können Sie jetzt gleich Ihren nächsten Termin wählen.",
    "en": "Hi {nome} 🤍\n\nIt's been a while since your last visit to {negocio}.\n\n"
          "If you'd like, you can already choose your next appointment.",
}
_BOTAO_PREVIEW = {"pt": "Marcar agora", "de": "Jetzt buchen", "en": "Book now"}


def render_preview(nome: str | None, idioma: str = "pt") -> dict:
    idioma = idioma if idioma in ("pt", "de", "en") else "pt"
    marcador_nome = {"pt": "Cliente", "de": "Kundin", "en": "Customer"}[idioma]
    primeiro_nome = (nome or "").strip().split(" ")[0] or marcador_nome
    texto = _TEXTO_PREVIEW[idioma].format(nome=primeiro_nome, negocio=config.BUSINESS_NAME)
    return {"texto": texto, "botao": _BOTAO_PREVIEW[idioma]}


def _template_do_idioma(idioma: str) -> str | None:
    return {
        "pt": config.WHATSAPP_CAMPAIGN_TEMPLATE_PT,
        "de": config.WHATSAPP_CAMPAIGN_TEMPLATE_DE,
        "en": config.WHATSAPP_CAMPAIGN_TEMPLATE_EN,
    }.get(idioma) or config.WHATSAPP_CAMPAIGN_TEMPLATE_PT


# ---------------------------------------------------------------------------
# CRUD — rascunho / listagem / detalhe
# ---------------------------------------------------------------------------
_CAMPOS_CAMPANHA = ("id", "tenant_id", "name", "status", "template_key", "segment_json",
                    "eligible_count", "excluded_count", "scheduled_at", "created_at",
                    "started_at", "completed_at", "cancelled_at")
_SQL_CAMPANHA = ", ".join(_CAMPOS_CAMPANHA)


def _linha_campanha(row) -> dict:
    d = dict(zip(_CAMPOS_CAMPANHA, row))
    try:
        d["segment_json"] = json.loads(d["segment_json"] or "{}")
    except (ValueError, TypeError):
        d["segment_json"] = {}
    return d


def _obter_linha(conn, campaign_id: int, tenant_id: int) -> dict | None:
    row = conn.execute(f"SELECT {_SQL_CAMPANHA} FROM campaigns WHERE id = ? AND tenant_id = ?",
                       (campaign_id, tenant_id)).fetchone()
    return _linha_campanha(row) if row else None


def _contagens(conn, campaign_id: int) -> dict:
    row = conn.execute(
        "SELECT COUNT(*), "
        "SUM(CASE WHEN status IN ('sent','clicked','converted') THEN 1 ELSE 0 END), "
        "SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END), "
        "SUM(CASE WHEN clicked_at IS NOT NULL THEN 1 ELSE 0 END), "
        "SUM(CASE WHEN status = 'converted' THEN 1 ELSE 0 END), "
        "SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END), "
        "SUM(CASE WHEN status = 'skipped' THEN 1 ELSE 0 END) "
        "FROM campaign_recipients WHERE campaign_id = ?", (campaign_id,)).fetchone()
    total, enviados, falhados, clicados, convertidos, pendentes, ignorados = row
    return {
        "recipients_total": total or 0,
        "recipients_sent": enviados or 0,
        "recipients_failed": falhados or 0,
        "recipients_clicked": clicados or 0,
        "recipients_converted": convertidos or 0,
        "recipients_pending": pendentes or 0,
        "recipients_skipped": ignorados or 0,
    }


def criar_rascunho(tenant_id: int, nome: str, filtros: dict | None = None,
                   template_key: str = TEMPLATE_CLIENT_REACTIVATION) -> dict:
    nome = (nome or "").strip()
    if not nome:
        raise ValueError("Escreva um nome para a campanha.")
    if template_key not in TEMPLATES_SUPORTADOS:
        raise ValueError("Template de campanha desconhecido.")
    filtros = _normalizar_filtros(filtros)
    agora = tempo.iso_utc()
    with db.ligacao() as c:
        cur = c.execute(
            "INSERT INTO campaigns (tenant_id, name, status, template_key, segment_json, created_at) "
            "VALUES (?, ?, 'draft', ?, ?, ?)",
            (tenant_id, nome, template_key, json.dumps(filtros, ensure_ascii=False), agora))
        cid = cur.lastrowid
        db.registar_evento(c, "campaign.created", "campaign", cid, {"name": nome},
                           dedupe_key=f"campaign.created:{cid}", tenant_id=tenant_id)
        camp = _obter_linha(c, cid, tenant_id)
    camp.update(_contagens_vazias())
    return camp


def _contagens_vazias() -> dict:
    return {"recipients_total": 0, "recipients_sent": 0, "recipients_failed": 0,
            "recipients_clicked": 0, "recipients_converted": 0, "recipients_pending": 0,
            "recipients_skipped": 0}


def atualizar_rascunho(campaign_id: int, tenant_id: int, patch: dict) -> dict:
    with db.ligacao() as c:
        camp = _obter_linha(c, campaign_id, tenant_id)
        if not camp:
            raise CampanhaNaoEncontrada()
        if camp["status"] != STATUS_DRAFT:
            raise EstadoInvalido("Só é possível editar uma campanha em rascunho.")
        sets, vals = [], []
        if "name" in patch:
            nome = (patch["name"] or "").strip()
            if not nome:
                raise ValueError("Escreva um nome para a campanha.")
            sets.append("name = ?"); vals.append(nome)
        if "filtros" in patch:
            filtros = _normalizar_filtros(patch["filtros"])
            sets.append("segment_json = ?"); vals.append(json.dumps(filtros, ensure_ascii=False))
        if sets:
            vals.append(campaign_id)
            c.execute(f"UPDATE campaigns SET {', '.join(sets)} WHERE id = ?", vals)
        camp = _obter_linha(c, campaign_id, tenant_id)
        camp.update(_contagens(c, campaign_id))
    return camp


def apagar_rascunho(campaign_id: int, tenant_id: int) -> None:
    with db.ligacao() as c:
        camp = _obter_linha(c, campaign_id, tenant_id)
        if not camp:
            raise CampanhaNaoEncontrada()
        if camp["status"] != STATUS_DRAFT:
            raise EstadoInvalido(
                "Só um rascunho pode ser apagado — cancele campanhas já agendadas/a decorrer.")
        c.execute("DELETE FROM campaigns WHERE id = ?", (campaign_id,))


def listar_campanhas(tenant_id: int = 1) -> list[dict]:
    with db.ligacao() as c:
        rows = c.execute(f"SELECT {_SQL_CAMPANHA} FROM campaigns WHERE tenant_id = ? ORDER BY id DESC",
                         (tenant_id,)).fetchall()
        campanhas = [_linha_campanha(r) for r in rows]
        for camp in campanhas:
            camp.update(_contagens(c, camp["id"]))
    return campanhas


def obter_campanha(campaign_id: int, tenant_id: int = 1) -> dict | None:
    with db.ligacao() as c:
        camp = _obter_linha(c, campaign_id, tenant_id)
        if not camp:
            return None
        camp.update(_contagens(c, campaign_id))
    return camp


_CAMPOS_RECIPIENTE = ("id", "customer_id", "name", "phone", "locale", "status", "sent_at",
                      "failed_at", "clicked_at", "converted_at", "booking_id", "last_error")


def _mascarar_telefone(tel: str | None) -> str:
    """§20 do patch — nunca mostrar o telefone completo desnecessariamente."""
    tel = tel or ""
    return ("•" * max(len(tel) - 4, 0)) + tel[-4:] if len(tel) > 4 else tel


def detalhe_recipientes(campaign_id: int, tenant_id: int = 1, limite: int = 1000) -> list[dict] | None:
    with db.ligacao() as c:
        camp = _obter_linha(c, campaign_id, tenant_id)
        if not camp:
            return None
        rows = c.execute(
            "SELECT r.id, r.customer_id, c.name, r.phone, r.locale, r.status, r.sent_at, "
            "r.failed_at, r.clicked_at, r.converted_at, r.booking_id, r.last_error "
            "FROM campaign_recipients r JOIN customers c ON c.id = r.customer_id "
            "WHERE r.campaign_id = ? ORDER BY r.id LIMIT ?", (campaign_id, limite)).fetchall()
    out = []
    for r in rows:
        d = dict(zip(_CAMPOS_RECIPIENTE, r))
        d["phone"] = _mascarar_telefone(d["phone"])
        out.append(d)
    return out


# ---------------------------------------------------------------------------
# Snapshot — §5/§14 do patch: uma vez só, nunca recalculado a meio de um envio
# ---------------------------------------------------------------------------
def _preparar_envio(conn, campaign_id: int, tenant_id: int, filtros: dict) -> None:
    ja_tem = conn.execute("SELECT 1 FROM campaign_recipients WHERE campaign_id = ? LIMIT 1",
                          (campaign_id,)).fetchone()
    if ja_tem:
        return
    elegiveis, motivos = _computar_segmento(tenant_id, filtros)
    agora = tempo.iso_utc()
    for cli in elegiveis:
        try:
            conn.execute(
                "INSERT INTO campaign_recipients (campaign_id, tenant_id, customer_id, phone, locale, "
                "status, created_at) VALUES (?, ?, ?, ?, ?, 'pending', ?)",
                (campaign_id, tenant_id, cli["id"], cli["phone"], cli["locale"] or "pt", agora))
        except sqlite3.IntegrityError:
            pass  # (campaign_id, customer_id) já existia — nunca duplica
    conn.execute("UPDATE campaigns SET eligible_count = ?, excluded_count = ? WHERE id = ?",
                (len(elegiveis), sum(motivos.values()), campaign_id))


def _chave_lote(campaign_id: int, offset: int) -> str:
    return f"campaign_send:{campaign_id}:{offset}"


def _garantir_template_configurado(filtros: dict) -> None:
    """Falha CEDO (na criação/agendamento), não só no envio, quando NENHUM
    dos 3 templates está configurado — evita agendar uma campanha para
    depois descobrir, no executor, que nunca poderia ter sido enviada."""
    if not any((config.WHATSAPP_CAMPAIGN_TEMPLATE_PT, config.WHATSAPP_CAMPAIGN_TEMPLATE_DE,
               config.WHATSAPP_CAMPAIGN_TEMPLATE_EN)):
        raise EstadoInvalido(
            "Nenhum template de campanha está configurado (WHATSAPP_CAMPAIGN_TEMPLATE_* em falta) "
            "— configure pelo menos um idioma antes de agendar ou enviar.")


def agendar(campaign_id: int, tenant_id: int, data_iso: str, hora_hhmm: str) -> dict:
    inicio = tempo.combinar_local(data_iso, hora_hhmm) if data_iso and hora_hhmm else None
    if not inicio:
        raise ValueError("Indique uma data e hora válidas para o agendamento.")
    run_at = tempo.iso_utc(inicio)
    with db.ligacao() as c:
        camp = _obter_linha(c, campaign_id, tenant_id)
        if not camp:
            raise CampanhaNaoEncontrada()
        if camp["status"] != STATUS_DRAFT:
            raise EstadoInvalido("Só um rascunho pode ser agendado.")
        _garantir_template_configurado(camp["segment_json"])
        _preparar_envio(c, campaign_id, tenant_id, camp["segment_json"])
        c.execute("UPDATE campaigns SET status = 'scheduled', scheduled_at = ? WHERE id = ?",
                  (run_at, campaign_id))
        db.registar_evento(c, "campaign.scheduled", "campaign", campaign_id, {"scheduled_at": run_at},
                           dedupe_key=f"campaign.scheduled:{campaign_id}:{run_at}", tenant_id=tenant_id)
    notif_jobs.reprogramar_ou_criar(
        TYPE_CAMPAIGN_SEND, run_at, tenant_id=tenant_id,
        payload={"campaign_id": campaign_id, "offset": 0},
        idempotency_key=_chave_lote(campaign_id, 0))
    return obter_campanha(campaign_id, tenant_id)


def enviar_agora(campaign_id: int, tenant_id: int) -> dict:
    with db.ligacao() as c:
        camp = _obter_linha(c, campaign_id, tenant_id)
        if not camp:
            raise CampanhaNaoEncontrada()
        if camp["status"] not in (STATUS_DRAFT, STATUS_SCHEDULED):
            # §14 — botão "Enviar agora" clicado 2x, ou campanha já a
            # decorrer/concluída: devolve o estado ATUAL, nunca duplica.
            camp.update(_contagens(c, campaign_id))
            return camp
        _garantir_template_configurado(camp["segment_json"])
        agora = tempo.iso_utc()
        _preparar_envio(c, campaign_id, tenant_id, camp["segment_json"])
        c.execute(
            "UPDATE campaigns SET status = 'running', started_at = COALESCE(started_at, ?), "
            "scheduled_at = COALESCE(scheduled_at, ?) WHERE id = ?", (agora, agora, campaign_id))
        db.registar_evento(c, "campaign.started", "campaign", campaign_id, {},
                           dedupe_key=f"campaign.started:{campaign_id}", tenant_id=tenant_id)
    notif_jobs.reprogramar_ou_criar(
        TYPE_CAMPAIGN_SEND, agora, tenant_id=tenant_id,
        payload={"campaign_id": campaign_id, "offset": 0},
        idempotency_key=_chave_lote(campaign_id, 0))
    return obter_campanha(campaign_id, tenant_id)


def cancelar(campaign_id: int, tenant_id: int) -> dict:
    with db.ligacao() as c:
        camp = _obter_linha(c, campaign_id, tenant_id)
        if not camp:
            raise CampanhaNaoEncontrada()
        if camp["status"] == STATUS_DRAFT:
            c.execute("DELETE FROM campaigns WHERE id = ?", (campaign_id,))
            return {"deleted": True}
        if camp["status"] not in (STATUS_SCHEDULED, STATUS_RUNNING):
            raise EstadoInvalido("Esta campanha já não pode ser cancelada.")
        agora = tempo.iso_utc()
        c.execute("UPDATE campaigns SET status = 'cancelled', cancelled_at = ? WHERE id = ?",
                  (agora, campaign_id))
        # §21 do patch — pendentes nunca chegam a ser processados; mensagens
        # já enviadas continuam "sent" (nunca se "desenvia" nada).
        c.execute("UPDATE campaign_recipients SET status = 'skipped' WHERE campaign_id = ? "
                  "AND status = 'pending'", (campaign_id,))
        c.execute(
            "UPDATE automation_jobs SET status = 'cancelled', last_error = ?, processed_at = ? "
            "WHERE tenant_id = ? AND type = ? AND idempotency_key LIKE ? AND status IN ('pending','processing')",
            ("campanha cancelada", agora, tenant_id, TYPE_CAMPAIGN_SEND, f"campaign_send:{campaign_id}:%"))
        db.registar_evento(c, "campaign.cancelled", "campaign", campaign_id, {},
                           dedupe_key=f"campaign.cancelled:{campaign_id}", tenant_id=tenant_id)
    return obter_campanha(campaign_id, tenant_id)


# ---------------------------------------------------------------------------
# Execução — handler do job "campaign_send" (jobs.process_due_jobs)
# ---------------------------------------------------------------------------
def _enviar_para_recipiente(campaign_id: int, recipient_id: int, customer_id: int,
                            phone_snapshot: str, locale_snapshot: str, tenant_id: int) -> None:
    """Revalida tudo em cima da hora (o snapshot pode ter dias) antes de
    enviar — mesmo padrão de notifications/reminders.py e
    notifications/followup.py. Uma exceção AQUI nunca aborta o lote: fica
    isolada neste destinatário (try/except por linha, §13 do patch)."""
    agora = tempo.iso_utc()
    cust = db.obter_customer(customer_id)
    motivo_invalido = None
    if not cust or cust.get("tenant_id") != tenant_id:
        motivo_invalido = "cliente já não existe"
    elif cust.get("blocked"):
        motivo_invalido = "cliente bloqueado entretanto"
    elif not cust.get("marketing_opt_in"):
        motivo_invalido = "consentimento de marketing revogado entretanto"
    elif not _telefone_valido(cust.get("phone")):
        motivo_invalido = "telefone inválido"

    if motivo_invalido:
        with db.ligacao() as c:
            c.execute("UPDATE campaign_recipients SET status = 'skipped', failed_at = ?, "
                      "last_error = ? WHERE id = ?", (agora, motivo_invalido, recipient_id))
        return

    idioma = cust.get("locale") or locale_snapshot or "pt"
    template_name = _template_do_idioma(idioma)
    if not template_name:
        # Bloqueio EXTERNO, não de código — igual ao reminder 24h/rebooking:
        # nunca finge um envio; fica "failed" até o template ser configurado.
        with db.ligacao() as c:
            c.execute("UPDATE campaign_recipients SET status = 'failed', failed_at = ?, "
                      "last_error = ? WHERE id = ?",
                      (agora, f"template não configurado para '{idioma}'", recipient_id))
        return

    primeiro_nome = (cust.get("name") or "").strip().split(" ")[0] or ""
    try:
        wa.enviar_template(
            cust["phone"], template_name, _CODIGO_META_POR_IDIOMA.get(idioma, idioma),
            parametros_corpo=[primeiro_nome],
            botoes_payload=[f"campanha_marcar_{campaign_id}_{recipient_id}"])
    except Exception as e:                      # noqa: BLE001 — isolar cada destinatário
        with db.ligacao() as c:
            c.execute("UPDATE campaign_recipients SET status = 'failed', failed_at = ?, "
                      "last_error = ? WHERE id = ?", (tempo.iso_utc(), str(e)[:500], recipient_id))
            db.registar_evento(c, "campaign.message_failed", "campaign", campaign_id,
                               {"recipient_id": recipient_id, "customer_id": customer_id},
                               dedupe_key=f"campaign.message_failed:{recipient_id}", tenant_id=tenant_id)
        return

    with db.ligacao() as c:
        c.execute("UPDATE campaign_recipients SET status = 'sent', sent_at = ? WHERE id = ?",
                  (tempo.iso_utc(), recipient_id))
        db.registar_evento(c, "campaign.message_sent", "campaign", campaign_id,
                           {"recipient_id": recipient_id, "customer_id": customer_id},
                           dedupe_key=f"campaign.message_sent:{recipient_id}", tenant_id=tenant_id)


def executar_envio_campanha(job: dict):
    """Handler de TYPE_CAMPAIGN_SEND — devolve `notifications.jobs.CANCELLED`
    quando a campanha já não é elegível para continuar (não existe/foi
    cancelada/já está concluída); caso contrário devolve sempre None,
    incluindo quando ainda falta trabalho — o resto vive num NOVO job (ver
    docstring do módulo), nunca nesta mesma linha."""
    tenant_id = job.get("tenant_id") or 1
    payload = job.get("payload") or {}
    campaign_id = payload.get("campaign_id")
    with db.ligacao() as c:
        camp = _obter_linha(c, campaign_id, tenant_id) if campaign_id else None
    if not camp:
        log.info("job #%s: campanha #%s já não existe", job["id"], campaign_id)
        return notif_jobs.CANCELLED
    if camp["status"] in (STATUS_CANCELLED, STATUS_COMPLETED):
        log.info("job #%s: campanha #%s está %s", job["id"], campaign_id, camp["status"])
        return notif_jobs.CANCELLED

    if camp["status"] == STATUS_SCHEDULED:
        agora = tempo.iso_utc()
        with db.ligacao() as c:
            c.execute("UPDATE campaigns SET status = 'running', started_at = COALESCE(started_at, ?) "
                      "WHERE id = ?", (agora, campaign_id))
            db.registar_evento(c, "campaign.started", "campaign", campaign_id, {},
                               dedupe_key=f"campaign.started:{campaign_id}", tenant_id=tenant_id)

    lote = config.CAMPAIGN_SEND_BATCH_SIZE
    with db.ligacao() as c:
        pendentes = c.execute(
            "SELECT id, customer_id, phone, locale FROM campaign_recipients "
            "WHERE campaign_id = ? AND status = 'pending' ORDER BY id LIMIT ?",
            (campaign_id, lote)).fetchall()

    for rid, customer_id, phone, locale in pendentes:
        _enviar_para_recipiente(campaign_id, rid, customer_id, phone, locale, tenant_id)

    with db.ligacao() as c:
        ainda_pendentes = c.execute(
            "SELECT COUNT(*) FROM campaign_recipients WHERE campaign_id = ? AND status = 'pending'",
            (campaign_id,)).fetchone()[0]

    if ainda_pendentes:
        novo_offset = (payload.get("offset") or 0) + len(pendentes)
        notif_jobs.enqueue_job(
            TYPE_CAMPAIGN_SEND, tempo.iso_utc(), tenant_id=tenant_id,
            payload={"campaign_id": campaign_id, "offset": novo_offset},
            idempotency_key=_chave_lote(campaign_id, novo_offset))
        return None

    with db.ligacao() as c:
        atual = c.execute("SELECT status FROM campaigns WHERE id = ?", (campaign_id,)).fetchone()
        if atual and atual[0] == STATUS_CANCELLED:
            return None  # cancelada entretanto — nunca reabre uma campanha cancelada
        agora = tempo.iso_utc()
        c.execute("UPDATE campaigns SET status = 'completed', completed_at = ? WHERE id = ?",
                  (agora, campaign_id))
        db.registar_evento(c, "campaign.completed", "campaign", campaign_id, {},
                           dedupe_key=f"campaign.completed:{campaign_id}", tenant_id=tenant_id)
    return None


# ---------------------------------------------------------------------------
# Clique + conversão
# ---------------------------------------------------------------------------
def registar_clique(campaign_id: int, recipient_id: int, telefone: str, tenant_id: int = 1) -> dict | None:
    """Chamado a partir do botão [Marcar agora] do WhatsApp (bot.py,
    id_botao "campanha_marcar_<campaign_id>_<recipient_id>"). Só age se
    `telefone` corresponder mesmo ao destinatário — nunca deixa outro número
    agir sobre um recipient alheio só por adivinhar o id (mesmo padrão de
    notifications.reminders.obter_appointment_do_job). Devolve sempre
    servico_id=None nesta primeira versão: o modelo mínimo de campanha (§4
    do patch) não tem um serviço associado — só a FILTER "serviço anterior"
    da segmentação, que é sobre quem recebe, não sobre o que se marca a
    seguir. "Marcar agora" entra sempre pela seleção de serviço normal."""
    with db.ligacao() as c:
        row = c.execute(
            "SELECT id, customer_id, phone, status, clicked_at FROM campaign_recipients "
            "WHERE id = ? AND campaign_id = ? AND tenant_id = ?",
            (recipient_id, campaign_id, tenant_id)).fetchone()
        if not row or row[2] != telefone:
            return None
        rid, customer_id, _, status, clicked_at = row
        if not clicked_at:
            novo_status = status if status == REC_CONVERTED else REC_CLICKED
            c.execute("UPDATE campaign_recipients SET clicked_at = ?, status = ? WHERE id = ?",
                      (tempo.iso_utc(), novo_status, rid))
            db.registar_evento(c, "campaign.clicked", "campaign", campaign_id,
                               {"recipient_id": rid, "customer_id": customer_id},
                               dedupe_key=f"campaign.clicked:{rid}", tenant_id=tenant_id)
    return {"campaign_id": campaign_id, "recipient_id": rid, "customer_id": customer_id, "servico_id": None}


def handler_evento_conversao(ev: dict) -> None:
    """booking.created/booking.pending -> se a marcação tiver campaign_id
    (só acontece quando booking_source == "whatsapp_campaign", ver bot.py),
    fecha o funil desta campanha para este cliente. Idempotente: um
    recipient já "converted" nunca é reescrito (reprocessar o mesmo evento —
    ex.: retry do drain — nunca duplica a conversão nem troca o booking_id)."""
    if ev.get("type") not in ("booking.created", "booking.pending"):
        return
    appointment_id = ev.get("entity_id")
    tenant_id = ev.get("tenant_id") or 1
    if not appointment_id:
        return
    with db.ligacao() as c:
        row = c.execute("SELECT campaign_id, customer_id FROM agendamentos WHERE id = ? AND tenant_id = ?",
                        (appointment_id, tenant_id)).fetchone()
        if not row or not row[0]:
            return
        campaign_id, customer_id = row
        rec = c.execute("SELECT id, status FROM campaign_recipients WHERE campaign_id = ? AND customer_id = ?",
                        (campaign_id, customer_id)).fetchone()
        if not rec:
            return
        rid, status = rec
        if status == REC_CONVERTED:
            return
        c.execute("UPDATE campaign_recipients SET status = 'converted', converted_at = ?, booking_id = ? "
                  "WHERE id = ?", (tempo.iso_utc(), appointment_id, rid))
        db.registar_evento(c, "campaign.converted", "campaign", campaign_id,
                           {"recipient_id": rid, "customer_id": customer_id, "booking_id": appointment_id},
                           dedupe_key=f"campaign.converted:{rid}", tenant_id=tenant_id)


# ---------------------------------------------------------------------------
# DEMO (§23 do patch) — chamado por bot.py:_semear_dados_demo, só quando
# ENABLE_DEMO_SEED está ligado. NUNCA envia WhatsApp: os estados dos
# destinatários são escritos diretamente (o mesmo espírito de
# bot._neutralizar_eventos_demo), nunca através de executar_envio_campanha —
# mesmo sendo DEMO_PHONE_PREFIX já bloqueado em messaging.whatsapp.enviar,
# esta função evita QUALQUER chamada real, por princípio, tal como o resto
# do seed. NUNCA liga marketing_opt_in em clientes reais: só nos
# `customer_ids` DEMO indicados pelo chamador.
# ---------------------------------------------------------------------------
_DEMO_DISTRIBUICAO = (REC_SENT, REC_SENT, REC_CLICKED, REC_CONVERTED, REC_SENT, REC_FAILED)


def seed_demo(tenant_id: int, customer_ids: list[int]) -> dict:
    if not customer_ids:
        return {"created": False}
    with db.ligacao() as c:
        ja = c.execute("SELECT COUNT(*) FROM campaigns WHERE tenant_id = ?", (tenant_id,)).fetchone()[0]
        if ja:
            return {"created": False}

        # Opt-in só numa PARTE dos clientes demo — mostra também exclusão por
        # falta de consentimento, tal como aconteceria com dados reais (nunca
        # 100% dos clientes "elegíveis" de propósito).
        opt_in_ids = customer_ids[: max(1, (len(customer_ids) * 7) // 10)]
        marcas = ", ".join("?" for _ in opt_in_ids)
        c.execute(f"UPDATE customers SET marketing_opt_in = 1 WHERE id IN ({marcas})", opt_in_ids)
        clientes = c.execute(
            f"SELECT id, phone, name, locale FROM customers WHERE id IN ({marcas}) ORDER BY id",
            opt_in_ids).fetchall()

        agora = tempo.iso_utc()
        excluidos_1 = max(0, len(customer_ids) - len(clientes))

        # 1) Campanha CONCLUÍDA — "Reativação" (sem visita há 90 dias)
        cur = c.execute(
            "INSERT INTO campaigns (tenant_id, name, status, template_key, segment_json, "
            "eligible_count, excluded_count, scheduled_at, created_at, started_at, completed_at) "
            "VALUES (?, ?, 'completed', ?, ?, ?, ?, ?, ?, ?, ?)",
            (tenant_id, "Reativação — clientes inativos", TEMPLATE_CLIENT_REACTIVATION,
             json.dumps({"no_visit_days": 90}, ensure_ascii=False), len(clientes), excluidos_1,
             agora, agora, agora, agora))
        camp1 = cur.lastrowid
        for i, (cid, phone, name, locale) in enumerate(clientes):
            estado = _DEMO_DISTRIBUICAO[i % len(_DEMO_DISTRIBUICAO)]
            booking_id = None
            if estado == REC_CONVERTED:
                fila = c.execute("SELECT id FROM agendamentos WHERE customer_id = ? ORDER BY id DESC LIMIT 1",
                                 (cid,)).fetchone()
                booking_id = fila[0] if fila else None
            c.execute(
                "INSERT INTO campaign_recipients (campaign_id, tenant_id, customer_id, phone, locale, "
                "status, sent_at, clicked_at, converted_at, failed_at, booking_id, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (camp1, tenant_id, cid, phone, locale or "pt", estado,
                 agora if estado != REC_FAILED else None,
                 agora if estado in (REC_CLICKED, REC_CONVERTED) else None,
                 agora if estado == REC_CONVERTED else None,
                 agora if estado == REC_FAILED else None,
                 booking_id, agora))

        # 2) Campanha AGENDADA — "Sem marcação futura" (destinatários "pending")
        futuro = tempo.iso_utc(tempo.agora_utc() + timedelta(days=3))
        metade = clientes[: max(1, len(clientes) // 2)]
        cur = c.execute(
            "INSERT INTO campaigns (tenant_id, name, status, template_key, segment_json, "
            "eligible_count, excluded_count, scheduled_at, created_at) "
            "VALUES (?, ?, 'scheduled', ?, ?, ?, ?, ?, ?)",
            (tenant_id, "Sem marcação futura", TEMPLATE_CLIENT_REACTIVATION,
             json.dumps({"no_future_booking": True}, ensure_ascii=False), len(metade),
             max(0, len(customer_ids) - len(metade)), futuro, agora))
        camp2 = cur.lastrowid
        for cid, phone, name, locale in metade:
            c.execute(
                "INSERT INTO campaign_recipients (campaign_id, tenant_id, customer_id, phone, locale, "
                "status, created_at) VALUES (?, ?, ?, ?, ?, 'pending', ?)",
                (camp2, tenant_id, cid, phone, locale or "pt", agora))

    # O job de envio existe (para o Attention/lista de automation_jobs ficar
    # coerente), mas com run_at daqui a 3 dias — nunca dispara durante a
    # navegação normal da demo.
    notif_jobs.enqueue_job(TYPE_CAMPAIGN_SEND, futuro, tenant_id=tenant_id,
                           payload={"campaign_id": camp2, "offset": 0},
                           idempotency_key=_chave_lote(camp2, 0))
    return {"created": True, "completed_campaign_id": camp1, "scheduled_campaign_id": camp2}
