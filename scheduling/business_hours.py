"""
scheduling/business_hours.py — horário de funcionamento + exceções + política.

Substitui a lista fixa `HORARIOS` do bot. É a base do motor de
disponibilidade (`scheduling.availability`).

Tabelas (migração 12):
  business_hours              (weekday, opens, closes, break_start, break_end)
  business_hours_exceptions   (date, closed, opens, closes, reason)
  booking_policy              (min_notice_min, max_notice_days, same_day, ...)
"""

from __future__ import annotations

from datetime import date, timedelta

import db
import tempo


def _hhmm_para_min(v) -> int | None:
    if not v:
        return None
    try:
        h, m = str(v).split(":")
        return int(h) * 60 + int(m)
    except (ValueError, AttributeError):
        return None


def politica(tenant_id: int = 1) -> dict:
    with db.ligacao() as c:
        r = c.execute(
            "SELECT min_notice_min, max_notice_days, same_day, slot_granularity_min, "
            "default_buffer_after_min FROM booking_policy WHERE tenant_id = ?", (tenant_id,)).fetchone()
    if not r:
        return {"min_notice_min": 120, "max_notice_days": 60, "same_day": True,
                "slot_granularity_min": 15, "default_buffer_after_min": 0}
    return dict(zip(("min_notice_min", "max_notice_days", "same_day",
                     "slot_granularity_min", "default_buffer_after_min"), r))


def janelas_do_dia(data_iso: str, tenant_id: int = 1, staff_id=None) -> list[tuple[int, int]]:
    """Janelas ABERTAS nesse dia, em minutos-desde-meia-noite: [(inicio, fim), ...].
    Uma exceção (feriado/férias/bloqueio) fechada devolve []. Uma pausa parte
    a janela em duas."""
    try:
        d = date.fromisoformat(data_iso)
    except ValueError:
        return []
    with db.ligacao() as c:
        exc = c.execute(
            "SELECT closed, opens, closes FROM business_hours_exceptions "
            "WHERE tenant_id = ? AND date = ? AND (staff_id IS ? OR staff_id IS NULL) "
            "ORDER BY staff_id DESC LIMIT 1",
            (tenant_id, data_iso, staff_id)).fetchone()
        if exc is not None:
            if exc[0]:
                return []
            o, cl = _hhmm_para_min(exc[1]), _hhmm_para_min(exc[2])
            return [(o, cl)] if o is not None and cl is not None and cl > o else []
        row = c.execute(
            "SELECT opens, closes, break_start, break_end FROM business_hours "
            "WHERE tenant_id = ? AND weekday = ? AND (staff_id IS ? OR staff_id IS NULL) "
            "ORDER BY staff_id DESC LIMIT 1",
            (tenant_id, d.weekday(), staff_id)).fetchone()
    if not row or not row[0] or not row[1]:
        return []
    o, cl = _hhmm_para_min(row[0]), _hhmm_para_min(row[1])
    if o is None or cl is None or cl <= o:
        return []
    bs, be = _hhmm_para_min(row[2]), _hhmm_para_min(row[3])
    if bs is not None and be is not None and o < bs < be < cl:
        return [(o, bs), (be, cl)]
    return [(o, cl)]


def dia_aberto(data_iso: str, tenant_id: int = 1, staff_id=None) -> bool:
    return bool(janelas_do_dia(data_iso, tenant_id, staff_id))


def proximos_dias_abertos(n: int = 7, tenant_id: int = 1, staff_id=None,
                          a_partir_de: date | None = None) -> list[str]:
    """Próximos `n` dias (YYYY-MM-DD) ABERTOS, respeitando a política
    (same_day, max_notice_days). Nunca inclui dias fechados/feriados/férias."""
    pol = politica(tenant_id)
    hoje = a_partir_de or tempo.hoje_zurique()
    limite = hoje + timedelta(days=pol["max_notice_days"])
    dias, d = [], hoje
    if not pol["same_day"]:
        d = d + timedelta(days=1)
    while len(dias) < n and d <= limite:
        if dia_aberto(d.isoformat(), tenant_id, staff_id):
            dias.append(d.isoformat())
        d += timedelta(days=1)
    return dias


# ---------------------------------------------------------------------------
# CRUD para o dashboard (página Horários)
# ---------------------------------------------------------------------------
def grelha_semanal(tenant_id: int = 1) -> list[dict]:
    with db.ligacao() as c:
        rows = c.execute(
            "SELECT weekday, opens, closes, break_start, break_end FROM business_hours "
            "WHERE tenant_id = ? AND staff_id IS NULL ORDER BY weekday", (tenant_id,)).fetchall()
    por_dia = {r[0]: r for r in rows}
    nomes = ["Segunda", "Terça", "Quarta", "Quinta", "Sexta", "Sábado", "Domingo"]
    out = []
    for wd in range(7):
        r = por_dia.get(wd)
        out.append({"weekday": wd, "nome": nomes[wd],
                    "opens": r[1] if r else None, "closes": r[2] if r else None,
                    "break_start": r[3] if r else None, "break_end": r[4] if r else None})
    return out


def definir_grelha(tenant_id: int, dias: list[dict]):
    """Substitui a grelha semanal. `dias` = [{weekday, opens, closes,
    break_start?, break_end?}]. opens/closes vazios = dia fechado."""
    with db.ligacao() as c:
        c.execute("DELETE FROM business_hours WHERE tenant_id = ? AND staff_id IS NULL", (tenant_id,))
        for d in dias:
            c.execute(
                "INSERT INTO business_hours (tenant_id, weekday, opens, closes, break_start, break_end) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (tenant_id, int(d["weekday"]), d.get("opens") or None, d.get("closes") or None,
                 d.get("break_start") or None, d.get("break_end") or None))


def listar_excecoes(tenant_id: int = 1, desde: str | None = None) -> list[dict]:
    desde = desde or tempo.hoje_zurique().isoformat()
    with db.ligacao() as c:
        rows = c.execute(
            "SELECT id, date, closed, opens, closes, reason FROM business_hours_exceptions "
            "WHERE tenant_id = ? AND date >= ? ORDER BY date", (tenant_id, desde)).fetchall()
    return [dict(zip(("id", "date", "closed", "opens", "closes", "reason"), r)) for r in rows]


def adicionar_excecao(tenant_id: int, data_inicio: str, data_fim: str | None = None,
                      closed: bool = True, opens: str | None = None, closes: str | None = None,
                      reason: str | None = None) -> list[str]:
    """Cria uma exceção para cada dia do intervalo [data_inicio, data_fim].
    Devolve as datas criadas. (Ver Fase W: detetar marcações afetadas ANTES
    de confirmar — feito na rota do dashboard, não aqui.)"""
    d0 = date.fromisoformat(data_inicio)
    d1 = date.fromisoformat(data_fim) if data_fim else d0
    criadas = []
    with db.ligacao() as c:
        d = d0
        while d <= d1:
            c.execute(
                "INSERT INTO business_hours_exceptions "
                "(tenant_id, date, closed, opens, closes, reason, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (tenant_id, d.isoformat(), 1 if closed else 0, opens, closes, reason, tempo.iso_utc()))
            criadas.append(d.isoformat())
            d += timedelta(days=1)
    return criadas


def remover_excecao(tenant_id: int, excecao_id: int):
    with db.ligacao() as c:
        c.execute("DELETE FROM business_hours_exceptions WHERE tenant_id = ? AND id = ?",
                  (tenant_id, excecao_id))
