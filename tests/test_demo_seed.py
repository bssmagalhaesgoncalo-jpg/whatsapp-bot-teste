"""Seed DEMO temporário (/api/dev/seed-dashboard) — nunca em produção real.

Cobre: endpoint desligado por omissão, autenticação obrigatória, criação de
dados, idempotência, catálogo/preços/durações corretos, CRM recalculado,
DELETE só apaga o prefixo demo, e zero envios de WhatsApp (mesmo depois de
um drain() explícito do outbox)."""

import base64

import bot
import db
from core import events as eventos
from conftest import marcar, data_pt

AUTH = {"Authorization": "Basic " + base64.b64encode(b"painel:painel-pw").decode()}
PREFIXO = bot.DEMO_TELEFONE_PREFIXO


def _sem_whatsapp(monkeypatch):
    enviados = []
    monkeypatch.setattr(bot._wa, "enviar_texto", lambda n, t: enviados.append((n, t)))
    return enviados


def test_desligado_por_omissao_devolve_404(cliente_http, base_dados, monkeypatch):
    monkeypatch.setattr(bot, "ENABLE_DEMO_SEED", False)
    assert cliente_http.post("/api/dev/seed-dashboard", headers=AUTH).status_code == 404
    assert cliente_http.delete("/api/dev/seed-dashboard", headers=AUTH).status_code == 404


def test_exige_autenticacao(cliente_http, base_dados, monkeypatch):
    monkeypatch.setattr(bot, "ENABLE_DEMO_SEED", True)
    assert cliente_http.post("/api/dev/seed-dashboard").status_code == 401


def test_seed_cria_dados_realistas(cliente_http, base_dados, monkeypatch):
    monkeypatch.setattr(bot, "ENABLE_DEMO_SEED", True)
    enviados = _sem_whatsapp(monkeypatch)

    r = cliente_http.post("/api/dev/seed-dashboard", headers=AUTH)
    assert r.status_code == 201
    j = r.get_json()
    assert j["ok"] is True
    assert 80 <= j["created"] <= 120
    assert 4 <= j["today"] <= 6
    assert j["customers"] >= 1
    assert enviados == []  # nunca envia WhatsApp

    with db.ligacao() as conn:
        linhas = conn.execute(
            "SELECT telefone, servico_id, duracao_min, preco_cents FROM agendamentos "
            "WHERE telefone LIKE ?", (f"{PREFIXO}%",)).fetchall()
    assert len(linhas) == j["created"]
    servicos = {s["id"]: s for s in db.listar_servicos()}
    for telefone, servico_id, duracao_min, preco_cents in linhas:
        assert telefone.startswith(PREFIXO)
        real = servicos[servico_id]
        assert duracao_min == real["duracao_min"]
        assert preco_cents == real["preco_cents"]


def test_segunda_chamada_nao_duplica(cliente_http, base_dados, monkeypatch):
    monkeypatch.setattr(bot, "ENABLE_DEMO_SEED", True)
    enviados = _sem_whatsapp(monkeypatch)

    primeira = cliente_http.post("/api/dev/seed-dashboard", headers=AUTH).get_json()
    segunda = cliente_http.post("/api/dev/seed-dashboard", headers=AUTH).get_json()
    assert segunda == {"ok": True, "already_seeded": True, "appointments": primeira["created"]}
    assert enviados == []

    with db.ligacao() as conn:
        total = conn.execute(
            "SELECT COUNT(*) FROM agendamentos WHERE telefone LIKE ?",
            (f"{PREFIXO}%",)).fetchone()[0]
    assert total == primeira["created"]  # segunda chamada não criou nada a mais


def test_clientes_recalculados(cliente_http, base_dados, monkeypatch):
    monkeypatch.setattr(bot, "ENABLE_DEMO_SEED", True)
    _sem_whatsapp(monkeypatch)
    cliente_http.post("/api/dev/seed-dashboard", headers=AUTH)

    clientes = [c for c in db.listar_customers() if (c.get("phone") or "").startswith(PREFIXO)]
    assert clientes
    assert any(c["visits_count"] > 0 for c in clientes)   # históricos completed contam
    for c in clientes:
        assert c["visits_count"] >= 0
        assert c["spend_cents"] >= 0
        assert c["no_show_count"] >= 0
        assert c["cancel_count"] >= 0


def test_delete_apaga_so_prefixo_demo(cliente_http, base_dados, monkeypatch):
    monkeypatch.setattr(bot, "ENABLE_DEMO_SEED", True)
    _sem_whatsapp(monkeypatch)

    # marcação REAL, fora do prefixo demo — tem de sobreviver ao DELETE.
    id_real = marcar("41780001111", "limpeza_pele", data_pt("2026-09-14"), "10:00",
                     nome="Cliente Real")

    cliente_http.post("/api/dev/seed-dashboard", headers=AUTH)
    r = cliente_http.delete("/api/dev/seed-dashboard", headers=AUTH)
    assert r.status_code == 200
    j = r.get_json()
    assert j["ok"] is True
    assert j["deleted_appointments"] > 0
    assert j["deleted_customers"] > 0

    with db.ligacao() as conn:
        restantes = conn.execute(
            "SELECT COUNT(*) FROM agendamentos WHERE telefone LIKE ?",
            (f"{PREFIXO}%",)).fetchone()[0]
    assert restantes == 0
    assert bot.obter_agendamento(id_real) is not None


def test_zero_whatsapp_mesmo_apos_drain_explicito(cliente_http, base_dados, monkeypatch):
    """Os eventos gerados pelo seed ficam pré-marcados como processados — um
    drain() chamado depois (ex.: pelo after_request de outra rota) não
    encontra nada por processar e portanto nunca envia WhatsApp."""
    monkeypatch.setattr(bot, "ENABLE_DEMO_SEED", True)
    enviados = _sem_whatsapp(monkeypatch)

    cliente_http.post("/api/dev/seed-dashboard", headers=AUTH)
    eventos.drain()
    assert enviados == []
