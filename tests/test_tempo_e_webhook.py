"""Testes 18, 19, 20: timezone Europe/Zurich e assinatura do webhook."""

import hashlib
import hmac
import json
from datetime import datetime

import bot
import tempo


def test_18_timezone_europe_zurich_e_dst():
    assert tempo.NOME_FUSO == "Europe/Zurich"
    agora = tempo.agora_zurique()
    assert agora.tzinfo is not None
    # Verão (agosto) -> UTC+2 ; Inverno (janeiro) -> UTC+1
    verao = datetime(2026, 8, 1, 12, 0, tzinfo=tempo.FUSO_ZURIQUE)
    inverno = datetime(2026, 1, 1, 12, 0, tzinfo=tempo.FUSO_ZURIQUE)
    assert verao.utcoffset().total_seconds() == 2 * 3600
    assert inverno.utcoffset().total_seconds() == 1 * 3600
    # combinar_local devolve hora de parede no fuso do salão
    dt = tempo.combinar_local("2026-07-15", "14:30")
    assert dt.hour == 14 and dt.utcoffset().total_seconds() == 2 * 3600
    # iso_utc é sempre com sufixo de fuso
    assert tempo.iso_utc().endswith("+00:00")


def _corpo():
    return json.dumps({"entry": [{"changes": [{"value": {"x": 1}}]}]}).encode()


def test_19_webhook_assinatura_invalida_rejeitada(cliente_http):
    r = cliente_http.post("/webhook", data=_corpo(), content_type="application/json",
                          headers={"X-Hub-Signature-256": "sha256=0000"})
    assert r.status_code == 403


def test_19b_webhook_sem_assinatura_rejeitado(cliente_http):
    r = cliente_http.post("/webhook", data=_corpo(), content_type="application/json")
    assert r.status_code == 403


def test_20_webhook_assinatura_valida_aceite(cliente_http):
    corpo = _corpo()
    assinatura = "sha256=" + hmac.new(b"segredo-de-teste", corpo, hashlib.sha256).hexdigest()
    r = cliente_http.post("/webhook", data=corpo, content_type="application/json",
                          headers={"X-Hub-Signature-256": assinatura})
    assert r.status_code == 200


def test_20b_verificacao_get_webhook(cliente_http):
    r = cliente_http.get("/webhook?hub.mode=subscribe&hub.verify_token=verify-de-teste&hub.challenge=xyz")
    assert r.status_code == 200 and r.data == b"xyz"
    r = cliente_http.get("/webhook?hub.mode=subscribe&hub.verify_token=errado&hub.challenge=xyz")
    assert r.status_code == 403
