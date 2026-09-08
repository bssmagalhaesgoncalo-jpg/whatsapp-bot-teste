"""PATCH P4 — Agenda premium + drag & drop + reagendamento operacional.

O drag & drop da Agenda passa pelo MESMO endpoint e pela MESMA função de
domínio do diálogo "Reagendar"/"Editar" (bot.reagendar_agendamento /
POST /api/agendamentos/<id>/reagendar) — só a `origem` distingue os dois no
histórico e nos eventos. Este ficheiro cobre:

  • disponibilidade real (expediente, pausas, exceções, duração, buffers,
    passado) só é aplicada quando o painel pede (`validar_expediente=True`,
    ligado no endpoint) — os chamadores existentes (WhatsApp, testes
    antigos) continuam com o comportamento anterior, sem regressão;
  • bloqueio por op_status (chegou/em curso/concluído) — aplica-se sempre,
    a qualquer chamador;
  • concorrência, eventos (origin=dashboard_drag), booking_source
    inalterado, reminder 24h (P1) recalculado, e P0/P2 não disparam.

Zero envios reais ao WhatsApp: o provider é mockado na fronteira HTTP, tal
como em tests/test_reminder_24h.py (mesmo padrão)."""

import base64
import threading

import pytest
import requests

import bot
import db
import estados
import tempo
from operations import engine as op
from notifications import jobs as notif_jobs
from conftest import marcar, data_pt

AUTH = {"Authorization": "Basic " + base64.b64encode(b"painel:painel-pw").decode()}

# Datas fixas, longe no futuro, dias da semana conhecidos (confirmado por
# data.weekday()): segunda a sábado abertos 09:00-18:00, domingo fechado
# (seed de db._m12_business_hours) — nunca `_futuro(horas)` relativo aqui,
# para o resultado não depender da hora a que a suite corre.
SEG = "2026-10-05"   # segunda
TER = "2026-10-06"   # terça
QUA = "2026-10-07"   # quarta
QUI = "2026-10-08"   # quinta
SEX = "2026-10-09"   # sexta
SAB = "2026-10-10"   # sábado
DOM = "2026-10-11"   # domingo — fechado


def _reagendar_http(cliente_http, id_ag, data, hora, origem=None):
    body = {"data": data, "hora": hora}
    if origem is not None:
        body["origem"] = origem
    return cliente_http.post(f"/api/agendamentos/{id_ag}/reagendar", json=body, headers=AUTH)


def _evento(tipo, entity_id):
    with db.ligacao() as c:
        rows = c.execute(
            "SELECT payload FROM events WHERE type = ? AND entity_id = ? ORDER BY id ASC",
            (tipo, entity_id)).fetchall()
    import json as _j
    return [_j.loads(r[0]) for r in rows]


def _jobs_do_tipo(tipo, booking_id):
    with db.ligacao() as c:
        return c.execute("SELECT id FROM automation_jobs WHERE type = ? AND booking_id = ?",
                         (tipo, booking_id)).fetchall()


# ===========================================================================
# 1-3 — mover para slot válido, persistência, no-op no mesmo slot
# ===========================================================================
def test_1_2_confirmed_move_para_slot_valido_persiste_estruturado(cliente_http, base_dados):
    a = marcar("41790030001", "limpeza_pele", data_pt(SEG), "10:00")
    r = _reagendar_http(cliente_http, a, TER, "11:00", origem="dashboard_drag")
    assert r.status_code == 200, r.get_json()
    ag = r.get_json()["agendamento"]
    assert ag["data_iso"] == TER and ag["hora_hhmm"] == "11:00"
    # colunas estruturadas são a fonte de verdade — não só o texto "bonito"
    assert ag["data"].startswith("06.10.2026")
    assert ag["hora"] == "11:00"


def test_3_mesmo_slot_e_idempotente(cliente_http, base_dados):
    a = marcar("41790030002", "limpeza_pele", data_pt(SEG), "10:00")
    r = _reagendar_http(cliente_http, a, SEG, "10:00", origem="dashboard_drag")
    assert r.status_code == 200, r.get_json()
    ag = r.get_json()["agendamento"]
    assert ag["data_iso"] == SEG and ag["hora_hhmm"] == "10:00"
    # nenhuma marcação duplicada, nenhum histórico "fantasma" além deste passo
    assert len(bot.historico_agendamento(a)) == 1


# ===========================================================================
# 4-5 — conflitos (slot ocupado / overlap parcial)
# ===========================================================================
def test_4_slot_ocupado_e_rejeitado(cliente_http, base_dados):
    marcar("41790030003", "limpeza_pele", data_pt(TER), "10:00")     # 10:00-11:00
    b = marcar("41790030004", "limpeza_pele", data_pt(SEG), "10:00")
    r = _reagendar_http(cliente_http, b, TER, "10:00", origem="dashboard_drag")
    assert r.status_code == 409
    assert "ocupado" in r.get_json()["erro"].lower()
    # a marcação B fica INTACTA na posição original
    assert bot.obter_agendamento(b)["data_iso"] == SEG


def test_5_overlap_parcial_e_rejeitado(cliente_http, base_dados):
    marcar("41790030005", "limpeza_pele", data_pt(TER), "10:00")     # 10:00-11:00 (60min)
    b = marcar("41790030006", "limpeza_pele", data_pt(SEG), "10:00")
    # tenta encaixar-se a começar 10:30, ainda dentro do intervalo ocupado
    r = _reagendar_http(cliente_http, b, TER, "10:30", origem="dashboard_drag")
    assert r.status_code == 409


# ===========================================================================
# 6-7 — duração real e buffers respeitados
# ===========================================================================
def test_6_duracao_real_e_respeitada(cliente_http, base_dados):
    # pestanas = 120min. Só há vaga até às 18:00 -> não cabe a partir das 17:00.
    b = marcar("41790030007", "pestanas", data_pt(SEG), "09:00")
    r = _reagendar_http(cliente_http, b, TER, "17:00", origem="dashboard_drag")
    assert r.status_code == 409
    assert bot.obter_agendamento(b)["data_iso"] == SEG


def test_7_buffers_sao_respeitados(cliente_http, base_dados):
    db.atualizar_servico("limpeza_pele", {"buffer_before_min": 0, "buffer_after_min": 30})
    marcar("41790030008", "limpeza_pele", data_pt(TER), "10:00")     # 10:00-11:00 +30min buffer -> ocupado até 11:30
    b = marcar("41790030009", "limpeza_pele", data_pt(SEG), "10:00")
    r = _reagendar_http(cliente_http, b, TER, "11:15", origem="dashboard_drag")
    assert r.status_code == 409, r.get_json()


# ===========================================================================
# 8-10 — horário de funcionamento, pausa, exceção/dia fechado
# ===========================================================================
def test_8_business_hours_sao_respeitados(cliente_http, base_dados):
    b = marcar("41790030010", "limpeza_pele", data_pt(SEG), "10:00")
    r = _reagendar_http(cliente_http, b, TER, "19:00", origem="dashboard_drag")   # fecha às 18:00
    assert r.status_code == 409
    assert "expediente" in r.get_json()["erro"].lower() or "cabe" in r.get_json()["erro"].lower()
    assert bot.obter_agendamento(b)["hora_hhmm"] == "10:00"


def test_9_pausa_e_respeitada(cliente_http, base_dados):
    with db.ligacao() as c:
        # terça (weekday=1) passa a ter pausa 12:00-13:00
        c.execute("UPDATE business_hours SET break_start = '12:00', break_end = '13:00' "
                  "WHERE tenant_id = 1 AND weekday = 1 AND staff_id IS NULL")
    b = marcar("41790030011", "limpeza_pele", data_pt(SEG), "10:00")
    r = _reagendar_http(cliente_http, b, TER, "12:15", origem="dashboard_drag")
    assert r.status_code == 409, r.get_json()


def test_10_dia_fechado_por_excecao_e_respeitado(cliente_http, base_dados):
    from scheduling import business_hours as bh
    bh.adicionar_excecao(1, QUA, closed=True, reason="Férias")
    b = marcar("41790030012", "limpeza_pele", data_pt(SEG), "10:00")
    r = _reagendar_http(cliente_http, b, QUA, "10:00", origem="dashboard_drag")
    assert r.status_code == 409


def test_10b_domingo_fechado_e_respeitado(cliente_http, base_dados):
    b = marcar("41790030013", "limpeza_pele", data_pt(SEG), "10:00")
    r = _reagendar_http(cliente_http, b, DOM, "10:00", origem="dashboard_drag")
    assert r.status_code == 409


# ===========================================================================
# 11 — horário no passado é rejeitado (SEMPRE, mesmo sem validar_expediente)
# ===========================================================================
def test_11_slot_no_passado_e_rejeitado(cliente_http, base_dados):
    b = marcar("41790030014", "limpeza_pele", data_pt(SEG), "10:00")
    r = _reagendar_http(cliente_http, b, "2020-01-01", "10:00", origem="dashboard_drag")
    assert r.status_code == 409
    assert "passad" in r.get_json()["erro"].lower()


def test_11b_no_passado_bloqueado_tambem_fora_do_painel(base_dados):
    """O bloqueio de horário passado aplica-se a QUALQUER chamador, não só ao
    endpoint do painel — não há origem legítima para reagendar para trás."""
    b = marcar("41790030015", "limpeza_pele", data_pt(SEG), "10:00")
    with pytest.raises(bot.HorarioNoPassado):
        bot.reagendar_agendamento(b, "2020-01-01", "10:00", origem="teste", avisar_cliente=False)


# ===========================================================================
# 12-15 — estados que NUNCA podem ser arrastados
# ===========================================================================
def test_12_cancelled_nao_pode_ser_arrastado(cliente_http, base_dados):
    b = marcar("41790030016", "limpeza_pele", data_pt(SEG), "10:00")
    bot.marcar_agendamento_cancelado(b)
    r = _reagendar_http(cliente_http, b, TER, "10:00", origem="dashboard_drag")
    assert r.status_code == 409


def test_13_completed_nao_pode_ser_arrastado(cliente_http, base_dados):
    b = marcar("41790030017", "limpeza_pele", data_pt(SEG), "10:00")
    op.transicao_operacional(b, "arrived")
    op.transicao_operacional(b, "in_progress")
    op.transicao_operacional(b, "done")
    r = _reagendar_http(cliente_http, b, TER, "10:00", origem="dashboard_drag")
    assert r.status_code == 409


def test_14_no_show_nao_pode_ser_arrastado(cliente_http, base_dados):
    b = marcar("41790030018", "limpeza_pele", data_pt(SEG), "10:00")
    bot.atualizar_estado_agendamento(b, estados.NO_SHOW)
    r = _reagendar_http(cliente_http, b, TER, "10:00", origem="dashboard_drag")
    assert r.status_code == 409


def test_15_arrived_bloqueado(cliente_http, base_dados):
    b = marcar("41790030019", "limpeza_pele", data_pt(SEG), "10:00")
    op.transicao_operacional(b, "arrived")
    r = _reagendar_http(cliente_http, b, TER, "10:00", origem="dashboard_drag")
    assert r.status_code == 409
    j = r.get_json()
    assert j.get("op_status") == "arrived"
    assert "chegou" in j["erro"].lower()


def test_15b_in_progress_bloqueado(cliente_http, base_dados):
    b = marcar("41790030020", "limpeza_pele", data_pt(SEG), "10:00")
    op.transicao_operacional(b, "arrived")
    op.transicao_operacional(b, "in_progress")
    r = _reagendar_http(cliente_http, b, TER, "10:00", origem="dashboard_drag")
    assert r.status_code == 409
    assert r.get_json().get("op_status") == "in_progress"
    # o commercial estado continua "confirmed" — é o op_status que bloqueia,
    # não o EstadoInvalido comercial (mensagens diferentes, motivo diferente)
    assert bot.chave_estado(bot.obter_agendamento(b)["estado"]) == estados.CONFIRMED


# ===========================================================================
# 16 — concorrência: duas tentativas simultâneas para o mesmo slot, só uma vence
# ===========================================================================
def test_16_duas_tentativas_simultaneas_so_uma_vence(cliente_http, base_dados):
    a = marcar("41790030021", "limpeza_pele", data_pt(SEG), "09:00")
    b = marcar("41790030022", "limpeza_pele", data_pt(SEG), "14:00")
    barreira = threading.Barrier(2)
    resultados = {}

    def mover(nome, idag):
        barreira.wait()
        try:
            bot.reagendar_agendamento(idag, TER, "11:00", origem="dashboard_drag",
                                      avisar_cliente=False, validar_expediente=True)
            resultados[nome] = "ok"
        except bot.HorarioOcupado:
            resultados[nome] = "ocupado"
        except Exception as e:                       # pragma: no cover
            resultados[nome] = "erro:" + repr(e)

    t1 = threading.Thread(target=mover, args=("a", a))
    t2 = threading.Thread(target=mover, args=("b", b))
    t1.start(); t2.start(); t1.join(); t2.join()

    assert sorted(resultados.values()) == ["ocupado", "ok"], resultados
    ativos = [x for x in bot.listar_agendamentos()
              if x["data_iso"] == TER and x["hora_hhmm"] == "11:00"
              and bot.agendamento_bloqueia_horario(x)]
    assert len(ativos) == 1


# ===========================================================================
# 17-19 — Reminder 24h (P1) recalculado após drag
# ===========================================================================
def test_17_18_19_drag_recalcula_reminder_24h(cliente_http, base_dados):
    a = marcar("41790030023", "limpeza_pele", data_pt(SEG), "10:00")
    bot.disparar_automacoes()
    with db.ligacao() as c:
        job_antigo = c.execute(
            "SELECT id, run_at FROM automation_jobs WHERE booking_id = ? AND type = ?",
            (a, notif_jobs.TYPE_REMINDER_24H)).fetchone()
    assert job_antigo   # reminder criado na marcação original (>24h no futuro)

    r = _reagendar_http(cliente_http, a, QUA, "11:00", origem="dashboard_drag")
    assert r.status_code == 200, r.get_json()
    bot.disparar_automacoes()

    with db.ligacao() as c:
        jobs = c.execute(
            "SELECT id, run_at, status FROM automation_jobs WHERE booking_id = ? AND type = ?",
            (a, notif_jobs.TYPE_REMINDER_24H)).fetchall()
    assert len(jobs) == 1                             # 18 — nunca um segundo job
    assert jobs[0][0] == job_antigo[0]                 # MESMA linha, reaproveitada
    assert jobs[0][1] != job_antigo[1]                 # run_at recalculado
    assert jobs[0][2] == notif_jobs.PENDING            # 19 — continua ativo (>24h)

    ag = bot.obter_agendamento(a)
    inicio = tempo.combinar_local(ag["data_iso"], ag["hora_hhmm"])
    from datetime import timedelta
    assert jobs[0][1] == tempo.iso_utc(inicio - timedelta(hours=24))


def test_19b_drag_para_menos_de_24h_cancela_reminder(cliente_http, base_dados):
    from datetime import timedelta
    a = marcar("41790030024", "limpeza_pele", data_pt(SEG), "10:00")
    bot.disparar_automacoes()
    perto = tempo.agora_zurique() + timedelta(hours=5)
    if perto.weekday() == 6 or not (9 <= perto.hour < 18):
        pytest.skip("janela relativa cai fora do expediente — não é isso que este teste cobre")
    r = _reagendar_http(cliente_http, a, perto.date().isoformat(), perto.strftime("%H:%M"),
                        origem="dashboard_drag")
    if r.status_code != 200:
        pytest.skip(f"slot relativo indisponível neste corrido: {r.get_json()}")
    bot.disparar_automacoes()
    with db.ligacao() as c:
        job = c.execute("SELECT status FROM automation_jobs WHERE booking_id = ? AND type = ?",
                        (a, notif_jobs.TYPE_REMINDER_24H)).fetchone()
    assert job[0] == notif_jobs.CANCELLED


# ===========================================================================
# 20-21 — P0/P2 não disparam com um simples reagendamento
# ===========================================================================
def test_20_drag_nao_cria_post_service(cliente_http, base_dados):
    a = marcar("41790030025", "limpeza_pele", data_pt(SEG), "10:00")
    r = _reagendar_http(cliente_http, a, TER, "10:00", origem="dashboard_drag")
    assert r.status_code == 200
    bot.disparar_automacoes()
    assert not _jobs_do_tipo(notif_jobs.TYPE_POST_SERVICE, a)
    assert bot.obter_agendamento(a)["post_service_thanks_sent_at"] is None


def test_21_drag_nao_cria_rebooking_followup(cliente_http, base_dados):
    a = marcar("41790030026", "limpeza_pele", data_pt(SEG), "10:00")
    r = _reagendar_http(cliente_http, a, TER, "10:00", origem="dashboard_drag")
    assert r.status_code == 200
    bot.disparar_automacoes()
    assert not _jobs_do_tipo(notif_jobs.TYPE_REBOOKING_FOLLOWUP, a)


# ===========================================================================
# 22-25 — eventos / origin / booking_source
# ===========================================================================
def test_22_23_24_evento_rescheduled_emitido_uma_vez_com_origin(cliente_http, base_dados):
    a = marcar("41790030027", "limpeza_pele", data_pt(SEG), "10:00")
    r = _reagendar_http(cliente_http, a, TER, "11:00", origem="dashboard_drag")
    assert r.status_code == 200
    eventos = _evento("booking.rescheduled", a)
    assert len(eventos) == 1                            # 22 — uma única vez
    ev = eventos[0]
    assert ev["data_antiga"].startswith("05.10.2026") and ev["hora_antiga"] == "10:00"
    assert ev["data_nova"].startswith("06.10.2026") and ev["hora_nova"] == "11:00"   # 23
    assert ev["origem"] == "dashboard_drag"              # 24


def test_24b_botao_normal_usa_origin_dashboard(cliente_http, base_dados):
    a = marcar("41790030028", "limpeza_pele", data_pt(SEG), "10:00")
    r = _reagendar_http(cliente_http, a, TER, "10:00")    # sem origem -> default "dashboard"
    assert r.status_code == 200
    eventos = _evento("booking.rescheduled", a)
    assert eventos[-1]["origem"] == "dashboard"


def test_24c_origem_desconhecida_cai_para_dashboard(cliente_http, base_dados):
    a = marcar("41790030029", "limpeza_pele", data_pt(SEG), "10:00")
    r = _reagendar_http(cliente_http, a, TER, "10:00", origem="algo_inventado")
    assert r.status_code == 200
    eventos = _evento("booking.rescheduled", a)
    assert eventos[-1]["origem"] == "dashboard"


def test_25_booking_source_permanece_inalterado(cliente_http, base_dados):
    a = marcar("41790030030", "limpeza_pele", data_pt(SEG), "10:00")
    antes = bot.obter_agendamento(a)["booking_source"]
    r = _reagendar_http(cliente_http, a, TER, "11:00", origem="dashboard_drag")
    assert r.status_code == 200
    depois = bot.obter_agendamento(a)["booking_source"]
    assert depois == antes    # reagendar NUNCA reescreve como a marcação nasceu


# ===========================================================================
# 26-28 — WhatsApp: mecanismo existente, DEMO nunca chama a Meta, falha não desfaz
# ===========================================================================
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


def test_26_notificacao_usa_mecanismo_existente(cliente_http, base_dados, monkeypatch):
    chamadas = _mock_provider(monkeypatch)
    a = marcar("41790030031", "limpeza_pele", data_pt(SEG), "10:00")
    r = _reagendar_http(cliente_http, a, TER, "10:00", origem="dashboard_drag")
    assert r.status_code == 200
    j = r.get_json()
    assert isinstance(j["cliente_notificado"], bool)
    # Se tentou notificar, foi SEMPRE pelo ponto único (messaging/whatsapp.py:
    # enviar -> a mesma Graph API URL) — nunca um POST ad-hoc à parte.
    for url, payload in chamadas:
        assert url == "https://graph.facebook.com/" + bot.config.GRAPH_API_VERSION + "/123456/messages"
        assert payload["to"] == "41790030031"


def test_27_demo_nao_chama_meta_real(cliente_http, base_dados, monkeypatch):
    chamadas = _mock_provider(monkeypatch)
    a = marcar(f"{bot.DEMO_TELEFONE_PREFIXO}0099", "limpeza_pele", data_pt(SEG), "10:00", nome="Demo")
    r = _reagendar_http(cliente_http, a, TER, "10:00", origem="dashboard_drag")
    assert r.status_code == 200
    assert chamadas == []   # nunca chegou a requests.post


def test_28_falha_meta_nao_desfaz_reagendamento_valido(cliente_http, base_dados, monkeypatch):
    _mock_provider(monkeypatch, falha=True)
    a = marcar("41790030032", "limpeza_pele", data_pt(SEG), "10:00")
    r = _reagendar_http(cliente_http, a, TER, "11:00", origem="dashboard_drag")
    assert r.status_code == 200, r.get_json()             # a escrita já tinha corrido
    j = r.get_json()
    assert j["cliente_notificado"] is False               # mas sabe-se que a notificação falhou
    ag = bot.obter_agendamento(a)
    assert ag["data_iso"] == TER and ag["hora_hhmm"] == "11:00"


# ===========================================================================
# 29-31 — códigos de resposta da API
# ===========================================================================
def test_29_input_invalido_400(cliente_http, base_dados):
    a = marcar("41790030033", "limpeza_pele", data_pt(SEG), "10:00")
    assert _reagendar_http(cliente_http, a, "não-é-data", "10:00").status_code == 400
    assert _reagendar_http(cliente_http, a, TER, "25:99").status_code == 400
    assert _reagendar_http(cliente_http, a, "2026-02-30", "10:00").status_code == 400


def test_30_booking_inexistente_404(cliente_http, base_dados):
    r = _reagendar_http(cliente_http, 999999, TER, "10:00")
    assert r.status_code == 404


def test_31_conflito_nunca_e_500(cliente_http, base_dados):
    marcar("41790030034", "limpeza_pele", data_pt(TER), "10:00")
    b = marcar("41790030035", "limpeza_pele", data_pt(SEG), "10:00")
    for hora in ("10:00", "19:00", "10:30"):
        r = _reagendar_http(cliente_http, b, TER, hora, origem="dashboard_drag")
        assert r.status_code in (200, 409), (hora, r.status_code, r.get_json())


# ===========================================================================
# 32 — front-end: cartão elegível tem de sair do agEventCard() com
# draggable="true" a sério. Não há framework de testes DOM JS no projeto
# (sem package.json/jsdom) — mesmo padrão estático já usado em
# test_resultados.py/test_pos_atendimento.py: ler o app.js e verificar o
# literal, sem instalar nada novo. Guarda de regressão do bug em que o
# helper genérico h() escrevia draggable="" (atributo enumerado, o browser
# trata como não-arrastável) em vez de draggable="true".
# ===========================================================================
def test_32_agenda_card_draggable_usa_atributo_enumerado_true():
    src = open("static/dashboard/app.js", encoding="utf-8").read()
    inicio = src.index("function agEventCard(")
    fim = src.index("\nfunction ", inicio + 1)
    corpo = src[inicio:fim]
    assert 'draggable: podeArrastar ? "true" : undefined' in corpo
    # nunca mais o padrão que gerava draggable="" via helper genérico h()
    assert "draggable: podeArrastar || undefined" not in corpo
