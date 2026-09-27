"""Registo de conversas (migração 25, messaging/conversas.py) e tudo o que
dele depende: o texto guardado nas duas direções, um clique num botão
legível por uma pessoa, a retenção dos 12 meses, o pedido de HUMANO a chegar
ao painel COM a pergunta, a janela de 24h a expor o prazo — e nenhum caminho
de equipa a devolver um link `wa.me` (o número pessoal da Daniela é privado).

Ponto de verificação do "gravar nunca parte nada": a resposta à cliente sai
mesmo quando o INSERT rebenta."""

import base64
import hashlib
import hmac
import json
from datetime import timedelta

import pytest

import bot
import config
import db
import tempo
from messaging import conversas
from conftest import marcar

AUTH = {"Authorization": "Basic " + base64.b64encode(b"painel:painel-pw").decode()}
CLIENTE = "41791234567"
DEMO = bot.DEMO_TELEFONE_PREFIXO + "01"


def _post(cliente_http, msg):
    corpo = json.dumps({"entry": [{"changes": [{"value": {
        "messaging_product": "whatsapp",
        "metadata": {"phone_number_id": "x"},
        "messages": [msg],
    }}]}]}).encode()
    sig = "sha256=" + hmac.new(b"segredo-de-teste", corpo, hashlib.sha256).hexdigest()
    return cliente_http.post("/webhook", data=corpo, content_type="application/json",
                             headers={"X-Hub-Signature-256": sig})


def _texto(txt, mid, de=CLIENTE):
    return {"from": de, "id": mid, "type": "text", "text": {"body": txt}}


def _botao(rid, titulo, mid, de=CLIENTE):
    return {"from": de, "id": mid, "type": "interactive",
            "interactive": {"type": "button_reply", "button_reply": {"id": rid, "title": titulo}}}


def _lista(rid, titulo, mid, de=CLIENTE, descricao=None):
    resposta = {"id": rid, "title": titulo}
    if descricao:
        resposta["description"] = descricao
    return {"from": de, "id": mid, "type": "interactive",
            "interactive": {"type": "list_reply", "list_reply": resposta}}


# ---------------------------------------------------------------------------
# Parte A — o texto fica guardado
# ---------------------------------------------------------------------------
def test_mensagem_recebida_fica_com_o_texto(cliente_http, base_dados):
    _post(cliente_http, _texto("Olá, tenho uma dúvida", "w1"))
    recebidas = [m for m in conversas.listar_mensagens(CLIENTE) if m["direcao"] == "recebida"]
    assert [m["texto"] for m in recebidas] == ["Olá, tenho uma dúvida"]
    assert recebidas[0]["tipo"] == "texto"
    assert recebidas[0]["wamid"] == "w1"


def test_mensagem_enviada_fica_com_o_texto(cliente_http, base_dados):
    _post(cliente_http, _texto("olá", "w1"))
    enviadas = [m for m in conversas.listar_mensagens(CLIENTE) if m["direcao"] == "enviada"]
    # A primeira interação devolve o seletor de idioma (mensagem interativa):
    # guarda-se o corpo E as opções apresentadas, senão a resposta da cliente
    # aparecia no painel sem se ver entre o que ela estava a escolher.
    assert enviadas, "nada gravado na direção 'enviada'"
    assert "Português" in enviadas[0]["texto"]


def test_clique_num_botao_guarda_o_que_a_cliente_VIU(cliente_http, base_dados):
    """`opt_1` não serve para nada a quem vai ler isto — guarda-se o TÍTULO,
    e o id fica à parte para quem estiver a depurar o fluxo."""
    _post(cliente_http, _botao("lang_pt", "Português", "w1"))
    _post(cliente_http, _lista("svc_limpeza_pele", "Limpeza de pele", "w2"))
    recebidas = [m for m in conversas.listar_mensagens(CLIENTE) if m["direcao"] == "recebida"]
    assert [m["texto"] for m in recebidas] == ["Português", "Limpeza de pele"]
    assert [m["id_interativo"] for m in recebidas] == ["lang_pt", "svc_limpeza_pele"]
    assert recebidas[0]["tipo"] == "botao"
    assert recebidas[1]["tipo"] == "lista"


def test_linha_de_lista_junta_a_descricao(base_dados):
    """Dois horários do mesmo serviço têm o mesmo título — é a descrição que
    os distingue."""
    lida = conversas.ler_entrada(_lista("opt_0", "09:00", "w1", descricao="Limpeza de pele"))
    assert lida["texto"] == "09:00 — Limpeza de pele"


def test_quick_reply_de_template_guarda_o_texto_do_botao(base_dados):
    """Formato "button" (template aprovado) — diferente do "interactive" das
    nossas mensagens: o visível está em `button.text`, o id em `payload`."""
    lida = conversas.ler_entrada({"from": CLIENTE, "id": "w1", "type": "button",
                                  "button": {"text": "Confirmar", "payload": "lembrete_confirmar_7"}})
    assert lida == {"tipo": "botao", "texto": "Confirmar", "id_interativo": "lembrete_confirmar_7"}


def test_mensagem_sem_texto_fica_descrita_e_nao_vazia(base_dados):
    lida = conversas.ler_entrada({"from": CLIENTE, "id": "w1", "type": "image",
                                  "image": {"caption": "é isto"}})
    assert lida["tipo"] == "imagem"
    assert lida["texto"] == "[imagem] é isto"


def test_numeros_demo_continuam_a_ser_gravados(base_dados, monkeypatch):
    """O envio real é bloqueado antes da Meta — o REGISTO acontece, e é isso
    que torna o fluxo testável sem mandar nada a sério."""
    chamadas = []
    monkeypatch.setattr(bot._wa.requests, "post", lambda *a, **k: chamadas.append(1))
    monkeypatch.setattr(bot._wa.config, "WHATSAPP_TOKEN", "token-de-teste")
    monkeypatch.setattr(bot._wa.config, "PHONE_NUMBER_ID", "123456")

    bot.enviar_texto(DEMO, "Mensagem demo")

    assert chamadas == [], "um número DEMO nunca pode chegar à Meta"
    mensagens = conversas.listar_mensagens(DEMO)
    assert [m["texto"] for m in mensagens] == ["Mensagem demo"]
    assert mensagens[0]["direcao"] == "enviada"


def test_avisos_internos_nao_criam_uma_conversa(base_dados, monkeypatch):
    """Uma mensagem para o número da própria Daniela é um aviso interno, não
    um fio com uma cliente — encheria a lista com uma conversa de ninguém."""
    monkeypatch.setattr(config, "PROVIDER_WHATSAPP", "41790000000")
    bot.enviar_texto("41790000000", "🚨 aviso interno")
    assert conversas.listar_conversas() == []


def test_falha_a_gravar_nao_impede_a_resposta(base_dados, monkeypatch, caplog):
    """Uma cliente NUNCA fica sem resposta porque um INSERT correu mal."""
    def _rebenta(*a, **k):
        raise RuntimeError("disco cheio")
    monkeypatch.setattr(conversas.db, "ligacao", _rebenta)

    enviados = []
    monkeypatch.setattr(bot._wa, "requests", bot._wa.requests)
    monkeypatch.setattr(bot._wa.config, "WHATSAPP_TOKEN", "token-de-teste")
    monkeypatch.setattr(bot._wa.config, "PHONE_NUMBER_ID", "123456")
    monkeypatch.setattr(bot._wa.requests, "post",
                        lambda url, headers=None, json=None, timeout=None:
                            enviados.append(json) or _RespostaOk())

    bot.enviar_texto(CLIENTE, "A tua marcação está confirmada")

    assert len(enviados) == 1, "o envio tem de seguir mesmo com a gravação em baixo"


class _RespostaOk:
    status_code = 200
    text = "{}"

    def json(self):
        return {"messages": [{"id": "wamid.enviado"}]}


def test_wamid_do_envio_fica_guardado(base_dados, monkeypatch):
    monkeypatch.setattr(bot._wa.config, "WHATSAPP_TOKEN", "token-de-teste")
    monkeypatch.setattr(bot._wa.config, "PHONE_NUMBER_ID", "123456")
    monkeypatch.setattr(bot._wa.requests, "post",
                        lambda url, headers=None, json=None, timeout=None: _RespostaOk())
    bot.enviar_texto(CLIENTE, "olá")
    assert conversas.listar_mensagens(CLIENTE)[0]["wamid"] == "wamid.enviado"


def test_retry_da_meta_nao_grava_duas_vezes(cliente_http, base_dados):
    """A gravação fica DEPOIS de reclamar o wamid: a idempotência do webhook
    protege também o registo."""
    _post(cliente_http, _texto("uma só vez", "w-repetido"))
    _post(cliente_http, _texto("uma só vez", "w-repetido"))
    recebidas = [m for m in conversas.listar_mensagens(CLIENTE) if m["direcao"] == "recebida"]
    assert len(recebidas) == 1


# ---------------------------------------------------------------------------
# Retenção
# ---------------------------------------------------------------------------
def _gravar_em(telefone, texto, quando):
    with db.ligacao() as c:
        c.execute(
            "INSERT INTO mensagens_conversa (tenant_id, telefone, direcao, tipo, texto, criado_em) "
            "VALUES (1, ?, 'recebida', 'texto', ?, ?)", (telefone, texto, tempo.iso_utc(quando)))


def test_limpeza_apaga_o_que_passou_dos_12_meses(base_dados):
    agora = tempo.agora_utc()
    _gravar_em(CLIENTE, "há 13 meses", agora - timedelta(days=30 * 13))
    _gravar_em(CLIENTE, "há 11 meses", agora - timedelta(days=30 * 11))
    _gravar_em(CLIENTE, "ontem", agora - timedelta(days=1))

    apagadas = conversas.limpar_antigas(agora=agora)

    assert apagadas == 1
    assert [m["texto"] for m in conversas.listar_mensagens(CLIENTE)] == ["há 11 meses", "ontem"]


def test_prazo_de_retencao_vem_do_ambiente(base_dados, monkeypatch):
    monkeypatch.setattr(config, "CONVERSAS_RETENCAO_MESES", 1)
    agora = tempo.agora_utc()
    _gravar_em(CLIENTE, "há 2 meses", agora - timedelta(days=70))
    _gravar_em(CLIENTE, "hoje", agora)

    assert conversas.limpar_antigas(agora=agora) == 1
    assert [m["texto"] for m in conversas.listar_mensagens(CLIENTE)] == ["hoje"]


def test_retencao_a_zero_desliga_a_limpeza(base_dados, monkeypatch):
    monkeypatch.setattr(config, "CONVERSAS_RETENCAO_MESES", 0)
    _gravar_em(CLIENTE, "muito antiga", tempo.agora_utc() - timedelta(days=3000))
    assert conversas.limpar_antigas() == 0
    assert len(conversas.listar_mensagens(CLIENTE)) == 1


def test_limpeza_corre_no_executor_de_automacoes_uma_vez_por_dia(base_dados):
    from notifications import jobs
    _gravar_em(CLIENTE, "antiga", tempo.agora_utc() - timedelta(days=500))

    jobs.process_due_jobs(1)
    assert conversas.listar_mensagens(CLIENTE) == []

    # Segunda passagem no mesmo dia não volta a varrer a tabela.
    _gravar_em(CLIENTE, "outra antiga", tempo.agora_utc() - timedelta(days=500))
    jobs.process_due_jobs(1)
    assert len(conversas.listar_mensagens(CLIENTE)) == 1


def test_limpeza_falhada_nao_derruba_o_executor(base_dados, monkeypatch):
    from notifications import jobs
    monkeypatch.setattr(conversas, "limpar_antigas",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("bd em baixo")))
    resumo = jobs.process_due_jobs(1)
    assert resumo["processados"] == 0        # correu até ao fim, sem levantar


# ---------------------------------------------------------------------------
# Parte B — janela de 24h com PRAZO
# ---------------------------------------------------------------------------
def test_janela_expoe_quando_expira(cliente_http, base_dados):
    _post(cliente_http, _texto("olá", "w1"))
    janela = conversas.estado_janela_24h(CLIENTE)
    assert janela["aberta"] is True
    assert janela["expira_em"]
    assert 23 * 60 <= janela["minutos_restantes"] <= 24 * 60
    # E o prazo é mesmo 24h depois da última mensagem da cliente.
    assert tempo.parse_iso(janela["expira_em"]) - tempo.parse_iso(janela["ultima_em"]) \
        == timedelta(hours=24)


def test_janela_fechada_para_quem_nunca_escreveu(base_dados):
    assert conversas.estado_janela_24h("41790000001") == {
        "aberta": False, "ultima_em": None, "expira_em": None, "minutos_restantes": None}


def test_dentro_da_janela_24h_continua_a_dar_sim_ou_nao(cliente_http, base_dados):
    assert bot.dentro_da_janela_24h(CLIENTE) is False
    _post(cliente_http, _texto("olá", "w1"))
    assert bot.dentro_da_janela_24h(CLIENTE) is True


def test_api_conversa_traz_a_janela_antes_de_ela_escrever(cliente_http, base_dados):
    _post(cliente_http, _texto("olá", "w1"))
    r = cliente_http.get(f"/api/conversas/{CLIENTE}", headers=AUTH)
    assert r.status_code == 200
    assert r.get_json()["janela"]["expira_em"]


def test_409_fora_da_janela_traz_a_janela(cliente_http, base_dados):
    _gravar_em(CLIENTE, "há dois dias", tempo.agora_utc() - timedelta(days=2))
    with db.ligacao() as c:
        c.execute("INSERT INTO interacoes_cliente (tenant_id, telefone, ultima_mensagem_em) "
                  "VALUES (1, ?, ?)", (CLIENTE, tempo.iso_utc(tempo.agora_utc() - timedelta(days=2))))
    r = cliente_http.post(f"/api/conversas/{CLIENTE}/mensagem",
                          json={"texto": "Olá"}, headers=AUTH)
    assert r.status_code == 409
    assert r.get_json()["janela"]["aberta"] is False


# ---------------------------------------------------------------------------
# Parte B — API das conversas
# ---------------------------------------------------------------------------
def test_api_conversas_exige_autenticacao(cliente_http, base_dados):
    assert cliente_http.get("/api/conversas").status_code == 401
    assert cliente_http.get(f"/api/conversas/{CLIENTE}").status_code == 401


def test_lista_de_conversas_mais_recente_primeiro(cliente_http, base_dados):
    _gravar_em("41790000011", "a mais antiga", tempo.agora_utc() - timedelta(hours=5))
    _gravar_em("41790000012", "a mais recente", tempo.agora_utc())
    lista = cliente_http.get("/api/conversas", headers=AUTH).get_json()["conversas"]
    assert [c["telefone"] for c in lista] == ["41790000012", "41790000011"]
    assert lista[0]["ultimo_texto"] == "a mais recente"


def test_conversa_inexistente_da_404(cliente_http, base_dados):
    assert cliente_http.get("/api/conversas/41799999999", headers=AUTH).status_code == 404
    # Um telefone que nem é um telefone nunca chega a tocar na base de dados.
    assert cliente_http.get("/api/conversas/nao-e-um-numero", headers=AUTH).status_code == 404


def test_nao_se_pode_responder_a_quem_nunca_escreveu(cliente_http, base_dados, monkeypatch):
    """O telefone vem do URL — só é aceite se já existir um fio com ele.
    Nunca se manda uma mensagem para um número arbitrário."""
    chamadas = []
    monkeypatch.setattr(bot._wa.config, "WHATSAPP_TOKEN", "token-de-teste")
    monkeypatch.setattr(bot._wa.config, "PHONE_NUMBER_ID", "123456")
    monkeypatch.setattr(bot._wa.requests, "post",
                        lambda *a, **k: chamadas.append(1) or _RespostaOk())
    r = cliente_http.post("/api/conversas/41760000000/mensagem", json={"texto": "Olá"}, headers=AUTH)
    assert r.status_code == 404
    assert chamadas == []


def test_resposta_pelo_painel_fica_no_fio(cliente_http, base_dados):
    _post(cliente_http, _texto("tenho uma dúvida", "w1"))
    r = cliente_http.post(f"/api/conversas/{CLIENTE}/mensagem",
                          json={"texto": "Diz-me, estou aqui"}, headers=AUTH)
    assert r.status_code == 200
    fio = conversas.listar_mensagens(CLIENTE)
    assert fio[-1]["direcao"] == "enviada"
    assert fio[-1]["texto"] == "Diz-me, estou aqui"


def test_composer_do_cliente_continua_a_funcionar(cliente_http, base_dados, monkeypatch):
    """A vista Conversas e o Client Manager partilham o MESMO envio
    (_enviar_mensagem_livre) — duas implementações seriam duas maneiras
    diferentes de falhar."""
    marcar("41780002222", "limpeza_pele", *_amanha(), nome="Cliente Real")
    cid = next(c["id"] for c in db.listar_customers() if c["phone"] == "41780002222")
    with db.ligacao() as c:
        c.execute("INSERT INTO interacoes_cliente (tenant_id, telefone, ultima_mensagem_em) "
                  "VALUES (1, ?, ?)", ("41780002222", tempo.iso_utc()))
    r = cliente_http.post(f"/api/clientes/{cid}/mensagem", json={"texto": "Até amanhã!"}, headers=AUTH)
    assert r.status_code == 200
    assert conversas.listar_mensagens("41780002222")[-1]["texto"] == "Até amanhã!"


def _amanha():
    from conftest import data_pt, dias_abertos
    return data_pt(dias_abertos(1)[0]), "10:00"


# ---------------------------------------------------------------------------
# Parte C — o pedido de HUMANO chega ao painel, e nenhum caminho dá wa.me
# ---------------------------------------------------------------------------
@pytest.fixture()
def equipa(monkeypatch):
    """O número interno da equipa configurado, e as mensagens que lhe saem."""
    monkeypatch.setattr(bot, "PROVIDER_WHATSAPP", "41790000000")
    monkeypatch.setattr(config, "PROVIDER_WHATSAPP", "41790000000")
    saida = []
    original = bot._wa.enviar

    def _espiar(payload):
        saida.append(payload)
        return original(payload)
    monkeypatch.setattr(bot._wa, "enviar", _espiar)
    monkeypatch.setattr(bot, "enviar", _espiar)
    return saida


def _para_a_equipa(saida):
    return [p for p in saida if p.get("to") == "41790000000"]


def test_pedido_de_humano_aparece_no_painel_com_a_pergunta(cliente_http, base_dados, equipa):
    _post(cliente_http, _botao("lang_pt", "Português", "w1"))
    _post(cliente_http, _texto("A minha pele está a reagir mal, o que faço?", "w2"))
    _post(cliente_http, _texto("humano", "w3"))

    from operations import engine as ops
    itens = [i for i in ops.attention_items(1) if i["tipo"] == "needs_human"]
    assert len(itens) == 1
    assert itens[0]["telefone"] == CLIENTE
    assert itens[0]["acao"] == "abrir_conversa"
    # O QUE ela escreveu, não só o número — e não o comando "humano", que não
    # diz nada a ninguém: a pergunta é a mensagem anterior.
    assert itens[0]["detalhe"] == "A minha pele está a reagir mal, o que faço?"
    assert "Ana" in itens[0]["titulo"] or "Cliente" in itens[0]["titulo"]
    assert itens[0]["pedido_em"]


def test_pedido_de_humano_sobrevive_ao_reinicio_da_sessao(cliente_http, base_dados, equipa):
    """Os quatro caminhos que pedem humano fazem reiniciar_sessao logo a
    seguir — sem a preservação, a marca era apagada no mesmo request."""
    _post(cliente_http, _botao("lang_pt", "Português", "w1"))
    _post(cliente_http, _texto("humano", "w2"))
    assert CLIENTE in conversas.pedidos_humanos_abertos(1)


def test_responder_pelo_painel_fecha_o_pedido(cliente_http, base_dados, equipa):
    _post(cliente_http, _botao("lang_pt", "Português", "w1"))
    _post(cliente_http, _texto("preciso de ajuda", "w2"))
    _post(cliente_http, _texto("humano", "w3"))
    assert CLIENTE in conversas.pedidos_humanos_abertos(1)

    r = cliente_http.post(f"/api/conversas/{CLIENTE}/mensagem",
                          json={"texto": "Olá, diz-me o que se passa"}, headers=AUTH)
    assert r.status_code == 200
    assert conversas.pedidos_humanos_abertos(1) == {}


def test_aviso_de_humano_leva_a_pergunta_e_nao_um_wa_me(cliente_http, base_dados, equipa):
    _post(cliente_http, _botao("lang_pt", "Português", "w1"))
    _post(cliente_http, _texto("Posso mudar a minha marcação?", "w2"))
    _post(cliente_http, _texto("humano", "w3"))

    avisos = [p["text"]["body"] for p in _para_a_equipa(equipa) if p.get("type") == "text"]
    assert avisos, "a Daniela tem de ser avisada onde está — o aviso mantém-se"
    aviso = avisos[-1]
    assert "wa.me" not in aviso
    assert "Posso mudar a minha marcação?" in aviso
    assert "painel" in aviso.lower()


def test_contactar_e_reagendar_nao_devolvem_wa_me(cliente_http, base_dados, equipa):
    ag = marcar(CLIENTE, "limpeza_pele", *_amanha(), nome="Ana")
    for prefixo in ("equipa_ag_contactar_", "equipa_ag_reagendar_"):
        equipa.clear()
        _post(cliente_http, _botao(f"{prefixo}{ag}", "x", f"w-{prefixo}", de="41790000000"))
        respostas = [p["text"]["body"] for p in _para_a_equipa(equipa) if p.get("type") == "text"]
        assert respostas, f"{prefixo} não respondeu nada"
        assert all("wa.me" not in r for r in respostas), f"{prefixo} ainda devolve wa.me"
        assert any("painel" in r.lower() for r in respostas)


def test_wa_me_link_ja_nao_existe():
    """Removido de propósito: o bot corre num número comprado só para isto e
    o WhatsApp pessoal da Daniela tem de continuar privado. Manter o helper
    era um convite a voltar a usá-lo."""
    assert not hasattr(bot, "wa_me_link")


def test_link_da_conversa_aponta_para_o_painel(base_dados, monkeypatch):
    monkeypatch.setattr(config, "PUBLIC_BASE_URL", "https://daniela.example/")
    assert bot.link_conversa(CLIENTE) == f"https://daniela.example/app#/conversas/{CLIENTE}"


def test_sem_endereco_publico_nao_se_inventa_um_link(base_dados, monkeypatch):
    monkeypatch.setattr(config, "PUBLIC_BASE_URL", None)
    assert bot.link_conversa(CLIENTE) is None
    assert "painel" in bot._instrucao_responder(CLIENTE).lower()


# ---------------------------------------------------------------------------
# Parte B — seed DEMO da vista (três estados)
# ---------------------------------------------------------------------------
def test_api_conversa_diz_se_o_numero_e_demo(cliente_http, base_dados, monkeypatch):
    """O painel precisa de o saber para não desativar a caixa de resposta em
    QA: um número demo pode receber texto livre com a janela fechada, porque
    o envio nunca chega à Meta. A regra do prefixo fica num sítio só."""
    monkeypatch.setattr(bot._wa.requests, "post", lambda *a, **k: _RespostaOk())
    monkeypatch.setattr(bot._wa.config, "WHATSAPP_TOKEN", "token-de-teste")
    monkeypatch.setattr(bot._wa.config, "PHONE_NUMBER_ID", "123456")
    bot.enviar_texto(DEMO, "Mensagem demo")
    assert cliente_http.get(f"/api/conversas/{DEMO}", headers=AUTH).get_json()["demo"] is True

    _post(cliente_http, _texto("olá", "w1"))
    assert cliente_http.get(f"/api/conversas/{CLIENTE}", headers=AUTH).get_json()["demo"] is False


def test_seed_demo_da_os_tres_estados_da_vista(base_dados):
    telefones = [bot.DEMO_TELEFONE_PREFIXO + f"{i:04d}" for i in range(3)]
    contagem = conversas.seed_demo(1, telefones)
    assert set(contagem) == set(telefones)

    # 1) histórico longo, janela aberta, pedido de ajuda à espera
    longo = conversas.listar_mensagens(telefones[0])
    assert len(longo) > 10
    assert {m["direcao"] for m in longo} == {conversas.RECEBIDA, conversas.ENVIADA}
    # Um toque num botão ficou legível por uma pessoa, não como `slot_1130`.
    botoes = [m for m in longo if m["id_interativo"] == "slot_1130"]
    assert botoes and botoes[0]["texto"] == "11:30"
    assert conversas.estado_janela_24h(telefones[0])["aberta"] is True
    assert telefones[0] in conversas.pedidos_humanos_abertos(1)

    # 2) janela de 24h fechada (a última mensagem da cliente foi há 40 horas)
    assert conversas.listar_mensagens(telefones[1])
    assert conversas.estado_janela_24h(telefones[1])["aberta"] is False

    # 3) fio recém-começado — uma mensagem só
    assert len(conversas.listar_mensagens(telefones[2])) == 1

    # E o pedido aberto sobe ao topo da lista.
    lista = conversas.listar_conversas(1)
    assert lista[0]["telefone"] == telefones[0]


def test_seed_demo_recusa_numeros_reais(base_dados):
    """O seed nunca pode escrever um fio inventado na conversa de alguém."""
    assert conversas.seed_demo(1, [CLIENTE, "41791111111", "41792222222"]) == {}
    assert conversas.listar_conversas(1) == []


def test_seed_demo_nao_duplica_fios(base_dados):
    telefones = [bot.DEMO_TELEFONE_PREFIXO + f"{i:04d}" for i in range(3)]
    conversas.seed_demo(1, telefones)
    antes = len(conversas.listar_mensagens(telefones[0]))
    conversas.seed_demo(1, telefones)
    assert len(conversas.listar_mensagens(telefones[0])) == antes


def test_pedido_de_humano_usa_o_nome_da_ficha_quando_a_sessao_nao_o_tem(cliente_http, base_dados):
    """O cartão do painel tem de dizer QUEM pediu. A sessão pode nem ter
    nome (sessão nova de um número que já é cliente) — nesse caso vale o
    nome da ficha."""
    marcar(CLIENTE, "limpeza_pele", *_amanha(), nome="Sofia Costa")
    conversas.marcar_pedido_humano(CLIENTE, "Está a arder, é normal?")
    assert conversas.pedidos_humanos_abertos(1)[CLIENTE]["nome"] == "Sofia Costa"
