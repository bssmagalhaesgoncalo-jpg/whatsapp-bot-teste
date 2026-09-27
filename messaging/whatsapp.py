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

# Códigos de erro da Graph API que significam "a CONTA/configuração está em
# baixo" — nenhuma mensagem sai enquanto isto durar, seja para quem for.
# (Um 4xx de payload inválido é outra coisa: falha só aquela mensagem.)
_CODIGOS_CONTA = {
    190,        # token expirado / invalidado
    102,        # sessão inválida
    10, 200,    # falta de permissão na app ou no número
    131031,     # conta bloqueada/suspensa
    131042,     # problema de pagamento na conta business
    368,        # bloqueio temporário por violação de políticas
}
_CODIGOS_LIMITE = {
    4,          # limite de chamadas da app
    80007,      # limite de volume
    130429,     # rate limit da Cloud API
    131048,     # limite anti-spam
    131056,     # limite por par (nós ↔ este número)
}

# "Pode haver uma avaria aberta que já não existe." Começa a True para que o
# primeiro envio bem-sucedido depois de um restart limpe um aviso antigo já
# resolvido (o token foi renovado enquanto o processo estava em baixo).
_talvez_avariado = True

# Um aviso de avaria é enviado por este mesmo módulo. Sem esta guarda, o
# aviso a falhar voltaria a entrar aqui — e o 401 do aviso geraria outro
# aviso, indefinidamente.
_a_avisar = False


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
        resposta_demo = _RespostaDemo()
        _registar_conversa(payload, resposta_demo)
        return resposta_demo
    url = config.graph_url()
    token = config.WHATSAPP_TOKEN
    if not url or not token:
        log.info("WhatsApp não configurado (WHATSAPP_TOKEN / PHONE_NUMBER_ID) — envio ignorado")
        _registar_conversa(payload, None)
        return None
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    try:
        r = requests.post(url, headers=headers, json=payload, timeout=10)
    except requests.RequestException as e:
        log.warning("Falha de rede ao enviar para a Meta: %s", e.__class__.__name__)
        raise
    _registar_conversa(payload, r)
    if r.status_code >= 400:
        log.warning("Meta devolveu %s: %s", r.status_code, r.text[:400])
        _tratar_falha(r)
    else:
        log.info("Meta %s", r.status_code)
        _marcar_canal_saudavel()
    return r


def _registar_conversa(payload: dict, resposta) -> None:
    """Grava a mensagem que sai no registo de conversas (messaging/conversas.py).

    Aqui, e só aqui, porque `enviar` é o ponto ÚNICO de saída — texto,
    botões, listas, documentos, templates e o composer do painel passam
    todos por cima desta linha. Um destinatário DEMO grava-se como os
    outros: é isso que torna o fluxo testável sem mandar nada a sério.

    Nunca levanta: ver conversas._gravar. A duplicação do try/except é
    barata e garante que nem um erro de IMPORT deste módulo (um typo numa
    edição futura) deixa uma cliente sem resposta."""
    try:
        from messaging import conversas
        conversas.registar_enviada(payload, resposta)
    except Exception:                       # noqa: BLE001
        log.warning("falhou o registo da mensagem enviada (o envio seguiu)", exc_info=True)


def _codigo_erro(resposta) -> tuple[int | None, str]:
    """(código, mensagem) do corpo de erro da Graph API. O corpo pode nem ser
    JSON (um proxy pelo meio, um 502 em HTML) — nesse caso fica só o texto."""
    try:
        erro = (resposta.json() or {}).get("error") or {}
    except (ValueError, AttributeError):
        return None, (getattr(resposta, "text", "") or "")[:200]
    codigo = erro.get("code")
    detalhe = erro.get("error_user_msg") or erro.get("message") or ""
    try:
        codigo = int(codigo) if codigo is not None else None
    except (TypeError, ValueError):
        codigo = None
    return codigo, str(detalhe)[:200]


def classificar_falha(status_code: int, codigo: int | None) -> str | None:
    """Chave de avaria, ou None se o 4xx é problema daquela mensagem.

    A distinção que interessa: um payload inválido falha uma mensagem e o
    resto do bot continua a funcionar; um token expirado ou um número
    suspenso deixa o bot MUDO para toda a gente — e aí é preciso avisar."""
    from core import health
    if codigo in _CODIGOS_LIMITE:
        return health.CHAVE_WHATSAPP_LIMITE
    if codigo in _CODIGOS_CONTA:
        return health.CHAVE_WHATSAPP_CONTA
    # 401/403 sem código reconhecido continuam a ser autenticação/permissão:
    # a Meta muda os subcódigos com o tempo, o significado do status não.
    if status_code in (401, 403):
        return health.CHAVE_WHATSAPP_CONTA
    if status_code == 429:
        return health.CHAVE_WHATSAPP_LIMITE
    return None


_TITULOS = {
    "whatsapp.conta": "O WhatsApp está a recusar os envios",
    "whatsapp.limite": "Limite de envios do WhatsApp atingido",
}


def _tratar_falha(resposta) -> None:
    """Regista a avaria no painel e, se for caso disso, avisa a Daniela.

    Nunca deixa rebentar: uma falha a registar a falha não pode impedir o
    resto do request (a marcação da cliente já foi gravada)."""
    global _talvez_avariado
    if _a_avisar:
        return
    try:
        from core import health
        codigo, detalhe = _codigo_erro(resposta)
        chave = classificar_falha(resposta.status_code, codigo)
        if not chave:
            return
        _talvez_avariado = True
        titulo = _TITULOS.get(chave, "Problema no envio de WhatsApp")
        detalhe_completo = f"HTTP {resposta.status_code}" + (f" · código {codigo}" if codigo else "")
        if detalhe:
            detalhe_completo += f" · {detalhe}"
        # O painel primeiro, SEMPRE: se o canal está em baixo, o aviso por
        # WhatsApp também não chega — o registo visível é a única garantia.
        if health.registar_avaria(chave, titulo, detalhe_completo):
            _avisar_daniela(titulo, detalhe_completo)
    except Exception:                       # noqa: BLE001
        log.exception("falhou o registo da avaria de envio")


def _avisar_daniela(titulo: str, detalhe: str) -> None:
    global _a_avisar
    if not config.PROVIDER_WHATSAPP:
        return
    _a_avisar = True
    try:
        enviar_texto(config.PROVIDER_WHATSAPP,
                     f"🚨 *{titulo}*\n\n{detalhe}\n\n"
                     "As clientes que escreverem agora não recebem resposta. "
                     "Vê o painel — o aviso está lá em «Precisa da tua atenção».")
    except Exception:                       # noqa: BLE001
        # Esperado quando o canal está mesmo em baixo. O painel já tem o registo.
        log.warning("não foi possível avisar por WhatsApp — o painel tem o registo")
    finally:
        _a_avisar = False


def _marcar_canal_saudavel() -> None:
    """Um envio bem-sucedido fecha as avarias do canal: se voltou a
    funcionar, o cartão do painel desaparece sem ela ter de o dispensar."""
    global _talvez_avariado
    if not _talvez_avariado or _a_avisar:
        return
    try:
        from core import health
        health.resolver(health.CHAVE_WHATSAPP_CONTA)
        health.resolver(health.CHAVE_WHATSAPP_LIMITE)
        _talvez_avariado = False
    except Exception:                       # noqa: BLE001
        log.exception("falhou a limpeza das avarias de envio")


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


# ---------------------------------------------------------------------------
# Download de media RECEBIDA (fotos que a cliente envia — bloco 3)
# ---------------------------------------------------------------------------
def descarregar_media(media_id: str) -> tuple[bytes, str] | None:
    """Vai buscar o conteúdo de uma imagem recebida à Graph API.

    Dois passos, como a Meta exige: GET /<media_id> devolve um URL
    temporário (~5 min) e o download desse URL precisa do MESMO token no
    header. Devolve (bytes, mime) ou None — nunca levanta: uma foto que não
    se consegue descarregar não pode partir o webhook, o fluxo trata None
    como "não veio nada". O limite de tamanho fica para quem grava
    (crm/registos.py), que é quem conhece a política."""
    token = config.WHATSAPP_TOKEN
    if not token or not media_id:
        return None
    headers = {"Authorization": f"Bearer {token}"}
    base = f"https://graph.facebook.com/{config.GRAPH_API_VERSION}"
    try:
        meta = requests.get(f"{base}/{media_id}", headers=headers, timeout=10)
        if meta.status_code >= 400:
            log.warning("media %s: Meta devolveu %s ao pedir o URL", media_id, meta.status_code)
            return None
        info = meta.json()
        url = info.get("url")
        if not url:
            return None
        ficheiro = requests.get(url, headers=headers, timeout=30)
        if ficheiro.status_code >= 400:
            log.warning("media %s: download devolveu %s", media_id, ficheiro.status_code)
            return None
        mime = info.get("mime_type") or ficheiro.headers.get("Content-Type", "")
        return ficheiro.content, mime
    except requests.RequestException as e:
        log.warning("media %s: falha de rede (%s)", media_id, e.__class__.__name__)
        return None
