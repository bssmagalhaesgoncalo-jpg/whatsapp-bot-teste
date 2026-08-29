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
    conn.execute(
        "CREATE TABLE IF NOT EXISTS agendamentos ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "telefone TEXT NOT NULL, "
        "nome TEXT, "
        "servico TEXT NOT NULL, "
        "data TEXT NOT NULL, "
        "hora TEXT NOT NULL, "
        "criado_em TEXT NOT NULL)"
    )
    return conn


def guardar_agendamento(telefone, sessao):
    """Grava uma marcação confirmada, para aparecer no dashboard/agenda."""
    from datetime import datetime
    with obter_bd() as conn:
        conn.execute(
            "INSERT INTO agendamentos (telefone, nome, servico, data, hora, criado_em) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                telefone,
                sessao.get("nome"),
                sessao.get("servico"),
                sessao.get("data"),
                sessao.get("hora"),
                datetime.utcnow().isoformat(),
            ),
        )


def listar_agendamentos():
    with obter_bd() as conn:
        linhas = conn.execute(
            "SELECT id, telefone, nome, servico, data, hora, criado_em "
            "FROM agendamentos ORDER BY id DESC"
        ).fetchall()
    return [
        {
            "id": l[0], "telefone": l[1], "nome": l[2] or l[1],
            "servico": l[3], "data": l[4], "hora": l[5], "criado_em": l[6],
        }
        for l in linhas
    ]


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
@app.route("/api/agendamentos", methods=["GET"])
def api_agendamentos():
    """Devolve todas as marcações confirmadas, em JSON, para o dashboard."""
    return jsonify(listar_agendamentos()), 200


@app.route("/dashboard", methods=["GET"])
def dashboard():
    """Painel simples que mostra as marcações recebidas via WhatsApp."""
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
    --gold:#e8b923; --text:#f2f3f5; --muted:#9aa1ac; --green:#2ecc71;
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
    <div class="card"><div class="n" id="st-servico">-</div><div class="l">Serviço mais pedido</div></div>
  </div>

  <div class="lista">
    <h2>Próximos agendamentos</h2>
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

  const contagem = {};
  dados.forEach(d => { contagem[d.servico] = (contagem[d.servico]||0) + 1; });
  const topServico = Object.entries(contagem).sort((a,b)=>b[1]-a[1])[0];
  document.getElementById('st-servico').textContent = topServico ? topServico[0] : '-';

  const cont = document.getElementById('conteudo');
  if(dados.length === 0){
    cont.innerHTML = '<div class="vazio">Ainda não há marcações. Manda uma mensagem ao bot no WhatsApp para testar 👋</div>';
    return;
  }

  let html = '<table><thead><tr><th>Cliente</th><th>Serviço</th><th>Data</th><th>Hora</th><th>Recebido em</th></tr></thead><tbody>';
  dados.forEach(d => {
    const criado = d.criado_em ? new Date(d.criado_em).toLocaleString('pt-PT') : '-';
    html += `<tr>
      <td>${d.nome || d.telefone}<br><span style="color:var(--muted);font-size:12px;">${d.telefone}</span></td>
      <td><span class="tag">${d.servico}</span></td>
      <td>${d.data}</td>
      <td>${d.hora}</td>
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

                guardar_agendamento(de, sessao)
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
