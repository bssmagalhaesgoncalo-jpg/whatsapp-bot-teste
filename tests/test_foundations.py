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
    # #3: uma marcação FUTURA confirmada não é visita realizada nem gasto
    assert cust["visits_count"] == 0
    assert cust["spend_cents"] == 0
    assert cust["next_visit"] == "2026-10-05"   # é a próxima marcação ativa


def test_segunda_marcacao_reusa_o_mesmo_customer(base_dados):
    a = marcar("41790000901", "limpeza_pele", DIA_TXT, "🕘 09:00")
    b = marcar("41790000901", "design_sobrancelhas", data_pt("2026-10-06"), "🕘 09:00")
    ca = bot.obter_agendamento(a)["customer_id"]
    cb = bot.obter_agendamento(b)["customer_id"]
    assert ca == cb
    # duas marcações futuras -> 0 visitas realizadas, next_visit = a mais próxima
    cust = db.obter_customer(ca)
    assert cust["visits_count"] == 0
    assert cust["next_visit"] == "2026-10-05"


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


# ==== #2 idempotência do webhook: claimed / processed / failed ====
def _wh_post(client, msg, secret=b"segredo-de-teste"):
    import hmac, hashlib
    body = json.dumps({"entry": [{"changes": [{"value": {"messages": [msg]}}]}]}).encode()
    sig = "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()
    return client.post("/webhook", data=body, content_type="application/json",
                       headers={"X-Hub-Signature-256": sig})


def test_idempotencia_retry_apos_falha_nao_e_perdido(cliente_http, base_dados, monkeypatch):
    import pytest
    monkeypatch.setattr(bot, "enviar", lambda p: None)
    CLI = "41790030001"
    msg = {"from": CLI, "id": "wamid.FAIL1", "type": "text", "text": {"body": "olá"}}

    # 1) primeiro processamento REBENTA depois do claim.
    # (o test client re-levanta a exceção; em produção o gunicorn devolve 500
    #  e a Meta reenvia — o que interessa é o estado deixado na BD.)
    boom = {"n": 0}
    orig = bot.enviar_seletor_idioma
    def rebenta(de):
        boom["n"] += 1
        raise RuntimeError("erro simulado depois do claim")
    monkeypatch.setattr(bot, "enviar_seletor_idioma", rebenta)

    with pytest.raises(RuntimeError):
        _wh_post(cliente_http, msg)
    assert boom["n"] == 1
    with db.ligacao() as c:
        st = c.execute("SELECT status FROM mensagens_processadas WHERE id='wamid.FAIL1'").fetchone()
    assert st and st[0] == "failed"          # marcado como falhado, NÃO como processado

    # 2) a Meta reenvia — o retry NÃO é descartado
    monkeypatch.setattr(bot, "enviar_seletor_idioma", orig)
    r = _wh_post(cliente_http, msg)
    assert r.status_code == 200
    assert json.loads(r.data)["status"] != "repetida"    # foi mesmo reprocessado
    with db.ligacao() as c:
        st = c.execute("SELECT status FROM mensagens_processadas WHERE id='wamid.FAIL1'").fetchone()
    assert st[0] == "processed"

    # 3) agora sim, um retry é descartado (não duplica)
    r = _wh_post(cliente_http, msg)
    assert json.loads(r.data)["status"] == "repetida"


def test_idempotencia_dois_webhooks_concorrentes_um_so_processa(cliente_http, base_dados, monkeypatch):
    monkeypatch.setattr(bot, "enviar", lambda p: None)
    CLI = "41790030002"
    # simula: wamid já 'claimed' agora mesmo por outro webhook
    with db.ligacao() as c:
        c.execute("INSERT INTO mensagens_processadas (id, recebida_em, status, tenant_id) "
                  "VALUES ('wamid.CONC', ?, 'claimed', 1)", (bot.tempo.iso_utc(),))
    msg = {"from": CLI, "id": "wamid.CONC", "type": "text", "text": {"body": "olá"}}
    r = _wh_post(cliente_http, msg)
    assert json.loads(r.data)["status"] == "repetida"    # o outro webhook está a tratar


def test_idempotencia_claim_preso_e_reclamado(cliente_http, base_dados, monkeypatch):
    monkeypatch.setattr(bot, "enviar", lambda p: None)
    from datetime import timedelta
    velho = bot.tempo.iso_utc(bot.tempo.agora_utc() - timedelta(minutes=10))
    with db.ligacao() as c:
        c.execute("INSERT INTO mensagens_processadas (id, recebida_em, status, tenant_id) "
                  "VALUES ('wamid.STUCK', ?, 'claimed', 1)", (velho,))
    msg = {"from": "41790030003", "id": "wamid.STUCK", "type": "text", "text": {"body": "olá"}}
    r = _wh_post(cliente_http, msg)
    assert json.loads(r.data)["status"] != "repetida"    # claim preso -> reprocessa


# ==== #3 CRM counters: só 'completed' conta como visita/gasto ====
def _novo_customer(phone="41790040001", name="Mix"):
    return db.obter_ou_criar_customer(phone, name)["id"]


def _ins_ag(cid, estado, data_iso, preco_cents=8000, hora="09:00"):
    with db.ligacao() as c:
        c.execute(
            "INSERT INTO agendamentos (telefone, nome, servico, data, hora, data_iso, "
            "hora_hhmm, estado, preco_cents, customer_id, criado_em) "
            "VALUES ('41790040001','Mix','Limpeza',?,?,?,?,?,?,?,?)",
            (data_pt(data_iso), hora, data_iso, hora, estado, preco_cents, cid, bot.tempo.iso_utc()))


def test_recalcular_customer_so_completed_conta(base_dados):
    cid = _novo_customer()
    _ins_ag(cid, estados.COMPLETED, "2026-01-10", preco_cents=5000)   # visita realizada
    _ins_ag(cid, estados.COMPLETED, "2026-02-15", preco_cents=7000)   # visita realizada
    _ins_ag(cid, estados.CONFIRMED, "2099-12-31", preco_cents=9000)   # futura -> NÃO conta
    _ins_ag(cid, estados.NO_SHOW,   "2026-01-20")
    _ins_ag(cid, estados.CANCELLED, "2026-01-25")

    db.recalcular_customer(cid)
    cust = db.obter_customer(cid)
    assert cust["visits_count"] == 2
    assert cust["spend_cents"] == 12000            # 5000 + 7000, sem os 9000 futuros
    assert cust["last_visit"] == "2026-02-15"
    assert cust["next_visit"] == "2099-12-31"      # confirmed futura
    assert cust["no_show_count"] == 1
    assert cust["cancel_count"] == 1


def test_next_visit_usa_data_local_nao_utc(base_dados, monkeypatch):
    """Uma marcação de hoje (data local Zurique) continua a ser 'próxima'
    mesmo que em UTC já seja outro dia."""
    cid = _novo_customer("41790040002")
    hoje_local = bot.tempo.hoje_zurique().isoformat()
    _ins_ag(cid, estados.CONFIRMED, hoje_local, hora="23:30")
    db.recalcular_customer(cid)
    assert db.obter_customer(cid)["next_visit"] == hoje_local


# ==== #4 backfill migration: recalcula customers já existentes ====
def test_migracao_14_corrige_contadores_antigos(base_dados):
    cid = _novo_customer("41790040003")
    _ins_ag(cid, estados.COMPLETED, "2026-01-10", preco_cents=5000)
    _ins_ag(cid, estados.CONFIRMED, "2099-11-11", preco_cents=9000)
    # simula o estado deixado pela migração 9 (semântica antiga: confirmed
    # contava como visita e gasto)
    with db.ligacao() as c:
        c.execute("UPDATE customers SET visits_count = 2, spend_cents = 14000, "
                  "last_visit = '2099-11-11', next_visit = NULL WHERE id = ?", (cid,))
        db._m14_recalcular_customers(c)
    cust = db.obter_customer(cid)
    assert cust["visits_count"] == 1
    assert cust["spend_cents"] == 5000
    assert cust["last_visit"] == "2026-01-10"
    assert cust["next_visit"] == "2099-11-11"


# ==== #6 booking.created: UMA marcação = UMA notificação privada ====
def test_uma_marcacao_gera_exatamente_uma_notificacao_ao_negocio(base_dados, monkeypatch):
    NEG = "41790000000"
    monkeypatch.setattr(bot.config, "PROVIDER_WHATSAPP", NEG)
    monkeypatch.setattr(bot, "PROVIDER_WHATSAPP", NEG)
    saida = []
    monkeypatch.setattr(bot._wa, "enviar", lambda payload: saida.append(payload) or None)

    import hmac, hashlib
    c = bot.app.test_client()

    def post(msg):
        body = json.dumps({"entry": [{"changes": [{"value": {"messages": [msg]}}]}]}).encode()
        sig = "sha256=" + hmac.new(b"segredo-de-teste", body, hashlib.sha256).hexdigest()
        return c.post("/webhook", data=body, content_type="application/json",
                      headers={"X-Hub-Signature-256": sig})

    CLI = "41790000951"
    def btn(rid, mid): return {"from": CLI, "id": mid, "type": "interactive",
        "interactive": {"type": "button_reply", "button_reply": {"id": rid, "title": rid}}}
    def lst(rid, tit, mid): return {"from": CLI, "id": mid, "type": "interactive",
        "interactive": {"type": "list_reply", "list_reply": {"id": rid, "title": tit}}}

    post(btn("lang_pt", "c1"))
    post(lst("mp_marcar", "M", "c2"))
    post(lst("svc_limpeza_pele", "Limpeza", "c3"))
    post(lst("opt_0", data_pt("2026-10-22"), "c4"))
    post(lst("opt_0", "🕘 09:00", "c5"))
    saida.clear()
    post(btn("confirmar", "c6"))

    # mensagens dirigidas ao NEGÓCIO que anunciam a marcação nova
    ao_negocio = [p for p in saida if p.get("to") == NEG]
    criacao = [p for p in ao_negocio
               if "Nova marcação" in json.dumps(p, ensure_ascii=False)]
    assert len(criacao) == 1, ao_negocio
    assert not db.eventos_por_processar()


# ==== #7 reschedule dedupe: A -> B -> A gera DOIS eventos distintos ====
def test_reagendar_ida_e_volta_gera_dois_eventos(base_dados):
    a = marcar("41790050001", "limpeza_pele", DIA_TXT, "🕘 09:00")

    def keys_rescheduled():
        with db.ligacao() as c:
            return [r[0] for r in c.execute(
                "SELECT dedupe_key FROM events WHERE type='booking.rescheduled' "
                "AND entity_id=? ORDER BY id", (a,)).fetchall()]

    bot.reagendar_agendamento(a, "2026-10-06", "13:00", origem="teste", avisar_cliente=False)
    bot.reagendar_agendamento(a, "2026-10-05", "09:00", origem="teste", avisar_cliente=False)  # volta a A

    ks = keys_rescheduled()
    assert len(ks) == 2 and len(set(ks)) == 2, ks   # dois movimentos, duas chaves


# ==== #5 tenant foundation: identidade = (tenant_id, chave) ====
def test_pk_composta_permite_mesmo_telefone_em_tenants_diferentes(base_dados):
    with db.ligacao() as c:
        c.execute("INSERT INTO tenants (id, nome, slug, criado_em) VALUES (2,'B','b',?)",
                  (bot.tempo.iso_utc(),))
        c.execute("INSERT INTO sessoes (tenant_id, telefone, dados) VALUES (1, '41790099999', '{\"a\":1}')")
        c.execute("INSERT INTO sessoes (tenant_id, telefone, dados) VALUES (2, '41790099999', '{\"a\":2}')")
        c.execute("INSERT INTO configuracoes (tenant_id, chave, valor, atualizado_em) "
                  "VALUES (1, 'k', 'v1', ?)", (bot.tempo.iso_utc(),))
        c.execute("INSERT INTO configuracoes (tenant_id, chave, valor, atualizado_em) "
                  "VALUES (2, 'k', 'v2', ?)", (bot.tempo.iso_utc(),))
    # os helpers isolam por tenant (default 1)
    assert bot.carregar_sessao("41790099999") == {"a": 1}
    assert bot.carregar_sessao("41790099999", tenant_id=2) == {"a": 2}
    assert bot.obter_configuracao("k") == "v1"
    assert bot.obter_configuracao("k", tenant_id=2) == "v2"


def test_pk_composta_colisao_dentro_do_mesmo_tenant(base_dados):
    import sqlite3
    with db.ligacao() as c:
        c.execute("INSERT INTO sessoes (tenant_id, telefone, dados) VALUES (1, '41790088888', '{}')")
        try:
            c.execute("INSERT INTO sessoes (tenant_id, telefone, dados) VALUES (1, '41790088888', '{}')")
            assert False, "devia ter colidido"
        except sqlite3.IntegrityError:
            pass


def test_migracao_15_reconstroi_sem_perder_linhas():
    """A migração 15 corre sobre uma BD parada na v14 (PK só telefone/chave)
    e reconstrói com PK composta sem perder dados."""
    import sqlite3
    c = sqlite3.connect(":memory:")
    c.executescript(
        "CREATE TABLE sessoes (telefone TEXT PRIMARY KEY, dados TEXT NOT NULL, tenant_id INTEGER NOT NULL DEFAULT 1);"
        "CREATE TABLE interacoes_cliente (telefone TEXT PRIMARY KEY, ultima_mensagem_em TEXT NOT NULL, tenant_id INTEGER NOT NULL DEFAULT 1);"
        "CREATE TABLE reservas_temporarias (telefone TEXT PRIMARY KEY, data TEXT NOT NULL, hora TEXT NOT NULL, "
        "servico TEXT, duracao TEXT, criado_em TEXT NOT NULL, expira_em TEXT NOT NULL, tenant_id INTEGER NOT NULL DEFAULT 1);"
        "CREATE TABLE configuracoes (chave TEXT PRIMARY KEY, valor TEXT NOT NULL, atualizado_em TEXT NOT NULL, tenant_id INTEGER NOT NULL DEFAULT 1);"
        "CREATE TABLE servicos (id TEXT PRIMARY KEY, nome_pt TEXT, tenant_id INTEGER NOT NULL DEFAULT 1);"
    )
    c.execute("INSERT INTO sessoes (telefone, dados) VALUES ('999', '{\"x\":1}')")
    c.execute("INSERT INTO configuracoes (chave, valor, atualizado_em) VALUES ('c', '7', 'now')")
    c.execute("INSERT INTO reservas_temporarias (telefone, data, hora, criado_em, expira_em) "
              "VALUES ('999', '05.10.2026', '09:00', 'now', 'later')")
    c.commit()

    db._m15_identidade_por_tenant(c)
    c.commit()

    assert c.execute("SELECT dados FROM sessoes WHERE tenant_id=1 AND telefone='999'").fetchone()[0] == '{"x":1}'
    assert c.execute("SELECT valor FROM configuracoes WHERE tenant_id=1 AND chave='c'").fetchone()[0] == '7'
    assert c.execute("SELECT hora FROM reservas_temporarias WHERE tenant_id=1 AND telefone='999'").fetchone()[0] == '09:00'
    pk = {r[1] for r in c.execute("PRAGMA table_info(sessoes)").fetchall() if r[5]}
    assert pk == {"tenant_id", "telefone"}
    c.close()
