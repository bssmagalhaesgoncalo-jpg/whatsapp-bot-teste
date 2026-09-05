"""Cancelada na Agenda operacional (ver dashboard/app.js matchFiltroAg):

  • cancelled + bloqueia_horario=0 (horário libertado) ...... "ruído" na
    Agenda: o servidor continua a devolvê-la em /api/calendario (fica no
    histórico do cliente e reaparece com o filtro "Canceladas" ligado do
    lado do cliente), mas o campo `bloqueia_horario` tem de vir certo para
    o frontend decidir esconder por omissão.
  • cancelled + bloqueia_horario=1 (horário ainda ocupado) ... continua a
    "contar" como ocupação real do slot.

Cobre também /api/agendamentos/<id>: `bloqueia_horario` tem de ser o valor
RESOLVIDO (agendamento_bloqueia_horario), nunca a coluna crua — mesma
semântica de evento_calendario()."""

import base64

import bot
from conftest import marcar, data_pt

AUTH = {"Authorization": "Basic " + base64.b64encode(b"painel:painel-pw").decode()}
DIA = "2026-09-10"
DIA_TXT = data_pt(DIA)


def test_cancelada_com_horario_libertado_tem_bloqueia_horario_false(cliente_http, base_dados):
    idag = marcar("41790000301", "limpeza_pele", DIA_TXT, "09:00")
    r = cliente_http.post(f"/api/agendamentos/{idag}/cancelar", json={"libertar": True}, headers=AUTH)
    assert r.status_code == 200

    r = cliente_http.get(f"/api/agendamentos/{idag}", headers=AUTH)
    corpo = r.get_json()
    assert corpo["estado"] == "cancelled"
    assert corpo["bloqueia_horario"] is False

    r = cliente_http.get(f"/api/calendario?inicio={DIA}&fim={DIA}", headers=AUTH)
    evento = next(e for e in r.get_json()["eventos"] if e["id"] == idag)
    assert evento["bloqueia_horario"] is False


def test_cancelada_sem_libertar_continua_a_bloquear_o_horario(cliente_http, base_dados):
    idag = marcar("41790000302", "limpeza_pele", DIA_TXT, "10:00")
    r = cliente_http.post(f"/api/agendamentos/{idag}/cancelar", json={"libertar": False}, headers=AUTH)
    assert r.status_code == 200

    r = cliente_http.get(f"/api/agendamentos/{idag}", headers=AUTH)
    corpo = r.get_json()
    assert corpo["estado"] == "cancelled"
    assert corpo["bloqueia_horario"] is True

    r = cliente_http.get(f"/api/calendario?inicio={DIA}&fim={DIA}", headers=AUTH)
    evento = next(e for e in r.get_json()["eventos"] if e["id"] == idag)
    assert evento["bloqueia_horario"] is True


def test_confirmada_tem_bloqueia_horario_true(cliente_http, base_dados):
    idag = marcar("41790000303", "limpeza_pele", DIA_TXT, "11:00")
    r = cliente_http.get(f"/api/agendamentos/{idag}", headers=AUTH)
    assert r.get_json()["bloqueia_horario"] is True
