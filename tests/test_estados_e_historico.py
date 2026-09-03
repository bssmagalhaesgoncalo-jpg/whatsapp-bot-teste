"""Testes 13, 14: completed / no_show mantêm histórico coerente."""

import bot
import db
import estados
from conftest import marcar, data_pt

DIA_TXT = data_pt("2026-09-10")


def test_estados_canonicos_normalizam_legado():
    assert estados.normalizar("confirmado") == "confirmed"
    assert estados.normalizar("Concluído") == "completed"
    assert estados.normalizar("cancelado") == "cancelled"
    assert estados.normalizar("reagendado") == "cancelled"
    assert estados.normalizar("NO-SHOW") == "no_show"


def test_13_completed_mantem_registo_e_historico(base_dados):
    idag = marcar("41790000201", "limpeza_pele", DIA_TXT, "🕘 09:00")
    ag2, _ = bot.reagendar_agendamento(idag, "2026-09-11", "10:30", origem="painel",
                                       avisar_cliente=False)
    bot.atualizar_estado_agendamento(idag, estados.COMPLETED)
    ag = bot.obter_agendamento(idag)
    assert bot.chave_estado(ag["estado"]) == "completed"
    # o registo permanece e o histórico do reagendamento também
    assert ag["servico"] == "Limpeza de pele"
    hist = bot.historico_agendamento(idag)
    assert len(hist) == 1
    # completed continua a ocupar o horário (marcação realizada)
    assert bot.agendamento_bloqueia_horario(ag) is True


def test_14_no_show_mantem_registo_e_liberta_agenda(base_dados):
    idag = marcar("41790000202", "pestanas", DIA_TXT, "🕘 09:00")
    bot.atualizar_estado_agendamento(idag, estados.NO_SHOW)
    ag = bot.obter_agendamento(idag)
    assert bot.chave_estado(ag["estado"]) == "no_show"
    assert ag["telefone"] == "41790000202"          # registo intacto
    assert ag["servico_id"] == "pestanas"
    # no_show não deve bloquear agenda futura
    assert bot.agendamento_bloqueia_horario(ag) is False


def test_api_estado_rejeita_estado_invalido(cliente_http, base_dados):
    import base64
    idag = marcar("41790000203", "limpeza_pele", DIA_TXT, "🕘 09:00")
    h = {"Authorization": "Basic " + base64.b64encode(b"painel:painel-pw").decode()}
    r = cliente_http.post(f"/api/agendamentos/{idag}/estado", json={"estado": "banana"}, headers=h)
    assert r.status_code == 400
    r = cliente_http.post(f"/api/agendamentos/{idag}/estado", json={"estado": "no_show"}, headers=h)
    assert r.status_code == 200
    r = cliente_http.post(f"/api/agendamentos/{idag}/estado", json={"estado": "completed"}, headers=h)
    assert r.status_code == 409          # já não está ativa
