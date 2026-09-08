"""PATCH P4.1 — REAGENDAMENTO INICIADO PELO PAINEL, com CONFIRMAÇÃO DO
CLIENTE (ver notifications/reschedule.py).

drag / diálogo "Reagendar"/"Editar" -> POST /reagendar-pedido -> pedido
PENDENTE (a marcação original NÃO muda; o horário novo fica reservado) ->
WhatsApp ao cliente [Confirmar novo horário]/[Manter horário atual] ->
cliente confirma -> aplica com bot.reagendar_agendamento (motor existente,
tal e qual) / cliente recusa -> marcação original intacta, horário novo
libertado / Daniela cancela o pedido -> idêntico à recusa, sem mensagem.

`/api/agendamentos/<id>/reagendar` (usado por este fluxo por baixo, quando o
pedido é aceite) e o reagendamento iniciado pelo PRÓPRIO CLIENTE pelo
WhatsApp continuam intocados — ver tests/test_agenda_reagendamento_drag.py e
tests/test_fluxo_bot.py (suite completa, sem regressão).

Zero envios reais ao WhatsApp: o provider é mockado na fronteira HTTP, tal
como em tests/test_reminder_24h.py (mesmo padrão)."""

import base64
import hashlib
import hmac
import json
import threading

import pytest
import requests

import bot
import db
import estados
import tempo
from operations import engine as op
from notifications import jobs as notif_jobs
from notifications import reschedule as notif_reschedule
from conftest import marcar, data_pt

AUTH = {"Authorization": "Basic " + base64.b64encode(b"painel:painel-pw").decode()}

# Mesmas datas fixas do test_agenda_reagendamento_drag.py: segunda a sábado
# abertos 09:00-18:00, domingo fechado (seed de db._m12_business_hours).
SEG = "2026-10-05"
TER = "2026-10-06"
QUA = "2026-10-07"


def _pedido_http(cliente_http, id_ag, data, hora, origem=None):
    body = {"data": data, "hora": hora}
    if origem is not None:
        body["origem"] = origem
    return cliente_http.post(f"/api/agendamentos/{id_ag}/reagendar-pedido", json=body, headers=AUTH)


def _cancelar_pedido_http(cliente_http, id_ag):
    return cliente_http.post(f"/api/agendamentos/{id_ag}/reagendar-pedido/cancelar", json={}, headers=AUTH)


def _pedido_pendente(id_ag):
    return notif_reschedule.pedido_pendente_da_marcacao(id_ag)


def _evento(tipo, entity_id):
    with db.ligacao() as c:
        rows = c.execute(
            "SELECT payload FROM events WHERE type = ? AND entity_id = ? ORDER BY id ASC",
            (tipo, entity_id)).fetchall()
    return [json.loads(r[0]) for r in rows]


def _mock_provider(monkeypatch, falha=False):
    chamadas = []

    class _Resp:
        status_code = 200
        text = "{}"

    def _post(url, headers=None, json=None, timeout=None):
        chamadas.append((url, json))
        if falha:
            raise requests.RequestException("falha de rede simulada")
        return _Resp()

    monkeypatch.setattr(bot._wa.requests, "post", _post)
    monkeypatch.setattr(bot._wa.config, "WHATSAPP_TOKEN", "token-de-teste")
    monkeypatch.setattr(bot._wa.config, "PHONE_NUMBER_ID", "123456")
    return chamadas


def _post_webhook(cliente_http, msg):
    corpo = json.dumps({"entry": [{"changes": [{"value": {
        "messaging_product": "whatsapp",
        "metadata": {"phone_number_id": "x"},
        "messages": [msg],
    }}]}]}).encode()
    sig = "sha256=" + hmac.new(b"segredo-de-teste", corpo, hashlib.sha256).hexdigest()
    return cliente_http.post("/webhook", data=corpo, content_type="application/json",
                             headers={"X-Hub-Signature-256": sig})


def _botao_interativo(tel, rid, mid):
    return {"from": tel, "id": mid, "type": "interactive",
            "interactive": {"type": "button_reply", "button_reply": {"id": rid, "title": rid}}}


def _confirmar(cliente_http, tel, request_id, mid="wm1"):
    return _post_webhook(cliente_http, _botao_interativo(tel, f"reagendar_pedido_confirmar_{request_id}", mid))


def _manter(cliente_http, tel, request_id, mid="wm2"):
    return _post_webhook(cliente_http, _botao_interativo(tel, f"reagendar_pedido_manter_{request_id}", mid))


# ===========================================================================
# 1 — drag/diálogo cria um PEDIDO, nunca move a marcação de imediato
# ===========================================================================
def test_1_criar_pedido_nao_move_a_marcacao(cliente_http, base_dados, monkeypatch):
    _mock_provider(monkeypatch)
    tel = "41790060001"
    a = marcar(tel, "limpeza_pele", data_pt(SEG), "10:00")
    bot.guardar_sessao(tel, {"idioma": "pt", "nome": "Cliente Teste"})

    r = _pedido_http(cliente_http, a, TER, "11:00", origem="dashboard_drag")
    assert r.status_code == 200, r.get_json()
    ag = bot.obter_agendamento(a)
    assert ag["data_iso"] == SEG and ag["hora_hhmm"] == "10:00"     # intacta

    pedido = _pedido_pendente(a)
    assert pedido and pedido["new_date"] == TER and pedido["new_time"] == "11:00"
    assert pedido["old_date"] == SEG and pedido["old_time"] == "10:00"
    assert pedido["origin"] == "dashboard_drag"


# ===========================================================================
# 2-3-4 — o horário NOVO fica protegido, o ANTIGO continua protegido, e um
# outro cliente nunca ocupa o horário novo entretanto
# ===========================================================================
def test_2_novo_slot_fica_protegido(cliente_http, base_dados, monkeypatch):
    _mock_provider(monkeypatch)
    a = marcar("41790060002", "limpeza_pele", data_pt(SEG), "10:00")
    r = _pedido_http(cliente_http, a, TER, "11:00", origem="dashboard_drag")
    assert r.status_code == 200, r.get_json()

    with pytest.raises(bot.HorarioOcupado):
        marcar("41790060003", "limpeza_pele", data_pt(TER), "11:00")


def test_3_old_slot_continua_protegido(cliente_http, base_dados, monkeypatch):
    _mock_provider(monkeypatch)
    a = marcar("41790060004", "limpeza_pele", data_pt(SEG), "10:00")
    r = _pedido_http(cliente_http, a, TER, "11:00", origem="dashboard_drag")
    assert r.status_code == 200, r.get_json()

    with pytest.raises(bot.HorarioOcupado):
        marcar("41790060005", "limpeza_pele", data_pt(SEG), "10:00")


def test_4_outro_reagendamento_tambem_nao_ocupa_o_novo_slot(cliente_http, base_dados, monkeypatch):
    _mock_provider(monkeypatch)
    a = marcar("41790060006", "limpeza_pele", data_pt(SEG), "10:00")
    b = marcar("41790060007", "limpeza_pele", data_pt(QUA), "10:00")
    r = _pedido_http(cliente_http, a, TER, "11:00", origem="dashboard_drag")
    assert r.status_code == 200, r.get_json()

    r2 = _pedido_http(cliente_http, b, TER, "11:00", origem="dashboard")
    assert r2.status_code == 409
    assert "ocupado" in r2.get_json()["erro"].lower()


# ===========================================================================
# 5-6-7 — aceite: aplica com o motor existente, liberta o antigo, consome o hold
# ===========================================================================
def test_5_6_7_aceite_move_liberta_antigo_e_consome_hold(cliente_http, base_dados, monkeypatch):
    chamadas = _mock_provider(monkeypatch)
    tel = "41790060008"
    a = marcar(tel, "limpeza_pele", data_pt(SEG), "10:00")
    bot.guardar_sessao(tel, {"idioma": "pt", "nome": "Cliente Teste"})
    r = _pedido_http(cliente_http, a, TER, "11:00", origem="dashboard_drag")
    assert r.status_code == 200, r.get_json()
    pedido = _pedido_pendente(a)
    chamadas.clear()

    rc = _confirmar(cliente_http, tel, pedido["id"])
    assert rc.status_code == 200

    ag = bot.obter_agendamento(a)
    assert ag["data_iso"] == TER and ag["hora_hhmm"] == "11:00"       # 5 — moveu
    assert _pedido_pendente(a) is None                                # já não pendente

    marcar("41790060009", "limpeza_pele", data_pt(SEG), "10:00")      # 6 — antigo livre outra vez

    eventos = _evento("reschedule.accepted", a)
    assert len(eventos) == 1
    assert len(_evento("booking.rescheduled", a)) == 1
    assert chamadas, "confirmação final devia ter tentado enviar WhatsApp ao cliente"


def test_7_hold_consumido_nao_bloqueia_o_proprio_slot_apos_aceite(cliente_http, base_dados, monkeypatch):
    """Depois de aceite, o pedido sai de 'pending' — holds_de_pedidos_reagendamento
    já não o conta; o slot novo continua ocupado (agora pela marcação REAL que
    lá está), não pelo hold."""
    chamadas = _mock_provider(monkeypatch)
    tel = "41790060011"
    a = marcar(tel, "limpeza_pele", data_pt(SEG), "10:00")
    bot.guardar_sessao(tel, {"idioma": "pt", "nome": "Cliente Teste"})
    r = _pedido_http(cliente_http, a, TER, "11:00", origem="dashboard_drag")
    pedido = _pedido_pendente(a)
    _confirmar(cliente_http, tel, pedido["id"])

    with db.ligacao() as c:
        row = c.execute("SELECT status FROM reschedule_requests WHERE id = ?", (pedido["id"],)).fetchone()
    assert row[0] == "accepted"
    with pytest.raises(bot.HorarioOcupado):     # o slot continua ocupado — pela marcação em si
        marcar("41790060012", "limpeza_pele", data_pt(TER), "11:00")


# ===========================================================================
# 8-9 — recusado: marcação original intacta, horário novo libertado
# ===========================================================================
def test_8_9_recusado_mantem_original_e_liberta_novo(cliente_http, base_dados, monkeypatch):
    chamadas = _mock_provider(monkeypatch)
    tel = "41790060013"
    a = marcar(tel, "limpeza_pele", data_pt(SEG), "10:00")
    bot.guardar_sessao(tel, {"idioma": "pt", "nome": "Cliente Teste"})
    r = _pedido_http(cliente_http, a, TER, "11:00", origem="dashboard_drag")
    pedido = _pedido_pendente(a)
    chamadas.clear()

    rm = _manter(cliente_http, tel, pedido["id"])
    assert rm.status_code == 200

    ag = bot.obter_agendamento(a)
    assert ag["data_iso"] == SEG and ag["hora_hhmm"] == "10:00"       # 8 — original intacta
    assert _pedido_pendente(a) is None
    with db.ligacao() as c:
        status = c.execute("SELECT status FROM reschedule_requests WHERE id = ?", (pedido["id"],)).fetchone()[0]
    assert status == "declined"

    marcar("41790060014", "limpeza_pele", data_pt(TER), "11:00")     # 9 — novo livre outra vez
    assert len(_evento("reschedule.declined", a)) == 1
    assert chamadas


# ===========================================================================
# 10 — Daniela cancela o pedido: marcação nunca mudou
# ===========================================================================
def test_10_cancelar_pedido_do_painel_mantem_a_marcacao(cliente_http, base_dados, monkeypatch):
    _mock_provider(monkeypatch)
    a = marcar("41790060015", "limpeza_pele", data_pt(SEG), "10:00")
    _pedido_http(cliente_http, a, TER, "11:00", origem="dashboard_drag")
    pedido = _pedido_pendente(a)

    rc = _cancelar_pedido_http(cliente_http, a)
    assert rc.status_code == 200, rc.get_json()

    ag = bot.obter_agendamento(a)
    assert ag["data_iso"] == SEG and ag["hora_hhmm"] == "10:00"
    assert _pedido_pendente(a) is None
    with db.ligacao() as c:
        status = c.execute("SELECT status FROM reschedule_requests WHERE id = ?", (pedido["id"],)).fetchone()[0]
    assert status == "cancelled"
    marcar("41790060016", "limpeza_pele", data_pt(TER), "11:00")     # slot novo livre outra vez


def test_10b_cancelar_sem_pedido_pendente_e_404(cliente_http, base_dados):
    a = marcar("41790060017", "limpeza_pele", data_pt(SEG), "10:00")
    assert _cancelar_pedido_http(cliente_http, a).status_code == 404


# ===========================================================================
# 11-12 — no máximo um pending por marcação; confirmação duplicada é idempotente
# ===========================================================================
def test_11_so_um_pending_por_marcacao(cliente_http, base_dados, monkeypatch):
    _mock_provider(monkeypatch)
    a = marcar("41790060018", "limpeza_pele", data_pt(SEG), "10:00")
    r1 = _pedido_http(cliente_http, a, TER, "11:00", origem="dashboard_drag")
    assert r1.status_code == 200
    r2 = _pedido_http(cliente_http, a, QUA, "12:00", origem="dashboard")
    assert r2.status_code == 409
    assert "pendente" in r2.get_json()["erro"].lower()


def test_12_confirmacao_duplicada_e_idempotente(cliente_http, base_dados, monkeypatch):
    chamadas = _mock_provider(monkeypatch)
    tel = "41790060019"
    a = marcar(tel, "limpeza_pele", data_pt(SEG), "10:00")
    bot.guardar_sessao(tel, {"idioma": "pt", "nome": "Cliente Teste"})
    _pedido_http(cliente_http, a, TER, "11:00", origem="dashboard_drag")
    pedido = _pedido_pendente(a)

    r1 = _confirmar(cliente_http, tel, pedido["id"], mid="d1")
    r2 = _confirmar(cliente_http, tel, pedido["id"], mid="d2")     # duplicate click
    assert r1.status_code == 200 and r2.status_code == 200

    assert len(_evento("reschedule.accepted", a)) == 1
    assert len(_evento("booking.rescheduled", a)) == 1
    ag = bot.obter_agendamento(a)
    assert ag["data_iso"] == TER and ag["hora_hhmm"] == "11:00"


def test_12b_recusar_depois_de_aceitar_nao_reverte(cliente_http, base_dados, monkeypatch):
    chamadas = _mock_provider(monkeypatch)
    tel = "41790060020"
    a = marcar(tel, "limpeza_pele", data_pt(SEG), "10:00")
    bot.guardar_sessao(tel, {"idioma": "pt", "nome": "Cliente Teste"})
    _pedido_http(cliente_http, a, TER, "11:00", origem="dashboard_drag")
    pedido = _pedido_pendente(a)
    _confirmar(cliente_http, tel, pedido["id"])

    r = _manter(cliente_http, tel, pedido["id"])
    assert r.status_code == 200
    ag = bot.obter_agendamento(a)
    assert ag["data_iso"] == TER and ag["hora_hhmm"] == "11:00"    # já aceite — recusa tardia não desfaz
    assert len(_evento("reschedule.declined", a)) == 0


# ===========================================================================
# 13 — marcação cancelada entretanto (proativo via evento + reativo no accept)
# ===========================================================================
def test_13_cancelamento_entretanto_cancela_o_pedido_proativamente(cliente_http, base_dados, monkeypatch):
    _mock_provider(monkeypatch)
    a = marcar("41790060021", "limpeza_pele", data_pt(SEG), "10:00")
    _pedido_http(cliente_http, a, TER, "11:00", origem="dashboard_drag")
    pedido = _pedido_pendente(a)

    bot.cancelar_agendamento(a, libertar=True, avisar_cliente=False)
    bot.disparar_automacoes()

    with db.ligacao() as c:
        status = c.execute("SELECT status FROM reschedule_requests WHERE id = ?", (pedido["id"],)).fetchone()[0]
    assert status == "cancelled"
    marcar("41790060022", "limpeza_pele", data_pt(TER), "11:00")     # novo slot já livre


def test_13b_accept_falha_com_gracia_se_marcacao_ja_nao_e_valida(cliente_http, base_dados, monkeypatch):
    """Sem drenar o evento booking.cancelled (nunca correu disparar_automacoes):
    aceitar() tem de revalidar sozinho e nunca rebentar."""
    _mock_provider(monkeypatch)
    tel = "41790060023"
    a = marcar(tel, "limpeza_pele", data_pt(SEG), "10:00")
    bot.guardar_sessao(tel, {"idioma": "pt", "nome": "Cliente Teste"})
    _pedido_http(cliente_http, a, TER, "11:00", origem="dashboard_drag")
    pedido = _pedido_pendente(a)

    with db.ligacao() as c:
        c.execute("UPDATE agendamentos SET estado = 'cancelled' WHERE id = ?", (a,))

    resultado = notif_reschedule.aceitar(pedido["id"], tel)
    assert resultado["resultado"] == "falhou"
    with db.ligacao() as c:
        status = c.execute("SELECT status FROM reschedule_requests WHERE id = ?", (pedido["id"],)).fetchone()[0]
    assert status == "cancelled"


# ===========================================================================
# 14 — completed/no_show nunca podem ter um pedido criado
# ===========================================================================
def test_14_completed_bloqueado(cliente_http, base_dados):
    a = marcar("41790060024", "limpeza_pele", data_pt(SEG), "10:00")
    op.transicao_operacional(a, "arrived")
    op.transicao_operacional(a, "in_progress")
    op.transicao_operacional(a, "done")
    r = _pedido_http(cliente_http, a, TER, "11:00")
    assert r.status_code == 409


def test_14b_no_show_bloqueado(cliente_http, base_dados):
    a = marcar("41790060025", "limpeza_pele", data_pt(SEG), "10:00")
    bot.atualizar_estado_agendamento(a, estados.NO_SHOW)
    r = _pedido_http(cliente_http, a, TER, "11:00")
    assert r.status_code == 409


def test_14c_em_curso_bloqueado(cliente_http, base_dados):
    a = marcar("41790060026", "limpeza_pele", data_pt(SEG), "10:00")
    op.transicao_operacional(a, "arrived")
    r = _pedido_http(cliente_http, a, TER, "11:00")
    assert r.status_code == 409
    assert r.get_json().get("op_status") == "arrived"


# ===========================================================================
# 15 — booking_source preservado depois de aceite
# ===========================================================================
def test_15_booking_source_preservado(cliente_http, base_dados, monkeypatch):
    chamadas = _mock_provider(monkeypatch)
    tel = "41790060027"
    a = marcar(tel, "limpeza_pele", data_pt(SEG), "10:00")
    bot.guardar_sessao(tel, {"idioma": "pt", "nome": "Cliente Teste"})
    antes = bot.obter_agendamento(a)["booking_source"]
    _pedido_http(cliente_http, a, TER, "11:00", origem="dashboard_drag")
    pedido = _pedido_pendente(a)
    _confirmar(cliente_http, tel, pedido["id"])
    depois = bot.obter_agendamento(a)["booking_source"]
    assert depois == antes


# ===========================================================================
# 16 — reminder 24h só recalcula depois de aceite (nunca enquanto pendente)
# ===========================================================================
def test_16_reminder_so_recalcula_apos_aceite(cliente_http, base_dados, monkeypatch):
    chamadas = _mock_provider(monkeypatch)
    tel = "41790060028"
    a = marcar(tel, "limpeza_pele", data_pt(SEG), "10:00")
    bot.guardar_sessao(tel, {"idioma": "pt", "nome": "Cliente Teste"})
    bot.disparar_automacoes()
    with db.ligacao() as c:
        job_antes = c.execute(
            "SELECT id, run_at FROM automation_jobs WHERE booking_id = ? AND type = ?",
            (a, notif_jobs.TYPE_REMINDER_24H)).fetchone()
    assert job_antes

    _pedido_http(cliente_http, a, QUA, "11:00", origem="dashboard_drag")
    bot.disparar_automacoes()
    with db.ligacao() as c:
        job_pendente = c.execute(
            "SELECT id, run_at FROM automation_jobs WHERE booking_id = ? AND type = ?",
            (a, notif_jobs.TYPE_REMINDER_24H)).fetchone()
    assert job_pendente == job_antes    # enquanto pendente, nada muda

    pedido = _pedido_pendente(a)
    _confirmar(cliente_http, tel, pedido["id"])
    bot.disparar_automacoes()
    with db.ligacao() as c:
        job_depois = c.execute(
            "SELECT id, run_at FROM automation_jobs WHERE booking_id = ? AND type = ?",
            (a, notif_jobs.TYPE_REMINDER_24H)).fetchone()
    assert job_depois[0] == job_antes[0]      # MESMA linha
    assert job_depois[1] != job_antes[1]      # run_at recalculado para o novo horário


# ===========================================================================
# 17 — nada disto gera fatura/revenue
# ===========================================================================
def test_17_sem_fatura_ou_job_de_pos_atendimento(cliente_http, base_dados, monkeypatch):
    chamadas = _mock_provider(monkeypatch)
    tel = "41790060029"
    a = marcar(tel, "limpeza_pele", data_pt(SEG), "10:00")
    bot.guardar_sessao(tel, {"idioma": "pt", "nome": "Cliente Teste"})
    _pedido_http(cliente_http, a, TER, "11:00", origem="dashboard_drag")
    pedido = _pedido_pendente(a)
    _confirmar(cliente_http, tel, pedido["id"])
    bot.disparar_automacoes()

    with db.ligacao() as c:
        faturas = c.execute("SELECT COUNT(*) FROM invoices WHERE appointment_id = ?", (a,)).fetchone()[0]
    assert faturas == 0
    with db.ligacao() as c:
        jobs = c.execute("SELECT COUNT(*) FROM automation_jobs WHERE booking_id = ? AND type = ?",
                         (a, notif_jobs.TYPE_POST_SERVICE)).fetchone()[0]
    assert jobs == 0


# ===========================================================================
# 18 — DEMO nunca chega à Meta
# ===========================================================================
def test_18_demo_nunca_chama_meta(cliente_http, base_dados, monkeypatch):
    chamadas = _mock_provider(monkeypatch)
    tel = f"{bot.DEMO_TELEFONE_PREFIXO}0088"
    a = marcar(tel, "limpeza_pele", data_pt(SEG), "10:00", nome="Demo")
    r = _pedido_http(cliente_http, a, TER, "11:00", origem="dashboard_drag")
    assert r.status_code == 200, r.get_json()
    assert chamadas == []


# ===========================================================================
# 19 — locale PT/DE/EN preservado na proposta ao cliente
# ===========================================================================
@pytest.mark.parametrize("idioma,pedaco", [
    ("pt", "propôs uma alteração"),
    ("de", "hat eine Änderung"),
    ("en", "proposed a change"),
])
def test_19_locale_preservado_na_proposta(cliente_http, base_dados, monkeypatch, idioma, pedaco):
    chamadas = _mock_provider(monkeypatch)
    tel = f"4179006003{['pt', 'de', 'en'].index(idioma)}"
    a = marcar(tel, "limpeza_pele", data_pt(SEG), "10:00")
    bot.guardar_sessao(tel, {"idioma": idioma, "nome": "Cliente Teste"})
    # dentro da janela de 24h -> mensagem interativa normal (enviar_botoes)
    bot.registar_interacao_cliente(tel)

    r = _pedido_http(cliente_http, a, TER, "11:00", origem="dashboard_drag")
    assert r.status_code == 200, r.get_json()
    assert chamadas, "devia ter tentado enviar a proposta"
    corpo = chamadas[-1][1]["interactive"]["body"]["text"]
    assert pedaco in corpo


# ===========================================================================
# 20 — o diálogo manual ("Reagendar"/"Editar") usa o MESMO fluxo do drag
# ===========================================================================
def test_20_dialogo_manual_usa_o_mesmo_endpoint_do_drag(cliente_http, base_dados, monkeypatch):
    _mock_provider(monkeypatch)
    a = marcar("41790060031", "limpeza_pele", data_pt(SEG), "10:00")
    r = _pedido_http(cliente_http, a, TER, "11:00", origem="dashboard")     # sem "_drag"
    assert r.status_code == 200, r.get_json()
    ag = bot.obter_agendamento(a)
    assert ag["data_iso"] == SEG and ag["hora_hhmm"] == "10:00"     # nunca move de imediato
    pedido = _pedido_pendente(a)
    assert pedido["origin"] == "dashboard"


# ===========================================================================
# 21 — /reagendar (endpoint de baixo nível) continua totalmente funcional
# ===========================================================================
def test_21_endpoint_reagendar_de_baixo_nivel_continua_intacto(cliente_http, base_dados):
    a = marcar("41790060032", "limpeza_pele", data_pt(SEG), "10:00")
    r = cliente_http.post(f"/api/agendamentos/{a}/reagendar",
                          json={"data": TER, "hora": "10:00", "origem": "dashboard_drag"}, headers=AUTH)
    assert r.status_code == 200, r.get_json()
    ag = bot.obter_agendamento(a)
    assert ag["data_iso"] == TER and ag["hora_hhmm"] == "10:00"


# ===========================================================================
# 22 — o reagendamento iniciado pelo PRÓPRIO CLIENTE (fluxo antigo) intacto
# ===========================================================================
def test_22_reagendamento_iniciado_pelo_cliente_sem_regressao(base_dados):
    a = marcar("41790060033", "limpeza_pele", data_pt(SEG), "10:00")
    ag, _ = bot.reagendar_agendamento(a, TER, "10:00", origem="whatsapp_bot", avisar_cliente=False)
    assert ag["data_iso"] == TER and ag["hora_hhmm"] == "10:00"


# ===========================================================================
# 23-24 — CONCORRÊNCIA: duas tentativas simultâneas nunca produzem dois
# pedidos pendentes para a mesma marcação, nem dois pedidos para marcações
# DIFERENTES a reservar o mesmo horário novo (mesmo padrão de
# test_agenda_reagendamento_drag.py::test_16).
# ===========================================================================
def test_23_duas_criacoes_simultaneas_para_a_mesma_marcacao_so_uma_vence(base_dados, monkeypatch):
    _mock_provider(monkeypatch)
    a = marcar("41790060034", "limpeza_pele", data_pt(SEG), "10:00")
    barreira = threading.Barrier(2)
    resultados = {}

    def pedir(nome, data, hora):
        barreira.wait()
        try:
            notif_reschedule.criar_pedido(a, data, hora, origin="dashboard_drag")
            resultados[nome] = "ok"
        except notif_reschedule.PedidoJaExistente:
            resultados[nome] = "ja_existente"
        except Exception as e:                    # pragma: no cover
            resultados[nome] = "erro:" + repr(e)

    t1 = threading.Thread(target=pedir, args=("a", TER, "11:00"))
    t2 = threading.Thread(target=pedir, args=("b", QUA, "12:00"))
    t1.start(); t2.start(); t1.join(); t2.join()

    assert sorted(resultados.values()) == ["ja_existente", "ok"], resultados
    with db.ligacao() as c:
        n = c.execute("SELECT COUNT(*) FROM reschedule_requests WHERE appointment_id = ? AND status = 'pending'",
                      (a,)).fetchone()[0]
    assert n == 1


def test_24_duas_marcacoes_diferentes_nao_reservam_o_mesmo_novo_slot(base_dados, monkeypatch):
    _mock_provider(monkeypatch)
    a = marcar("41790060035", "limpeza_pele", data_pt(SEG), "10:00")
    b = marcar("41790060036", "limpeza_pele", data_pt(QUA), "10:00")
    barreira = threading.Barrier(2)
    resultados = {}

    def pedir(nome, appointment_id):
        barreira.wait()
        try:
            notif_reschedule.criar_pedido(appointment_id, TER, "11:00", origin="dashboard_drag")
            resultados[nome] = "ok"
        except bot.HorarioOcupado:
            resultados[nome] = "ocupado"
        except Exception as e:                    # pragma: no cover
            resultados[nome] = "erro:" + repr(e)

    t1 = threading.Thread(target=pedir, args=("a", a))
    t2 = threading.Thread(target=pedir, args=("b", b))
    t1.start(); t2.start(); t1.join(); t2.join()

    assert sorted(resultados.values()) == ["ocupado", "ok"], resultados
    with db.ligacao() as c:
        n = c.execute("SELECT COUNT(*) FROM reschedule_requests WHERE new_date = ? AND new_time = ? "
                      "AND status = 'pending'", (TER, "11:00")).fetchone()[0]
    assert n == 1
