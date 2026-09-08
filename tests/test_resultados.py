"""PATCH P3 — RESULTADOS / IMPACTO ECONÓMICO DO BMS.

Testa a camada de agregação (`reports/results.py`) e o endpoint
`GET /api/resultados`. Nada aqui repete os testes das automações em si
(reminder 24h, rebooking, pós-atendimento — ver test_reminder_24h.py,
test_rebooking_followup.py, test_pos_atendimento.py); em vez disso, exercita
o CONTRATO de eventos que essas automações já garantem (tipo + payload +
dedupe_key, confirmados por leitura direta do código) e verifica que
reports/results.py os agrega corretamente, sem inventar nada e sem
duplicar/misturar métricas que o patch exige manter separadas.

Uma exceção: o funil de pós-atendimento (§14, tempo poupado) É exercitado
de ponta a ponta (booking -> completed -> fatura paga -> job -> WhatsApp),
porque é onde `booking_source`, `post_service.sent`, `feedback.requested`
e `pdf_sent_at` se cruzam de verdade."""

import base64
from datetime import date, timedelta

import pytest
import requests

import bot
import config
import db
import estados
import tempo
from billing import engine as bi
from notifications import jobs as notif_jobs
from reports import results
from conftest import data_pt

AUTH = {"Authorization": "Basic " + base64.b64encode(b"painel:painel-pw").decode()}
SERVICO_A = "limpeza_pele"
SERVICO_B = "design_sobrancelhas"


def _hoje_iso():
    return tempo.hoje_zurique().isoformat()


def _marca(tel, sid, dia_iso, hora, nome="Cliente Teste", booking_source=None):
    s = db.obter_servico(sid)
    sess = {"idioma": "pt", "nome": nome, "servico_id": sid, "servico": s["nome_pt"],
            "duracao_min": s["duracao_min"], "duracao": f"{s['duracao_min']} min",
            "preco_cents": s["preco_cents"],
            "preco": round(s["preco_cents"] / 100, 2) if s["preco_cents"] is not None else None,
            "data": data_pt(dia_iso), "hora": hora}
    if booking_source:
        sess["booking_source"] = booking_source
    return bot.guardar_agendamento(tel, sess)


def _forcar_criado_em(appointment_id, quando_iso):
    with db.ligacao() as c:
        c.execute("UPDATE agendamentos SET criado_em = ? WHERE id = ?", (quando_iso, appointment_id))


def _forcar_first_seen(telefone, quando_iso, tenant_id=1):
    with db.ligacao() as c:
        c.execute("UPDATE customers SET first_seen = ? WHERE tenant_id = ? AND phone = ?",
                  (quando_iso, tenant_id, telefone))


def _evento(tenant_id, tipo, entity_id, payload=None, dedupe_key=None, created_at=None):
    """Insere um evento diretamente na outbox — simula o CONTRATO já
    garantido pelas automações reais (ver docstring do módulo), sem
    reexecutar toda a cadeia de webhooks/jobs."""
    with db.ligacao() as c:
        if created_at is None:
            db.registar_evento(c, tipo, "appointment", entity_id, payload, dedupe_key=dedupe_key,
                               tenant_id=tenant_id)
        else:
            import json
            c.execute(
                "INSERT INTO events (tenant_id, type, entity_type, entity_id, payload, dedupe_key, created_at) "
                "VALUES (?, ?, 'appointment', ?, ?, ?, ?)",
                (tenant_id, tipo, entity_id, json.dumps(payload or {}, ensure_ascii=False),
                 dedupe_key, created_at))


def _completar(a):
    bot.atualizar_estado_agendamento(a, "completed")
    bot.disparar_automacoes()


def _job_post_service(a):
    with db.ligacao() as c:
        row = c.execute(
            "SELECT id, run_at FROM automation_jobs WHERE booking_id = ? AND type = ?",
            (a, notif_jobs.TYPE_POST_SERVICE)).fetchone()
    return {"id": row[0], "run_at": row[1]} if row else None


def _mock_provider(monkeypatch):
    chamadas = []

    class _Resp:
        status_code = 200
        text = "{}"

    def _post(url, headers=None, json=None, timeout=None):
        chamadas.append((url, json))
        return _Resp()

    monkeypatch.setattr(bot._wa.requests, "post", _post)
    monkeypatch.setattr(bot._wa.config, "WHATSAPP_TOKEN", "token-de-teste")
    monkeypatch.setattr(bot._wa.config, "PHONE_NUMBER_ID", "123456")
    return chamadas


# ===========================================================================
# Período (Europe/Zurich, 7d/30d/90d/ano + comparação)
# ===========================================================================
def test_periodo_invalido_levanta_erro(base_dados):
    with pytest.raises(results.PeriodoInvalido):
        results.calcular_resultados(1, "365d")


def test_limites_7d_inclui_hoje_e_mais_6_dias(base_dados):
    hoje = date(2026, 3, 15)
    lim = results._limites("7d", hoje)
    assert lim["inicio"] == "2026-03-09"
    assert lim["fim"] == "2026-03-15"
    assert lim["dias"] == 7


def test_limites_ano_comeca_em_1_de_janeiro(base_dados):
    hoje = date(2026, 3, 15)
    lim = results._limites("ano", hoje)
    assert lim["inicio"] == "2026-01-01"
    assert lim["fim"] == "2026-03-15"


def test_limites_periodo_anterior_tem_a_mesma_duracao(base_dados):
    hoje = date(2026, 3, 15)
    lim = results._limites("30d", hoje)
    dias_atual = (date.fromisoformat(lim["fim"]) - date.fromisoformat(lim["inicio"])).days + 1
    dias_anterior = (date.fromisoformat(lim["fim_anterior"]) - date.fromisoformat(lim["inicio_anterior"])).days + 1
    assert dias_atual == dias_anterior == 30
    assert date.fromisoformat(lim["fim_anterior"]) == date.fromisoformat(lim["inicio"]) - timedelta(days=1)


# ===========================================================================
# Marcações via BMS (§7) — só whatsapp_bot + rebooking_followup
# ===========================================================================
def test_marcacoes_bms_soma_so_whatsapp_bot_e_rebooking(base_dados):
    hoje = _hoje_iso()
    _marca("41790010001", SERVICO_A, hoje, "09:00")  # default -> whatsapp_bot
    _marca("41790010002", SERVICO_A, hoje, "10:00", booking_source="rebooking_followup")
    _marca("41790010003", SERVICO_A, hoje, "11:00", booking_source="dashboard")
    a4 = _marca("41790010004", SERVICO_A, hoje, "12:00")
    with db.ligacao() as c:
        c.execute("UPDATE agendamentos SET booking_source = 'unknown' WHERE id = ?", (a4,))

    r = results.calcular_resultados(1, "30d")
    b = r["bookings"]
    assert b["whatsapp_bot"] == 1
    assert b["rebooking_followup"] == 1
    assert b["dashboard"] == 1
    assert b["unknown"] == 1
    assert b["bms"] == 2                 # NUNCA dashboard, NUNCA unknown
    assert b["total"] == 4


def test_marcacoes_fora_do_periodo_nao_contam(base_dados):
    hoje = _hoje_iso()
    a = _marca("41790010005", SERVICO_A, hoje, "09:00")
    _forcar_criado_em(a, tempo.iso_utc(tempo.agora_zurique() - timedelta(days=40)))

    r = results.calcular_resultados(1, "30d")
    assert r["bookings"]["total"] == 0


# ===========================================================================
# Receita atribuída (§8) — só faturas PAGAS
# ===========================================================================
def test_receita_so_conta_faturas_pagas(base_dados, monkeypatch):
    _mock_provider(monkeypatch)
    hoje = _hoje_iso()
    pago = _marca("41790020001", SERVICO_A, hoje, "09:00")
    _completar(pago)                                       # gera fatura PAGA (cash)

    rascunho_appt = _marca("41790020002", SERVICO_B, hoje, "10:00")
    bi.gerar_fatura_de_marcacao(rascunho_appt)              # fica em draft — nunca conta

    r = results.calcular_resultados(1, "30d")
    rev = r["revenue"]
    assert rev["faturas_pagas"] == 1
    assert rev["total_paid_cents"] == db.obter_servico(SERVICO_A)["preco_cents"]


def test_receita_bms_exclui_dashboard_e_unknown(base_dados, monkeypatch):
    _mock_provider(monkeypatch)
    hoje = _hoje_iso()
    via_bot = _marca("41790020003", SERVICO_A, hoje, "09:00")
    _completar(via_bot)
    via_dashboard = _marca("41790020004", SERVICO_B, hoje, "10:00", booking_source="dashboard")
    _completar(via_dashboard)

    r = results.calcular_resultados(1, "30d")
    rev = r["revenue"]
    preco_a = db.obter_servico(SERVICO_A)["preco_cents"]
    preco_b = db.obter_servico(SERVICO_B)["preco_cents"]
    assert rev["whatsapp_bot_cents"] == preco_a
    assert rev["dashboard_cents"] == preco_b
    assert rev["bms_cents"] == preco_a                     # dashboard NUNCA entra no BMS
    assert rev["total_paid_cents"] == preco_a + preco_b


def test_receita_por_servico_usa_descricao_da_linha_da_fatura(base_dados, monkeypatch):
    _mock_provider(monkeypatch)
    hoje = _hoje_iso()
    a = _marca("41790020005", SERVICO_A, hoje, "09:00")
    _completar(a)

    r = results.calcular_resultados(1, "30d")
    servico = db.obter_servico(SERVICO_A)
    descricoes = {linha["servico"] for linha in r["revenue"]["por_servico"]}
    assert servico["nome_pt"] in descricoes


def test_ticket_medio_none_sem_faturas(base_dados):
    r = results.calcular_resultados(1, "30d")
    assert r["revenue"]["ticket_medio_cents"] is None
    assert r["revenue"]["total_paid_cents"] == 0


# ===========================================================================
# No-show (§10) — nunca conflar com cancelamento; denominador zero -> None
# ===========================================================================
def test_no_show_rate_formula_correta(base_dados):
    hoje = _hoje_iso()
    c1 = _marca("41790030001", SERVICO_A, hoje, "09:00")
    c2 = _marca("41790030002", SERVICO_A, hoje, "10:00")
    ns = _marca("41790030003", SERVICO_A, hoje, "11:00")
    bot.atualizar_estado_agendamento(c1, "completed")
    bot.atualizar_estado_agendamento(c2, "completed")
    bot.atualizar_estado_agendamento(ns, "no_show")

    r = results.calcular_resultados(1, "30d")
    no_show = r["no_show"]
    assert no_show["no_shows"] == 1
    assert no_show["elegiveis"] == 3
    assert no_show["rate_pct"] == pytest.approx(33.3, abs=0.1)


def test_no_show_rate_zero_denominador_e_none_nao_nan(base_dados):
    hoje = _hoje_iso()
    _marca("41790030004", SERVICO_A, hoje, "09:00")   # continua "confirmed" — não é elegível

    r = results.calcular_resultados(1, "30d")
    assert r["no_show"]["elegiveis"] == 0
    assert r["no_show"]["rate_pct"] is None


def test_cancelamento_e_metrica_separada_do_no_show(base_dados):
    hoje = _hoje_iso()
    a = _marca("41790030005", SERVICO_A, hoje, "09:00")
    bot.marcar_agendamento_cancelado(a, libertar=True, exigir_confirmado=False)

    r = results.calcular_resultados(1, "30d")
    assert r["no_show"]["no_shows"] == 0
    assert r["no_show"]["elegiveis"] == 0            # cancelled NUNCA entra no denominador do no-show
    assert r["cancellations"]["cancelamentos"] == 1
    assert r["cancellations"]["marcacoes_no_periodo"] == 1
    assert r["cancellations"]["rate_pct"] == 100.0


# ===========================================================================
# Clientes novos / recorrentes (§9)
# ===========================================================================
def test_clientes_novos_conta_first_seen_no_periodo(base_dados):
    hoje = _hoje_iso()
    _marca("41790040001", SERVICO_A, hoje, "09:00")

    r = results.calcular_resultados(1, "30d")
    assert r["customers"]["novos"] == 1


def test_clientes_novos_fora_do_periodo_nao_conta(base_dados):
    hoje = _hoje_iso()
    a = _marca("41790040002", SERVICO_A, hoje, "09:00")
    _forcar_first_seen("41790040002", tempo.iso_utc(tempo.agora_zurique() - timedelta(days=90)))

    r = results.calcular_resultados(1, "30d")
    assert r["customers"]["novos"] == 0


def test_cliente_recorrente_reusa_visits_count_do_crm(base_dados):
    hoje = _hoje_iso()
    tel = "41790040003"
    a1 = _marca(tel, SERVICO_A, hoje, "09:00")
    bot.atualizar_estado_agendamento(a1, "completed")
    a2 = _marca(tel, SERVICO_A, hoje, "10:00")
    bot.atualizar_estado_agendamento(a2, "completed")

    outro = "41790040004"
    a3 = _marca(outro, SERVICO_A, hoje, "11:00")
    bot.atualizar_estado_agendamento(a3, "completed")

    r = results.calcular_resultados(1, "30d")
    cust = r["customers"]
    assert cust["atendidos"] == 2          # 2 clientes distintos atendidos no período
    assert cust["recorrentes"] == 1        # só o de 2 visitas concluídas
    assert cust["taxa_recorrencia_pct"] == 50.0


# ===========================================================================
# Reminder 24h (§11) — factos comprovados, nunca causalidade inferida
# ===========================================================================
def test_reminder_enviados_e_confirmacoes_via_origin(base_dados):
    hoje = _hoje_iso()
    a = _marca("41790050001", SERVICO_A, hoje, "09:00")
    _evento(1, "reminder.sent", a, {}, dedupe_key="reminder.sent:999:x")
    _evento(1, "booking.confirmed", a, {"automation_job_id": 999, "origin": "reminder_24h", "job_run_at": "x"},
            dedupe_key="booking.confirmed:reminder_24h:999:x")

    r = results.calcular_resultados(1, "30d")
    rem = r["reminders"]
    assert rem["enviados"] == 1
    assert rem["confirmacoes"] == 1


def test_reminder_confirmacao_sem_origin_reminder_nao_conta(base_dados):
    hoje = _hoje_iso()
    a = _marca("41790050002", SERVICO_A, hoje, "09:00")
    # confirmação SEM origin=reminder_24h (ex.: outro caminho qualquer) não
    # pode ser atribuída ao reminder.
    _evento(1, "booking.confirmed", a, {"origin": "outra_coisa"}, dedupe_key="booking.confirmed:x")

    r = results.calcular_resultados(1, "30d")
    assert r["reminders"]["confirmacoes"] == 0


def test_reminder_reschedule_iniciado_fica_separado_de_confirmacoes(base_dados):
    hoje = _hoje_iso()
    a = _marca("41790050003", SERVICO_A, hoje, "09:00")
    _evento(1, "reminder.reschedule_started", a, {}, dedupe_key="reminder.reschedule_started:1:x")

    r = results.calcular_resultados(1, "30d")
    rem = r["reminders"]
    assert rem["reagendamentos_iniciados"] == 1
    assert rem["confirmacoes"] == 0            # "iniciado" nunca vira "confirmado" sozinho


def test_reminder_cancelamento_contabilizado(base_dados):
    hoje = _hoje_iso()
    a = _marca("41790050004", SERVICO_A, hoje, "09:00")
    _evento(1, "reminder.cancelled", a, {}, dedupe_key="reminder.cancelled:1")

    r = results.calcular_resultados(1, "30d")
    assert r["reminders"]["cancelamentos"] == 1


# ===========================================================================
# Feedback (§12) — só respostas reais
# ===========================================================================
def test_feedback_taxa_de_resposta(base_dados):
    hoje = _hoje_iso()
    a1 = _marca("41790060001", SERVICO_A, hoje, "09:00")
    a2 = _marca("41790060002", SERVICO_A, hoje, "10:00")
    _evento(1, "feedback.requested", a1, {}, dedupe_key=f"feedback.requested:{a1}")
    _evento(1, "feedback.requested", a2, {}, dedupe_key=f"feedback.requested:{a2}")
    _evento(1, "feedback.received", a1, {"texto": "Adorei!"}, dedupe_key=f"feedback.received:{a1}")

    r = results.calcular_resultados(1, "30d")
    fb = r["feedback"]
    assert fb["pedidos"] == 2
    assert fb["recebidos"] == 1
    assert fb["taxa_resposta_pct"] == 50.0


def test_feedback_sem_pedidos_taxa_none(base_dados):
    r = results.calcular_resultados(1, "30d")
    assert r["feedback"]["pedidos"] == 0
    assert r["feedback"]["taxa_resposta_pct"] is None


# ===========================================================================
# Rebooking automático (§13)
# ===========================================================================
def test_rebooking_conversao_e_proporcao_agregada_no_periodo(base_dados, monkeypatch):
    _mock_provider(monkeypatch)
    hoje = _hoje_iso()
    _evento(1, "rebooking_followup.sent", 1001, {}, dedupe_key="rebooking_followup.sent:1")
    _evento(1, "rebooking_followup.sent", 1002, {}, dedupe_key="rebooking_followup.sent:2")
    nova = _marca("41790070001", SERVICO_A, hoje, "09:00", booking_source="rebooking_followup")
    _completar(nova)

    r = results.calcular_resultados(1, "30d")
    rb = r["rebooking"]
    assert rb["seguimentos_enviados"] == 2
    assert rb["marcacoes_criadas"] == 1
    assert rb["conversao_pct"] == 50.0
    assert rb["receita_atribuida_cents"] == db.obter_servico(SERVICO_A)["preco_cents"]


def test_rebooking_mais_tarde_contabilizado_separadamente(base_dados):
    _evento(1, "rebooking_followup.snoozed", 1003, {}, dedupe_key="rebooking_followup.snoozed:1")

    r = results.calcular_resultados(1, "30d")
    assert r["rebooking"]["mais_tarde"] == 1
    assert r["rebooking"]["seguimentos_enviados"] == 0


def test_rebooking_conversao_none_sem_envios(base_dados):
    r = results.calcular_resultados(1, "30d")
    assert r["rebooking"]["conversao_pct"] is None


# ===========================================================================
# Tempo poupado (§14) — SEMPRE estimativa, nunca "tempo real"
# ===========================================================================
def test_tempo_poupado_usa_o_default_de_3_minutos(base_dados):
    assert config.ESTIMATED_MINUTES_SAVED_PER_AUTOMATION == 3
    r = results.calcular_resultados(1, "30d")
    assert r["time_saved"]["minutos_por_acao"] == 3
    assert r["time_saved"]["estimativa"] is True


def test_tempo_poupado_e_configuravel(base_dados, monkeypatch):
    monkeypatch.setattr(config, "ESTIMATED_MINUTES_SAVED_PER_AUTOMATION", 5)
    hoje = _hoje_iso()
    _marca("41790080001", SERVICO_A, hoje, "09:00")   # 1 marcação via whatsapp_bot

    r = results.calcular_resultados(1, "30d")
    ts = r["time_saved"]
    assert ts["minutos_por_acao"] == 5
    assert ts["acoes_automatizadas"] >= 1
    assert ts["minutos_estimados"] == ts["acoes_automatizadas"] * 5


def test_tempo_poupado_conta_acoes_reais_do_pos_atendimento_ponta_a_ponta(base_dados, monkeypatch):
    _mock_provider(monkeypatch)
    monkeypatch.setattr(config, "PUBLIC_BASE_URL", "https://exemplo.test")
    hoje = _hoje_iso()
    a = _marca("41790080002", SERVICO_A, hoje, "09:00")
    _completar(a)
    job = _job_post_service(a)
    assert job is not None
    resumo = notif_jobs.process_due_jobs(agora=job["run_at"])
    assert resumo["concluidos"] == 1

    r = results.calcular_resultados(1, "30d")
    ts = r["time_saved"]["detalhe"]
    assert ts["agradecimentos_pos_atendimento"] == 1
    assert ts["pedidos_feedback"] == 1
    assert ts["pdfs_fatura_enviados"] == 1
    assert ts["marcacoes_whatsapp_bot"] == 1


# ===========================================================================
# Comparação com o período anterior — nunca fabricada sem dados
# ===========================================================================
def test_comparacao_e_none_sem_dados_no_periodo_anterior(base_dados):
    hoje = _hoje_iso()
    _marca("41790090001", SERVICO_A, hoje, "09:00")

    r = results.calcular_resultados(1, "30d")
    assert r["comparacao"] is None


def test_comparacao_aparece_e_calcula_delta_quando_ha_dados_antes(base_dados):
    hoje_dt = tempo.agora_zurique()
    a1 = _marca("41790090002", SERVICO_A, hoje_dt.date().isoformat(), "09:00")
    a2 = _marca("41790090003", SERVICO_A, hoje_dt.date().isoformat(), "10:00")
    _forcar_criado_em(a1, tempo.iso_utc(hoje_dt - timedelta(days=45)))  # período ANTERIOR (30-60d atrás)

    r = results.calcular_resultados(1, "30d")
    assert r["comparacao"] is not None
    assert r["comparacao"]["marcacoes_bms"]["anterior"] == 1
    assert r["comparacao"]["marcacoes_bms"]["delta_pct"] == 0.0  # 1 -> 1 (a2 é o atual)


# ===========================================================================
# Endpoint GET /api/resultados
# ===========================================================================
def test_api_resultados_exige_autenticacao(cliente_http):
    resp = cliente_http.get("/api/resultados")
    assert resp.status_code == 401


def test_api_resultados_periodo_invalido_e_400(cliente_http):
    resp = cliente_http.get("/api/resultados?periodo=xyz", headers=AUTH)
    assert resp.status_code == 400
    assert "erro" in resp.get_json()


def test_api_resultados_periodo_omitido_usa_30d(cliente_http):
    resp = cliente_http.get("/api/resultados", headers=AUTH)
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["period"]["periodo"] == "30d"


def test_api_resultados_forma_da_resposta(base_dados, cliente_http):
    hoje = _hoje_iso()
    _marca("41790100001", SERVICO_A, hoje, "09:00")
    resp = cliente_http.get("/api/resultados?periodo=7d", headers=AUTH)
    assert resp.status_code == 200
    body = resp.get_json()
    for chave in ("bookings", "revenue", "customers", "no_show", "cancellations",
                  "reminders", "feedback", "rebooking", "time_saved", "period"):
        assert chave in body


# ===========================================================================
# UI — rota registada, item de sidebar, sem inventar nome
# ===========================================================================
def test_rota_resultados_registada_no_router_js():
    src = open("static/dashboard/app.js", encoding="utf-8").read()
    assert "resultados: viewResultados" in src
    assert "async function viewResultados(" in src


def test_sidebar_tem_item_resultados_com_o_nome_exato():
    src = open("templates/dashboard/shell.html", encoding="utf-8").read()
    assert 'href="#/resultados"' in src
    assert ">Resultados<" in src
    for proibido in ("Analytics", "Advanced Metrics", ">BI<"):
        assert proibido not in src


def test_ui_marca_tempo_poupado_como_estimativa():
    src = open("static/dashboard/app.js", encoding="utf-8").read()
    inicio = src.index("async function viewResultados(")
    fim = src.index("\n}\n", inicio)
    corpo = src[inicio:fim]
    assert "estimativa" in corpo.lower() or "estim" in corpo.lower()
    assert "tempo real poupado" not in corpo.lower()
