# Base de dados — estado e migração para PostgreSQL

## Estado atual

- **Backend em uso:** SQLite (`config.SQLITE_PATH`, por omissão `sessoes.db`).
- **Schema:** construído por migrações **versionadas** em `db.py` (lista
  `MIGRACOES`), registadas na tabela `schema_migrations`. Correm uma vez, no
  arranque (`db.garantir_migracoes()`) e no `preDeployCommand` do Render.
  **Não há `CREATE/ALTER TABLE` a cada request.**
- **Ligações:** `db.ligacao()` é um context manager que **fecha sempre** a
  ligação. WAL + `busy_timeout=15s`. Escritas críticas usam `BEGIN IMMEDIATE`.
- A camada Postgres está **preparada mas não ativada**: se `DATABASE_URL`
  apontar para Postgres, `db.py` aborta com uma mensagem explícita em vez de
  correr meio configurado.

## Migrações aplicadas

| nº | nome | o que faz |
|----|------|-----------|
| 1 | baseline | tabelas históricas (`sessoes`, `agendamentos`, `pedidos_orcamento`, `fotografias`, `orcamentos`, `orcamento_linhas`, `interacoes_cliente`, `agendamento_historico`, `configuracoes`, `reservas_temporarias`) |
| 2 | colunas_legadas | `carrinho_json`, `modo_pedido`, `bloqueia_horario` |
| 3 | idempotencia_wamid | tabela `mensagens_processadas` (`id` = wamid) |
| 4 | agendamentos_estruturados | `servico_id`, `data_iso`, `hora_hhmm`, `duracao_min`, `preco_cents` |
| 5 | servicos_catalogo | tabela `servicos` semeada de `catalogo.SERVICOS_SEED` |
| 6 | backfill_estruturado | preenche as colunas novas a partir do texto legado |
| 7 | estados_canonicos | `confirmado`→`confirmed`, `cancelado`→`cancelled`, `concluído`→`completed`, `reagendado`→`cancelled` |
| 8 | tenants_foundation | tabela `tenants` (#1 = Daniela Beauty) + coluna `tenant_id` (`NOT NULL DEFAULT 1`) em `agendamentos`, `sessoes`, `servicos`, `configuracoes`, `agendamento_historico`, `reservas_temporarias`, `interacoes_cliente`, `mensagens_processadas` |
| 9 | customers | tabela `customers` (`UNIQUE(tenant_id, phone)`) + `agendamentos.customer_id`; backfill de um customer por telefone distinto |
| 10 | events_outbox | tabela `events` (`dedupe_key` UNIQUE, `processed_at`) — transactional outbox |
| 11 | operational_status | `agendamentos.op_status` + `arrived_at` / `started_at` / `completed_at` |
| 12 | business_hours | `business_hours` (grelha semanal), `business_hours_exceptions`, `booking_policy` (`tenant_id` PK) + seed do tenant 1 |
| 13 | webhook_idempotencia_estado | `mensagens_processadas.status` (`claimed`/`processed`/`failed`) + `tenant_id` |
| 14 | recalcular_customers | recalcula todos os `customers` com a semântica correta (visitas/gasto = só `completed`; `next_visit` em data local) — **corrige** o que a migração 9 semeou; não edita a 9 |
| 15 | identidade_por_tenant | rebuild de `sessoes`, `interacoes_cliente`, `reservas_temporarias`, `configuracoes` com **PK composta `(tenant_id, <chave>)`** + `UNIQUE INDEX ix_servicos_tenant_id (tenant_id, id)`. Rebuild padrão SQLite (rename/create/copy/drop), sem perda de dados; todas as linhas ficam `tenant_id = 1` |

Nenhuma é destrutiva: colunas antigas mantêm-se, linhas antigas continuam a
resolver. As migrações 9 e 14 são idempotentes por natureza (recalculam a
partir de `agendamentos`).

## Tabelas (schema atual)

| tabela | chave | notas |
|--------|-------|-------|
| `tenants` | `id` | #1 = Daniela Beauty; routing não ativado |
| `agendamentos` | `id` | `estado` (comercial) + `op_status` (operacional); `tenant_id`, `customer_id`, `servico_id`, colunas estruturadas |
| `agendamento_historico` | `id` | uma linha por reagendamento; `dedupe_key` do evento = este `id` |
| `sessoes` | **`(tenant_id, telefone)`** | estado efémero da conversa |
| `interacoes_cliente` | **`(tenant_id, telefone)`** | última mensagem do cliente (janela 24h da Meta) |
| `reservas_temporarias` | **`(tenant_id, telefone)`** | retenção de 15 min do horário a confirmar |
| `configuracoes` | **`(tenant_id, chave)`** | flags do painel |
| `servicos` | `id` (slug) | `+ UNIQUE(tenant_id, id)`; catálogo por tenant, mas o slug ainda é global — ver "Pendente" |
| `customers` | `id` | `UNIQUE(tenant_id, phone)`; contadores derivados de `agendamentos` |
| `events` | `id` | outbox; `dedupe_key` UNIQUE, `processed_at` NULL = por processar |
| `mensagens_processadas` | `id` (wamid) | `status` claimed/processed/failed; wamid é globalmente único (Meta) |
| `business_hours` | `id` | grelha semanal por `(tenant_id, weekday, staff_id)` |
| `business_hours_exceptions` | `id` | feriados / dias especiais |
| `booking_policy` | `tenant_id` | min-notice, janela de reserva, etc. |
| `pedidos_orcamento`, `fotografias`, `orcamentos`, `orcamento_linhas` | `id` | **legado Spotless** — inativo no fluxo Daniela |

## Preservar os dados existentes (SQLite)

O ficheiro `.db` está em `.gitignore` e, em produção, deve viver num **disco
persistente** do Render (`render.yaml`, `mountPath: /var/data`). Sem disco, o
filesystem do Render é efémero.

Cópia de segurança antes de qualquer operação:

```bash
sqlite3 /var/data/sessoes.db ".backup '/var/data/sessoes-backup-$(date +%F).db'"
```

A migração 15 faz rebuild de 4 tabelas — **fazer o backup acima antes do
deploy que a aplica**. É idempotente por versão (`schema_migrations`), corre
uma só vez.

## Passo SEPARADO: migrar SQLite → PostgreSQL (antes do deploy definitivo)

1. Criar a base Postgres no Render e obter `DATABASE_URL`.
2. Implementar o backend Postgres em `db.py`:
   - `_conectar()` passa a usar `psycopg` (já em `requirements.txt`) quando
     `config.usa_postgres()`.
   - Traduzir, para **todas as 15 migrações** (mesma lista, SQL compatível):
     - placeholders `?` → `%s`;
     - `INTEGER PRIMARY KEY AUTOINCREMENT` → `BIGSERIAL PRIMARY KEY`;
     - `INSERT OR IGNORE` → `INSERT ... ON CONFLICT DO NOTHING`;
     - `ON CONFLICT(col) DO UPDATE SET x = excluded.x` → sintaxe Postgres
       (`EXCLUDED` maiúsculo, igual);
     - `PRAGMA table_info` (usado por `_coluna_existe` / `_tabela_existe` e
       pelos rebuilds das migrações 14/15) → `information_schema.columns` /
       `to_regclass`;
     - o rebuild da migração 15 (`ALTER TABLE ... RENAME` + `CREATE` + `INSERT
       SELECT` + `DROP`) funciona igual em Postgres, mas a PK composta pode ser
       declarada diretamente com `PRIMARY KEY (tenant_id, telefone)` sem rebuild
       se a tabela for criada de raiz no Postgres;
     - `DATETIME`/texto ISO → `timestamptz` (opcional; a app guarda ISO-8601 em
       texto e faz o parsing em `tempo.py`).
   - Rever os `conn.execute` fora de `db.py`: `bot.py` (sessão, config,
     reservas, interações, ocupação inline em rotas de exceções),
     `scheduling/business_hours.py`, `scheduling/availability.py`,
     `operations/engine.py`, `notifications/business.py`. A maioria do SQL de
     negócio já está centralizada em `db.py`.
3. Exportar de SQLite e importar em Postgres, tabela a tabela, na ordem:
   `tenants` → `servicos` → `customers` → `agendamentos` →
   `agendamento_historico` → `sessoes` → `configuracoes` →
   `interacoes_cliente` → `reservas_temporarias` → `business_hours` →
   `business_hours_exceptions` → `booking_policy` → `events` →
   `mensagens_processadas`. As tabelas legado Spotless
   (`pedidos_orcamento`, `fotografias`, `orcamentos`, `orcamento_linhas`)
   podem não ser migradas.
4. Definir `DATABASE_URL` no Render, remover o `disk:` do `render.yaml`, subir
   `--workers` no `startCommand`/`Procfile`.
5. Correr a suite (`pytest -q`) apontada à nova base antes de trocar o tráfego.

Enquanto o ponto 2 não estiver feito, **manter `DATABASE_URL` vazio** — a app
funciona em SQLite.

## Pendente / decisões em aberto

- **`servicos` por tenant:** hoje o `id` (slug) é PK global; dois tenants não
  podem ter o slug `limpeza_pele`. O `UNIQUE(tenant_id, id)` da migração 15 é
  só um marcador. O fix real — PK `(tenant_id, id)` + ajustar o join
  `servicos s ON s.id = a.servico_id` (motor de disponibilidade) e a
  referência `agendamentos.servico_id` — fica para o onboarding do 2.º
  negócio, para não reescrever o motor de slots antes de ser preciso.
- **Routing multi-tenant:** a estrutura está pronta (colunas + PKs + helpers
  com `tenant_id`); falta resolver o tenant por `PHONE_NUMBER_ID` (webhook) e
  por subdomínio/login (painel) e passar o `tenant_id` real nos call sites.
