# Daniela Beauty — bot de marcações + painel

Bot WhatsApp (Cloud API da Meta) que marca serviços da **Daniela Beauty**, e um
painel web para a equipa gerir a agenda e o dia de trabalho. Um único serviço
Flask (monólito modular).

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
interna diz explicitamente que o preço ainda não foi definido. Cada serviço
tem ainda `buffer_before_min` / `buffer_after_min` (folga antes/depois) e
`rebook_days` (sugestão de reagendamento), editáveis no painel.

## Estados de uma marcação

`pending` · `confirmed` · `cancelled` · `completed` · `no_show`
(ver `estados.py`). Um **reagendamento** altera a mesma marcação e regista uma
linha em `agendamento_historico` — não existe estado "reagendado".
`BOOKING_REQUIRES_APPROVAL=true` faz as marcações entrarem como `pending`.

O **estado comercial** (`estado`) é distinto do **estado operacional**
(`op_status`: `scheduled → arrived → in_progress → done`, com carimbos
`arrived_at` / `started_at` / `completed_at`). As transições operacionais só
avançam um passo de cada vez; saltar ou recuar exige confirmação explícita
(`operations/engine.py::transicao_operacional(forcar=True)`).

Comparações de estado passam **sempre** pelas constantes de `estados.py`
(`estados.CONFIRMED`, `estados.sql_lista(*estados.ATIVOS)`, `chave_estado()`);
não há literais PT ("confirmado"/"cancelado") no caminho de execução —
`estados.normalizar()` só existe para ler linhas legadas.

## Arquitetura

| ficheiro / pacote | responsabilidade |
|-------------------|------------------|
| `bot.py` | Flask: webhook WhatsApp, fluxo, painel (`/painel` e `/dashboard`, HTML embutido), API do painel |
| `config.py` | variáveis de ambiente (sem defaults sensíveis) |
| `db.py` | ligação + migrações versionadas + CRM (`customers`) + outbox (`events`) + ocupação do dia |
| `catalogo.py` | fonte única dos serviços + formatação de preço/duração |
| `estados.py` | vocabulário canónico de estados + regra de ocupação do horário |
| `tempo.py` | "agora" e fuso `Europe/Zurich` (timezone-aware, trata DST) |
| `parsing.py` | interpretação de datas/horas/durações **legadas** (texto) |
| `core/events.py` | barramento de eventos de domínio (`registar`, `drain`) sobre a outbox |
| `scheduling/` | `business_hours.py` (horário semanal + exceções + política) e `availability.py` (motor de slots livres) |
| `operations/engine.py` | cockpit do dia: cartão operacional, transições, impacto de atrasos, itens de atenção |
| `notifications/business.py` | **ponto único** da notificação privada ao negócio (formata + envia todos os eventos) |
| `messaging/whatsapp.py` | POST à Graph API |
| `crm/`, `optimization/` | pacotes reservados (lógica CRM vive hoje em `db.py` + `notifications/`) |

### Eventos (transactional outbox)

Uma operação de domínio grava as suas linhas **e** o(s) evento(s) na mesma
transação (`db.registar_evento(conn, ...)`, tabela `events`, `dedupe_key`
UNIQUE). No fim do request (`after_request` do `/webhook` e das rotas
`/api/agendamentos/*`), `core.events.drain()` corre os handlers; um handler que
rebente não perde o evento (fica por processar e é re-tentado). Consumidor V1:
a notificação privada ao negócio. Uma marcação nova gera **exatamente uma**
notificação, pelo handler `bot._notificar_criacao_marcacao` (evento
`booking.created` / `booking.pending`) — o webhook já não envia diretamente.

Garantias de agenda:

- **zero double booking**: a verificação de conflitos e a escrita correm na
  mesma transação `BEGIN IMMEDIATE`; a duração do serviço (mais buffers)
  bloqueia o intervalo real; reservas temporárias (15 min) retêm o horário
  enquanto o cliente confirma.
- **idempotência do webhook**: `mensagens_processadas` é uma máquina de estados
  `claimed → processed | failed`. Dois webhooks concorrentes com o mesmo
  `wamid` — só um processa; um retry da Meta a seguir a um processamento
  **falhado** volta a ser processado (não se perde); a seguir a um processado,
  é descartado.
- **cancelar** liberta o horário (configurável no painel).
- **reagendar** liberta o anterior e reserva o novo **atomicamente** — se
  falhar, a marcação antiga fica intacta. O evento leva o id da linha de
  histórico como `dedupe_key`, por isso um ciclo A→B→A gera dois eventos
  distintos.

### CRM (`customers`)

Um `customer` por `(tenant_id, telefone)`. `db.recalcular_customer()` corre
depois de cada mudança de estado e usa **data local Europe/Zurich** para o
"hoje":

- `visits_count` / `spend_cents` — só marcações `completed`;
- `last_visit` — última `completed`;
- `next_visit` — próxima `confirmed`/`pending` **futura**;
- `no_show_count` / `cancel_count` — os respetivos estados.

Uma marcação futura confirmada **não** conta como visita nem gasto.

### Tenant foundation (preparação, routing NÃO ativado)

Todas as tabelas de negócio têm `tenant_id` (migração 8) e as tabelas de
identidade (`sessoes`, `interacoes_cliente`, `reservas_temporarias`,
`configuracoes`) têm **PK composta `(tenant_id, <chave>)`** (migração 15) — o
`tenant_id` faz parte da identidade, não é decorativo. Os helpers de sessão /
config / interação / reserva aceitam `tenant_id` (default `1`); o routing por
`PHONE_NUMBER_ID` (bot) e subdomínio (painel) é o passo seguinte e só precisa
de passar o `tenant_id` real nos call sites. `servicos` ainda tem `id` (slug)
global como PK — rebuild para `(tenant_id, id)` fica para o onboarding do 2.º
negócio (ver `MIGRATION.md`).

## Correr localmente

**Python:** fixado pelo ficheiro `.python-version` = **3.12.7** (o mesmo valor
está em `PYTHON_VERSION` no `render.yaml`). Não usar 3.13/3.14 — algumas wheels
ainda não cobrem essas versões.

```bash
uv venv .venv --python 3.12.7 && uv pip install -r requirements.txt   # ou venv/pip
cp .env.example .env          # preencher WhatsApp/painel; sem DATABASE_URL usa SQLite
.venv/bin/python -c "import db; db.migrar(verbose=True)"     # cria/atualiza sessoes.db
.venv/bin/flask --app bot run           # ou: gunicorn bot:app
```

- Painel operacional (dia de trabalho): `http://localhost:5000/painel`
- Painel calendário (legado, mantido): `http://localhost:5000/dashboard`
  (ambos HTTP Basic — `DASHBOARD_USER` / `DASHBOARD_PASSWORD`)
- Webhook Meta: `https://<host>/webhook` (GET verify + POST mensagens)

## Testes

```bash
.venv/bin/python -m pytest -q
```

81 testes: disponibilidade/conflitos e business hours, concorrência
(confirmações e reagendamentos), estados/histórico, timezone, assinatura e
idempotência do webhook, fundações (customers, outbox, notificação única,
tenant), cockpit operacional, validação de input da API, e o fluxo do bot
ponta-a-ponta.

## Variáveis de ambiente

Ver `.env.example`. Obrigatórias para o bot: `WHATSAPP_TOKEN`,
`PHONE_NUMBER_ID`, `VERIFY_TOKEN`, `PROVIDER_WHATSAPP`. Para o painel:
`DASHBOARD_USER`, `DASHBOARD_PASSWORD`. Recomendado em produção: `APP_SECRET`
(valida a assinatura `X-Hub-Signature-256` dos webhooks).

## Deploy (Render)

Ver `render.yaml` e `MIGRATION.md`. **Enquanto usar SQLite é obrigatório um
disco persistente** (o filesystem do Render é efémero). As migrações correm no
`preDeployCommand`.

- **Python:** `.python-version` (raiz) fixa **3.12.7**; `PYTHON_VERSION=3.12.7`
  no `render.yaml` repete o valor. Não há `runtime.txt` (evita ter três sítios
  a dizer a versão).
- **Dependências:** `Flask`, `gunicorn`, `requests` — nada mais. Não há
  `psycopg` (PostgreSQL não é suportado nesta versão; `db.py` aborta se
  `DATABASE_URL` for Postgres). Volta quando o backend Postgres existir de
  facto — ver `MIGRATION.md`.
