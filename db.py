"""
Camada de base de dados — ligação + MIGRAÇÕES versionadas.

O que muda face à versão antiga:
  • A abertura de uma ligação (`ligacao()`) já NÃO cria tabelas nem corre
    ALTER TABLE. Só abre, aplica PRAGMAs de segurança/concorrência, e FECHA
    a ligação no fim (a versão antiga nunca fechava — fuga de ligações).
  • O schema é construído por uma lista ORDENADA de migrações, cada uma com
    um número. Uma tabela `schema_migrations` regista o que já foi aplicado.
    `migrar()` corre só as que faltam e é chamado UMA vez no arranque.
  • Sem `ALTER TABLE` aleatório a cada request "para sempre".

PostgreSQL (produção): a estrutura está preparada (ver `MIGRATION.md`), mas a
troca efetiva de SQLite -> Postgres é um passo SEPARADO, feito antes do deploy,
para não misturar uma migração de dados com as alterações de funcionalidade
desta fase. Se `DATABASE_URL` apontar para Postgres, este módulo aborta com
uma mensagem explícita em vez de correr meio configurado.
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager

import config
import catalogo
import parsing
from tempo import iso_utc

_LOCK = threading.Lock()
_MIGRADO = False


def _assert_backend_suportado():
    if config.usa_postgres():
        raise RuntimeError(
            "DATABASE_URL aponta para PostgreSQL, mas a migração SQLite->Postgres "
            "ainda não foi executada. Ver MIGRATION.md. Para desenvolvimento local, "
            "deixa DATABASE_URL vazio (usa SQLite em %s)." % config.SQLITE_PATH
        )


def _conectar() -> sqlite3.connect:
    _assert_backend_suportado()
    conn = sqlite3.connect(config.SQLITE_PATH, timeout=15)
    # Concorrência: WAL deixa leitores e um escritor coexistir; busy_timeout
    # faz um segundo escritor ESPERAR (até 15s) em vez de rebentar com
    # "database is locked". A serialização real das marcações continua a vir
    # do BEGIN IMMEDIATE em bot.py.
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 15000")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA foreign_keys = OFF")  # schema legado não impõe FKs
    return conn


@contextmanager
def ligacao():
    """Context manager: entrega uma ligação, faz commit no fim (ou rollback
    se rebentar) e FECHA sempre. Substitui o antigo `with obter_bd() as conn`
    sem mudar a semântica de quem chama."""
    garantir_migracoes()
    conn = _conectar()
    try:
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Utilidades de migração
# ---------------------------------------------------------------------------
def _coluna_existe(conn, tabela, coluna) -> bool:
    linhas = conn.execute(f"PRAGMA table_info({tabela})").fetchall()
    return any(l[1] == coluna for l in linhas)


def _tabela_existe(conn, tabela) -> bool:
    return bool(conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (tabela,)).fetchone())


def _limpo_env(nome):
    import os
    v = os.environ.get(nome)
    return v.strip() if v and v.strip() else None


def _add_coluna_se_falta(conn, tabela, coluna, definicao):
    if not _coluna_existe(conn, tabela, coluna):
        conn.execute(f"ALTER TABLE {tabela} ADD COLUMN {coluna} {definicao}")


# ---------------------------------------------------------------------------
# MIGRAÇÕES — nunca editar uma já lançada; acrescentar sempre uma nova no fim.
# ---------------------------------------------------------------------------
def _m1_baseline(conn):
    """Todas as tabelas históricas. CREATE TABLE IF NOT EXISTS: uma base de
    dados SQLite já existente (com dados de teste) passa por aqui sem perder
    nada."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS sessoes (
            telefone TEXT PRIMARY KEY,
            dados TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS agendamentos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telefone TEXT NOT NULL,
            nome TEXT,
            categoria TEXT,
            servico TEXT NOT NULL,
            extra TEXT,
            data TEXT,
            hora TEXT,
            preco REAL,
            duracao TEXT,
            estado TEXT DEFAULT 'confirmado',
            criado_em TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS pedidos_orcamento (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telefone TEXT NOT NULL,
            nome TEXT,
            veiculo TEXT,
            ano_veiculo TEXT,
            tipo_wrap TEXT,
            cor_acabamento TEXT,
            estado TEXT DEFAULT 'novo',
            agendamento_id INTEGER,
            criado_em TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS fotografias (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pedido_id INTEGER NOT NULL,
            nome_ficheiro TEXT NOT NULL,
            mime_tipo TEXT,
            criado_em TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS orcamentos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pedido_id INTEGER NOT NULL,
            versao INTEGER NOT NULL,
            estado TEXT NOT NULL DEFAULT 'rascunho',
            desconto_centimos INTEGER NOT NULL DEFAULT 0,
            observacoes TEXT,
            validade_dias INTEGER,
            criado_em TEXT NOT NULL,
            atualizado_em TEXT NOT NULL,
            enviado_em TEXT,
            respondido_em TEXT
        );
        CREATE TABLE IF NOT EXISTS orcamento_linhas (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            orcamento_id INTEGER NOT NULL,
            descricao TEXT NOT NULL,
            quantidade INTEGER NOT NULL DEFAULT 1,
            preco_centimos INTEGER NOT NULL DEFAULT 0,
            criado_em TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS interacoes_cliente (
            telefone TEXT PRIMARY KEY,
            ultima_mensagem_em TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS agendamento_historico (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            agendamento_id INTEGER NOT NULL,
            data_anterior TEXT,
            hora_anterior TEXT,
            data_nova TEXT,
            hora_nova TEXT,
            origem TEXT NOT NULL DEFAULT 'dashboard',
            alterado_em TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS configuracoes (
            chave TEXT PRIMARY KEY,
            valor TEXT NOT NULL,
            atualizado_em TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS reservas_temporarias (
            telefone TEXT PRIMARY KEY,
            data TEXT NOT NULL,
            hora TEXT NOT NULL,
            servico TEXT,
            duracao TEXT,
            criado_em TEXT NOT NULL,
            expira_em TEXT NOT NULL
        );
        """
    )


def _m2_colunas_legadas(conn):
    """Colunas que a versão antiga adicionava à socapa em cada request."""
    _add_coluna_se_falta(conn, "agendamentos", "carrinho_json", "TEXT")
    _add_coluna_se_falta(conn, "pedidos_orcamento", "carrinho_json", "TEXT")
    _add_coluna_se_falta(conn, "pedidos_orcamento", "modo_pedido", "TEXT")
    if not _coluna_existe(conn, "agendamentos", "bloqueia_horario"):
        conn.execute("ALTER TABLE agendamentos ADD COLUMN bloqueia_horario INTEGER NOT NULL DEFAULT 1")
        conn.execute(
            "UPDATE agendamentos SET bloqueia_horario = 0 "
            "WHERE LOWER(COALESCE(estado, '')) IN ('cancelado', 'reagendado')"
        )


def _m3_idempotencia(conn):
    """wamid das mensagens já processadas — evita marcações duplicadas quando
    a Meta reenvia o webhook. (Estava só na conversão 'Nails' por commitar;
    é um mecanismo sólido e fica formalizado aqui.)"""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS mensagens_processadas ("
        "id TEXT PRIMARY KEY, "
        "recebida_em TEXT NOT NULL)"
    )


def _m4_agendamentos_estruturados(conn):
    """Campos ESTRUTURADOS na marcação. As colunas de texto (`data`, `hora`,
    `duracao`) continuam a ser preenchidas para o calendário/painel legados,
    mas a LÓGICA passa a usar estas."""
    _add_coluna_se_falta(conn, "agendamentos", "servico_id", "TEXT")
    _add_coluna_se_falta(conn, "agendamentos", "data_iso", "TEXT")       # YYYY-MM-DD
    _add_coluna_se_falta(conn, "agendamentos", "hora_hhmm", "TEXT")      # HH:MM
    _add_coluna_se_falta(conn, "agendamentos", "duracao_min", "INTEGER")
    _add_coluna_se_falta(conn, "agendamentos", "preco_cents", "INTEGER")


def _m5_servicos(conn):
    """Catálogo de serviços em tabela — fonte única, editável na Fase 4.
    Semeada a partir de `catalogo.SERVICOS_SEED`."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS servicos ("
        "id TEXT PRIMARY KEY, "
        "nome_pt TEXT NOT NULL, "
        "nome_de TEXT NOT NULL, "
        "nome_en TEXT NOT NULL, "
        "duracao_min INTEGER NOT NULL, "
        "preco_cents INTEGER, "               # NULL = preço a confirmar
        "ativo INTEGER NOT NULL DEFAULT 1, "
        "cor TEXT, "
        "ordem INTEGER NOT NULL DEFAULT 0, "
        "atualizado_em TEXT NOT NULL)"
    )
    for i, s in enumerate(catalogo.SERVICOS_SEED):
        existe = conn.execute("SELECT 1 FROM servicos WHERE id = ?", (s["id"],)).fetchone()
        if existe:
            continue
        conn.execute(
            "INSERT INTO servicos (id, nome_pt, nome_de, nome_en, duracao_min, preco_cents, "
            "ativo, cor, ordem, atualizado_em) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (s["id"], s["nome_pt"], s["nome_de"], s["nome_en"], s["duracao_min"],
             s["preco_cents"], 1 if s["ativo"] else 0, s.get("cor"), i, iso_utc()),
        )


def _m6_backfill_estruturado(conn):
    """Preenche `servico_id/data_iso/hora_hhmm/duracao_min/preco_cents` das
    marcações ANTIGAS a partir do texto legado. Não destrói nada: as colunas
    de texto ficam como estão."""
    nomes_para_id = {row[1]: row[0] for row in conn.execute(
        "SELECT id, nome_pt FROM servicos").fetchall()}
    linhas = conn.execute(
        "SELECT id, servico, data, hora, duracao, preco FROM agendamentos "
        "WHERE data_iso IS NULL OR hora_hhmm IS NULL OR duracao_min IS NULL"
    ).fetchall()
    for _id, servico, data_txt, hora_txt, dur_txt, preco in linhas:
        data_iso = parsing.data_iso_de_texto(data_txt)
        hora_hhmm = parsing.hora_hhmm_de_texto(hora_txt)
        minutos, _ = parsing.duracao_para_minutos(dur_txt)
        servico_id = nomes_para_id.get((servico or "").strip())
        preco_cents = int(round(float(preco) * 100)) if preco not in (None, "") else None
        conn.execute(
            "UPDATE agendamentos SET servico_id = COALESCE(servico_id, ?), "
            "data_iso = COALESCE(data_iso, ?), hora_hhmm = COALESCE(hora_hhmm, ?), "
            "duracao_min = COALESCE(duracao_min, ?), preco_cents = COALESCE(preco_cents, ?) "
            "WHERE id = ?",
            (servico_id, data_iso, hora_hhmm, minutos, preco_cents, _id),
        )


def _m7_estados_canonicos(conn):
    """Vocabulário de estados PT -> canónico EN
    (confirmado->confirmed, cancelado->cancelled, concluído->completed,
    reagendado->cancelled). Não destrói nada — só renomeia o valor da coluna."""
    import estados as _est
    linhas = conn.execute("SELECT DISTINCT estado FROM agendamentos").fetchall()
    for (valor,) in linhas:
        canonico = _est.normalizar(valor)
        if canonico != (valor or ""):
            conn.execute("UPDATE agendamentos SET estado = ? WHERE estado IS ?", (canonico, valor))


def _m8_tenants(conn):
    """Fundação multi-tenant (row-level, schema partilhado). tenant #1 =
    Daniela Beauty. O routing por tenant NÃO é ativado aqui — só a estrutura,
    para o 2.º negócio não exigir reescrita."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS tenants ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "nome TEXT NOT NULL, "
        "slug TEXT UNIQUE, "
        "timezone TEXT NOT NULL DEFAULT 'Europe/Zurich', "
        "moeda TEXT NOT NULL DEFAULT 'CHF', "
        "idioma_omissao TEXT NOT NULL DEFAULT 'pt', "
        "tipo TEXT NOT NULL DEFAULT 'beauty', "
        "estado TEXT NOT NULL DEFAULT 'ativo', "
        "criado_em TEXT NOT NULL)"
    )
    nome = _limpo_env("BUSINESS_NAME") or "Daniela Beauty"
    existe = conn.execute("SELECT 1 FROM tenants WHERE id = 1").fetchone()
    if not existe:
        conn.execute(
            "INSERT INTO tenants (id, nome, slug, criado_em) VALUES (1, ?, 'daniela-beauty', ?)",
            (nome, iso_utc()))

    # tenant_id em todas as tabelas de negócio (default 1, backfill implícito).
    for tabela in ("agendamentos", "sessoes", "servicos", "configuracoes",
                   "agendamento_historico", "reservas_temporarias", "interacoes_cliente",
                   "mensagens_processadas"):
        if _tabela_existe(conn, tabela):
            _add_coluna_se_falta(conn, tabela, "tenant_id", "INTEGER NOT NULL DEFAULT 1")


def _m9_customers(conn):
    """Entidade `customers` — hoje o cliente é só telefone+nome desnormalizados.
    Backfill: um customer por (tenant_id, telefone) distinto das marcações."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS customers ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "tenant_id INTEGER NOT NULL DEFAULT 1, "
        "phone TEXT NOT NULL, "
        "name TEXT, "
        "locale TEXT, "
        "first_seen TEXT, "
        "last_visit TEXT, "
        "next_visit TEXT, "
        "visits_count INTEGER NOT NULL DEFAULT 0, "
        "spend_cents INTEGER NOT NULL DEFAULT 0, "
        "no_show_count INTEGER NOT NULL DEFAULT 0, "
        "cancel_count INTEGER NOT NULL DEFAULT 0, "
        "tags TEXT NOT NULL DEFAULT '[]', "
        "vip INTEGER NOT NULL DEFAULT 0, "
        "blocked INTEGER NOT NULL DEFAULT 0, "
        "notes_internal TEXT, "
        "created_at TEXT NOT NULL, "
        "updated_at TEXT NOT NULL, "
        "UNIQUE(tenant_id, phone))"
    )
    _add_coluna_se_falta(conn, "agendamentos", "customer_id", "INTEGER")

    agora = iso_utc()
    telefones = conn.execute(
        "SELECT DISTINCT tenant_id, telefone FROM agendamentos WHERE telefone IS NOT NULL"
    ).fetchall()
    for tenant_id, phone in telefones:
        tenant_id = tenant_id or 1
        ja = conn.execute("SELECT id FROM customers WHERE tenant_id = ? AND phone = ?",
                          (tenant_id, phone)).fetchone()
        if ja:
            cust_id = ja[0]
        else:
            nome = conn.execute(
                "SELECT nome FROM agendamentos WHERE tenant_id = ? AND telefone = ? "
                "AND nome IS NOT NULL ORDER BY id DESC LIMIT 1", (tenant_id, phone)).fetchone()
            criado = conn.execute(
                "SELECT MIN(criado_em) FROM agendamentos WHERE tenant_id = ? AND telefone = ?",
                (tenant_id, phone)).fetchone()[0] or agora
            cur = conn.execute(
                "INSERT INTO customers (tenant_id, phone, name, first_seen, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (tenant_id, phone, nome[0] if nome else None, criado, agora, agora))
            cust_id = cur.lastrowid
        conn.execute("UPDATE agendamentos SET customer_id = ? WHERE customer_id IS NULL "
                     "AND tenant_id = ? AND telefone = ?", (cust_id, tenant_id, phone))


def _m10_events(conn):
    """Outbox de eventos de domínio + base para automações/notificações.
    Um evento é escrito na MESMA transação que a mudança de estado."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS events ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "tenant_id INTEGER NOT NULL DEFAULT 1, "
        "type TEXT NOT NULL, "
        "entity_type TEXT, "
        "entity_id INTEGER, "
        "payload TEXT NOT NULL DEFAULT '{}', "
        "dedupe_key TEXT UNIQUE, "
        "created_at TEXT NOT NULL, "
        "processed_at TEXT)"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS ix_events_unprocessed "
                 "ON events (processed_at) WHERE processed_at IS NULL")


def _m11_operational_status(conn):
    """Estado OPERACIONAL da marcação, separado do estado comercial
    (confirmed/cancelled/...). scheduled -> arrived -> in_progress -> done.
    Ver bot.estado_operacional / OPERATION ENGINE."""
    _add_coluna_se_falta(conn, "agendamentos", "op_status", "TEXT NOT NULL DEFAULT 'scheduled'")
    _add_coluna_se_falta(conn, "agendamentos", "arrived_at", "TEXT")
    _add_coluna_se_falta(conn, "agendamentos", "started_at", "TEXT")
    _add_coluna_se_falta(conn, "agendamentos", "completed_at", "TEXT")
    # marcações já concluídas ficam com op_status coerente
    conn.execute("UPDATE agendamentos SET op_status = 'done' "
                 "WHERE LOWER(COALESCE(estado,'')) IN ('completed','concluido','concluído')")


def _m12_business_hours(conn):
    """Horário de funcionamento por dia da semana + exceções (fechado,
    feriados, férias, bloqueios). Substitui a lista fixa HORARIOS.
    Semeado com um horário-tipo (seg-sáb 09:00-18:00, dom fechado) que a
    Daniela ajusta no painel."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS business_hours ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "tenant_id INTEGER NOT NULL DEFAULT 1, "
        "staff_id INTEGER, "                         # NULL = horário do negócio
        "weekday INTEGER NOT NULL, "                 # 0=segunda ... 6=domingo
        "opens TEXT, "                               # 'HH:MM' ou NULL = fechado
        "closes TEXT, "
        "break_start TEXT, "                         # pausa opcional
        "break_end TEXT)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS business_hours_exceptions ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "tenant_id INTEGER NOT NULL DEFAULT 1, "
        "staff_id INTEGER, "
        "date TEXT NOT NULL, "                       # 'YYYY-MM-DD'
        "closed INTEGER NOT NULL DEFAULT 1, "
        "opens TEXT, closes TEXT, "                  # se closed=0, horário especial
        "reason TEXT, "
        "created_at TEXT NOT NULL)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS booking_policy ("
        "tenant_id INTEGER PRIMARY KEY, "
        "min_notice_min INTEGER NOT NULL DEFAULT 120, "      # antecedência mínima
        "max_notice_days INTEGER NOT NULL DEFAULT 60, "      # antecedência máxima
        "same_day INTEGER NOT NULL DEFAULT 1, "              # permitir marcar hoje
        "slot_granularity_min INTEGER NOT NULL DEFAULT 15, " # passo da grelha
        "default_buffer_after_min INTEGER NOT NULL DEFAULT 0)"
    )
    if not conn.execute("SELECT 1 FROM business_hours WHERE tenant_id = 1 AND staff_id IS NULL").fetchone():
        for wd in range(6):     # segunda a sábado
            conn.execute(
                "INSERT INTO business_hours (tenant_id, weekday, opens, closes) VALUES (1, ?, '09:00', '18:00')",
                (wd,))
        conn.execute("INSERT INTO business_hours (tenant_id, weekday, opens, closes) VALUES (1, 6, NULL, NULL)")
    if not conn.execute("SELECT 1 FROM booking_policy WHERE tenant_id = 1").fetchone():
        conn.execute("INSERT INTO booking_policy (tenant_id) VALUES (1)")

    # buffers por serviço (o motor de disponibilidade usa-os)
    _add_coluna_se_falta(conn, "servicos", "buffer_before_min", "INTEGER NOT NULL DEFAULT 0")
    _add_coluna_se_falta(conn, "servicos", "buffer_after_min", "INTEGER NOT NULL DEFAULT 0")
    _add_coluna_se_falta(conn, "servicos", "rebook_days", "INTEGER")


def _m13_webhook_idempotencia_estado(conn):
    """Máquina de estados para os wamid: claimed -> processed | failed.
    A versão antiga gravava o wamid ANTES de processar — se rebentasse a
    seguir, o retry da Meta era descartado como 'repetido' e a marcação
    perdia-se. Agora um retry de um processamento FALHADO volta a ser
    processado, mantendo a proteção contra dois webhooks concorrentes.
    (As linhas antigas ficam 'processed' — já foram tratadas e são podadas
    às 24h.)"""
    _add_coluna_se_falta(conn, "mensagens_processadas", "status",
                         "TEXT NOT NULL DEFAULT 'processed'")
    _add_coluna_se_falta(conn, "mensagens_processadas", "tenant_id",
                         "INTEGER NOT NULL DEFAULT 1")


def _m14_recalcular_customers(conn):
    """Recalcula os contadores dos customers com a NOVA semântica (visitas =
    completed, spend = completed, next_visit = próximo confirmed/pending
    futuro em hora LOCAL). A migração 9 foi aplicada com a semântica antiga
    (confirmed contava como visita) — esta corrige o que ela deixou."""
    import estados as _est
    from tempo import hoje_zurique
    hoje = hoje_zurique().isoformat()
    ids = [r[0] for r in conn.execute("SELECT id FROM customers").fetchall()]
    for cid in ids:
        rows = conn.execute(
            "SELECT estado, data_iso, preco_cents, preco FROM agendamentos WHERE customer_id = ?",
            (cid,)).fetchall()
        visits = spend = no_show = cancel = 0
        last_visit = None
        next_visit = None
        for estado, data_iso, pc, preco in rows:
            e = _est.normalizar(estado)
            cents = pc if pc is not None else (int(round(float(preco) * 100)) if preco else 0)
            if e == _est.COMPLETED:
                visits += 1
                spend += cents or 0
                if data_iso and (last_visit is None or data_iso > last_visit):
                    last_visit = data_iso
            elif e in (_est.CONFIRMED, _est.PENDING):
                if data_iso and data_iso >= hoje and (next_visit is None or data_iso < next_visit):
                    next_visit = data_iso
            elif e == _est.NO_SHOW:
                no_show += 1
            elif e == _est.CANCELLED:
                cancel += 1
        conn.execute(
            "UPDATE customers SET visits_count = ?, spend_cents = ?, no_show_count = ?, "
            "cancel_count = ?, last_visit = ?, next_visit = ?, updated_at = ? WHERE id = ?",
            (visits, spend, no_show, cancel, last_visit, next_visit, iso_utc(), cid))


def _m15_identidade_por_tenant(conn):
    """TENANT FOUNDATION (fase 2): tornar `tenant_id` PARTE DA IDENTIDADE, não
    uma coluna decorativa. As tabelas keyed só por telefone/chave passam a ter
    PK composta (tenant_id, <chave>) — o 2.º negócio deixa de colidir com o 1.º
    no SQLite. Routing por tenant continua DESATIVADO (todas as linhas ficam
    tenant_id=1); só a estrutura muda. Rebuild padrão do SQLite (create/copy/
    drop/rename) — tabelas pequenas e efémeras, sem perda de dados.

    servicos: mantém `id` (slug) como PK e ganha UNIQUE(tenant_id, id) — o
    catálogo é por tenant mas o slug continua a ser a referência em
    agendamentos.servico_id."""
    rebuilds = {
        "sessoes": (
            "tenant_id INTEGER NOT NULL DEFAULT 1, telefone TEXT NOT NULL, "
            "dados TEXT NOT NULL, PRIMARY KEY (tenant_id, telefone)",
            "tenant_id, telefone, dados"),
        "interacoes_cliente": (
            "tenant_id INTEGER NOT NULL DEFAULT 1, telefone TEXT NOT NULL, "
            "ultima_mensagem_em TEXT NOT NULL, PRIMARY KEY (tenant_id, telefone)",
            "tenant_id, telefone, ultima_mensagem_em"),
        "reservas_temporarias": (
            "tenant_id INTEGER NOT NULL DEFAULT 1, telefone TEXT NOT NULL, "
            "data TEXT NOT NULL, hora TEXT NOT NULL, servico TEXT, duracao TEXT, "
            "criado_em TEXT NOT NULL, expira_em TEXT NOT NULL, "
            "PRIMARY KEY (tenant_id, telefone)",
            "tenant_id, telefone, data, hora, servico, duracao, criado_em, expira_em"),
        "configuracoes": (
            "tenant_id INTEGER NOT NULL DEFAULT 1, chave TEXT NOT NULL, "
            "valor TEXT NOT NULL, atualizado_em TEXT NOT NULL, "
            "PRIMARY KEY (tenant_id, chave)",
            "tenant_id, chave, valor, atualizado_em"),
    }
    for tabela, (schema, colunas) in rebuilds.items():
        if not _tabela_existe(conn, tabela):
            continue
        conn.execute(f"ALTER TABLE {tabela} RENAME TO _mig15_{tabela}")
        conn.execute(f"CREATE TABLE {tabela} ({schema})")
        conn.execute(f"INSERT INTO {tabela} ({colunas}) "
                     f"SELECT {colunas} FROM _mig15_{tabela}")
        conn.execute(f"DROP TABLE _mig15_{tabela}")

    if _tabela_existe(conn, "servicos"):
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS ix_servicos_tenant_id "
                     "ON servicos (tenant_id, id)")


def _m16_faturacao(conn):
    """FATURAÇÃO integrada (BILLING ENGINE). Uma marcação concluída gera uma
    fatura. Tudo em cêntimos inteiros — NUNCA float para dinheiro.

    - billing_settings: dados legais do negócio por tenant. vat_enabled=0 por
      omissão; NADA de IBAN / MWST / UID inventado — só o que a Daniela puser.
    - invoices: número atribuído no MOMENTO DA EMISSÃO (não na criação do
      rascunho) -> a série emitida fica sem buracos. draft/issued/paid/cancelled.
    - invoice_lines: linhas da fatura.
    - snapshots: nome/morada do cliente e do negócio + info de imposto ficam
      CONGELADOS na fatura — se o preço do serviço mudar daqui a 3 meses, a
      fatura antiga não muda.
    """
    from tempo import iso_utc as _agora
    conn.execute(
        "CREATE TABLE IF NOT EXISTS billing_settings ("
        "tenant_id INTEGER PRIMARY KEY, "
        "legal_name TEXT, address TEXT, postal_code TEXT, city TEXT, "
        "country TEXT NOT NULL DEFAULT 'CH', email TEXT, phone TEXT, iban TEXT, "
        "vat_enabled INTEGER NOT NULL DEFAULT 0, "
        "vat_rate_bps INTEGER NOT NULL DEFAULT 0, "     # 810 = 8.10 %
        "vat_number TEXT, "
        "invoice_prefix TEXT, "
        "payment_terms_days INTEGER NOT NULL DEFAULT 30, "
        "invoice_footer TEXT, "
        "currency TEXT NOT NULL DEFAULT 'CHF', "
        "updated_at TEXT NOT NULL)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS invoices ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "tenant_id INTEGER NOT NULL DEFAULT 1, "
        "appointment_id INTEGER, customer_id INTEGER, "
        "year INTEGER, seq INTEGER, invoice_number TEXT, "
        "status TEXT NOT NULL DEFAULT 'draft', "
        "currency TEXT NOT NULL DEFAULT 'CHF', "
        "issue_date TEXT, due_date TEXT, "
        "subtotal_cents INTEGER NOT NULL DEFAULT 0, "
        "discount_cents INTEGER NOT NULL DEFAULT 0, "
        "tax_rate_bps INTEGER NOT NULL DEFAULT 0, "
        "tax_cents INTEGER NOT NULL DEFAULT 0, "
        "total_cents INTEGER NOT NULL DEFAULT 0, "
        "customer_name_snapshot TEXT, customer_address_snapshot TEXT, "
        "business_name_snapshot TEXT, business_address_snapshot TEXT, "
        "business_vat_snapshot TEXT, "
        "notes TEXT, "
        "created_at TEXT NOT NULL, issued_at TEXT, paid_at TEXT, cancelled_at TEXT)"
    )
    # número único por tenant; série anual sem buracos
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS ix_invoices_number "
                 "ON invoices (tenant_id, invoice_number) WHERE invoice_number IS NOT NULL")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS ix_invoices_serie "
                 "ON invoices (tenant_id, year, seq) WHERE seq IS NOT NULL")
    # idempotência: uma marcação -> no máximo UMA fatura viva (não anulada)
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS ix_invoices_appointment "
                 "ON invoices (tenant_id, appointment_id) "
                 "WHERE appointment_id IS NOT NULL AND status <> 'cancelled'")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_invoices_status "
                 "ON invoices (tenant_id, status)")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS invoice_lines ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "invoice_id INTEGER NOT NULL, "
        "description TEXT NOT NULL, "
        "quantity INTEGER NOT NULL DEFAULT 1, "
        "unit_price_cents INTEGER NOT NULL DEFAULT 0, "
        "line_total_cents INTEGER NOT NULL DEFAULT 0, "
        "sort_order INTEGER NOT NULL DEFAULT 0)"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS ix_invoice_lines_invoice "
                 "ON invoice_lines (invoice_id)")

    # seed do tenant 1 — só a partir do ambiente; nada inventado
    if not conn.execute("SELECT 1 FROM billing_settings WHERE tenant_id = 1").fetchone():
        nome = _limpo_env("BUSINESS_NAME") or "Daniela Beauty"
        morada = _limpo_env("BUSINESS_ADDRESS")
        conn.execute(
            "INSERT INTO billing_settings (tenant_id, legal_name, address, updated_at) "
            "VALUES (1, ?, ?, ?)", (nome, morada, _agora()))


MIGRACOES = [
    (1, "baseline", _m1_baseline),
    (2, "colunas_legadas", _m2_colunas_legadas),
    (3, "idempotencia_wamid", _m3_idempotencia),
    (4, "agendamentos_estruturados", _m4_agendamentos_estruturados),
    (5, "servicos_catalogo", _m5_servicos),
    (6, "backfill_estruturado", _m6_backfill_estruturado),
    (7, "estados_canonicos", _m7_estados_canonicos),
    (8, "tenants_foundation", _m8_tenants),
    (9, "customers", _m9_customers),
    (10, "events_outbox", _m10_events),
    (11, "operational_status", _m11_operational_status),
    (12, "business_hours", _m12_business_hours),
    (13, "webhook_idempotencia_estado", _m13_webhook_idempotencia_estado),
    (14, "recalcular_customers", _m14_recalcular_customers),
    (15, "identidade_por_tenant", _m15_identidade_por_tenant),
    (16, "faturacao", _m16_faturacao),
]


def migrar(verbose: bool = False):
    """Aplica as migrações em falta. Idempotente e seguro para correr em
    cada arranque."""
    conn = _conectar()
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "versao INTEGER PRIMARY KEY, nome TEXT NOT NULL, aplicada_em TEXT NOT NULL)"
        )
        conn.commit()
        feitas = {r[0] for r in conn.execute("SELECT versao FROM schema_migrations").fetchall()}
        for versao, nome, funcao in MIGRACOES:
            if versao in feitas:
                continue
            if verbose:
                print(f"[db] migração {versao}: {nome}")
            funcao(conn)
            conn.execute(
                "INSERT INTO schema_migrations (versao, nome, aplicada_em) VALUES (?, ?, ?)",
                (versao, nome, iso_utc()),
            )
            conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def garantir_migracoes():
    """Corre `migrar()` uma única vez por processo."""
    global _MIGRADO
    if _MIGRADO:
        return
    with _LOCK:
        if _MIGRADO:
            return
        migrar()
        _MIGRADO = True


def resetar_estado_migracao_para_testes():
    """Só para a suite de testes, que troca de base de dados entre casos."""
    global _MIGRADO
    _MIGRADO = False


# ---------------------------------------------------------------------------
# Leitura do catálogo de serviços (tabela `servicos`)
# ---------------------------------------------------------------------------
_CAMPOS_SERVICO = ("id", "nome_pt", "nome_de", "nome_en", "duracao_min",
                   "preco_cents", "ativo", "cor", "ordem",
                   "buffer_before_min", "buffer_after_min", "rebook_days")


def _linha_servico(row) -> dict:
    d = dict(zip(_CAMPOS_SERVICO, row))
    d["ativo"] = bool(d["ativo"])
    return d


def listar_servicos(incluir_inativos: bool = False, conn=None) -> list[dict]:
    def _ler(c):
        sql = ("SELECT id, nome_pt, nome_de, nome_en, duracao_min, preco_cents, ativo, cor, ordem, buffer_before_min, buffer_after_min, rebook_days "
               "FROM servicos")
        if not incluir_inativos:
            sql += " WHERE ativo = 1"
        sql += " ORDER BY ordem ASC, nome_pt ASC"
        return [_linha_servico(r) for r in c.execute(sql).fetchall()]

    if conn is not None:
        return _ler(conn)
    with ligacao() as c:
        return _ler(c)


def obter_servico(servico_id: str, conn=None) -> dict | None:
    if not servico_id:
        return None

    def _ler(c):
        r = c.execute(
            "SELECT id, nome_pt, nome_de, nome_en, duracao_min, preco_cents, ativo, cor, ordem, buffer_before_min, buffer_after_min, rebook_days "
            "FROM servicos WHERE id = ?", (servico_id,)).fetchone()
        return _linha_servico(r) if r else None

    if conn is not None:
        return _ler(conn)
    with ligacao() as c:
        return _ler(c)


def servico_por_nome_pt(nome_pt: str, conn=None) -> dict | None:
    """Resolve um serviço pelo nome canónico português — para marcações
    legadas que só têm `agendamentos.servico` em texto."""
    if not nome_pt:
        return None

    def _ler(c):
        r = c.execute(
            "SELECT id, nome_pt, nome_de, nome_en, duracao_min, preco_cents, ativo, cor, ordem, buffer_before_min, buffer_after_min, rebook_days "
            "FROM servicos WHERE nome_pt = ?", (nome_pt.strip(),)).fetchone()
        return _linha_servico(r) if r else None

    if conn is not None:
        return _ler(conn)
    with ligacao() as c:
        return _ler(c)


def criar_servico(dados: dict) -> str:
    """Cria um serviço no catálogo (CRUD do dashboard). `dados` tem id,
    nome_pt/de/en, duracao_min, preco_cents (ou None), ativo, cor, ordem."""
    with ligacao() as c:
        c.execute(
            "INSERT INTO servicos (id, nome_pt, nome_de, nome_en, duracao_min, preco_cents, "
            "ativo, cor, ordem, atualizado_em, tenant_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)",
            (dados["id"], dados["nome_pt"], dados.get("nome_de") or dados["nome_pt"],
             dados.get("nome_en") or dados["nome_pt"], int(dados["duracao_min"]),
             dados.get("preco_cents"), 1 if dados.get("ativo", True) else 0,
             dados.get("cor") or catalogo.COR_OMISSAO, int(dados.get("ordem", 99)), iso_utc()))
    return dados["id"]


def atualizar_servico(servico_id: str, dados: dict):
    campos, valores = [], []
    for col in ("nome_pt", "nome_de", "nome_en", "duracao_min", "preco_cents", "ativo", "cor",
                "ordem", "rebook_days", "buffer_before_min", "buffer_after_min"):
        if col in dados:
            v = dados[col]
            if col == "ativo":
                v = 1 if v else 0
            campos.append(f"{col} = ?")
            valores.append(v)
    if not campos:
        return
    campos.append("atualizado_em = ?")
    valores.append(iso_utc())
    valores.append(servico_id)
    with ligacao() as c:
        c.execute(f"UPDATE servicos SET {', '.join(campos)} WHERE id = ?", valores)


# ---------------------------------------------------------------------------
# Clientes (CRM) — a verdade é a tabela `customers`
# ---------------------------------------------------------------------------
_CAMPOS_CUSTOMER = ("id", "tenant_id", "phone", "name", "locale", "first_seen", "last_visit",
                    "next_visit", "visits_count", "spend_cents", "no_show_count",
                    "cancel_count", "tags", "vip", "blocked", "notes_internal",
                    "created_at", "updated_at")


def _linha_customer(row) -> dict:
    import json as _j
    d = dict(zip(_CAMPOS_CUSTOMER, row))
    d["vip"] = bool(d["vip"])
    d["blocked"] = bool(d["blocked"])
    try:
        d["tags"] = _j.loads(d["tags"] or "[]")
    except (ValueError, TypeError):
        d["tags"] = []
    return d


_SQL_CUSTOMER = ", ".join(_CAMPOS_CUSTOMER)


def obter_ou_criar_customer(telefone: str, nome: str | None = None, locale: str | None = None,
                            tenant_id: int = 1, conn=None) -> dict:
    """Devolve o customer deste telefone (cria se não existir). Atualiza o
    nome/locale se vierem preenchidos e ainda faltarem. NÃO conta visitas —
    isso é `registar_visita_customer`, chamado ao gravar a marcação."""
    def _run(c):
        agora = iso_utc()
        r = c.execute(f"SELECT {_SQL_CUSTOMER} FROM customers WHERE tenant_id = ? AND phone = ?",
                      (tenant_id, telefone)).fetchone()
        if r:
            cust = _linha_customer(r)
            mudou = []
            if nome and not cust["name"]:
                mudou.append(("name", nome))
            if locale and not cust["locale"]:
                mudou.append(("locale", locale))
            if mudou:
                sets = ", ".join(f"{k} = ?" for k, _ in mudou) + ", updated_at = ?"
                c.execute(f"UPDATE customers SET {sets} WHERE id = ?",
                          [v for _, v in mudou] + [agora, cust["id"]])
                r = c.execute(f"SELECT {_SQL_CUSTOMER} FROM customers WHERE id = ?",
                              (cust["id"],)).fetchone()
            return _linha_customer(r)
        cur = c.execute(
            "INSERT INTO customers (tenant_id, phone, name, locale, first_seen, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (tenant_id, telefone, nome, locale, agora, agora, agora))
        r = c.execute(f"SELECT {_SQL_CUSTOMER} FROM customers WHERE id = ?", (cur.lastrowid,)).fetchone()
        return _linha_customer(r)

    if conn is not None:
        return _run(conn)
    with ligacao() as c:
        return _run(c)


def obter_customer(customer_id: int, conn=None) -> dict | None:
    def _run(c):
        r = c.execute(f"SELECT {_SQL_CUSTOMER} FROM customers WHERE id = ?", (customer_id,)).fetchone()
        return _linha_customer(r) if r else None
    if conn is not None:
        return _run(conn)
    with ligacao() as c:
        return _run(c)


def listar_customers(tenant_id: int = 1) -> list[dict]:
    with ligacao() as c:
        rows = c.execute(
            f"SELECT {_SQL_CUSTOMER} FROM customers WHERE tenant_id = ? "
            "ORDER BY COALESCE(last_visit, first_seen) DESC", (tenant_id,)).fetchall()
    return [_linha_customer(r) for r in rows]


def recalcular_customer(customer_id: int, conn=None):
    """Recalcula contadores (visitas/gasto/no-shows/cancelamentos/últimas
    datas) a partir das marcações. Barato e sempre correto — chamado após
    qualquer mudança de estado de uma marcação do cliente."""
    def _run(c):
        import estados as _est
        from tempo import hoje_zurique
        # "hoje" em data LOCAL (Europe/Zurique): uma marcação às 20h de hoje
        # continua a ser "futura" mesmo já sendo amanhã em UTC.
        hoje = hoje_zurique().isoformat()
        rows = c.execute(
            "SELECT estado, data_iso, preco_cents, preco FROM agendamentos WHERE customer_id = ?",
            (customer_id,)).fetchall()
        visits = spend = no_show = cancel = 0
        last_visit = next_visit = None
        for estado, data_iso, pc, preco in rows:
            e = _est.normalizar(estado)
            cents = pc if pc is not None else (int(round(float(preco) * 100)) if preco else 0)
            if e == _est.COMPLETED:
                # visita REALIZADA + gasto efetivo (até existirem pagamentos)
                visits += 1
                spend += cents or 0
                if data_iso and (last_visit is None or data_iso > last_visit):
                    last_visit = data_iso
            elif e in (_est.CONFIRMED, _est.PENDING):
                # marcação futura ativa -> candidata a next_visit; NÃO conta
                # como visita nem como gasto (ainda não aconteceu)
                if data_iso and data_iso >= hoje and (next_visit is None or data_iso < next_visit):
                    next_visit = data_iso
            elif e == _est.NO_SHOW:
                no_show += 1
            elif e == _est.CANCELLED:
                cancel += 1
        c.execute(
            "UPDATE customers SET visits_count = ?, spend_cents = ?, no_show_count = ?, "
            "cancel_count = ?, last_visit = ?, next_visit = ?, updated_at = ? WHERE id = ?",
            (visits, spend, no_show, cancel, last_visit, next_visit, iso_utc(), customer_id))

    if conn is not None:
        return _run(conn)
    with ligacao() as c:
        return _run(c)


# ---------------------------------------------------------------------------
# Eventos (outbox) — escritos na MESMA transação que a mudança de estado
# ---------------------------------------------------------------------------
def registar_evento(conn, tipo: str, entity_type: str | None, entity_id: int | None,
                    payload: dict | None = None, dedupe_key: str | None = None,
                    tenant_id: int = 1):
    """Escreve uma linha em `events`. RECEBE a conexão — para correr dentro da
    transação da operação de domínio (transactional outbox). `dedupe_key`
    UNIQUE evita eventos duplicados (webhook reenviado, retry)."""
    import json as _j
    try:
        conn.execute(
            "INSERT INTO events (tenant_id, type, entity_type, entity_id, payload, dedupe_key, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (tenant_id, tipo, entity_type, entity_id, _j.dumps(payload or {}, ensure_ascii=False),
             dedupe_key, iso_utc()))
    except sqlite3.IntegrityError:
        pass  # dedupe_key colidiu — evento já registado, ignora em silêncio


def eventos_por_processar(limite: int = 100) -> list[dict]:
    import json as _j
    with ligacao() as c:
        rows = c.execute(
            "SELECT id, tenant_id, type, entity_type, entity_id, payload, created_at "
            "FROM events WHERE processed_at IS NULL ORDER BY id ASC LIMIT ?", (limite,)).fetchall()
    out = []
    for r in rows:
        d = dict(zip(("id", "tenant_id", "type", "entity_type", "entity_id", "payload", "created_at"), r))
        try:
            d["payload"] = _j.loads(d["payload"] or "{}")
        except (ValueError, TypeError):
            d["payload"] = {}
        out.append(d)
    return out


def marcar_evento_processado(evento_id: int):
    with ligacao() as c:
        c.execute("UPDATE events SET processed_at = ? WHERE id = ?", (iso_utc(), evento_id))


# ---------------------------------------------------------------------------
# Idempotência do webhook — máquina de estados por wamid
#   claimed   -> reclamado por um webhook, a processar
#   processed -> tratado com sucesso; retry é descartado
#   failed    -> o processamento rebentou; um retry da Meta VOLTA a processar
# ---------------------------------------------------------------------------
IDEMPOTENCIA_HORAS = 24
_CLAIM_PRESO_SEGUNDOS = 90          # claim 'preso' há mais que isto = worker crashou


def reclamar_mensagem(wamid, tenant_id: int = 1) -> str:
    """Devolve 'nova' (deve processar-se) ou 'duplicada' (ignorar: já
    processada, ou a ser processada AGORA por outro webhook concorrente).
    O BEGIN IMMEDIATE serializa dois webhooks com o mesmo wamid."""
    if not wamid:
        return "nova"
    from datetime import datetime, timedelta
    agora = datetime.now(__import__("tempo").FUSO_UTC)
    agora_iso = iso_utc(agora)
    prune = iso_utc(agora - timedelta(hours=IDEMPOTENCIA_HORAS))
    preso = iso_utc(agora - timedelta(seconds=_CLAIM_PRESO_SEGUNDOS))
    with ligacao() as c:
        c.execute("BEGIN IMMEDIATE")
        c.execute("DELETE FROM mensagens_processadas WHERE recebida_em < ?", (prune,))
        row = c.execute("SELECT status, recebida_em FROM mensagens_processadas WHERE id = ?",
                        (str(wamid),)).fetchone()
        if row is None:
            c.execute("INSERT INTO mensagens_processadas (id, recebida_em, status, tenant_id) "
                      "VALUES (?, ?, 'claimed', ?)", (str(wamid), agora_iso, tenant_id))
            return "nova"
        status, quando = row
        if status == "processed":
            return "duplicada"
        if status == "claimed" and (quando or "") >= preso:
            return "duplicada"          # outro webhook está a tratar agora mesmo
        # 'failed', ou um claim preso há muito tempo -> volta a reclamar
        c.execute("UPDATE mensagens_processadas SET status = 'claimed', recebida_em = ? WHERE id = ?",
                  (agora_iso, str(wamid)))
        return "nova"


def confirmar_mensagem(wamid):
    if not wamid:
        return
    with ligacao() as c:
        c.execute("UPDATE mensagens_processadas SET status = 'processed' WHERE id = ?", (str(wamid),))


def falhar_mensagem(wamid):
    if not wamid:
        return
    with ligacao() as c:
        c.execute("UPDATE mensagens_processadas SET status = 'failed' WHERE id = ?", (str(wamid),))


# ---------------------------------------------------------------------------
# Ocupação de um dia — para o motor de disponibilidade (scheduling.availability)
# ---------------------------------------------------------------------------
def ocupacao_do_dia(data_iso: str, tenant_id: int = 1, conn=None) -> list[dict]:
    """Marcações que BLOQUEIAM o horário + reservas temporárias ativas nesse
    dia. Cada item: {inicio_min, dur_min, buffer_before, buffer_after, id,
    telefone}. `inicio_min` = minutos desde a meia-noite."""
    import estados as _est
    import parsing as _p

    def _run(c):
        c.execute("DELETE FROM reservas_temporarias WHERE expira_em <= ?", (iso_utc(),))
        itens = []
        rows = c.execute(
            "SELECT a.id, a.telefone, a.estado, a.bloqueia_horario, a.hora_hhmm, a.hora, "
            "a.duracao_min, a.duracao, a.servico, s.buffer_before_min, s.buffer_after_min "
            "FROM agendamentos a LEFT JOIN servicos s ON s.id = a.servico_id "
            "WHERE a.tenant_id = ? AND (a.data_iso = ? OR a.data LIKE ?)",
            (tenant_id, data_iso, f"%{_iso_para_dmy(data_iso)}%")).fetchall()
        for (aid, tel, estado, bloq, hhmm, hora_txt, dmin, dur_txt, servico, bb, ba) in rows:
            if not _est.bloqueia_horario(estado, bloq):
                continue
            hh = hhmm or _p.hora_hhmm_de_texto(hora_txt)
            mins = dmin
            if mins is None:
                mins, _ = _p.duracao_para_minutos(dur_txt)
            im = _hhmm_para_min(hh)
            if im is None or not mins:
                continue
            itens.append({"id": aid, "telefone": tel, "inicio_min": im, "dur_min": int(mins),
                          "buffer_before": bb or 0, "buffer_after": ba or 0})
        for (tel, hora_txt, servico, dur_txt) in c.execute(
                "SELECT telefone, hora, servico, duracao FROM reservas_temporarias "
                "WHERE tenant_id = ? AND expira_em > ? AND (data LIKE ? OR data = ?)",
                (tenant_id, iso_utc(), f"%{_iso_para_dmy(data_iso)}%", data_iso)).fetchall():
            hh = _p.hora_hhmm_de_texto(hora_txt)
            mins, _ = _p.duracao_para_minutos(dur_txt)
            im = _hhmm_para_min(hh)
            if im is None or not mins:
                continue
            itens.append({"id": None, "telefone": tel, "inicio_min": im, "dur_min": int(mins),
                          "buffer_before": 0, "buffer_after": 0, "retencao": True})
        return itens

    if conn is not None:
        return _run(conn)
    with ligacao() as c:
        return _run(c)


def _hhmm_para_min(v):
    if not v:
        return None
    try:
        h, m = str(v).split(":")
        return int(h) * 60 + int(m)
    except (ValueError, AttributeError):
        return None


def _iso_para_dmy(data_iso: str) -> str:
    """'2026-09-07' -> '07.09.2026' (a coluna legada `data` usa este formato)."""
    try:
        a, m, d = data_iso.split("-")
        return f"{d}.{m}.{a}"
    except ValueError:
        return data_iso
