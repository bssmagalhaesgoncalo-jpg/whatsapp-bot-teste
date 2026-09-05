"""Regressão do bug crítico: quando uma data escolhida não tem horários
livres, o bot tinha de voltar à escolha de DATA — mas deixava sessao["data"]
por limpar. O list_reply seguinte (uma NOVA data escolhida pelo cliente) caía
no ramo que decide "é a hora" só porque sessao["data"] já estava preenchida,
e a data acabava gravada como se fosse a hora (ex.: Data: 04.09.2026,
Hora: 05.09.2026). Ver bot.passo_hora e o dispatcher de list_reply em
receber_mensagem()."""

import hashlib
import hmac
import json

import pytest

import bot
from conftest import data_pt

CLIENTE = "41791234567"
D1 = "2026-09-04"   # sem vagas
D2 = "2026-09-05"   # com vagas
D3 = "2026-09-06"   # também sem vagas (cenário: duas seguidas sem vagas)


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


def _sem_vagas_em(monkeypatch, *datas_iso):
    """Faz horarios_livres_para_sessao devolver [] só para as datas indicadas,
    delegando no motor real para qualquer outra data."""
    original = bot.horarios_livres_para_sessao
    fechadas = set(datas_iso)

    def fake(sessao, telefone=None):
        data_iso = bot.data_iso_de_texto((sessao or {}).get("data"))
        if data_iso in fechadas:
            return []
        return original(sessao, telefone=telefone)

    monkeypatch.setattr(bot, "horarios_livres_para_sessao", fake)


def _ate_escolha_de_data(cliente_http):
    _post(cliente_http, _botao("lang_pt", "m1"))
    _post(cliente_http, _texto("Ana Teste", "m2"))
    _post(cliente_http, _lista("mp_marcar", "Marcar", "m3"))
    _post(cliente_http, _lista("svc_limpeza_pele", "Limpeza de pele", "m4"))


def test_1_data_com_vagas_avanca_normalmente_para_hora_e_confirmacao(cliente_http, base_dados, enviados):
    _ate_escolha_de_data(cliente_http)
    _post(cliente_http, _lista("opt_0", data_pt(D2), "d1"))
    sessao = bot.carregar_sessao(CLIENTE)
    assert bot.data_iso_de_texto(sessao["data"]) == D2
    assert "hora" not in sessao

    _post(cliente_http, _lista("opt_0", "🕘 09:00", "h1"))
    sessao = bot.carregar_sessao(CLIENTE)
    assert bot.data_iso_de_texto(sessao["data"]) == D2
    assert bot.hora_hhmm_de_texto(sessao["hora"]) == "09:00"

    resumo = json.dumps(enviados[-1], ensure_ascii=False)
    assert data_pt(D2) in resumo
    assert "09:00" in resumo


def test_2_primeira_data_sem_vagas_segunda_com_vagas_confirmacao_usa_segunda_data(
        cliente_http, base_dados, enviados, monkeypatch):
    _sem_vagas_em(monkeypatch, D1)
    _ate_escolha_de_data(cliente_http)

    _post(cliente_http, _lista("opt_0", data_pt(D1), "d1"))    # sem vagas
    sessao = bot.carregar_sessao(CLIENTE)
    assert "data" not in sessao, "sessao['data'] tem de ficar limpa quando não há vagas"

    _post(cliente_http, _lista("opt_1", data_pt(D2), "d2"))    # nova data, com vagas
    sessao = bot.carregar_sessao(CLIENTE)
    assert bot.data_iso_de_texto(sessao["data"]) == D2, "a segunda escolha tem de ser tratada como DATA"
    assert "hora" not in sessao, "a data nunca pode ser gravada no campo hora"

    _post(cliente_http, _lista("opt_0", "🕙 10:30", "h1"))
    sessao = bot.carregar_sessao(CLIENTE)
    assert bot.data_iso_de_texto(sessao["data"]) == D2
    assert bot.hora_hhmm_de_texto(sessao["hora"]) == "10:30"

    resumo = json.dumps(enviados[-1], ensure_ascii=False)
    assert data_pt(D2) in resumo
    assert data_pt(D1) not in resumo


def test_3_duas_datas_consecutivas_sem_vagas_mantem_sempre_na_escolha_de_data(
        cliente_http, base_dados, enviados, monkeypatch):
    _sem_vagas_em(monkeypatch, D1, D3)
    _ate_escolha_de_data(cliente_http)

    _post(cliente_http, _lista("opt_0", data_pt(D1), "d1"))
    assert "data" not in bot.carregar_sessao(CLIENTE)

    _post(cliente_http, _lista("opt_0", data_pt(D3), "d2"))
    sessao = bot.carregar_sessao(CLIENTE)
    assert "data" not in sessao and "hora" not in sessao, \
        "depois de duas datas sem vagas a sessão continua na etapa de data, sem nada preso"


def test_4_payload_de_data_recebido_a_espera_de_hora_e_rejeitado(cliente_http, base_dados, enviados):
    _ate_escolha_de_data(cliente_http)
    _post(cliente_http, _lista("opt_0", data_pt(D2), "d1"))
    sessao_antes = dict(bot.carregar_sessao(CLIENTE))
    assert "hora" not in sessao_antes

    # Payload de LISTA DE DATA chega quando o bot está à espera de HORA.
    _post(cliente_http, _lista("opt_9", data_pt(D3), "x1"))
    sessao_depois = bot.carregar_sessao(CLIENTE)
    assert "hora" not in sessao_depois, "uma data nunca pode ser aceite como hora"
    assert sessao_depois.get("data") == sessao_antes.get("data"), "a data escolhida não pode ser substituída"


def test_5_voltar_na_escolha_de_data_e_hora_mantem_estado_consistente(cliente_http, base_dados, enviados):
    _ate_escolha_de_data(cliente_http)
    _post(cliente_http, _lista("opt_0", data_pt(D2), "d1"))
    _post(cliente_http, _lista("opt_0", "🕘 09:00", "h1"))
    assert "hora" in bot.carregar_sessao(CLIENTE)

    _post(cliente_http, _lista(bot.ID_VOLTAR, "Voltar", "v1"))  # volta da hora para a data
    sessao = bot.carregar_sessao(CLIENTE)
    assert "hora" not in sessao and "data" in sessao

    _post(cliente_http, _lista(bot.ID_VOLTAR, "Voltar", "v2"))  # volta da hora para a data
    sessao = bot.carregar_sessao(CLIENTE)
    assert "data" not in sessao and "hora" not in sessao
    assert sessao.get("servico_id") == "limpeza_pele"           # serviço mantém-se

    # e o fluxo volta a aceitar corretamente uma nova escolha de data
    _post(cliente_http, _lista("opt_0", data_pt(D2), "v4"))
    sessao = bot.carregar_sessao(CLIENTE)
    assert bot.data_iso_de_texto(sessao["data"]) == D2 and "hora" not in sessao


def test_6_cancelar_limpa_estado_transitorio(cliente_http, base_dados, enviados):
    _ate_escolha_de_data(cliente_http)
    _post(cliente_http, _lista("opt_0", data_pt(D2), "d1"))
    _post(cliente_http, _botao(bot.ID_CANCELAR, "c1"))
    sessao = bot.carregar_sessao(CLIENTE)
    assert sessao.get("servico_id") is None
    assert "data" not in sessao and "hora" not in sessao


def test_7_confirmar_com_hora_no_formato_de_data_nunca_cria_marcacao(cliente_http, base_dados, enviados):
    """Defesa em profundidade: mesmo que a sessão fique corrompida (ex.: um
    bug futuro noutro sítio), o botão "confirmar" nunca grava uma marcação
    com Hora: DD.MM.YYYY — recupera para a escolha de hora em vez disso."""
    _ate_escolha_de_data(cliente_http)
    _post(cliente_http, _lista("opt_0", data_pt(D2), "d1"))

    sessao = bot.carregar_sessao(CLIENTE)
    sessao["hora"] = data_pt(D3)          # estado inconsistente forçado
    bot.guardar_sessao(CLIENTE, sessao)

    _post(cliente_http, _botao("confirmar", "c1"))

    assert not [a for a in bot.listar_agendamentos() if a["telefone"] == CLIENTE], \
        "nenhuma marcação pode ser criada com uma data no campo hora"
    sessao_depois = bot.carregar_sessao(CLIENTE)
    assert "hora" not in sessao_depois, "o estado inconsistente é limpo, não reenviado tal e qual"
