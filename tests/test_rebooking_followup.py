"""PATCH P2 — rebooking automático (follow-up de reativação).

booking.completed (já existente) -> serviço com follow_up_enabled +
rebook_days válido -> UM job "rebooking_followup" agendado para
completed_at + rebook_days dias -> run_at vencido -> executor REVALIDA tudo
-> WhatsApp TEMPLATE com [Marcar novamente]/[Mais tarde] -> "Marcar
novamente" reaproveita o fluxo normal de marcação com
booking_source=rebooking_followup. Ver notifications/followup.py,
notifications/jobs.py.

Zero envios reais: o provider é sempre mockado na fronteira HTTP, tal como
em tests/test_pos_atendimento.py e tests/test_reminder_24h.py (mesmo
padrão)."""

import base64
import hashlib
import hmac
import json
from datetime import timedelta

import pytest
import requests

import bot
import catalogo
import config
import db
import estados
import tempo
from notifications import jobs as notif_jobs
from notifications import followup as notif_followup
from conftest import marcar, data_pt

AUTH = {"Authorization": "Basic " + base64.b64encode(b"painel:painel-pw").decode()}
DIA = "2026-09-07"          # segunda-feira (dentro do horário semeado)
DIA_TXT = data_pt(DIA)


def _configurar_servico(sid="limpeza_pele", rebook_days=21, enabled=True, ativo=True):
    db.atualizar_servico(sid, {"rebook_days": rebook_days, "follow_up_enabled": enabled, "ativo": ativo})


def _completar(tel, sid="limpeza_pele", hora="09:00", nome="Cliente Teste"):
    a = marcar(tel, sid, DIA_TXT, hora, nome=nome)
    bot.atualizar_estado_agendamento(a, "completed")
    bot.disparar_automacoes()
    return a


def _completar_isolado(tel, chamadas, sid="limpeza_pele", hora="09:00", nome="Cliente Teste"):
    """booking.completed também dispara o P0 (post_service, +5min — já
    coberto por tests/test_pos_atendimento.py), que não tem nada a ver com
    o que se testa aqui. Fecha-o diretamente na BD (sem passar pelo provider
    mockado — nunca consome um índice de `falha_na_chamada` nem entra em
    `chamadas`), para os testes deste ficheiro ficarem isolados ao
    rebooking_followup."""
    a = _completar(tel, sid, hora, nome)
    with db.ligacao() as c:
        c.execute("UPDATE automation_jobs SET status = ? WHERE booking_id = ? AND type = ?",
                  (notif_jobs.DONE, a, notif_jobs.TYPE_POST_SERVICE))
    chamadas.clear()
    return a


def _job_rebooking(a):
    with db.ligacao() as c:
        rows = c.execute(
            "SELECT id, type, run_at, status, attempts, last_error, idempotency_key "
            "FROM automation_jobs WHERE booking_id = ? AND type = ?",
            (a, notif_jobs.TYPE_REBOOKING_FOLLOWUP)).fetchall()
    campos = ("id", "type", "run_at", "status", "attempts", "last_error", "idempotency_key")
    return [dict(zip(campos, r)) for r in rows]


def _customer_id(telefone):
    for c in db.listar_customers():
        if c["phone"] == telefone:
            return c["id"]
    raise AssertionError(f"cliente {telefone} não encontrado")


def _mock_provider(monkeypatch, falha_na_chamada=None):
    chamadas = []

    class _Resp:
        status_code = 200
        text = "{}"

    def _post(url, headers=None, json=None, timeout=None):
        indice = len(chamadas)
        chamadas.append((url, json))
        if falha_na_chamada is not None and indice == falha_na_chamada:
            raise requests.RequestException("falha de rede simulada")
        return _Resp()

    monkeypatch.setattr(bot._wa.requests, "post", _post)
    monkeypatch.setattr(bot._wa.config, "WHATSAPP_TOKEN", "token-de-teste")
    monkeypatch.setattr(bot._wa.config, "PHONE_NUMBER_ID", "123456")
    return chamadas


def _configurar_templates(monkeypatch):
    monkeypatch.setattr(config, "WHATSAPP_REBOOKING_TEMPLATE_PT", "rebooking_pt")
    monkeypatch.setattr(config, "WHATSAPP_REBOOKING_TEMPLATE_DE", "rebooking_de")
    monkeypatch.setattr(config, "WHATSAPP_REBOOKING_TEMPLATE_EN", "rebooking_en")


def _post_webhook(cliente_http, msg):
    corpo = json.dumps({"entry": [{"changes": [{"value": {
        "messaging_product": "whatsapp",
        "metadata": {"phone_number_id": "x"},
        "messages": [msg],
    }}]}]}).encode()
    sig = "sha256=" + hmac.new(b"segredo-de-teste", corpo, hashlib.sha256).hexdigest()
    return cliente_http.post("/webhook", data=corpo, content_type="application/json",
                             headers={"X-Hub-Signature-256": sig})


def _botao(rid, tel, mid):
    return {"from": tel, "id": mid, "type": "interactive",
            "interactive": {"type": "button_reply", "button_reply": {"id": rid, "title": rid}}}


# ===========================================================================
# CONFIG — rebook_days válido / null / zero / negativo
# ===========================================================================
def test_rebook_days_valido_cria_job(base_dados):
    _configurar_servico(rebook_days=21)
    a = _completar("41790060001")
    assert len(_job_rebooking(a)) == 1


def test_rebook_days_null_nao_cria_job(base_dados):
    _configurar_servico(rebook_days=None)
    a = _completar("41790060002")
    assert _job_rebooking(a) == []


def test_rebook_days_zero_e_tratado_como_desativado(base_dados):
    # api_servico_editar já trata 0 como "desativa" (rd or None) — aqui
    # gravamos diretamente 0 na BD para confirmar que o AGENDAMENTO nunca
    # assume um valor inválido como se fosse "hoje mesmo".
    db.atualizar_servico("limpeza_pele", {"follow_up_enabled": True})
    with db.ligacao() as conn:
        conn.execute("UPDATE servicos SET rebook_days = 0 WHERE id = ?", ("limpeza_pele",))
    a = _completar("41790060003")
    assert _job_rebooking(a) == []


def test_rebook_days_negativo_nunca_cria_job(base_dados):
    db.atualizar_servico("limpeza_pele", {"follow_up_enabled": True})
    with db.ligacao() as conn:
        conn.execute("UPDATE servicos SET rebook_days = -5 WHERE id = ?", ("limpeza_pele",))
    a = _completar("41790060004")
    assert _job_rebooking(a) == []


def test_sem_follow_up_enabled_nao_cria_job(base_dados):
    db.atualizar_servico("limpeza_pele", {"rebook_days": 21, "follow_up_enabled": False})
    a = _completar("41790060005")
    assert _job_rebooking(a) == []


# ===========================================================================
# SCHEDULING — run_at correto + idempotência
# ===========================================================================
def test_completed_cria_job_com_run_at_completed_mais_rebook_days(base_dados):
    _configurar_servico(rebook_days=21)
    a = _completar("41790060006")
    job = _job_rebooking(a)[0]
    ag = bot.obter_agendamento(a)
    assert job["status"] == notif_jobs.PENDING
    assert job["idempotency_key"] == f"rebooking_followup:{a}"
    delta = (tempo.parse_iso(job["run_at"]) - tempo.parse_iso(ag["completed_at"])).total_seconds()
    assert delta == 21 * 86400


def test_reprocessar_booking_completed_nao_duplica_job(base_dados):
    _configurar_servico(rebook_days=21)
    a = _completar("41790060007")
    notif_followup.agendar_rebooking_followup(a)
    notif_followup.agendar_rebooking_followup(a)
    assert len(_job_rebooking(a)) == 1


# ===========================================================================
# REVALIDAÇÃO — cada regra impede o envio isoladamente
# ===========================================================================
def test_booking_futura_mesmo_servico_impede_envio(base_dados, monkeypatch):
    chamadas = _mock_provider(monkeypatch)
    _configurar_templates(monkeypatch)
    _configurar_servico(rebook_days=21)
    a = _completar_isolado("41790060010", chamadas)
    marcar("41790060010", "limpeza_pele", data_pt("2026-10-20"), "10:00")
    job = _job_rebooking(a)[0]
    resumo = notif_jobs.process_due_jobs(agora=job["run_at"])
    assert resumo["cancelados"] == 1
    assert chamadas == []
    estado = notif_followup.estado_rebooking_para_ui(a)
    assert estado["estado"] == "cancelado_marcacao_existente"


def test_booking_futura_cancelada_nao_impede_envio(base_dados, monkeypatch):
    chamadas = _mock_provider(monkeypatch)
    _configurar_templates(monkeypatch)
    _configurar_servico(rebook_days=21)
    a = _completar_isolado("41790060011", chamadas)
    outra = marcar("41790060011", "limpeza_pele", data_pt("2026-10-20"), "10:00")
    bot.marcar_agendamento_cancelado(outra, exigir_confirmado=False)
    job = _job_rebooking(a)[0]
    resumo = notif_jobs.process_due_jobs(agora=job["run_at"])
    assert resumo["concluidos"] == 1
    assert len(chamadas) == 1


def test_cliente_bloqueado_impede_envio(base_dados, monkeypatch):
    chamadas = _mock_provider(monkeypatch)
    _configurar_templates(monkeypatch)
    _configurar_servico(rebook_days=21)
    a = _completar_isolado("41790060012", chamadas)
    cid = _customer_id("41790060012")
    with db.ligacao() as conn:
        conn.execute("UPDATE customers SET blocked = 1 WHERE id = ?", (cid,))
    job = _job_rebooking(a)[0]
    resumo = notif_jobs.process_due_jobs(agora=job["run_at"])
    assert resumo["cancelados"] == 1
    assert chamadas == []


def test_cliente_opt_out_impede_envio(base_dados, monkeypatch):
    chamadas = _mock_provider(monkeypatch)
    _configurar_templates(monkeypatch)
    _configurar_servico(rebook_days=21)
    a = _completar_isolado("41790060013", chamadas)
    cid = _customer_id("41790060013")
    with db.ligacao() as conn:
        conn.execute("UPDATE customers SET follow_up_opt_out = 1 WHERE id = ?", (cid,))
    job = _job_rebooking(a)[0]
    resumo = notif_jobs.process_due_jobs(agora=job["run_at"])
    assert resumo["cancelados"] == 1
    assert chamadas == []


def test_demo_nunca_chama_o_provider_mas_job_conclui(base_dados, monkeypatch):
    chamadas = _mock_provider(monkeypatch)
    _configurar_templates(monkeypatch)
    _configurar_servico(rebook_days=21)
    telefone_demo = f"{config.DEMO_PHONE_PREFIX}0002"
    a = _completar_isolado(telefone_demo, chamadas, nome="Cliente Demo")
    job = _job_rebooking(a)[0]
    resumo = notif_jobs.process_due_jobs(agora=job["run_at"])
    assert resumo["concluidos"] == 1
    assert chamadas == []                       # nunca chega à Meta
    ag = bot.obter_agendamento(a)
    assert ag["follow_up_status"] == "sent"


def test_servico_desativado_depois_de_agendado_impede_envio(base_dados, monkeypatch):
    chamadas = _mock_provider(monkeypatch)
    _configurar_templates(monkeypatch)
    _configurar_servico(rebook_days=21)
    a = _completar_isolado("41790060014", chamadas)
    db.atualizar_servico("limpeza_pele", {"ativo": False})
    job = _job_rebooking(a)[0]
    resumo = notif_jobs.process_due_jobs(agora=job["run_at"])
    assert resumo["cancelados"] == 1
    assert chamadas == []


# ===========================================================================
# WHATSAPP — template Meta por idioma + botões
# ===========================================================================
@pytest.mark.parametrize("idioma,template,codigo", [
    ("pt", "rebooking_pt", "pt_PT"), ("de", "rebooking_de", "de"), ("en", "rebooking_en", "en_US")])
def test_envia_template_no_idioma_do_cliente(base_dados, monkeypatch, idioma, template, codigo):
    chamadas = _mock_provider(monkeypatch)
    _configurar_templates(monkeypatch)
    _configurar_servico(rebook_days=21)
    a = _completar_isolado("41790060020", chamadas, nome="Marta Silva")
    cid = _customer_id("41790060020")
    with db.ligacao() as conn:
        conn.execute("UPDATE customers SET locale = ? WHERE id = ?", (idioma, cid))
    job = _job_rebooking(a)[0]
    notif_jobs.process_due_jobs(agora=job["run_at"])
    assert len(chamadas) == 1
    payload = chamadas[0][1]
    assert payload["template"]["name"] == template
    assert payload["template"]["language"]["code"] == codigo
    payloads_botoes = [c["parameters"][0]["payload"] for c in payload["template"]["components"]
                       if c["type"] == "button"]
    assert payloads_botoes == [f"followup_marcar_limpeza_pele", f"followup_depois_{a}"]


def test_template_nao_configurado_falha_e_fica_visivel_no_attention_center(base_dados, monkeypatch):
    _mock_provider(monkeypatch)
    _configurar_servico(rebook_days=21)
    a = _completar("41790060021")
    job = _job_rebooking(a)[0]
    for _ in range(notif_jobs.MAX_TENTATIVAS):
        notif_jobs.process_due_jobs(agora=job["run_at"])
    linhas = _job_rebooking(a)
    assert linhas[0]["status"] == notif_jobs.FAILED
    import operations.engine as op
    itens = op.attention_items()
    assert any(i["tipo"] == "automacao_falhou" for i in itens)


# ===========================================================================
# FLOW — "Marcar novamente" reaproveita o fluxo normal, com booking_source
# ===========================================================================
def test_marcar_novamente_pre_seleciona_servico_e_marca_origem(cliente_http, base_dados, monkeypatch):
    """"Marcar novamente" (botão da mensagem de rebooking) entra no MESMO
    passo de escolha de data que qualquer outra marcação nova — nada de
    segundo fluxo — mas com booking_source já marcado como
    'rebooking_followup' (nunca 'whatsapp_bot') antes de sequer perguntar
    a data."""
    tel = "41790060030"
    bot.guardar_sessao(tel, {"idioma": "pt", "nome": "Cliente Teste"})
    r = _post_webhook(cliente_http, _botao("followup_marcar_limpeza_pele", tel, "f1"))
    assert r.status_code == 200
    sessao = bot.carregar_sessao(tel)
    assert sessao["servico_id"] == "limpeza_pele"
    assert sessao["fluxo"] == "beauty"
    assert sessao["booking_source"] == "rebooking_followup"


def test_booking_final_via_marcar_novamente_tem_booking_source_rebooking(base_dados):
    """A sessão marcada por "Marcar novamente" chega intacta até
    guardar_agendamento() — a marcação final fica com
    booking_source='rebooking_followup', nunca 'whatsapp_bot' (ver
    bot.guardar_agendamento: sessao.get("booking_source") or "whatsapp_bot")."""
    servico = db.obter_servico("limpeza_pele")
    sessao = {
        "idioma": "pt", "nome": "Cliente Teste", "booking_source": "rebooking_followup",
        "servico_id": "limpeza_pele", "servico": catalogo.nome_pt(servico),
        "duracao": catalogo.duracao_label(servico["duracao_min"]),
        "duracao_min": servico["duracao_min"], "preco_cents": servico["preco_cents"],
        "preco": round(servico["preco_cents"] / 100, 2) if servico["preco_cents"] is not None else None,
        "data": DIA_TXT, "hora": "10:00",
    }
    a = bot.guardar_agendamento("41790060031", sessao)
    with db.ligacao() as c:
        origem = c.execute("SELECT booking_source FROM agendamentos WHERE id = ?", (a,)).fetchone()[0]
    assert origem == "rebooking_followup"


# ===========================================================================
# MAIS TARDE — snooze/cooldown, nunca opt-out permanente
# ===========================================================================
def test_mais_tarde_marca_declined_e_regista_evento(base_dados, monkeypatch):
    _mock_provider(monkeypatch)
    _configurar_templates(monkeypatch)
    _configurar_servico(rebook_days=21)
    a = _completar("41790060040")
    notif_followup.marcar_follow_up_recusado(a)
    ag = bot.obter_agendamento(a)
    assert ag["follow_up_status"] == "declined"
    eventos = db.eventos_da_entidade("appointment", a)
    assert any(e["type"] == "rebooking_followup.snoozed" for e in eventos)


def test_mais_tarde_nunca_vira_opt_out_do_cliente(base_dados):
    _configurar_servico(rebook_days=21)
    a = _completar("41790060041")
    cid = _customer_id("41790060041")
    notif_followup.marcar_follow_up_recusado(a)
    cust = db.obter_customer(cid)
    with db.ligacao() as conn:
        opt = conn.execute("SELECT follow_up_opt_out FROM customers WHERE id = ?", (cid,)).fetchone()[0]
    assert not opt
    # a PRÓXIMA marcação concluída gera o SEU follow-up do zero
    b = _completar("41790060041", hora="11:00")
    assert bot.obter_agendamento(b)["follow_up_status"] is None
    assert len(_job_rebooking(b)) == 1


# ===========================================================================
# IDEMPOTÊNCIA — dois process_due_jobs -> um único envio
# ===========================================================================
def test_processar_duas_vezes_envia_uma_unica_mensagem(base_dados, monkeypatch):
    chamadas = _mock_provider(monkeypatch)
    _configurar_templates(monkeypatch)
    _configurar_servico(rebook_days=21)
    a = _completar_isolado("41790060050", chamadas)
    job = _job_rebooking(a)[0]
    notif_jobs.process_due_jobs(agora=job["run_at"])
    notif_jobs.process_due_jobs(agora=job["run_at"])
    assert len(chamadas) == 1
    assert _job_rebooking(a)[0]["status"] == notif_jobs.DONE


def test_retry_apos_falha_de_rede_reenvia_com_sucesso(base_dados, monkeypatch):
    chamadas = _mock_provider(monkeypatch, falha_na_chamada=0)
    _configurar_templates(monkeypatch)
    _configurar_servico(rebook_days=21)
    a = _completar_isolado("41790060051", chamadas)
    job = _job_rebooking(a)[0]
    resumo = notif_jobs.process_due_jobs(agora=job["run_at"])
    assert resumo["falharam"] == 1
    assert _job_rebooking(a)[0]["status"] == notif_jobs.PENDING
    resumo2 = notif_jobs.process_due_jobs(agora=job["run_at"])
    assert resumo2["concluidos"] == 1
    assert len(chamadas) == 2


# ===========================================================================
# CRM — próxima manutenção + estado do follow-up
# ===========================================================================
def test_info_rebooking_mostra_proxima_manutencao(base_dados):
    _configurar_servico(rebook_days=42)
    a = _completar("41790060060")
    ag = bot.obter_agendamento(a)
    info = notif_followup.info_rebooking_para_ui(a)
    assert info["rebook_days"] == 42
    from datetime import datetime, timedelta as _td
    esperado = (datetime.fromisoformat(ag["completed_at"].replace("Z", "+00:00")).date()
                + _td(days=42)).isoformat()
    assert info["proxima_manutencao"] == esperado
    assert info["estado"] == "agendado"


def test_estado_rebooking_transita_agendado_para_enviado(base_dados, monkeypatch):
    _mock_provider(monkeypatch)
    _configurar_templates(monkeypatch)
    _configurar_servico(rebook_days=21)
    a = _completar("41790060061")
    assert notif_followup.estado_rebooking_para_ui(a)["estado"] == "agendado"
    job = _job_rebooking(a)[0]
    notif_jobs.process_due_jobs(agora=job["run_at"])
    assert notif_followup.estado_rebooking_para_ui(a)["estado"] == "enviado"


def test_info_rebooking_none_sem_follow_up_configurado(base_dados):
    a = _completar("41790060062")
    assert notif_followup.info_rebooking_para_ui(a) is None
    assert notif_followup.estado_rebooking_para_ui(a) is None


# ===========================================================================
# EVENTOS — scheduled / sent / snoozed, sem duplicados
# ===========================================================================
def test_eventos_scheduled_e_sent_sao_registados_sem_duplicados(base_dados, monkeypatch):
    _mock_provider(monkeypatch)
    _configurar_templates(monkeypatch)
    _configurar_servico(rebook_days=21)
    a = _completar("41790060070")
    notif_followup.agendar_rebooking_followup(a)          # reprocessar não duplica evento (dedupe_key)
    job = _job_rebooking(a)[0]
    notif_jobs.process_due_jobs(agora=job["run_at"])
    eventos = db.eventos_da_entidade("appointment", a)
    scheduled = [e for e in eventos if e["type"] == "rebooking_followup.scheduled"]
    sent = [e for e in eventos if e["type"] == "rebooking_followup.sent"]
    assert len(scheduled) == 1
    assert len(sent) == 1


# ===========================================================================
# REGRESSÃO — P0/P1 continuam intactos (ver tests dedicados; aqui só um
# sanity check cruzado: um serviço com AMBOS reminder 24h e rebooking
# followup ligados não faz um interferir no outro).
# ===========================================================================
def test_rebooking_nao_interfere_com_reminder_24h(base_dados, monkeypatch):
    from notifications import reminders as notif_reminders
    _mock_provider(monkeypatch)
    _configurar_templates(monkeypatch)
    monkeypatch.setattr(config, "WHATSAPP_REMINDER_TEMPLATE_PT", "reminder_24h_pt")
    _configurar_servico(rebook_days=21)
    futuro = tempo.agora_zurique() + timedelta(hours=48)
    a = marcar("41790060080", "limpeza_pele", data_pt(futuro.date().isoformat()), futuro.strftime("%H:%M"))
    bot.disparar_automacoes()
    assert notif_reminders.estado_reminder_para_ui(a)["estado"] == "agendado"
    assert _job_rebooking(a) == []          # ainda não completed
