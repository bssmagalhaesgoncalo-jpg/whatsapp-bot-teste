"""PATCH P1 — reminder automático 24h.

booking.created/.approved/.rescheduled/.cancelled/.completed/.no_show (já
existentes) -> sincronizar_reminder_24h resincroniza o MESMO job
"reminder_24h" (nunca dois) -> run_at vencido -> executor REVALIDA tudo ->
WhatsApp TEMPLATE (Confirmar/Reagendar/Cancelar). Ver
notifications/reminders.py, notifications/jobs.py.

Zero envios reais: o provider é sempre mockado na fronteira HTTP, tal como
em tests/test_pos_atendimento.py (mesmo padrão)."""

import base64
import hashlib
import hmac
import json
from datetime import timedelta

import pytest
import requests

import bot
import config
import db
import estados
import tempo
from notifications import jobs as notif_jobs
from notifications import reminders as notif_reminders
from conftest import data_pt

AUTH = {"Authorization": "Basic " + base64.b64encode(b"painel:painel-pw").decode()}


def _futuro(horas):
    """(data_iso, hora_hhmm) a `horas` a partir de AGORA (Europe/Zurique) —
    nunca uma data fixa: a elegibilidade do reminder depende de "no futuro",
    por isso os testes têm de ser relativos ao momento em que correm."""
    momento = tempo.agora_zurique() + timedelta(hours=horas)
    return momento.date().isoformat(), momento.strftime("%H:%M")


def _marca(tel, sid, dia_iso, hora, nome="Cliente Teste"):
    s = db.obter_servico(sid)
    sess = {"idioma": "pt", "nome": nome, "servico_id": sid, "servico": s["nome_pt"],
            "duracao_min": s["duracao_min"], "duracao": f"{s['duracao_min']} min",
            "preco_cents": s["preco_cents"],
            "preco": round(s["preco_cents"] / 100, 2) if s["preco_cents"] is not None else None,
            "data": data_pt(dia_iso), "hora": hora}
    return bot.guardar_agendamento(tel, sess)


def _marca_futura(tel, sid, horas=48, nome="Cliente Teste"):
    dia_iso, hora = _futuro(horas)
    return _marca(tel, sid, dia_iso, hora, nome)


def _job_reminder(a):
    with db.ligacao() as c:
        rows = c.execute(
            "SELECT id, type, run_at, status, attempts, last_error, idempotency_key "
            "FROM automation_jobs WHERE booking_id = ? AND type = ?",
            (a, notif_jobs.TYPE_REMINDER_24H)).fetchall()
    campos = ("id", "type", "run_at", "status", "attempts", "last_error", "idempotency_key")
    return [dict(zip(campos, r)) for r in rows]


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
    monkeypatch.setattr(config, "WHATSAPP_REMINDER_TEMPLATE_PT", "reminder_24h_pt")
    monkeypatch.setattr(config, "WHATSAPP_REMINDER_TEMPLATE_DE", "reminder_24h_de")
    monkeypatch.setattr(config, "WHATSAPP_REMINDER_TEMPLATE_EN", "reminder_24h_en")


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
    """Toque num quick-reply de uma mensagem de TEMPLATE — formato "button",
    DIFERENTE do "interactive" das nossas próprias mensagens (ver
    bot.receber_mensagem)."""
    return {"from": tel, "id": mid, "type": "button",
            "button": {"payload": payload, "text": payload}}


def _botao_interativo(tel, rid, mid):
    return {"from": tel, "id": mid, "type": "interactive",
            "interactive": {"type": "button_reply", "button_reply": {"id": rid, "title": rid}}}


def _lista(tel, rid, titulo, mid):
    return {"from": tel, "id": mid, "type": "interactive",
            "interactive": {"type": "list_reply", "list_reply": {"id": rid, "title": titulo}}}


# ===========================================================================
# 1-2 — CRIAÇÃO do job + run_at correto
# ===========================================================================
def test_confirmada_futura_cria_job_com_run_at_24h_antes(base_dados):
    a = _marca_futura("41790050001", "limpeza_pele", horas=48)
    bot.disparar_automacoes()
    jobs = _job_reminder(a)
    assert len(jobs) == 1
    job = jobs[0]
    assert job["status"] == notif_jobs.PENDING
    assert job["idempotency_key"] == f"reminder_24h:{a}"

    ag = bot.obter_agendamento(a)
    inicio = tempo.combinar_local(ag["data_iso"], ag["hora_hhmm"])
    esperado = tempo.iso_utc(inicio - timedelta(hours=24))
    assert job["run_at"] == esperado


def test_dst_run_at_mantem_o_mesmo_horario_local_no_dia_anterior(base_dados):
    """2026-10-25 é o dia em que a Suíça sai do horário de verão — a
    marcação fica em CET (+1) mas o reminder do dia anterior ainda em
    CEST (+2). run_at tem de refletir a MESMA hora local (10:00) no dia
    anterior, não uma subtração ingénua de 24h absolutas em UTC (que daria
    9h locais em vez de 10h)."""
    from zoneinfo import ZoneInfo
    a = _marca("41790050002", "limpeza_pele", "2026-10-25", "10:00")
    bot.disparar_automacoes()
    job = _job_reminder(a)[0]

    zurique = ZoneInfo("Europe/Zurich")
    run_at_local = tempo.parse_iso(job["run_at"]).astimezone(zurique)
    assert run_at_local.date().isoformat() == "2026-10-24"
    assert run_at_local.strftime("%H:%M") == "10:00"

    ag = bot.obter_agendamento(a)
    inicio_local = tempo.combinar_local(ag["data_iso"], ag["hora_hhmm"])
    assert inicio_local.utcoffset() != run_at_local.utcoffset()   # atravessou o DST


# ===========================================================================
# 3-4 — NUNCA para marcações a <24h ou pending
# ===========================================================================
def test_marcacao_criada_a_menos_de_24h_nao_cria_job(base_dados):
    a = _marca_futura("41790050003", "limpeza_pele", horas=3)
    bot.disparar_automacoes()
    assert _job_reminder(a) == []


def test_marcacao_pending_nao_cria_job(base_dados, monkeypatch):
    monkeypatch.setattr(config, "BOOKING_REQUIRES_APPROVAL", True)
    monkeypatch.setattr(bot, "BOOKING_REQUIRES_APPROVAL", True, raising=False)
    a = _marca_futura("41790050004", "limpeza_pele", horas=48)
    assert estados.normalizar(bot.obter_agendamento(a)["estado"]) == estados.PENDING
    bot.disparar_automacoes()
    assert _job_reminder(a) == []


# ===========================================================================
# 5 — pending -> confirmed (aprovação) cria o job
# ===========================================================================
def test_aprovacao_pending_para_confirmed_cria_job(base_dados, monkeypatch):
    monkeypatch.setattr(config, "BOOKING_REQUIRES_APPROVAL", True)
    monkeypatch.setattr(bot, "BOOKING_REQUIRES_APPROVAL", True, raising=False)
    a = _marca_futura("41790050005", "limpeza_pele", horas=48)
    bot.disparar_automacoes()
    assert _job_reminder(a) == []

    bot.atualizar_estado_agendamento(a, "confirmed")
    bot.disparar_automacoes()
    jobs = _job_reminder(a)
    assert len(jobs) == 1
    assert jobs[0]["status"] == notif_jobs.PENDING


# ===========================================================================
# 6-8 — nunca envia depois de cancelled/completed/no_show
# ===========================================================================
def test_cancelamento_antes_do_envio_cancela_o_job(base_dados):
    a = _marca_futura("41790050006", "limpeza_pele", horas=48)
    bot.disparar_automacoes()
    assert _job_reminder(a)[0]["status"] == notif_jobs.PENDING

    bot.marcar_agendamento_cancelado(a, exigir_confirmado=False)
    bot.disparar_automacoes()
    job = _job_reminder(a)[0]
    assert job["status"] == notif_jobs.CANCELLED


def test_completado_antes_do_envio_cancela_o_job(base_dados):
    a = _marca_futura("41790050007", "limpeza_pele", horas=48)
    bot.disparar_automacoes()
    bot.atualizar_estado_agendamento(a, "completed")
    bot.disparar_automacoes()
    assert _job_reminder(a)[0]["status"] == notif_jobs.CANCELLED


def test_no_show_antes_do_envio_cancela_o_job(base_dados):
    a = _marca_futura("41790050008", "limpeza_pele", horas=48)
    bot.disparar_automacoes()
    bot.atualizar_estado_agendamento(a, "no_show")
    bot.disparar_automacoes()
    assert _job_reminder(a)[0]["status"] == notif_jobs.CANCELLED


def test_revalidacao_no_executor_marcacao_ja_nao_confirmed(base_dados, monkeypatch):
    """Mesmo que o job já esteja pending/devido, o EXECUTOR revalida de novo
    (defensivo — ver notifications/postservice.py, o mesmo padrão)."""
    _configurar_templates(monkeypatch)
    chamadas = _mock_provider(monkeypatch)
    a = _marca_futura("41790050009", "limpeza_pele", horas=48)
    bot.disparar_automacoes()
    job = _job_reminder(a)[0]
    with db.ligacao() as c:
        c.execute("UPDATE agendamentos SET estado = 'cancelled' WHERE id = ?", (a,))
    resumo = notif_jobs.process_due_jobs(agora=job["run_at"])
    assert resumo["cancelados"] == 1
    assert chamadas == []


# ===========================================================================
# 9 — reagendamento: reminder antigo nunca é enviado, novo run_at correto
# ===========================================================================
def test_reagendamento_atualiza_o_mesmo_job_nunca_cria_um_segundo(base_dados):
    a = _marca_futura("41790050010", "limpeza_pele", horas=48)
    bot.disparar_automacoes()
    job_antigo = _job_reminder(a)[0]

    nova_data, nova_hora = _futuro(96)
    bot.reagendar_agendamento(a, nova_data, nova_hora, origem="dashboard", avisar_cliente=False)
    bot.disparar_automacoes()

    jobs = _job_reminder(a)
    assert len(jobs) == 1                          # nunca dois jobs para a mesma marcação
    job_novo = jobs[0]
    assert job_novo["id"] == job_antigo["id"]       # MESMA linha, reaproveitada
    assert job_novo["run_at"] != job_antigo["run_at"]
    assert job_novo["status"] == notif_jobs.PENDING

    ag = bot.obter_agendamento(a)
    inicio = tempo.combinar_local(ag["data_iso"], ag["hora_hhmm"])
    assert job_novo["run_at"] == tempo.iso_utc(inicio - timedelta(hours=24))


def test_reagendamento_para_menos_de_24h_cancela_o_job(base_dados):
    a = _marca_futura("41790050011", "limpeza_pele", horas=48)
    bot.disparar_automacoes()
    assert _job_reminder(a)[0]["status"] == notif_jobs.PENDING

    nova_data, nova_hora = _futuro(2)
    bot.reagendar_agendamento(a, nova_data, nova_hora, origem="dashboard", avisar_cliente=False)
    bot.disparar_automacoes()
    job = _job_reminder(a)[0]
    assert job["status"] == notif_jobs.CANCELLED


def test_job_ja_enviado_nao_reenvia_apos_marcacao_mudar_de_data(base_dados, monkeypatch):
    """O executor revalida data/hora contra o payload do PRÓPRIO job — se a
    marcação mudou entretanto, cancela em vez de mandar informação
    desatualizada (sincronizar_reminder_24h trata do job certo à parte)."""
    _configurar_templates(monkeypatch)
    chamadas = _mock_provider(monkeypatch)
    a = _marca_futura("41790050012", "limpeza_pele", horas=48)
    bot.disparar_automacoes()
    job = _job_reminder(a)[0]
    with db.ligacao() as c:
        c.execute("UPDATE agendamentos SET data_iso = '2099-01-01', hora_hhmm = '10:00' WHERE id = ?", (a,))
    resumo = notif_jobs.process_due_jobs(agora=job["run_at"])
    assert resumo["cancelados"] == 1
    assert chamadas == []


# ===========================================================================
# 10-12 — idempotência: uma só execução, sem duplicar em retry
# ===========================================================================
def test_execucao_dupla_do_mesmo_job_nao_reenvia(base_dados, monkeypatch):
    _configurar_templates(monkeypatch)
    chamadas = _mock_provider(monkeypatch)
    a = _marca_futura("41790050013", "limpeza_pele", horas=48)
    bot.disparar_automacoes()
    job = _job_reminder(a)[0]

    resumo1 = notif_jobs.process_due_jobs(agora=job["run_at"])
    assert resumo1["concluidos"] == 1
    n_envios = len(chamadas)
    assert n_envios == 1

    resumo2 = notif_jobs.process_due_jobs(agora=job["run_at"])
    assert resumo2["processados"] == 0
    assert len(chamadas) == n_envios


def test_retry_apos_falha_reenvia_uma_unica_vez_com_sucesso(base_dados, monkeypatch):
    _configurar_templates(monkeypatch)
    chamadas = _mock_provider(monkeypatch, falha_na_chamada=0)
    a = _marca_futura("41790050014", "limpeza_pele", horas=48)
    bot.disparar_automacoes()
    job = _job_reminder(a)[0]

    resumo1 = notif_jobs.process_due_jobs(agora=job["run_at"])
    assert resumo1["falharam"] == 1
    j = _job_reminder(a)[0]
    assert j["status"] == notif_jobs.PENDING and j["attempts"] == 1

    resumo2 = notif_jobs.process_due_jobs(agora=job["run_at"])
    assert resumo2["concluidos"] == 1
    assert len(chamadas) == 2                      # 1 falhada + 1 com sucesso, nunca mais


def test_evento_reminder_sent_registado_uma_so_vez(base_dados, monkeypatch):
    _configurar_templates(monkeypatch)
    _mock_provider(monkeypatch)
    a = _marca_futura("41790050015", "limpeza_pele", horas=48)
    bot.disparar_automacoes()
    job = _job_reminder(a)[0]
    notif_jobs.process_due_jobs(agora=job["run_at"])
    notif_jobs.process_due_jobs(agora=job["run_at"])   # segunda passagem — nada a fazer

    eventos = db.eventos_da_entidade("appointment", a)
    sents = [e for e in eventos if e["type"] == "reminder.sent"]
    assert len(sents) == 1


# ===========================================================================
# 13 — DEMO nunca chega à Meta
# ===========================================================================
def test_demo_nunca_chama_o_provider(base_dados, monkeypatch):
    _configurar_templates(monkeypatch)
    chamadas = _mock_provider(monkeypatch)
    telefone_demo = f"{config.DEMO_PHONE_PREFIX}0002"
    a = _marca_futura(telefone_demo, "limpeza_pele", horas=48, nome="Cliente Demo")
    bot.disparar_automacoes()
    job = _job_reminder(a)[0]
    resumo = notif_jobs.process_due_jobs(agora=job["run_at"])
    assert resumo["concluidos"] == 1
    assert chamadas == []


# ===========================================================================
# 14-16 — templates PT/DE/EN
# ===========================================================================
@pytest.mark.parametrize("idioma,template_esperado,codigo_meta", [
    ("pt", "reminder_24h_pt", "pt_PT"),
    ("de", "reminder_24h_de", "de"),
    ("en", "reminder_24h_en", "en_US"),
])
def test_template_e_idioma_meta_corretos_por_idioma(base_dados, monkeypatch, idioma, template_esperado, codigo_meta):
    _configurar_templates(monkeypatch)
    chamadas = _mock_provider(monkeypatch)
    a = _marca_futura(f"417900501{idioma}", "limpeza_pele", horas=48, nome="Cliente")
    with db.ligacao() as c:
        cid = c.execute("SELECT customer_id FROM agendamentos WHERE id = ?", (a,)).fetchone()[0]
        c.execute("UPDATE customers SET locale = ? WHERE id = ?", (idioma, cid))
    bot.disparar_automacoes()
    job = _job_reminder(a)[0]
    resumo = notif_jobs.process_due_jobs(agora=job["run_at"])
    assert resumo["concluidos"] == 1
    (_url, corpo) = chamadas[0]
    assert corpo["template"]["name"] == template_esperado
    assert corpo["template"]["language"]["code"] == codigo_meta


def test_template_nao_configurado_falha_e_nunca_finge_envio(base_dados, monkeypatch):
    """Sem WHATSAPP_REMINDER_TEMPLATE_* configurado (omissão nos testes,
    como em produção antes da aprovação Meta), o job falha — nunca envia
    nem finge sucesso."""
    chamadas = _mock_provider(monkeypatch)
    a = _marca_futura("41790050016", "limpeza_pele", horas=48)
    bot.disparar_automacoes()
    job = _job_reminder(a)[0]
    resumo = notif_jobs.process_due_jobs(agora=job["run_at"])
    assert resumo["falharam"] == 1
    assert chamadas == []
    j = _job_reminder(a)[0]
    assert j["status"] == notif_jobs.PENDING and "template" in (j["last_error"] or "").lower()
    eventos = db.eventos_da_entidade("appointment", a)
    assert "reminder.sent" not in {e["type"] for e in eventos}


# ===========================================================================
# 17-19 — botões do cliente: Confirmar / Reagendar / Cancelar
# ===========================================================================
def test_botao_confirmar_regista_evento_sem_mudar_estado(base_dados, monkeypatch):
    _configurar_templates(monkeypatch)
    _mock_provider(monkeypatch)
    tel = "41790050017"
    a = _marca_futura(tel, "limpeza_pele", horas=48)
    bot.disparar_automacoes()
    job = _job_reminder(a)[0]
    notif_jobs.process_due_jobs(agora=job["run_at"])

    estado_antes = bot.obter_agendamento(a)["estado"]
    assert notif_reminders.registar_confirmacao(job["id"], tel) is True
    assert bot.obter_agendamento(a)["estado"] == estado_antes   # nunca reexecuta confirmed->confirmed

    eventos = db.eventos_da_entidade("appointment", a)
    confirmados = [e for e in eventos if e["type"] == "booking.confirmed"]
    assert len(confirmados) == 1
    assert confirmados[0]["payload"]["origin"] == "reminder_24h"
    assert confirmados[0]["payload"]["automation_job_id"] == job["id"]

    # tocar outra vez não duplica o evento
    notif_reminders.registar_confirmacao(job["id"], tel)
    eventos2 = db.eventos_da_entidade("appointment", a)
    assert len([e for e in eventos2 if e["type"] == "booking.confirmed"]) == 1


def test_botao_confirmar_via_webhook_com_tipo_button(cliente_http, base_dados, monkeypatch):
    """A Meta manda o toque num quick-reply de TEMPLATE como type="button",
    não "interactive" — regressão do parsing em bot.receber_mensagem."""
    _configurar_templates(monkeypatch)
    _mock_provider(monkeypatch)
    tel = "41790050018"
    a = _marca_futura(tel, "limpeza_pele", horas=48)
    bot.guardar_sessao(tel, {"idioma": "pt", "nome": "Cliente Teste"})
    bot.disparar_automacoes()
    job = _job_reminder(a)[0]
    notif_jobs.process_due_jobs(agora=job["run_at"])

    r = _post_webhook(cliente_http, _botao_template(tel, f"lembrete_confirmar_{job['id']}", "wm1"))
    assert r.status_code == 200
    eventos = db.eventos_da_entidade("appointment", a)
    assert any(e["type"] == "booking.confirmed" for e in eventos)


def test_botao_reagendar_entra_no_fluxo_existente_e_recalcula_reminder(cliente_http, base_dados, monkeypatch):
    _configurar_templates(monkeypatch)
    _mock_provider(monkeypatch)
    tel = "41790050019"
    a = _marca_futura(tel, "limpeza_pele", horas=48)
    bot.guardar_sessao(tel, {"idioma": "pt", "nome": "Cliente Teste"})
    bot.disparar_automacoes()
    job = _job_reminder(a)[0]
    notif_jobs.process_due_jobs(agora=job["run_at"])

    r = _post_webhook(cliente_http, _botao_template(tel, f"lembrete_reagendar_{job['id']}", "wm2"))
    assert r.status_code == 200
    eventos = db.eventos_da_entidade("appointment", a)
    assert any(e["type"] == "reminder.reschedule_started" for e in eventos)
    # entrou no fluxo de reagendamento real (sessão com reagendar_id) —
    # nenhum "slot engine" novo, é a MESMA sessão de sempre.
    sessao = bot.carregar_sessao(tel)
    assert sessao.get("fluxo") == "reagendar" and sessao.get("reagendar_id") == a

    nova_data, nova_hora = _futuro(96)
    bot.reagendar_agendamento(a, nova_data, nova_hora, origem="whatsapp_bot", avisar_cliente=False)
    bot.disparar_automacoes()
    jobs = _job_reminder(a)
    assert len(jobs) == 1
    assert jobs[0]["id"] == job["id"]              # mesmo job, run_at recalculado
    assert jobs[0]["status"] == notif_jobs.PENDING


def test_botao_cancelar_exige_confirmacao_e_so_cancela_no_segundo_toque(cliente_http, base_dados, monkeypatch):
    _configurar_templates(monkeypatch)
    _mock_provider(monkeypatch)
    tel = "41790050020"
    a = _marca_futura(tel, "limpeza_pele", horas=48)
    bot.guardar_sessao(tel, {"idioma": "pt", "nome": "Cliente Teste"})
    bot.disparar_automacoes()
    job = _job_reminder(a)[0]
    notif_jobs.process_due_jobs(agora=job["run_at"])

    r1 = _post_webhook(cliente_http, _botao_template(tel, f"lembrete_cancelar_{job['id']}", "wm3"))
    assert r1.status_code == 200
    # primeiro toque NUNCA cancela — só mostra o ecrã de confirmação
    assert estados.normalizar(bot.obter_agendamento(a)["estado"]) == estados.CONFIRMED

    r2 = _post_webhook(cliente_http, _botao_interativo(tel, f"cancelar_sim_{a}", "wm4"))
    assert r2.status_code == 200
    assert estados.normalizar(bot.obter_agendamento(a)["estado"]) == estados.CANCELLED

    eventos = db.eventos_da_entidade("appointment", a)
    cancelados = [e for e in eventos if e["type"] == "reminder.cancelled"]
    assert len(cancelados) == 1
    assert cancelados[0]["payload"]["automation_job_id"] == job["id"]


def test_cancelamento_normal_sem_vir_do_reminder_nao_gera_atribuicao(cliente_http, base_dados, monkeypatch):
    """Um cancelamento NORMAL (não veio de nenhum reminder) nunca cria um
    evento reminder.cancelled — a atribuição só existe quando veio mesmo do
    botão do reminder."""
    tel = "41790050021"
    a = _marca_futura(tel, "limpeza_pele", horas=48)
    bot.guardar_sessao(tel, {"idioma": "pt", "nome": "Cliente Teste"})
    bot.disparar_automacoes()

    r1 = _post_webhook(cliente_http, _botao_interativo(tel, f"cancelar_confirmar_{a}", "wm5"))
    assert r1.status_code == 200
    r2 = _post_webhook(cliente_http, _botao_interativo(tel, f"cancelar_sim_{a}", "wm6"))
    assert r2.status_code == 200
    assert estados.normalizar(bot.obter_agendamento(a)["estado"]) == estados.CANCELLED

    eventos = db.eventos_da_entidade("appointment", a)
    assert "reminder.cancelled" not in {e["type"] for e in eventos}


# ===========================================================================
# 20 — telefone/job de OUTRO número nunca é aceite
# ===========================================================================
def test_botao_do_reminder_ignora_telefone_diferente(base_dados, monkeypatch):
    _configurar_templates(monkeypatch)
    _mock_provider(monkeypatch)
    a = _marca_futura("41790050022", "limpeza_pele", horas=48)
    bot.disparar_automacoes()
    job = _job_reminder(a)[0]
    notif_jobs.process_due_jobs(agora=job["run_at"])

    assert notif_reminders.registar_confirmacao(job["id"], "41799990000") is False
    eventos = db.eventos_da_entidade("appointment", a)
    assert "booking.confirmed" not in {e["type"] for e in eventos}


# ===========================================================================
# 21 — booking_source nunca é alterado por nada disto
# ===========================================================================
def test_booking_source_nao_e_alterado_pelo_reminder(base_dados, monkeypatch):
    _configurar_templates(monkeypatch)
    _mock_provider(monkeypatch)
    a = _marca_futura("41790050023", "limpeza_pele", horas=48)
    bot.disparar_automacoes()
    job = _job_reminder(a)[0]
    notif_jobs.process_due_jobs(agora=job["run_at"])
    with db.ligacao() as c:
        origem = c.execute("SELECT booking_source FROM agendamentos WHERE id = ?", (a,)).fetchone()[0]
    assert origem == "whatsapp_bot"


# ===========================================================================
# 22-23 — cliente bloqueado / telefone inválido nunca envia
# ===========================================================================
def test_revalidacao_cliente_bloqueado_nao_envia(base_dados, monkeypatch):
    _configurar_templates(monkeypatch)
    chamadas = _mock_provider(monkeypatch)
    a = _marca_futura("41790050024", "limpeza_pele", horas=48)
    bot.disparar_automacoes()
    cid = bot.obter_agendamento(a)["customer_id"]
    with db.ligacao() as c:
        c.execute("UPDATE customers SET blocked = 1 WHERE id = ?", (cid,))
    job = _job_reminder(a)[0]
    resumo = notif_jobs.process_due_jobs(agora=job["run_at"])
    assert resumo["cancelados"] == 1
    assert chamadas == []


def test_revalidacao_sem_telefone_valido_nao_envia(base_dados, monkeypatch):
    _configurar_templates(monkeypatch)
    chamadas = _mock_provider(monkeypatch)
    a = _marca_futura("41790050025", "limpeza_pele", horas=48)
    bot.disparar_automacoes()
    cid = bot.obter_agendamento(a)["customer_id"]
    with db.ligacao() as c:
        c.execute("UPDATE customers SET phone = '' WHERE id = ?", (cid,))
        c.execute("UPDATE agendamentos SET telefone = '' WHERE id = ?", (a,))
    job = _job_reminder(a)[0]
    resumo = notif_jobs.process_due_jobs(agora=job["run_at"])
    assert resumo["cancelados"] == 1
    assert chamadas == []


# ===========================================================================
# 24 — Attention Center conta jobs "failed" (post_service + reminder_24h)
# ===========================================================================
def test_attention_center_conta_reminder_falhado(base_dados, monkeypatch):
    from operations import engine as ops
    chamadas = _mock_provider(monkeypatch)
    a = _marca_futura("41790050026", "limpeza_pele", horas=48)
    bot.disparar_automacoes()
    job = _job_reminder(a)[0]
    for _ in range(notif_jobs.MAX_TENTATIVAS):
        notif_jobs.process_due_jobs(agora=job["run_at"])
    j = _job_reminder(a)[0]
    assert j["status"] == notif_jobs.FAILED
    assert chamadas == []

    itens = ops.attention_items()
    falhas = [i for i in itens if i["tipo"] == "automacao_falhou"]
    assert len(falhas) == 1


# ===========================================================================
# 25 — UI: estado_reminder_para_ui / api_agendamento_detalhe
# ===========================================================================
def test_estado_reminder_para_ui_agendado_enviado_confirmado(base_dados, monkeypatch):
    _configurar_templates(monkeypatch)
    _mock_provider(monkeypatch)
    a = _marca_futura("41790050027", "limpeza_pele", horas=48)
    bot.disparar_automacoes()
    assert notif_reminders.estado_reminder_para_ui(a)["estado"] == "agendado"

    job = _job_reminder(a)[0]
    notif_jobs.process_due_jobs(agora=job["run_at"])
    assert notif_reminders.estado_reminder_para_ui(a)["estado"] == "enviado"

    tel = bot.obter_agendamento(a)["telefone"]
    notif_reminders.registar_confirmacao(job["id"], tel)
    assert notif_reminders.estado_reminder_para_ui(a)["estado"] == "confirmado"


def test_marcacao_sempre_a_menos_de_24h_nunca_mostra_nada_no_ui(base_dados):
    """V1: nunca chegou a existir job nenhum para esta marcação (nunca esteve
    a >=24h de distância) — o drawer não mostra "não aplicável" nem nada,
    simplesmente omite a linha (None)."""
    a_curta = _marca_futura("41790050028", "limpeza_pele", horas=3)
    bot.disparar_automacoes()
    assert notif_reminders.estado_reminder_para_ui(a_curta) is None

    a_sem_job = _marca_futura("41790050029", "limpeza_pele", horas=48)
    # sem disparar_automacoes(): nunca chegou a existir job nenhum
    assert notif_reminders.estado_reminder_para_ui(a_sem_job) is None


def test_reagendamento_para_menos_de_24h_mostra_nao_aplicavel_no_ui(base_dados):
    """Diferente do caso acima: aqui já existia um job "agendado" (>=24h),
    e um reagendamento posterior invalidou-o (ficou a <24h) — o job existe
    mesmo (cancelled), por isso o UI distingue esse "não aplicável" de um
    cancelamento genuíno da marcação."""
    a = _marca_futura("41790050031", "limpeza_pele", horas=48)
    bot.disparar_automacoes()
    assert notif_reminders.estado_reminder_para_ui(a)["estado"] == "agendado"

    nova_data, nova_hora = _futuro(2)
    bot.reagendar_agendamento(a, nova_data, nova_hora, origem="dashboard", avisar_cliente=False)
    bot.disparar_automacoes()
    assert notif_reminders.estado_reminder_para_ui(a)["estado"] == "nao_aplicavel"


def test_api_agendamento_detalhe_inclui_reminder_24h(cliente_http, base_dados):
    a = _marca_futura("41790050030", "limpeza_pele", horas=48)
    bot.disparar_automacoes()
    r = cliente_http.get(f"/api/agendamentos/{a}", headers=AUTH)
    assert r.status_code == 200
    corpo = r.get_json()
    assert corpo["reminder_24h"]["estado"] == "agendado"


# ===========================================================================
# Textos PT/DE/EN
# ===========================================================================
def test_texto_lembrete_confirmado_existe_nos_3_idiomas():
    for idioma in ("pt", "de", "en"):
        assert bot.t("lembrete_confirmado", idioma).strip()
