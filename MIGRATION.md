# Base de dados — estado e migração para PostgreSQL

## Estado atual (Fase 0 + 1)

- **Backend em uso:** SQLite (`config.SQLITE_PATH`, por omissão `sessoes.db`).
- **Schema:** construído por migrações **versionadas** em `db.py` (lista
  `MIGRACOES`), registadas na tabela `schema_migrations`. Correm uma vez, no
  arranque (`db.garantir_migracoes()`) e no `preDeployCommand` do Render.
  **Já não há `CREATE/ALTER TABLE` a cada request.**
- **Ligações:** `db.ligacao()` é um context manager que **fecha sempre** a
  ligação (a versão antiga nunca fechava). WAL + `busy_timeout=15s`.
- A camada Postgres está **preparada mas não ativada**: se `DATABASE_URL`
  apontar para Postgres, `db.py` aborta com uma mensagem explícita em vez de
  correr meio configurado (decisão de segurança — não misturar a migração de
  dados com as alterações de funcionalidade desta fase).

## Migrações aplicadas

| nº | nome | o que faz |
|----|------|-----------|
| 1 | baseline | todas as tabelas históricas (`CREATE TABLE IF NOT EXISTS`) |
| 2 | colunas_legadas | `carrinho_json`, `modo_pedido`, `bloqueia_horario` |
| 3 | idempotencia_wamid | tabela `mensagens_processadas` |
| 4 | agendamentos_estruturados | `servico_id`, `data_iso`, `hora_hhmm`, `duracao_min`, `preco_cents` |
| 5 | servicos_catalogo | tabela `servicos` semeada de `catalogo.SERVICOS_SEED` |
| 6 | backfill_estruturado | preenche as colunas novas a partir do texto legado |
| 7 | estados_canonicos | `confirmado`→`confirmed`, `cancelado`→`cancelled`, `concluído`→`completed`, `reagendado`→`cancelled` |

Nenhuma é destrutiva: colunas antigas mantêm-se, linhas antigas continuam a
resolver.

## Preservar os dados existentes (SQLite)

O ficheiro `.db` está em `.gitignore` e, em produção, deve viver num **disco
persistente** do Render (ver `render.yaml`, `mountPath: /var/data`). Sem disco,
o filesystem do Render é efémero e os dados perdem-se a cada deploy.

Cópia de segurança antes de qualquer operação:

```bash
sqlite3 /var/data/sessoes.db ".backup '/var/data/sessoes-backup-$(date +%F).db'"
```

## Passo SEPARADO: migrar SQLite → PostgreSQL (antes do deploy definitivo)

1. Criar a base Postgres no Render e obter `DATABASE_URL`.
2. Implementar o backend Postgres em `db.py`:
   - `_conectar()` passa a usar `psycopg` (já em `requirements.txt`) quando
     `config.usa_postgres()`.
   - Ajustar os placeholders `?` → `%s` e `INSERT OR IGNORE` /
     `ON CONFLICT` / `AUTOINCREMENT` → equivalentes Postgres. A maioria do
     SQL da app está centralizada; a lista de sítios a rever está em
     `bot.py` (procurar `conn.execute`).
   - Reaplicar as migrações 1–7 traduzidas (mesma lista, SQL compatível).
3. Exportar os dados de SQLite e importar em Postgres (tabela a tabela;
   `sessoes`, `agendamentos`, `agendamento_historico`, `configuracoes`,
   `servicos`, `interacoes_cliente` são as que interessam — `pedidos_orcamento`
   e afins são legado Spotless e podem não ser migradas).
4. Definir `DATABASE_URL` no Render, remover o `disk:` do `render.yaml`,
   subir `--workers` no `startCommand`/`Procfile`.
5. Correr a suite de testes apontada à nova base antes de trocar o tráfego.

Enquanto o ponto 2 não estiver feito, **manter `DATABASE_URL` vazio** — a app
funciona em SQLite.
