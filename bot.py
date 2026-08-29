"""
Bot de marcação via WhatsApp Cloud API — fluxo por botões/listas.

Fluxo:
  Cliente escreve qualquer coisa -> bot mostra lista de serviços
  Cliente escolhe serviço -> bot mostra lista de datas (próximos 5 dias)
  Cliente escolhe data -> bot mostra lista de horários
  Cliente escolhe horário -> bot confirma ao cliente E reencaminha o pedido
  para o WhatsApp do prestador de serviço (via mensagem de texto normal).

Configuração necessária (variáveis de ambiente):
  WHATSAPP_TOKEN       - access token (temporário ou permanente) da Meta
  PHONE_NUMBER_ID      - ID do número de teste/produção (em API Setup)
  VERIFY_TOKEN         - qualquer string à tua escolha, usada na verificação do webhook
  PROVIDER_WHATSAPP    - número do prestador de serviço em formato internacional, ex: 41795886305

Como correr:
  pip install flask requests
  export WHATSAPP_TOKEN=... PHONE_NUMBER_ID=... VERIFY_TOKEN=... PROVIDER_WHATSAPP=...
  python bot.py
  # noutro terminal: ngrok http 5000
  # usa o URL https do ngrok + "/webhook" como Callback URL na Meta, com o mesmo VERIFY_TOKEN
"""

import os
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

# título, emoji e descrição curta de cada serviço (a descrição aparece
# como subtítulo em cada linha da lista do WhatsApp)
SERVICOS = [
    {"emoji": "🎨", "titulo": "Car-Wrap",              "descricao": "Muda a cor do teu carro com película"},
    {"emoji": "🛡️", "titulo": "PPF",                    "descricao": "Película de proteção de pintura"},
    {"emoji": "💡", "titulo": "Polimento de faróis",    "descricao": "Recupera a transparência dos faróis"},
    {"emoji": "🧼", "titulo": "Limpeza interior/exterior","descricao": "Higienização completa do veículo"},
    {"emoji": "✨", "titulo": "Polimento",               "descricao": "Remove riscos e devolve o brilho"},
    {"emoji": "🏷️", "titulo": "Stickers",               "descricao": "Autocolantes personalizados"},
    {"emoji": "🖼️", "titulo": "Overlay",                "descricao": "Acabamento especial de destaque"},
]
HORARIOS = ["🕘 09:00", "🕥 10:30", "🕐 13:00", "🕝 14:30", "🕓 16:00"]

# Estado simples em memória (por número de telefone). Para produção real,
# trocar por uma base de dados — isto reinicia sempre que o processo reinicia.
sessoes = {}


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


def enviar_lista(destinatario, corpo, titulo_seccao, opcoes, botao="👉 Escolher"):
    """Envia uma lista interativa (até 10 opções) — o equivalente aos botões do Telegram.

    `opcoes` pode ser uma lista de strings simples (ex.: horários) ou de
    dicionários {"emoji", "titulo", "descricao"} (ex.: serviços), para dar
    um subtítulo a cada linha.
    """
    rows = []
    for i, opc in enumerate(opcoes):
        if isinstance(opc, dict):
            titulo = f"{opc['emoji']} {opc['titulo']}"[:24]
            row = {"id": f"opt_{i}", "title": titulo, "description": opc.get("descricao", "")[:72]}
        else:
            row = {"id": f"opt_{i}", "title": str(opc)[:24]}
        rows.append(row)

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


DIAS_SEMANA_PT = ["seg", "ter", "qua", "qui", "sex", "sáb", "dom"]


def proximos_dias(n=5):
    hoje = date.today()
    dias = []
    for i in range(1, n + 1):
        d = hoje + timedelta(days=i)
        dias.append(f"{d.strftime('%d.%m.%Y')} ({DIAS_SEMANA_PT[d.weekday()]})")
    return dias


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
        sessao = sessoes.setdefault(de, {})

        # Cliente escolheu algo numa lista
        if msg.get("type") == "interactive" and msg["interactive"]["type"] == "list_reply":
            id_escolhido = msg["interactive"]["list_reply"]["id"]
            titulo_bruto = msg["interactive"]["list_reply"]["title"]

            if "servico" not in sessao:
                servico = titulo_escolhido(SERVICOS, id_escolhido, titulo_bruto)
                sessao["servico"] = servico
                enviar_lista(
                    de,
                    f"Ótima escolha! ✅ *{servico}*\n\n📅 Para que dia gostaria de marcar?",
                    "Datas disponíveis",
                    proximos_dias(),
                    botao="📅 Escolher dia",
                )

            elif "data" not in sessao:
                sessao["data"] = titulo_bruto
                enviar_lista(
                    de,
                    f"Perfeito, dia *{titulo_bruto}* 👍\n\n⏰ A que horas lhe convém?",
                    "Horários disponíveis",
                    HORARIOS,
                    botao="⏰ Escolher hora",
                )

            elif "hora" not in sessao:
                sessao["hora"] = titulo_bruto
                resumo = (f"👤 Contacto: {de}\n🔧 Serviço: {sessao['servico']}\n"
                          f"📅 Data: {sessao['data']}\n⏰ Hora: {sessao['hora']}")

                enviar_texto(de, f"🎉 Obrigado! O seu pedido de marcação:\n\n{resumo}\n\n"
                                  f"✅ Vamos confirmar o mais depressa possível.\n"
                                  f"_{NOME_OFICINA} agradece a sua preferência!_ 🚗💨")

                if PROVIDER_WHATSAPP:
                    enviar_texto(PROVIDER_WHATSAPP,
                                 f"🆕📅 *Novo pedido de marcação através do bot:*\n\n{resumo}\n"
                                 f"💬 Cliente: wa.me/{de}")

                sessoes.pop(de, None)  # limpa para a próxima marcação

        # Qualquer mensagem de texto normal reinicia o fluxo
        elif msg.get("type") == "text":
            sessoes[de] = {}
            enviar_lista(
                de,
                f"👋 Olá! Bem-vindo(a) à *{NOME_OFICINA}* 🚗✨\n\nQual serviço gostaria de marcar?",
                "Os nossos serviços",
                SERVICOS,
                botao="🔧 Ver serviços",
            )

    except (KeyError, IndexError):
        pass  # notificações de status (entregue/lido) chegam neste mesmo endpoint — ignora-as

    return jsonify(status="ok"), 200


if __name__ == "__main__":
    # O Render (e serviços parecidos) define a porta via variável de ambiente PORT.
    # Localmente, sem essa variável, continua a usar 5000 como até agora.
    porta = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=porta, debug=True)
