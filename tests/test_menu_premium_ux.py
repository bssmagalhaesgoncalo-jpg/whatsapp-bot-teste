"""Regressão do menu principal premium (WHATSAPP PREMIUM UX):
exatamente 4 opções, na ordem certa, em PT/DE/EN — e o novo item
"Serviços & preços" abre um catálogo de leitura que nunca inicia sozinho
uma marcação (só o botão "Marcar agora" no detalhe o faz)."""

import hashlib
import hmac
import json

import pytest

import bot

CLIENTE = "41791234599"

IDS_ESPERADOS = ["mp_marcar", "mp_gerir", "mp_servicos", "mp_humano"]


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


def _botao(rid, mid, cliente=CLIENTE):
    return {"from": cliente, "id": mid, "type": "interactive",
            "interactive": {"type": "button_reply", "button_reply": {"id": rid, "title": rid}}}


def _lista(rid, titulo, mid, cliente=CLIENTE):
    return {"from": cliente, "id": mid, "type": "interactive",
            "interactive": {"type": "list_reply", "list_reply": {"id": rid, "title": titulo}}}


def _linhas_do_ultimo_menu(enviados):
    payload = enviados[-1]
    return payload["interactive"]["action"]["sections"][0]["rows"]


@pytest.mark.parametrize("lang_id,idioma", [("lang_pt", "pt"), ("lang_de", "de"), ("lang_en", "en")])
def test_menu_principal_tem_exatamente_4_opcoes_na_ordem_certa(cliente_http, base_dados, enviados,
                                                                 lang_id, idioma):
    cliente = f"{CLIENTE}{idioma}"
    _post(cliente_http, _botao(lang_id, "m1", cliente))
    _post(cliente_http, {"from": cliente, "id": "m2", "type": "text", "text": {"body": "Ana"}})
    linhas = _linhas_do_ultimo_menu(enviados)
    assert [r["id"] for r in linhas] == IDS_ESPERADOS


def test_servicos_e_precos_mostra_catalogo_e_nao_inicia_marcacao_sozinho(cliente_http, base_dados, enviados):
    _post(cliente_http, _botao("lang_pt", "m1"))
    _post(cliente_http, {"from": CLIENTE, "id": "m2", "type": "text", "text": {"body": "Ana"}})
    _post(cliente_http, _lista("mp_servicos", "Serviços & preços", "m3"))
    # a sessão de marcação continua vazia — é só leitura
    assert bot.carregar_sessao(CLIENTE).get("servico_id") is None
    catalogo_payload = enviados[-1]
    linhas = catalogo_payload["interactive"]["action"]["sections"][0]["rows"]
    assert linhas  # há pelo menos um serviço listado
    primeiro_id = linhas[0]["id"]
    assert primeiro_id.startswith("svcdet_")

    _post(cliente_http, _lista(primeiro_id, "qualquer", "m4"))
    assert bot.carregar_sessao(CLIENTE).get("servico_id") is None  # detalhe, ainda não marcou
    detalhe = enviados[-1]
    botoes = detalhe["interactive"]["action"]["buttons"]
    ids_botoes = [b["reply"]["id"] for b in botoes]
    assert any(bid.startswith("svc_") for bid in ids_botoes)
