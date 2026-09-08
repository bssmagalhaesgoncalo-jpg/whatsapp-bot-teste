"""
RESULTADOS / IMPACTO ECONÓMICO DO BMS (P3).

Responde a UMA pergunta, para a Daniela: "o sistema está a produzir valor?".
É estratégico, não operacional — o "o que preciso de fazer agora" continua
no Attention Center (`operations/engine.py`). As duas páginas não se
confundem (ver §19 do patch).

Regra de ouro deste módulo: TUDO vem de tabelas/eventos que já existem
(`agendamentos`, `invoices`, `invoice_lines`, `customers`, `events`,
`automation_jobs`). Nenhuma métrica é inventada, estimada sem o dizer, ou
inferida sem um evento real por trás. Onde uma métrica não pode ser
calculada com confiança (ex.: ocupação de agenda), não aparece — não se
mostra um número fabricado (ver §26 do patch).

Cada KPI tem UMA definição, UMA fonte, e um teste dedicado em
`tests/test_resultados.py`. Ver esse ficheiro para a leitura "por exemplo"
de cada regra abaixo.

Convenções de período usadas:
  • bookings / origem / BMS       -> `agendamentos.criado_em` (quando o
    trabalho foi feito pelo sistema, não quando o serviço vai acontecer).
  • receita                        -> `invoices.paid_at` (quando o dinheiro
    entrou de facto).
  • no-show / cancelamentos        -> `agendamentos.data_iso` (o que
    aconteceu nesse período de agenda).
  • clientes novos                 -> `customers.first_seen` (mesma
    semântica já usada em `operations.engine.resumo_hoje`).
  • reminders / feedback / rebooking / tempo poupado -> `events.created_at`
    (o instante em que a automação agiu de facto).
"""

from __future__ import annotations

import json
from datetime import date, timedelta

import config
import db
import estados
import tempo

PERIODOS_DIAS = {"7d": 7, "30d": 30, "90d": 90}
PERIODOS_VALIDOS = ("7d", "30d", "90d", "ano")

# Únicas fontes que contam como "trabalho feito pelo BMS" — nunca
# 'dashboard' (a Daniela a marcar manualmente) nem 'unknown' (marcações
# antigas, anteriores a esta coluna, cuja origem nunca se inventa).
FONTES_BMS = ("whatsapp_bot", "rebooking_followup")


class PeriodoInvalido(ValueError):
    """periodo fora de {7d, 30d, 90d, ano} -> quem chama devolve HTTP 400."""


# ---------------------------------------------------------------------------
# Período
# ---------------------------------------------------------------------------
def _limites(periodo: str, hoje: date) -> dict:
    if periodo not in PERIODOS_VALIDOS:
        raise PeriodoInvalido(f"período inválido: {periodo!r}")
    if periodo == "ano":
        inicio = date(hoje.year, 1, 1)
    else:
        inicio = hoje - timedelta(days=PERIODOS_DIAS[periodo] - 1)
    dias = (hoje - inicio).days + 1
    fim_anterior = inicio - timedelta(days=1)
    inicio_anterior = fim_anterior - timedelta(days=dias - 1)
    return {
        "periodo": periodo,
        "inicio": inicio.isoformat(),
        "fim": hoje.isoformat(),
        "dias": dias,
        "inicio_anterior": inicio_anterior.isoformat(),
        "fim_anterior": fim_anterior.isoformat(),
    }


def _inicio_utc(data_iso: str) -> str:
    return tempo.iso_utc(tempo.combinar_local(data_iso, "00:00"))


def _fim_utc_exclusivo(data_iso: str) -> str:
    """Meia-noite do dia SEGUINTE a `data_iso`, em UTC — limite superior
    EXCLUSIVO (evita comparar strings ISO com hora fixa "23:59:59")."""
    seguinte = date.fromisoformat(data_iso) + timedelta(days=1)
    return tempo.iso_utc(tempo.combinar_local(seguinte.isoformat(), "00:00"))


# ---------------------------------------------------------------------------
# Eventos (outbox) — leitura genérica, parse do payload em Python (o mesmo
# padrão usado em notifications/reminders.py e notifications/followup.py
# para ler payloads gravados por `db.registar_evento`).
# ---------------------------------------------------------------------------
def _eventos_no_periodo(conn, tenant_id, tipos, ini_utc, fim_utc) -> list[dict]:
    marcadores = ",".join("?" for _ in tipos)
    linhas = conn.execute(
        f"SELECT type, payload, created_at FROM events "
        f"WHERE tenant_id = ? AND type IN ({marcadores}) "
        f"AND created_at >= ? AND created_at < ?",
        (tenant_id, *tipos, ini_utc, fim_utc),
    ).fetchall()
    eventos = []
    for tipo, payload, criado_em in linhas:
        try:
            dados = json.loads(payload or "{}")
        except (TypeError, ValueError):
            dados = {}
        eventos.append({"type": tipo, "payload": dados, "created_at": criado_em})
    return eventos


# ---------------------------------------------------------------------------
# Marcações via BMS (§7)
# ---------------------------------------------------------------------------
def _bookings(conn, tenant_id, ini_utc, fim_utc) -> dict:
    linhas = conn.execute(
        "SELECT COALESCE(NULLIF(TRIM(booking_source), ''), 'unknown'), COUNT(*) "
        "FROM agendamentos WHERE tenant_id = ? AND criado_em >= ? AND criado_em < ? "
        "GROUP BY 1",
        (tenant_id, ini_utc, fim_utc),
    ).fetchall()
    por_origem = {origem: n for origem, n in linhas}
    total = sum(por_origem.values())
    bms = sum(por_origem.get(f, 0) for f in FONTES_BMS)
    return {
        "total": total,
        "bms": bms,
        "whatsapp_bot": por_origem.get("whatsapp_bot", 0),
        "rebooking_followup": por_origem.get("rebooking_followup", 0),
        "dashboard": por_origem.get("dashboard", 0),
        "unknown": por_origem.get("unknown", 0),
    }


# ---------------------------------------------------------------------------
# Receita atribuída (§8) — só faturas PAGAS, `paid_at` no período.
# ---------------------------------------------------------------------------
def _revenue(conn, tenant_id, ini_utc, fim_utc) -> dict:
    linhas = conn.execute(
        "SELECT i.total_cents, COALESCE(NULLIF(TRIM(a.booking_source), ''), 'unknown') "
        "FROM invoices i LEFT JOIN agendamentos a ON a.id = i.appointment_id "
        "WHERE i.tenant_id = ? AND i.status = 'paid' AND i.paid_at >= ? AND i.paid_at < ?",
        (tenant_id, ini_utc, fim_utc),
    ).fetchall()
    total_cents = sum(cents for cents, _ in linhas)
    faturas_pagas = len(linhas)
    por_origem_cents: dict[str, int] = {}
    for cents, origem in linhas:
        por_origem_cents[origem] = por_origem_cents.get(origem, 0) + cents
    bms_cents = sum(por_origem_cents.get(f, 0) for f in FONTES_BMS)

    por_servico = conn.execute(
        "SELECT il.description, SUM(il.line_total_cents) AS total "
        "FROM invoice_lines il JOIN invoices i ON i.id = il.invoice_id "
        "WHERE i.tenant_id = ? AND i.status = 'paid' AND i.paid_at >= ? AND i.paid_at < ? "
        "GROUP BY il.description ORDER BY total DESC",
        (tenant_id, ini_utc, fim_utc),
    ).fetchall()

    return {
        "total_paid_cents": total_cents,
        "faturas_pagas": faturas_pagas,
        "ticket_medio_cents": round(total_cents / faturas_pagas) if faturas_pagas else None,
        "whatsapp_bot_cents": por_origem_cents.get("whatsapp_bot", 0),
        "rebooking_followup_cents": por_origem_cents.get("rebooking_followup", 0),
        "dashboard_cents": por_origem_cents.get("dashboard", 0),
        "unknown_cents": por_origem_cents.get("unknown", 0),
        "bms_cents": bms_cents,
        "por_servico": [{"servico": desc, "total_cents": tot} for desc, tot in por_servico],
    }


# ---------------------------------------------------------------------------
# Clientes novos / recorrentes (§9)
# ---------------------------------------------------------------------------
def _customers(conn, tenant_id, ini_utc, fim_utc, ini_date, fim_date) -> dict:
    novos = conn.execute(
        "SELECT COUNT(*) FROM customers WHERE tenant_id = ? "
        "AND first_seen >= ? AND first_seen < ?",
        (tenant_id, ini_utc, fim_utc),
    ).fetchone()[0]

    atendidos_ids = [
        r[0] for r in conn.execute(
            "SELECT DISTINCT customer_id FROM agendamentos WHERE tenant_id = ? "
            "AND LOWER(estado) = ? AND data_iso BETWEEN ? AND ? AND customer_id IS NOT NULL",
            (tenant_id, estados.COMPLETED, ini_date, fim_date),
        ).fetchall()
    ]
    atendidos = len(atendidos_ids)
    recorrentes = 0
    if atendidos_ids:
        marcadores = ",".join("?" for _ in atendidos_ids)
        # "Cliente recorrente" reusa a definição já existente em
        # db.recalcular_customer(): visits_count (histórico completo de
        # marcações `completed`) >= 2. Não se inventa uma 2.ª definição
        # concorrente só para este período.
        recorrentes = conn.execute(
            f"SELECT COUNT(*) FROM customers WHERE id IN ({marcadores}) AND visits_count >= 2",
            atendidos_ids,
        ).fetchone()[0]

    taxa = round(recorrentes / atendidos * 100, 1) if atendidos else None
    return {
        "novos": novos,
        "atendidos": atendidos,
        "recorrentes": recorrentes,
        "taxa_recorrencia_pct": taxa,
    }


# ---------------------------------------------------------------------------
# No-show (§10) — nunca conflar com cancelamento; denominador zero -> None
# (a UI mostra "Sem dados suficientes", nunca NaN/Infinity).
# ---------------------------------------------------------------------------
def _no_show(conn, tenant_id, ini_date, fim_date) -> dict:
    linhas = conn.execute(
        "SELECT LOWER(estado), COUNT(*) FROM agendamentos WHERE tenant_id = ? "
        "AND data_iso BETWEEN ? AND ? AND LOWER(estado) IN (?, ?) GROUP BY 1",
        (tenant_id, ini_date, fim_date, estados.COMPLETED, estados.NO_SHOW),
    ).fetchall()
    por_estado = dict(linhas)
    elegiveis = sum(por_estado.values())
    no_shows = por_estado.get(estados.NO_SHOW, 0)
    rate = round(no_shows / elegiveis * 100, 1) if elegiveis else None
    return {"no_shows": no_shows, "elegiveis": elegiveis, "rate_pct": rate}


# ---------------------------------------------------------------------------
# Cancelamentos (§10) — métrica SEPARADA do no-show.
# ---------------------------------------------------------------------------
def _cancellations(conn, tenant_id, ini_date, fim_date) -> dict:
    total = conn.execute(
        "SELECT COUNT(*) FROM agendamentos WHERE tenant_id = ? AND data_iso BETWEEN ? AND ?",
        (tenant_id, ini_date, fim_date),
    ).fetchone()[0]
    cancelados = conn.execute(
        "SELECT COUNT(*) FROM agendamentos WHERE tenant_id = ? AND data_iso BETWEEN ? AND ? "
        "AND LOWER(estado) = ?",
        (tenant_id, ini_date, fim_date, estados.CANCELLED),
    ).fetchone()[0]
    rate = round(cancelados / total * 100, 1) if total else None
    return {"cancelamentos": cancelados, "marcacoes_no_periodo": total, "rate_pct": rate}


# ---------------------------------------------------------------------------
# Reminder 24h (§11) — só factos comprovados. `reminder.reschedule_started`
# fica separado como "iniciados" (dispara ANTES do reagendamento em si) —
# nunca se soma a `confirmacoes`, e nunca se afirma causalidade sobre o
# no-show ter (ou não) mudado por causa disto.
# ---------------------------------------------------------------------------
def _reminders(conn, tenant_id, ini_utc, fim_utc) -> dict:
    tipos = ("reminder.sent", "booking.confirmed", "reminder.reschedule_started",
             "reminder.cancelled")
    eventos = _eventos_no_periodo(conn, tenant_id, tipos, ini_utc, fim_utc)
    enviados = sum(1 for e in eventos if e["type"] == "reminder.sent")
    confirmacoes = sum(
        1 for e in eventos
        if e["type"] == "booking.confirmed" and e["payload"].get("origin") == "reminder_24h"
    )
    reagendamentos_iniciados = sum(1 for e in eventos if e["type"] == "reminder.reschedule_started")
    cancelamentos = sum(1 for e in eventos if e["type"] == "reminder.cancelled")
    return {
        "enviados": enviados,
        "confirmacoes": confirmacoes,
        "reagendamentos_iniciados": reagendamentos_iniciados,
        "cancelamentos": cancelamentos,
    }


# ---------------------------------------------------------------------------
# Feedback (§12) — só respostas reais recebidas. Sem sentiment analysis.
# ---------------------------------------------------------------------------
def _feedback(conn, tenant_id, ini_utc, fim_utc) -> dict:
    eventos = _eventos_no_periodo(
        conn, tenant_id, ("feedback.requested", "feedback.received"), ini_utc, fim_utc)
    pedidos = sum(1 for e in eventos if e["type"] == "feedback.requested")
    recebidos = sum(1 for e in eventos if e["type"] == "feedback.received")
    taxa = round(recebidos / pedidos * 100, 1) if pedidos else None
    return {"pedidos": pedidos, "recebidos": recebidos, "taxa_resposta_pct": taxa}


# ---------------------------------------------------------------------------
# Rebooking automático (§13) — conversão = proporção agregada na mesma
# janela (marcações criadas com origem rebooking_followup / follow-ups
# enviados nesse período). É uma proporção, não um match causal por cliente
# individual — documentado assim de propósito (ver notas do módulo).
# ---------------------------------------------------------------------------
def _rebooking(conn, tenant_id, ini_utc, fim_utc, bookings: dict, revenue: dict) -> dict:
    eventos = _eventos_no_periodo(
        conn, tenant_id, ("rebooking_followup.sent", "rebooking_followup.snoozed"),
        ini_utc, fim_utc)
    enviados = sum(1 for e in eventos if e["type"] == "rebooking_followup.sent")
    mais_tarde = sum(1 for e in eventos if e["type"] == "rebooking_followup.snoozed")
    marcacoes_criadas = bookings["rebooking_followup"]
    conversao = round(marcacoes_criadas / enviados * 100, 1) if enviados else None
    return {
        "seguimentos_enviados": enviados,
        "mais_tarde": mais_tarde,
        "marcacoes_criadas": marcacoes_criadas,
        "conversao_pct": conversao,
        "receita_atribuida_cents": revenue["rebooking_followup_cents"],
    }


# ---------------------------------------------------------------------------
# Tempo poupado (§14) — ESTIMATIVA, nunca "tempo real". Só ações
# automatizadas reais e desduplicadas (uma linha de evento/coluna por
# ação); ver GAP documentado abaixo sobre o que fica de fora.
#
# GAP CONHECIDO (disclosure, não invenção): reagendamentos/cancelamentos
# "self-service" pelo cliente via WhatsApp NÃO entram aqui, porque os
# eventos `booking.rescheduled` / `booking.cancelled` genéricos não trazem
# hoje um marcador de origem (cliente vs. equipa no painel) — contá-los
# arriscaria atribuir à automação um trabalho que a equipa fez manualmente
# no painel. Ver ponto (16) do relatório final.
# ---------------------------------------------------------------------------
def _time_saved(conn, tenant_id, ini_utc, fim_utc, bookings, reminders, feedback, rebooking) -> dict:
    post_service_enviados = conn.execute(
        "SELECT COUNT(*) FROM events WHERE tenant_id = ? AND type = 'post_service.sent' "
        "AND created_at >= ? AND created_at < ?",
        (tenant_id, ini_utc, fim_utc),
    ).fetchone()[0]
    pdfs_enviados = conn.execute(
        "SELECT COUNT(*) FROM invoices WHERE tenant_id = ? "
        "AND pdf_sent_at >= ? AND pdf_sent_at < ?",
        (tenant_id, ini_utc, fim_utc),
    ).fetchone()[0]

    detalhe = {
        "marcacoes_whatsapp_bot": bookings["whatsapp_bot"],
        "marcacoes_rebooking": bookings["rebooking_followup"],
        "reminders_enviados": reminders["enviados"],
        "cancelamentos_via_reminder": reminders["cancelamentos"],
        "seguimentos_rebooking_enviados": rebooking["seguimentos_enviados"],
        "agradecimentos_pos_atendimento": post_service_enviados,
        "pedidos_feedback": feedback["pedidos"],
        "pdfs_fatura_enviados": pdfs_enviados,
    }
    total_acoes = sum(detalhe.values())
    minutos_por_acao = config.ESTIMATED_MINUTES_SAVED_PER_AUTOMATION
    return {
        "acoes_automatizadas": total_acoes,
        "detalhe": detalhe,
        "minutos_por_acao": minutos_por_acao,
        "minutos_estimados": total_acoes * minutos_por_acao,
        "estimativa": True,
    }


# ---------------------------------------------------------------------------
# Agregação de uma janela [inicio, fim] (datas locais, inclusive)
# ---------------------------------------------------------------------------
def _calcular_janela(tenant_id: int, inicio_date: str, fim_date: str) -> dict:
    ini_utc = _inicio_utc(inicio_date)
    fim_utc = _fim_utc_exclusivo(fim_date)
    with db.ligacao() as conn:
        bookings = _bookings(conn, tenant_id, ini_utc, fim_utc)
        revenue = _revenue(conn, tenant_id, ini_utc, fim_utc)
        customers = _customers(conn, tenant_id, ini_utc, fim_utc, inicio_date, fim_date)
        no_show = _no_show(conn, tenant_id, inicio_date, fim_date)
        cancellations = _cancellations(conn, tenant_id, inicio_date, fim_date)
        reminders = _reminders(conn, tenant_id, ini_utc, fim_utc)
        feedback = _feedback(conn, tenant_id, ini_utc, fim_utc)
        rebooking = _rebooking(conn, tenant_id, ini_utc, fim_utc, bookings, revenue)
        time_saved = _time_saved(conn, tenant_id, ini_utc, fim_utc, bookings, reminders,
                                  feedback, rebooking)
    return {
        "bookings": bookings,
        "revenue": revenue,
        "customers": customers,
        "no_show": no_show,
        "cancellations": cancellations,
        "reminders": reminders,
        "feedback": feedback,
        "rebooking": rebooking,
        "time_saved": time_saved,
    }


def _comparacao(atual: dict, anterior: dict) -> dict:
    def _delta_pct(a, p):
        if not p:
            return None
        return round((a - p) / p * 100, 1)

    def _delta_pp(a, p):
        if a is None or p is None:
            return None
        return round(a - p, 1)

    return {
        "marcacoes_bms": {
            "anterior": anterior["bookings"]["bms"],
            "delta_pct": _delta_pct(atual["bookings"]["bms"], anterior["bookings"]["bms"]),
        },
        "receita_paga_cents": {
            "anterior": anterior["revenue"]["total_paid_cents"],
            "delta_pct": _delta_pct(atual["revenue"]["total_paid_cents"],
                                     anterior["revenue"]["total_paid_cents"]),
        },
        "receita_bms_cents": {
            "anterior": anterior["revenue"]["bms_cents"],
            "delta_pct": _delta_pct(atual["revenue"]["bms_cents"], anterior["revenue"]["bms_cents"]),
        },
        "no_show_rate_pct": {
            "anterior": anterior["no_show"]["rate_pct"],
            "delta_pp": _delta_pp(atual["no_show"]["rate_pct"], anterior["no_show"]["rate_pct"]),
        },
    }


def calcular_resultados(tenant_id: int = 1, periodo: str = "30d", agora=None) -> dict:
    """Ponto de entrada único — usado por `GET /api/resultados`.

    `agora` só existe para os testes (injetar um "hoje" fixo); em produção
    usa sempre `tempo.agora_zurique()`."""
    hoje = (agora or tempo.agora_zurique()).date()
    lim = _limites(periodo, hoje)

    atual = _calcular_janela(tenant_id, lim["inicio"], lim["fim"])
    atual["period"] = {
        "periodo": periodo,
        "inicio": lim["inicio"],
        "fim": lim["fim"],
        "dias": lim["dias"],
        "timezone": tempo.NOME_FUSO,
    }

    anterior = _calcular_janela(tenant_id, lim["inicio_anterior"], lim["fim_anterior"])
    # Só se compara ao período anterior quando há dados lá — nunca se
    # fabrica uma comparação "0 -> X" (§4 do patch: "nunca fabricar
    # comparação").
    tem_dados_anteriores = anterior["bookings"]["total"] > 0 or anterior["revenue"]["faturas_pagas"] > 0
    atual["comparacao"] = _comparacao(atual, anterior) if tem_dados_anteriores else None
    return atual
