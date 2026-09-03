"""
Configuração central — tudo vem do AMBIENTE, nada de segredos no código.

Ao contrário da versão antiga (que trazia PHONE_NUMBER_ID, PROVIDER_WHATSAPP e
VERIFY_TOKEN="teste123" embutidos no ficheiro), aqui não há defaults sensíveis.
Uma variável em falta fica a None e as rotas que dela dependem falham de forma
controlada (ver `bot.configuracao_em_falta`).

Ver `.env.example` para a lista completa.
"""

from __future__ import annotations

import os


def _limpo(nome: str):
    valor = os.environ.get(nome)
    if valor is None:
        return None
    valor = valor.strip()
    return valor or None


# --- Identidade do negócio -------------------------------------------------
BUSINESS_NAME = _limpo("BUSINESS_NAME") or "Daniela Beauty"
BUSINESS_ADDRESS = _limpo("BUSINESS_ADDRESS") or ""

# --- WhatsApp Cloud API (Meta) — SEM defaults -----------------------------
WHATSAPP_TOKEN = _limpo("WHATSAPP_TOKEN")
PHONE_NUMBER_ID = _limpo("PHONE_NUMBER_ID")
VERIFY_TOKEN = _limpo("VERIFY_TOKEN")
PROVIDER_WHATSAPP = _limpo("PROVIDER_WHATSAPP")
# App Secret da app Meta — usado para validar a assinatura X-Hub-Signature-256
# dos webhooks. Sem ele, a validação de assinatura fica em modo "aviso" (ver
# bot.verificar_assinatura) — aceitável em desenvolvimento, NÃO em produção.
APP_SECRET = _limpo("APP_SECRET")

GRAPH_API_VERSION = _limpo("GRAPH_API_VERSION") or "v21.0"

# --- Painel / dashboard (HTTP Basic) — SEM defaults ----------------------
DASHBOARD_USER = _limpo("DASHBOARD_USER")
DASHBOARD_PASSWORD = _limpo("DASHBOARD_PASSWORD")

# --- Infra ---------------------------------------------------------------
# Produção: Postgres via DATABASE_URL. Desenvolvimento/local: SQLite.
DATABASE_URL = _limpo("DATABASE_URL")
SQLITE_PATH = _limpo("SESSOES_DB") or "sessoes.db"
MEDIA_DIR = _limpo("MEDIA_DIR") or "media_pedidos"
PUBLIC_BASE_URL = (_limpo("PUBLIC_BASE_URL") or "").rstrip("/")

# Reter o horário assim que é escolhido, enquanto o cliente confirma.
RESERVA_TEMPORARIA_MINUTOS = int(_limpo("RESERVA_TEMPORARIA_MINUTOS") or "15")

# --- Comportamento do fluxo de marcação ---------------------------------
# False (por agora): marcações do bot entram já como "confirmed".
# Ligar mais tarde não exige refactor: `bot.estado_inicial_marcacao()` já
# devolve "pending" quando isto for True.
BOOKING_REQUIRES_APPROVAL = (_limpo("BOOKING_REQUIRES_APPROVAL") or "false").lower() in (
    "1", "true", "yes", "sim", "on",
)


def graph_url() -> str | None:
    if not PHONE_NUMBER_ID:
        return None
    return f"https://graph.facebook.com/{GRAPH_API_VERSION}/{PHONE_NUMBER_ID}/messages"


def usa_postgres() -> bool:
    return bool(DATABASE_URL and DATABASE_URL.startswith(("postgres://", "postgresql://")))


def em_falta_para_whatsapp() -> list[str]:
    """Nomes das variáveis obrigatórias para o bot WhatsApp funcionar."""
    faltam = []
    for nome, valor in (
        ("WHATSAPP_TOKEN", WHATSAPP_TOKEN),
        ("PHONE_NUMBER_ID", PHONE_NUMBER_ID),
        ("VERIFY_TOKEN", VERIFY_TOKEN),
        ("PROVIDER_WHATSAPP", PROVIDER_WHATSAPP),
    ):
        if not valor:
            faltam.append(nome)
    return faltam


def em_falta_para_painel() -> list[str]:
    return [n for n, v in (("DASHBOARD_USER", DASHBOARD_USER),
                           ("DASHBOARD_PASSWORD", DASHBOARD_PASSWORD)) if not v]
