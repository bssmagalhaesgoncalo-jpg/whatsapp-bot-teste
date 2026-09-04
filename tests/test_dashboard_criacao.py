"""Dashboard V3 — Fase Funcional 1: criação e edição de marcações a partir do
painel (POST /api/agendamentos, POST /api/agendamentos/<id>/editar).

Ambos os endpoints reutilizam o motor existente (guardar_agendamento,
conflitos_no_intervalo) — nada de lógica de marcação duplicada."""

import base64

import bot
import db
from conftest import marcar, data_pt

SEG = "2026-09-07"          # segunda-feira
SEG_TXT = data_pt(SEG)
AUTH = {"Authorization": "Basic " + base64.b64encode(b"painel:painel-pw").decode()}


# --- POST /api/agendamentos (criar) ---------------------------------------
def test_criar_marcacao_com_cliente_novo(cliente_http, base_dados):
    r = cliente_http.post("/api/agendamentos", json={
        "telefone": "41790020001", "nome": "Rita Nova",
        "servico_id": "limpeza_pele", "data": SEG, "hora": "10:00",
    }, headers=AUTH)
    assert r.status_code == 201, r.get_json()
    j = r.get_json()
    ag = j["agendamento"]
    assert ag["nome"] == "Rita Nova"
    assert ag["servico_id"] == "limpeza_pele"
    assert ag["data_iso"] == SEG and ag["hora_hhmm"] == "10:00"
    assert ag["duracao_min"] == 60          # herdado do serviço
    assert ag["preco_cents"] == 8000        # herdado do serviço
    # cliente foi criado
    cust = db.obter_customer(ag["customer_id"])
    assert cust and cust["phone"] == "41790020001"


def test_criar_marcacao_sem_preco_do_servico_fica_a_confirmar(cliente_http, base_dados):
    r = cliente_http.post("/api/agendamentos", json={
        "telefone": "41790020002", "nome": "Ana",
        "servico_id": "pestanas", "data": SEG, "hora": "09:00",
    }, headers=AUTH)
    assert r.status_code == 201
    ag = r.get_json()["agendamento"]
    assert ag["preco_cents"] is None        # NUNCA CHF 0


def test_criar_marcacao_usa_cliente_existente(cliente_http, base_dados):
    marcar("41790020003", "limpeza_pele", SEG_TXT, "09:00", nome="Carla")
    cust = db.listar_customers(1)[0]
    r = cliente_http.post("/api/agendamentos", json={
        "customer_id": cust["id"],
        "servico_id": "design_sobrancelhas", "data": SEG, "hora": "11:00",
    }, headers=AUTH)
    assert r.status_code == 201, r.get_json()
    ag = r.get_json()["agendamento"]
    assert ag["telefone"] == "41790020003"
    assert ag["nome"] == "Carla"


def test_criar_marcacao_conflito_devolve_409(cliente_http, base_dados):
    marcar("41790020004", "limpeza_pele", SEG_TXT, "10:00")   # 10:00-11:00
    r = cliente_http.post("/api/agendamentos", json={
        "telefone": "41790020005", "nome": "Outra",
        "servico_id": "design_sobrancelhas", "data": SEG, "hora": "10:15",
    }, headers=AUTH)
    assert r.status_code == 409


def test_criar_marcacao_valida_campos_obrigatorios(cliente_http, base_dados):
    r = cliente_http.post("/api/agendamentos", json={"telefone": "1", "nome": "X"}, headers=AUTH)
    assert r.status_code == 400
    r = cliente_http.post("/api/agendamentos", json={
        "telefone": "1", "nome": "X", "servico_id": "limpeza_pele",
        "data": "31-12-2026", "hora": "10:00",
    }, headers=AUTH)
    assert r.status_code == 400
    r = cliente_http.post("/api/agendamentos", json={
        "telefone": "1", "nome": "X", "servico_id": "limpeza_pele",
        "data": SEG, "hora": "25:99",
    }, headers=AUTH)
    assert r.status_code == 400


def test_criar_marcacao_requer_autenticacao(cliente_http, base_dados):
    r = cliente_http.post("/api/agendamentos", json={
        "telefone": "1", "nome": "X", "servico_id": "limpeza_pele",
        "data": SEG, "hora": "10:00",
    })
    assert r.status_code == 401


# --- POST /api/agendamentos/<id>/editar ------------------------------------
def test_editar_preco_e_notas(cliente_http, base_dados):
    idag = marcar("41790030001", "pestanas", SEG_TXT, "09:00")
    r = cliente_http.post(f"/api/agendamentos/{idag}/editar",
                          json={"preco_cents": 9000, "notas": "cliente pediu desconto"}, headers=AUTH)
    assert r.status_code == 200, r.get_json()
    ag = r.get_json()["agendamento"]
    assert ag["preco_cents"] == 9000
    assert ag["extra"] == "cliente pediu desconto"


def test_editar_duracao_sem_conflito(cliente_http, base_dados):
    idag = marcar("41790030002", "design_sobrancelhas", SEG_TXT, "09:00")  # 30min -> 09:00-09:30
    r = cliente_http.post(f"/api/agendamentos/{idag}/editar", json={"duracao_min": 45}, headers=AUTH)
    assert r.status_code == 200
    assert r.get_json()["agendamento"]["duracao_min"] == 45


def test_editar_duracao_com_conflito_devolve_409(cliente_http, base_dados):
    idag = marcar("41790030003", "design_sobrancelhas", SEG_TXT, "09:00")  # 09:00-09:30
    marcar("41790030004", "design_sobrancelhas", SEG_TXT, "09:30")        # 09:30-10:00
    r = cliente_http.post(f"/api/agendamentos/{idag}/editar", json={"duracao_min": 60}, headers=AUTH)
    assert r.status_code == 409


def test_editar_servico_atualiza_preco_por_omissao(cliente_http, base_dados):
    idag = marcar("41790030005", "design_sobrancelhas", SEG_TXT, "09:00")
    r = cliente_http.post(f"/api/agendamentos/{idag}/editar",
                          json={"servico_id": "limpeza_pele"}, headers=AUTH)
    assert r.status_code == 200, r.get_json()
    ag = r.get_json()["agendamento"]
    assert ag["servico_id"] == "limpeza_pele"
    assert ag["duracao_min"] == 60
    assert ag["preco_cents"] == 8000


def test_editar_marcacao_cancelada_devolve_409(cliente_http, base_dados):
    idag = marcar("41790030006", "limpeza_pele", SEG_TXT, "09:00")
    bot.marcar_agendamento_cancelado(idag)
    r = cliente_http.post(f"/api/agendamentos/{idag}/editar", json={"notas": "x"}, headers=AUTH)
    assert r.status_code == 409


def test_editar_nada_para_atualizar_devolve_400(cliente_http, base_dados):
    idag = marcar("41790030007", "limpeza_pele", SEG_TXT, "09:00")
    r = cliente_http.post(f"/api/agendamentos/{idag}/editar", json={}, headers=AUTH)
    assert r.status_code == 400
