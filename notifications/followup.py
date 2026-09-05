"""
notifications/followup.py — arquitetura (dados + lógica pura) para o
FOLLOW-UP automático de reativação por WhatsApp.

Exemplo do que isto prepara: "há 21 dias a Marta fez uma Limpeza de pele e
não tem nenhuma marcação futura marcada — vale a pena perguntar se quer
marcar outra vez?". O intervalo (21 dias, neste exemplo) NUNCA é fixo no
código: vem de `servicos.rebook_days`, configurável por serviço.

FASE 1 (esta sessão) — só o que é seguro e pequeno:
  • migração 17 (db.py): servicos.follow_up_enabled + follow_up_template_*,
    agendamentos.follow_up_status/follow_up_sent_at, customers.follow_up_opt_out.
  • candidatos_follow_up(): consulta as marcações CONCLUÍDAS elegíveis,
    aplicando TODAS as condições abaixo — não envia nada, só lê.
  • render_follow_up(): o TEXTO da mensagem (nunca o envio em si).
  • marcar_follow_up_enviado() / marcar_follow_up_recusado(): idempotência —
    no máximo um follow-up automático por marcação concluída.
  • bot.py já sabe responder aos dois botões que uma mensagem de follow-up
    teria de mandar (followup_marcar_<servico_id> / followup_depois_<id>) —
    "Marcar novamente" reaproveita o fluxo normal de marcação (escolher_
    servico -> data -> hora -> resumo -> confirmar): NÃO existe um segundo
    fluxo de marcação.

O QUE FALTA para a PRÓXIMA fase (deliberadamente não construído agora, para
não sair do âmbito desta sessão nem fazer um mega-refactor):
  • Um SCHEDULER/worker periódico que corra candidatos_follow_up() todos os
    dias e chame messaging.whatsapp.enviar + marcar_follow_up_enviado — o
    mesmo padrão já documentado em core/events.py ("V1.5: passa a ser
    chamado por um cron worker"). Hoje NADA corre isto automaticamente.
  • Um ecrã no painel para a Daniela escrever/editar
    follow_up_template_{pt,de,en} por serviço (fora do âmbito: esta sessão
    é só a experiência do cliente no WhatsApp).
  • Um cooldown configurável para tentar de novo depois de um "Agora não"
    (hoje marcar_follow_up_recusado() marca a marcação como "declined" e o
    worker futuro simplesmente nunca mais a reconsidera — idempotência
    simples, sem novo ciclo automático).

Nada aqui contorna as regras de janela/template da Meta: o envio (quando o
worker existir) passa sempre por messaging.whatsapp.enviar — o mesmo ponto
único usado por todo o resto do bot, incluindo a proteção DEMO (um número
`config.DEMO_PHONE_PREFIX` nunca chega à Meta, sem precisar de nenhuma
lógica extra aqui)."""

from __future__ import annotations

import logging
from datetime import date

import config
import catalogo
import db
import estados
import tempo

log = logging.getLogger("notif.followup")


def _telefone_demo(telefone: str) -> bool:
    return bool(telefone) and str(telefone).startswith(config.DEMO_PHONE_PREFIX)


def _tem_marcacao_futura_ativa(conn, telefone: str, tenant_id: int) -> bool:
    """True se o cliente já tem uma marcação confirmada/pendente a partir de
    hoje — nesse caso não faz sentido oferecer-lhe outra."""
    hoje = tempo.hoje_zurique().isoformat()
    linha = conn.execute(
        "SELECT 1 FROM agendamentos WHERE telefone = ? AND tenant_id = ? "
        "AND estado IN (?, ?) AND data_iso >= ? LIMIT 1",
        (telefone, tenant_id, estados.CONFIRMED, estados.PENDING, hoje)).fetchone()
    return bool(linha)


def candidatos_follow_up(tenant_id: int = 1) -> list[dict]:
    """Marcações CONCLUÍDAS elegíveis para um follow-up automático, hoje.

    Condições (todas obrigatórias, ver docstring do módulo):
      • agendamentos.estado == completed, com follow_up_status ainda por
        preencher (nunca se reconsidera a mesma marcação duas vezes);
      • o serviço tem follow_up_enabled E rebook_days configurados — sem os
        dois, esse serviço nunca gera follow-up;
      • já passaram >= rebook_days dias desde a marcação;
      • o cliente não tem nenhuma marcação futura ativa (confirmed/pending);
      • o cliente não está bloqueado nem em opt-out;
      • o número não é DEMO.

    Devolve dados só de LEITURA — quem envia (quando o worker existir) tem
    de chamar marcar_follow_up_enviado() logo a seguir, na mesma passagem,
    para nunca reenviar."""
    hoje = tempo.hoje_zurique()
    candidatos = []
    with db.ligacao() as conn:
        cur = conn.execute(
            "SELECT id, telefone, nome, servico_id, data_iso, hora_hhmm "
            "FROM agendamentos WHERE tenant_id = ? AND estado = ? AND follow_up_status IS NULL "
            "AND data_iso IS NOT NULL AND servico_id IS NOT NULL",
            (tenant_id, estados.COMPLETED))
        colunas = [d[0] for d in cur.description]
        for linha in cur.fetchall():
            ag = dict(zip(colunas, linha))
            if _telefone_demo(ag["telefone"]):
                continue
            servico = db.obter_servico(ag["servico_id"], conn=conn)
            if not servico or not servico.get("follow_up_enabled") or not servico.get("rebook_days"):
                continue
            try:
                concluida_em = date.fromisoformat(ag["data_iso"])
            except ValueError:
                continue
            if (hoje - concluida_em).days < int(servico["rebook_days"]):
                continue
            cliente = conn.execute(
                "SELECT blocked, follow_up_opt_out, name, locale FROM customers "
                "WHERE tenant_id = ? AND phone = ?", (tenant_id, ag["telefone"])).fetchone()
            if cliente and (cliente[0] or cliente[1]):
                continue
            if _tem_marcacao_futura_ativa(conn, ag["telefone"], tenant_id):
                continue
            ag["servico"] = servico
            ag["nome"] = (cliente[2] if cliente else None) or ag.get("nome")
            ag["idioma"] = (cliente[3] if cliente and cliente[3] else None) or "pt"
            candidatos.append(ag)
    return candidatos


def render_follow_up(candidato: dict, idioma: str | None = None) -> str:
    """Texto do follow-up — humano, leve, nunca insistente. Usa o template
    do serviço se a Daniela tiver escrito um; senão um texto genérico."""
    idioma = idioma or candidato.get("idioma") or "pt"
    servico = candidato["servico"]
    nome_servico = catalogo.nome(servico, idioma)
    primeiro_nome = (candidato.get("nome") or "").strip().split(" ")[0] or None

    modelo = servico.get(f"follow_up_template_{idioma}") or servico.get("follow_up_template_pt")
    if modelo:
        return modelo.format(nome=primeiro_nome or "", servico=nome_servico)

    saudacoes = {"pt": f"Olá, {primeiro_nome}." if primeiro_nome else "Olá!",
                 "de": f"Hallo, {primeiro_nome}." if primeiro_nome else "Hallo!",
                 "en": f"Hi {primeiro_nome}," if primeiro_nome else "Hi!"}
    corpos = {
        "pt": f"Já lá vai um tempo desde a sua última {nome_servico}. Quer marcar outra vez?",
        "de": f"Es ist eine Weile her seit Ihrer letzten {nome_servico}. Möchten Sie wieder einen Termin buchen?",
        "en": f"It's been a while since your last {nome_servico}. Would you like to book again?",
    }
    saudacao = saudacoes.get(idioma, saudacoes["pt"])
    corpo = corpos.get(idioma, corpos["pt"])
    return f"{saudacao} {corpo}"


def marcar_follow_up_enviado(agendamento_id: int) -> None:
    """Idempotência: marca esta marcação como já tendo gerado um follow-up
    — candidatos_follow_up() nunca mais a devolve."""
    with db.ligacao() as conn:
        conn.execute(
            "UPDATE agendamentos SET follow_up_status = 'sent', follow_up_sent_at = ? "
            "WHERE id = ? AND follow_up_status IS NULL",
            (tempo.iso_utc(), agendamento_id))


def marcar_follow_up_recusado(agendamento_id: int) -> None:
    """"Agora não" — a marcação fica marcada como recusada; o worker futuro
    nunca mais a reapresenta (sem isto seria fácil voltar a insistir com o
    mesmo cliente no ciclo seguinte)."""
    with db.ligacao() as conn:
        conn.execute(
            "UPDATE agendamentos SET follow_up_status = 'declined', follow_up_sent_at = ? "
            "WHERE id = ? AND follow_up_status IS NULL",
            (tempo.iso_utc(), agendamento_id))
