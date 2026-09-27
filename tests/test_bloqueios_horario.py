"""Bloqueios de horário a meio do dia (migração 27) — ideia dos PMS de
hotelaria ("reservas bloqueadas").

Cobre: a subtração nas janelas do dia (business_hours.janelas_do_dia é o
único ponto — o motor de disponibilidade herda), o bot a deixar de oferecer
horários bloqueados, a API (criar/validar/apagar, aviso de marcações já
dentro do bloqueio sem nunca as cancelar), autenticação, e o calendário a
devolver os bloqueios do intervalo."""

import base64

import pytest

import db
from scheduling import business_hours as bh
from scheduling import availability as av
from conftest import marcar, data_pt, dias_abertos

AUTH = {"Authorization": "Basic " + base64.b64encode(b"painel:painel-pw").decode()}


@pytest.fixture
def dia(base_dados):
    return dias_abertos(1)[0]


# ---------------------------------------------------------------------------
# Camada de horários
# ---------------------------------------------------------------------------
def test_bloqueio_parte_a_janela_em_duas(dia):
    antes = bh.janelas_do_dia(dia)
    assert antes, "fixture: o dia tem de estar aberto"
    (abre, fecha) = antes[0]
    bh.adicionar_bloqueio(1, dia, "12:00", "13:00", "almoço")
    depois = bh.janelas_do_dia(dia)
    assert (abre, 12 * 60) in depois and (13 * 60, ) [0] in [j[0] for j in depois]
    assert all(not (j[0] < 12 * 60 + 30 < j[1]) for j in depois)   # 12:30 fora


def test_bloqueio_do_dia_inteiro_fecha_o_dia(dia):
    bh.adicionar_bloqueio(1, dia, "00:00", "23:59", "férias de última hora")
    assert bh.janelas_do_dia(dia) == []
    assert bh.dia_aberto(dia) is False


def test_slots_do_bot_respeitam_o_bloqueio(dia):
    servico = db.listar_servicos()[0]["id"]
    livres_antes = av.slots(servico, dia)
    if not livres_antes:
        pytest.skip("dia sem slots — política de antecedência")
    alvo = livres_antes[0]
    h, m = alvo.split(":")
    fim = f"{int(h) + 1:02d}:{m}"
    bh.adicionar_bloqueio(1, dia, alvo, fim, "formação")
    assert alvo not in av.slots(servico, dia)


def test_validacao_de_dados():
    with pytest.raises(ValueError):
        bh.adicionar_bloqueio(1, "não-é-data", "12:00", "13:00")
    with pytest.raises(ValueError):
        bh.adicionar_bloqueio(1, "2030-01-06", "13:00", "12:00")   # fim antes do início
    with pytest.raises(ValueError):
        bh.adicionar_bloqueio(1, "2030-01-06", "meiodia", "13:00")


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------
def test_api_criar_listar_no_calendario_e_apagar(cliente_http, dia):
    r = cliente_http.post("/api/horarios/bloqueios",
                          json={"data": dia, "inicio": "14:00", "fim": "16:00",
                                "motivo": "formação"}, headers=AUTH)
    assert r.status_code == 201
    bloqueio = r.get_json()["bloqueio"]
    assert bloqueio["reason"] == "formação"

    cal = cliente_http.get(f"/api/calendario?inicio={dia}&fim={dia}", headers=AUTH).get_json()
    assert any(b["id"] == bloqueio["id"] for b in cal["bloqueios"])

    assert cliente_http.delete(f"/api/horarios/bloqueios/{bloqueio['id']}",
                               headers=AUTH).status_code == 200
    cal = cliente_http.get(f"/api/calendario?inicio={dia}&fim={dia}", headers=AUTH).get_json()
    assert cal["bloqueios"] == []
    assert cliente_http.delete("/api/horarios/bloqueios/999",
                               headers=AUTH).status_code == 404


def test_api_avisa_marcacoes_dentro_do_bloqueio_sem_cancelar(cliente_http, dia):
    servico = db.listar_servicos()[0]["id"]
    aid = marcar("41790001234", servico, data_pt(dia), "10:00")

    corpo = {"data": dia, "inicio": "09:30", "fim": "11:30", "motivo": "pessoal"}
    r = cliente_http.post("/api/horarios/bloqueios", json=corpo, headers=AUTH)
    assert r.status_code == 200
    j = r.get_json()
    assert j["precisa_confirmacao"] is True
    assert [a["id"] for a in j["afetadas"]] == [aid]
    assert bh.listar_bloqueios(1, dia, dia) == []            # nada gravado ainda

    r = cliente_http.post("/api/horarios/bloqueios",
                          json={**corpo, "confirmar": True}, headers=AUTH)
    assert r.status_code == 201
    # a marcação continua viva — bloquear nunca cancela
    est = cliente_http.get(f"/api/agendamentos/{aid}", headers=AUTH).get_json()
    assert est["estado"].lower() in ("confirmed", "pending")


def test_api_validacao_e_autenticacao(cliente_http, dia):
    assert cliente_http.post("/api/horarios/bloqueios",
                             json={"data": dia, "inicio": "13:00", "fim": "12:00"},
                             headers=AUTH).status_code == 400
    assert cliente_http.post("/api/horarios/bloqueios",
                             json={"data": dia, "inicio": "12:00", "fim": "13:00"}).status_code == 401
    assert cliente_http.delete("/api/horarios/bloqueios/1").status_code == 401
