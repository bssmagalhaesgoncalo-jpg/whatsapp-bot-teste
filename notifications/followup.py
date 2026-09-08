"""
notifications/followup.py — arquitetura (dados + lógica pura) para o
FOLLOW-UP automático de reativação por WhatsApp.

Exemplo do que isto prepara: "há 21 dias a Marta fez uma Limpeza de pele e
não tem nenhuma marcação futura marcada — vale a pena perguntar se quer
marcar outra vez?". O intervalo (21 dias, neste exemplo) NUNCA é fixo no
código: vem de `servicos.rebook_days`, configurável por serviço.

FASE 1 (dados + lógica pura — arquitetura inicial):
  • migração 17 (db.py): servicos.follow_up_enabled + follow_up_template_*,
    agendamentos.follow_up_status/follow_up_sent_at, customers.follow_up_opt_out.
  • candidatos_follow_up(): consulta (só leitura) as marcações CONCLUÍDAS
    elegíveis pela regra "passaram >= rebook_days dias" — pensado para um
    varrimento periódico manual/futuro; NÃO é o caminho usado pelo P2
    automático (ver FASE 2) — mantido tal como estava, com os seus testes.
  • render_follow_up(): o TEXTO da mensagem (nunca o envio em si).
  • marcar_follow_up_enviado() / marcar_follow_up_recusado(): idempotência —
    no máximo um follow-up automático por marcação concluída.
  • bot.py já sabia responder aos dois botões que uma mensagem de follow-up
    manda (followup_marcar_<servico_id> / followup_depois_<id>) —
    "Marcar novamente" reaproveita o fluxo normal de marcação (escolher_
    servico -> data -> hora -> resumo -> confirmar): NÃO existe um segundo
    fluxo de marcação.

FASE 2 (P2 — rebooking automático, este patch) — liga a FASE 1 ao MESMO
executor genérico do P0/P1 (`automation_jobs` / notifications.jobs), sem
criar um segundo mecanismo:
  • agendar_rebooking_followup(): reage a `booking.completed` (ver bot.py) —
    se o serviço tiver follow_up_enabled + rebook_days válido (>0), cria UM
    job "rebooking_followup" para completed_at + rebook_days dias. Chave
    ESTÁVEL por marcação (`rebooking_followup:<id>`) — processar
    booking.completed outra vez nunca duplica o job.
  • executar_rebooking_followup(): handler do job (chamado por
    jobs.process_due_jobs) — REVALIDA tudo em cima da hora (marcação ainda
    completed? serviço ainda ativo/com follow-up ligado? cliente não
    bloqueado/opt-out? não é DEMO? já tem marcação futura do MESMO serviço?)
    antes de enviar o TEMPLATE Meta com os botões [Marcar novamente]
    [Mais tarde] — exatamente os mesmos followup_marcar_/followup_depois_
    que bot.py já sabia tratar.
  • "Marcar novamente" (bot.py) agora marca a sessão com
    booking_source="rebooking_followup" antes de entrar no fluxo normal —
    a origem COMERCIAL da nova marcação fica distinta do canal (WhatsApp).
  • estado_rebooking_para_ui() / info_rebooking_para_ui(): estado resumido
    para o Client Manager (drawer) — mesmo padrão de
    notifications.reminders.estado_reminder_para_ui. A "próxima manutenção"
    NUNCA é persistida em duplicado: é sempre completed_at + rebook_days,
    calculada na hora.

O QUE CONTINUA de fora (deliberadamente, para não sair do âmbito deste
patch nem fazer um mega-refactor):
  • Um ecrã no painel para a Daniela escrever/editar
    follow_up_template_{pt,de,en} por serviço.
  • Um cooldown configurável para tentar de novo depois de um "Mais tarde"
    (hoje marcar_follow_up_recusado() marca a marcação como "declined" e
    nunca mais a reconsidera — não é opt-out permanente do CLIENTE, é só
    esta marcação: a próxima marcação concluída gera o SEU próprio follow-up
    do zero, com follow_up_status novamente NULL).

Nada aqui contorna as regras de janela/template da Meta: o envio passa
sempre por messaging.whatsapp.enviar_template — o mesmo ponto único usado
por todo o resto do bot (P1 incluído), incluindo a proteção DEMO (um número
`config.DEMO_PHONE_PREFIX` nunca chega à Meta)."""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone

import config
import catalogo
import db
import estados
import tempo
from messaging import whatsapp as wa
from notifications import jobs as notif_jobs

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


def marcar_follow_up_recusado(agendamento_id: int, tenant_id: int = 1) -> None:
    """"Mais tarde" — a marcação fica marcada como recusada; nunca mais se
    reapresenta O MESMO follow-up (sem isto seria fácil voltar a insistir
    com o mesmo cliente no ciclo seguinte). Isto é um SNOOZE por marcação,
    não um opt-out do cliente: a próxima marcação concluída (com o mesmo
    serviço ou outro) gera o SEU follow-up do zero, com follow_up_status de
    novo NULL — "Mais tarde" nunca vira "não contactar outra vez"."""
    with db.ligacao() as conn:
        conn.execute(
            "UPDATE agendamentos SET follow_up_status = 'declined', follow_up_sent_at = ? "
            "WHERE id = ? AND follow_up_status IS NULL",
            (tempo.iso_utc(), agendamento_id))
        db.registar_evento(conn, "rebooking_followup.snoozed", "appointment", agendamento_id, {},
                           dedupe_key=f"rebooking_followup.snoozed:{agendamento_id}", tenant_id=tenant_id)


# ===========================================================================
# FASE 2 (P2) — AGENDAMENTO: reage a booking.completed, cria o job genérico
# ===========================================================================
def _chave(appointment_id: int) -> str:
    return f"rebooking_followup:{appointment_id}"


def _rebook_days_valido(servico: dict | None) -> int | None:
    """>0 é o único valor válido quando o follow-up está ligado. None (não
    configurado) e <= 0 (inválido) nunca geram job — nada é assumido para
    serviços que a Daniela não configurou."""
    if not servico:
        return None
    dias = servico.get("rebook_days")
    if dias is None:
        return None
    try:
        dias = int(dias)
    except (TypeError, ValueError):
        return None
    return dias if dias > 0 else None


def _somar_dias(iso_ts: str | None, dias: int) -> str:
    try:
        dt = datetime.fromisoformat(str(iso_ts).replace("Z", "+00:00"))
    except (ValueError, AttributeError, TypeError):
        dt = datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return tempo.iso_utc(dt + timedelta(days=dias))


def agendar_rebooking_followup(appointment_id: int, tenant_id: int = 1) -> dict | None:
    """Reage a `booking.completed` (ver bot.py) — mesmo padrão de
    notifications.postservice.handler_booking_completed. Sem
    follow_up_enabled OU sem rebook_days válido no serviço, NÃO cria nada.
    Idempotente por marcação (chave ESTÁVEL `rebooking_followup:<id>`) —
    processar booking.completed outra vez nunca duplica o job (usa
    enqueue_job, não reprogramar_ou_criar: ao contrário do reminder 24h,
    este job não muda de `run_at` depois de criado)."""
    with db.ligacao() as c:
        row = c.execute(
            "SELECT estado, servico_id, completed_at, customer_id FROM agendamentos "
            "WHERE id = ? AND tenant_id = ?", (appointment_id, tenant_id)).fetchone()
    if not row:
        return None
    estado, servico_id, completed_at, customer_id = row
    if estados.normalizar(estado) != estados.COMPLETED or not servico_id:
        return None
    servico = db.obter_servico(servico_id)
    if not servico or not servico.get("follow_up_enabled"):
        return None
    dias = _rebook_days_valido(servico)
    if dias is None:
        return None

    run_at = _somar_dias(completed_at, dias)
    job = notif_jobs.enqueue_job(
        notif_jobs.TYPE_REBOOKING_FOLLOWUP, run_at, tenant_id=tenant_id, booking_id=appointment_id,
        customer_id=customer_id, payload={"servico_id": servico_id},
        idempotency_key=_chave(appointment_id))

    with db.ligacao() as c:
        db.registar_evento(c, "rebooking_followup.scheduled", "appointment", appointment_id,
                           {"job_id": job["id"], "run_at": run_at, "rebook_days": dias},
                           dedupe_key=f"rebooking_followup.scheduled:{appointment_id}", tenant_id=tenant_id)
    return job


def handler_evento(ev: dict) -> None:
    if ev.get("type") != "booking.completed":
        return
    appointment_id = ev.get("entity_id")
    if appointment_id:
        agendar_rebooking_followup(appointment_id, ev.get("tenant_id") or 1)


# ===========================================================================
# FASE 2 (P2) — EXECUÇÃO: handler do job "rebooking_followup" (jobs.process_due_jobs)
# ===========================================================================
def _template_do_idioma(idioma: str) -> str | None:
    return {
        "pt": config.WHATSAPP_REBOOKING_TEMPLATE_PT,
        "de": config.WHATSAPP_REBOOKING_TEMPLATE_DE,
        "en": config.WHATSAPP_REBOOKING_TEMPLATE_EN,
    }.get(idioma) or config.WHATSAPP_REBOOKING_TEMPLATE_PT


def _tem_marcacao_futura_do_servico(conn, telefone: str, servico_id: str, tenant_id: int) -> bool:
    """True se o cliente já tem confirmed/pending a partir de hoje PARA O
    MESMO SERVIÇO (só por servico_id — nunca por nome/texto). cancelled,
    completed e no_show NUNCA contam como "marcação futura ativa"."""
    hoje = tempo.hoje_zurique().isoformat()
    linha = conn.execute(
        "SELECT 1 FROM agendamentos WHERE telefone = ? AND tenant_id = ? AND servico_id = ? "
        "AND estado IN (?, ?) AND data_iso >= ? LIMIT 1",
        (telefone, tenant_id, servico_id, estados.CONFIRMED, estados.PENDING, hoje)).fetchone()
    return bool(linha)


def executar_rebooking_followup(job: dict):
    """Devolve notifications.jobs.CANCELLED quando a revalidação chumba
    (nunca 'failed' — não é um erro, é o follow-up já não fazer sentido).
    Uma exceção (ex.: template não configurado) deixa o job pending/failed
    para retry — ver notifications.jobs.process_due_jobs."""
    import bot  # obter_agendamento/nome_servico_traduzido — import tardio evita ciclo

    tenant_id = job.get("tenant_id") or 1
    appointment_id = job.get("booking_id")
    ag = bot.obter_agendamento(appointment_id) if appointment_id else None
    if not ag or (ag.get("tenant_id") or 1) != tenant_id:
        log.info("job #%s: marcação #%s já não existe (ou tenant errado)", job["id"], appointment_id)
        return notif_jobs.CANCELLED
    if estados.normalizar(ag.get("estado")) != estados.COMPLETED:
        log.info("job #%s: marcação #%s deixou de estar completed", job["id"], appointment_id)
        return notif_jobs.CANCELLED
    if ag.get("follow_up_status"):
        log.info("job #%s: marcação #%s já teve follow-up (%s)",
                 job["id"], appointment_id, ag["follow_up_status"])
        return notif_jobs.CANCELLED

    servico_id = ag.get("servico_id")
    servico = db.obter_servico(servico_id) if servico_id else None
    if not servico or not servico.get("ativo") or not servico.get("follow_up_enabled"):
        log.info("job #%s: serviço #%s inativo ou sem follow-up ligado", job["id"], servico_id)
        return notif_jobs.CANCELLED
    if _rebook_days_valido(servico) is None:
        log.info("job #%s: serviço #%s sem rebook_days válido", job["id"], servico_id)
        return notif_jobs.CANCELLED

    customer_id = ag.get("customer_id")
    cust = db.obter_customer(customer_id) if customer_id else None
    if not cust:
        log.info("job #%s: marcação #%s sem cliente associado", job["id"], appointment_id)
        return notif_jobs.CANCELLED
    if cust.get("blocked"):
        log.info("job #%s: cliente #%s bloqueado", job["id"], customer_id)
        return notif_jobs.CANCELLED
    with db.ligacao() as c:
        opt_out = c.execute("SELECT follow_up_opt_out FROM customers WHERE id = ?",
                            (customer_id,)).fetchone()
    if opt_out and opt_out[0]:
        log.info("job #%s: cliente #%s em follow_up_opt_out", job["id"], customer_id)
        return notif_jobs.CANCELLED

    telefone = cust.get("phone") or ag.get("telefone")
    if not telefone:
        log.info("job #%s: sem telefone válido", job["id"])
        return notif_jobs.CANCELLED
    # DEMO: nenhum check explícito aqui — mesmo padrão do P0/P1
    # (messaging.whatsapp.enviar é o ÚNICO ponto que bloqueia um destinatário
    # DEMO de chegar à Meta; o job corre normalmente e fica "done").

    with db.ligacao() as c:
        tem_futura = _tem_marcacao_futura_do_servico(c, telefone, servico_id, tenant_id)
    if tem_futura:
        log.info("job #%s: marcação #%s já tem marcação futura do mesmo serviço", job["id"], appointment_id)
        with db.ligacao() as c:
            db.registar_evento(c, "rebooking_followup.skipped_existing_booking", "appointment",
                               appointment_id, {"job_id": job["id"]},
                               dedupe_key=f"rebooking_followup.skipped_existing_booking:{job['id']}",
                               tenant_id=tenant_id)
        return notif_jobs.CANCELLED

    idioma = cust.get("locale") or "pt"
    template_name = _template_do_idioma(idioma)
    if not template_name:
        raise RuntimeError(f"template WhatsApp do rebooking followup não configurado para '{idioma}' "
                           "(WHATSAPP_REBOOKING_TEMPLATE_* em falta — bloqueio externo, não de código)")

    nome = ag.get("nome") or cust.get("name") or ""
    servico_disp = catalogo.nome(servico, idioma)
    wa.enviar_template(
        telefone, template_name, {"pt": "pt_PT", "de": "de", "en": "en_US"}.get(idioma, idioma),
        parametros_corpo=[nome, servico_disp or ""],
        botoes_payload=[f"followup_marcar_{servico_id}", f"followup_depois_{appointment_id}"])

    marcar_follow_up_enviado(appointment_id)
    with db.ligacao() as c:
        db.registar_evento(c, "rebooking_followup.sent", "appointment", appointment_id,
                           {"job_id": job["id"]}, dedupe_key=f"rebooking_followup.sent:{job['id']}",
                           tenant_id=tenant_id)
    return None


# ===========================================================================
# PAINEL — estado resumido para o Client Manager (ver bot.api_agendamento_detalhe)
# ===========================================================================
def estado_rebooking_para_ui(appointment_id: int, tenant_id: int = 1) -> dict | None:
    """agendado / enviado / mais_tarde / cancelado_marcacao_existente /
    cancelado / falhou. Devolve None quando nunca chegou a existir job
    nenhum (serviço sem follow-up ligado, ou marcação anterior a este
    patch) — o Client Manager simplesmente não mostra a linha."""
    with db.ligacao() as c:
        job_row = c.execute(
            "SELECT id, status, run_at FROM automation_jobs "
            "WHERE tenant_id = ? AND idempotency_key = ?", (tenant_id, _chave(appointment_id))).fetchone()
        ag_row = c.execute("SELECT follow_up_status FROM agendamentos WHERE id = ?",
                           (appointment_id,)).fetchone()
    if not job_row:
        return None
    job_id, status, run_at = job_row
    follow_up_status = ag_row[0] if ag_row else None

    if follow_up_status == "declined":
        estado_ui = "mais_tarde"
    elif status in (notif_jobs.PENDING, notif_jobs.PROCESSING):
        estado_ui = "agendado"
    elif status == notif_jobs.DONE:
        estado_ui = "enviado"
    elif status == notif_jobs.CANCELLED:
        eventos = db.eventos_da_entidade("appointment", appointment_id, tenant_id=tenant_id)
        tem_marcacao = any(e["type"] == "rebooking_followup.skipped_existing_booking" for e in eventos)
        estado_ui = "cancelado_marcacao_existente" if tem_marcacao else "cancelado"
    else:
        estado_ui = "falhou"
    return {"estado": estado_ui, "run_at": run_at, "job_id": job_id}


def info_rebooking_para_ui(appointment_id: int, tenant_id: int = 1) -> dict | None:
    """Bloco "próxima manutenção" do Client Manager: rebook_days + a data
    recomendada (SEMPRE calculada a partir de completed_at + rebook_days,
    nunca persistida em duplicado) + o estado do follow-up. None quando a
    marcação não está completed ou o serviço não tem follow-up configurado."""
    with db.ligacao() as c:
        row = c.execute("SELECT estado, servico_id, completed_at FROM agendamentos "
                        "WHERE id = ? AND tenant_id = ?", (appointment_id, tenant_id)).fetchone()
    if not row:
        return None
    estado, servico_id, completed_at = row
    if estados.normalizar(estado) != estados.COMPLETED or not servico_id:
        return None
    servico = db.obter_servico(servico_id)
    dias = _rebook_days_valido(servico) if servico else None
    if not servico or not servico.get("follow_up_enabled") or dias is None:
        return None

    proxima = None
    if completed_at:
        try:
            base = datetime.fromisoformat(str(completed_at).replace("Z", "+00:00"))
            proxima = (base.date() + timedelta(days=dias)).isoformat()
        except (ValueError, TypeError):
            proxima = None

    estado_ui = estado_rebooking_para_ui(appointment_id, tenant_id=tenant_id)
    return {
        "rebook_days": dias,
        "proxima_manutencao": proxima,
        "estado": (estado_ui or {}).get("estado"),
        "run_at": (estado_ui or {}).get("run_at"),
    }
