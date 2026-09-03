"""
Fixtures da suite. Cada teste corre contra uma base de dados SQLite NOVA
(ficheiro temporário) já migrada, sem tocar em nada real.
"""

import os
import sys
import pathlib

import pytest

RAIZ = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))

# Ambiente mínimo ANTES de importar os módulos da app.
os.environ.setdefault("APP_SECRET", "segredo-de-teste")
os.environ.setdefault("VERIFY_TOKEN", "verify-de-teste")
os.environ.setdefault("BUSINESS_NAME", "Daniela Beauty")
os.environ.setdefault("BUSINESS_ADDRESS", "Rua de Teste 1, Visp")
os.environ.setdefault("DASHBOARD_USER", "painel")
os.environ.setdefault("DASHBOARD_PASSWORD", "painel-pw")
os.environ.pop("DATABASE_URL", None)

import config          # noqa: E402,F401
import db              # noqa: E402
import catalogo        # noqa: E402
import bot             # noqa: E402,F401


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
