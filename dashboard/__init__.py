"""
dashboard/ — o painel operacional (SaaS shell) servido a partir de
templates/ + static/, fora de bot.py.

Serve o shell (uma página) e o router client-side (static/dashboard/app.js)
consome as APIs JSON que já vivem em bot.py (/api/painel/hoje, /api/agendamentos,
/api/faturas, …). Este blueprint NÃO tem lógica de domínio.

Montado em /app. As rotas antigas /painel e /dashboard mantêm-se em bot.py até
esta versão estar validada.
"""

from __future__ import annotations

import hashlib
import hmac
import pathlib

from flask import Blueprint, Response, render_template, request

import config

bp = Blueprint("dashboard", __name__)

_STATIC = pathlib.Path(__file__).resolve().parent.parent / "static" / "dashboard"


def _asset_version() -> str:
    """Hash curto do css+js -> cache-bust automático quando algo muda."""
    h = hashlib.sha1()
    for nome in ("app.css", "app.js"):
        p = _STATIC / nome
        if p.exists():
            h.update(p.read_bytes())
    return h.hexdigest()[:8]


@bp.before_request
def _exige_auth():
    user, pw = config.DASHBOARD_USER, config.DASHBOARD_PASSWORD
    if not user or not pw:
        return Response("Painel não configurado.", 503)
    auth = request.authorization
    if (not auth or not hmac.compare_digest(auth.username or "", user)
            or not hmac.compare_digest(auth.password or "", pw)):
        return Response("Autenticacao necessaria.", 401,
                        {"WWW-Authenticate": 'Basic realm="Painel", charset="UTF-8"'})
    return None


@bp.route("/app")
@bp.route("/app/")
@bp.route("/app/<path:_resto>")
def shell(_resto: str = ""):
    return render_template("dashboard/shell.html",
                           business_name=config.BUSINESS_NAME or "Daniela Beauty",
                           asset_v=_asset_version())
