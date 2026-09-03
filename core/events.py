"""
core/events.py — barramento de eventos de domínio (AUTOMATION ENGINE, base).

Fluxo:
    operação de domínio  ->  db.registar_evento(conn, ...)  [MESMA transação]
    commit
    core.events.drain()   ->  para cada evento por processar, chama os handlers
                              registados e marca-o como processado.

V1: `drain()` é chamado de forma SÍNCRONA no fim do request do webhook / da
ação do painel. V1.5: passa a ser chamado por um cron worker (cron.py).
Em qualquer dos casos, a fiabilidade vem da OUTBOX: se o `events` row foi
gravado, o evento não se perde; um handler que rebente não impede os outros
nem perde o evento (fica por processar e é re-tentado no próximo drain).
"""

from __future__ import annotations

import logging

import db

log = logging.getLogger("events")

# tipo de evento -> lista de handlers (callables que recebem o dict do evento)
_HANDLERS: dict[str, list] = {}


def registar(tipo: str, handler):
    _HANDLERS.setdefault(tipo, []).append(handler)


def on(tipo: str):
    def _deco(fn):
        registar(tipo, fn)
        return fn
    return _deco


def _handlers_para(tipo: str) -> list:
    return _HANDLERS.get(tipo, []) + _HANDLERS.get("*", [])


def drain(limite: int = 100) -> int:
    """Processa os eventos pendentes. Devolve quantos foram processados.
    Um evento só é marcado como processado se TODOS os seus handlers
    correrem sem exceção — assim uma falha transitória é re-tentada."""
    processados = 0
    for ev in db.eventos_por_processar(limite):
        ok = True
        for h in _handlers_para(ev["type"]):
            try:
                h(ev)
            except Exception:               # noqa: BLE001 — isolar cada handler
                ok = False
                log.exception("handler de %s falhou (evento #%s)", ev["type"], ev["id"])
        if ok:
            db.marcar_evento_processado(ev["id"])
            processados += 1
    return processados
