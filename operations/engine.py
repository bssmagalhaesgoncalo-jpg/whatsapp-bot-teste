"""
operations/engine.py — OPERATION ENGINE.

O que a Daniela precisa de ver e fazer AGORA:
  • serviço a decorrer / próxima cliente  (card do topo — Fase E1)
  • marcar "chegou" / "iniciar" / "concluir"  (Fase E3-E5)
  • calcular quem é afetado por um atraso ANTES de avisar  (Fase E6)
  • attention center: só o que é acionável  (Fase F)

Estado OPERACIONAL (coluna op_status) é distinto do comercial (estado):
  scheduled -> arrived -> in_progress -> done
Uma marcação cancelled/no_show nunca "chega".
"""

from __future__ import annotations

from datetime import datetime, timedelta

import db
import estados
import catalogo
import tempo
import parsing


# ---------------------------------------------------------------------------
def _marcacoes_de_hoje(tenant_id: int = 1, data_iso: str | None = None) -> list[dict]:
    data_iso = data_iso or tempo.hoje_zurique().isoformat()
    dmy = f"{data_iso[8:10]}.{data_iso[5:7]}.{data_iso[0:4]}"
    campos = ("id", "telefone", "nome", "servico", "servico_id", "data", "hora",
              "data_iso", "hora_hhmm", "duracao_min", "preco_cents", "estado",
              "op_status", "arrived_at", "started_at", "completed_at", "customer_id")
    with db.ligacao() as c:
        rows = c.execute(
            f"SELECT {', '.join(campos)} FROM agendamentos "
            "WHERE tenant_id = ? AND (data_iso = ? OR data LIKE ?) ORDER BY hora_hhmm, hora",
            (tenant_id, data_iso, f"%{dmy}%")).fetchall()
    out = []
    for r in rows:
        d = dict(zip(campos, r))
        if estados.normalizar(d["estado"]) in (estados.CANCELLED,):
            continue
        d["hhmm"] = d["hora_hhmm"] or parsing.hora_hhmm_de_texto(d["hora"])
        d["dur_min"] = d["duracao_min"] or parsing.minutos_de_duracao_texto(d.get("servico") and d.get("duracao"))
        out.append(d)
    return out


def _dt_local(data_iso: str, hhmm: str) -> datetime | None:
    return tempo.combinar_local(data_iso, hhmm) if data_iso and hhmm else None


# ---------------------------------------------------------------------------
# E1 / E2 — serviço a decorrer OU próxima cliente
# ---------------------------------------------------------------------------
def cartao_operacional(tenant_id: int = 1, agora: datetime | None = None) -> dict:
    """O card do topo do painel. `kind`:
        'in_progress'  -> serviço a decorrer (op_status=in_progress OU, se
                          ainda 'scheduled/arrived', a hora já passou)
        'next'         -> próxima cliente de hoje
        'done'         -> não há mais marcações hoje
    """
    agora = agora or tempo.agora_zurique()
    hoje = agora.date().isoformat()
    marcs = [m for m in _marcacoes_de_hoje(tenant_id, hoje) if m["hhmm"]]

    # 1) a decorrer: explicitamente iniciada
    em_curso = next((m for m in marcs if m["op_status"] == "in_progress"), None)
    # 2) senão, a que já devia estar a decorrer pelo relógio (e não está done)
    if not em_curso:
        for m in marcs:
            ini = _dt_local(hoje, m["hhmm"])
            fim = ini + timedelta(minutes=m["dur_min"] or 60) if ini else None
            if ini and ini <= agora < (fim or ini) and m["op_status"] != "done" \
                    and estados.normalizar(m["estado"]) != estados.NO_SHOW:
                em_curso = m
                break

    if em_curso:
        ini = _dt_local(hoje, em_curso["hhmm"])
        dur = em_curso["dur_min"] or 60
        fim = ini + timedelta(minutes=dur)
        decorrido = max(0, int((agora - ini).total_seconds() // 60))
        restante = max(0, int((fim - agora).total_seconds() // 60))
        return {
            "kind": "in_progress",
            "marcacao": _resumo(em_curso, tenant_id),
            "inicio": em_curso["hhmm"],
            "fim_previsto": fim.strftime("%H:%M"),
            "decorrido_min": decorrido,
            "restante_min": restante,
            "atrasado": agora > fim,
        }

    # próxima cliente (hora >= agora, ainda não done/no_show)
    futuras = []
    for m in marcs:
        ini = _dt_local(hoje, m["hhmm"])
        if ini and ini >= agora and m["op_status"] not in ("done",) \
                and estados.normalizar(m["estado"]) != estados.NO_SHOW:
            futuras.append((ini, m))
    futuras.sort(key=lambda x: x[0])
    if futuras:
        ini, m = futuras[0]
        faltam = int((ini - agora).total_seconds() // 60)
        return {
            "kind": "next",
            "marcacao": _resumo(m, tenant_id),
            "hora": m["hhmm"],
            "faltam_min": faltam,
            "chegou": m["op_status"] in ("arrived", "in_progress"),
        }

    return {"kind": "done", "marcacoes_hoje": len(marcs)}


def _resumo(m: dict, tenant_id: int) -> dict:
    servico = db.obter_servico(m["servico_id"]) if m.get("servico_id") else None
    cust = db.obter_customer(m["customer_id"]) if m.get("customer_id") else None
    cents = m.get("preco_cents")
    if cents is None and servico:
        cents = servico.get("preco_cents")
    ultima_do_servico = None
    if cust and m.get("servico_id"):
        with db.ligacao() as c:
            r = c.execute(
                "SELECT data_iso FROM agendamentos WHERE customer_id = ? AND servico_id = ? "
                "AND id <> ? AND LOWER(estado) IN ('confirmed','completed') "
                "ORDER BY data_iso DESC LIMIT 1", (cust["id"], m["servico_id"], m["id"])).fetchone()
            ultima_do_servico = r[0] if r else None
    return {
        "id": m["id"],
        "cliente": m.get("nome") or (cust or {}).get("name") or "Cliente",
        "telefone": m.get("telefone"),
        "customer_id": m.get("customer_id"),
        "servico": catalogo.nome_pt(servico) if servico else m.get("servico"),
        "servico_id": m.get("servico_id"),
        "duracao_min": m.get("dur_min"),
        "preco_label": ("Preço a confirmar" if cents is None else catalogo.formatar_cents(cents, "pt")),
        "preco_por_confirmar": cents is None,
        "op_status": m.get("op_status"),
        "estado": estados.normalizar(m.get("estado")),
        "cliente_visitas": (cust or {}).get("visits_count"),
        "cliente_no_shows": (cust or {}).get("no_show_count"),
        "cliente_ultima_visita": (cust or {}).get("last_visit"),
        "ultima_do_servico": ultima_do_servico,
        "notas": (cust or {}).get("notes_internal"),
    }


# ---------------------------------------------------------------------------
# E3-E5 — transições operacionais
# ---------------------------------------------------------------------------
_TRANSICOES = {
    "arrived": ("scheduled", "arrived_at"),
    "in_progress": ("arrived", "started_at"),
    "done": ("in_progress", "completed_at"),
}


def transicao_operacional(id_agendamento: int, novo: str, tenant_id: int = 1) -> dict:
    """arrived / in_progress / done. Regista o timestamp e (para 'done') passa
    também o estado comercial a 'completed'. Devolve o card atualizado."""
    if novo not in _TRANSICOES:
        raise ValueError(f"transição inválida: {novo}")
    coluna_ts = _TRANSICOES[novo][1]
    with db.ligacao() as c:
        r = c.execute("SELECT op_status, estado, tenant_id, customer_id, servico, servico_id, "
                      "data, hora FROM agendamentos WHERE id = ?", (id_agendamento,)).fetchone()
        if not r:
            raise LookupError("Marcação não encontrada.")
        agora = tempo.iso_utc()
        c.execute(f"UPDATE agendamentos SET op_status = ?, {coluna_ts} = COALESCE({coluna_ts}, ?) "
                  "WHERE id = ?", (novo, agora, id_agendamento))
        if novo == "done":
            c.execute("UPDATE agendamentos SET estado = ?, completed_at = COALESCE(completed_at, ?) "
                      "WHERE id = ?", (estados.COMPLETED, agora, id_agendamento))
            if estados.normalizar(r[1]) != estados.COMPLETED:
                db.registar_evento(c, "booking.completed", "appointment", id_agendamento,
                                   {"servico": r[4], "servico_id": r[5], "data": r[6], "hora": r[7],
                                    "customer_id": r[3]},
                                   dedupe_key=f"booking.completed:{id_agendamento}",
                                   tenant_id=r[2] or 1)
            if r[3]:
                db.recalcular_customer(r[3], conn=c)
    return cartao_operacional(tenant_id)


# ---------------------------------------------------------------------------
# E6 — atraso: quem é afetado (calcular ANTES de avisar)
# ---------------------------------------------------------------------------
def marcacoes_afetadas_por_atraso(minutos: int, tenant_id: int = 1,
                                  agora: datetime | None = None) -> dict:
    """Se a Daniela se atrasa `minutos`, que marcações de hoje AINDA por
    começar passam a sobrepor-se? Não envia nada — só devolve a lista para
    ela confirmar."""
    agora = agora or tempo.agora_zurique()
    hoje = agora.date().isoformat()
    marcs = sorted((m for m in _marcacoes_de_hoje(tenant_id, hoje) if m["hhmm"]),
                   key=lambda m: m["hhmm"])
    # ponto a partir do qual tudo desliza: fim previsto do que está a decorrer
    # (ou agora), + atraso.
    cursor = agora + timedelta(minutes=minutos)
    afetadas = []
    for m in marcs:
        if m["op_status"] in ("done",) or estados.normalizar(m["estado"]) == estados.NO_SHOW:
            continue
        ini = _dt_local(hoje, m["hhmm"])
        if not ini or ini < agora:
            # já a decorrer / passou — empurra o cursor à mesma
            fim = (ini or agora) + timedelta(minutes=(m["dur_min"] or 60) + minutos)
            cursor = max(cursor, fim)
            continue
        if ini < cursor:
            novo_ini = cursor
            afetadas.append({
                "id": m["id"], "cliente": m.get("nome") or "Cliente",
                "telefone": m.get("telefone"),
                "servico": m.get("servico"),
                "hora_original": m["hhmm"],
                "hora_estimada": novo_ini.strftime("%H:%M"),
                "atraso_min": int((novo_ini - ini).total_seconds() // 60),
            })
            cursor = novo_ini + timedelta(minutes=m["dur_min"] or 60)
        else:
            cursor = ini + timedelta(minutes=m["dur_min"] or 60)
    return {"atraso_min": minutos, "afetadas": afetadas}


# ---------------------------------------------------------------------------
# F — attention center (só o acionável)
# ---------------------------------------------------------------------------
def attention_items(tenant_id: int = 1, agora: datetime | None = None) -> list[dict]:
    agora = agora or tempo.agora_zurique()
    hoje = agora.date().isoformat()
    itens: list[dict] = []

    # preço por definir em marcações futuras
    with db.ligacao() as c:
        pend = c.execute(
            "SELECT a.id, a.nome, a.servico, a.data, a.hora FROM agendamentos a "
            "WHERE a.tenant_id = ? AND a.preco_cents IS NULL AND a.servico_id IS NOT NULL "
            "AND LOWER(a.estado) IN ('confirmed','pending') AND COALESCE(a.data_iso,'') >= ?",
            (tenant_id, hoje)).fetchall()
    for (aid, nome, servico, data, hora) in pend:
        itens.append({"nivel": "hoje", "tipo": "preco_pendente",
                      "titulo": f"Preço por definir — {nome or 'Cliente'} · {servico}",
                      "detalhe": f"{data} {hora}", "acao": "definir_preco",
                      "appointment_id": aid})

    # notificações que falharam todas as tentativas (só owner)
    with db.ligacao() as c:
        falhas = c.execute(
            "SELECT COUNT(*) FROM events WHERE tenant_id = ? AND processed_at IS NULL "
            "AND created_at < ?", (tenant_id, tempo.iso_utc(agora - timedelta(minutes=15)))).fetchone()[0]
    if falhas:
        itens.append({"nivel": "agora", "tipo": "automacao_falhou",
                      "titulo": f"{falhas} notificação(ões) por enviar há >15 min",
                      "detalhe": "A Meta pode estar em baixo — as marcações estão OK.",
                      "acao": "re_tentar"})

    # cliente pediu humano (bot pausado)
    with db.ligacao() as c:
        humanos = c.execute(
            "SELECT telefone, dados FROM sessoes WHERE tenant_id = ? "
            "AND dados LIKE '%\"needs_human\"%'", (tenant_id,)).fetchall()
    for (tel, _dados) in humanos:
        itens.append({"nivel": "agora", "tipo": "needs_human",
                      "titulo": "Cliente pediu para falar com a equipa",
                      "detalhe": tel, "acao": "abrir_conversa", "telefone": tel})

    # marcações de hoje sem confirmação e a < 6h
    for m in _marcacoes_de_hoje(tenant_id, hoje):
        if not m["hhmm"]:
            continue
        ini = _dt_local(hoje, m["hhmm"])
        if ini and agora < ini <= agora + timedelta(hours=6) and m["op_status"] == "scheduled":
            cust = db.obter_customer(m["customer_id"]) if m.get("customer_id") else None
            if cust and cust["no_show_count"] > 0:
                itens.append({"nivel": "hoje", "tipo": "risco_no_show",
                              "titulo": f"{m.get('nome') or 'Cliente'} — {m['hhmm']} · {cust['no_show_count']} no-show(s)",
                              "detalhe": "Confirmar por telefone?", "acao": "abrir_marcacao",
                              "appointment_id": m["id"]})

    ordem = {"agora": 0, "hoje": 1, "quando_puder": 2}
    itens.sort(key=lambda x: ordem.get(x["nivel"], 3))
    return itens


def resumo_hoje(tenant_id: int = 1, agora: datetime | None = None) -> dict:
    agora = agora or tempo.agora_zurique()
    hoje = agora.date().isoformat()
    marcs = _marcacoes_de_hoje(tenant_id, hoje)
    receita = 0
    receita_por_confirmar = False
    for m in marcs:
        if estados.normalizar(m["estado"]) in (estados.CONFIRMED, estados.COMPLETED):
            cents = m.get("preco_cents")
            if cents is None:
                receita_por_confirmar = True
            else:
                receita += cents
    with db.ligacao() as c:
        novos = c.execute(
            "SELECT COUNT(*) FROM customers WHERE tenant_id = ? AND substr(first_seen,1,10) = ?",
            (tenant_id, hoje)).fetchone()[0]
        cancel = c.execute(
            "SELECT COUNT(*) FROM agendamentos WHERE tenant_id = ? AND LOWER(estado) = 'cancelled' "
            "AND (data_iso = ? OR data LIKE ?)",
            (tenant_id, hoje, f"%{hoje[8:10]}.{hoje[5:7]}.{hoje[0:4]}%")).fetchone()[0]
    return {
        "data": hoje,
        "marcacoes": len(marcs),
        "concluidas": sum(1 for m in marcs if m["op_status"] == "done"),
        "receita_cents": receita,
        "receita_por_confirmar": receita_por_confirmar,
        "novos_clientes": novos,
        "cancelamentos": cancel,
        "primeira_hora": min((m["hhmm"] for m in marcs if m["hhmm"]), default=None),
    }
