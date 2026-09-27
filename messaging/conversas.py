"""
messaging/conversas.py — o TEXTO das mensagens, que até aqui se perdia todo.

Havia duas tabelas a tocar em mensagens e nenhuma a guardar o que foi dito:
`mensagens_processadas` tem o `wamid` e a hora (idempotência do webhook) e
`interacoes_cliente` tem a hora da última mensagem (janela de 24h da Meta).
O painel conseguia dizer que a Sofia pediu ajuda — não o que ela perguntou.
Nas primeiras semanas o bot vai falhar a perceber coisas e o HUMANO vai ser
muito usado; "alguém precisa de ti" sem a pergunta não serve para nada.

Dois pontos de escrita, os dois já existiam e são ÚNICOS:

  • entrada — o webhook (`bot.receber_mensagem`), logo depois de reclamar o
    `wamid`: assim grava-se mesmo quando o fluxo devolve cedo (seletor de
    idioma, comando de texto, sessão retomada).
  • saída  — `messaging/whatsapp.py:enviar()`, o ponto único por onde passa
    tudo (texto, botões, listas, documentos, templates e o composer do
    painel). Um único sítio em vez de centenas de call sites.

Três decisões que definem a utilidade disto:

  1. GRAVAR NUNCA PODE PARTIR NADA. Todas as escritas passam por `_gravar`,
     que engole qualquer exceção e deixa um aviso no log. Uma cliente nunca
     fica sem resposta porque um INSERT correu mal.

  2. O QUE SE GUARDA DE UMA MENSAGEM INTERATIVA é o que a pessoa VIU. Quando
     a cliente toca num botão, o webhook traz o id (`opt_1`) e o título
     ("Limpeza de pele"); `texto` fica com o título e o id vai para
     `id_interativo`, à parte. Guardar só o id daria um painel ilegível.

  3. NÚMEROS DEMO gravam-se como os outros. O envio real é bloqueado antes
     da Meta (`config.DEMO_PHONE_PREFIX`), mas o registo acontece — é isso
     que torna o fluxo inteiro testável sem mandar nada a sério.

O que NÃO se grava: mensagens para o número da própria Daniela
(`config.PROVIDER_WHATSAPP`). São avisos internos, não uma conversa com uma
cliente, e enchiam a lista com um "fio" que não é de ninguém.

RETENÇÃO: isto passa a ser conteúdo de conversas de pessoas reais numa base
de dados. `limpar_antigas` apaga o que passou de `config.CONVERSAS_RETENCAO_MESES`
(12 por omissão) e corre uma vez por dia dentro do executor de automações que
já existe (ver `limpar_antigas_se_devido` e notifications/jobs.py).
"""

from __future__ import annotations

import json
import logging

import config
import db
import tempo

log = logging.getLogger("conversas")

RECEBIDA = "recebida"
ENVIADA = "enviada"

# Tipos — o rótulo que a Daniela vê na conversa vem de TIPO_ROTULO, abaixo.
# Deliberadamente grosseiros: interessa distinguir "escreveu" de "tocou num
# botão" de "mandámos um template", não catalogar a API da Meta.
TIPO_TEXTO = "texto"
TIPO_BOTAO = "botao"
TIPO_LISTA = "lista"
TIPO_TEMPLATE = "template"
TIPO_DOCUMENTO = "documento"
TIPO_IMAGEM = "imagem"
TIPO_OUTRO = "outro"

TIPO_ROTULO = {
    TIPO_TEXTO: "Texto",
    TIPO_BOTAO: "Botão",
    TIPO_LISTA: "Lista",
    TIPO_TEMPLATE: "Template",
    TIPO_DOCUMENTO: "Documento",
    TIPO_IMAGEM: "Imagem",
    TIPO_OUTRO: "Outro",
}

# Tipos de mensagem de ENTRADA que a Meta manda e para os quais não há texto
# nenhum — guarda-se uma descrição em vez de uma linha vazia, senão a
# conversa no painel tem buracos sem explicação.
_ENTRADA_SEM_TEXTO = {
    "image": (TIPO_IMAGEM, "[imagem]"),
    "audio": (TIPO_OUTRO, "[mensagem de voz]"),
    "video": (TIPO_OUTRO, "[vídeo]"),
    "document": (TIPO_DOCUMENTO, "[documento]"),
    "sticker": (TIPO_OUTRO, "[sticker]"),
    "location": (TIPO_OUTRO, "[localização]"),
    "contacts": (TIPO_OUTRO, "[contacto]"),
}

# Teto do texto guardado. Uma mensagem de WhatsApp vai até 4096 caracteres;
# 4000 chega para tudo o que é real e impede que um payload esquisito faça
# crescer a tabela sem limite.
MAX_TEXTO = 4000


# ---------------------------------------------------------------------------
# Escrita
# ---------------------------------------------------------------------------
def _numero_da_equipa(telefone) -> bool:
    equipa = (config.PROVIDER_WHATSAPP or "").lstrip("+")
    return bool(equipa) and str(telefone or "").lstrip("+") == equipa


def _gravar(direcao: str, telefone: str, tipo: str, texto: str | None,
            id_interativo: str | None, wamid: str | None, tenant_id: int) -> None:
    """Insere UMA linha. Nunca levanta: a gravação é um extra sobre o que
    interessa (responder à cliente), e uma falha aqui não pode cancelar
    isso. Fica um aviso no log — e, se isto começar a falhar sempre, a
    conversa no painel fica vazia, que é visível."""
    if not telefone or _numero_da_equipa(telefone):
        return
    try:
        telefone = str(telefone).lstrip("+")
        with db.ligacao() as c:
            customer_id = c.execute(
                "SELECT id FROM customers WHERE tenant_id = ? AND phone = ?",
                (tenant_id, telefone)).fetchone()
            c.execute(
                "INSERT INTO mensagens_conversa (tenant_id, telefone, customer_id, direcao, "
                "tipo, texto, id_interativo, wamid, criado_em) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (tenant_id, telefone, customer_id[0] if customer_id else None, direcao,
                 tipo, (texto or "")[:MAX_TEXTO] or None, id_interativo, wamid, tempo.iso_utc()))
    except Exception:                       # noqa: BLE001 — ver docstring
        log.warning("não foi possível gravar a mensagem %s de %s (o envio segue)",
                    direcao, telefone, exc_info=True)


def registar_recebida(msg: dict, tenant_id: int = 1) -> None:
    """Grava a mensagem tal como chegou no webhook. Recebe o dicionário
    `messages[0]` da Meta em bruto — a leitura dos vários formatos vive em
    `ler_entrada`, para o webhook não ter de saber nada disto."""
    lida = ler_entrada(msg)
    if lida is None:
        return
    _gravar(RECEBIDA, msg.get("from"), lida["tipo"], lida["texto"],
            lida["id_interativo"], msg.get("id"), tenant_id)


def registar_enviada(payload: dict, resposta=None, tenant_id: int = 1) -> None:
    """Grava o que saiu. Recebe o payload da Graph API (o mesmo que
    `messaging/whatsapp.py:enviar` acabou de mandar) e, quando existe, a
    resposta — é dela que vem o `wamid` do que enviámos."""
    lida = ler_saida(payload)
    if lida is None:
        return
    _gravar(ENVIADA, payload.get("to"), lida["tipo"], lida["texto"],
            lida["id_interativo"], _wamid_da_resposta(resposta), tenant_id)


def _wamid_da_resposta(resposta) -> str | None:
    """`messages[0].id` da resposta da Graph API. Melhor esforço: a resposta
    pode ser None (WhatsApp não configurado) ou sintética (destinatário DEMO)."""
    if resposta is None:
        return None
    try:
        corpo = resposta.json() or {}
        return (corpo.get("messages") or [{}])[0].get("id")
    except Exception:                       # noqa: BLE001
        return None


# ---------------------------------------------------------------------------
# Leitura dos formatos da Meta — só interpretação, sem efeitos
# ---------------------------------------------------------------------------
def ler_entrada(msg: dict) -> dict | None:
    """{tipo, texto, id_interativo} a partir de `messages[0]` do webhook.

    O título do botão/linha vem SEMPRE no próprio webhook (`button_reply.title`,
    `list_reply.title`, `button.text`) — por isso não é preciso um registo de
    ids nossos para tornar isto legível: a Meta devolve o que a cliente viu.
    """
    if not isinstance(msg, dict):
        return None
    tipo_meta = msg.get("type")

    if tipo_meta == "text":
        return {"tipo": TIPO_TEXTO, "texto": (msg.get("text") or {}).get("body") or "",
                "id_interativo": None}

    if tipo_meta == "interactive":
        interativo = msg.get("interactive") or {}
        if interativo.get("type") == "button_reply":
            resposta = interativo.get("button_reply") or {}
            return {"tipo": TIPO_BOTAO, "texto": resposta.get("title") or resposta.get("id") or "",
                    "id_interativo": resposta.get("id")}
        if interativo.get("type") == "list_reply":
            resposta = interativo.get("list_reply") or {}
            # A descrição da linha é o que distingue duas opções com o mesmo
            # título (ex.: dois horários do mesmo serviço) — guarda-se também.
            titulo = resposta.get("title") or resposta.get("id") or ""
            descricao = resposta.get("description") or ""
            return {"tipo": TIPO_LISTA, "texto": f"{titulo} — {descricao}" if descricao else titulo,
                    "id_interativo": resposta.get("id")}
        return {"tipo": TIPO_OUTRO, "texto": "[mensagem interativa]", "id_interativo": None}

    if tipo_meta == "button":
        # Toque num "quick reply" de um TEMPLATE: formato diferente do
        # "interactive" das nossas próprias mensagens (ver whatsapp.enviar_template).
        botao = msg.get("button") or {}
        return {"tipo": TIPO_BOTAO, "texto": botao.get("text") or botao.get("payload") or "",
                "id_interativo": botao.get("payload")}

    if tipo_meta in _ENTRADA_SEM_TEXTO:
        tipo, marcador = _ENTRADA_SEM_TEXTO[tipo_meta]
        legenda = (msg.get(tipo_meta) or {}).get("caption") if isinstance(msg.get(tipo_meta), dict) else None
        return {"tipo": tipo, "texto": f"{marcador} {legenda}".strip() if legenda else marcador,
                "id_interativo": None}

    # Um tipo novo da Meta (ou um "system"/"reaction") continua a ficar
    # registado: melhor uma linha a dizer o tipo do que um buraco na conversa.
    return {"tipo": TIPO_OUTRO, "texto": f"[{tipo_meta or 'desconhecido'}]", "id_interativo": None}


def ler_saida(payload: dict) -> dict | None:
    """{tipo, texto, id_interativo} a partir do payload enviado à Graph API.

    Numa mensagem interativa guarda-se o corpo MAIS as opções apresentadas:
    sem elas, a conversa mostrava "Qual o serviço?" e depois a resposta da
    cliente sem que se visse entre o que ela estava a escolher."""
    if not isinstance(payload, dict):
        return None
    tipo_meta = payload.get("type")

    if tipo_meta == "text":
        return {"tipo": TIPO_TEXTO, "texto": (payload.get("text") or {}).get("body") or "",
                "id_interativo": None}

    if tipo_meta == "interactive":
        interativo = payload.get("interactive") or {}
        corpo = ((interativo.get("body") or {}).get("text") or "").strip()
        acao = interativo.get("action") or {}
        opcoes = [((b.get("reply") or {}).get("title") or "") for b in (acao.get("buttons") or [])]
        e_lista = False
        for seccao in (acao.get("sections") or []):
            e_lista = True
            opcoes += [(r.get("title") or "") for r in (seccao.get("rows") or [])]
        opcoes = [o for o in opcoes if o]
        return {"tipo": TIPO_LISTA if e_lista else TIPO_BOTAO,
                "texto": f"{corpo}\n[{' · '.join(opcoes)}]" if opcoes else corpo,
                "id_interativo": None}

    if tipo_meta == "template":
        template = payload.get("template") or {}
        # O conteúdo do template vive na Meta, não aqui — guarda-se o NOME e
        # os parâmetros, que é o que permite reconstituir o que foi dito.
        parametros = []
        for componente in (template.get("components") or []):
            if componente.get("type") == "body":
                parametros = [p.get("text") or "" for p in (componente.get("parameters") or [])]
        nome = template.get("name") or "?"
        return {"tipo": TIPO_TEMPLATE,
                "texto": f"[template {nome}] {' · '.join(parametros)}".strip(),
                "id_interativo": nome}

    if tipo_meta == "document":
        documento = payload.get("document") or {}
        nome = documento.get("filename") or documento.get("link") or ""
        legenda = documento.get("caption") or ""
        return {"tipo": TIPO_DOCUMENTO, "texto": f"[documento {nome}] {legenda}".strip(),
                "id_interativo": None}

    return {"tipo": TIPO_OUTRO, "texto": f"[{tipo_meta or 'desconhecido'}]", "id_interativo": None}


# ---------------------------------------------------------------------------
# Leitura para o painel
# ---------------------------------------------------------------------------
_CAMPOS = ("id", "telefone", "customer_id", "direcao", "tipo", "texto",
           "id_interativo", "wamid", "criado_em")
_SQL_CAMPOS = ", ".join(_CAMPOS)


def _linha(row) -> dict:
    d = dict(zip(_CAMPOS, row))
    d["tipo_rotulo"] = TIPO_ROTULO.get(d["tipo"], d["tipo"])
    return d


def listar_conversas(tenant_id: int = 1, limite: int = 300) -> list[dict]:
    """Uma linha por telefone, mais recente primeiro: nome, última mensagem
    e quando foi. Quem tem um pedido de HUMANO por responder vem em cima
    (`pedido_humano` preenchido) — é essa a ordenação que interessa quando ela
    abre o painel entre duas clientes."""
    pedidos = pedidos_humanos_abertos(tenant_id)
    nomes_sessao = _nomes_das_sessoes(tenant_id)
    with db.ligacao() as c:
        # MAX(id) e não MAX(criado_em): o id é monotónico na inserção e duas
        # mensagens no mesmo segundo têm o mesmo carimbo de tempo.
        linhas = c.execute(
            "SELECT m.telefone, m.direcao, m.tipo, m.texto, m.criado_em, m.customer_id, "
            "       cu.name, t.total "
            "FROM mensagens_conversa m "
            "JOIN (SELECT telefone, MAX(id) AS ultimo, COUNT(*) AS total "
            "        FROM mensagens_conversa WHERE tenant_id = ? GROUP BY telefone) t "
            "  ON t.ultimo = m.id "
            "LEFT JOIN customers cu ON cu.tenant_id = m.tenant_id AND cu.phone = m.telefone "
            "ORDER BY m.criado_em DESC, m.id DESC LIMIT ?", (tenant_id, limite)).fetchall()
    conversas = []
    for (telefone, direcao, tipo, texto, criado_em, customer_id, nome, total) in linhas:
        conversas.append({
            "telefone": telefone,
            "customer_id": customer_id,
            "nome": nome or nomes_sessao.get(telefone) or "",
            "ultima_direcao": direcao,
            "ultimo_tipo": tipo,
            "ultimo_texto": texto or "",
            "ultima_em": criado_em,
            "total": total,
            "pedido_humano": pedidos.get(telefone),
        })
    # Pedido de HUMANO por responder sobe ao topo. `sort` é estável, por isso
    # dentro de cada grupo mantém-se a recência que o SQL já deu.
    conversas.sort(key=lambda x: 0 if x["pedido_humano"] else 1)
    return conversas


def listar_mensagens(telefone: str, tenant_id: int = 1, limite: int = 300) -> list[dict]:
    """O fio de uma conversa, por ordem cronológica. `limite` corta as MAIS
    ANTIGAS — o que interessa numa conversa é sempre o fim."""
    telefone = str(telefone).lstrip("+")
    with db.ligacao() as c:
        linhas = c.execute(
            f"SELECT {_SQL_CAMPOS} FROM mensagens_conversa WHERE tenant_id = ? AND telefone = ? "
            "ORDER BY criado_em DESC, id DESC LIMIT ?", (tenant_id, telefone, limite)).fetchall()
    return [_linha(r) for r in reversed(linhas)]


def ultima_recebida(telefone: str, tenant_id: int = 1) -> dict | None:
    """A última coisa que a cliente escreveu. É isto que vai no aviso por
    WhatsApp e no Attention Center — "alguém precisa de ti" sem a pergunta
    não serve para nada."""
    telefone = str(telefone).lstrip("+")
    with db.ligacao() as c:
        linha = c.execute(
            f"SELECT {_SQL_CAMPOS} FROM mensagens_conversa WHERE tenant_id = ? AND telefone = ? "
            "AND direcao = ? ORDER BY criado_em DESC, id DESC LIMIT 1",
            (tenant_id, telefone, RECEBIDA)).fetchone()
    return _linha(linha) if linha else None


def ultima_pergunta(telefone: str, tenant_id: int = 1, ignorar=()) -> str:
    """A última coisa que a cliente ESCREVEU e que é uma pergunta.

    Não é o mesmo que `ultima_recebida`: quando ela escreve "humano", a
    última mensagem recebida é literalmente "humano" — e citar isso no aviso
    à Daniela («humano») não diz nada a ninguém. A pergunta é a mensagem de
    texto anterior. `ignorar` traz os comandos permanentes e os ids dos
    botões de "falar com a equipa" (quem os conhece é o bot, não este
    módulo).

    Só TEXTO livre: um percurso feito todo a botões não tem pergunta
    nenhuma, e é mais honesto não citar nada do que citar "Menu principal".
    Devolve "" nesse caso — quem chama omite a citação.
    """
    telefone = str(telefone).lstrip("+")
    proibidos = {str(i).strip().lower() for i in ignorar}
    with db.ligacao() as c:
        linhas = c.execute(
            f"SELECT {_SQL_CAMPOS} FROM mensagens_conversa WHERE tenant_id = ? AND telefone = ? "
            "AND direcao = ? AND tipo = ? ORDER BY criado_em DESC, id DESC LIMIT 20",
            (tenant_id, telefone, RECEBIDA, TIPO_TEXTO)).fetchall()
    for linha in linhas:
        m = _linha(linha)
        texto = (m["texto"] or "").strip()
        if not texto or texto.lower() in proibidos:
            continue
        if m["id_interativo"] and str(m["id_interativo"]).lower() in proibidos:
            continue
        return texto
    return ""


def _nomes_das_sessoes(tenant_id: int = 1) -> dict[str, str]:
    """Nome do perfil de WhatsApp, para quem ainda não tem ficha de cliente
    (uma ficha só nasce na primeira marcação — ver db.obter_ou_criar_customer)."""
    nomes = {}
    with db.ligacao() as c:
        for (telefone, dados) in c.execute(
                "SELECT telefone, dados FROM sessoes WHERE tenant_id = ?", (tenant_id,)).fetchall():
            try:
                nome = (json.loads(dados or "{}") or {}).get("nome")
            except (ValueError, TypeError):
                continue
            if nome:
                nomes[telefone] = nome
    return nomes


# ---------------------------------------------------------------------------
# Pedidos de HUMANO
# ---------------------------------------------------------------------------
# A marca vive na SESSÃO (`needs_human`) e não numa tabela nova porque é ali
# que `operations/engine.py:attention_items` já a procurava — faltava era
# alguém a escrevê-la (`falar_com_equipa` nunca a punha, e por isso o pedido
# nunca chegava ao painel). Guarda-se também a hora, para o painel poder
# dizer "há 20 minutos" em vez de só "pediu ajuda".
CHAVE_PEDIDO = "needs_human"
CHAVE_PEDIDO_EM = "needs_human_em"
# A pergunta fica GRAVADA no momento do pedido, e não é recalculada depois:
# assim o aviso por WhatsApp e o cartão do painel citam exactamente a mesma
# frase, mesmo que a cliente continue a escrever enquanto espera.
CHAVE_PEDIDO_TEXTO = "needs_human_texto"
CHAVES_PEDIDO = (CHAVE_PEDIDO, CHAVE_PEDIDO_EM, CHAVE_PEDIDO_TEXTO)


def _sessao(c, telefone: str, tenant_id: int) -> dict:
    linha = c.execute("SELECT dados FROM sessoes WHERE tenant_id = ? AND telefone = ?",
                      (tenant_id, telefone)).fetchone()
    if not linha:
        return {}
    try:
        return json.loads(linha[0] or "{}") or {}
    except (ValueError, TypeError):
        return {}


def marcar_pedido_humano(telefone: str, pergunta: str = "", tenant_id: int = 1) -> None:
    telefone = str(telefone).lstrip("+")
    try:
        with db.ligacao() as c:
            sessao = _sessao(c, telefone, tenant_id)
            sessao[CHAVE_PEDIDO] = True
            sessao[CHAVE_PEDIDO_EM] = tempo.iso_utc()
            sessao[CHAVE_PEDIDO_TEXTO] = (pergunta or "")[:MAX_TEXTO]
            c.execute(
                "INSERT INTO sessoes (tenant_id, telefone, dados) VALUES (?, ?, ?) "
                "ON CONFLICT(tenant_id, telefone) DO UPDATE SET dados = excluded.dados",
                (tenant_id, telefone, json.dumps(sessao)))
    except Exception:                       # noqa: BLE001 — nunca parte o fluxo
        log.warning("não foi possível marcar o pedido de humano de %s", telefone, exc_info=True)


def fechar_pedido_humano(telefone: str, tenant_id: int = 1) -> None:
    """Chamado quando ela RESPONDE pelo painel. As chaves são REMOVIDAS, não
    postas a falso: a query do Attention Center procura o nome da chave no
    JSON, e um `"needs_human": false` continuaria a acusar um pedido aberto."""
    telefone = str(telefone).lstrip("+")
    try:
        with db.ligacao() as c:
            sessao = _sessao(c, telefone, tenant_id)
            if not any(k in sessao for k in CHAVES_PEDIDO):
                return
            for chave in CHAVES_PEDIDO:
                sessao.pop(chave, None)
            c.execute("UPDATE sessoes SET dados = ? WHERE tenant_id = ? AND telefone = ?",
                      (json.dumps(sessao), tenant_id, telefone))
    except Exception:                       # noqa: BLE001
        log.warning("não foi possível fechar o pedido de humano de %s", telefone, exc_info=True)


def pedidos_humanos_abertos(tenant_id: int = 1) -> dict[str, dict]:
    """{telefone: {pedido_em, texto, nome}} — um por cliente à espera."""
    abertos: dict[str, dict] = {}
    with db.ligacao() as c:
        linhas = c.execute(
            "SELECT telefone, dados FROM sessoes WHERE tenant_id = ? AND dados LIKE ?",
            (tenant_id, f'%"{CHAVE_PEDIDO}"%')).fetchall()
    for (telefone, dados) in linhas:
        try:
            sessao = json.loads(dados or "{}") or {}
        except (ValueError, TypeError):
            continue
        if not sessao.get(CHAVE_PEDIDO):
            continue
        abertos[telefone] = {
            "telefone": telefone,
            "nome": sessao.get("nome") or "",
            "pedido_em": sessao.get(CHAVE_PEDIDO_EM),
            "texto": sessao.get(CHAVE_PEDIDO_TEXTO) or "",
        }
    # Sem nome na sessão (sessão nova, ou retomada de um número que já é
    # cliente) vai-se buscar o da ficha: o cartão do painel diz "a Sofia
    # pediu para falar contigo", não "uma cliente".
    sem_nome = [t for (t, p) in abertos.items() if not p["nome"]]
    if sem_nome:
        with db.ligacao() as c:
            marcas = ", ".join("?" for _ in sem_nome)
            for (telefone, nome) in c.execute(
                    f"SELECT phone, name FROM customers WHERE tenant_id = ? "
                    f"AND phone IN ({marcas})", [tenant_id, *sem_nome]).fetchall():
                if nome:
                    abertos[telefone]["nome"] = nome
    return abertos


# ---------------------------------------------------------------------------
# Janela de 24h — o prazo, não só o sim/não
# ---------------------------------------------------------------------------
JANELA_HORAS = 24


def estado_janela_24h(telefone: str, tenant_id: int = 1, agora=None) -> dict:
    """{aberta, ultima_em, expira_em, minutos_restantes}.

    `bot.dentro_da_janela_24h` só devolvia sim/não, e por isso o painel só
    podia descobrir que a janela tinha fechado DEPOIS de ela escrever uma
    resposta longa e levar com um 409. Expor QUANDO expira é a diferença
    entre uma ferramenta e uma armadilha."""
    from datetime import timedelta
    telefone = str(telefone).lstrip("+")
    agora = agora or tempo.agora_utc()
    with db.ligacao() as c:
        linha = c.execute(
            "SELECT ultima_mensagem_em FROM interacoes_cliente WHERE tenant_id = ? AND telefone = ?",
            (tenant_id, telefone)).fetchone()
    ultima = tempo.parse_iso(linha[0]) if linha and linha[0] else None
    if ultima is None:
        return {"aberta": False, "ultima_em": None, "expira_em": None, "minutos_restantes": None}
    expira = ultima + timedelta(hours=JANELA_HORAS)
    restantes = int((expira - agora).total_seconds() // 60)
    return {"aberta": restantes > 0, "ultima_em": tempo.iso_utc(ultima),
            "expira_em": tempo.iso_utc(expira), "minutos_restantes": max(restantes, 0)}


# ---------------------------------------------------------------------------
# Retenção
# ---------------------------------------------------------------------------
# Chave do pulso que serve de acelerador: o executor de automações corre de
# 5 em 5 minutos e não faz sentido varrer a tabela 288 vezes por dia.
CHAVE_LIMPEZA = "conversas.limpeza"
INTERVALO_LIMPEZA_MIN = 24 * 60


def limpar_antigas(meses: int | None = None, tenant_id: int | None = None, agora=None) -> int:
    """Apaga as mensagens com mais de `meses` meses e devolve quantas foram.

    `meses = 0` desliga a limpeza (devolve 0 sem apagar nada) — é a saída
    para quem, mais tarde, tiver uma obrigação legal de guardar mais tempo.
    Sem `tenant_id` limpa todos: a retenção é uma regra da base de dados
    inteira, não de um tenant."""
    from datetime import timedelta
    meses = config.CONVERSAS_RETENCAO_MESES if meses is None else meses
    if meses <= 0:
        return 0
    # 30 dias por mês, de propósito: um prazo de retenção não precisa de
    # aritmética de calendário, e "mais de 12 meses" com 365 dias ou com 360
    # é a mesma decisão de negócio.
    limite = tempo.iso_utc((agora or tempo.agora_utc()) - timedelta(days=30 * meses))
    with db.ligacao() as c:
        if tenant_id is None:
            cur = c.execute("DELETE FROM mensagens_conversa WHERE criado_em < ?", (limite,))
        else:
            cur = c.execute("DELETE FROM mensagens_conversa WHERE tenant_id = ? AND criado_em < ?",
                            (tenant_id, limite))
        return cur.rowcount or 0


def limpar_antigas_se_devido(tenant_id: int = 1) -> int:
    """Uma vez por dia, a partir do executor de automações. Nunca levanta:
    uma limpeza falhada não pode fazer os lembretes do dia falharem."""
    try:
        from core import health
        parado_ha = health.minutos_sem_pulso(CHAVE_LIMPEZA, tenant_id)
        if parado_ha is not None and parado_ha < INTERVALO_LIMPEZA_MIN:
            return 0
        apagadas = limpar_antigas()
        health.pulso(CHAVE_LIMPEZA, tenant_id)
        if apagadas:
            log.info("retenção de conversas: %s mensagens apagadas", apagadas)
        return apagadas
    except Exception:                       # noqa: BLE001
        log.warning("falhou a limpeza de conversas antigas", exc_info=True)
        return 0


# ---------------------------------------------------------------------------
# Seed DEMO
# ---------------------------------------------------------------------------
# Vive aqui, ao lado da tabela, e não no bot.py — mesmo sítio e mesma forma
# de `campaigns.engine.seed_demo`. Serve para ver a vista Conversas nos três
# estados que importam (fio recém-começado, histórico longo, janela de 24h
# fechada) sem falar com a Meta nem tocar numa cliente real: só aceita
# números com o prefixo DEMO.
_SEED_FIO_LONGO = [
    (RECEBIDA, TIPO_TEXTO, "Boa tarde! Queria marcar uma limpeza de pele", None),
    (ENVIADA, TIPO_TEXTO, "Olá! Claro 😊 Qual o seu nome?", None),
    (RECEBIDA, TIPO_TEXTO, "Sofia Costa", None),
    (ENVIADA, TIPO_LISTA, "Escolha o serviço:\n[Limpeza de pele · Massagem · Manicure]", None),
    (RECEBIDA, TIPO_LISTA, "Limpeza de pele — 60 min", "servico_limpeza_pele"),
    (ENVIADA, TIPO_BOTAO, "Temos estes horários para quinta:\n[09:00 · 11:30 · 15:00]", None),
    (RECEBIDA, TIPO_BOTAO, "11:30", "slot_1130"),
    (ENVIADA, TIPO_TEXTO, "Marcado! Limpeza de pele, quinta às 11:30. Até já ✨", None),
    (RECEBIDA, TIPO_TEXTO, "Perfeito, obrigada!", None),
    (ENVIADA, TIPO_TEMPLATE, "[template lembrete_24h] Limpeza de pele | quinta | 11:30", "lembrete_24h"),
    (RECEBIDA, TIPO_BOTAO, "Confirmo", "confirmar_marcacao"),
    (ENVIADA, TIPO_TEXTO, "Obrigada pela confirmação 💛", None),
    (RECEBIDA, TIPO_IMAGEM, "[imagem] é assim que a pele está desde ontem", None),
    (RECEBIDA, TIPO_TEXTO, "A minha pele está a reagir mal ao creme, o que faço?", None),
    (ENVIADA, TIPO_TEXTO, "Vou passar a mensagem à Daniela — respondemos já 💛", None),
]

_SEED_FIO_FECHADO = [
    (RECEBIDA, TIPO_TEXTO, "Olá, ainda têm vaga para sábado?", None),
    (ENVIADA, TIPO_BOTAO, "Sábado temos:\n[10:00 · 14:00]", None),
    (RECEBIDA, TIPO_BOTAO, "14:00", "slot_1400"),
    (ENVIADA, TIPO_TEXTO, "Marcado para sábado às 14:00 ✨", None),
]

_SEED_FIO_CURTO = [
    (RECEBIDA, TIPO_TEXTO, "Olá 👋", None),
]

# Fios curtos só para a LISTA ter a densidade de um dia normal: com três
# linhas não se percebe o que ela vai ver quando o link estiver na bio do
# Instagram. Cada um leva uma idade diferente, para haver "agora", "hoje" e
# "há dias" na coluna da direita.
_SEED_FIOS_EXTRA = [
    (45, [(RECEBIDA, TIPO_TEXTO, "Bom dia, trabalham ao sábado?", None),
          (ENVIADA, TIPO_TEXTO, "Bom dia! Sim, das 9h às 16h 💛", None)]),
    (3 * 60, [(RECEBIDA, TIPO_BOTAO, "Ver serviços", "mp_servicos"),
              (ENVIADA, TIPO_LISTA, "Os nossos serviços:\n[Limpeza de pele · Massagem · Manicure]", None)]),
    (9 * 60, [(RECEBIDA, TIPO_TEXTO, "Quanto custa a massagem de 60 min?", None),
              (ENVIADA, TIPO_TEXTO, "São CHF 120. Quer marcar? ✨", None),
              (RECEBIDA, TIPO_TEXTO, "Depois digo, obrigada!", None)]),
    (30 * 60, [(RECEBIDA, TIPO_TEXTO, "Preciso de desmarcar a de amanhã", None),
               (ENVIADA, TIPO_TEXTO, "Sem problema, já está desmarcada.", None)]),
    (52 * 60, [(RECEBIDA, TIPO_IMAGEM, "[imagem] ficou tão bem 😍", None),
               (ENVIADA, TIPO_TEXTO, "Que bom ler isso! Obrigada 💛", None)]),
]


def seed_demo(tenant_id: int = 1, telefones=()) -> dict:
    """Semeia três fios DEMO. Devolve {telefone: nº de mensagens}.

    O tempo é forçado à mão (`criado_em` e `interacoes_cliente`) porque é o
    que define os estados: o fio longo fica dentro da janela de 24h, o
    segundo fora dela (última mensagem da cliente há 40 horas)."""
    from datetime import timedelta
    demo = [str(t).lstrip("+") for t in telefones
            if str(t).lstrip("+").startswith(config.DEMO_PHONE_PREFIX)]
    if len(demo) < 3:
        return {}
    agora = tempo.agora_utc()
    guiao = [
        # (telefone, mensagens, minutos desde a última mensagem, minutos
        #  entre mensagens) — o fio longo espalha-se por vários dias, que é
        #  o que faz aparecer os separadores de dia na vista.
        (demo[0], _SEED_FIO_LONGO, 12, 190),
        (demo[1], _SEED_FIO_FECHADO, 40 * 60, 7),
        (demo[2], _SEED_FIO_CURTO, 3, 7),
    ]
    for (i, (idade_min, mensagens)) in enumerate(_SEED_FIOS_EXTRA):
        if len(demo) > 3 + i:
            guiao.append((demo[3 + i], mensagens, idade_min, 7))
    contagem: dict[str, int] = {}
    with db.ligacao() as c:
        for (telefone, mensagens, idade_min, passo_min) in guiao:
            if c.execute("SELECT 1 FROM mensagens_conversa WHERE tenant_id = ? AND telefone = ?",
                         (tenant_id, telefone)).fetchone():
                continue
            linha = c.execute("SELECT id FROM customers WHERE tenant_id = ? AND phone = ?",
                              (tenant_id, telefone)).fetchone()
            customer_id = linha[0] if linha else None
            # A conversa desenrola-se do fim para trás, `passo_min`
            # minutos entre mensagens — sem aritmética de calendário.
            ultima_recebida_em = None
            total = len(mensagens)
            for (i, (direcao, tipo, texto, id_interativo)) in enumerate(mensagens):
                quando = agora - timedelta(minutes=idade_min + (total - 1 - i) * passo_min)
                c.execute(
                    "INSERT INTO mensagens_conversa (tenant_id, telefone, customer_id, direcao, "
                    "tipo, texto, id_interativo, wamid, criado_em) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (tenant_id, telefone, customer_id, direcao, tipo, texto, id_interativo,
                     f"wamid.demo.{telefone}.{i}", tempo.iso_utc(quando)))
                if direcao == RECEBIDA:
                    ultima_recebida_em = quando
            if ultima_recebida_em is not None:
                c.execute(
                    "INSERT INTO interacoes_cliente (tenant_id, telefone, ultima_mensagem_em) "
                    "VALUES (?, ?, ?) ON CONFLICT(tenant_id, telefone) DO UPDATE SET "
                    "ultima_mensagem_em = excluded.ultima_mensagem_em",
                    (tenant_id, telefone, tempo.iso_utc(ultima_recebida_em)))
            contagem[telefone] = total
    # O fio longo acaba num pedido de ajuda ainda aberto — é o cartão do
    # Attention Center e a linha que sobe ao topo da lista.
    if demo[0] in contagem:
        marcar_pedido_humano(demo[0], "A minha pele está a reagir mal ao creme, o que faço?", tenant_id)
    return contagem
