"""
core/seguranca.py — limite de tentativas de autenticação e headers de segurança.

Atrás do HTTP Basic do painel estão nomes, telemóveis, histórico e faturas de
clientes reais. Até aqui não havia limite de tentativas nem um único header de
segurança: uma password fraca era atacável à velocidade da rede.

Sem dependências novas de propósito (nada de Flask-Limiter/Talisman): o serviço
corre com UM worker, por isso um contador em memória é o estado partilhado por
todos os pedidos — e a lista de coisas que podem partir no dia do lançamento
fica mais curta.
"""

from __future__ import annotations

import logging
import threading
import time

log = logging.getLogger("seguranca")

# 10 tentativas FALHADAS por minuto e por IP. A Daniela engana-se na password
# uma ou duas vezes; um ataque de dicionário precisa de milhares.
TENTATIVAS_MAX = 10
JANELA_SEG = 60

# Teto de IPs em memória. Sem isto, um atacante a forjar X-Forwarded-For faria
# o dicionário crescer sem fim. Ao chegar ao teto limpa-se o que já expirou —
# e, se mesmo assim estiver cheio, o IP mais antigo sai.
MAX_IPS = 5_000

_LOCK = threading.Lock()
_tentativas: dict[str, list[float]] = {}


def ip_do_pedido(request) -> str:
    """IP do cliente. No Render o pedido chega por um proxy, por isso
    `remote_addr` é sempre o do proxy — o IP real vem no X-Forwarded-For.

    Um atacante pode forjar esse header e contornar o contador; é uma
    limitação assumida. A alternativa (contar só por `remote_addr`) seria
    pior: um único contador para o mundo inteiro atrás do proxy, e a
    primeira tentativa falhada de um bot qualquer trancava a Daniela fora
    do painel dela.
    """
    encaminhado = (request.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
    return encaminhado or (request.remote_addr or "desconhecido")


def _limpar(agora: float) -> None:
    """Fora do _LOCK não se chama isto — assume que já está seguro."""
    for ip in [ip for ip, marcas in _tentativas.items()
               if not marcas or marcas[-1] <= agora - JANELA_SEG]:
        _tentativas.pop(ip, None)
    if len(_tentativas) >= MAX_IPS:
        _tentativas.pop(next(iter(_tentativas)), None)


def excedeu_tentativas(ip: str, agora: float | None = None) -> bool:
    """True se este IP já gastou as tentativas desta janela."""
    agora = agora if agora is not None else time.monotonic()
    with _LOCK:
        marcas = [t for t in _tentativas.get(ip, []) if t > agora - JANELA_SEG]
        if marcas:
            _tentativas[ip] = marcas
        return len(marcas) >= TENTATIVAS_MAX


def registar_falha(ip: str, agora: float | None = None) -> None:
    """Conta uma tentativa FALHADA. Um login bem-sucedido não conta — quem
    sabe a password não tem de ser travado."""
    agora = agora if agora is not None else time.monotonic()
    with _LOCK:
        if len(_tentativas) >= MAX_IPS and ip not in _tentativas:
            _limpar(agora)
        marcas = [t for t in _tentativas.get(ip, []) if t > agora - JANELA_SEG]
        marcas.append(agora)
        _tentativas[ip] = marcas
        if len(marcas) == TENTATIVAS_MAX:
            log.warning("IP %s atingiu o limite de tentativas de autenticação", ip)


def limpar_tudo() -> None:
    """Só para a suite de testes — o contador é estado de processo e passaria
    de um teste para o seguinte."""
    with _LOCK:
        _tentativas.clear()


# ---------------------------------------------------------------------------
# Headers de segurança
# ---------------------------------------------------------------------------
# CSP desenhada para o painel REAL, não para um exemplo:
#   • script-src 'self' — o app.js é servido do próprio domínio e não há um
#     único <script> inline no shell.html. Aqui pode ser estrito.
#   • style-src precisa de 'unsafe-inline': o app.js constrói os elementos com
#     h("div", {style: "..."}) em dezenas de sítios e as fontes vêm do Google
#     Fonts (o único terceiro). Tirar o 'unsafe-inline' exigia reescrever o
#     app.js todo — fica para quando houver build; o risco de XSS por estilo é
#     muito menor do que por script, que continua fechado.
#   • frame-ancestors 'none' duplica o X-Frame-Options (os browsers antigos só
#     percebem o segundo).
#   • object-src 'none' + base-uri 'self' fecham dois vetores clássicos.
# O PDF público da fatura (/faturas/pdf/<token>) é servido inline e abre
# sempre numa janela normal, nunca embebido — nada disto lhe toca.
CSP = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
    "font-src 'self' https://fonts.gstatic.com data:; "
    "img-src 'self' data: blob:; "
    "connect-src 'self'; "
    "object-src 'none'; "
    "base-uri 'self'; "
    "form-action 'self'; "
    "frame-ancestors 'none'"
)

HEADERS = {
    # Um ano, com subdomínios. O Render serve sempre por HTTPS.
    "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Content-Security-Policy": CSP,
}


def aplicar_headers(resposta):
    """Aplica os headers sem esmagar um que a rota tenha definido de
    propósito."""
    for nome, valor in HEADERS.items():
        resposta.headers.setdefault(nome, valor)
    return resposta
