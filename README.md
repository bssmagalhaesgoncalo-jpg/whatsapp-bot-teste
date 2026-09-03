# Daniela Beauty — bot de marcações + painel

Bot WhatsApp (Cloud API da Meta) que marca serviços da **Daniela Beauty**, e um
painel web para a equipa gerir a agenda. Um único serviço Flask.

## Fluxo do cliente

```
idioma → serviço → dia → hora disponível → resumo → confirmar
       → marcação criada (o horário fica indisponível) → confirmação
```

O cliente pode ainda **consultar**, **reagendar** e **cancelar** a marcação.
Comandos em texto livre a qualquer momento: `MENU`, `VOLTAR`, `CANCELAR`,
`AJUDA`, `HUMANO`, `GERIR`, `IDIOMA`.

## Serviços (fonte única: tabela `servicos`, semeada de `catalogo.py`)

| id | nome | duração | preço |
|----|------|---------|-------|
| `limpeza_pele` | Limpeza de pele | 60 min | CHF 80 |
| `design_sobrancelhas` | Design de sobrancelhas | 30 min | CHF 25 |
| `brow_lamination` | Brow Lamination | 60 min | *a confirmar* |
| `pestanas` | Pestanas | 120 min | *a confirmar* |
| `dermaplaning` | Dermaplaning | 60 min | *a confirmar* |

Preço "a confirmar" (`preco_cents = NULL`) nunca aparece como `CHF 0`: o
cliente vê "Preço a confirmar", o painel vê "A confirmar" e a notificação
interna diz explicitamente que o preço ainda não foi definido.

## Estados de uma marcação

`pending` · `confirmed` · `cancelled` · `completed` · `no_show`
(ver `estados.py`). Um **reagendamento** altera a mesma marcação e regista uma
linha em `agendamento_historico` — não existe estado "reagendado".
`BOOKING_REQUIRES_APPROVAL=true` faz as marcações entrarem como `pending`.

## Arquitetura

| ficheiro | responsabilidade |
|----------|------------------|
| `bot.py` | Flask: webhook WhatsApp, fluxo, painel (HTML embutido), API do painel |
| `config.py` | variáveis de ambiente (sem defaults sensíveis) |
| `db.py` | ligação + migrações versionadas (SQLite agora, Postgres-ready) |
| `catalogo.py` | fonte única dos serviços + formatação de preço/duração |
| `estados.py` | vocabulário canónico de estados + regra de ocupação do horário |
| `tempo.py` | "agora" e fuso `Europe/Zurich` (timezone-aware, trata DST) |
| `parsing.py` | interpretação de datas/horas/durações **legadas** (texto) |

Garantias de agenda:

- **zero double booking**: a verificação de conflitos e a escrita correm na
  mesma transação `BEGIN IMMEDIATE`; a duração do serviço bloqueia o intervalo
  real (um serviço de 2h bloqueia 2h); reservas temporárias (15 min) retêm o
  horário enquanto o cliente confirma; idempotência por `wamid`.
- **cancelar** liberta o horário (configurável no painel).
- **reagendar** liberta o anterior e reserva o novo **atomicamente** — se
  falhar, a marcação antiga fica intacta.

## Correr localmente

```bash
python -m venv .venv && . .venv/bin/activate      # ou: uv venv
pip install -r requirements.txt
cp .env.example .env          # preencher WhatsApp/painel; sem DATABASE_URL usa SQLite
python -c "import db; db.migrar(verbose=True)"     # cria/atualiza sessoes.db
flask --app bot run           # ou: gunicorn bot:app
```

- Painel: `http://localhost:5000/dashboard` (HTTP Basic — `DASHBOARD_USER` / `DASHBOARD_PASSWORD`)
- Webhook Meta: `https://<host>/webhook` (GET verify + POST mensagens)

## Testes

```bash
pip install pytest
pytest -q
```

31 testes: disponibilidade/conflitos, concorrência (confirmações e
reagendamentos), estados/histórico, timezone, assinatura do webhook, e o
fluxo do bot ponta-a-ponta.

## Variáveis de ambiente

Ver `.env.example`. Obrigatórias para o bot: `WHATSAPP_TOKEN`,
`PHONE_NUMBER_ID`, `VERIFY_TOKEN`, `PROVIDER_WHATSAPP`. Para o painel:
`DASHBOARD_USER`, `DASHBOARD_PASSWORD`. Recomendado em produção: `APP_SECRET`
(valida a assinatura `X-Hub-Signature-256` dos webhooks).

## Deploy (Render)

Ver `render.yaml` e `MIGRATION.md`. **Enquanto usar SQLite é obrigatório um
disco persistente** (o filesystem do Render é efémero). PostgreSQL é o destino
de produção — a camada está preparada; a migração de dados é um passo à parte
(`MIGRATION.md`).

## Multi-negócio (preparação, não implementado)

A identidade vem toda de `BUSINESS_NAME` / `BUSINESS_ADDRESS` (ambiente) e o
catálogo é uma tabela. O próximo passo para vários negócios é uma coluna
`tenant_id` nas tabelas de negócio + resolução por `PHONE_NUMBER_ID` (bot) e
subdomínio (painel) — ver auditoria, Fase 5.
