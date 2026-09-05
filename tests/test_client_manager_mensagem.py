"""POST /api/clientes/<id>/mensagem — composer do Client Manager.

Cobre: autenticação, 404 cliente inexistente, 400 texto vazio/demasiado
longo, telefone NUNCA vindo do corpo do pedido, janela de 24h respeitada
para clientes reais, ZERO chamadas ao provider para clientes DEMO, e o
registo de um evento `message.manual_sent` por envio bem-sucedido.

O ponto de verificação real do "zero envios" é `requests.post` (a fronteira
HTTP dentro de messaging/whatsapp.py) — não o wrapper `bot.enviar_texto` —
porque é exatamente aí que vive o guard do prefixo DEMO (messaging/whatsapp.py)."""

import base64

import bot
import db
from conftest import marcar

AUTH = {"Authorization": "Basic " + base64.b64encode(b"painel:painel-pw").decode()}
PREFIXO = bot.DEMO_TELEFONE_PREFIXO


def _sem_post_real(monkeypatch):
    chamadas = []

    class _RespostaFalsa:
        status_code = 200
        text = "{}"

    def _post_falso(url, headers=None, json=None, timeout=None):
        chamadas.append((url, json))
        return _RespostaFalsa()

    monkeypatch.setattr(bot._wa.requests, "post", _post_falso)
    monkeypatch.setattr(bot._wa.config, "WHATSAPP_TOKEN", "token-de-teste")
    monkeypatch.setattr(bot._wa.config, "PHONE_NUMBER_ID", "123456")
    return chamadas


def _customer_id_de(telefone):
    for c in db.listar_customers():
        if c["phone"] == telefone:
            return c["id"]
    raise AssertionError(f"cliente {telefone} não encontrado")


def test_exige_autenticacao(cliente_http, base_dados):
    r = cliente_http.post("/api/clientes/1/mensagem", json={"texto": "Olá"})
    assert r.status_code == 401


def test_404_cliente_inexistente(cliente_http, base_dados):
    r = cliente_http.post("/api/clientes/999999/mensagem", json={"texto": "Olá"}, headers=AUTH)
    assert r.status_code == 404


def test_400_texto_vazio(cliente_http, base_dados, monkeypatch):
    chamadas = _sem_post_real(monkeypatch)
    marcar("41780001111", "limpeza_pele", "07.09.2026 (seg)", "10:00", nome="Cliente Real")
    cid = _customer_id_de("41780001111")

    r = cliente_http.post(f"/api/clientes/{cid}/mensagem", json={"texto": "   "}, headers=AUTH)
    assert r.status_code == 400
    assert chamadas == []


def test_400_texto_demasiado_longo(cliente_http, base_dados, monkeypatch):
    chamadas = _sem_post_real(monkeypatch)
    marcar("41780001112", "limpeza_pele", "07.09.2026 (seg)", "10:00", nome="Cliente Real")
    cid = _customer_id_de("41780001112")

    r = cliente_http.post(f"/api/clientes/{cid}/mensagem", json={"texto": "x" * 1001}, headers=AUTH)
    assert r.status_code == 400
    assert chamadas == []


def test_corpo_nao_escolhe_telefone(cliente_http, base_dados, monkeypatch):
    """O corpo do pedido pode tentar mandar um telefone — é sempre ignorado;
    quem manda é o telefone real do cliente (customer_id -> bd.obter_customer)."""
    chamadas = _sem_post_real(monkeypatch)
    marcar("41780001113", "limpeza_pele", "07.09.2026 (seg)", "10:00", nome="Cliente Real")
    cid = _customer_id_de("41780001113")
    bot.registar_interacao_cliente("41780001113")

    r = cliente_http.post(f"/api/clientes/{cid}/mensagem",
                           json={"texto": "Confirmo o seu horário.", "telefone": "41999999999"},
                           headers=AUTH)
    assert r.status_code == 200
    assert len(chamadas) == 1
    assert chamadas[0][1]["to"] == "41780001113"


def test_cliente_real_dentro_da_janela_24h_envia_uma_vez(cliente_http, base_dados, monkeypatch):
    chamadas = _sem_post_real(monkeypatch)
    marcar("41780001114", "limpeza_pele", "07.09.2026 (seg)", "10:00", nome="Cliente Real")
    cid = _customer_id_de("41780001114")
    bot.registar_interacao_cliente("41780001114")   # entra na janela de 24h

    r = cliente_http.post(f"/api/clientes/{cid}/mensagem", json={"texto": "Relembro a sua marcação."},
                           headers=AUTH)
    assert r.status_code == 200
    j = r.get_json()
    assert j["ok"] is True
    assert j["demo"] is False
    assert len(chamadas) == 1
    assert chamadas[0][1]["text"]["body"] == "Relembro a sua marcação."

    envios = [e for e in db.eventos_da_entidade("customer", cid) if e["type"] == "message.manual_sent"]
    assert len(envios) == 1


def test_cliente_real_fora_da_janela_24h_bloqueia(cliente_http, base_dados, monkeypatch):
    chamadas = _sem_post_real(monkeypatch)
    marcar("41780001115", "limpeza_pele", "07.09.2026 (seg)", "10:00", nome="Cliente Real")
    cid = _customer_id_de("41780001115")
    # nunca interagiu -> fora da janela de 24h

    r = cliente_http.post(f"/api/clientes/{cid}/mensagem", json={"texto": "Olá"}, headers=AUTH)
    assert r.status_code == 409
    assert chamadas == []
    envios = [e for e in db.eventos_da_entidade("customer", cid) if e["type"] == "message.manual_sent"]
    assert envios == []


def test_cliente_demo_zero_chamadas_ao_provider(cliente_http, base_dados, monkeypatch):
    chamadas = _sem_post_real(monkeypatch)
    telefone_demo = f"{PREFIXO}0001"
    marcar(telefone_demo, "limpeza_pele", "07.09.2026 (seg)", "10:00", nome="Cliente Demo")
    cid = _customer_id_de(telefone_demo)
    # propositadamente NÃO chama registar_interacao_cliente: um cliente demo
    # tem de funcionar mesmo fora da janela de 24h (é só para QA).

    r = cliente_http.post(f"/api/clientes/{cid}/mensagem", json={"texto": "Mensagem de teste"},
                           headers=AUTH)
    assert r.status_code == 200
    j = r.get_json()
    assert j["ok"] is True
    assert j["demo"] is True
    assert chamadas == []   # NUNCA chega à Meta

    envios = [e for e in db.eventos_da_entidade("customer", cid) if e["type"] == "message.manual_sent"]
    assert len(envios) == 1
