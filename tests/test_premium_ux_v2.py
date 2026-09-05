"""WHATSAPP UX v2 — regressão dos 3 reforços feitos nesta sessão:

  1) lista de datas com rótulos relativos ("Hoje"/"Amanhã"), selecionada
     pelo ID (data_<iso>) — o texto GRAVADO continua sempre o formato
     canónico (dd.mm.yyyy), nunca o rótulo relativo mostrado;
  2) cancelamento nunca acontece ao primeiro toque — passa sempre por um
     ecrã de confirmação ("Sim, cancelar" / "Voltar"); o botão LEGADO
     (cancelar_ag_<id>, cancelamento imediato) continua a funcionar tal e
     qual, para uma mensagem antiga na conversa;
  3) arquitetura (schema + funções puras, sem worker) do follow-up
     automático de reativação — notifications/followup.py — incluindo os
     dois botões que uma futura mensagem de follow-up dispararia."""

import hashlib
import hmac
import json
from datetime import date, timedelta

import pytest

import bot
import db
import estados
import tempo
from notifications import followup as notif_followup
from conftest import marcar, data_pt

CLIENTE = "41791239001"


@pytest.fixture()
def enviados(monkeypatch):
    saida = []
    monkeypatch.setattr(bot, "enviar", lambda payload: saida.append(payload) or None)
    # bot.enviar_texto() vai direto a _wa.enviar_texto (nunca passa por
    # bot.enviar — ver messaging/whatsapp.py), por isso precisa do seu
    # próprio intercetor para as mensagens de texto simples aparecerem aqui.
    monkeypatch.setattr(
        bot, "enviar_texto",
        lambda destinatario, texto: saida.append(
            {"messaging_product": "whatsapp", "to": destinatario,
             "type": "text", "text": {"body": texto}}) or None)
    monkeypatch.setattr(bot, "enviar_notificacao_interna_marcacao", lambda *a, **k: None)
    return saida


def _post(cliente_http, msg):
    corpo = json.dumps({"entry": [{"changes": [{"value": {
        "messaging_product": "whatsapp",
        "metadata": {"phone_number_id": "x"},
        "messages": [msg],
    }}]}]}).encode()
    sig = "sha256=" + hmac.new(b"segredo-de-teste", corpo, hashlib.sha256).hexdigest()
    return cliente_http.post("/webhook", data=corpo, content_type="application/json",
                             headers={"X-Hub-Signature-256": sig})


def _texto(txt, mid, cliente=CLIENTE):
    return {"from": cliente, "id": mid, "type": "text", "text": {"body": txt}}


def _lista(rid, titulo, mid, cliente=CLIENTE):
    return {"from": cliente, "id": mid, "type": "interactive",
            "interactive": {"type": "list_reply", "list_reply": {"id": rid, "title": titulo}}}


def _botao(rid, mid, cliente=CLIENTE):
    return {"from": cliente, "id": mid, "type": "interactive",
            "interactive": {"type": "button_reply", "button_reply": {"id": rid, "title": rid}}}


def _linhas(payload):
    return payload["interactive"]["action"]["sections"][0]["rows"]


def _botoes(payload):
    return payload["interactive"]["action"]["buttons"]


def _definir_idioma(cliente_http, cliente=CLIENTE, nome="Ana"):
    """Um cliente só vê menus/serviços depois de ter um idioma guardado (ver
    o portão de idioma em receber_mensagem) — mesmo o alvo de um botão de
    follow-up, que só é enviado a um cliente já conhecido. O nome vem sempre
    do perfil de contacto do WhatsApp (entry["contacts"]), nunca de um texto
    livre pedido pelo bot — por isso aqui só há o botão de idioma; um texto a
    seguir cairia no "não entendi" (não é um comando conhecido)."""
    _post(cliente_http, _botao("lang_pt", "i1", cliente))


def _dia_util_futuro(dias_a_partir_de_hoje):
    """Um dia útil (nunca domingo, fechado por omissão) — sem hardcode.

    Quem chama `marcar()` (INSERT direto, sem passar pelo motor de
    disponibilidade) tem de usar um offset >= 22: a seed de demonstração do
    dashboard (bot.py: _demo_seed_periodo) povoa deterministicamente de -14 a
    +21 dias em torno de "hoje" com marcações reais — um offset pequeno
    colide com ocupação genuína do negócio, não é flakiness."""
    d = tempo.hoje_zurique() + timedelta(days=dias_a_partir_de_hoje)
    while d.weekday() == 6:
        d += timedelta(days=1)
    return d


# ---------------------------------------------------------------------------
# 1) Datas com rótulos relativos
# ---------------------------------------------------------------------------
def test_lista_datas_tem_hoje_e_amanha_com_id_data(base_dados):
    opcoes = bot.dias_para_marcacao({}, "pt")
    assert opcoes, "sem dias abertos — ajustar o business_hours de teste"
    hoje = tempo.hoje_zurique()
    primeiro_id = f"data_{hoje.isoformat()}"
    ids = [o["id"] for o in opcoes]
    titulos = {o["id"]: o["titulo"] for o in opcoes}
    if primeiro_id in ids:
        assert titulos[primeiro_id] == "Hoje"
    amanha_id = f"data_{(hoje + timedelta(days=1)).isoformat()}"
    if amanha_id in ids:
        assert titulos[amanha_id] == "Amanhã"
    # todos os ids seguem o formato data_<iso>
    assert all(o["id"].startswith("data_") for o in opcoes)


def test_selecionar_data_por_id_grava_texto_canonico_nao_relativo(cliente_http, base_dados, enviados):
    _post(cliente_http, _botao("lang_pt", "m1"))
    _post(cliente_http, _texto("Ana", "m2"))
    _post(cliente_http, _lista("svc_limpeza_pele", "Limpeza de pele", "m3"))

    # um dia bem lá à frente — "Hoje"/"Amanhã" podem ficar sem vagas perto do
    # fecho, dependendo da hora a que a suite corre (ver passo_hora); um dia
    # bem no futuro tem sempre a grelha toda livre.
    alvo_iso = _dia_util_futuro(5).isoformat()
    escolhido = next(o for o in bot.dias_para_marcacao({}, "pt") if o["id"] == f"data_{alvo_iso}")
    # a etiqueta mostrada pode ser "Hoje"/"Amanhã" (nunca uma data por
    # extenso) — é o ID, não o título, que tem de decidir a data gravada.
    _post(cliente_http, _lista(escolhido["id"], escolhido["titulo"], "m4"))

    sessao = bot.carregar_sessao(CLIENTE)
    data_iso_escolhida = escolhido["id"][len("data_"):]
    assert sessao["data"] == bot._data_display(data_iso_escolhida, "pt")
    assert sessao["data"] not in ("Hoje", "Amanhã")
    assert bot.data_iso_de_texto(sessao["data"]) == data_iso_escolhida


def test_selecionar_data_por_titulo_continua_a_funcionar_legado(cliente_http, base_dados, enviados):
    """A lista antiga (id "opt_N", título com a data por extenso) — usada
    por todos os testes de fluxo já existentes — continua a funcionar sem
    qualquer alteração."""
    _post(cliente_http, _botao("lang_pt", "m1"))
    _post(cliente_http, _texto("Ana", "m2"))
    _post(cliente_http, _lista("svc_limpeza_pele", "Limpeza de pele", "m3"))
    dia_txt = data_pt(_dia_util_futuro(5).isoformat())
    _post(cliente_http, _lista("opt_0", dia_txt, "m4"))
    assert bot.carregar_sessao(CLIENTE)["data"] == dia_txt


# ---------------------------------------------------------------------------
# 2) Cancelamento com confirmação
# ---------------------------------------------------------------------------
def test_cancelamento_exige_confirmacao_antes_de_cancelar(cliente_http, base_dados, enviados):
    dia = _dia_util_futuro(30).isoformat()
    idag = marcar(CLIENTE, "limpeza_pele", data_pt(dia), "09:00")
    _definir_idioma(cliente_http)

    # "mp_gerir" é uma linha do menu principal (lista, nunca botão) — chega
    # sempre como list_reply.
    _post(cliente_http, _lista("mp_gerir", "Gerir marcação", "g1"))
    gestao = enviados[-1]
    ids_gestao = ([b["reply"]["id"] for b in gestao["interactive"]["action"]["buttons"]]
                  if gestao["interactive"]["type"] == "button" else [r["id"] for r in _linhas(gestao)])
    id_cancelar = next(i for i in ids_gestao if i.startswith("cancelar_confirmar_"))
    assert id_cancelar == f"cancelar_confirmar_{idag}"

    _post(cliente_http, _botao(id_cancelar, "g2"))
    # AINDA não cancelou — só mostrou o ecrã de confirmação.
    ag = bot.obter_agendamento(idag)
    assert bot.chave_estado(ag["estado"]) == "confirmed"
    confirmar = enviados[-1]
    ids_confirmar = [b["reply"]["id"] for b in _botoes(confirmar)]
    assert f"cancelar_sim_{idag}" in ids_confirmar
    assert any(i.startswith("gerir_ag_") for i in ids_confirmar)   # "Voltar"

    _post(cliente_http, _botao(f"cancelar_sim_{idag}", "g3"))
    ag = bot.obter_agendamento(idag)
    assert bot.chave_estado(ag["estado"]) == "cancelled"

    # depois de cancelar: botões de seguimento, nunca um beco sem saída.
    seguimento = enviados[-1]
    ids_seguimento = [b["reply"]["id"] for b in _botoes(seguimento)]
    assert bot.ACAO_NOVA_MARCACAO in ids_seguimento
    assert bot.ACAO_MENU in ids_seguimento


def test_cancelamento_voltar_nao_cancela(cliente_http, base_dados, enviados):
    dia = _dia_util_futuro(30).isoformat()
    idag = marcar(CLIENTE, "limpeza_pele", data_pt(dia), "09:00")
    _definir_idioma(cliente_http)

    _post(cliente_http, _botao(f"cancelar_confirmar_{idag}", "v1"))
    confirmar = enviados[-1]
    id_voltar = next(b["reply"]["id"] for b in _botoes(confirmar) if b["reply"]["id"].startswith("gerir_ag_"))
    _post(cliente_http, _botao(id_voltar, "v2"))

    ag = bot.obter_agendamento(idag)
    assert bot.chave_estado(ag["estado"]) == "confirmed"


def test_cancelar_ag_legado_continua_a_cancelar_imediatamente(cliente_http, base_dados, enviados):
    """Uma mensagem ANTIGA na conversa do cliente pode ainda ter o botão com
    o id legado — tem de continuar a cancelar de imediato, exatamente como
    sempre fez (nunca se remove um id antigo, só se para de o criar de novo)."""
    dia = _dia_util_futuro(30).isoformat()
    idag = marcar(CLIENTE, "limpeza_pele", data_pt(dia), "09:00")
    _definir_idioma(cliente_http)
    _post(cliente_http, _botao(f"cancelar_ag_{idag}", "l1"))
    ag = bot.obter_agendamento(idag)
    assert bot.chave_estado(ag["estado"]) == "cancelled"


def test_marcacao_cancelada_nunca_e_ressuscitada_por_reagendamento(base_dados):
    dia = _dia_util_futuro(30).isoformat()
    idag = marcar(CLIENTE, "limpeza_pele", data_pt(dia), "09:00")
    bot.marcar_agendamento_cancelado(idag, exigir_confirmado=False)
    with pytest.raises(bot.EstadoInvalido):
        bot.reagendar_agendamento(idag, _dia_util_futuro(32).isoformat(), "10:00", origem="cliente")
    ag = bot.obter_agendamento(idag)
    assert bot.chave_estado(ag["estado"]) == "cancelled"


# ---------------------------------------------------------------------------
# 3) Follow-up / reativação — arquitetura (sem worker)
# ---------------------------------------------------------------------------
def _customer_id(telefone):
    for c in db.listar_customers():
        if c["phone"] == telefone:
            return c["id"]
    raise AssertionError(f"cliente {telefone} não encontrado")


def _configurar_follow_up_servico(servico_id, rebook_days, enabled=1, template_pt=None):
    with db.ligacao() as conn:
        conn.execute(
            "UPDATE servicos SET rebook_days = ?, follow_up_enabled = ?, follow_up_template_pt = ? "
            "WHERE id = ?", (rebook_days, enabled, template_pt, servico_id))


def _marcar_concluida_ha_dias(telefone, servico_id, dias_atras, nome="Cliente Teste"):
    dia_iso = (tempo.hoje_zurique() - timedelta(days=dias_atras)).isoformat()
    idag = marcar(telefone, servico_id, data_pt(dia_iso), "09:00", nome=nome)
    bot.atualizar_estado_agendamento(idag, estados.COMPLETED)
    return idag


def test_candidato_elegivel_apos_dias_suficientes(base_dados):
    _configurar_follow_up_servico("limpeza_pele", rebook_days=21)
    idag = _marcar_concluida_ha_dias(CLIENTE, "limpeza_pele", 21)
    candidatos = notif_followup.candidatos_follow_up()
    assert idag in [c["id"] for c in candidatos]


def test_candidato_nao_elegivel_sem_dias_suficientes(base_dados):
    _configurar_follow_up_servico("limpeza_pele", rebook_days=21)
    idag = _marcar_concluida_ha_dias(CLIENTE, "limpeza_pele", 5)
    assert idag not in [c["id"] for c in notif_followup.candidatos_follow_up()]


def test_candidato_nao_elegivel_sem_follow_up_enabled(base_dados):
    _configurar_follow_up_servico("limpeza_pele", rebook_days=21, enabled=0)
    idag = _marcar_concluida_ha_dias(CLIENTE, "limpeza_pele", 30)
    assert idag not in [c["id"] for c in notif_followup.candidatos_follow_up()]


def test_candidato_nao_elegivel_sem_rebook_days_configurado(base_dados):
    # follow_up_enabled=1 mas SEM rebook_days -> nunca dispara (nada assumido)
    _configurar_follow_up_servico("limpeza_pele", rebook_days=None, enabled=1)
    idag = _marcar_concluida_ha_dias(CLIENTE, "limpeza_pele", 90)
    assert idag not in [c["id"] for c in notif_followup.candidatos_follow_up()]


def test_candidato_nao_elegivel_com_marcacao_futura_ativa(base_dados):
    _configurar_follow_up_servico("limpeza_pele", rebook_days=21)
    idag = _marcar_concluida_ha_dias(CLIENTE, "limpeza_pele", 30)
    marcar(CLIENTE, "design_sobrancelhas", data_pt(_dia_util_futuro(30).isoformat()), "10:00")
    assert idag not in [c["id"] for c in notif_followup.candidatos_follow_up()]


def test_candidato_nao_elegivel_cliente_bloqueado_ou_opt_out(base_dados):
    _configurar_follow_up_servico("limpeza_pele", rebook_days=21)
    idag = _marcar_concluida_ha_dias(CLIENTE, "limpeza_pele", 30)
    cid = _customer_id(CLIENTE)
    with db.ligacao() as conn:
        conn.execute("UPDATE customers SET follow_up_opt_out = 1 WHERE id = ?", (cid,))
    assert idag not in [c["id"] for c in notif_followup.candidatos_follow_up()]


def test_candidato_nao_elegivel_numero_demo(base_dados):
    telefone_demo = f"{bot.DEMO_TELEFONE_PREFIXO}9001"
    _configurar_follow_up_servico("limpeza_pele", rebook_days=21)
    idag = _marcar_concluida_ha_dias(telefone_demo, "limpeza_pele", 30)
    assert idag not in [c["id"] for c in notif_followup.candidatos_follow_up()]


def test_marcar_follow_up_enviado_e_idempotente(base_dados):
    _configurar_follow_up_servico("limpeza_pele", rebook_days=21)
    idag = _marcar_concluida_ha_dias(CLIENTE, "limpeza_pele", 30)
    assert idag in [c["id"] for c in notif_followup.candidatos_follow_up()]
    notif_followup.marcar_follow_up_enviado(idag)
    # nunca mais reaparece — no máximo um follow-up automático por marcação
    assert idag not in [c["id"] for c in notif_followup.candidatos_follow_up()]


def test_render_follow_up_generico_nunca_insistente(base_dados):
    _configurar_follow_up_servico("limpeza_pele", rebook_days=21)
    _marcar_concluida_ha_dias(CLIENTE, "limpeza_pele", 30, nome="Marta Silva")
    candidato = notif_followup.candidatos_follow_up()[0]
    texto = notif_followup.render_follow_up(candidato, "pt")
    assert "Marta" in texto
    assert "Limpeza de pele" in texto


def test_render_follow_up_usa_template_do_servico(base_dados):
    _configurar_follow_up_servico("limpeza_pele", rebook_days=21,
                                   template_pt="Olá {nome}! Que tal outra {servico}?")
    _marcar_concluida_ha_dias(CLIENTE, "limpeza_pele", 30, nome="Marta Silva")
    candidato = notif_followup.candidatos_follow_up()[0]
    texto = notif_followup.render_follow_up(candidato, "pt")
    assert texto == "Olá Marta! Que tal outra Limpeza de pele?"


def test_webhook_followup_marcar_novamente_reusa_fluxo_normal(cliente_http, base_dados, enviados):
    """"Marcar novamente" não é um segundo fluxo: entra diretamente no MESMO
    passo de escolha de data, com o serviço já pré-selecionado."""
    _definir_idioma(cliente_http)
    _post(cliente_http, _botao("followup_marcar_limpeza_pele", "f1"))
    sessao = bot.carregar_sessao(CLIENTE)
    assert sessao["servico_id"] == "limpeza_pele"
    assert sessao["fluxo"] == "beauty"
    ultima = enviados[-1]
    assert _linhas(ultima)[0]["id"].startswith("data_")


def test_webhook_followup_depois_marca_recusado_e_responde(cliente_http, base_dados, enviados):
    _configurar_follow_up_servico("limpeza_pele", rebook_days=21)
    idag = _marcar_concluida_ha_dias(CLIENTE, "limpeza_pele", 30)
    _definir_idioma(cliente_http)
    _post(cliente_http, _botao(f"followup_depois_{idag}", "f2"))
    ag = bot.obter_agendamento(idag)
    assert ag["follow_up_status"] == "declined"
    assert idag not in [c["id"] for c in notif_followup.candidatos_follow_up()]
    resposta = enviados[-1]
    assert resposta["type"] == "text"


def test_followup_demo_nunca_chama_o_provider(cliente_http, base_dados, monkeypatch):
    chamadas = []

    class _RespostaFalsa:
        status_code = 200
        text = "{}"

    monkeypatch.setattr(bot._wa.requests, "post",
                         lambda *a, **k: chamadas.append(1) or _RespostaFalsa())
    monkeypatch.setattr(bot._wa.config, "WHATSAPP_TOKEN", "token-de-teste")
    monkeypatch.setattr(bot._wa.config, "PHONE_NUMBER_ID", "123456")

    telefone_demo = f"{bot.DEMO_TELEFONE_PREFIXO}9002"
    idag = marcar(telefone_demo, "limpeza_pele", data_pt(_dia_util_futuro(30).isoformat()), "09:00")
    _definir_idioma(cliente_http, cliente=telefone_demo)
    _post(cliente_http, _botao(f"followup_depois_{idag}", "d1", cliente=telefone_demo))
    _post(cliente_http, _botao("followup_marcar_limpeza_pele", "d2", cliente=telefone_demo))
    assert chamadas == []
