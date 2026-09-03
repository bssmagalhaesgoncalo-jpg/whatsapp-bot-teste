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


MIGRACOES = [
    (1, "baseline", _m1_baseline),
    (2, "colunas_legadas", _m2_colunas_legadas),
    (3, "idempotencia_wamid", _m3_idempotencia),
    (4, "agendamentos_estruturados", _m4_agendamentos_estruturados),
    (5, "servicos_catalogo", _m5_servicos),
    (6, "backfill_estruturado", _m6_backfill_estruturado),
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
                   "preco_cents", "ativo", "cor", "ordem")


def _linha_servico(row) -> dict:
    d = dict(zip(_CAMPOS_SERVICO, row))
    d["ativo"] = bool(d["ativo"])
    return d


def listar_servicos(incluir_inativos: bool = False, conn=None) -> list[dict]:
    def _ler(c):
        sql = ("SELECT id, nome_pt, nome_de, nome_en, duracao_min, preco_cents, ativo, cor, ordem "
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
            "SELECT id, nome_pt, nome_de, nome_en, duracao_min, preco_cents, ativo, cor, ordem "
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
            "SELECT id, nome_pt, nome_de, nome_en, duracao_min, preco_cents, ativo, cor, ordem "
            "FROM servicos WHERE nome_pt = ?", (nome_pt.strip(),)).fetchone()
        return _linha_servico(r) if r else None

    if conn is not None:
        return _ler(conn)
    with ligacao() as c:
        return _ler(c)
