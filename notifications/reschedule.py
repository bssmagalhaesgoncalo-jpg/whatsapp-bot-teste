"""
notifications/reschedule.py — P4.1: REAGENDAMENTO INICIADO PELO PAINEL, com
CONFIRMAÇÃO DO CLIENTE.

Até aqui (P4), arrastar uma marcação na Agenda (ou usar o diálogo
"Reagendar"/"Editar") movia-a de imediato via `/api/agendamentos/<id>/reagendar`
-> `bot.reagendar_agendamento`. Esse endpoint e essa função continuam EXATAMENTE
iguais (zero regressão nos testes que já os cobrem) — passam a ser o MOTOR de
baixo nível que ESTE fluxo usa por baixo, só quando o cliente confirma.

Fluxo novo:

    drag / diálogo "Reagendar" no painel
        -> POST /api/agendamentos/<id>/reagendar-pedido -> criar_pedido()
           (a marcação ORIGINAL não muda; o horário NOVO fica reservado —
           ver db.ocupacao_do_dia / bot.holds_de_pedidos_reagendamento)
        -> WhatsApp ao cliente: [Confirmar novo horário] [Manter horário atual]
    cliente toca "Confirmar" -> aceitar()
        -> aplica o reagendamento com bot.reagendar_agendamento (motor
           existente, tal e qual — revalida tudo outra vez: estado, op_status,
           expediente, conflitos) -> WhatsApp de confirmação final
    cliente toca "Manter" -> recusar()
        -> a marcação original nunca mexeu; só liberta o horário novo
    Daniela cancela o pedido no painel -> cancelar_pedido_da_marcacao()
        -> idêntico a recusar(), sem mensagem ao cliente (ação dela, não dele)
    a marcação é cancelada por outro caminho enquanto o pedido está pendente
        -> handler_evento (booking.cancelled) cancela o pedido também —
           nunca fica um horário novo reservado para uma marcação morta

Nunca há um segundo booking "a sério": o horário novo é só um HOLD (uma linha
`reschedule_requests` com status='pending') que `db.ocupacao_do_dia` e
`bot.holds_de_pedidos_reagendamento` tratam como ocupação, exatamente como uma
`reservas_temporarias` — reagendar_agendamento() é sempre quem, no fim,
escreve de facto a mudança na ÚNICA linha de `agendamentos` que já existia.

Concorrência / idempotência: cada transição de estado é um
`UPDATE ... WHERE status = 'pending'` dentro de BEGIN IMMEDIATE — um duplicate
click, ou um clique tardio depois de o pedido já ter sido decidido por outro
caminho, nunca reaplica nada (a UPDATE simplesmente não afeta nenhuma linha).
O índice único parcial em `reschedule_requests` garante, ao nível da BD, que
nunca há dois pedidos pendentes para a mesma marcação.

IMPORTANTE (evitar deadlock): nunca abrir uma segunda ligação SQLite escrita
dentro de uma transação BEGIN IMMEDIATE já aberta — `av_mod.slots()` /
`db.ocupacao_do_dia()` fazem um DELETE de limpeza; chamá-los com uma
transação já aberta bloquearia à espera de si própria. Por isso todas as
funções aqui seguem o MESMO padrão em duas fases que `bot.reagendar_agendamento`
já usa: validar o expediente ANTES de abrir a transação, revalidar conflitos
DENTRO dela só com a MESMA conexão (nunca abrir uma segunda)."""

from __future__ import annotations

import logging
from datetime import date

import config
import db
import estados
import tempo
from messaging import whatsapp as wa

log = logging.getLogger("notif.reschedule")

PENDING, ACCEPTED, DECLINED, CANCELLED = "pending", "accepted", "declined", "cancelled"

_CAMPOS = ("id", "tenant_id", "appointment_id", "old_date", "old_time", "new_date",
           "new_time", "status", "origin", "created_at", "responded_at")


class PedidoJaExistente(Exception):
    """Já há um pedido de reagendamento pendente para esta marcação (409 no painel)."""


def _codigo_meta(idioma: str) -> str:
    return {"pt": "pt_PT", "de": "de", "en": "en_US"}.get(idioma, idioma)


def _template_do_idioma(idioma: str) -> str | None:
    return {
        "pt": config.WHATSAPP_RESCHEDULE_TEMPLATE_PT,
        "de": config.WHATSAPP_RESCHEDULE_TEMPLATE_DE,
        "en": config.WHATSAPP_RESCHEDULE_TEMPLATE_EN,
    }.get(idioma) or config.WHATSAPP_RESCHEDULE_TEMPLATE_PT


def _fmt_data(data_iso: str | None) -> str:
    try:
        d = date.fromisoformat(data_iso)
    except (TypeError, ValueError):
        return data_iso or "-"
    return d.strftime("%d/%m/%Y")


def obter_pedido(request_id: int, tenant_id: int = 1) -> dict | None:
    with db.ligacao() as conn:
        row = conn.execute(
            f"SELECT {', '.join(_CAMPOS)} FROM reschedule_requests WHERE id = ? AND tenant_id = ?",
            (request_id, tenant_id)).fetchone()
    return dict(zip(_CAMPOS, row)) if row else None


def pedido_pendente_da_marcacao(appointment_id: int, tenant_id: int = 1) -> dict | None:
    with db.ligacao() as conn:
        row = conn.execute(
            f"SELECT {', '.join(_CAMPOS)} FROM reschedule_requests "
            "WHERE appointment_id = ? AND tenant_id = ? AND status = 'pending'",
            (appointment_id, tenant_id)).fetchone()
    return dict(zip(_CAMPOS, row)) if row else None


def pendentes_por_marcacoes(appointment_ids, tenant_id: int = 1) -> dict:
    """Versão em lote de `pedido_pendente_para_ui`, para anotar uma lista de
    eventos do calendário (ver bot.eventos_calendario) sem N+1 queries — o
    painel só precisa de mostrar "Reagendamento pendente" no cartão, nunca
    de mudar a marcação em si. Devolve {appointment_id: {new_date, new_time}},
    só com as que têm mesmo um pedido 'pending'."""
    ids = sorted({int(i) for i in appointment_ids if i is not None})
    if not ids:
        return {}
    marcadores = ", ".join("?" * len(ids))
    with db.ligacao() as conn:
        rows = conn.execute(
            "SELECT appointment_id, new_date, new_time FROM reschedule_requests "
            f"WHERE tenant_id = ? AND status = 'pending' AND appointment_id IN ({marcadores})",
            (tenant_id, *ids)).fetchall()
    return {r[0]: {"new_date": r[1], "new_time": r[2]} for r in rows}


# ===========================================================================
# CRIAR — dashboard-initiated (drag ou diálogo "Reagendar"/"Editar")
# ===========================================================================
def criar_pedido(appointment_id: int, new_date: str, new_time: str, origin: str = "dashboard",
                 tenant_id: int = 1):
    """Cria um PEDIDO de reagendamento pendente e propõe-o ao cliente por
    WhatsApp — a marcação original NUNCA é tocada aqui (só quando o cliente
    aceitar, ver `aceitar`). Mesma revalidação real de disponibilidade que
    `bot.reagendar_agendamento(validar_expediente=True)` já usa — nunca se
    reimplementam aqui essas regras, só se acrescenta o próprio pedido ao
    conjunto do que conta como ocupado (ver bot.holds_de_pedidos_reagendamento).

    Levanta LookupError, EstadoInvalido, OperacaoEmCurso, HorarioNoPassado,
    HorarioForaDoExpediente, HorarioOcupado ou PedidoJaExistente. Devolve
    (pedido, cliente_notificado)."""
    import bot  # import tardio evita ciclo (mesmo padrão de notifications/reminders.py)
    from scheduling import availability as av_mod

    alvo = bot.obter_agendamento(appointment_id)
    if not alvo:
        raise LookupError("Marcação não encontrada.")
    if bot.chave_estado(alvo.get("estado")) not in estados.GERIVEIS_PELO_CLIENTE:
        raise bot.EstadoInvalido(alvo.get("estado"))
    if (alvo.get("op_status") or "scheduled") in ("arrived", "in_progress", "done"):
        raise bot.OperacaoEmCurso(alvo.get("op_status"))

    novo_inicio = tempo.combinar_local(new_date, new_time)
    if novo_inicio and novo_inicio <= tempo.agora_zurique():
        raise bot.HorarioNoPassado(f"{new_date} {new_time}")

    # Fase 1 (SEM transação aberta) — cabe no expediente real? Mesma lógica
    # de bot.reagendar_agendamento: se a hora pedida não está nos slots livres
    # mas TAMBÉM não há ninguém a ocupá-la, o motivo só pode ser o expediente.
    if alvo.get("servico_id"):
        livres = av_mod.slots(alvo["servico_id"], new_date, telefone=alvo.get("telefone"),
                              ignorar_id=appointment_id, tenant_id=tenant_id)
        if new_time not in livres and not bot.conflitos_de_horario(appointment_id, new_date, new_time):
            raise bot.HorarioForaDoExpediente(f"{new_date} {new_time}")

    # Fase 2 (BEGIN IMMEDIATE) — revalida tudo DENTRO da transação, com a
    # MESMA conexão do início ao fim (nunca abrir uma segunda ligação aqui).
    with db.ligacao() as conn:
        conn.execute("BEGIN IMMEDIATE")
        linha = conn.execute(
            "SELECT estado, op_status, tenant_id FROM agendamentos WHERE id = ?",
            (appointment_id,)).fetchone()
        if not linha or bot.chave_estado(linha[0]) not in estados.GERIVEIS_PELO_CLIENTE:
            raise bot.EstadoInvalido(linha[0] if linha else "inexistente")
        if (linha[1] or "scheduled") in ("arrived", "in_progress", "done"):
            raise bot.OperacaoEmCurso(linha[1])
        tenant_real = linha[2] or 1

        existe = conn.execute(
            "SELECT 1 FROM reschedule_requests WHERE appointment_id = ? AND status = 'pending'",
            (appointment_id,)).fetchone()
        if existe:
            raise PedidoJaExistente(f"appointment {appointment_id}")

        ocup = (bot._agendamentos_da_conexao(conn)
                + bot.horarios_retidos(excluir_telefone=alvo.get("telefone"), conn=conn)
                + bot.holds_de_pedidos_reagendamento(conn=conn, tenant_id=tenant_real))
        if bot.conflitos_no_intervalo(ocup, new_date, new_time, alvo.get("servico"),
                                      alvo.get("duracao"), ignorar_id=appointment_id):
            raise bot.HorarioOcupado(f"{new_date} {new_time}")

        agora = tempo.iso_utc()
        cur = conn.execute(
            "INSERT INTO reschedule_requests (tenant_id, appointment_id, old_date, old_time, "
            "new_date, new_time, status, origin, created_at) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?)",
            (tenant_real, appointment_id, alvo.get("data_iso"), alvo.get("hora_hhmm"),
             new_date, new_time, origin, agora))
        request_id = cur.lastrowid
        db.registar_evento(
            conn, "reschedule.requested", "appointment", appointment_id,
            {"request_id": request_id, "old_date": alvo.get("data_iso"), "old_time": alvo.get("hora_hhmm"),
             "new_date": new_date, "new_time": new_time, "origin": origin},
            dedupe_key=f"reschedule.requested:{request_id}", tenant_id=tenant_real)

    pedido = obter_pedido(request_id, tenant_id=tenant_real)
    notificado = _enviar_proposta_ao_cliente(bot.obter_agendamento(appointment_id), pedido)
    return pedido, notificado


def _enviar_proposta_ao_cliente(ag: dict, pedido: dict) -> bool:
    """Mesma regra de `bot._avisar_cliente_marcacao_reagendada`: a tentativa
    de envio é sempre feita; devolve True só quando podia mesmo ter chegado
    (dentro da janela de 24h) e não rebentou. Fora da janela usa-se sempre o
    TEMPLATE Meta (nunca uma mensagem interativa normal, que a API recusaria)
    — sem o nome do template configurado, esta proposta simplesmente não
    finge um envio (mesma regra do reminder 24h / rebooking followup)."""
    import bot

    telefone = ag["telefone"]
    idioma = bot.idioma_do_cliente(telefone)
    servico_disp = bot.nome_servico_traduzido(ag.get("servico"), idioma)
    data_antiga_disp = _fmt_data(pedido["old_date"])
    data_nova_disp = _fmt_data(pedido["new_date"])
    nome = ag.get("nome") or ""
    dentro_janela = bot.dentro_da_janela_24h(telefone)
    try:
        if dentro_janela:
            bot.enviar_botoes(telefone, bot.t(
                "reagendar_pedido_corpo", idioma, nome=nome, negocio=bot.BUSINESS_NAME,
                servico=servico_disp or "", data_antiga=data_antiga_disp, hora_antiga=pedido["old_time"],
                data_nova=data_nova_disp, hora_nova=pedido["new_time"]), [
                {"id": f"reagendar_pedido_confirmar_{pedido['id']}",
                 "titulo": bot.t("reagendar_pedido_botao_confirmar", idioma)},
                {"id": f"reagendar_pedido_manter_{pedido['id']}",
                 "titulo": bot.t("reagendar_pedido_botao_manter", idioma)},
            ], idioma)
        else:
            template_name = _template_do_idioma(idioma)
            if not template_name:
                raise RuntimeError(
                    f"template WhatsApp da proposta de reagendamento não configurado para '{idioma}' "
                    "(WHATSAPP_RESCHEDULE_TEMPLATE_* em falta — bloqueio externo, não de código)")
            wa.enviar_template(
                telefone, template_name, _codigo_meta(idioma),
                parametros_corpo=[nome, servico_disp or "", data_antiga_disp, pedido["old_time"],
                                  data_nova_disp, pedido["new_time"]],
                botoes_payload=[f"reagendar_pedido_confirmar_{pedido['id']}",
                                f"reagendar_pedido_manter_{pedido['id']}"])
    except Exception:
        log.warning("pedido #%s: falha ao propor o novo horário ao cliente", pedido["id"], exc_info=True)
        return False
    return dentro_janela


# ===========================================================================
# BOTÕES DO CLIENTE — Confirmar novo horário / Manter horário atual
# ===========================================================================
def _appointment_do_pedido_para_cliente(request_id: int, telefone: str, tenant_id: int = 1):
    """Devolve (appointment_id, pedido) só se `telefone` for mesmo o da
    marcação — nunca deixa um cliente agir sobre um pedido de outro número só
    por adivinhar/copiar o id (mesmo padrão de
    notifications.reminders.obter_appointment_do_job)."""
    pedido = obter_pedido(request_id, tenant_id=tenant_id)
    if not pedido:
        return None, None
    with db.ligacao() as conn:
        row = conn.execute("SELECT telefone FROM agendamentos WHERE id = ?",
                           (pedido["appointment_id"],)).fetchone()
    if not row or row[0] != telefone:
        return None, None
    return pedido["appointment_id"], pedido


def aceitar(request_id: int, telefone: str, tenant_id: int = 1) -> dict:
    """Tocar em "Confirmar novo horário" — aplica o reagendamento com o
    MOTOR EXISTENTE (bot.reagendar_agendamento, tal e qual: revalida estado,
    op_status, expediente e conflitos outra vez). Idempotente: um duplicate
    click sobre um pedido já aceite/decidido nunca reaplica nada. Esta função
    já envia a mensagem final ao cliente em qualquer desfecho — quem chama
    (bot.receber_mensagem) só precisa de devolver 200."""
    import bot

    appointment_id, pedido = _appointment_do_pedido_para_cliente(request_id, telefone, tenant_id=tenant_id)
    if appointment_id is None:
        return {"resultado": "invalido"}
    idioma = bot.idioma_do_cliente(telefone)

    # Fase A — "reclamar" o pedido atomicamente (pending -> accepted). Um
    # UPDATE com WHERE status='pending' só afeta uma linha se ainda estiver
    # pendente: duplicate click, ou um clique tardio depois de já ter sido
    # decidido por outro caminho, nunca reaplica nada.
    with db.ligacao() as conn:
        conn.execute("BEGIN IMMEDIATE")
        linha = conn.execute("SELECT status FROM reschedule_requests WHERE id = ? AND tenant_id = ?",
                             (request_id, tenant_id)).fetchone()
        if not linha:
            return {"resultado": "invalido"}
        if linha[0] != PENDING:
            if linha[0] == ACCEPTED:
                ag = bot.obter_agendamento(appointment_id)
                if ag:
                    bot.enviar_texto(telefone, bot.t("reagendar_pedido_aceite_cliente", idioma,
                                                     id=appointment_id, data=ag["data"], hora=ag["hora"]))
                return {"resultado": "ja_aceite"}
            bot.enviar_texto(telefone, bot.t("reagendar_pedido_ja_nao_valido_cliente", idioma))
            return {"resultado": "ja_resolvido", "status": linha[0]}
        conn.execute("UPDATE reschedule_requests SET status = 'accepted', responded_at = ? WHERE id = ?",
                     (tempo.iso_utc(), request_id))

    # Fase B — transação PRÓPRIA e SEQUENCIAL (a de cima já fechou/committou;
    # nunca aninhar isto dentro da transação de cima — ver docstring do
    # módulo). O pedido já saiu de 'pending', por isso já não conta como o
    # seu próprio conflito em holds_de_pedidos_reagendamento.
    try:
        ag, _notificado = bot.reagendar_agendamento(
            appointment_id, pedido["new_date"], pedido["new_time"],
            origem=pedido["origin"], avisar_cliente=False, validar_expediente=True)
    except (bot.EstadoInvalido, bot.OperacaoEmCurso, LookupError,
            bot.HorarioOcupado, bot.HorarioForaDoExpediente, bot.HorarioNoPassado) as e:
        # O pedido já não pode ser aplicado (a marcação mudou de estado
        # entretanto, ou o horário deixou de estar livre/válido). Fica
        # 'cancelled' — nunca de volta a 'pending': o horário proposto já
        # não é garantidamente válido, reabrir o mesmo pedido reintroduzia o
        # mesmo risco. A marcação ORIGINAL nunca foi tocada.
        with db.ligacao() as conn:
            conn.execute("UPDATE reschedule_requests SET status = 'cancelled', responded_at = ? "
                        "WHERE id = ? AND status = 'accepted'", (tempo.iso_utc(), request_id))
            db.registar_evento(conn, "reschedule.accept_failed", "appointment", appointment_id,
                              {"request_id": request_id, "motivo": type(e).__name__},
                              dedupe_key=f"reschedule.accept_failed:{request_id}", tenant_id=tenant_id)
        bot.enviar_texto(telefone, bot.t("reagendar_pedido_falhou_cliente", idioma))
        log.warning("pedido #%s: accept falhou (%s) — marcação #%s mantida no horário original",
                   request_id, e, appointment_id)
        return {"resultado": "falhou"}

    with db.ligacao() as conn:
        db.registar_evento(conn, "reschedule.accepted", "appointment", appointment_id,
                          {"request_id": request_id, "new_date": pedido["new_date"],
                           "new_time": pedido["new_time"], "origin": pedido["origin"]},
                          dedupe_key=f"reschedule.accepted:{request_id}", tenant_id=tenant_id)

    bot.enviar_texto(telefone, bot.t("reagendar_pedido_aceite_cliente", idioma,
                                     id=appointment_id, data=ag["data"], hora=ag["hora"]))
    return {"resultado": "ok", "appointment_id": appointment_id}


def recusar(request_id: int, telefone: str, tenant_id: int = 1) -> dict:
    """Tocar em "Manter horário atual" — a marcação original NUNCA é tocada;
    só liberta o horário novo (sai de 'pending', logo deixa de contar em
    bot.holds_de_pedidos_reagendamento)."""
    import bot

    appointment_id, pedido = _appointment_do_pedido_para_cliente(request_id, telefone, tenant_id=tenant_id)
    if appointment_id is None:
        return {"resultado": "invalido"}
    idioma = bot.idioma_do_cliente(telefone)

    with db.ligacao() as conn:
        conn.execute("BEGIN IMMEDIATE")
        linha = conn.execute("SELECT status FROM reschedule_requests WHERE id = ? AND tenant_id = ?",
                             (request_id, tenant_id)).fetchone()
        status = linha[0] if linha else None
        if status != PENDING:
            if status == DECLINED:
                bot.enviar_texto(telefone, bot.t("reagendar_pedido_recusado_cliente", idioma))
                return {"resultado": "ja_recusado"}
            bot.enviar_texto(telefone, bot.t("reagendar_pedido_ja_nao_valido_cliente", idioma))
            return {"resultado": "ja_resolvido", "status": status}
        conn.execute("UPDATE reschedule_requests SET status = 'declined', responded_at = ? WHERE id = ?",
                     (tempo.iso_utc(), request_id))
        db.registar_evento(conn, "reschedule.declined", "appointment", appointment_id,
                          {"request_id": request_id, "new_date": pedido["new_date"],
                           "new_time": pedido["new_time"]},
                          dedupe_key=f"reschedule.declined:{request_id}", tenant_id=tenant_id)

    bot.enviar_texto(telefone, bot.t("reagendar_pedido_recusado_cliente", idioma))
    return {"resultado": "ok"}


# ===========================================================================
# PAINEL — Daniela cancela o pedido pendente (drawer)
# ===========================================================================
def cancelar_pedido_da_marcacao(appointment_id: int, tenant_id: int = 1) -> dict:
    """Cancela (a partir do painel) o pedido PENDENTE desta marcação — a
    marcação original nunca foi tocada; só liberta o horário novo. Sem
    mensagem ao cliente (é a Daniela a desistir do pedido, não o cliente a
    recusar — ver `recusar` para esse caso). Levanta LookupError se não
    houver nenhum pedido pendente."""
    with db.ligacao() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT id FROM reschedule_requests WHERE appointment_id = ? AND tenant_id = ? AND status = 'pending'",
            (appointment_id, tenant_id)).fetchone()
        if not row:
            raise LookupError("Nenhum pedido de reagendamento pendente para esta marcação.")
        request_id = row[0]
        conn.execute("UPDATE reschedule_requests SET status = 'cancelled', responded_at = ? WHERE id = ?",
                     (tempo.iso_utc(), request_id))
        db.registar_evento(conn, "reschedule.cancelled", "appointment", appointment_id,
                          {"request_id": request_id, "motivo": "dashboard"},
                          dedupe_key=f"reschedule.cancelled:{request_id}", tenant_id=tenant_id)
    return obter_pedido(request_id, tenant_id=tenant_id)


def pedido_pendente_para_ui(appointment_id: int, tenant_id: int = 1) -> dict | None:
    """Pedido PENDENTE desta marcação, resumido para o drawer do painel
    (ver bot.api_agendamento_detalhe) — None quando não há nenhum (o drawer
    simplesmente não mostra a linha, mesmo padrão de
    notifications.reminders.estado_reminder_para_ui)."""
    pedido = pedido_pendente_da_marcacao(appointment_id, tenant_id=tenant_id)
    if not pedido:
        return None
    return {"id": pedido["id"], "new_date": pedido["new_date"], "new_time": pedido["new_time"],
            "created_at": pedido["created_at"]}


# ===========================================================================
# REAÇÃO A booking.cancelled — um pedido pendente nunca sobrevive à marcação
# a que pertence (senão o horário novo ficava reservado para sempre).
# ===========================================================================
def _cancelar_pendentes_por_cancelamento(appointment_id: int, tenant_id: int = 1) -> None:
    with db.ligacao() as conn:
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute(
            "SELECT id FROM reschedule_requests WHERE appointment_id = ? AND tenant_id = ? AND status = 'pending'",
            (appointment_id, tenant_id)).fetchall()
        for (request_id,) in rows:
            conn.execute("UPDATE reschedule_requests SET status = 'cancelled', responded_at = ? WHERE id = ?",
                         (tempo.iso_utc(), request_id))
            db.registar_evento(conn, "reschedule.cancelled", "appointment", appointment_id,
                              {"request_id": request_id, "motivo": "appointment_cancelled"},
                              dedupe_key=f"reschedule.cancelled:{request_id}", tenant_id=tenant_id)


def handler_evento(ev: dict) -> None:
    if ev.get("type") != "booking.cancelled":
        return
    appointment_id = ev.get("entity_id")
    if appointment_id:
        _cancelar_pendentes_por_cancelamento(appointment_id, ev.get("tenant_id") or 1)
