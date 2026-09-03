"""
notifications/business.py — notificação PRIVADA para o negócio (a Daniela).

Ponto ÚNICO. Hoje o destinatário é `config.PROVIDER_WHATSAPP` (um número).
Quando `notification_recipients` existir (V2), só esta função muda — os
call sites (bot.py, painel) continuam a chamar `notificar_negocio(...)`.

Fiabilidade: uma falha de envio NUNCA afeta a marcação. Esta função é
chamada a partir de um handler de evento (core.events); se rebentar, o
evento fica por processar e é re-tentado no próximo `drain()`.
"""

from __future__ import annotations

import logging

import config
import catalogo
import db
from messaging import whatsapp

log = logging.getLogger("notif.business")


def destinatarios(event_type: str | None = None, tenant_id: int = 1) -> list[str]:
    """V1: o número em PROVIDER_WHATSAPP, se configurado. V2: consulta
    `notification_recipients` (tenant + event_type + enabled + staff)."""
    return [config.PROVIDER_WHATSAPP] if config.PROVIDER_WHATSAPP else []


def notificar_negocio(texto: str, event_type: str | None = None, tenant_id: int = 1) -> int:
    """Envia `texto` a todos os destinatários internos. Devolve quantos
    envios foram feitos. Sem destinatário configurado, não faz nada."""
    n = 0
    for numero in destinatarios(event_type, tenant_id):
        whatsapp.enviar_texto(numero, texto)
        n += 1
    if not n:
        log.info("notificar_negocio: sem destinatário configurado (evento=%s)", event_type)
    return n


# ---------------------------------------------------------------------------
# Formatação por tipo de evento — consistente entre criação/reagendamento/
# cancelamento. (A criação tem também o formato rico em bot.mensagem_
# notificacao_provider, com a lista de ações da equipa; este é o fallback e
# o formato dos restantes eventos.)
# ---------------------------------------------------------------------------
def _servico_nome(payload: dict) -> str:
    sid = payload.get("servico_id")
    if sid:
        s = db.obter_servico(sid)
        if s:
            return catalogo.nome_pt(s)
    return payload.get("servico") or "Serviço"


def _preco_linha(payload: dict) -> str:
    cents = payload.get("preco_cents")
    return "💰 Preço a confirmar" if cents is None else f"💰 {catalogo.formatar_cents(cents, 'pt')}"


def _duracao_linha(payload: dict) -> str | None:
    m = payload.get("duracao_min")
    if not m:
        sid = payload.get("servico_id")
        s = db.obter_servico(sid) if sid else None
        m = s.get("duracao_min") if s else None
    return f"⏱️ {catalogo.duracao_label(m)}" if m else None


def render_evento(ev: dict) -> str | None:
    t = ev["type"]
    p = ev.get("payload") or {}
    ident = ev.get("entity_id")

    if t in ("booking.created", "booking.pending"):
        # Formato ÚNICO da criação (o envio com a lista de ações da equipa é
        # feito pelo handler de bot.py — ver _notificar_criacao_marcacao).
        aprovar = t == "booking.pending"
        cab = "⏳ *Nova marcação (a APROVAR)" if aprovar else "🔔 *Nova marcação"
        linhas = [f"{cab} · #{ident}*", "",
                  f"👤 {p.get('cliente') or 'Cliente'}",
                  f"✨ {_servico_nome(p)}",
                  f"📅 {p.get('data') or '-'}",
                  f"🕒 {p.get('hora') or '-'}"]
        dur = _duracao_linha(p)
        if dur:
            linhas.append(dur)
        linhas += [_preco_linha(p), "",
                   f"📱 {p.get('telefone') or '-'}",
                   "⏳ A aguardar aprovação" if aprovar else "✅ Confirmada"]
        return "\n".join(linhas)

    if t == "booking.rescheduled":
        return (f"✏️ *Marcação reagendada · #{ident}*\n\n"
                f"👤 {p.get('cliente') or 'Cliente'}\n"
                f"✨ {_servico_nome(p)}\n\n"
                f"Antes:\n📅 {p.get('data_antiga') or '-'}\n🕒 {p.get('hora_antiga') or '-'}\n\n"
                f"Agora:\n📅 {p.get('data_nova') or '-'}\n🕒 {p.get('hora_nova') or '-'}")

    if t == "booking.cancelled":
        libertou = p.get("horario_libertado")
        rodape = ("\n\n✅ O horário voltou a ficar disponível." if libertou
                  else "\n\n🔒 O horário continua reservado." if libertou is False else "")
        return (f"❌ *Marcação cancelada · #{ident}*\n\n"
                f"👤 {p.get('cliente') or 'Cliente'}\n"
                f"✨ {_servico_nome(p)}\n"
                f"📅 {p.get('data') or '-'}\n🕒 {p.get('hora') or '-'}"
                f"{rodape}")

    if t == "booking.confirmed":
        return (f"✅ *Cliente confirmou · #{ident}*\n\n"
                f"👤 {p.get('cliente') or 'Cliente'} — {p.get('data') or ''} {p.get('hora') or ''}")

    if t == "booking.no_show":
        return (f"⚠️ *Não compareceu · #{ident}*\n\n"
                f"👤 {p.get('cliente') or 'Cliente'}\n✨ {_servico_nome(p)}\n"
                f"📅 {p.get('data') or '-'} 🕒 {p.get('hora') or '-'}")

    if t == "customer.created":
        return (f"👤 *Novo cliente*\n\n{p.get('nome') or 'Sem nome'}\n📱 {p.get('telefone') or '-'}")

    if t == "message.needs_human":
        estava = p.get("contexto")
        return ("💬 *Cliente precisa de ajuda*\n\n"
                f"👤 {p.get('nome') or 'Cliente'}"
                + (f"\n🗒️ Estava: {estava}" if estava else "")
                + (f"\n💬 \"{p.get('ultima_mensagem')}\"" if p.get("ultima_mensagem") else ""))

    return None


def handler_evento(ev: dict):
    """Handler registado no barramento (core.events). Formata e envia. A
    criação (`booking.created` / `booking.pending`) é enviada pelo handler
    dedicado de bot.py (com a lista de ações da equipa) — NÃO aqui, para a
    marcação gerar exatamente UMA notificação privada."""
    if ev["type"] in ("booking.created", "booking.pending"):
        return
    texto = render_evento(ev)
    if texto:
        notificar_negocio(texto, event_type=ev["type"], tenant_id=ev.get("tenant_id", 1))
