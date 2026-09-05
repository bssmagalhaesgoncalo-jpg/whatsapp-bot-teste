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


class _RespostaDemo:
    """Devolvida em vez de uma `requests.Response` real quando o destinatário
    é um número DEMO — nunca se chama `requests.post` para este prefixo."""
    status_code = 200
    text = '{"demo": true}'

    def json(self):
        return {"demo": True}


def _e_telefone_demo(destinatario) -> bool:
    return bool(destinatario) and str(destinatario).startswith(config.DEMO_PHONE_PREFIX)


def enviar(payload: dict):
    """Faz POST do payload à Graph API. Devolve a Response, ou None se o
    WhatsApp não estiver configurado.

    Ponto ÚNICO de saída: todo o envio (texto, listas, botões) passa por
    aqui, incluindo o composer do Client Manager. Um destinatário DEMO
    (`config.DEMO_PHONE_PREFIX`) NUNCA chega a `requests.post` — devolve-se
    uma resposta sintética de sucesso, para o resto do código continuar a
    funcionar normalmente em QA sem qualquer risco de enviar uma mensagem
    real a um número de teste."""
    if _e_telefone_demo(payload.get("to")):
        log.info("Destinatário demo — envio real ignorado (nunca chama a Meta)")
        return _RespostaDemo()
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


def enviar_documento(destinatario: str, link: str, filename: str | None = None,
                     caption: str | None = None):
    """Documento por LINK público (ex.: o PDF de uma fatura) — evita o upload
    em dois passos da Graph API. Passa sempre por `enviar()`: DEMO nunca
    chega à Meta, tal como o texto."""
    documento = {"link": link}
    if filename:
        documento["filename"] = filename
    if caption:
        documento["caption"] = caption
    return enviar({
        "messaging_product": "whatsapp",
        "to": destinatario,
        "type": "document",
        "document": documento,
    })


def enviar_template(destinatario: str, template_name: str, idioma_meta: str,
                    parametros_corpo: list[str] | None = None,
                    botoes_payload: list[str] | None = None):
    """Mensagem de TEMPLATE (pré-aprovada pela Meta) — o único tipo permitido
    para uma mensagem PROATIVA fora da janela de 24h de atendimento (ver
    notifications/reminders.py, P1). Passa sempre por `enviar()`: DEMO nunca
    chega à Meta, tal como o texto/documento.

    `parametros_corpo`: valores, por ordem, das variáveis {{1}}, {{2}}... do
    corpo do template já aprovado.
    `botoes_payload`: um payload por botão de "quick reply" do template, na
    MESMA ordem em que foram aprovados — é este payload que volta no webhook
    (mensagem tipo "button") quando o cliente toca, exatamente como um
    button_reply de uma mensagem interativa nossa."""
    componentes = []
    if parametros_corpo:
        componentes.append({
            "type": "body",
            "parameters": [{"type": "text", "text": str(v)} for v in parametros_corpo],
        })
    for indice, payload in enumerate(botoes_payload or []):
        componentes.append({
            "type": "button", "sub_type": "quick_reply", "index": str(indice),
            "parameters": [{"type": "payload", "payload": payload}],
        })
    template = {"name": template_name, "language": {"code": idioma_meta}}
    if componentes:
        template["components"] = componentes
    return enviar({
        "messaging_product": "whatsapp",
        "to": destinatario,
        "type": "template",
        "template": template,
    })
