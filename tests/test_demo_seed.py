"""Seed DEMO temporário (/api/dev/seed-dashboard) — nunca em produção real.

Cobre: endpoint desligado por omissão, autenticação obrigatória, criação de
um "dia cheio" realista (clientes, agenda de hoje, semana, histórico,
faturação), idempotência, catálogo/preços/durações corretos, CRM recalculado,
DELETE só apaga o prefixo demo (incluindo faturas), e zero envios de
WhatsApp (mesmo depois de um drain() explícito do outbox)."""

import base64

import bot
import db
import estados
from core import events as eventos
from conftest import marcar, data_pt

AUTH = {"Authorization": "Basic " + base64.b64encode(b"painel:painel-pw").decode()}
PREFIXO = bot.DEMO_TELEFONE_PREFIXO


def _sem_whatsapp(monkeypatch):
    enviados = []
    monkeypatch.setattr(bot._wa, "enviar_texto", lambda n, t: enviados.append((n, t)))
    return enviados


def test_prefixo_demo_e_o_convencionado():
    assert PREFIXO == "4179998"


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
    servicos_antes = db.listar_servicos()

    r = cliente_http.post("/api/dev/seed-dashboard", headers=AUTH)
    assert r.status_code == 201
    j = r.get_json()
    assert j["ok"] is True
    assert 100 <= j["created"] <= 170
    assert 6 <= j["today"] <= 10                  # 8-10 tentadas, ~75-90% capacidade
    assert 30 <= j["customers"] <= 50              # universo de clientes pedido
    assert j["invoices"] >= 15
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
        if real["preco_cents"] is not None:
            assert preco_cents == real["preco_cents"]
        else:
            # "preço a confirmar": só muda de None se uma fatura demo tiver
            # confirmado o preço nesta marcação (nunca no catálogo global).
            assert preco_cents is None or preco_cents == bot.DEMO_PRECO_A_CONFIRMAR_CENTS[servico_id]
    assert db.listar_servicos() == servicos_antes  # catálogo global intocado



def test_hoje_tem_boa_ocupacao_sem_conflitos(cliente_http, base_dados, monkeypatch):
    """~75-90% da capacidade de um único recurso (09:00-18:00 = 540 min) e
    ZERO sobreposições entre marcações que bloqueiam o horário."""
    monkeypatch.setattr(bot, "ENABLE_DEMO_SEED", True)
    _sem_whatsapp(monkeypatch)
    cliente_http.post("/api/dev/seed-dashboard", headers=AUTH)

    hoje = bot.tempo.hoje_zurique().isoformat()
    with db.ligacao() as conn:
        linhas = conn.execute(
            "SELECT hora_hhmm, duracao_min, estado, bloqueia_horario FROM agendamentos "
            "WHERE telefone LIKE ? AND data_iso = ?", (f"{PREFIXO}%", hoje)).fetchall()
    assert 6 <= len(linhas) <= 10

    total_min = sum(dur or 0 for (_hh, dur, _e, _b) in linhas)
    assert total_min >= 540 * 0.55  # dia visivelmente preenchido, não vazio

    ocupam = sorted(
        (_hhmm_min(hh), dur) for (hh, dur, estado, bloq) in linhas
        if bloq and estados.bloqueia_horario(estado, bloq))
    for i in range(1, len(ocupam)):
        fim_anterior = ocupam[i - 1][0] + ocupam[i - 1][1]
        assert fim_anterior <= ocupam[i][0], "conflito entre marcações de hoje"


def _hhmm_min(hhmm):
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def test_semana_sem_conflitos_no_dia(cliente_http, base_dados, monkeypatch):
    """Zero sobreposição de horários bloqueados em NENHUM dia semeado (não só
    hoje) — cobre o histórico e o futuro."""
    monkeypatch.setattr(bot, "ENABLE_DEMO_SEED", True)
    _sem_whatsapp(monkeypatch)
    cliente_http.post("/api/dev/seed-dashboard", headers=AUTH)

    with db.ligacao() as conn:
        linhas = conn.execute(
            "SELECT data_iso, hora_hhmm, duracao_min, estado, bloqueia_horario "
            "FROM agendamentos WHERE telefone LIKE ? AND data_iso IS NOT NULL",
            (f"{PREFIXO}%",)).fetchall()

    por_dia = {}
    for data_iso, hh, dur, estado, bloq in linhas:
        if not (bloq and estados.bloqueia_horario(estado, bloq)) or not hh or not dur:
            continue
        por_dia.setdefault(data_iso, []).append((_hhmm_min(hh), dur))

    for data_iso, itens in por_dia.items():
        itens.sort()
        for i in range(1, len(itens)):
            fim_anterior = itens[i - 1][0] + itens[i - 1][1]
            assert fim_anterior <= itens[i][0], f"conflito em {data_iso}"


def test_estados_variados(cliente_http, base_dados, monkeypatch):
    """Hoje e no geral: não fica tudo no mesmo estado — 'Em curso' vem SEMPRE
    de op_status=in_progress, nunca de inferência pelo relógio."""
    monkeypatch.setattr(bot, "ENABLE_DEMO_SEED", True)
    _sem_whatsapp(monkeypatch)
    cliente_http.post("/api/dev/seed-dashboard", headers=AUTH)

    hoje = bot.tempo.hoje_zurique().isoformat()
    with db.ligacao() as conn:
        hoje_rows = conn.execute(
            "SELECT estado, op_status FROM agendamentos WHERE telefone LIKE ? AND data_iso = ?",
            (f"{PREFIXO}%", hoje)).fetchall()
        gerais = conn.execute(
            "SELECT DISTINCT estado FROM agendamentos WHERE telefone LIKE ?",
            (f"{PREFIXO}%",)).fetchall()

    estados_hoje = {e for (e, _op) in hoje_rows}
    assert "completed" in estados_hoje
    assert "pending" in estados_hoje
    assert len(estados_hoje) >= 3          # não é tudo o mesmo estado

    em_curso = [op for (_e, op) in hoje_rows if op == "in_progress"]
    assert len(em_curso) >= 1              # "1 em curso" pedido, e só via op_status

    estados_gerais = {e for (e,) in gerais}
    assert {"completed", "confirmed", "cancelled"} <= estados_gerais


def test_historico_e_futuro_existem(cliente_http, base_dados, monkeypatch):
    monkeypatch.setattr(bot, "ENABLE_DEMO_SEED", True)
    _sem_whatsapp(monkeypatch)
    cliente_http.post("/api/dev/seed-dashboard", headers=AUTH)

    hoje = bot.tempo.hoje_zurique().isoformat()
    with db.ligacao() as conn:
        passado = conn.execute(
            "SELECT COUNT(*) FROM agendamentos WHERE telefone LIKE ? AND data_iso < ?",
            (f"{PREFIXO}%", hoje)).fetchone()[0]
        futuro = conn.execute(
            "SELECT COUNT(*) FROM agendamentos WHERE telefone LIKE ? AND data_iso > ?",
            (f"{PREFIXO}%", hoje)).fetchone()[0]
    assert passado > 0
    assert futuro > 0

    # pelo menos um cliente com histórico de visitas repetidas (CRM credível)
    clientes = [c for c in db.listar_customers() if (c.get("phone") or "").startswith(PREFIXO)]
    assert any(c["visits_count"] >= 2 for c in clientes)


def test_perfis_vip_e_notas_so_numa_parte(cliente_http, base_dados, monkeypatch):
    monkeypatch.setattr(bot, "ENABLE_DEMO_SEED", True)
    _sem_whatsapp(monkeypatch)
    cliente_http.post("/api/dev/seed-dashboard", headers=AUTH)

    clientes = [c for c in db.listar_customers() if (c.get("phone") or "").startswith(PREFIXO)]
    vips = [c for c in clientes if c.get("vip")]
    com_nota = [c for c in clientes if c.get("notes_internal")]
    assert 1 <= len(vips) <= 5
    assert 1 <= len(com_nota) <= len(bot.DEMO_NOTAS)
    assert len(com_nota) < len(clientes)   # nunca todos


def test_canceladas_ficam_no_historico_sem_poluir_agenda(cliente_http, base_dados, monkeypatch):
    monkeypatch.setattr(bot, "ENABLE_DEMO_SEED", True)
    _sem_whatsapp(monkeypatch)
    cliente_http.post("/api/dev/seed-dashboard", headers=AUTH)

    with db.ligacao() as conn:
        canceladas = conn.execute(
            "SELECT bloqueia_horario FROM agendamentos WHERE telefone LIKE ? AND estado = 'cancelled'",
            (f"{PREFIXO}%",)).fetchall()
    assert canceladas
    # libertadas (o caso normal do histórico) não contam como ocupação real
    assert any(b == 0 for (b,) in canceladas)


def test_faturas_demo_mistura_realista_sem_chf_zero(cliente_http, base_dados, monkeypatch):
    monkeypatch.setattr(bot, "ENABLE_DEMO_SEED", True)
    _sem_whatsapp(monkeypatch)
    r = cliente_http.post("/api/dev/seed-dashboard", headers=AUTH)
    j = r.get_json()

    from billing import engine as bi
    faturas = bi.listar_faturas(tenant_id=1, limite=500)
    faturas_demo_ids = set()
    with db.ligacao() as conn:
        for row in conn.execute(
                "SELECT id FROM invoices WHERE appointment_id IN "
                "(SELECT id FROM agendamentos WHERE telefone LIKE ?)", (f"{PREFIXO}%",)):
            faturas_demo_ids.add(row[0])
    faturas = [f for f in faturas if f["id"] in faturas_demo_ids]
    assert len(faturas) == j["invoices"]

    por_estado = {}
    for f in faturas:
        por_estado.setdefault(f["status"], 0)
        por_estado[f["status"]] += 1
        assert f["total_cents"] > 0            # nunca CHF 0 (preço a confirmar resolvido)

    assert por_estado.get("paid", 0) >= 5      # "várias Pagas"
    assert por_estado.get("draft", 0) >= 1     # "alguns Rascunhos"
    assert por_estado.get("cancelled", 0) >= 1 # "1-2 Anuladas"

    vencidas = [f for f in faturas if f["status"] == "issued"
               and f["due_date"] and f["due_date"] < bot.tempo.hoje_zurique().isoformat()]
    assert 1 <= len(vencidas) <= 2             # "1-2 Vencidas"


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
        total_faturas = conn.execute(
            "SELECT COUNT(*) FROM invoices WHERE appointment_id IN "
            "(SELECT id FROM agendamentos WHERE telefone LIKE ?)", (f"{PREFIXO}%",)).fetchone()[0]
    assert total == primeira["created"]           # segunda chamada não criou nada a mais
    assert total_faturas == primeira["invoices"]  # faturas demo também são idempotentes


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


def test_delete_apaga_so_prefixo_demo_incluindo_faturas(cliente_http, base_dados, monkeypatch):
    monkeypatch.setattr(bot, "ENABLE_DEMO_SEED", True)
    _sem_whatsapp(monkeypatch)

    # marcação + fatura REAIS, fora do prefixo demo — têm de sobreviver ao DELETE.
    id_real = marcar("41780001111", "limpeza_pele", data_pt("2026-09-14"), "10:00",
                     nome="Cliente Real")
    bot.atualizar_estado_agendamento(id_real, "completed")
    from billing import engine as bi
    fatura_real = bi.gerar_fatura_de_marcacao(id_real, tenant_id=1)

    cliente_http.post("/api/dev/seed-dashboard", headers=AUTH)
    r = cliente_http.delete("/api/dev/seed-dashboard", headers=AUTH)
    assert r.status_code == 200
    j = r.get_json()
    assert j["ok"] is True
    assert j["deleted_appointments"] > 0
    assert j["deleted_customers"] > 0
    assert j["deleted_invoices"] > 0

    with db.ligacao() as conn:
        restantes = conn.execute(
            "SELECT COUNT(*) FROM agendamentos WHERE telefone LIKE ?",
            (f"{PREFIXO}%",)).fetchone()[0]
        faturas_restantes = conn.execute(
            "SELECT COUNT(*) FROM invoices WHERE appointment_id IN "
            "(SELECT id FROM agendamentos WHERE telefone LIKE ?)", (f"{PREFIXO}%",)).fetchone()[0]
    assert restantes == 0
    assert faturas_restantes == 0
    assert bot.obter_agendamento(id_real) is not None
    assert bi.obter_fatura(fatura_real["id"], tenant_id=1) is not None


def test_zero_whatsapp_mesmo_apos_drain_explicito(cliente_http, base_dados, monkeypatch):
    """Os eventos gerados pelo seed (incluindo os de faturação) ficam
    pré-marcados como processados — um drain() chamado depois (ex.: pelo
    after_request de outra rota) não encontra nada por processar e portanto
    nunca envia WhatsApp."""
    monkeypatch.setattr(bot, "ENABLE_DEMO_SEED", True)
    enviados = _sem_whatsapp(monkeypatch)

    cliente_http.post("/api/dev/seed-dashboard", headers=AUTH)
    eventos.drain()
    assert enviados == []
