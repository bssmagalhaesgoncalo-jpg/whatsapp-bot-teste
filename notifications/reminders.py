"""
notifications/reminders.py — REMINDER AUTOMÁTICO 24H (P1).

Fluxo:

    booking.created / booking.approved / booking.rescheduled / .cancelled /
    .completed / .no_show (eventos de domínio já existentes — bot.py)
        -> handler_evento
            -> sincronizar_reminder_24h: relê a marcação e garante que o job
               "reminder_24h" reflete o estado ATUAL (elegível + run_at
               certo) — cria, atualiza ou cancela o MESMO job, nunca dois.
    run_at vencido, quando o executor corre (jobs.process_due_jobs)
        -> executar_reminder_24h
            -> REVALIDA tudo (marcação ainda existe/confirmed/no futuro?
               data/hora ainda batem com as do job? cliente não bloqueado?
               telefone válido?) -> envia TEMPLATE Meta (Confirmar/Reagendar/
               Cancelar).

Nada disto cria um segundo scheduler nem uma segunda tabela de jobs: reusa
`automation_jobs`/`notifications.jobs` (P0) e a outbox de eventos já
existente (core/events.py). O botão "Reagendar"/"Cancelar" do reminder
entra no MESMO fluxo do cliente já existente (reagendar_/cancelar_confirmar_
em bot.py) — aqui só se regista a atribuição (`origin=reminder_24h`) antes
de "cair" nesse fluxo; a lógica de reagendar/cancelar em si não é tocada.

Idempotência: chave ESTÁVEL por marcação (`reminder_24h:<id>`) — ao
contrário do padrão `enqueue_job` (P0), aqui um reagendamento ATUALIZA a
mesma linha de `automation_jobs` em vez de criar uma nova (ver
`notifications.jobs.reprogramar_ou_criar`); nunca há dois reminders ativos
para a mesma marcação, mesmo depois de reagendamentos sucessivos.

Templates Meta: se `config.WHATSAPP_REMINDER_TEMPLATE_*` não estiver
configurado para o idioma da cliente, o job NUNCA finge um envio — falha
(fica "failed" ao fim de MAX_TENTATIVAS, visível no Attention Center) até o
template ser aprovado e configurado. Ver resposta final, "bloqueio externo"."""

from __future__ import annotations

import logging
from datetime import timedelta

import config
import db
import estados
import tempo
from messaging import whatsapp as wa
from notifications import jobs as notif_jobs

log = logging.getLogger("notif.reminders")

_HORAS_ANTES = 24

# Código de idioma Meta (BCP-47) por idioma interno — provisório: tem de
# corresponder exatamente ao idioma com que o template foi aprovado.
_CODIGO_META_POR_IDIOMA = {"pt": "pt_PT", "de": "de", "en": "en_US"}

_EVENTOS_RELEVANTES = ("booking.created", "booking.approved", "booking.rescheduled",
                       "booking.cancelled", "booking.completed", "booking.no_show")


def _chave(appointment_id: int) -> str:
    return f"reminder_24h:{appointment_id}"


def _run_at(data_iso: str | None, hora_hhmm: str | None) -> str | None:
    if not data_iso or not hora_hhmm:
        return None
    inicio = tempo.combinar_local(data_iso, hora_hhmm)
    if not inicio:
        return None
    return tempo.iso_utc(inicio - timedelta(hours=_HORAS_ANTES))


def _template_do_idioma(idioma: str) -> str | None:
    return {
        "pt": config.WHATSAPP_REMINDER_TEMPLATE_PT,
        "de": config.WHATSAPP_REMINDER_TEMPLATE_DE,
        "en": config.WHATSAPP_REMINDER_TEMPLATE_EN,
    }.get(idioma) or config.WHATSAPP_REMINDER_TEMPLATE_PT


# ===========================================================================
# AGENDAMENTO — reage a eventos de domínio já existentes, nunca cria um novo
# ===========================================================================
def sincronizar_reminder_24h(appointment_id: int, tenant_id: int = 1) -> dict | None:
    """Garante que o job "reminder_24h" desta marcação reflete o estado
    ATUAL (relido da BD, nunca do payload do evento que disparou isto) —
    chamado a reagir a booking.created/.approved/.rescheduled/.cancelled/
    .completed/.no_show. Idempotente e seguro correr várias vezes seguidas:
    a chave é ESTÁVEL por marcação, nunca duplica nem deixa dois jobs
    ativos."""
    with db.ligacao() as c:
        row = c.execute(
            "SELECT estado, data_iso, hora_hhmm, customer_id FROM agendamentos "
            "WHERE id = ? AND tenant_id = ?", (appointment_id, tenant_id)).fetchone()
    if not row:
        return None
    estado, data_iso, hora_hhmm, customer_id = row
    chave = _chave(appointment_id)

    elegivel = estados.normalizar(estado) == estados.CONFIRMED and bool(data_iso and hora_hhmm)
    run_at = _run_at(data_iso, hora_hhmm) if elegivel else None
    if run_at and run_at <= tempo.iso_utc():
        # §4 do patch — marcação criada/reagendada a MENOS de 24h: nunca se
        # dispara um reminder "imediato" (a confirmação normal já chegou).
        # A string "NAO_APLICAVEL" (maiúsculas, nunca usada nos outros
        # motivos) é o marcador que estado_reminder_para_ui distingue de um
        # cancelamento genuíno — não pode conter as MESMAS palavras que
        # "cancelado" para o UI nunca confundir os dois.
        run_at = None
        motivo = "NAO_APLICAVEL: agendada a menos de 24h de antecedência"
    elif not elegivel:
        motivo = "marcação já não está confirmed"
    else:
        motivo = None

    if not run_at:
        notif_jobs.cancelar_job(chave, tenant_id=tenant_id, motivo=motivo)
        return None

    return notif_jobs.reprogramar_ou_criar(
        notif_jobs.TYPE_REMINDER_24H, run_at, tenant_id=tenant_id, booking_id=appointment_id,
        customer_id=customer_id, payload={"data_iso": data_iso, "hora_hhmm": hora_hhmm},
        idempotency_key=chave)


def handler_evento(ev: dict) -> None:
    if ev.get("type") not in _EVENTOS_RELEVANTES:
        return
    appointment_id = ev.get("entity_id")
    if appointment_id:
        sincronizar_reminder_24h(appointment_id, ev.get("tenant_id") or 1)


# ===========================================================================
# EXECUÇÃO — handler do job "reminder_24h" (chamado por jobs.process_due_jobs)
# ===========================================================================
def executar_reminder_24h(job: dict):
    """Devolve notifications.jobs.CANCELLED quando a revalidação chumba
    (nunca 'failed' — não é um erro, é a marcação já não ser elegível ou ter
    mudado de data/hora entretanto). Uma exceção (ex.: template não
    configurado) deixa o job "pending"/"failed" para retry — ver
    notifications.jobs.process_due_jobs."""
    import bot  # obter_agendamento/nome_servico_traduzido — import tardio evita ciclo

    tenant_id = job.get("tenant_id") or 1
    appointment_id = job.get("booking_id")
    ag = bot.obter_agendamento(appointment_id) if appointment_id else None
    if not ag or (ag.get("tenant_id") or 1) != tenant_id:
        log.info("job #%s: marcação #%s já não existe (ou tenant errado)", job["id"], appointment_id)
        return notif_jobs.CANCELLED
    if estados.normalizar(ag.get("estado")) != estados.CONFIRMED:
        log.info("job #%s: marcação #%s deixou de estar confirmed", job["id"], appointment_id)
        return notif_jobs.CANCELLED

    payload = job.get("payload") or {}
    if ag.get("data_iso") != payload.get("data_iso") or ag.get("hora_hhmm") != payload.get("hora_hhmm"):
        # A marcação mudou de data/hora depois de ESTE job ter sido criado —
        # nunca se envia informação desatualizada. sincronizar_reminder_24h
        # já tratou (ou vai tratar) do job certo para a data/hora nova.
        log.info("job #%s: marcação #%s já não corresponde a este job (data/hora mudou)",
                 job["id"], appointment_id)
        return notif_jobs.CANCELLED

    inicio = tempo.combinar_local(ag.get("data_iso"), ag.get("hora_hhmm"))
    if not inicio or inicio <= tempo.agora_zurique():
        log.info("job #%s: marcação #%s já começou/passou", job["id"], appointment_id)
        return notif_jobs.CANCELLED

    customer_id = ag.get("customer_id")
    cust = db.obter_customer(customer_id) if customer_id else None
    if not cust:
        log.info("job #%s: marcação #%s sem cliente associado", job["id"], appointment_id)
        return notif_jobs.CANCELLED
    if cust.get("blocked"):
        log.info("job #%s: cliente #%s bloqueado", job["id"], customer_id)
        return notif_jobs.CANCELLED

    telefone = cust.get("phone") or ag.get("telefone")
    if not telefone:
        log.info("job #%s: sem telefone válido", job["id"])
        return notif_jobs.CANCELLED

    idioma = cust.get("locale") or "pt"
    template_name = _template_do_idioma(idioma)
    if not template_name:
        raise RuntimeError(f"template WhatsApp do reminder 24h não configurado para '{idioma}' "
                           "(WHATSAPP_REMINDER_TEMPLATE_* em falta — bloqueio externo, não de código)")

    nome = ag.get("nome") or cust.get("name") or ""
    servico_disp = bot.nome_servico_traduzido(ag.get("servico"), idioma)
    wa.enviar_template(
        telefone, template_name, _CODIGO_META_POR_IDIOMA.get(idioma, idioma),
        parametros_corpo=[nome, servico_disp or "", ag.get("data") or "", ag.get("hora") or ""],
        botoes_payload=[f"lembrete_confirmar_{job['id']}", f"lembrete_reagendar_{job['id']}",
                        f"lembrete_cancelar_{job['id']}"])

    with db.ligacao() as c:
        db.registar_evento(c, "reminder.sent", "appointment", appointment_id, {"job_id": job["id"]},
                           dedupe_key=f"reminder.sent:{job['id']}:{job['run_at']}", tenant_id=tenant_id)
    return None


# ===========================================================================
# BOTÕES DO CLIENTE — Confirmar / Reagendar / Cancelar (ver bot.receber_mensagem)
# ===========================================================================
def obter_appointment_do_job(job_id: int, telefone: str, tenant_id: int = 1) -> int | None:
    """Devolve o appointment_id do job "reminder_24h", só se `telefone` for
    mesmo o da marcação — nunca deixa um cliente agir sobre um job de outro
    número só por adivinhar/copiar o id."""
    job = notif_jobs.obter_job(job_id, tenant_id=tenant_id)
    if not job or job.get("type") != notif_jobs.TYPE_REMINDER_24H or not job.get("booking_id"):
        return None
    with db.ligacao() as c:
        row = c.execute("SELECT telefone FROM agendamentos WHERE id = ?", (job["booking_id"],)).fetchone()
    if not row or row[0] != telefone:
        return None
    return job["booking_id"]


def registar_confirmacao(job_id: int, telefone: str, tenant_id: int = 1) -> bool:
    """"Confirmar" no reminder NUNCA reexecuta a transição comercial
    confirmed->confirmed — só regista o facto "a cliente confirmou a
    presença via reminder" (evento reutilizado: booking.confirmed, com
    origin=reminder_24h). Idempotente por job+run_at (dedupe_key) — o
    run_at, não só o job_id, entra na chave porque a MESMA linha de
    automation_jobs é reaproveitada em cada reagendamento (chave estável,
    ver reprogramar_ou_criar): sem o run_at, confirmar duas vezes em dois
    ciclos de reminder diferentes (com um reagendamento pelo meio) só
    registaria a primeira."""
    appointment_id = obter_appointment_do_job(job_id, telefone, tenant_id=tenant_id)
    if not appointment_id:
        return False
    job = notif_jobs.obter_job(job_id, tenant_id=tenant_id)
    with db.ligacao() as c:
        db.registar_evento(c, "booking.confirmed", "appointment", appointment_id,
                           {"automation_job_id": job_id, "origin": "reminder_24h",
                            "job_run_at": job.get("run_at")},
                           dedupe_key=f"booking.confirmed:{job_id}:{job.get('run_at')}",
                           tenant_id=tenant_id)
    return True


def registar_reschedule_iniciado(job_id: int, telefone: str, tenant_id: int = 1) -> int | None:
    """Chamado ao TOCAR em "Reagendar" no reminder — antes de entrar no
    fluxo de reagendamento já existente. Devolve o appointment_id (para
    bot.py continuar no mesmo caminho de sempre) ou None se o job já não for
    válido para este telefone."""
    appointment_id = obter_appointment_do_job(job_id, telefone, tenant_id=tenant_id)
    if not appointment_id:
        return None
    job = notif_jobs.obter_job(job_id, tenant_id=tenant_id)
    with db.ligacao() as c:
        db.registar_evento(c, "reminder.reschedule_started", "appointment", appointment_id,
                           {"automation_job_id": job_id},
                           dedupe_key=f"reminder.reschedule_started:{job_id}:{job.get('run_at')}",
                           tenant_id=tenant_id)
    return appointment_id


def registar_cancelamento(job_id: int, appointment_id: int, tenant_id: int = 1) -> None:
    """Chamado DEPOIS de a marcação já ter sido cancelada de facto pelo
    fluxo existente (marcar_agendamento_cancelado, com a SUA própria
    confirmação — nunca ao primeiro toque) — isto só regista a atribuição
    "veio do reminder 24h", nunca decide se se cancela."""
    with db.ligacao() as c:
        db.registar_evento(c, "reminder.cancelled", "appointment", appointment_id,
                           {"automation_job_id": job_id}, dedupe_key=f"reminder.cancelled:{job_id}",
                           tenant_id=tenant_id)


# ===========================================================================
# PAINEL — estado resumido para o drawer da marcação (ver bot.api_agendamento_detalhe)
# ===========================================================================
def estado_reminder_para_ui(appointment_id: int, tenant_id: int = 1) -> dict | None:
    """Um dos 5 conceitos do §15 do patch: agendado / enviado / confirmado /
    nao_aplicavel / cancelado (falhas ficam "falhou"). Devolve None quando
    nunca chegou a existir nenhum job — o drawer simplesmente não mostra a
    linha (nunca sobrecarrega a UI com "N/A")."""
    with db.ligacao() as c:
        row = c.execute(
            "SELECT id, status, run_at, last_error FROM automation_jobs "
            "WHERE tenant_id = ? AND idempotency_key = ?", (tenant_id, _chave(appointment_id))).fetchone()
    if not row:
        return None
    job_id, status, run_at, last_error = row
    if status in (notif_jobs.PENDING, notif_jobs.PROCESSING):
        estado_ui = "agendado"
    elif status == notif_jobs.DONE:
        eventos = db.eventos_da_entidade("appointment", appointment_id, tenant_id=tenant_id)
        # payload leva job_run_at (ver registar_confirmacao) — exige-se a
        # MESMA correspondência, para um "Confirmar" de um ciclo anterior
        # (antes de um reagendamento ter reaproveitado esta linha) nunca
        # aparecer como confirmação do ciclo ATUAL.
        confirmado = any(
            e["type"] == "booking.confirmed" and (e.get("payload") or {}).get("automation_job_id") == job_id
            and (e.get("payload") or {}).get("job_run_at") == run_at
            for e in eventos)
        estado_ui = "confirmado" if confirmado else "enviado"
    elif status == notif_jobs.CANCELLED:
        estado_ui = "nao_aplicavel" if (last_error or "").startswith("NAO_APLICAVEL") else "cancelado"
    else:
        estado_ui = "falhou"
    return {"estado": estado_ui, "run_at": run_at, "job_id": job_id}
