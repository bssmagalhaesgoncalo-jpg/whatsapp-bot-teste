"""Testes 13, 14: completed / no_show mantêm histórico coerente."""

import bot
import db
import estados
from conftest import marcar, data_pt, dias_abertos

# Datas RELATIVAS: com datas fixas, a suite passava a falhar sozinha no dia
# em que elas ficavam para trás (era o caso destas — 09/2026).
DIA_ISO, DIA2_ISO = dias_abertos(2)
DIA_TXT = data_pt(DIA_ISO)


def test_estados_canonicos_normalizam_legado():
    assert estados.normalizar("confirmado") == "confirmed"
    assert estados.normalizar("Concluído") == "completed"
    assert estados.normalizar("cancelado") == "cancelled"
    assert estados.normalizar("reagendado") == "cancelled"
    assert estados.normalizar("NO-SHOW") == "no_show"


def test_13_completed_mantem_registo_e_historico(base_dados):
    idag = marcar("41790000201", "limpeza_pele", DIA_TXT, "🕘 09:00")
    ag2, _ = bot.reagendar_agendamento(idag, DIA2_ISO, "10:30", origem="painel",
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
    # #9: a marcação é no futuro -> concluir/faltar exige confirmação
    r = cliente_http.post(f"/api/agendamentos/{idag}/estado", json={"estado": "no_show"}, headers=h)
    assert r.status_code == 409 and (r.get_json() or {}).get("precisa_confirmacao") is True
    r = cliente_http.post(f"/api/agendamentos/{idag}/estado",
                          json={"estado": "no_show", "confirmar": True}, headers=h)
    assert r.status_code == 200
    r = cliente_http.post(f"/api/agendamentos/{idag}/estado",
                          json={"estado": "completed", "confirmar": True}, headers=h)
    assert r.status_code == 409          # já não está ativa


def _dia_passado():
    """Um dia que já passou de certeza, seja qual for o dia de hoje."""
    from datetime import timedelta
    import tempo
    return (tempo.hoje_zurique() - timedelta(days=28)).isoformat()


def test_api_estado_no_passado_nao_precisa_confirmacao(cliente_http, base_dados):
    import base64
    idag = marcar("41790000204", "limpeza_pele", data_pt(_dia_passado()), "🕘 09:00")
    h = {"Authorization": "Basic " + base64.b64encode(b"painel:painel-pw").decode()}
    r = cliente_http.post(f"/api/agendamentos/{idag}/estado", json={"estado": "completed"}, headers=h)
    assert r.status_code == 200
