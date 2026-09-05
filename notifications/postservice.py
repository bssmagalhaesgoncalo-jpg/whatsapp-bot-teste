"""
notifications/postservice.py — PIPELINE PÓS-ATENDIMENTO (P0).

Fluxo:

    booking.completed (evento de domínio já existente — operations/engine.py)
        -> handler_booking_completed
            -> fatura criada/reutilizada (billing.engine, idempotente)
            -> emitida
            -> paga em Cash
            -> job "post_service" agendado para +5 min (idempotente por
               marcação — ver notifications/jobs.py)
    +5 minutos, quando o executor corre (jobs.process_due_jobs)
        -> executar_post_service
            -> REVALIDA tudo (marcação ainda completed? cliente existe e não
               está bloqueado? telefone válido? fatura ainda emitida/paga?)
            -> agradecimento -> PDF -> pedido de feedback, cada um com o SEU
               próprio marcador — um retry nunca repete um envio que já teve
               sucesso (ver §15 do patch).

Nada disto cria um segundo mecanismo de conclusão: o painel só chama
/api/agendamentos/<id>/op (operations.engine.transicao_operacional), que já
regista o evento `booking.completed` — este módulo só REAGE a esse evento.

Se o preço ficar por confirmar no momento da conclusão, `gerar_fatura_de_
marcacao` levanta PrecoEmFalta e este módulo não cria fatura nem job — a
marcação continua completed na mesma (a faturação nunca bloqueia o
atendimento). Quando a Daniela confirmar o preço mais tarde pelo fluxo já
existente (POST /api/agendamentos/<id>/fatura), esse caminho manual não
reabre sozinho o pipeline automático deste patch — fica para uma iteração
seguinte (ver resposta final, "parcial").

Resposta do cliente ao pedido de feedback: `tentar_guardar_feedback` só
associa o texto quando este telefone tem mesmo um pedido em aberto
(feedback_requested_at preenchido, feedback_text ainda vazio) — nunca
"adivinha" feedback quando não há nenhum pedido pendente."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import config
import db
import estados
import tempo
from billing import engine as bi
from messaging import whatsapp as wa
from notifications import jobs as notif_jobs

log = logging.getLogger("notif.postservice")

_MINUTOS_POS_ATENDIMENTO = 5


def handler_booking_completed(ev: dict) -> None:
    """Regista-se no evento `booking.completed` (eventos.registar, em
    bot.py) — prepara a faturação e agenda o job; NUNCA envia WhatsApp aqui
    dentro (isso só acontece no job, +5 min depois — ver §10 do patch)."""
    if ev.get("type") != "booking.completed":
        return
    appointment_id = ev.get("entity_id")
    tenant_id = ev.get("tenant_id") or 1
    if not appointment_id:
        return

    try:
        inv = bi.gerar_fatura_de_marcacao(appointment_id, tenant_id=tenant_id)
    except bi.PrecoEmFalta:
        log.info("booking.completed #%s sem preço — fatura fica por confirmar manualmente", appointment_id)
        return
    except bi.ErroFaturacao:
        log.exception("gerar_fatura_de_marcacao falhou para a marcação #%s", appointment_id)
        return

    try:
        if inv["status"] == bi.STATUS_RASCUNHO:
            inv = bi.emitir_fatura(inv["id"], tenant_id=tenant_id)
        if inv["status"] == bi.STATUS_EMITIDA:
            inv = bi.marcar_paga(inv["id"], tenant_id=tenant_id, metodo=bi.PAGAMENTO_CASH)
    except bi.ErroFaturacao:
        log.exception("emitir/marcar paga falhou para a fatura #%s (marcação #%s)", inv["id"], appointment_id)
        return

    with db.ligacao() as c:
        row = c.execute("SELECT completed_at, customer_id FROM agendamentos WHERE id = ?",
                        (appointment_id,)).fetchone()
    completed_at = row[0] if row else None
    customer_id = row[1] if row else None
    run_at = _somar_minutos(completed_at or tempo.iso_utc(), _MINUTOS_POS_ATENDIMENTO)

    job = notif_jobs.enqueue_job(
        notif_jobs.TYPE_POST_SERVICE, run_at, tenant_id=tenant_id, booking_id=appointment_id,
        customer_id=customer_id, payload={"invoice_id": inv["id"]},
        idempotency_key=f"post_service:booking:{appointment_id}")

    with db.ligacao() as c:
        db.registar_evento(c, "post_service.scheduled", "appointment", appointment_id,
                           {"job_id": job["id"], "invoice_id": inv["id"], "run_at": run_at},
                           dedupe_key=f"post_service.scheduled:{appointment_id}", tenant_id=tenant_id)


def _somar_minutos(iso_ts: str, minutos: int) -> str:
    try:
        dt = datetime.fromisoformat(str(iso_ts).replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        dt = datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (dt + timedelta(minutes=minutos)).isoformat()


def executar_post_service(job: dict):
    """Handler do job "post_service" (chamado por notifications.jobs.
    process_due_jobs). Devolve notifications.jobs.CANCELLED quando a
    revalidação chumba — nesse caso o job fica 'cancelled', nunca 'failed'
    (não é um erro, é a marcação já não ser elegível)."""
    import bot  # obter_agendamento / t() — import tardio evita ciclo (mesmo padrão de billing/engine.py)

    tenant_id = job.get("tenant_id") or 1
    appointment_id = job.get("booking_id")
    ag = bot.obter_agendamento(appointment_id) if appointment_id else None
    if not ag:
        log.info("job #%s: marcação #%s já não existe", job["id"], appointment_id)
        return notif_jobs.CANCELLED
    if estados.normalizar(ag.get("estado")) != estados.COMPLETED:
        log.info("job #%s: marcação #%s deixou de estar completed", job["id"], appointment_id)
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

    inv = bi.obter_fatura_por_agendamento(appointment_id, tenant_id=tenant_id)
    if not inv or inv["status"] not in (bi.STATUS_EMITIDA, bi.STATUS_PAGA):
        log.info("job #%s: fatura da marcação #%s não está válida para envio (%s)",
                 job["id"], appointment_id, inv["status"] if inv else "inexistente")
        return notif_jobs.CANCELLED

    idioma = cust.get("locale") or "pt"

    # Cada efeito tem o SEU marcador — um retry (ex.: falha de rede a meio)
    # nunca repete um envio que já teve sucesso.
    if not ag.get("post_service_thanks_sent_at"):
        wa.enviar_texto(telefone, bot.t("pos_atendimento_obrigada", idioma))
        with db.ligacao() as c:
            c.execute("UPDATE agendamentos SET post_service_thanks_sent_at = "
                      "COALESCE(post_service_thanks_sent_at, ?) WHERE id = ?",
                      (tempo.iso_utc(), appointment_id))
            db.registar_evento(c, "post_service.sent", "appointment", appointment_id, {},
                               dedupe_key=f"post_service.sent:{appointment_id}", tenant_id=tenant_id)

    if not inv.get("pdf_sent_at"):
        if config.PUBLIC_BASE_URL:
            token = bi.garantir_pdf_token(inv["id"], tenant_id=tenant_id)
            link = f"{config.PUBLIC_BASE_URL}/faturas/pdf/{token}"
            nome_ficheiro = f"fatura-{inv.get('invoice_number') or inv['id']}.pdf"
            wa.enviar_documento(telefone, link, filename=nome_ficheiro,
                                caption=bot.t("pos_atendimento_pdf_legenda", idioma))
            bi.marcar_pdf_enviado(inv["id"], tenant_id=tenant_id)
        else:
            log.warning("PUBLIC_BASE_URL não configurado — PDF da fatura #%s não pôde ser enviado "
                       "(bloqueio de configuração externa, não de código)", inv["id"])

    if not ag.get("feedback_requested_at"):
        wa.enviar_texto(telefone, bot.t("pos_atendimento_feedback_pedido", idioma))
        with db.ligacao() as c:
            c.execute("UPDATE agendamentos SET feedback_requested_at = "
                      "COALESCE(feedback_requested_at, ?) WHERE id = ?",
                      (tempo.iso_utc(), appointment_id))
            db.registar_evento(c, "feedback.requested", "appointment", appointment_id, {},
                               dedupe_key=f"feedback.requested:{appointment_id}", tenant_id=tenant_id)

    return None


def tentar_guardar_feedback(telefone: str, texto: str, tenant_id: int = 1) -> int | None:
    """Se este telefone tem um pedido de feedback em aberto (pedido feito,
    ainda sem resposta), guarda `texto` como feedback dessa marcação e
    devolve o id. Devolve None sem tocar em nada quando não há nenhum pedido
    pendente — nunca associa feedback "a monte" a uma mensagem qualquer."""
    texto = (texto or "").strip()
    if not texto or not telefone:
        return None
    with db.ligacao() as c:
        row = c.execute(
            "SELECT id FROM agendamentos WHERE telefone = ? AND tenant_id = ? "
            "AND feedback_requested_at IS NOT NULL AND feedback_text IS NULL "
            "ORDER BY feedback_requested_at DESC LIMIT 1", (telefone, tenant_id)).fetchone()
        if not row:
            return None
        appointment_id = row[0]
        c.execute("UPDATE agendamentos SET feedback_text = ?, feedback_at = ? WHERE id = ?",
                  (texto, tempo.iso_utc(), appointment_id))
        db.registar_evento(c, "feedback.received", "appointment", appointment_id,
                           {"texto": texto[:300]}, dedupe_key=f"feedback.received:{appointment_id}",
                           tenant_id=tenant_id)
    return appointment_id
