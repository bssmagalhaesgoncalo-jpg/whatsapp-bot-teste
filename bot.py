"""
Bot de marcação via WhatsApp Cloud API — fluxo por botões/listas, tipo
"mini-formulário": categoria (botões) -> serviço (lista) -> dia (lista) ->
hora (lista) -> confirmação (botões Sim/Não) -> reencaminha ao prestador.

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
from datetime import date, timedelta
from flask import Flask, request, jsonify

app = Flask(__name__)

# Os valores por defeito abaixo já vêm preenchidos com os dados do teu app
# "Booking Bot Teste" na Meta, para testares mais depressa. Troca-os por
# variáveis de ambiente sempre que os regenerares (o token temporário expira
# em ~24h) — nunca deixes um token real num ficheiro que vá para o GitHub.
TOKEN = os.environ.get("WHATSAPP_TOKEN", "EAAO1JeV5Q60BSRXGwSYzqbZBmOFY9FsyH0O3C6s4v5D45thREU1TGNehSkYvkkJ0jSSH7xl6ZBViIeD2DGTdHzTyxH6byTDLQPIVvfpJGIE1ZBhChENZAN43AY070yMzNKdl21A9pMQiMoG5O30lDdoGgGIDZC1tr3ucb8hQZApvaUyHYGYSueRWEPaotgW6iExH0sIjAgJZA0XZA2P38BMfd5MPhGYiXBFJ6ZBM0l1URdXczEGZB0ZBW5DPFiADAspUVo7ArkvSBVkxvZCZCABqd7CWwCAZDZD")
PHONE_NUMBER_ID = os.environ.get("PHONE_NUMBER_ID", "1052227394639217")
VERIFY_TOKEN = os.environ.get("VERIFY_TOKEN", "teste123")
PROVIDER_WHATSAPP = os.environ.get("PROVIDER_WHATSAPP", "41795886305")

GRAPH_URL = f"https://graph.facebook.com/v21.0/{PHONE_NUMBER_ID}/messages"

# Nome da oficina fictícia, só para testes. Serviços copiados do site real da
# Spotless Car Detail (cardetailspotless.com) — usados aqui só para testar o
# fluxo, a oficina em si é inventada.
NOME_OFICINA = "Spotless Car Detail (TESTE)"

# Passo 1 do "mini-formulário": categorias, mostradas como botões (máx. 3
# botões por mensagem no WhatsApp — é o limite da própria API).
CATEGORIAS = [
    {"id": "cat_protecao", "titulo": "🎨 Proteção & Wrap"},
    {"id": "cat_limpeza",  "titulo": "🧼 Limpeza"},
    {"id": "cat_extra",    "titulo": "✨ Estética Extra"},
]

# Passo 2: dentro de cada categoria, a lista de serviços específicos
# (título, emoji e descrição curta — a descrição aparece como subtítulo em
# cada linha da lista do WhatsApp). Serviços copiados do site real da
# Spotless Car Detail, só para testar o fluxo.
SERVICOS_POR_CATEGORIA = {
    "cat_protecao": [
        {"emoji": "🎨", "titulo": "Car-Wrap", "descricao": "Muda a cor do teu carro com película"},
        {"emoji": "🛡️", "titulo": "PPF",       "descricao": "Película de proteção de pintura"},
    ],
    "cat_limpeza": [
        {"emoji": "🧼", "titulo": "Limpeza interior/exterior", "descricao": "Higienização completa do veículo"},
    ],
    "cat_extra": [
        {"emoji": "💡", "titulo": "Polimento de faróis", "descricao": "Recupera a transparência dos faróis"},
        {"emoji": "✨", "titulo": "Polimento",             "descricao": "Remove riscos e devolve o brilho"},
        {"emoji": "🏷️", "titulo": "Stickers",             "descricao": "Autocolantes personalizados"},
        {"emoji": "🖼️", "titulo": "Overlay",              "descricao": "Acabamento especial de destaque"},
    ],
}

NOME_CATEGORIA = {c["id"]: c["titulo"] for c in CATEGORIAS}

HORARIOS = ["🕘 09:00", "🕥 10:30", "🕐 13:00", "🕝 14:30", "🕓 16:00"]

DIAS_SEMANA_PT = ["seg", "ter", "qua", "qui", "sex", "sáb", "dom"]

ID_VOLTAR = "voltar"


# ---------------------------------------------------------------------------
# Persistência das sessões em SQLite (em vez de um dicionário em memória).
# Assim, se o processo reiniciar (deploy novo, ou o serviço "adormecer" e
# acordar), as conversas em curso não se perdem. Nota: no plano gratuito do
# Render o disco pode ser limpo quando fazes um deploy novo — para produção
# a sério, o ideal é uma base de dados externa (Postgres, Redis, etc.).
# ---------------------------------------------------------------------------
DB_PATH = os.environ.get("SESSOES_DB", "sessoes.db")


def obter_bd():
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS sessoes (telefone TEXT PRIMARY KEY, dados TEXT NOT NULL)"
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


def enviar_lista(destinatario, corpo, titulo_seccao, opcoes, botao="👉 Escolher", com_voltar=False):
    """Envia uma lista interativa (até 10 opções) — o equivalente aos botões do Telegram.

    `opcoes` pode ser uma lista de strings simples (ex.: horários) ou de
    dicionários {"emoji", "titulo", "descricao"} (ex.: serviços), para dar
    um subtítulo a cada linha. Com `com_voltar=True`, acrescenta uma última
    linha "🔙 Voltar" para o cliente recuar um passo.
    """
    rows = []
    for i, opc in enumerate(opcoes):
        if isinstance(opc, dict):
            titulo = f"{opc['emoji']} {opc['titulo']}"[:24]
            row = {"id": f"opt_{i}", "title": titulo, "description": opc.get("descricao", "")[:72]}
        else:
            row = {"id": f"opt_{i}", "title": str(opc)[:24]}
        rows.append(row)

    if com_voltar:
        rows.append({"id": ID_VOLTAR, "title": "🔙 Voltar", "description": "Escolher outra vez o passo anterior"})

    enviar({
        "messaging_product": "whatsapp",
        "to": destinatario,
        "type": "interactive",
        "interactive": {
            "type": "list",
            "body": {"text": corpo},
            "action": {
                "button": botao,
                "sections": [{"title": titulo_seccao, "rows": rows}],
            },
        },
    })


def enviar_botoes(destinatario, corpo, botoes, rodape=None):
    """Envia até 3 botões de resposta rápida — aparecem já na conversa,
    sem precisar abrir uma lista (é o equivalente mais próximo de um
    'campo' de formulário do WhatsApp)."""
    interactive = {
        "type": "button",
        "body": {"text": corpo},
        "action": {
            "buttons": [
                {"type": "reply", "reply": {"id": b["id"], "title": b["titulo"][:20]}}
                for b in botoes[:3]
            ]
        },
    }
    if rodape:
        interactive["footer"] = {"text": rodape}

    enviar({
        "messaging_product": "whatsapp",
        "to": destinatario,
        "type": "interactive",
        "interactive": interactive,
    })


def titulo_escolhido(opcoes, id_escolhido, titulo_bruto):
    """Recupera o título 'limpo' (sem emoji) de uma opção, a partir do id devolvido pelo WhatsApp."""
    try:
        indice = int(id_escolhido.replace("opt_", ""))
        opc = opcoes[indice]
        if isinstance(opc, dict):
            return opc["titulo"]
    except (ValueError, IndexError, KeyError):
        pass
    return titulo_bruto


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


# ---------------------------------------------------------------------------
# Passos do formulário — cada função envia o menu de um passo específico
# ---------------------------------------------------------------------------
def passo_categoria(de, saudacao=True):
    corpo = "Que tipo de serviço procura?"
    if saudacao:
        corpo = f"👋 Olá! Bem-vindo(a) à *{NOME_OFICINA}* 🚗✨\n\n{corpo}"
    enviar_botoes(de, corpo, CATEGORIAS, rodape="Escolha uma categoria para continuar")


def passo_servico(de, categoria_id):
    enviar_lista(
        de,
        f"Categoria: *{NOME_CATEGORIA[categoria_id]}*\n\n🔧 Escolha o serviço:",
        "Serviços",
        SERVICOS_POR_CATEGORIA[categoria_id],
        botao="🔧 Ver serviços",
        com_voltar=True,
    )


def passo_data(de, servico):
    enviar_lista(
        de,
        f"Ótima escolha! ✅ *{servico}*\n\n📅 Para que dia gostaria de marcar?",
        "Datas disponíveis",
        proximos_dias(),
        botao="📅 Escolher dia",
        com_voltar=True,
    )


def passo_hora(de, dia):
    enviar_lista(
        de,
        f"Perfeito, dia *{dia}* 👍\n\n⏰ A que horas lhe convém?",
        "Horários disponíveis",
        HORARIOS,
        botao="⏰ Escolher hora",
        com_voltar=True,
    )


def passo_confirmacao(de, sessao):
    nome = primeiro_nome(sessao.get("nome"))
    saudacao = f"{nome}, confirma" if nome else "Confirma"
    resumo = montar_resumo(de, sessao)
    enviar_botoes(
        de,
        f"{saudacao} os dados da sua marcação? 🧐\n\n{resumo}",
        [
            {"id": "confirmar", "titulo": "✅ Confirmar"},
            {"id": "alterar",   "titulo": "✏️ Alterar"},
        ],
        rodape="Toque num dos botões acima",
    )


def montar_resumo(de, sessao):
    nome = sessao.get("nome")
    quem = f"{nome} ({de})" if nome else de
    return (f"👤 Contacto: {quem}\n🔧 Serviço: {sessao['servico']}\n"
            f"📅 Data: {sessao['data']}\n⏰ Hora: {sessao['hora']}")


# ---------------------------------------------------------------------------
# Webhook
# ---------------------------------------------------------------------------
@app.route("/versao", methods=["GET"])
def versao():
    """Rota simples para confirmar qual versão do código está a correr no Render."""
    return jsonify(versao="v2-com-voltar-confirmacao-sqlite", tem_botao_voltar=True), 200


@app.route("/webhook", methods=["GET"])
def verificar_webhook():
    """A Meta chama isto uma vez, para confirmar que o webhook é teu."""
    if request.args.get("hub.verify_token") == VERIFY_TOKEN:
        return request.args.get("hub.challenge", ""), 200
    return "Token inválido", 403


@app.route("/webhook", methods=["POST"])
def receber_mensagem():
    data = request.get_json(force=True)
    try:
        entry = data["entry"][0]["changes"][0]["value"]
        if "messages" not in entry:
            return jsonify(status="ignorado"), 200

        msg = entry["messages"][0]
        de = msg["from"]  # número do cliente
        sessao = carregar_sessao(de)

        # Nome do perfil de WhatsApp do cliente, quando disponível
        try:
            nome_perfil = entry["contacts"][0]["profile"]["name"]
            if nome_perfil:
                sessao["nome"] = nome_perfil
        except (KeyError, IndexError):
            pass

        tipo = msg.get("type")

        # --- Botões (categoria, ou confirmar/alterar no fim) ---------------
        if tipo == "interactive" and msg["interactive"]["type"] == "button_reply":
            id_botao = msg["interactive"]["button_reply"]["id"]

            if id_botao in SERVICOS_POR_CATEGORIA:
                sessao["categoria"] = id_botao
                sessao.pop("servico", None)
                sessao.pop("data", None)
                sessao.pop("hora", None)
                guardar_sessao(de, sessao)
                passo_servico(de, id_botao)

            elif id_botao == "confirmar":
                resumo = montar_resumo(de, sessao)
                nome = primeiro_nome(sessao.get("nome"))
                saudacao = f"Obrigado, {nome}!" if nome else "Obrigado!"

                enviar_texto(de, f"🎉 {saudacao} A sua marcação está confirmada:\n\n{resumo}\n\n"
                                  f"✅ Vamos preparar tudo para o seu dia.\n"
                                  f"_{NOME_OFICINA} agradece a sua preferência!_ 🚗💨")

                if PROVIDER_WHATSAPP:
                    enviar_texto(PROVIDER_WHATSAPP,
                                 f"🆕📅 *Novo pedido de marcação através do bot:*\n\n{resumo}\n"
                                 f"💬 Cliente: wa.me/{de}")

                apagar_sessao(de)
                return jsonify(status="ok"), 200

            elif id_botao == "alterar":
                sessao.pop("servico", None)
                sessao.pop("data", None)
                sessao.pop("hora", None)
                sessao.pop("categoria", None)
                guardar_sessao(de, sessao)
                passo_categoria(de, saudacao=False)
                return jsonify(status="ok"), 200

        # --- Listas (serviço, dia, hora, ou "voltar") -----------------------
        elif tipo == "interactive" and msg["interactive"]["type"] == "list_reply":
            id_escolhido = msg["interactive"]["list_reply"]["id"]
            titulo_bruto = msg["interactive"]["list_reply"]["title"]

            if id_escolhido == ID_VOLTAR:
                if "data" in sessao:
                    sessao.pop("data", None)
                    guardar_sessao(de, sessao)
                    passo_servico(de, sessao["categoria"])
                elif "servico" in sessao:
                    sessao.pop("servico", None)
                    guardar_sessao(de, sessao)
                    passo_categoria(de, saudacao=False)
                else:
                    sessao.pop("categoria", None)
                    guardar_sessao(de, sessao)
                    passo_categoria(de, saudacao=False)

            elif "servico" not in sessao:
                opcoes_categoria = SERVICOS_POR_CATEGORIA.get(sessao.get("categoria"), [])
                sessao["servico"] = titulo_escolhido(opcoes_categoria, id_escolhido, titulo_bruto)
                guardar_sessao(de, sessao)
                passo_data(de, sessao["servico"])

            elif "data" not in sessao:
                sessao["data"] = titulo_bruto
                guardar_sessao(de, sessao)
                passo_hora(de, titulo_bruto)

            elif "hora" not in sessao:
                sessao["hora"] = titulo_bruto
                guardar_sessao(de, sessao)
                passo_confirmacao(de, sessao)

        # --- Mensagem de texto normal reinicia o fluxo ----------------------
        elif tipo == "text":
            sessao = {"nome": sessao.get("nome")} if sessao.get("nome") else {}
            guardar_sessao(de, sessao)
            passo_categoria(de, saudacao=True)

        # --- Qualquer outro tipo (áudio, imagem, sticker, etc.) -------------
        else:
            enviar_texto(de, "🤔 Não percebi essa mensagem. Escreva *\"olá\"* para começar uma marcação.")

    except (KeyError, IndexError):
        pass  # notificações de status (entregue/lido) chegam neste mesmo endpoint — ignora-as

    return jsonify(status="ok"), 200


if __name__ == "__main__":
    # O Render (e serviços parecidos) define a porta via variável de ambiente PORT.
    # Localmente, sem essa variável, continua a usar 5000 como até agora.
    porta = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=porta, debug=True)
