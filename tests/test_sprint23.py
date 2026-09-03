"""Sprint 2/3 — motor de disponibilidade, business_hours, OPERATION ENGINE, API."""

import base64
import json
from datetime import datetime

import pytest

import bot
import db
import tempo
from scheduling import availability as av, business_hours as bh
from operations import engine as op
from conftest import marcar, data_pt

SEG = "2026-09-07"          # segunda-feira
SEG_TXT = data_pt(SEG)
AUTH = {"Authorization": "Basic " + base64.b64encode(b"painel:painel-pw").decode()}


# --- Fase D: business_hours + availability -------------------------------
def test_horario_negocio_semeado(base_dados):
    assert bh.janelas_do_dia(SEG) == [(9 * 60, 18 * 60)]     # seg 09:00-18:00
    assert bh.janelas_do_dia("2026-09-13") == []             # domingo fechado


def test_slots_respeita_duracao_e_fecho(base_dados):
    # limpeza (60min): último início cabe às 17:00
    s = av.slots("limpeza_pele", SEG)
    assert s[0] == "09:00" and "17:00" in s and "17:15" not in s
    # pestanas (120min): último início às 16:00
    p = av.slots("pestanas", SEG)
    assert p[-1] == "16:00"


def test_slots_exclui_marcacao_e_reserva(base_dados):
    marcar("41790010001", "limpeza_pele", SEG_TXT, "10:00")           # 10:00-11:00
    bot.reter_horario("41790010002", {"servico_id": "limpeza_pele", "servico": "Limpeza de pele",
                                      "duracao": "1h", "duracao_min": 60,
                                      "data": SEG_TXT, "hora": "14:00"})
    s = av.slots("limpeza_pele", SEG, telefone="novo")
    assert "10:00" not in s and "10:30" not in s and "11:00" in s
    assert "14:00" not in s and "13:00" in s
    # o próprio cliente da reserva vê o seu horário
    s2 = av.slots("limpeza_pele", SEG, telefone="41790010002")
    assert "14:00" in s2


def test_slots_buffer(base_dados):
    db.atualizar_servico("dermaplaning", {"buffer_after_min": 15})
    marcar("41790010003", "dermaplaning", SEG_TXT, "10:00")  # 10:00-11:00 + 15min buffer
    s = av.slots("limpeza_pele", SEG)
    assert "11:00" not in s and "11:15" in s


def test_excecao_fecha_o_dia(base_dados):
    bh.adicionar_excecao(1, SEG, reason="Feriado")
    assert bh.janelas_do_dia(SEG) == []
    assert av.slots("limpeza_pele", SEG) == []


def test_excecao_deteta_marcacoes_afetadas(cliente_http, base_dados):
    marcar("41790010004", "limpeza_pele", SEG_TXT, "10:00", nome="Ana")
    r = cliente_http.post("/api/horarios/excecoes", json={"data_inicio": SEG, "reason": "Férias"},
                          headers=AUTH)
    j = r.get_json()
    assert j["precisa_confirmacao"] and len(j["afetadas"]) == 1
    # confirmando, cria a exceção (não cancela ninguém automaticamente)
    r = cliente_http.post("/api/horarios/excecoes",
                          json={"data_inicio": SEG, "reason": "Férias", "confirmar": True}, headers=AUTH)
    assert r.status_code == 201
    ag = bot.obter_agendamento(bot.listar_agendamentos()[0]["id"])
    assert bot.chave_estado(ag["estado"]) == "confirmed"     # continua marcada


def test_min_notice_esconde_slots_de_hoje(base_dados, monkeypatch):
    hoje = tempo.hoje_zurique()
    txt = op._marcacoes_de_hoje  # noqa
    # às 10:00 de hoje, min_notice 120 -> slots < 12:00 escondidos
    fixo = datetime(hoje.year, hoje.month, hoje.day, 10, 0, tzinfo=tempo.FUSO_ZURIQUE)
    monkeypatch.setattr(tempo, "agora_zurique", lambda: fixo)
    if not bh.janelas_do_dia(hoje.isoformat()):
        pytest.skip("hoje está fechado")
    s = av.slots("limpeza_pele", hoje.isoformat())
    assert all(x >= "12:00" for x in s)


# --- Fase E: OPERATION ENGINE -----------------------------------------
def _hoje_txt():
    d = tempo.hoje_zurique()
    return data_pt(d.isoformat())


def test_cartao_proxima_cliente(base_dados, monkeypatch):
    d = tempo.hoje_zurique()
    if d.weekday() == 6:
        pytest.skip("domingo")
    marcar("41790020001", "limpeza_pele", _hoje_txt(), "15:00", nome="Ana Müller")
    agora = datetime(d.year, d.month, d.day, 14, 30, tzinfo=tempo.FUSO_ZURIQUE)
    ck = op.cartao_operacional(agora=agora)
    assert ck["kind"] == "next"
    assert ck["marcacao"]["cliente"] == "Ana Müller"
    assert ck["faltam_min"] == 30
    assert ck["marcacao"]["cliente_visitas"] == 1


def test_cartao_em_curso_pelo_relogio(base_dados, monkeypatch):
    d = tempo.hoje_zurique()
    if d.weekday() == 6:
        pytest.skip("domingo")
    a = marcar("41790020002", "limpeza_pele", _hoje_txt(), "14:00")   # 14:00-15:00
    agora = datetime(d.year, d.month, d.day, 14, 20, tzinfo=tempo.FUSO_ZURIQUE)
    ck = op.cartao_operacional(agora=agora)
    assert ck["kind"] == "in_progress" and ck["decorrido_min"] == 20 and ck["restante_min"] == 40


def test_transicoes_chegou_iniciar_concluir(base_dados):
    d = tempo.hoje_zurique()
    a = marcar("41790020003", "limpeza_pele", _hoje_txt() if d.weekday() != 6 else data_pt(SEG), "14:00")
    op.transicao_operacional(a, "arrived")
    assert bot.obter_agendamento(a)["op_status"] == "arrived"
    assert bot.obter_agendamento(a)["arrived_at"]
    op.transicao_operacional(a, "in_progress")
    assert bot.obter_agendamento(a)["op_status"] == "in_progress"
    op.transicao_operacional(a, "done")
    ag = bot.obter_agendamento(a)
    assert ag["op_status"] == "done" and ag["completed_at"]
    assert bot.chave_estado(ag["estado"]) == "completed"
    assert "booking.completed" in {e["type"] for e in db.eventos_por_processar()}


def test_atraso_nao_envia_e_calcula_afetadas(cliente_http, base_dados, monkeypatch):
    d = tempo.hoje_zurique()
    if d.weekday() == 6:
        pytest.skip("domingo")
    marcar("41790020010", "limpeza_pele", _hoje_txt(), "14:00", nome="Ana")   # 14-15
    marcar("41790020011", "limpeza_pele", _hoje_txt(), "15:00", nome="Marta")  # 15-16
    marcar("41790020012", "limpeza_pele", _hoje_txt(), "17:00", nome="Rita")   # 17-18
    agora = datetime(d.year, d.month, d.day, 13, 55, tzinfo=tempo.FUSO_ZURIQUE)
    monkeypatch.setattr(tempo, "agora_zurique", lambda: agora)

    enviados = []
    monkeypatch.setattr(bot._wa, "enviar_texto", lambda n, t: enviados.append((n, t)))

    r = cliente_http.post("/api/painel/atraso", json={"minutos": 30}, headers=AUTH)
    j = r.get_json()
    nomes = {a["cliente"] for a in j["afetadas"]}
    assert "Marta" in nomes            # 15:00 empurrada
    assert "Rita" not in nomes         # 17:00 tem folga
    assert enviados == []              # NADA foi enviado


# --- Serviços CRUD -----------------------------------------------------
def test_criar_e_editar_servico(cliente_http, base_dados):
    r = cliente_http.post("/api/servicos", json={
        "id": "massagem", "nome_pt": "Massagem", "duracao_min": 50, "preco_cents": 9000}, headers=AUTH)
    assert r.status_code == 201
    assert db.obter_servico("massagem")["preco_cents"] == 9000
    r = cliente_http.patch("/api/servicos/massagem", json={"preco_cents": None, "rebook_days": 30}, headers=AUTH)
    assert r.status_code == 200
    s = db.obter_servico("massagem")
    assert s["preco_cents"] is None and s["rebook_days"] == 30
    # id inválido
    r = cliente_http.post("/api/servicos", json={"id": "X 1", "nome_pt": "z", "duracao_min": 30}, headers=AUTH)
    assert r.status_code == 400


def test_editar_horarios(cliente_http, base_dados):
    grelha = [{"weekday": i, "opens": "08:00", "closes": "20:00"} for i in range(5)]
    grelha += [{"weekday": 5, "opens": "", "closes": ""}, {"weekday": 6, "opens": "", "closes": ""}]
    r = cliente_http.put("/api/horarios", json={"grelha": grelha}, headers=AUTH)
    assert r.status_code == 200
    assert bh.janelas_do_dia("2026-09-07") == [(8 * 60, 20 * 60)]
    assert bh.janelas_do_dia("2026-09-12") == []   # sábado agora fechado
