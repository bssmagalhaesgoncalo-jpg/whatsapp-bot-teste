"""
Saúde do sistema: o bot mudo e o cron parado (core/health.py).

O que se prova aqui é o que ninguém via antes: um 401 da Meta deixava de
responder às clientes e só ficava no log. Agora fica no painel, ela é
avisada uma vez — e a mesma avaria não a inunda de mensagens.
"""

from __future__ import annotations

import json

import pytest

import config
import db  # noqa: F401  (a fixture base_dados migra a BD)
import tempo
from core import health
from messaging import whatsapp
from operations import engine as op


class _Resposta:
    """Uma resposta da Graph API, como a `requests` a devolveria."""

    def __init__(self, status_code, corpo=None):
        self.status_code = status_code
        self._corpo = corpo or {}
        self.text = json.dumps(self._corpo)

    def json(self):
        return self._corpo


def _erro(codigo, mensagem="Error validating access token"):
    return {"error": {"code": codigo, "message": mensagem, "type": "OAuthException"}}


@pytest.fixture()
def meta(monkeypatch):
    """Configura o WhatsApp e intercepta o POST à Meta. Devolve a lista de
    payloads enviados e deixa o teste escolher a resposta."""
    monkeypatch.setattr(config, "WHATSAPP_TOKEN", "token-de-teste")
    monkeypatch.setattr(config, "PHONE_NUMBER_ID", "123", raising=False)
    monkeypatch.setattr(config, "PROVIDER_WHATSAPP", "41790000001")
    monkeypatch.setattr(config, "graph_url", lambda: "https://graph.test/v20.0/123/messages")
    monkeypatch.setattr(whatsapp, "_talvez_avariado", True, raising=False)

    estado = {"resposta": _Resposta(200, {"messages": [{"id": "wamid.1"}]}), "enviados": []}

    def _post(url, headers=None, json=None, timeout=None):
        estado["enviados"].append(json)
        return estado["resposta"]

    monkeypatch.setattr(whatsapp.requests, "post", _post)
    return estado


# ---------------------------------------------------------------------------
# Classificação: conta/limite vs. mensagem concreta
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("status,codigo,esperado", [
    (401, 190, health.CHAVE_WHATSAPP_CONTA),      # token expirado
    (403, None, health.CHAVE_WHATSAPP_CONTA),     # sem permissão
    (400, 131031, health.CHAVE_WHATSAPP_CONTA),   # conta suspensa
    (400, 131042, health.CHAVE_WHATSAPP_CONTA),   # problema de pagamento
    (400, 130429, health.CHAVE_WHATSAPP_LIMITE),  # limite de envios
    (429, None, health.CHAVE_WHATSAPP_LIMITE),
    (400, 100, None),                             # payload inválido: só esta mensagem
    (404, None, None),
])
def test_classificacao_distingue_conta_de_payload(status, codigo, esperado):
    assert whatsapp.classificar_falha(status, codigo) == esperado


# ---------------------------------------------------------------------------
# 401 da Meta: registo visível no painel + um único aviso
# ---------------------------------------------------------------------------
def test_401_fica_registado_no_painel(base_dados, meta):
    meta["resposta"] = _Resposta(401, _erro(190))
    whatsapp.enviar_texto("41791234567", "olá")

    avarias = health.avarias_abertas()
    assert [a["chave"] for a in avarias] == [health.CHAVE_WHATSAPP_CONTA]
    assert "190" in avarias[0]["detalhe"]

    itens = op.attention_items()
    avaria = [i for i in itens if i["tipo"] == "avaria_sistema"]
    assert len(avaria) == 1
    assert avaria[0]["nivel"] == "agora"
    assert "WhatsApp" in avaria[0]["titulo"]


def test_401_avisa_a_daniela_uma_unica_vez(base_dados, meta):
    meta["resposta"] = _Resposta(401, _erro(190))
    for _ in range(5):
        whatsapp.enviar_texto("41791234567", "olá")

    avisos = [p for p in meta["enviados"] if p["to"] == config.PROVIDER_WHATSAPP]
    assert len(avisos) == 1, "a mesma avaria não pode inundar o telemóvel dela"
    assert "🚨" in avisos[0]["text"]["body"]
    # ...mas todas as ocorrências ficam contadas no painel.
    assert health.avarias_abertas()[0]["ocorrencias"] == 5


def test_registo_no_painel_acontece_mesmo_sem_conseguir_avisar(base_dados, meta, monkeypatch):
    """O ciclo óbvio: se o WhatsApp está em baixo, o aviso por WhatsApp
    também falha. O painel tem de ficar com o registo à mesma."""
    def _post_sempre_rebenta(*a, **kw):
        raise whatsapp.requests.RequestException("rede em baixo")

    meta["resposta"] = _Resposta(401, _erro(190))
    original = whatsapp.enviar

    def _enviar(payload):
        if payload.get("to") == config.PROVIDER_WHATSAPP:
            raise whatsapp.requests.RequestException("rede em baixo")
        return original(payload)

    monkeypatch.setattr(whatsapp, "enviar", _enviar)
    whatsapp.enviar_texto("41791234567", "olá")

    assert health.avarias_abertas()[0]["chave"] == health.CHAVE_WHATSAPP_CONTA


def test_payload_invalido_nao_gera_alarme(base_dados, meta):
    meta["resposta"] = _Resposta(400, _erro(100, "Invalid parameter"))
    whatsapp.enviar_texto("41791234567", "olá")

    assert health.avarias_abertas() == []
    assert [p for p in meta["enviados"] if p["to"] == config.PROVIDER_WHATSAPP] == []


def test_envio_bem_sucedido_fecha_a_avaria(base_dados, meta):
    meta["resposta"] = _Resposta(401, _erro(190))
    whatsapp.enviar_texto("41791234567", "olá")
    assert health.avarias_abertas()

    meta["resposta"] = _Resposta(200, {"messages": [{"id": "wamid.2"}]})
    whatsapp.enviar_texto("41791234567", "olá outra vez")
    assert health.avarias_abertas() == []
    assert not [i for i in op.attention_items() if i["tipo"] == "avaria_sistema"]


def test_avaria_resolvida_que_volta_avisa_de_novo(base_dados, meta):
    meta["resposta"] = _Resposta(401, _erro(190))
    whatsapp.enviar_texto("41791234567", "olá")
    meta["resposta"] = _Resposta(200, {"messages": [{"id": "wamid.2"}]})
    whatsapp.enviar_texto("41791234567", "olá")
    meta["resposta"] = _Resposta(401, _erro(190))
    whatsapp.enviar_texto("41791234567", "olá")

    avisos = [p for p in meta["enviados"] if p["to"] == config.PROVIDER_WHATSAPP]
    assert len(avisos) == 2


def test_numero_demo_nunca_chega_a_meta_nem_gera_avaria(base_dados, meta):
    meta["resposta"] = _Resposta(401, _erro(190))
    whatsapp.enviar_texto(config.DEMO_PHONE_PREFIX + "111", "olá")
    assert meta["enviados"] == []
    assert health.avarias_abertas() == []


# ---------------------------------------------------------------------------
# Automações paradas
# ---------------------------------------------------------------------------
def test_sem_nunca_ter_corrido_nao_ha_alarme(base_dados):
    """Instalação nova: ninguém chamou o cron ainda. Isso não é avaria."""
    assert health.minutos_sem_pulso(health.CHAVE_AUTOMACOES) is None
    assert not [i for i in op.attention_items() if i["tipo"] == "automacoes_paradas"]


def test_correr_as_automacoes_regista_o_pulso(cliente_http):
    r = cliente_http.post("/api/automacoes/correr", auth=("painel", "painel-pw"))
    assert r.status_code == 200
    assert health.ultimo_pulso(health.CHAVE_AUTOMACOES) is not None
    assert health.minutos_sem_pulso(health.CHAVE_AUTOMACOES) == 0


def test_painel_avisa_quando_o_cron_para(cliente_http):
    from datetime import timedelta

    cliente_http.post("/api/automacoes/correr", auth=("painel", "painel-pw"))
    assert not [i for i in op.attention_items() if i["tipo"] == "automacoes_paradas"]

    # Relógio adiantado 45 min — o cron corre de 5 em 5, portanto isto só
    # pode significar que deixou de correr.
    itens = op.attention_items(agora=tempo.agora_zurique() + timedelta(minutes=45))
    aviso = [i for i in itens if i["tipo"] == "automacoes_paradas"]
    assert len(aviso) == 1
    assert aviso[0]["nivel"] == "agora"
    assert "45" in aviso[0]["titulo"]


def test_anti_inundacao_respeita_a_janela(base_dados):
    """Fora da janela, a MESMA avaria volta a avisar — se ela não fez nada,
    o problema continua de pé e tem de voltar a aparecer."""
    from datetime import timedelta

    assert health.registar_avaria("teste.avaria", "Título", "detalhe") is True
    assert health.registar_avaria("teste.avaria", "Título", "detalhe") is False
    assert health.registar_avaria("teste.avaria", "Título", "detalhe",
                                  janela_min=0) is True
