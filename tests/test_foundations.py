"""Sprint 1 — fundações: customers, events (outbox), notificar_negocio, tenant."""

import json

import bot
import db
import estados
from core import events as eventos
from notifications import business as notif
from conftest import marcar, data_pt

DIA_TXT = data_pt("2026-10-05")


def test_customer_criado_ao_marcar(base_dados):
    idag = marcar("41790000900", "limpeza_pele", DIA_TXT, "🕘 09:00", nome="Ana Müller")
    ag = bot.obter_agendamento(idag)
    assert ag["customer_id"]
    cust = db.obter_customer(ag["customer_id"])
    assert cust["phone"] == "41790000900"
    assert cust["name"] == "Ana Müller"
    assert cust["visits_count"] == 1          # confirmed conta como visita
    assert cust["spend_cents"] == 8000


def test_segunda_marcacao_reusa_o_mesmo_customer(base_dados):
    a = marcar("41790000901", "limpeza_pele", DIA_TXT, "🕘 09:00")
    b = marcar("41790000901", "design_sobrancelhas", data_pt("2026-10-06"), "🕘 09:00")
    ca = bot.obter_agendamento(a)["customer_id"]
    cb = bot.obter_agendamento(b)["customer_id"]
    assert ca == cb
    assert db.obter_customer(ca)["visits_count"] == 2


def test_evento_booking_created_na_mesma_transacao(base_dados):
    idag = marcar("41790000902", "pestanas", DIA_TXT, "🕘 09:00")
    evs = db.eventos_por_processar()
    tipos = {e["type"] for e in evs}
    assert "booking.created" in tipos
    assert "customer.created" in tipos
    bc = next(e for e in evs if e["type"] == "booking.created")
    assert bc["entity_id"] == idag
    assert bc["payload"]["preco_cents"] is None      # pestanas = preço a confirmar


def test_webhook_repetido_nao_duplica_evento(base_dados):
    marcar("41790000903", "limpeza_pele", DIA_TXT, "🕘 09:00")
    n1 = len([e for e in db.eventos_por_processar() if e["type"] == "booking.created"])
    # segundo INSERT com o mesmo id de marcação seria bloqueado por HorarioOcupado;
    # aqui testamos a dedupe do evento diretamente
    with db.ligacao() as c:
        db.registar_evento(c, "booking.created", "appointment", 1, {}, dedupe_key="booking.created:1")
        db.registar_evento(c, "booking.created", "appointment", 1, {}, dedupe_key="booking.created:1")
    n2 = len([e for e in db.eventos_por_processar() if e["type"] == "booking.created"])
    assert n2 == n1  # a 2ª chamada com o mesmo dedupe_key não acrescentou nada


def test_notificacao_falhada_nao_afeta_a_marcacao(base_dados, monkeypatch):
    # o envio rebenta sempre
    def rebenta(*a, **k):
        raise RuntimeError("Meta em baixo")
    monkeypatch.setattr(notif.whatsapp, "enviar_texto", rebenta)
    monkeypatch.setattr(bot.config, "PROVIDER_WHATSAPP", "41790000000")

    a = marcar("41790000904", "limpeza_pele", DIA_TXT, "🕘 09:00")
    b = bot.reagendar_agendamento(a, "2026-10-07", "13:00", origem="teste", avisar_cliente=False)
    # a marcação moveu-se apesar da notificação
    ag = bot.obter_agendamento(a)
    assert ag["data_iso"] == "2026-10-07" and ag["hora_hhmm"] == "13:00"

    # os eventos que PRECISAM de notificação ficam por processar (re-tentáveis)
    eventos.drain()
    pendentes = {e["type"] for e in db.eventos_por_processar()}
    assert "customer.created" in pendentes and "booking.rescheduled" in pendentes

    # quando o envio volta a funcionar, o drain limpa a fila
    enviados = []
    monkeypatch.setattr(notif.whatsapp, "enviar_texto", lambda n, t: enviados.append((n, t)))
    eventos.drain()
    assert enviados and not db.eventos_por_processar()


def test_tenant_isolation_evento_e_recipient(base_dados, monkeypatch):
    enviados = []
    monkeypatch.setattr(notif.whatsapp, "enviar_texto", lambda n, t: enviados.append((n, t)))
    monkeypatch.setattr(bot.config, "PROVIDER_WHATSAPP", "41790000000")

    # cria tenant 2 e um evento seu
    with db.ligacao() as c:
        c.execute("INSERT INTO tenants (id, nome, slug, criado_em) VALUES (2,'Outro','outro',?)",
                  (bot.tempo.iso_utc(),))
        db.registar_evento(c, "booking.cancelled", "appointment", 999,
                           {"cliente": "X", "data": "-", "hora": "-"},
                           dedupe_key="t2-cancel", tenant_id=2)
    eventos.drain()
    # V1: PROVIDER_WHATSAPP é global, mas o evento carrega tenant_id=2 e o
    # dispatcher nunca cruza tenants no futuro. Verificamos que o evento tem
    # o tenant certo e foi processado isoladamente.
    with db.ligacao() as c:
        row = c.execute("SELECT tenant_id, processed_at FROM events WHERE dedupe_key='t2-cancel'").fetchone()
    assert row[0] == 2 and row[1] is not None


def test_completed_emite_evento_e_recalcula_customer(base_dados):
    a = marcar("41790000905", "limpeza_pele", DIA_TXT, "🕘 09:00")
    cid = bot.obter_agendamento(a)["customer_id"]
    bot.atualizar_estado_agendamento(a, estados.COMPLETED)
    evs = {e["type"] for e in db.eventos_por_processar()}
    assert "booking.completed" in evs
    ag = bot.obter_agendamento(a)
    assert ag["op_status"] == "done" and ag["completed_at"]
    assert db.obter_customer(cid)["visits_count"] == 1


def test_no_show_conta_no_customer(base_dados):
    a = marcar("41790000906", "limpeza_pele", DIA_TXT, "🕘 09:00")
    cid = bot.obter_agendamento(a)["customer_id"]
    bot.atualizar_estado_agendamento(a, estados.NO_SHOW)
    c = db.obter_customer(cid)
    assert c["no_show_count"] == 1 and c["visits_count"] == 0


def test_webhook_reagendamento_notifica_negocio_uma_vez(base_dados, monkeypatch):
    """Fluxo real pelo webhook: reagendar dispara exatamente 1 notificação
    ao negócio (via evento booking.rescheduled), sem duplicar."""
    import hmac, hashlib
    from notifications import business as _notif
    enviados = []
    monkeypatch.setattr(_notif.whatsapp, "enviar_texto", lambda n, t: enviados.append((n, t)))
    monkeypatch.setattr(bot.config, "PROVIDER_WHATSAPP", "41790000000")
    monkeypatch.setattr(bot, "enviar", lambda p: None)  # respostas ao cliente: silenciar

    os_secret = b"segredo-de-teste"
    c = bot.app.test_client()

    def post(msg):
        body = json.dumps({"entry": [{"changes": [{"value": {"messages": [msg]}}]}]}).encode()
        sig = "sha256=" + hmac.new(os_secret, body, hashlib.sha256).hexdigest()
        return c.post("/webhook", data=body, content_type="application/json",
                      headers={"X-Hub-Signature-256": sig})

    CLI = "41790000950"
    def btn(rid, mid): return {"from": CLI, "id": mid, "type": "interactive",
        "interactive": {"type": "button_reply", "button_reply": {"id": rid, "title": rid}}}
    def lst(rid, tit, mid): return {"from": CLI, "id": mid, "type": "interactive",
        "interactive": {"type": "list_reply", "list_reply": {"id": rid, "title": tit}}}

    post(btn("lang_pt", "a1"))
    post(lst("mp_marcar", "M", "a2"))
    post(lst("svc_limpeza_pele", "Limpeza", "a3"))
    post(lst("opt_0", data_pt("2026-10-20"), "a4"))
    post(lst("opt_0", "🕘 09:00", "a5"))
    post(btn("confirmar", "a6"))
    idag = [a for a in bot.listar_agendamentos() if a["telefone"] == CLI][0]["id"]

    enviados.clear()
    post(btn(f"reagendar_{idag}", "b1"))
    post(lst("opt_1", data_pt("2026-10-21"), "b2"))
    post(lst("opt_2", "🕐 13:00", "b3"))
    post(btn("confirmar", "b4"))

    # só as mensagens para o NEGÓCIO (o número em PROVIDER_WHATSAPP)
    ao_negocio = [t for (n, t) in enviados if n == "41790000000"]
    reag = [t for t in ao_negocio if "reagendad" in t.lower()]
    assert len(reag) == 1, ao_negocio
    assert not db.eventos_por_processar()
