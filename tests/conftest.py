"""
Fixtures da suite. Cada teste corre contra uma base de dados SQLite NOVA
(ficheiro temporário) já migrada, sem tocar em nada real.

ISOLAMENTO DO AMBIENTE (patch de segurança — ver skill-observations #4/#5):
o shell onde os testes correm pode legitimamente ter as credenciais REAIS
do projeto exportadas (é o MESMO shell usado para correr `flask run`
durante o desenvolvimento) — DASHBOARD_USER/PASSWORD, WHATSAPP_TOKEN,
PHONE_NUMBER_ID, etc. Se este ficheiro se limitasse a
`os.environ.setdefault(...)`, essas variáveis JÁ presentes venceriam
sempre e a suite passaria a correr com credenciais reais em vez das
constantes que os testes esperam — foi exatamente isto que causou 151
falhas "fantasma" (todas com a mesma assinatura: 401, porque
DASHBOARD_USER/PASSWORD reais != "painel"/"painel-pw" que os testes usam),
e nalguns casos chegou a fazer chamadas REAIS à Meta Graph API com um
token real herdado do ambiente (só não enviou nada porque esse token
estava inválido).

Por isso, abaixo:

  1. TODAS as variáveis que config.py lê do ambiente são apagadas e
     substituídas pelos valores de TESTE — de forma INCONDICIONAL (nunca
     setdefault), ANTES de "config"/"bot" serem importados pela primeira
     vez (a leitura é feita uma única vez, ao nível do módulo — ver
     config.py). Nenhuma fica "herdada" do shell:
       • WHATSAPP_TOKEN/PHONE_NUMBER_ID/PROVIDER_WHATSAPP e todos os
         WHATSAPP_*_TEMPLATE_* ficam de propósito por configurar (None) —
         messaging.whatsapp.enviar() já trata "não configurado" como um
         no-op seguro; um teste que precise de simular um envio mocka
         bot._wa.requests.post e define os seus próprios valores fake
         (ver tests/test_reminder_24h.py, tests/test_campanhas.py).
       • DASHBOARD_USER/PASSWORD/APP_SECRET/VERIFY_TOKEN ficam fixos nas
         constantes que todos os testes já assumem.
       • DATABASE_URL fica sempre vazia (SQLite local) e SESSOES_DB aponta
         para um caminho-sentinela que nunca chega a ser aberto (a
         fixture `base_dados`, abaixo, substitui SEMPRE por um ficheiro
         tmp_path por teste) — só existe para nunca apontar, nem por
         omissão, para a base de dados real do negócio.
  2. Como segunda camada INDEPENDENTE (mesmo que o isolamento acima
     falhasse por algum motivo): a fixture `_bloquear_rede_externa`,
     autouse, bloqueia qualquer chamada de rede real durante toda a
     suite. Um teste que precise de simular uma chamada HTTP mocka
     requests.post/get explicitamente (padrão já usado em toda a suite);
     esse mock substitui a função ANTES de esta rede-fechada ser
     alcançada, por isso os dois mecanismos nunca colidem.

Ordem de import auditada: conftest.py é sempre o primeiro ficheiro que o
pytest importa em tests/ (não há pytest.ini/pyproject.toml/setup.cfg com
plugins que importem módulos do projeto antes disto) — o bloco de
isolamento abaixo corre sempre antes de "import config"."""

from __future__ import annotations

import os
import sys
import pathlib
import tempfile

import pytest

RAIZ = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))

# ---------------------------------------------------------------------------
# 1. Isolamento do ambiente — TEM de correr antes de "import config".
# ---------------------------------------------------------------------------
_ENV_VARS_DA_APP = (
    "BUSINESS_NAME", "BUSINESS_ADDRESS",
    "WHATSAPP_TOKEN", "PHONE_NUMBER_ID", "VERIFY_TOKEN", "PROVIDER_WHATSAPP",
    "APP_SECRET", "GRAPH_API_VERSION",
    "WHATSAPP_REMINDER_TEMPLATE_PT", "WHATSAPP_REMINDER_TEMPLATE_DE", "WHATSAPP_REMINDER_TEMPLATE_EN",
    "WHATSAPP_REBOOKING_TEMPLATE_PT", "WHATSAPP_REBOOKING_TEMPLATE_DE", "WHATSAPP_REBOOKING_TEMPLATE_EN",
    "WHATSAPP_RESCHEDULE_TEMPLATE_PT", "WHATSAPP_RESCHEDULE_TEMPLATE_DE", "WHATSAPP_RESCHEDULE_TEMPLATE_EN",
    "WHATSAPP_CAMPAIGN_TEMPLATE_PT", "WHATSAPP_CAMPAIGN_TEMPLATE_DE", "WHATSAPP_CAMPAIGN_TEMPLATE_EN",
    "CAMPAIGN_SEND_BATCH_SIZE",
    "DASHBOARD_USER", "DASHBOARD_PASSWORD",
    "DATABASE_URL", "SESSOES_DB", "MEDIA_DIR", "PUBLIC_BASE_URL",
    "RESERVA_TEMPORARIA_MINUTOS", "BOOKING_REQUIRES_APPROVAL", "ENABLE_DEMO_SEED",
    "ESTIMATED_MINUTES_SAVED_PER_AUTOMATION",
)
# Nenhuma destas fica "herdada" do shell — cada uma é apagada primeiro,
# nunca só complementada (ver docstring: setdefault era precisamente o bug).
for _nome in _ENV_VARS_DA_APP:
    os.environ.pop(_nome, None)

os.environ["APP_SECRET"] = "segredo-de-teste"
os.environ["VERIFY_TOKEN"] = "verify-de-teste"
os.environ["BUSINESS_NAME"] = "Daniela Beauty"
os.environ["BUSINESS_ADDRESS"] = "Rua de Teste 1, Visp"
os.environ["DASHBOARD_USER"] = "painel"
os.environ["DASHBOARD_PASSWORD"] = "painel-pw"
os.environ["ENABLE_DEMO_SEED"] = "false"
os.environ["BOOKING_REQUIRES_APPROVAL"] = "false"
# Sentinela óbvia — nunca chega a ser aberta de facto: a fixture
# `base_dados` substitui SEMPRE config.SQLITE_PATH por um tmp_path.
os.environ["SESSOES_DB"] = str(pathlib.Path(tempfile.gettempdir()) / "bms-tests-NUNCA-USAR-sessoes-reais.db")
# WHATSAPP_TOKEN / PHONE_NUMBER_ID / PROVIDER_WHATSAPP / DATABASE_URL e
# todos os WHATSAPP_*_TEMPLATE_* ficam de propósito por definir (None) —
# ver docstring do módulo.

import config          # noqa: E402,F401
import db              # noqa: E402
import catalogo        # noqa: E402
import bot             # noqa: E402,F401
import requests        # noqa: E402


# ---------------------------------------------------------------------------
# 2. Rede-fechada por omissão — segunda camada, independente da de cima.
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _bloquear_rede_externa(monkeypatch):
    """Qualquer chamada de rede real (requests.get/post/...) passa sempre
    por requests.sessions.Session.request — bloqueá-lo aqui cobre TODOS os
    verbos de uma só vez. Um teste que precise de simular uma resposta
    mocka requests.post/get (ou bot._wa.requests.post) explicitamente ANTES
    de chamar o código sob teste — isso substitui a função em si, nunca
    chega a esta proteção, por isso os dois mecanismos nunca colidem (ver
    ex. tests/test_reminder_24h.py:_mock_provider). O cliente de testes do
    Flask (`cliente_http`) nunca passa por aqui: é despacho WSGI direto,
    não usa a biblioteca requests."""
    def _bloqueado(self, method, url, *args, **kwargs):
        raise RuntimeError(
            f"Chamada de rede externa bloqueada durante os testes: {method} {url}. "
            "Mock requests.post/get explicitamente neste teste (ver tests/test_reminder_24h.py).")
    monkeypatch.setattr(requests.sessions.Session, "request", _bloqueado)


@pytest.fixture()
def base_dados(tmp_path, monkeypatch):
    """BD limpa e migrada para este teste."""
    caminho = str(tmp_path / "teste.db")
    monkeypatch.setattr(config, "SQLITE_PATH", caminho)
    monkeypatch.setattr(bot, "DB_PATH", caminho, raising=False)
    db.resetar_estado_migracao_para_testes()
    db.migrar()
    yield caminho
    db.resetar_estado_migracao_para_testes()


@pytest.fixture()
def cliente_http(base_dados):
    bot.app.config["TESTING"] = True
    return bot.app.test_client()


def marcar(telefone, servico_id, data_texto, hora_texto, nome="Cliente Teste"):
    """Cria uma marcação confirmada diretamente (como o fluxo do bot faria),
    devolvendo o id. Levanta HorarioOcupado em conflito."""
    servico = db.obter_servico(servico_id)
    sessao = {
        "idioma": "pt", "nome": nome,
        "servico_id": servico_id,
        "servico": catalogo.nome_pt(servico),
        "duracao": catalogo.duracao_label(servico["duracao_min"]),
        "duracao_min": servico["duracao_min"],
        "preco_cents": servico["preco_cents"],
        "preco": round(servico["preco_cents"] / 100, 2) if servico["preco_cents"] is not None else None,
        "data": data_texto, "hora": hora_texto,
    }
    return bot.guardar_agendamento(telefone, sessao)


def data_pt(iso):
    """'2026-09-07' -> '07.09.2026 (seg)'"""
    from datetime import date
    d = date.fromisoformat(iso)
    return f"{d.strftime('%d.%m.%Y')} ({bot.DIAS_SEMANA['pt'][d.weekday()]})"
