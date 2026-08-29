"""
Bot "rececionista digital" via WhatsApp Cloud API para a Spotless Car Detail
(oficina fictícia de testes). Menu principal com 4 opções (Marcar / Orçamento /
Gerir marcação / Falar com a equipa), fluxos diferentes por tipo de serviço
(Limpeza / Estética / Wrap), comandos permanentes (MENU, VOLTAR, CANCELAR,
AJUDA, HUMANO), recuperação de sessão abandonada, indicador de progresso,
resumo com preço e duração estimada, e confirmação final mais completa.

Configuração necessária (variáveis de ambiente):
  WHATSAPP_TOKEN       - access token (temporário ou permanente) da Meta
  PHONE_NUMBER_ID      - ID do número de teste/produção (em API Setup)
  VERIFY_TOKEN         - qualquer string à tua escolha, usada na verificação do webhook
  PROVIDER_WHATSAPP    - número do prestador de serviço em formato internacional, ex: 41795886305

Como correr:
  pip install flask requests
  export WHATSAPP_TOKEN=... PHONE_NUMBER_ID=... VERIFY_TOKEN=... PROVIDER_WHATSAPP=...
  python bot.py
"""

import os
import json
import sqlite3
import requests
from datetime import date, timedelta, datetime
from flask import Flask, request, jsonify

app = Flask(__name__)

TOKEN = os.environ.get("WHATSAPP_TOKEN", "")
PHONE_NUMBER_ID = os.environ.get("PHONE_NUMBER_ID", "1052227394639217")
VERIFY_TOKEN = os.environ.get("VERIFY_TOKEN", "teste123")
PROVIDER_WHATSAPP = os.environ.get("PROVIDER_WHATSAPP", "41795886305")

GRAPH_URL = f"https://graph.facebook.com/v21.0/{PHONE_NUMBER_ID}/messages"

NOME_OFICINA = "Spotless Car Detail (TESTE)"
MORADA_OFICINA = "Spotless Car Detail, Zermatt"

DIAS_SEMANA_PT = ["seg", "ter", "qua", "qui", "sex", "sáb", "dom"]

# IDs usados em botões/listas em todo o fluxo
ID_VOLTAR = "voltar"
ID_CANCELAR = "cancelar_processo"

COMANDOS_TEXTO = {"menu", "voltar", "cancelar", "ajuda", "humano"}

# ---------------------------------------------------------------------------
# Catálogo de serviços, preços e durações (valores fictícios, para testar)
# ---------------------------------------------------------------------------
LIMPEZA_TIPOS = [
    {"id": "lp_int", "titulo": "Interior", "descricao": "Aspiração e higienização completa do habitáculo", "preco": 80, "duracao": "1h30"},
    {"id": "lp_ext", "titulo": "Exterior", "descricao": "Lavagem exterior à mão + secagem", "preco": 60, "duracao": "1h"},
    {"id": "lp_full", "titulo": "Interior + Exterior", "descricao": "Pacote completo por dentro e por fora", "preco": 130, "duracao": "2h"},
]

TAMANHOS_VEICULO = [
    {"id": "tam_p", "titulo": "Pequeno", "descricao": "Ex: Smart, Polo, Corsa", "fator": 1.0},
    {"id": "tam_m", "titulo": "Médio", "descricao": "Ex: Golf, Sedan, Berlina", "fator": 1.15},
    {"id": "tam_g", "titulo": "Grande", "descricao": "Ex: SUV, Van, Pick-up", "fator": 1.35},
]

EXTRAS_LIMPEZA = [
    {"id": "ex_nenhum", "titulo": "Nenhum extra", "descricao": "Seguir sem extras", "preco": 0},
    {"id": "ex_pelos", "titulo": "Remoção de pelos de animal", "descricao": "Tratamento específico", "preco": 25},
    {"id": "ex_odores", "titulo": "Tratamento de odores", "descricao": "Ozono / neutralização de cheiros", "preco": 20},
    {"id": "ex_bancos", "titulo": "Proteção de bancos", "descricao": "Impermeabilização têxtil/pele", "preco": 15},
]

ESTETICA_SERVICOS = [
    {"id": "es_polimento", "titulo": "Polimento", "descricao": "Remove riscos e devolve o brilho", "preco": 150, "duracao": "3h"},
    {"id": "es_ceramica", "titulo": "Proteção cerâmica", "descricao": "Proteção de longa duração", "preco": 350, "duracao": "1 dia"},
    {"id": "es_farois", "titulo": "Polimento de faróis", "descricao": "Recupera a transparência dos faróis", "preco": 60, "duracao": "45min"},
]

ESTADO_VEICULO = [
    {"id": "est_bom", "titulo": "✅ Bom estado", "fator": 1.0},
    {"id": "est_medio", "titulo": "🟡 Estado médio", "fator": 1.0},
    {"id": "est_mau", "titulo": "🔴 Precisa de atenção especial", "fator": 1.15},
]

EXTRAS_ESTETICA = [
    {"id": "exe_nenhum", "titulo": "Nenhum extra", "descricao": "Seguir sem extras", "preco": 0},
    {"id": "exe_farois", "titulo": "Polimento de faróis", "descricao": "Complementar ao serviço principal", "preco": 60},
    {"id": "exe_pneus", "titulo": "Tratamento de pneus/jantes", "descricao": "Acabamento final", "preco": 20},
]

HORARIOS = ["🕘 09:00", "🕥 10:30", "🕐 13:00", "🕝 14:30", "🕓 16:00"]

MENU_PRINCIPAL = [
    {"id": "mp_marcar", "titulo": "📅 Marcar um serviço", "descricao": "Escolher serviço, data e hora"},
    {"id": "mp_orcamento", "titulo": "💰 Pedir orçamento", "descricao": "Sem compromisso, resposta da equipa"},
    {"id": "mp_gerir", "titulo": "🗓️ Gerir a minha marcação", "descricao": "Ver, reagendar ou cancelar"},
    {"id": "mp_humano", "titulo": "💬 Falar com a equipa", "descricao": "Um humano responde-lhe em breve"},
]

CATEGORIAS_MARCAR = [
    {"id": "cat_limpeza", "titulo": "🧼 Limpeza"},
    {"id": "cat_estetica", "titulo": "✨ Estética"},
    {"id": "cat_wrap", "titulo": "🎨 Wrap & Proteção"},
]

NOME_CATEGORIA = {c["id"]: c["titulo"] for c in CATEGORIAS_MARCAR}

RODAPE_PADRAO = "Escreva VOLTAR, CANCELAR ou MENU a qualquer momento"


# ---------------------------------------------------------------------------
# Persistência em SQLite: sessões em curso + agendamentos confirmados
# ---------------------------------------------------------------------------
DB_PATH = os.environ.get("SESSOES_DB", "sessoes.db")


def obter_bd():
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS sessoes (telefone TEXT PRIMARY KEY, dados TEXT NOT NULL)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS agendamentos ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "telefone TEXT NOT NULL, "
        "nome TEXT, "
        "categoria TEXT, "
        "servico TEXT NOT NULL, "
        "extra TEXT, "
        "data TEXT, "
        "hora TEXT, "
        "preco REAL, "
        "duracao TEXT, "
        "estado TEXT DEFAULT 'confirmado', "
        "criado_em TEXT NOT NULL)"
    )
    return conn


def carregar_sessao(telefone):
    with obter_bd() as conn:
        linha = conn.execute(
            "SELECT dados FROM sessoes WHERE telefone = ?", (telefone,)
        ).fetchone()
    return json.loads(linha[0]) if linha else {}


def guardar_sessao(telefone, sessao):
    with obter_bd() as conn:
        conn.execute(
            "INSERT INTO sessoes (telefone, dados) VALUES (?, ?) "
            "ON CONFLICT(telefone) DO UPDATE SET dados = excluded.dados",
            (telefone, json.dumps(sessao)),
        )


def apagar_sessao(telefone):
    with obter_bd() as conn:
        conn.execute("DELETE FROM sessoes WHERE telefone = ?", (telefone,))


def guardar_agendamento(telefone, sessao):
    with obter_bd() as conn:
        cur = conn.execute(
            "INSERT INTO agendamentos "
            "(telefone, nome, categoria, servico, extra, data, hora, preco, duracao, estado, criado_em) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'confirmado', ?)",
            (
                telefone, sessao.get("nome"), sessao.get("categoria"),
                sessao.get("servico"), sessao.get("extra"),
                sessao.get("data"), sessao.get("hora"),
                sessao.get("preco"), sessao.get("duracao"),
                datetime.utcnow().isoformat(),
            ),
        )
        return cur.lastrowid


def listar_agendamentos():
    with obter_bd() as conn:
        linhas = conn.execute(
            "SELECT id, telefone, nome, categoria, servico, extra, data, hora, preco, duracao, estado, criado_em "
            "FROM agendamentos ORDER BY id DESC"
        ).fetchall()
    campos = ["id", "telefone", "nome", "categoria", "servico", "extra", "data", "hora",
              "preco", "duracao", "estado", "criado_em"]
    return [dict(zip(campos, l)) for l in linhas]


def ultimo_agendamento_ativo(telefone):
    with obter_bd() as conn:
        linha = conn.execute(
            "SELECT id, servico, data, hora, preco, duracao FROM agendamentos "
            "WHERE telefone = ? AND estado = 'confirmado' ORDER BY id DESC LIMIT 1",
            (telefone,),
        ).fetchone()
    if not linha:
        return None
    return {"id": linha[0], "servico": linha[1], "data": linha[2], "hora": linha[3],
            "preco": linha[4], "duracao": linha[5]}


def atualizar_estado_agendamento(id_agendamento, estado):
    with obter_bd() as conn:
        conn.execute("UPDATE agendamentos SET estado = ? WHERE id = ?", (estado, id_agendamento))


# ---------------------------------------------------------------------------
# Envio de mensagens
# ---------------------------------------------------------------------------
def enviar(payload):
    headers = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}
    r = requests.post(GRAPH_URL, headers=headers, json=payload, timeout=10)
    print("Resposta da Meta:", r.status_code, r.text)
    return r


def enviar_texto(destinatario, texto):
    enviar({
        "messaging_product": "whatsapp",
        "to": destinatario,
        "type": "text",
        "text": {"body": texto},
    })


def enviar_lista(destinatario, corpo, titulo_seccao, opcoes, botao="👉 Escolher", com_voltar=False, rodape=None):
    """`opcoes`: lista de dicts {"id","titulo","descricao"?} ou strings simples."""
    rows = []
    for i, opc in enumerate(opcoes):
        if isinstance(opc, dict):
            row = {"id": opc.get("id", f"opt_{i}"), "title": opc["titulo"][:24]}
            if opc.get("descricao"):
                row["description"] = opc["descricao"][:72]
        else:
            row = {"id": f"opt_{i}", "title": str(opc)[:24]}
        rows.append(row)

    if com_voltar:
        rows.append({"id": ID_VOLTAR, "title": "⬅️ Voltar", "description": "Passo anterior"})
        rows.append({"id": ID_CANCELAR, "title": "❌ Cancelar processo", "description": "Terminar sem marcar"})

    interactive = {
        "type": "list",
        "body": {"text": corpo},
        "action": {"button": botao, "sections": [{"title": titulo_seccao, "rows": rows}]},
    }
    if rodape:
        interactive["footer"] = {"text": rodape}

    enviar({
        "messaging_product": "whatsapp", "to": destinatario, "type": "interactive",
        "interactive": interactive,
    })


def enviar_botoes(destinatario, corpo, botoes, rodape=None):
    interactive = {
        "type": "button",
        "body": {"text": corpo},
        "action": {"buttons": [
            {"type": "reply", "reply": {"id": b["id"], "title": b["titulo"][:20]}}
            for b in botoes[:3]
        ]},
    }
    if rodape:
        interactive["footer"] = {"text": rodape}
    enviar({
        "messaging_product": "whatsapp", "to": destinatario, "type": "interactive",
        "interactive": interactive,
    })


def encontrar_opcao(opcoes, id_escolhido):
    for opc in opcoes:
        if isinstance(opc, dict) and opc.get("id") == id_escolhido:
            return opc
    return None


def proximos_dias(n=5):
    hoje = date.today()
    dias = []
    for i in range(1, n + 1):
        d = hoje + timedelta(days=i)
        dias.append(f"{d.strftime('%d.%m.%Y')} ({DIAS_SEMANA_PT[d.weekday()]})")
    return dias


def primeiro_nome(nome_completo):
    if not nome_completo:
        return None
    return nome_completo.strip().split(" ")[0]


def formatar_telefone(numero):
    """+41 79 588 63 05 em vez de 41795886305."""
    n = numero.lstrip("+")
    if n.startswith("41") and len(n) == 11:
        return f"+41 {n[2:4]} {n[4:7]} {n[7:9]} {n[9:11]}"
    return f"+{n}"


def preco_formatado(valor):
    return f"CHF {valor:.0f}" if valor else "a combinar"


# ---------------------------------------------------------------------------
# Passos do fluxo "Marcar" — Limpeza
# ---------------------------------------------------------------------------
def passo_limpeza_tipo(de):
    enviar_lista(de, "Passo 1 de 5 — Escolha o tipo de limpeza:", "Tipo de limpeza",
                 LIMPEZA_TIPOS, botao="🧼 Escolher", com_voltar=True, rodape=RODAPE_PADRAO)


def passo_limpeza_tamanho(de):
    enviar_lista(de, "Passo 2 de 5 — Qual o tamanho do veículo?", "Tamanho do veículo",
                 TAMANHOS_VEICULO, botao="🚗 Escolher", com_voltar=True, rodape=RODAPE_PADRAO)


def passo_limpeza_extra(de):
    enviar_lista(de, "Passo 3 de 5 — Deseja algum extra?", "Extras disponíveis",
                 EXTRAS_LIMPEZA, botao="➕ Escolher", com_voltar=True, rodape=RODAPE_PADRAO)


# ---------------------------------------------------------------------------
# Passos do fluxo "Marcar" — Estética
# ---------------------------------------------------------------------------
def passo_estetica_servico(de):
    enviar_lista(de, "Passo 1 de 5 — Escolha o serviço de estética:", "Estética automóvel",
                 ESTETICA_SERVICOS, botao="✨ Escolher", com_voltar=True, rodape=RODAPE_PADRAO)


def passo_estetica_estado(de):
    enviar_lista(de, "Passo 2 de 5 — Como está o estado atual do veículo?", "Estado do veículo",
                 ESTADO_VEICULO, botao="🚗 Escolher", com_voltar=True, rodape=RODAPE_PADRAO)


def passo_estetica_extra(de):
    enviar_lista(de, "Passo 3 de 5 — Deseja algum extra?", "Extras disponíveis",
                 EXTRAS_ESTETICA, botao="➕ Escolher", com_voltar=True, rodape=RODAPE_PADRAO)


# ---------------------------------------------------------------------------
# Data / hora / resumo / confirmação (comuns a limpeza e estética)
# ---------------------------------------------------------------------------
def passo_data(de, passo_n=4):
    enviar_lista(de, f"Passo {passo_n} de 5 — Para que dia gostaria de marcar?", "Datas disponíveis",
                 proximos_dias(), botao="📅 Escolher dia", com_voltar=True, rodape=RODAPE_PADRAO)


def passo_hora(de, passo_n=5):
    enviar_lista(de, f"Passo {passo_n} de 5 — A que horas lhe convém?", "Horários disponíveis",
                 HORARIOS, botao="⏰ Escolher hora", com_voltar=True, rodape=RODAPE_PADRAO)


def calcular_preco_duracao(sessao):
    if sessao.get("categoria") == "cat_limpeza":
        tipo = encontrar_opcao(LIMPEZA_TIPOS, sessao.get("tipo_id")) or {}
        tamanho = encontrar_opcao(TAMANHOS_VEICULO, sessao.get("tamanho_id")) or {"fator": 1.0}
        extra = encontrar_opcao(EXTRAS_LIMPEZA, sessao.get("extra_id")) or {"preco": 0, "titulo": None}
        preco = tipo.get("preco", 0) * tamanho.get("fator", 1.0) + extra.get("preco", 0)
        return round(preco), tipo.get("duracao", "-"), tipo.get("titulo"), extra.get("titulo")
    if sessao.get("categoria") == "cat_estetica":
        serv = encontrar_opcao(ESTETICA_SERVICOS, sessao.get("tipo_id")) or {}
        estado = encontrar_opcao(ESTADO_VEICULO, sessao.get("estado_id")) or {"fator": 1.0}
        extra = encontrar_opcao(EXTRAS_ESTETICA, sessao.get("extra_id")) or {"preco": 0, "titulo": None}
        preco = serv.get("preco", 0) * estado.get("fator", 1.0) + extra.get("preco", 0)
        return round(preco), serv.get("duracao", "-"), serv.get("titulo"), extra.get("titulo")
    return None, None, None, None


def passo_resumo(de, sessao):
    preco, duracao, servico, extra = calcular_preco_duracao(sessao)
    sessao["servico"] = servico
    sessao["extra"] = extra if extra and "nenhum" not in extra.lower() else None
    sessao["preco"] = preco
    sessao["duracao"] = duracao
    guardar_sessao(de, sessao)

    nome = primeiro_nome(sessao.get("nome"))
    linhas = [f"📋 *Confirme a sua marcação*{f', {nome}' if nome else ''}"]
    linhas.append(f"🔧 Serviço: {servico}")
    if sessao.get("extra"):
        linhas.append(f"➕ Extra: {sessao['extra']}")
    linhas.append(f"📅 Data: {sessao['data']}")
    linhas.append(f"🕒 Hora: {sessao['hora']}")
    linhas.append(f"⏱️ Duração estimada: {duracao}")
    linhas.append(f"💰 Preço: {preco_formatado(preco)}")
    linhas.append("\nEstá tudo correto?")

    enviar_botoes(de, "\n".join(linhas), [
        {"id": "confirmar", "titulo": "✅ Confirmar"},
        {"id": "alterar", "titulo": "✏️ Alterar"},
        {"id": ID_CANCELAR, "titulo": "❌ Cancelar"},
    ])


def mensagem_confirmacao_final(sessao):
    nome = primeiro_nome(sessao.get("nome"))
    saudacao = f"Obrigado, {nome}!" if nome else "Obrigado!"
    linhas = [f"🎉 {saudacao} A sua marcação está confirmada!", ""]
    linhas.append(f"🔧 {sessao['servico']}")
    if sessao.get("extra"):
        linhas.append(f"➕ {sessao['extra']}")
    linhas.append(f"📅 {sessao['data']} às {sessao['hora'].split(' ')[-1] if ' ' in sessao['hora'] else sessao['hora']}")
    linhas.append(f"⏱️ Duração: aproximadamente {sessao.get('duracao', '-')}")
    linhas.append(f"📍 {MORADA_OFICINA}")
    linhas.append(f"💰 {preco_formatado(sessao.get('preco'))}")
    linhas.append("")
    linhas.append("Por favor, retire os seus objetos pessoais do veículo antes da entrega.")
    linhas.append("")
    linhas.append("Escreva MENU para nova marcação, ou GERIR para consultar/alterar esta.")
    return "\n".join(linhas)


def mensagem_notificacao_provider(de, sessao, id_agendamento):
    linhas = [f"🆕📅 *Novo pedido confirmado (#{id_agendamento})*", ""]
    linhas.append(f"👤 Cliente: {sessao.get('nome') or 'sem nome'}")
    linhas.append(f"📱 Contacto: {formatar_telefone(de)}")
    linhas.append(f"🔧 Serviço: {sessao['servico']}")
    if sessao.get("extra"):
        linhas.append(f"➕ Extra: {sessao['extra']}")
    linhas.append(f"📅 Data: {sessao['data']} às {sessao['hora']}")
    linhas.append(f"💰 Preço: {preco_formatado(sessao.get('preco'))}")
    linhas.append("")
    linhas.append("Responda com: CONTACTAR, REAGENDAR, CANCELAR ou CONCLUIDO seguido do número da marcação.")
    return "\n".join(linhas)


# ---------------------------------------------------------------------------
# Fluxo "Wrap & Proteção" — mais consultivo, termina em pedido de orçamento
# ---------------------------------------------------------------------------
def passo_wrap_veiculo(de):
    enviar_texto(de, "Passo 1 de 4 — Indique marca, modelo e ano do veículo (ex: \"BMW M4, 2022\").\n\n"
                      f"{RODAPE_PADRAO}")


def passo_wrap_tipo(de):
    enviar_botoes(de, "Passo 2 de 4 — Pretende wrap total ou parcial?", [
        {"id": "wrap_total", "titulo": "🚗 Wrap total"},
        {"id": "wrap_parcial", "titulo": "🔧 Wrap parcial"},
        {"id": ID_CANCELAR, "titulo": "❌ Cancelar"},
    ], rodape=RODAPE_PADRAO)


def passo_wrap_cor(de):
    enviar_texto(de, "Passo 3 de 4 — Que cor/acabamento pretende? (ex: \"Preto fosco\", \"Verde metalizado\")\n\n"
                      f"{RODAPE_PADRAO}")


def passo_wrap_fotos(de):
    enviar_texto(de, "Passo 4 de 4 — Por favor envie 2 a 3 fotografias do veículo (frente, lado e traseira) "
                      "diretamente aqui na conversa. Assim que recebermos, a equipa prepara o seu orçamento.\n\n"
                      "Se preferir avançar sem fotos agora, escreva CONTINUAR.")


def finalizar_pedido_wrap(de, sessao):
    linhas = ["📋 *Pedido de orçamento — Wrap & Proteção*", ""]
    linhas.append(f"👤 Cliente: {sessao.get('nome') or 'sem nome'}")
    linhas.append(f"📱 Contacto: {formatar_telefone(de)}")
    linhas.append(f"🚗 Veículo: {sessao.get('wrap_veiculo', '-')}")
    linhas.append(f"🎨 Tipo: {'Wrap total' if sessao.get('wrap_tipo') == 'wrap_total' else 'Wrap parcial'}")
    linhas.append(f"🖌️ Cor/acabamento: {sessao.get('wrap_cor', '-')}")
    texto_provider = "\n".join(linhas)

    enviar_texto(de, "✅ Pedido de orçamento enviado! A nossa equipa vai analisar os detalhes "
                      f"(e as fotografias, se enviadas) e responde-lhe em breve com o orçamento e disponibilidade "
                      f"para *{sessao.get('wrap_veiculo', 'o seu veículo')}*.\n\n"
                      "Escreva MENU para voltar ao início.")
    if PROVIDER_WHATSAPP:
        enviar_texto(PROVIDER_WHATSAPP, texto_provider + f"\n\n💬 Responda com: CONTACTAR {formatar_telefone(de)}")


# ---------------------------------------------------------------------------
# Menu principal / orçamento genérico / gerir marcação / humano
# ---------------------------------------------------------------------------
def enviar_menu_principal(de, saudacao=True):
    corpo = "Posso ajudá-lo em menos de 1 minuto. O que deseja fazer?"
    if saudacao:
        nome = primeiro_nome(carregar_sessao(de).get("nome"))
        ola = f"👋 Olá, {nome}! Bem-vindo de volta à *{NOME_OFICINA}*." if nome else f"👋 Olá! Bem-vindo à *{NOME_OFICINA}*."
        corpo = f"{ola}\n\n{corpo}"
    enviar_lista(de, corpo, "Menu principal", MENU_PRINCIPAL, botao="👉 Escolher opção")


def passo_orcamento_generico(de):
    enviar_texto(de, "💰 Sem problema! Descreva em poucas palavras o serviço que pretende e o veículo "
                      "(ex: \"Polimento completo, Audi A4 2019\"). A nossa equipa responde com um orçamento em breve.\n\n"
                      f"{RODAPE_PADRAO}")


def mostrar_gestao_marcacao(de):
    ag = ultimo_agendamento_ativo(de)
    if not ag:
        enviar_texto(de, "Não encontrei nenhuma marcação ativa associada a este número.\n\n"
                          "Escreva MENU para fazer uma nova marcação.")
        return
    corpo = (f"🗓️ A sua marcação #{ag['id']}:\n\n"
             f"🔧 {ag['servico']}\n📅 {ag['data']} às {ag['hora']}\n"
             f"⏱️ Duração: {ag.get('duracao', '-')}\n💰 {preco_formatado(ag.get('preco'))}\n\n"
             "O que deseja fazer?")
    enviar_botoes(de, corpo, [
        {"id": f"reagendar_{ag['id']}", "titulo": "✏️ Reagendar"},
        {"id": f"cancelar_ag_{ag['id']}", "titulo": "❌ Cancelar"},
        {"id": "mp_marcar", "titulo": "📅 Nova marcação"},
    ])


def falar_com_equipa(de, sessao):
    enviar_texto(de, "💬 Vou avisar já a nossa equipa — em breve alguém entra em contacto consigo por aqui.\n\n"
                      "Escreva MENU a qualquer momento para voltar ao início.")
    if PROVIDER_WHATSAPP:
        nome = sessao.get("nome") or "sem nome"
        enviar_texto(PROVIDER_WHATSAPP, f"💬 *Pedido de contacto direto*\n\n👤 {nome}\n"
                                         f"📱 {formatar_telefone(de)}\n\nResponda com: CONTACTAR {formatar_telefone(de)}")


def mensagem_ajuda():
    return ("🆘 *Comandos disponíveis, a qualquer momento:*\n\n"
            "• MENU — voltar ao menu principal\n"
            "• VOLTAR — passo anterior\n"
            "• CANCELAR — cancelar o processo atual\n"
            "• GERIR — ver/alterar a sua marcação\n"
            "• AJUDA — mostrar esta mensagem\n"
            "• HUMANO — falar diretamente com a equipa")


def mensagem_nao_entendi():
    return "Desculpe, não consegui perceber 😅\n\nEscolha uma opção ou escreva MENU para recomeçar."


# ---------------------------------------------------------------------------
# Webhook
# ---------------------------------------------------------------------------
@app.route("/api/agendamentos", methods=["GET"])
def api_agendamentos():
    return jsonify(listar_agendamentos()), 200


@app.route("/dashboard", methods=["GET"])
def dashboard():
    return DASHBOARD_HTML


DASHBOARD_HTML = """
<!doctype html>
<html lang="pt">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Painel de Agendamentos</title>
<style>
  :root{
    --bg:#0d0f12; --panel:#15181d; --panel2:#1b1f26; --border:#262b33;
    --gold:#e8b923; --text:#f2f3f5; --muted:#9aa1ac;
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--text);font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;}
  header{padding:24px 28px;border-bottom:1px solid var(--border);display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:12px;}
  header h1{margin:0;font-size:20px;letter-spacing:.3px;}
  header h1 span{color:var(--gold);}
  header .sub{color:var(--muted);font-size:13px;margin-top:4px;}
  .wrap{padding:24px 28px;max-width:1200px;margin:0 auto;}
  .stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:14px;margin-bottom:22px;}
  .card{background:var(--panel);border:1px solid var(--border);border-radius:12px;padding:16px 18px;}
  .card .n{font-size:26px;font-weight:700;color:var(--gold);}
  .card .l{color:var(--muted);font-size:12.5px;margin-top:4px;}
  .lista{background:var(--panel);border:1px solid var(--border);border-radius:12px;overflow:hidden;}
  .lista h2{font-size:15px;margin:0;padding:16px 18px;border-bottom:1px solid var(--border);color:var(--muted);font-weight:600;letter-spacing:.3px;text-transform:uppercase;}
  table{width:100%;border-collapse:collapse;}
  th,td{text-align:left;padding:12px 18px;font-size:14px;border-bottom:1px solid var(--border);}
  th{color:var(--muted);font-weight:600;font-size:12px;text-transform:uppercase;letter-spacing:.4px;}
  tr:last-child td{border-bottom:none;}
  tr:hover td{background:var(--panel2);}
  .tag{display:inline-block;background:rgba(232,185,35,.15);color:var(--gold);padding:3px 9px;border-radius:20px;font-size:12px;font-weight:600;}
  .estado-cancelado{color:#e05252;}
  .vazio{padding:40px 18px;text-align:center;color:var(--muted);}
  .refresh{color:var(--muted);font-size:12px;}
  a.btn{background:var(--gold);color:#1a1400;padding:8px 14px;border-radius:8px;text-decoration:none;font-size:13px;font-weight:700;}
</style>
</head>
<body>
<header>
  <div>
    <h1><span>COVER</span>LAB — Painel de Agendamentos</h1>
    <div class="sub">Marcações recebidas automaticamente via WhatsApp</div>
  </div>
  <a class="btn" href="javascript:location.reload()">🔄 Atualizar</a>
</header>

<div class="wrap">
  <div class="stats">
    <div class="card"><div class="n" id="st-total">0</div><div class="l">Total de agendamentos</div></div>
    <div class="card"><div class="n" id="st-hoje">0</div><div class="l">Marcados hoje (por criação)</div></div>
    <div class="card"><div class="n" id="st-clientes">0</div><div class="l">Clientes únicos</div></div>
    <div class="card"><div class="n" id="st-receita">CHF 0</div><div class="l">Receita estimada (confirmados)</div></div>
  </div>

  <div class="lista">
    <h2>Agendamentos</h2>
    <div id="conteudo"><div class="vazio">A carregar…</div></div>
  </div>
  <div class="refresh" style="margin-top:10px;">Atualiza-se sozinho a cada 20 segundos.</div>
</div>

<script>
async function carregar(){
  const resp = await fetch('/api/agendamentos');
  const dados = await resp.json();

  document.getElementById('st-total').textContent = dados.length;

  const hojeStr = new Date().toISOString().slice(0,10);
  const hoje = dados.filter(d => (d.criado_em||'').slice(0,10) === hojeStr).length;
  document.getElementById('st-hoje').textContent = hoje;

  const clientes = new Set(dados.map(d => d.telefone));
  document.getElementById('st-clientes').textContent = clientes.size;

  const receita = dados.filter(d => d.estado === 'confirmado').reduce((s,d) => s + (d.preco||0), 0);
  document.getElementById('st-receita').textContent = 'CHF ' + receita.toFixed(0);

  const cont = document.getElementById('conteudo');
  if(dados.length === 0){
    cont.innerHTML = '<div class="vazio">Ainda não há marcações. Manda uma mensagem ao bot no WhatsApp para testar 👋</div>';
    return;
  }

  let html = '<table><thead><tr><th>Cliente</th><th>Serviço</th><th>Data</th><th>Hora</th><th>Preço</th><th>Estado</th><th>Recebido em</th></tr></thead><tbody>';
  dados.forEach(d => {
    const criado = d.criado_em ? new Date(d.criado_em).toLocaleString('pt-PT') : '-';
    const classeEstado = d.estado !== 'confirmado' ? 'estado-cancelado' : '';
    html += `<tr>
      <td>${d.nome || d.telefone}<br><span style="color:var(--muted);font-size:12px;">${d.telefone}</span></td>
      <td><span class="tag">${d.servico}</span>${d.extra ? '<br><span style="color:var(--muted);font-size:12px;">+ '+d.extra+'</span>' : ''}</td>
      <td>${d.data || '-'}</td>
      <td>${d.hora || '-'}</td>
      <td>${d.preco ? 'CHF '+d.preco : '-'}</td>
      <td class="${classeEstado}">${d.estado}</td>
      <td style="color:var(--muted);">${criado}</td>
    </tr>`;
  });
  html += '</tbody></table>';
  cont.innerHTML = html;
}
carregar();
setInterval(carregar, 20000);
</script>
</body>
</html>
"""


@app.route("/versao", methods=["GET"])
def versao():
    return jsonify(versao="v3-recepcionista-digital", fluxos=["limpeza", "estetica", "wrap"]), 200


@app.route("/webhook", methods=["GET"])
def verificar_webhook():
    if request.args.get("hub.verify_token") == VERIFY_TOKEN:
        return request.args.get("hub.challenge", ""), 200
    return "Token inválido", 403


def reiniciar_sessao(de, manter_nome=True):
    sessao_antiga = carregar_sessao(de)
    nova = {"nome": sessao_antiga.get("nome")} if manter_nome and sessao_antiga.get("nome") else {}
    guardar_sessao(de, nova)
    return nova


def sessao_em_curso(sessao):
    """Considera-se 'em curso' se já escolheu categoria mas ainda não confirmou."""
    return bool(sessao.get("categoria") or sessao.get("fluxo"))


def processar_comando_texto(de, sessao, comando):
    if comando == "menu":
        reiniciar_sessao(de)
        enviar_menu_principal(de, saudacao=True)
        return True
    if comando == "ajuda":
        enviar_texto(de, mensagem_ajuda())
        return True
    if comando == "humano":
        falar_com_equipa(de, sessao)
        reiniciar_sessao(de)
        return True
    if comando == "gerir":
        mostrar_gestao_marcacao(de)
        return True
    if comando == "cancelar":
        reiniciar_sessao(de)
        enviar_texto(de, "❌ Processo cancelado. Escreva MENU quando quiser recomeçar.")
        return True
    if comando == "voltar":
        voltar_um_passo(de, sessao)
        return True
    return False


def voltar_um_passo(de, sessao):
    fluxo = sessao.get("fluxo")
    categoria = sessao.get("categoria")

    if fluxo == "wrap":
        if "wrap_cor" in sessao:
            sessao.pop("wrap_cor", None); guardar_sessao(de, sessao); passo_wrap_cor(de)
        elif "wrap_tipo" in sessao:
            sessao.pop("wrap_tipo", None); guardar_sessao(de, sessao); passo_wrap_tipo(de)
        elif "wrap_veiculo" in sessao:
            sessao.pop("wrap_veiculo", None); guardar_sessao(de, sessao); passo_wrap_veiculo(de)
        else:
            reiniciar_sessao(de); enviar_menu_principal(de, saudacao=False)
        return

    if categoria in ("cat_limpeza", "cat_estetica"):
        if "hora" in sessao:
            sessao.pop("hora", None); guardar_sessao(de, sessao); passo_hora(de)
        elif "data" in sessao:
            sessao.pop("data", None); guardar_sessao(de, sessao); passo_data(de)
        elif "extra_id" in sessao:
            sessao.pop("extra_id", None); guardar_sessao(de, sessao)
            (passo_limpeza_extra if categoria == "cat_limpeza" else passo_estetica_extra)(de)
        elif categoria == "cat_limpeza" and "tamanho_id" in sessao:
            sessao.pop("tamanho_id", None); guardar_sessao(de, sessao); passo_limpeza_tamanho(de)
        elif categoria == "cat_estetica" and "estado_id" in sessao:
            sessao.pop("estado_id", None); guardar_sessao(de, sessao); passo_estetica_estado(de)
        elif "tipo_id" in sessao:
            sessao.pop("tipo_id", None); sessao.pop("categoria", None)
            guardar_sessao(de, sessao)
            enviar_lista(de, "Que tipo de serviço procura?", "Categorias", CATEGORIAS_MARCAR,
                         botao="👉 Escolher") if False else enviar_botoes(
                de, "Que tipo de serviço procura?", CATEGORIAS_MARCAR, rodape=RODAPE_PADRAO)
        else:
            reiniciar_sessao(de); enviar_menu_principal(de, saudacao=False)
        return

    reiniciar_sessao(de)
    enviar_menu_principal(de, saudacao=False)


@app.route("/webhook", methods=["POST"])
def receber_mensagem():
    data = request.get_json(force=True)
    try:
        entry = data["entry"][0]["changes"][0]["value"]
        if "messages" not in entry:
            return jsonify(status="ignorado"), 200

        msg = entry["messages"][0]
        de = msg["from"]
        sessao = carregar_sessao(de)

        try:
            nome_perfil = entry["contacts"][0]["profile"]["name"]
            if nome_perfil:
                sessao["nome"] = nome_perfil
                guardar_sessao(de, sessao)
        except (KeyError, IndexError):
            pass

        tipo = msg.get("type")

        # --- Texto livre: comandos permanentes, retomar sessão, ou 1ª msg ---
        if tipo == "text":
            texto = msg["text"]["body"].strip().lower()

            if texto in COMANDOS_TEXTO:
                processar_comando_texto(de, sessao, texto)
                return jsonify(status="ok"), 200

            if sessao.get("_a_confirmar_retomar"):
                sessao.pop("_a_confirmar_retomar", None)
                if texto in ("continuar", "sim"):
                    guardar_sessao(de, sessao)
                    reenviar_passo_atual(de, sessao)
                else:
                    reiniciar_sessao(de)
                    enviar_menu_principal(de, saudacao=True)
                return jsonify(status="ok"), 200

            if sessao.get("fluxo") == "wrap" and "wrap_veiculo" not in sessao:
                sessao["wrap_veiculo"] = msg["text"]["body"].strip()
                guardar_sessao(de, sessao)
                passo_wrap_tipo(de)
                return jsonify(status="ok"), 200

            if sessao.get("fluxo") == "wrap" and "wrap_tipo" in sessao and "wrap_cor" not in sessao:
                sessao["wrap_cor"] = msg["text"]["body"].strip()
                guardar_sessao(de, sessao)
                passo_wrap_fotos(de)
                return jsonify(status="ok"), 200

            if sessao.get("fluxo") == "wrap" and "wrap_cor" in sessao and texto == "continuar":
                finalizar_pedido_wrap(de, sessao)
                reiniciar_sessao(de)
                return jsonify(status="ok"), 200

            if sessao.get("fluxo") == "orcamento":
                enviar_texto(de, "✅ Recebido! A equipa vai analisar e responde-lhe em breve.\n\nEscreva MENU para voltar ao início.")
                if PROVIDER_WHATSAPP:
                    enviar_texto(PROVIDER_WHATSAPP,
                                 f"💰 *Pedido de orçamento genérico*\n\n👤 {sessao.get('nome') or 'sem nome'}\n"
                                 f"📱 {formatar_telefone(de)}\n📝 \"{msg['text']['body'].strip()}\"")
                reiniciar_sessao(de)
                return jsonify(status="ok"), 200

            # sessão em curso (categoria já escolhida, mas mensagem de texto inesperada)
            if sessao_em_curso(sessao):
                sessao["_a_confirmar_retomar"] = True
                guardar_sessao(de, sessao)
                enviar_botoes(de, "Encontrámos uma marcação que ainda não terminou.\nDeseja continuar ou começar novamente?", [
                    {"id": "retomar_continuar", "titulo": "▶️ Continuar"},
                    {"id": "retomar_recomecar", "titulo": "🔄 Recomeçar"},
                ])
                return jsonify(status="ok"), 200

            # primeira mensagem / sem sessão em curso -> menu principal
            enviar_menu_principal(de, saudacao=True)
            return jsonify(status="ok"), 200

        # --- Botões -----------------------------------------------------
        if tipo == "interactive" and msg["interactive"]["type"] == "button_reply":
            id_botao = msg["interactive"]["button_reply"]["id"]

            if id_botao == ID_CANCELAR:
                reiniciar_sessao(de)
                enviar_texto(de, "❌ Processo cancelado. Escreva MENU quando quiser recomeçar.")
                return jsonify(status="ok"), 200

            if id_botao in ("retomar_continuar", "retomar_recomecar"):
                sessao.pop("_a_confirmar_retomar", None)
                if id_botao == "retomar_continuar":
                    guardar_sessao(de, sessao)
                    reenviar_passo_atual(de, sessao)
                else:
                    reiniciar_sessao(de)
                    enviar_menu_principal(de, saudacao=True)
                return jsonify(status="ok"), 200

            if id_botao in NOME_CATEGORIA:  # categoria dentro de "Marcar"
                if id_botao == "cat_wrap":
                    sessao.update({"fluxo": "wrap", "categoria": "cat_wrap"})
                    guardar_sessao(de, sessao)
                    passo_wrap_veiculo(de)
                else:
                    sessao.update({"fluxo": "marcar", "categoria": id_botao})
                    guardar_sessao(de, sessao)
                    (passo_limpeza_tipo if id_botao == "cat_limpeza" else passo_estetica_servico)(de)
                return jsonify(status="ok"), 200

            if id_botao in ("wrap_total", "wrap_parcial"):
                sessao["wrap_tipo"] = id_botao
                guardar_sessao(de, sessao)
                passo_wrap_cor(de)
                return jsonify(status="ok"), 200

            if id_botao == "confirmar":
                id_ag = guardar_agendamento(de, sessao)
                enviar_texto(de, mensagem_confirmacao_final(sessao))
                if PROVIDER_WHATSAPP:
                    enviar_texto(PROVIDER_WHATSAPP, mensagem_notificacao_provider(de, sessao, id_ag))
                reiniciar_sessao(de)
                return jsonify(status="ok"), 200

            if id_botao == "alterar":
                categoria = sessao.get("categoria")
                for campo in ("tipo_id", "tamanho_id", "estado_id", "extra_id", "data", "hora",
                              "servico", "extra", "preco", "duracao"):
                    sessao.pop(campo, None)
                guardar_sessao(de, sessao)
                (passo_limpeza_tipo if categoria == "cat_limpeza" else passo_estetica_servico)(de)
                return jsonify(status="ok"), 200

            if id_botao.startswith("reagendar_"):
                id_ag = int(id_botao.split("_")[-1])
                atualizar_estado_agendamento(id_ag, "reagendado")
                sessao = {"nome": sessao.get("nome")} if sessao.get("nome") else {}
                guardar_sessao(de, sessao)
                enviar_texto(de, "Sem problema, vamos criar uma nova marcação. A anterior foi arquivada.")
                enviar_botoes(de, "Que tipo de serviço procura?", CATEGORIAS_MARCAR, rodape=RODAPE_PADRAO)
                return jsonify(status="ok"), 200

            if id_botao.startswith("cancelar_ag_"):
                id_ag = int(id_botao.split("_")[-1])
                atualizar_estado_agendamento(id_ag, "cancelado")
                enviar_texto(de, "✅ A sua marcação foi cancelada. Escreva MENU quando quiser marcar novamente.")
                if PROVIDER_WHATSAPP:
                    enviar_texto(PROVIDER_WHATSAPP, f"❌ Marcação #{id_ag} cancelada pelo cliente {formatar_telefone(de)}.")
                return jsonify(status="ok"), 200

            enviar_texto(de, mensagem_nao_entendi())
            return jsonify(status="ok"), 200

        # --- Listas -------------------------------------------------------
        if tipo == "interactive" and msg["interactive"]["type"] == "list_reply":
            id_escolhido = msg["interactive"]["list_reply"]["id"]

            if id_escolhido == ID_CANCELAR:
                reiniciar_sessao(de)
                enviar_texto(de, "❌ Processo cancelado. Escreva MENU quando quiser recomeçar.")
                return jsonify(status="ok"), 200

            if id_escolhido == ID_VOLTAR:
                voltar_um_passo(de, sessao)
                return jsonify(status="ok"), 200

            # Menu principal
            if id_escolhido == "mp_marcar":
                sessao["fluxo"] = "escolher_categoria"
                guardar_sessao(de, sessao)
                enviar_botoes(de, "Que tipo de serviço procura?", CATEGORIAS_MARCAR, rodape=RODAPE_PADRAO)
                return jsonify(status="ok"), 200
            if id_escolhido == "mp_orcamento":
                sessao["fluxo"] = "orcamento"
                guardar_sessao(de, sessao)
                passo_orcamento_generico(de)
                return jsonify(status="ok"), 200
            if id_escolhido == "mp_gerir":
                mostrar_gestao_marcacao(de)
                return jsonify(status="ok"), 200
            if id_escolhido == "mp_humano":
                falar_com_equipa(de, sessao)
                reiniciar_sessao(de)
                return jsonify(status="ok"), 200

            categoria = sessao.get("categoria")

            # Limpeza
            if categoria == "cat_limpeza":
                if "tipo_id" not in sessao:
                    sessao["tipo_id"] = id_escolhido; guardar_sessao(de, sessao); passo_limpeza_tamanho(de)
                elif "tamanho_id" not in sessao:
                    sessao["tamanho_id"] = id_escolhido; guardar_sessao(de, sessao); passo_limpeza_extra(de)
                elif "extra_id" not in sessao:
                    sessao["extra_id"] = id_escolhido; guardar_sessao(de, sessao); passo_data(de)
                elif "data" not in sessao:
                    sessao["data"] = msg["interactive"]["list_reply"]["title"]; guardar_sessao(de, sessao); passo_hora(de)
                elif "hora" not in sessao:
                    sessao["hora"] = msg["interactive"]["list_reply"]["title"]; guardar_sessao(de, sessao)
                    passo_resumo(de, sessao)
                return jsonify(status="ok"), 200

            # Estética
            if categoria == "cat_estetica":
                if "tipo_id" not in sessao:
                    sessao["tipo_id"] = id_escolhido; guardar_sessao(de, sessao); passo_estetica_estado(de)
                elif "estado_id" not in sessao:
                    sessao["estado_id"] = id_escolhido; guardar_sessao(de, sessao); passo_estetica_extra(de)
                elif "extra_id" not in sessao:
                    sessao["extra_id"] = id_escolhido; guardar_sessao(de, sessao); passo_data(de)
                elif "data" not in sessao:
                    sessao["data"] = msg["interactive"]["list_reply"]["title"]; guardar_sessao(de, sessao); passo_hora(de)
                elif "hora" not in sessao:
                    sessao["hora"] = msg["interactive"]["list_reply"]["title"]; guardar_sessao(de, sessao)
                    passo_resumo(de, sessao)
                return jsonify(status="ok"), 200

            enviar_texto(de, mensagem_nao_entendi())
            return jsonify(status="ok"), 200

        # --- Qualquer outro tipo (áudio, imagem, sticker, etc.) -------------
        if tipo == "image" and sessao.get("fluxo") == "wrap" and "wrap_cor" in sessao:
            enviar_texto(de, "📸 Fotografia recebida, obrigado! Pode enviar mais, ou escreva CONTINUAR para terminarmos o pedido.")
            return jsonify(status="ok"), 200

        enviar_texto(de, mensagem_nao_entendi())

    except (KeyError, IndexError):
        pass  # notificações de status (entregue/lido) chegam neste mesmo endpoint — ignora-as

    return jsonify(status="ok"), 200


def reenviar_passo_atual(de, sessao):
    """Reenvia o ecrã correspondente ao ponto exato onde a sessão ficou."""
    categoria = sessao.get("categoria")
    fluxo = sessao.get("fluxo")

    if fluxo == "wrap":
        if "wrap_cor" in sessao:
            passo_wrap_fotos(de)
        elif "wrap_tipo" in sessao:
            passo_wrap_cor(de)
        elif "wrap_veiculo" in sessao:
            passo_wrap_tipo(de)
        else:
            passo_wrap_veiculo(de)
        return

    if categoria == "cat_limpeza":
        if "hora" in sessao:
            passo_resumo(de, sessao)
        elif "data" in sessao:
            passo_hora(de)
        elif "extra_id" in sessao:
            passo_data(de)
        elif "tamanho_id" in sessao:
            passo_limpeza_extra(de)
        elif "tipo_id" in sessao:
            passo_limpeza_tamanho(de)
        else:
            passo_limpeza_tipo(de)
        return

    if categoria == "cat_estetica":
        if "hora" in sessao:
            passo_resumo(de, sessao)
        elif "data" in sessao:
            passo_hora(de)
        elif "extra_id" in sessao:
            passo_data(de)
        elif "estado_id" in sessao:
            passo_estetica_extra(de)
        elif "tipo_id" in sessao:
            passo_estetica_estado(de)
        else:
            passo_estetica_servico(de)
        return

    enviar_menu_principal(de, saudacao=False)


if __name__ == "__main__":
    porta = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=porta, debug=True)
