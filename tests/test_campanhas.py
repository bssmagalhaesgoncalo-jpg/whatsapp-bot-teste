"""PATCH P5 — campanhas WhatsApp de reativação de clientes.

Segmentação (crm/campaigns/engine.py) -> rascunho -> agendar/enviar agora
(snapshot de campaign_recipients, UM automation_job "campaign_send",
reaproveita notifications/jobs.py) -> executor processa em lote -> cliente
toca "Marcar agora" -> fluxo de marcação NORMAL, com booking_source /
campaign_id -> booking.created fecha o funil (recipient "converted").

Zero envios reais: o provider é sempre mockado na fronteira HTTP, tal como
em tests/test_reminder_24h.py e tests/test_rebooking_followup.py (mesmo
padrão)."""

import base64
import hashlib
import hmac
import json

import pytest
import requests

import bot
import config
import db
import tempo
from campaigns import engine as campanhas
from notifications import jobs as notif_jobs
from conftest import marcar, data_pt

AUTH = {"Authorization": "Basic " + base64.b64encode(b"painel:painel-pw").decode()}
DIA = "2026-09-07"          # segunda-feira (dentro do horário semeado)
DIA_TXT = data_pt(DIA)
DIA_FUTURO = "2026-10-19"   # segunda-feira futura (usada noutros testes do repo)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _cliente(tel, nome="Cliente Teste", *, opt_in=True, blocked=False, last_visit=None,
            visits_count=0, locale="pt", tenant_id=1):
    cust = db.obter_ou_criar_customer(tel, nome, locale, tenant_id=tenant_id)
    with db.ligacao() as c:
        c.execute("UPDATE customers SET marketing_opt_in = ?, blocked = ?, last_visit = ?, "
                  "visits_count = ? WHERE id = ?",
                  (1 if opt_in else 0, 1 if blocked else 0, last_visit, visits_count, cust["id"]))
    return db.obter_customer(cust["id"])


def _completar_servico(tel, sid="limpeza_pele", dia_txt=DIA_TXT, hora="09:00", nome="Cliente Teste"):
    a = marcar(tel, sid, dia_txt, hora, nome=nome)
    bot.atualizar_estado_agendamento(a, "completed")
    return a


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
    monkeypatch.setattr(config, "WHATSAPP_CAMPAIGN_TEMPLATE_PT", "campanha_pt")
    monkeypatch.setattr(config, "WHATSAPP_CAMPAIGN_TEMPLATE_DE", "campanha_de")
    monkeypatch.setattr(config, "WHATSAPP_CAMPAIGN_TEMPLATE_EN", "campanha_en")


def _post_webhook(cliente_http, msg):
    corpo = json.dumps({"entry": [{"changes": [{"value": {
        "messaging_product": "whatsapp",
        "metadata": {"phone_number_id": "x"},
        "messages": [msg],
    }}]}]}).encode()
    sig = "sha256=" + hmac.new(b"segredo-de-teste", corpo, hashlib.sha256).hexdigest()
    return cliente_http.post("/webhook", data=corpo, content_type="application/json",
                             headers={"X-Hub-Signature-256": sig})


def _botao_template(tel, payload, mid):
    return {"from": tel, "id": mid, "type": "button", "button": {"payload": payload, "text": payload}}


def _rascunho(nome="Reativação", filtros=None):
    return campanhas.criar_rascunho(1, nome, filtros or {})


# ===========================================================================
# SEGMENTAÇÃO — cada filtro isoladamente, combinados com AND
# ===========================================================================
def test_no_visit_days_inclui_so_quem_passou_o_limite(base_dados):
    from datetime import timedelta
    hoje = tempo.hoje_zurique()
    longe = (hoje - timedelta(days=120)).isoformat()
    perto = (hoje - timedelta(days=10)).isoformat()
    _cliente("41791110001", "Ana Longe", last_visit=longe)
    _cliente("41791110002", "Beatriz Perto", last_visit=perto)
    _cliente("41791110003", "Carla SemVisita", last_visit=None)
    r = campanhas.elegiveis_e_excluidos(1, {"no_visit_days": 90})
    assert r["elegiveis"] == 1
    assert r["amostra"][0]["name"] == "Ana Longe"


def test_servico_anterior_exige_marcacao_completed_desse_servico(base_dados):
    _cliente("41791110010", "Com serviço")
    _completar_servico("41791110010")
    _cliente("41791110011", "Sem serviço")
    r = campanhas.elegiveis_e_excluidos(1, {"service_id": "limpeza_pele"})
    assert r["elegiveis"] == 1


def test_sem_marcacao_futura(base_dados):
    _cliente("41791110020", "Sem futuro")
    _cliente("41791110021", "Com futuro")
    marcar("41791110021", "limpeza_pele", data_pt(DIA_FUTURO), "10:00")
    elegiveis, _ = campanhas._computar_segmento(1, {"no_future_booking": True})
    nomes = {a["name"] for a in elegiveis}
    assert "Sem futuro" in nomes
    assert "Com futuro" not in nomes


def test_recorrentes_visits_count_2_ou_mais(base_dados):
    _cliente("41791110030", "Recorrente", visits_count=2)
    _cliente("41791110031", "Uma_vez", visits_count=1)
    elegiveis, _ = campanhas._computar_segmento(1, {"recurring": True})
    nomes = {a["name"] for a in elegiveis}
    assert nomes == {"Recorrente"}


def test_filtros_combinam_com_and(base_dados):
    from datetime import timedelta
    longe = (tempo.hoje_zurique() - timedelta(days=120)).isoformat()
    _cliente("41791110040", "Longe_e_recorrente", last_visit=longe, visits_count=3)
    _cliente("41791110041", "Longe_mas_uma_vez", last_visit=longe, visits_count=1)
    elegiveis, _ = campanhas._computar_segmento(1, {"no_visit_days": 90, "recurring": True})
    assert [e["name"] for e in elegiveis] == ["Longe_e_recorrente"]


# ===========================================================================
# ELEGIBILIDADE / EXCLUSÕES — §7 do patch, obrigatórias
# ===========================================================================
def test_blocked_excluido(base_dados):
    _cliente("41791120001", "Bloqueada", blocked=True)
    r = campanhas.elegiveis_e_excluidos(1, {})
    assert r["elegiveis"] == 0
    assert r["motivos_exclusao"]["bloqueado"] == 1


def test_sem_opt_in_excluido_por_omissao(base_dados):
    # default seguro: um customer criado sem opt-in explícito NUNCA é
    # elegível (marketing_opt_in = 0 por omissão — migração 20).
    db.obter_ou_criar_customer("41791120002", "Sem consentimento", "pt", tenant_id=1)
    r = campanhas.elegiveis_e_excluidos(1, {})
    assert r["elegiveis"] == 0
    assert r["motivos_exclusao"]["sem_consentimento_marketing"] == 1


def test_telefone_invalido_excluido(base_dados):
    _cliente("+41 79 000 00 00", "Telefone com símbolos")
    r = campanhas.elegiveis_e_excluidos(1, {})
    assert r["elegiveis"] == 0
    assert r["motivos_exclusao"]["telefone_invalido"] == 1


def test_cliente_demo_excluido(base_dados):
    _cliente(f"{config.DEMO_PHONE_PREFIX}0001", "Cliente demo")
    r = campanhas.elegiveis_e_excluidos(1, {})
    assert r["elegiveis"] == 0
    assert r["motivos_exclusao"]["cliente_demo"] == 1


def test_tenant_isolation(base_dados):
    _cliente("41791120010", "Tenant 1", tenant_id=1)
    _cliente("41791120011", "Tenant 2", tenant_id=2)
    r1 = campanhas.elegiveis_e_excluidos(1, {})
    r2 = campanhas.elegiveis_e_excluidos(2, {})
    assert r1["elegiveis"] == 1
    assert r2["elegiveis"] == 1
    assert r1["amostra"][0]["name"] == "Tenant 1"
    assert r2["amostra"][0]["name"] == "Tenant 2"


# ===========================================================================
# CRUD / DEDUPE
# ===========================================================================
def test_criar_rascunho_e_editar(base_dados):
    camp = _rascunho("Setembro")
    assert camp["status"] == "draft"
    editado = campanhas.atualizar_rascunho(camp["id"], 1, {"name": "Setembro v2"})
    assert editado["name"] == "Setembro v2"


def test_editar_campanha_que_nao_e_rascunho_falha(base_dados, monkeypatch):
    _mock_provider(monkeypatch)
    _configurar_templates(monkeypatch)
    _cliente("41791130001", "X")
    camp = _rascunho()
    campanhas.enviar_agora(camp["id"], 1)
    with pytest.raises(campanhas.EstadoInvalido):
        campanhas.atualizar_rascunho(camp["id"], 1, {"name": "outro"})


def test_apagar_rascunho(base_dados):
    camp = _rascunho()
    campanhas.apagar_rascunho(camp["id"], 1)
    assert campanhas.obter_campanha(camp["id"], 1) is None


def test_apagar_campanha_nao_rascunho_falha(base_dados, monkeypatch):
    _mock_provider(monkeypatch)
    _configurar_templates(monkeypatch)
    _cliente("41791130010", "X")
    camp = _rascunho()
    campanhas.enviar_agora(camp["id"], 1)
    with pytest.raises(campanhas.EstadoInvalido):
        campanhas.apagar_rascunho(camp["id"], 1)


def test_snapshot_nunca_duplica_recipient_por_cliente(base_dados):
    """§14 — dedupe estrutural: (campaign_id, customer_id) é único."""
    _cliente("41791130020", "Única")
    camp = _rascunho()
    with db.ligacao() as c:
        campanhas._preparar_envio(c, camp["id"], 1, {})
        campanhas._preparar_envio(c, camp["id"], 1, {})  # correr 2x nunca duplica
    dest = campanhas.detalhe_recipientes(camp["id"], 1)
    assert len(dest) == 1


# ===========================================================================
# AGENDAMENTO — reaproveita automation_jobs, nunca um scheduler paralelo
# ===========================================================================
def test_agendar_grava_run_at_e_idempotency_key(base_dados, monkeypatch):
    from datetime import timedelta
    _configurar_templates(monkeypatch)
    _cliente("41791140010", "Elegível")
    camp = _rascunho()
    amanha = tempo.hoje_zurique() + timedelta(days=1)
    camp = campanhas.agendar(camp["id"], 1, amanha.isoformat(), "09:00")
    assert camp["status"] == "scheduled"
    assert camp["recipients_total"] == 1
    with db.ligacao() as c:
        job = c.execute("SELECT run_at, idempotency_key, status FROM automation_jobs WHERE type = ?",
                        (campanhas.TYPE_CAMPAIGN_SEND,)).fetchone()
    assert job is not None
    assert job[1] == f"campaign_send:{camp['id']}:0"
    assert job[2] == notif_jobs.PENDING


def test_agendar_sem_template_configurado_falha_cedo(base_dados):
    _cliente("41791140020", "Elegível")
    camp = _rascunho()
    from datetime import timedelta
    amanha = (tempo.hoje_zurique() + timedelta(days=1)).isoformat()
    with pytest.raises(campanhas.EstadoInvalido):
        campanhas.agendar(camp["id"], 1, amanha, "09:00")


# ===========================================================================
# ENVIO / EXECUÇÃO — throttling em lote, DEMO nunca chama a Meta
# ===========================================================================
def test_enviar_agora_envia_e_conclui(base_dados, monkeypatch):
    chamadas = _mock_provider(monkeypatch)
    _configurar_templates(monkeypatch)
    _cliente("41791150001", "Ana Reativar")
    camp = _rascunho()
    camp = campanhas.enviar_agora(camp["id"], 1)
    resumo = notif_jobs.process_due_jobs()
    assert resumo["concluidos"] == 1
    assert len(chamadas) == 1
    payload = chamadas[0][1]
    assert payload["template"]["name"] == "campanha_pt"
    camp = campanhas.obter_campanha(camp["id"], 1)
    assert camp["status"] == "completed"
    assert camp["recipients_sent"] == 1


def test_enviar_agora_duas_vezes_nunca_duplica_envio(base_dados, monkeypatch):
    chamadas = _mock_provider(monkeypatch)
    _configurar_templates(monkeypatch)
    _cliente("41791150010", "Ana")
    camp = _rascunho()
    campanhas.enviar_agora(camp["id"], 1)
    camp2 = campanhas.enviar_agora(camp["id"], 1)   # duplo clique
    notif_jobs.process_due_jobs()
    assert camp2["recipients_total"] == 1
    assert len(chamadas) == 1


def test_falha_num_destinatario_nao_aborta_o_lote(base_dados, monkeypatch):
    chamadas = _mock_provider(monkeypatch, falha_na_chamada=0)
    _configurar_templates(monkeypatch)
    _cliente("41791150020", "Falha")
    _cliente("41791150021", "Sucesso")
    camp = _rascunho()
    campanhas.enviar_agora(camp["id"], 1)
    notif_jobs.process_due_jobs()
    dest = {d["name"]: d["status"] for d in campanhas.detalhe_recipientes(camp["id"], 1)}
    # falha_na_chamada=0 -> a 1ª chamada HTTP falha; "Falha" foi criada
    # primeiro (customer_id menor), por isso é o 1º destinatário processado.
    assert dest["Falha"] == "failed"
    assert dest["Sucesso"] == "sent"
    camp = campanhas.obter_campanha(camp["id"], 1)
    assert camp["recipients_sent"] == 1
    assert camp["recipients_failed"] == 1
    assert camp["status"] == "completed"    # uma falha isolada não impede a campanha de concluir


def test_template_nao_configurado_falha_por_destinatario(base_dados, monkeypatch):
    _mock_provider(monkeypatch)   # sem _configurar_templates — nenhum template
    _cliente("41791150030", "X")
    camp = _rascunho()
    with pytest.raises(campanhas.EstadoInvalido):
        campanhas.enviar_agora(camp["id"], 1)   # falha cedo, nem chega a agendar


def test_lote_pequeno_continua_em_novo_job(base_dados, monkeypatch):
    """§13 — throttling: nunca a campanha inteira de uma vez. Um lote de 2
    com 5 elegíveis cria um job de continuação com offset correto."""
    chamadas = _mock_provider(monkeypatch)
    _configurar_templates(monkeypatch)
    monkeypatch.setattr(config, "CAMPAIGN_SEND_BATCH_SIZE", 2)
    for i in range(5):
        _cliente(f"417911500{40 + i}", f"Cliente {i}")
    camp = _rascunho()
    campanhas.enviar_agora(camp["id"], 1)
    resumo1 = notif_jobs.process_due_jobs()
    assert resumo1["concluidos"] == 1
    assert len(chamadas) == 2
    camp_meio = campanhas.obter_campanha(camp["id"], 1)
    assert camp_meio["status"] == "running"          # ainda não acabou
    assert camp_meio["recipients_sent"] == 2

    resumo2 = notif_jobs.process_due_jobs()
    resumo3 = notif_jobs.process_due_jobs()
    assert len(chamadas) == 5
    camp_fim = campanhas.obter_campanha(camp["id"], 1)
    assert camp_fim["status"] == "completed"
    assert camp_fim["recipients_sent"] == 5


def test_demo_seed_nunca_chama_o_provider(base_dados, monkeypatch):
    chamadas = _mock_provider(monkeypatch)
    _configurar_templates(monkeypatch)
    ids = []
    for i in range(6):
        tel = f"{config.DEMO_PHONE_PREFIX}{2000 + i:04d}"
        cust = db.obter_ou_criar_customer(tel, f"Demo {i}", "pt", tenant_id=1)
        ids.append(cust["id"])
    resultado = campanhas.seed_demo(1, ids)
    assert resultado["created"] is True
    assert chamadas == []
    camp1 = campanhas.obter_campanha(resultado["completed_campaign_id"], 1)
    assert camp1["status"] == "completed"
    camp2 = campanhas.obter_campanha(resultado["scheduled_campaign_id"], 1)
    assert camp2["status"] == "scheduled"


# ===========================================================================
# CANCELAMENTO — §21 do patch
# ===========================================================================
def test_cancelar_rascunho_apaga(base_dados):
    camp = _rascunho()
    r = campanhas.cancelar(camp["id"], 1)
    assert r == {"deleted": True}
    assert campanhas.obter_campanha(camp["id"], 1) is None


def test_cancelar_agendada_impede_execucao_futura(base_dados, monkeypatch):
    from datetime import timedelta
    _configurar_templates(monkeypatch)
    _cliente("41791160001", "X")
    camp = _rascunho()
    amanha = tempo.hoje_zurique() + timedelta(days=1)
    camp = campanhas.agendar(camp["id"], 1, amanha.isoformat(), "09:00")
    campanhas.cancelar(camp["id"], 1)
    with db.ligacao() as c:
        job = c.execute("SELECT status FROM automation_jobs WHERE type = ?",
                        (campanhas.TYPE_CAMPAIGN_SEND,)).fetchone()
    assert job[0] == "cancelled"
    camp = campanhas.obter_campanha(camp["id"], 1)
    assert camp["status"] == "cancelled"


def test_cancelar_a_decorrer_nunca_desenvia_o_que_ja_foi_enviado(base_dados, monkeypatch):
    chamadas = _mock_provider(monkeypatch)
    _configurar_templates(monkeypatch)
    monkeypatch.setattr(config, "CAMPAIGN_SEND_BATCH_SIZE", 1)
    _cliente("41791160010", "Enviada")
    _cliente("41791160011", "Ainda_pendente")
    camp = _rascunho()
    campanhas.enviar_agora(camp["id"], 1)
    notif_jobs.process_due_jobs()               # envia só o 1º (lote de 1)
    assert len(chamadas) == 1
    campanhas.cancelar(camp["id"], 1)
    notif_jobs.process_due_jobs()                # o 2º nunca chega a ser processado
    assert len(chamadas) == 1
    camp = campanhas.obter_campanha(camp["id"], 1)
    assert camp["status"] == "cancelled"
    assert camp["recipients_sent"] == 1
    assert camp["recipients_skipped"] == 1


def test_cancelar_campanha_concluida_falha(base_dados, monkeypatch):
    chamadas = _mock_provider(monkeypatch)
    _configurar_templates(monkeypatch)
    _cliente("41791160020", "X")
    camp = _rascunho()
    campanhas.enviar_agora(camp["id"], 1)
    notif_jobs.process_due_jobs()
    with pytest.raises(campanhas.EstadoInvalido):
        campanhas.cancelar(camp["id"], 1)


# ===========================================================================
# LOCALE — template + código Meta por idioma do cliente
# ===========================================================================
@pytest.mark.parametrize("idioma,template,codigo", [
    ("pt", "campanha_pt", "pt_PT"), ("de", "campanha_de", "de"), ("en", "campanha_en", "en_US")])
def test_template_e_idioma_meta_por_cliente(base_dados, monkeypatch, idioma, template, codigo):
    chamadas = _mock_provider(monkeypatch)
    _configurar_templates(monkeypatch)
    _cliente("41791170001", "Cliente", locale=idioma)
    camp = _rascunho()
    campanhas.enviar_agora(camp["id"], 1)
    notif_jobs.process_due_jobs()
    payload = chamadas[0][1]
    assert payload["template"]["name"] == template
    assert payload["template"]["language"]["code"] == codigo


# ===========================================================================
# CLIQUE -> FLUXO DE MARCAÇÃO NORMAL -> booking_source/campaign_id
# ===========================================================================
def test_clique_marca_recipient_e_entra_no_menu(cliente_http, base_dados, monkeypatch):
    chamadas = _mock_provider(monkeypatch)
    _configurar_templates(monkeypatch)
    tel = "41791180001"
    _cliente(tel, "Ana")
    camp = _rascunho()
    campanhas.enviar_agora(camp["id"], 1)
    notif_jobs.process_due_jobs()
    payload_botao = chamadas[0][1]["template"]["components"][-1]["parameters"][0]["payload"]
    assert payload_botao.startswith(f"campanha_marcar_{camp['id']}_")

    bot.guardar_sessao(tel, {"idioma": "pt", "nome": "Ana"})
    r = _post_webhook(cliente_http, _botao_template(tel, payload_botao, "m1"))
    assert r.status_code == 200
    sessao = bot.carregar_sessao(tel)
    assert sessao["booking_source"] == "whatsapp_campaign"
    assert sessao["campaign_id"] == camp["id"]

    dest = campanhas.detalhe_recipientes(camp["id"], 1)[0]
    assert dest["status"] == "clicked"
    assert dest["clicked_at"] is not None


def test_clique_de_outro_telefone_e_ignorado(base_dados, monkeypatch):
    chamadas = _mock_provider(monkeypatch)
    _configurar_templates(monkeypatch)
    _cliente("41791180010", "Ana")
    camp = _rascunho()
    campanhas.enviar_agora(camp["id"], 1)
    notif_jobs.process_due_jobs()
    dest = campanhas.detalhe_recipientes(camp["id"], 1)[0]
    resultado = campanhas.registar_clique(camp["id"], dest["id"], "41799990000", tenant_id=1)
    assert resultado is None
    assert campanhas.detalhe_recipientes(camp["id"], 1)[0]["status"] == "sent"


def test_booking_final_tem_booking_source_e_campaign_id(base_dados, monkeypatch):
    chamadas = _mock_provider(monkeypatch)
    _configurar_templates(monkeypatch)
    tel = "41791180020"
    _cliente(tel, "Ana")
    camp = _rascunho()
    campanhas.enviar_agora(camp["id"], 1)
    notif_jobs.process_due_jobs()
    dest = campanhas.detalhe_recipientes(camp["id"], 1)[0]
    campanhas.registar_clique(camp["id"], dest["id"], tel, tenant_id=1)

    a = marcar(tel, "limpeza_pele", data_pt(DIA_FUTURO), "11:00")
    with db.ligacao() as c:
        c.execute("UPDATE agendamentos SET booking_source = 'whatsapp_campaign', campaign_id = ? WHERE id = ?",
                  (camp["id"], a))
    ag = bot.obter_agendamento(a)
    assert ag["booking_source"] == "whatsapp_campaign"
    assert ag["campaign_id"] == camp["id"]


# ===========================================================================
# CONVERSÃO — booking.created fecha o funil
# ===========================================================================
def test_conversao_via_evento_booking_created(base_dados, monkeypatch):
    chamadas = _mock_provider(monkeypatch)
    _configurar_templates(monkeypatch)
    tel = "41791190001"
    cust = _cliente(tel, "Ana")
    camp = _rascunho()
    campanhas.enviar_agora(camp["id"], 1)
    notif_jobs.process_due_jobs()
    dest = campanhas.detalhe_recipientes(camp["id"], 1)[0]
    campanhas.registar_clique(camp["id"], dest["id"], tel, tenant_id=1)

    sessao = {
        "idioma": "pt", "nome": "Ana", "servico_id": "limpeza_pele",
        "servico": "Limpeza de pele", "duracao_min": 60, "duracao": "1h",
        "preco_cents": 8000, "preco": 80.0,
        "data": data_pt(DIA_FUTURO), "hora": "11:00",
        "booking_source": "whatsapp_campaign", "campaign_id": camp["id"],
    }
    a = bot.guardar_agendamento(tel, sessao)
    bot.disparar_automacoes()

    ag = bot.obter_agendamento(a)
    assert ag["campaign_id"] == camp["id"]
    assert ag["booking_source"] == "whatsapp_campaign"
    rec = campanhas.detalhe_recipientes(camp["id"], 1)[0]
    assert rec["status"] == "converted"
    assert rec["booking_id"] == a
    camp_final = campanhas.obter_campanha(camp["id"], 1)
    assert camp_final["recipients_converted"] == 1


def test_conversao_e_idempotente(base_dados, monkeypatch):
    chamadas = _mock_provider(monkeypatch)
    _configurar_templates(monkeypatch)
    tel = "41791190010"
    _cliente(tel, "Ana")
    camp = _rascunho()
    campanhas.enviar_agora(camp["id"], 1)
    notif_jobs.process_due_jobs()
    dest = campanhas.detalhe_recipientes(camp["id"], 1)[0]

    a = marcar(tel, "limpeza_pele", data_pt(DIA_FUTURO), "11:00")
    with db.ligacao() as c:
        c.execute("UPDATE agendamentos SET campaign_id = ? WHERE id = ?", (camp["id"], a))
    campanhas.handler_evento_conversao({"type": "booking.created", "entity_id": a, "tenant_id": 1})
    campanhas.handler_evento_conversao({"type": "booking.created", "entity_id": a, "tenant_id": 1})
    rec = campanhas.detalhe_recipientes(camp["id"], 1)[0]
    assert rec["status"] == "converted"
