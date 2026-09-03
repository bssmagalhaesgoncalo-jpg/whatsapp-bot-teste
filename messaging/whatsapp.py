"""
messaging/whatsapp.py — camada de saída para a WhatsApp Cloud API.

Ponto ÚNICO de envio. Sem credenciais configuradas não rebenta (útil em
testes e antes de configurar o ambiente); o token vai só no header, nunca
num log. A verificação da assinatura de ENTRADA fica em bot.verificar_assinatura.
"""

from __future__ import annotations

import logging

import requests

import config

log = logging.getLogger("whatsapp")


def enviar(payload: dict):
    """Faz POST do payload à Graph API. Devolve a Response, ou None se o
    WhatsApp não estiver configurado."""
    url = config.graph_url()
    token = config.WHATSAPP_TOKEN
    if not url or not token:
        log.info("WhatsApp não configurado (WHATSAPP_TOKEN / PHONE_NUMBER_ID) — envio ignorado")
        return None
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    try:
        r = requests.post(url, headers=headers, json=payload, timeout=10)
    except requests.RequestException as e:
        log.warning("Falha de rede ao enviar para a Meta: %s", e.__class__.__name__)
        raise
    if r.status_code >= 400:
        log.warning("Meta devolveu %s: %s", r.status_code, r.text[:400])
    else:
        log.info("Meta %s", r.status_code)
    return r


def enviar_texto(destinatario: str, texto: str):
    return enviar({
        "messaging_product": "whatsapp",
        "to": destinatario,
        "type": "text",
        "text": {"body": texto},
    })
