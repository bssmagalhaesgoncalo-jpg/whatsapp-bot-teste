"""Regressões do fluxo do bot pelo webhook real: marcação Daniela Beauty e
o BUG P0 do reagendamento do cliente (a marcação antiga nunca se perde)."""

import hashlib
import hmac
import json

import pytest

import bot
import db
from conftest import data_pt

CLIENTE = "41791234567"
DIA_TXT = data_pt("2026-09-14")


@pytest.fixture()
def enviados(monkeypatch):
    saida = []
    monkeypatch.setattr(bot, "enviar", lambda payload: saida.append(payload) or None)
    monkeypatch.setattr(bot, "enviar_notificacao_interna_marcacao", lambda *a, **k: None)
    return saida


def _post(cliente_http, msg):
    corpo = json.dumps({"entry": [{"changes": [{"value": {
        "messaging_product": "whatsapp",
        "metadata": {"phone_number_id": "x"},
        "messages": [msg],
    }}]}]}).encode()
    sig = "sha256=" + hmac.new(b"segredo-de-teste", corpo, hashlib.sha256).hexdigest()
    return cliente_http.post("/webhook", data=corpo, content_type="application/json",
                             headers={"X-Hub-Signature-256": sig})


def _texto(txt, mid):
    return {"from": CLIENTE, "id": mid, "type": "text", "text": {"body": txt}}


def _lista(rid, titulo, mid):
    return {"from": CLIENTE, "id": mid, "type": "interactive",
            "interactive": {"type": "list_reply", "list_reply": {"id": rid, "title": titulo}}}


def _botao(rid, mid):
    return {"from": CLIENTE, "id": mid, "type": "interactive",
            "interactive": {"type": "button_reply", "button_reply": {"id": rid, "title": rid}}}


def _reservar_marcacao(cliente_http, enviados, hora_titulo="🕘 09:00"):
    _post(cliente_http, _botao("lang_pt", "m1"))
    _post(cliente_http, _texto("Ana Teste", "m2"))          # nome
    _post(cliente_http, _lista("mp_marcar", "Marcar", "m3"))
    _post(cliente_http, _lista("svc_limpeza_pele", "Limpeza de pele", "m4"))
    _post(cliente_http, _lista("opt_0", DIA_TXT, "m5"))     # dia (título = texto)
    _post(cliente_http, _lista("opt_0", hora_titulo, "m6"))  # hora
    _post(cliente_http, _botao("confirmar", "m7"))


def test_marcacao_completa_cria_agendamento(cliente_http, base_dados, enviados):
    _reservar_marcacao(cliente_http, enviados)
    ags = [a for a in bot.listar_agendamentos() if a["telefone"] == CLIENTE]
    assert len(ags) == 1
    a = ags[0]
    assert a["servico_id"] == "limpeza_pele"
    assert a["data_iso"] == "2026-09-14"
    assert a["hora_hhmm"] == "09:00"
    assert a["duracao_min"] == 60
    assert bot.chave_estado(a["estado"]) == "confirmed"
    # não sobra sessão a meio
    assert bot.carregar_sessao(CLIENTE).get("servico_id") is None


def test_p0_reagendamento_cliente_nao_perde_a_marcacao_se_desistir(cliente_http, base_dados, enviados):
    _reservar_marcacao(cliente_http, enviados)
    idag = [a for a in bot.listar_agendamentos() if a["telefone"] == CLIENTE][0]["id"]

    # cliente inicia reagendamento e ESCOLHE dia... e depois desiste (MENU)
    _post(cliente_http, _botao(f"reagendar_{idag}", "r1"))
    _post(cliente_http, _lista("opt_1", data_pt("2026-09-15"), "r2"))
    _post(cliente_http, _texto("MENU", "r3"))

    ag = bot.obter_agendamento(idag)
    assert bot.chave_estado(ag["estado"]) == "confirmed"     # continua ativa!
    assert ag["data_iso"] == "2026-09-14"                    # data inalterada
    assert ag["hora_hhmm"] == "09:00"
    # e não foi criado nenhum registo novo
    assert len([a for a in bot.listar_agendamentos() if a["telefone"] == CLIENTE]) == 1


def test_p0_reagendamento_cliente_move_a_mesma_marcacao(cliente_http, base_dados, enviados):
    _reservar_marcacao(cliente_http, enviados)
    idag = [a for a in bot.listar_agendamentos() if a["telefone"] == CLIENTE][0]["id"]

    _post(cliente_http, _botao(f"reagendar_{idag}", "r1"))
    _post(cliente_http, _lista("opt_1", data_pt("2026-09-15"), "r2"))
    _post(cliente_http, _lista("opt_2", "🕐 13:00", "r3"))
    _post(cliente_http, _botao("confirmar", "r4"))

    ags = [a for a in bot.listar_agendamentos() if a["telefone"] == CLIENTE]
    assert len(ags) == 1                                    # MESMO registo
    a = ags[0]
    assert a["id"] == idag
    assert a["data_iso"] == "2026-09-15" and a["hora_hhmm"] == "13:00"
    assert bot.chave_estado(a["estado"]) == "confirmed"
    assert len(bot.historico_agendamento(idag)) == 1
    # horário antigo (14-09 09:00) voltou a ficar livre
    ocup = bot.ocupacoes()
    assert not bot.conflitos_no_intervalo(ocup, "2026-09-14", "09:00", "Limpeza de pele", "1h")


def test_idempotencia_wamid_nao_duplica(cliente_http, base_dados, enviados):
    _reservar_marcacao(cliente_http, enviados)
    # reenvia o "confirmar" com o mesmo id -> ignorado
    r = _post(cliente_http, _botao("confirmar", "m7"))
    assert json.loads(r.data)["status"] == "repetida"
    assert len([a for a in bot.listar_agendamentos() if a["telefone"] == CLIENTE]) == 1
