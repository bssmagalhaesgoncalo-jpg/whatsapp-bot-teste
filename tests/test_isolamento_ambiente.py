"""Patch de segurança — isolar a suite pytest do ambiente real do
utilizador (ver skill-observations #4/#5).

Contexto: tests/conftest.py costumava usar `os.environ.setdefault(...)`
para as credenciais de teste — um shell com as credenciais REAIS do
projeto já exportadas (o mesmo usado para correr `flask run` durante o
desenvolvimento) vencia sempre, fazendo a suite correr com
DASHBOARD_USER/PASSWORD reais (todos os testes ficavam com 401 contra o
"painel"/"painel-pw" hardcoded) e, nalguns casos, tentar chamadas REAIS à
Meta Graph API com um WHATSAPP_TOKEN real herdado do ambiente.

Este ficheiro só PROVA que as duas camadas de proteção (isolamento de
env vars + rede-fechada por omissão, ambas em conftest.py) funcionam —
não testa lógica de negócio nenhuma."""

from __future__ import annotations

import pathlib
import subprocess
import sys

import pytest
import requests

RAIZ = pathlib.Path(__file__).resolve().parent.parent


def test_conftest_isola_credenciais_mesmo_com_shell_contaminado():
    """§7 do patch — um processo Python NOVO, com o ambiente deliberadamente
    "contaminado" com credenciais verosímeis (mas falsas) de produção, tem
    de resolver sempre para os valores de TESTE definidos em conftest.py.
    Corre num subprocesso a sério (não um monkeypatch dentro do próprio
    processo de teste) para provar o isolamento tal como ele realmente
    acontece: env vars definidas ANTES do processo pytest arrancar."""
    env_contaminado = {
        "PATH": __import__("os").environ.get("PATH", ""),
        "DASHBOARD_USER": "real-admin-nao-usar",
        "DASHBOARD_PASSWORD": "real-secret-nao-usar",
        "WHATSAPP_TOKEN": "fake-production-token-nao-usar",
        "PHONE_NUMBER_ID": "123456789",
        "PROVIDER_WHATSAPP": "meta",
        "SESSOES_DB": "/tmp/nao-deve-ser-usado-real.db",
        "DATABASE_URL": "postgres://nao-usar-nunca/db",
        "APP_SECRET": "outro-segredo-de-producao",
        "ENABLE_DEMO_SEED": "true",
        "BOOKING_REQUIRES_APPROVAL": "true",
    }
    script = (
        "import sys; sys.path.insert(0, {tests!r}); "
        "import conftest; import config; "
        "print(config.DASHBOARD_USER); print(config.DASHBOARD_PASSWORD); "
        "print(config.WHATSAPP_TOKEN); print(config.PHONE_NUMBER_ID); "
        "print(config.PROVIDER_WHATSAPP); print(config.usa_postgres()); "
        "print(config.SQLITE_PATH); print(config.APP_SECRET); "
        "print(config.ENABLE_DEMO_SEED); print(config.BOOKING_REQUIRES_APPROVAL)"
    ).format(tests=str(RAIZ / "tests"))
    r = subprocess.run([sys.executable, "-c", script], env=env_contaminado,
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    linhas = r.stdout.strip().splitlines()
    (dashboard_user, dashboard_password, whatsapp_token, phone_number_id,
     provider, usa_postgres, sqlite_path, app_secret, demo_seed, requires_approval) = linhas

    # Os valores de TESTE de conftest.py venceram — nunca os "reais" que
    # foram deliberadamente exportados acima.
    assert dashboard_user == "painel"
    assert dashboard_password == "painel-pw"
    assert app_secret == "segredo-de-teste"
    # WhatsApp fica de propósito por configurar — nunca herda o token/PHONE
    # "de produção" que o ambiente contaminado tentou impor.
    assert whatsapp_token == "None"
    assert phone_number_id == "None"
    assert provider == "None"
    # DATABASE_URL "postgres://..." do ambiente contaminado nunca chega a
    # ser usado — a suite fica sempre em SQLite.
    assert usa_postgres == "False"
    assert "nao-deve-ser-usado-real.db" not in sqlite_path
    assert "NUNCA-USAR" in sqlite_path
    # ENABLE_DEMO_SEED / BOOKING_REQUIRES_APPROVAL também não herdam "true".
    assert demo_seed == "False"
    assert requires_approval == "False"


def test_rede_externa_get_bloqueada_por_omissao():
    """§8 do patch — uma chamada não mockada nunca chega à internet: falha
    localmente, de imediato, sem qualquer tentativa de rede real."""
    with pytest.raises(RuntimeError, match="bloqueada"):
        requests.get("https://graph.facebook.com/v21.0/123/messages", timeout=1)


def test_rede_externa_post_bloqueada_por_omissao():
    with pytest.raises(RuntimeError, match="bloqueada"):
        requests.post("https://graph.facebook.com/v21.0/123/messages", json={"a": 1}, timeout=1)


def test_mock_explicito_continua_a_funcionar_apesar_da_rede_fechada(monkeypatch):
    """A rede-fechada nunca impede o padrão já usado em toda a suite: mockar
    requests.post substitui a função em si, nunca chega a Session.request."""
    chamadas = []

    class _Resp:
        status_code = 200
        text = "{}"

    monkeypatch.setattr(requests, "post", lambda *a, **k: chamadas.append((a, k)) or _Resp())
    r = requests.post("https://graph.facebook.com/qualquer", json={"x": 1})
    assert r.status_code == 200
    assert len(chamadas) == 1
