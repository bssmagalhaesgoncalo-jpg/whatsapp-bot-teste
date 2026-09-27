"""
Consentimento de marketing pedido à cliente pelo BOT (crm/consent.py).

O que se prova: pergunta-se UMA vez, no fim da primeira marcação; um "não"
é uma resposta gravada e persistente; e a marcação funciona na mesma se a
cliente nunca responder — a pergunta é um extra, não um passo do fluxo.
"""

from __future__ import annotations

import hashlib
import hmac
import json

import pytest

import bot
import db
from crm import consent
from campaigns import engine as campanhas

CLIENTE = "41791234567"


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


def _texto(txt, mid, de=CLIENTE):
    return {"from": de, "id": mid, "type": "text", "text": {"body": txt}}


def _lista(rid, titulo, mid, de=CLIENTE):
    return {"from": de, "id": mid, "type": "interactive",
            "interactive": {"type": "list_reply", "list_reply": {"id": rid, "title": titulo}}}


def _botao(rid, mid, de=CLIENTE):
    return {"from": de, "id": mid, "type": "interactive",
            "interactive": {"type": "button_reply", "button_reply": {"id": rid, "title": rid}}}


def _escolher(cliente_http, enviados, mid, de=CLIENTE, indice=0):
    """Toca na opção `indice` da última lista enviada. Assim o teste não
    depende de nenhuma data: segue a lista que o bot ofereceu de facto."""
    for payload in reversed(enviados):
        seccoes = (payload.get("interactive", {}).get("action", {}).get("sections") or [])
        linhas = [l for s in seccoes for l in (s.get("rows") or [])]
        if linhas:
            linha = linhas[indice]
            return _post(cliente_http, _lista(linha["id"], linha["title"], mid, de))
    raise AssertionError("nenhuma lista enviada")


def _marcar(cliente_http, enviados, sufixo="a", nome="Ana Teste", de=CLIENTE):
    """Percorre o fluxo completo até à confirmação, com datas relativas."""
    _post(cliente_http, _botao("lang_pt", f"{sufixo}1", de))
    _post(cliente_http, _texto(nome, f"{sufixo}2", de))
    _post(cliente_http, _lista("mp_marcar", "Marcar", f"{sufixo}3", de))
    _post(cliente_http, _lista("svc_limpeza_pele", "Limpeza de pele", f"{sufixo}4", de))
    _escolher(cliente_http, enviados, f"{sufixo}5", de, indice=1)   # amanhã
    _escolher(cliente_http, enviados, f"{sufixo}6", de)             # 1.ª hora livre
    _post(cliente_http, _botao("confirmar", f"{sufixo}7", de))


def _pergunta_enviada(enviados):
    return [p for p in enviados
            if consent.BOTAO_SIM in json.dumps(p, ensure_ascii=False)]


# ---------------------------------------------------------------------------
def test_primeira_marcacao_pergunta_o_consentimento(cliente_http, base_dados, enviados):
    _marcar(cliente_http, enviados)

    perguntas = _pergunta_enviada(enviados)
    assert len(perguntas) == 1
    # A pergunta vem DEPOIS da confirmação — nunca antes de a marcação estar feita.
    corpo = json.dumps(perguntas[0], ensure_ascii=False)
    assert consent.BOTAO_NAO in corpo
    assert enviados.index(perguntas[0]) > 0

    estado = consent.estado(CLIENTE)
    assert estado["perguntado_em"]
    assert estado["resposta"] is None      # perguntada, ainda sem resposta


def test_a_marcacao_fica_feita_mesmo_sem_resposta(cliente_http, base_dados, enviados):
    _marcar(cliente_http, enviados)
    ags = [a for a in bot.listar_agendamentos() if a["telefone"] == CLIENTE]
    assert len(ags) == 1
    assert bot.chave_estado(ags[0]["estado"]) == "confirmed"
    assert consent.estado(CLIENTE)["opt_in"] is False


def test_um_sim_liga_o_opt_in_com_data_e_origem(cliente_http, base_dados, enviados):
    _marcar(cliente_http, enviados)
    _post(cliente_http, _botao(consent.BOTAO_SIM, "c1"))

    estado = consent.estado(CLIENTE)
    assert estado["opt_in"] is True
    assert estado["resposta"] == "sim"
    assert estado["respondido_em"]
    assert estado["origem"] == consent.ORIGEM_WHATSAPP


def test_um_nao_fica_gravado_e_e_persistente(cliente_http, base_dados, enviados):
    _marcar(cliente_http, enviados)
    _post(cliente_http, _botao(consent.BOTAO_NAO, "c1"))

    estado = consent.estado(CLIENTE)
    assert estado["opt_in"] is False
    assert estado["resposta"] == "nao"     # um "não" é resposta, não ausência de "sim"
    assert estado["origem"] == consent.ORIGEM_WHATSAPP

    # Segunda marcação: nunca mais se pergunta a quem já disse que não.
    antes = len(_pergunta_enviada(enviados))
    _marcar(cliente_http, enviados, sufixo="b")
    assert len(_pergunta_enviada(enviados)) == antes
    assert consent.estado(CLIENTE)["resposta"] == "nao"


def test_nao_se_pergunta_duas_vezes_a_quem_ja_foi_perguntado(cliente_http, base_dados, enviados):
    _marcar(cliente_http, enviados)
    assert len(_pergunta_enviada(enviados)) == 1
    # Sem responder nada, faz outra marcação — não se insiste.
    _marcar(cliente_http, enviados, sufixo="b")
    assert len(_pergunta_enviada(enviados)) == 1


def test_resposta_avulsa_nao_parte_nada(cliente_http, base_dados, enviados):
    """Alguém que toca no botão sem ter ficha (ou muito depois)."""
    _post(cliente_http, _botao(consent.BOTAO_SIM, "z1", de="41790000999"))
    assert consent.estado("41790000999") is None


# ---------------------------------------------------------------------------
# A ligação ao motor de campanhas continua a recusar quem não consentiu
# ---------------------------------------------------------------------------
def test_campanhas_continuam_a_recusar_quem_nao_tem_opt_in(cliente_http, base_dados, enviados):
    _marcar(cliente_http, enviados)
    cust = db.obter_ou_criar_customer(CLIENTE)

    assert campanhas._motivo_exclusao(cust) is not None   # sem resposta: fora

    _post(cliente_http, _botao(consent.BOTAO_NAO, "c1"))
    assert campanhas._motivo_exclusao(db.obter_customer(cust["id"])) is not None


def test_o_toggle_do_painel_tambem_grava_a_prova(cliente_http, base_dados, enviados):
    _marcar(cliente_http, enviados)
    cust = db.obter_ou_criar_customer(CLIENTE)
    r = cliente_http.patch(f"/api/clientes/{cust['id']}", json={"marketing_opt_in": True},
                           auth=("painel", "painel-pw"))
    assert r.status_code == 200

    estado = consent.estado(CLIENTE)
    assert estado["opt_in"] is True
    assert estado["resposta"] == "sim"
    assert estado["origem"] == "painel"
