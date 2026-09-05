"""PATCH P0 — pós-atendimento automático.

booking.completed (já existente) -> fatura criada/reutilizada -> emitida ->
paga em Cash -> job "post_service" +5min -> executor revalida tudo -> WhatsApp
(obrigada -> PDF -> pedido de feedback) -> resposta do cliente associada à
marcação certa. Ver notifications/postservice.py, notifications/jobs.py,
billing/engine.py, billing/pdf.py.

Zero envios reais: o provider é sempre mockado na fronteira HTTP
(requests.post dentro de messaging/whatsapp.py) — o mesmo ponto usado pelos
testes já existentes do composer do Client Manager."""

import base64
from datetime import timedelta

import pytest
import requests

import bot
import config
import db
import estados
import tempo
from billing import engine as bi
from billing import pdf as bi_pdf
from notifications import jobs as notif_jobs
from notifications import postservice as notif_postservice
from conftest import data_pt

AUTH = {"Authorization": "Basic " + base64.b64encode(b"painel:painel-pw").decode()}
DIA = "2026-09-07"          # segunda-feira (dentro do horário semeado)
DIA_TXT = data_pt(DIA)


def _marca(tel, sid, hora, nome="Cliente Teste"):
    s = db.obter_servico(sid)
    sess = {"idioma": "pt", "nome": nome, "servico_id": sid, "servico": s["nome_pt"],
            "duracao_min": s["duracao_min"], "duracao": f"{s['duracao_min']} min",
            "preco_cents": s["preco_cents"],
            "preco": round(s["preco_cents"] / 100, 2) if s["preco_cents"] is not None else None,
            "data": DIA_TXT, "hora": hora}
    return bot.guardar_agendamento(tel, sess)


def _completar(a):
    """O MESMO caminho que o botão "Concluir" do painel usa (POST
    /api/agendamentos/<id>/estado) — direto pela função de domínio + outbox,
    sem precisar do cliente HTTP."""
    bot.atualizar_estado_agendamento(a, "completed")
    bot.disparar_automacoes()


def _job_da_marcacao(a):
    with db.ligacao() as c:
        rows = c.execute(
            "SELECT id, type, run_at, status, attempts, last_error, idempotency_key "
            "FROM automation_jobs WHERE booking_id = ?", (a,)).fetchall()
    campos = ("id", "type", "run_at", "status", "attempts", "last_error", "idempotency_key")
    return [dict(zip(campos, r)) for r in rows]


def _mock_provider(monkeypatch, falha_na_chamada=None):
    """Substitui requests.post (a fronteira real da Meta) por um fake que
    regista as chamadas. `falha_na_chamada` (índice 0-based) faz essa
    chamada levantar RequestException, simulando uma falha de rede real."""
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


# ===========================================================================
# DOMÍNIO — booking.completed é o único gatilho
# ===========================================================================
def test_completed_dispara_fatura_paga_e_job_post_service(base_dados, monkeypatch):
    _mock_provider(monkeypatch)
    a = _marca("41790030001", "limpeza_pele", "09:00", "Ana Müller")
    _completar(a)

    inv = bi.obter_fatura_por_agendamento(a)
    assert inv and inv["status"] == bi.STATUS_PAGA
    assert inv["payment_method"] == bi.PAGAMENTO_CASH
    assert inv["invoice_number"]              # já foi emitida (tem número)
    assert inv["total_cents"] == 8000

    jobs = _job_da_marcacao(a)
    assert len(jobs) == 1
    job = jobs[0]
    assert job["type"] == notif_jobs.TYPE_POST_SERVICE
    assert job["status"] == notif_jobs.PENDING
    assert job["idempotency_key"] == f"post_service:booking:{a}"

    ag = bot.obter_agendamento(a)
    delta = (tempo.parse_iso(job["run_at"]) - tempo.parse_iso(ag["completed_at"])).total_seconds()
    assert 4 * 60 <= delta <= 6 * 60           # ~5 minutos


def test_handler_repetido_nao_duplica_fatura_nem_job(base_dados, monkeypatch):
    _mock_provider(monkeypatch)
    a = _marca("41790030002", "limpeza_pele", "09:00")
    _completar(a)
    ev = {"type": "booking.completed", "entity_id": a, "tenant_id": 1}
    # chamar o handler outra vez à mão (ex.: um retry da outbox) não pode
    # criar uma segunda fatura nem um segundo job
    notif_postservice.handler_booking_completed(ev)
    notif_postservice.handler_booking_completed(ev)

    with db.ligacao() as c:
        n_faturas = c.execute("SELECT COUNT(*) FROM invoices WHERE appointment_id = ?", (a,)).fetchone()[0]
    assert n_faturas == 1
    assert len(_job_da_marcacao(a)) == 1


def test_frontend_concluir_so_chama_o_endpoint_de_estado():
    """A UI ("Concluir") não pode conter lógica de faturação/pós-atendimento
    — só muda o estado; o resto acontece no backend a partir de
    booking.completed. Ver static/dashboard/app.js: concluirEFaturar()."""
    src = open("static/dashboard/app.js", encoding="utf-8").read()
    inicio = src.index("async function concluirEFaturar")
    fim = src.index("\n}", inicio)
    corpo = src[inicio:fim]
    assert "/estado" in corpo
    for proibido in ("/fatura", "/faturas", "postservice", "mensagem", "whatsapp"):
        assert proibido not in corpo.lower()


# ===========================================================================
# INVOICE — reuso, idempotência, preço em falta nunca vira CHF 0
# ===========================================================================
def test_preco_a_confirmar_nao_gera_fatura_nem_job(base_dados, monkeypatch):
    chamadas = _mock_provider(monkeypatch)
    a = _marca("41790030010", "pestanas", "11:00")     # pestanas = preço NULL
    assert bot.obter_agendamento(a)["preco_cents"] is None
    _completar(a)

    assert bi.obter_fatura_por_agendamento(a) is None
    assert _job_da_marcacao(a) == []
    # a marcação continua completed — a faturação nunca bloqueia o atendimento
    ag = bot.obter_agendamento(a)
    assert estados.normalizar(ag["estado"]) == estados.COMPLETED
    assert ag["op_status"] == "done"
    assert chamadas == []


def test_invoice_ja_existente_e_reutilizada_pelo_handler(base_dados, monkeypatch):
    _mock_provider(monkeypatch)
    a = _marca("41790030011", "limpeza_pele", "09:00")
    inv_manual = bi.gerar_fatura_de_marcacao(a)      # já existe um rascunho manual
    _completar(a)
    inv = bi.obter_fatura_por_agendamento(a)
    assert inv["id"] == inv_manual["id"]
    assert inv["status"] == bi.STATUS_PAGA


def test_pdf_corresponde_exatamente_a_fatura(base_dados):
    a = _marca("41790030012", "limpeza_pele", "09:00", "Rita Kern")
    inv = bi.emitir_fatura(bi.gerar_fatura_de_marcacao(a)["id"])
    settings = bi.definicoes_faturacao()
    corpo = bi_pdf.gerar_pdf_fatura(inv, settings)
    assert corpo.startswith(b"%PDF-1.4")
    texto = corpo.decode("latin-1", errors="ignore")
    assert inv["invoice_number"] in texto
    assert "Rita Kern" in texto
    assert "80,00" in texto            # 8000 cêntimos, formatação PT
    for proibido in ("IVA (", "UID", "QR-Bill", "IBAN"):
        assert proibido not in texto   # nada inventado/não configurado


# ===========================================================================
# JOB — executor único, +5 min, idempotência, retries
# ===========================================================================
def test_job_nao_executa_antes_da_hora(base_dados, monkeypatch):
    chamadas = _mock_provider(monkeypatch)
    a = _marca("41790030020", "limpeza_pele", "09:00")
    _completar(a)
    job = _job_da_marcacao(a)[0]
    antes = tempo.iso_utc(tempo.parse_iso(job["run_at"]) - timedelta(minutes=1))

    resumo = notif_jobs.process_due_jobs(agora=antes)
    assert resumo["processados"] == 0
    assert chamadas == []
    assert _job_da_marcacao(a)[0]["status"] == notif_jobs.PENDING


def test_job_executa_quando_devido_e_segunda_passagem_nao_duplica(base_dados, monkeypatch):
    chamadas = _mock_provider(monkeypatch)
    a = _marca("41790030021", "limpeza_pele", "09:00")
    _completar(a)
    job = _job_da_marcacao(a)[0]

    resumo = notif_jobs.process_due_jobs(agora=job["run_at"])
    assert resumo["concluidos"] == 1
    ag = bot.obter_agendamento(a)
    assert ag["post_service_thanks_sent_at"] and ag["feedback_requested_at"]
    n_envios = len(chamadas)
    assert n_envios >= 2                      # obrigada + pedido de feedback

    # segunda passagem (ex.: dois pingers do cron a correr perto um do outro)
    resumo2 = notif_jobs.process_due_jobs(agora=job["run_at"])
    assert resumo2["processados"] == 0
    assert len(chamadas) == n_envios          # nada foi reenviado


def test_retry_grava_erro_e_nao_reenvia_efeito_ja_confirmado(base_dados, monkeypatch):
    # a 2ª chamada ao provider (o pedido de feedback) falha da primeira vez
    chamadas = _mock_provider(monkeypatch, falha_na_chamada=1)
    a = _marca("41790030022", "limpeza_pele", "09:00")
    _completar(a)
    job = _job_da_marcacao(a)[0]

    resumo = notif_jobs.process_due_jobs(agora=job["run_at"])
    assert resumo["falharam"] == 1
    j = _job_da_marcacao(a)[0]
    assert j["status"] == notif_jobs.PENDING and j["attempts"] == 1 and j["last_error"]
    ag = bot.obter_agendamento(a)
    assert ag["post_service_thanks_sent_at"]           # o "obrigada" já tinha tido sucesso
    assert not ag["feedback_requested_at"]              # o feedback ainda não

    n_antes = len(chamadas)
    resumo2 = notif_jobs.process_due_jobs(agora=job["run_at"])
    assert resumo2["concluidos"] == 1
    ag2 = bot.obter_agendamento(a)
    assert ag2["feedback_requested_at"]
    # só o efeito que faltava foi reenviado — o "obrigada" não se repetiu
    textos_obrigada = [j for (_u, j) in chamadas[n_antes:] if "Obrigada" in (j.get("text", {}).get("body", ""))]
    assert textos_obrigada == []


def test_job_sem_pdf_quando_public_base_url_nao_configurado(base_dados, monkeypatch):
    """PUBLIC_BASE_URL vazio (por omissão nos testes, como em produção sem
    esta variável) -> o PDF não pode ser enviado. Marca-se explicitamente
    como não enviado (pdf_sent_at continua None) — nunca se finge um envio
    que não aconteceu; o resto do pós-atendimento continua normalmente."""
    monkeypatch.setattr(config, "PUBLIC_BASE_URL", "")
    _mock_provider(monkeypatch)
    a = _marca("41790030023", "limpeza_pele", "09:00")
    _completar(a)
    job = _job_da_marcacao(a)[0]
    resumo = notif_jobs.process_due_jobs(agora=job["run_at"])
    assert resumo["concluidos"] == 1
    inv = bi.obter_fatura_por_agendamento(a)
    assert inv["pdf_sent_at"] is None


def test_job_envia_pdf_quando_public_base_url_configurado(base_dados, monkeypatch):
    monkeypatch.setattr(config, "PUBLIC_BASE_URL", "https://exemplo.test")
    chamadas = _mock_provider(monkeypatch)
    a = _marca("41790030024", "limpeza_pele", "09:00")
    _completar(a)
    job = _job_da_marcacao(a)[0]
    resumo = notif_jobs.process_due_jobs(agora=job["run_at"])
    assert resumo["concluidos"] == 1
    inv = bi.obter_fatura_por_agendamento(a)
    assert inv["pdf_sent_at"]
    documentos = [j for (_u, j) in chamadas if j.get("type") == "document"]
    assert len(documentos) == 1
    assert documentos[0]["document"]["link"].startswith("https://exemplo.test/faturas/pdf/")


# ===========================================================================
# REVALIDAÇÃO — a marcação pode mudar durante os 5 minutos
# ===========================================================================
def test_revalidacao_marcacao_reaberta_nao_envia(base_dados, monkeypatch):
    chamadas = _mock_provider(monkeypatch)
    a = _marca("41790030030", "limpeza_pele", "09:00")
    _completar(a)
    job = _job_da_marcacao(a)[0]

    # a marcação foi corrigida/reaberta antes do job correr
    with db.ligacao() as c:
        c.execute("UPDATE agendamentos SET estado = 'confirmed', op_status = 'scheduled' WHERE id = ?", (a,))

    resumo = notif_jobs.process_due_jobs(agora=job["run_at"])
    assert resumo["cancelados"] == 1
    assert chamadas == []
    assert _job_da_marcacao(a)[0]["status"] == notif_jobs.CANCELLED


def test_revalidacao_cliente_bloqueado_nao_envia(base_dados, monkeypatch):
    chamadas = _mock_provider(monkeypatch)
    a = _marca("41790030031", "limpeza_pele", "09:00")
    _completar(a)
    cid = bot.obter_agendamento(a)["customer_id"]
    with db.ligacao() as c:
        c.execute("UPDATE customers SET blocked = 1 WHERE id = ?", (cid,))
    job = _job_da_marcacao(a)[0]

    resumo = notif_jobs.process_due_jobs(agora=job["run_at"])
    assert resumo["cancelados"] == 1
    assert chamadas == []


def test_revalidacao_sem_telefone_valido_nao_envia(base_dados, monkeypatch):
    chamadas = _mock_provider(monkeypatch)
    a = _marca("41790030032", "limpeza_pele", "09:00")
    _completar(a)
    cid = bot.obter_agendamento(a)["customer_id"]
    with db.ligacao() as c:
        c.execute("UPDATE customers SET phone = '' WHERE id = ?", (cid,))
        c.execute("UPDATE agendamentos SET telefone = '' WHERE id = ?", (a,))
    job = _job_da_marcacao(a)[0]

    resumo = notif_jobs.process_due_jobs(agora=job["run_at"])
    assert resumo["cancelados"] == 1
    assert chamadas == []


def test_revalidacao_fatura_invalida_cancela(base_dados, monkeypatch):
    """Defensivo: mesmo que uma fatura pare de ser válida por um caminho
    inesperado, o executor nunca envia com base numa fatura que não está
    emitida/paga."""
    chamadas = _mock_provider(monkeypatch)
    a = _marca("41790030033", "limpeza_pele", "09:00")
    _completar(a)
    inv = bi.obter_fatura_por_agendamento(a)
    with db.ligacao() as c:
        c.execute("UPDATE invoices SET status = 'cancelled' WHERE id = ?", (inv["id"],))
    job = _job_da_marcacao(a)[0]

    resumo = notif_jobs.process_due_jobs(agora=job["run_at"])
    assert resumo["cancelados"] == 1
    assert chamadas == []


# ===========================================================================
# WHATSAPP — textos PT/DE/EN, DEMO nunca chama o provider
# ===========================================================================
def test_textos_pt_de_en_existem():
    for chave in ("pos_atendimento_obrigada", "pos_atendimento_pdf_legenda",
                  "pos_atendimento_feedback_pedido", "pos_atendimento_feedback_obrigada"):
        for idioma in ("pt", "de", "en"):
            assert bot.t(chave, idioma).strip()


def test_demo_nunca_chama_o_provider(base_dados, monkeypatch):
    chamadas = _mock_provider(monkeypatch)
    telefone_demo = f"{config.DEMO_PHONE_PREFIX}0001"
    a = _marca(telefone_demo, "limpeza_pele", "09:00", "Cliente Demo")
    _completar(a)
    job = _job_da_marcacao(a)[0]
    resumo = notif_jobs.process_due_jobs(agora=job["run_at"])
    assert resumo["concluidos"] == 1
    assert chamadas == []                       # nunca chega à Meta
    ag = bot.obter_agendamento(a)
    assert ag["post_service_thanks_sent_at"] and ag["feedback_requested_at"]


# ===========================================================================
# FEEDBACK — associação segura à marcação certa
# ===========================================================================
def test_feedback_associa_se_ha_pedido_em_aberto(base_dados, monkeypatch):
    _mock_provider(monkeypatch)
    a = _marca("41790030040", "limpeza_pele", "09:00")
    _completar(a)
    job = _job_da_marcacao(a)[0]
    notif_jobs.process_due_jobs(agora=job["run_at"])

    aid = notif_postservice.tentar_guardar_feedback("41790030040", "Adorei o resultado!")
    assert aid == a
    ag = bot.obter_agendamento(a)
    assert ag["feedback_text"] == "Adorei o resultado!" and ag["feedback_at"]
    eventos = db.eventos_da_entidade("appointment", a)
    assert "feedback.received" in {e["type"] for e in eventos}


def test_feedback_sem_pedido_pendente_nao_associa_nada(base_dados, monkeypatch):
    _mock_provider(monkeypatch)
    a = _marca("41790030041", "limpeza_pele", "09:00")
    # nunca chegou a correr o job -> nenhum pedido de feedback em aberto
    assert notif_postservice.tentar_guardar_feedback("41790030041", "Olá") is None
    assert bot.obter_agendamento(a)["feedback_text"] is None


def test_feedback_aparece_no_client_manager(cliente_http, base_dados, monkeypatch):
    monkeypatch.setattr(config, "PUBLIC_BASE_URL", "https://exemplo.test")
    _mock_provider(monkeypatch)
    a = _marca("41790030042", "limpeza_pele", "09:00", "Sofia Weber")
    _completar(a)
    job = _job_da_marcacao(a)[0]
    notif_jobs.process_due_jobs(agora=job["run_at"])
    notif_postservice.tentar_guardar_feedback("41790030042", "Adorei!")

    cid = bot.obter_agendamento(a)["customer_id"]
    r = cliente_http.get(f"/api/clientes/{cid}", headers=AUTH)
    assert r.status_code == 200
    historico = r.get_json()["historico"]
    visita = next(v for v in historico if v["id"] == a)
    assert visita["feedback_text"] == "Adorei!"
    assert visita["fatura"]["status"] == bi.STATUS_PAGA
    assert visita["fatura"]["payment_method"] == bi.PAGAMENTO_CASH
    assert visita["fatura"]["pdf_sent_at"]


# ===========================================================================
# INSTRUMENTAÇÃO — booking_source
# ===========================================================================
def test_booking_do_bot_fica_whatsapp_bot(base_dados):
    a = _marca("41790030050", "limpeza_pele", "09:00")
    with db.ligacao() as c:
        origem = c.execute("SELECT booking_source FROM agendamentos WHERE id = ?", (a,)).fetchone()[0]
    assert origem == "whatsapp_bot"


def test_booking_do_dashboard_fica_dashboard(cliente_http, base_dados):
    r = cliente_http.post("/api/agendamentos", json={
        "telefone": "41790030051", "nome": "Cliente Painel",
        "servico_id": "limpeza_pele", "data": DIA, "hora": "10:00",
    }, headers=AUTH)
    assert r.status_code == 201
    aid = r.get_json()["agendamento"]["id"]
    with db.ligacao() as c:
        origem = c.execute("SELECT booking_source FROM agendamentos WHERE id = ?", (aid,)).fetchone()[0]
    assert origem == "dashboard"


def test_booking_source_nunca_e_assumido_para_registos_antigos(base_dados):
    """Uma linha inserida por fora dos dois caminhos conhecidos (ex.: um
    registo antigo, de antes desta coluna existir) nunca vira "whatsapp_bot"
    por omissão — fica "unknown" (default da coluna, migração 18)."""
    with db.ligacao() as c:
        cur = c.execute(
            "INSERT INTO agendamentos (telefone, nome, servico, data, hora, estado, criado_em, tenant_id) "
            "VALUES ('41799990000', 'Legado', 'Serviço', '01.01.2020', '10:00', 'confirmed', ?, 1)",
            (tempo.iso_utc(),))
        aid = cur.lastrowid
    assert bot.obter_agendamento(aid)["booking_source"] == "unknown"


# ===========================================================================
# PDF por token / reenvio manual (painel)
# ===========================================================================
def test_download_pdf_pelo_token(cliente_http, base_dados):
    a = _marca("41790030060", "limpeza_pele", "09:00", "Nina Roth")
    inv = bi.emitir_fatura(bi.gerar_fatura_de_marcacao(a)["id"])
    token = bi.garantir_pdf_token(inv["id"])
    r = cliente_http.get(f"/faturas/pdf/{token}")
    assert r.status_code == 200
    assert r.mimetype == "application/pdf"
    assert r.data.startswith(b"%PDF-1.4")


def test_reenviar_pdf_via_api(cliente_http, base_dados, monkeypatch):
    monkeypatch.setattr(config, "PUBLIC_BASE_URL", "https://exemplo.test")
    chamadas = _mock_provider(monkeypatch)
    a = _marca("41790030061", "limpeza_pele", "09:00")
    inv = bi.marcar_paga(bi.emitir_fatura(bi.gerar_fatura_de_marcacao(a)["id"])["id"])

    r = cliente_http.post(f"/api/faturas/{inv['id']}/reenviar", headers=AUTH)
    assert r.status_code == 200
    assert r.get_json()["pdf_sent_at"]
    documentos = [j for (_u, j) in chamadas if j.get("type") == "document"]
    assert len(documentos) == 1
