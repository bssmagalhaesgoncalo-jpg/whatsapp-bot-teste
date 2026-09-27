# Auditoria — whatsapp-bot-render (Daniela Beauty)
Data: 2026-09-17 · Âmbito: geral (arquitectura, correcção, segurança, dependências, testes)

## Veredicto

Projecto sólido e invulgarmente bem documentado para um projecto pessoal. A postura de
segurança está acima da média (fail-closed em todo o lado, HMAC do webhook, auth em 100%
das rotas sensíveis, zero SQL injection). Os problemas são de **manutenibilidade** e de
**robustez operacional**, não de desenho.

Estado: 417 testes a passar, 3 a falhar, 1 skip.

---

## 🔴 Crítico

**1. Suite de testes com datas hardcoded — bombas-relógio**
3 testes falham hoje só porque a data passou:
- `tests/test_estados_e_historico.py` (`2026-09-10`/`2026-09-11`)
- `tests/test_fluxo_bot.py` (`2026-09-14`/`2026-09-15`) → `HorarioNoPassado`

Não é um bug de produção, mas mata a confiança na suite: daqui a um mês falham mais.
**Correcção:** datas relativas (`hoje + N dias`) ou congelar o relógio numa fixture
(`freezegun` / monkeypatch de `tempo.agora_zurique`).

**2. Sem rate limiting no HTTP Basic do painel**
`/painel`, `/dashboard` e toda a `/api/*` aceitam tentativas ilimitadas. Uma password fraca
no Render cai a brute-force. **Correcção:** `Flask-Limiter` (ex.: 10 tentativas/min por IP)
ou, melhor, pôr o painel atrás de um proxy com protecção.

## 🟠 Importante

**3. `bot.py` com 7 504 linhas (381 KB)**
É 1/3 de todo o código. O resto do projecto está bem modularizado (`billing/`, `campaigns/`,
`notifications/`, `scheduling/`) — o `bot.py` ficou com tudo o resto: 45 rotas, fluxo
conversacional, HTML do painel, seed demo. Qualquer alteração aqui tem raio de impacto
enorme. **Correcção incremental:** extrair primeiro as rotas do painel para
`dashboard/routes.py` (Blueprint), depois o seed demo para `dev/seed.py`.

**4. Sem headers de segurança HTTP**
Nenhum `Strict-Transport-Security`, `X-Frame-Options`, `X-Content-Type-Options`,
`Content-Security-Policy`. O painel mostra dados de clientes reais (nomes, telefones,
faturas) — vale 10 linhas no `@app.after_request` que já existe.

**5. `.env.backup-local-cleanup` com segredos reais no disco**
Está fora do git (ok, `.env.*` ignorado), mas é um ficheiro de credenciais esquecido numa
pasta do Desktop. Apagar.

**6. SQLite + 1 worker = tecto de escala já atingido**
O `Procfile` e o `render.yaml` documentam-no honestamente, mas na prática: 8 threads sobre
um ficheiro SQLite, sem réplica, num disco de 1 GB. O `MIGRATION.md` existe — o Postgres
devia deixar de ser "fase seguinte" antes de haver volume a sério.

**7. Cron das automações depende de HTTP self-call com as credenciais do painel**
`render.yaml` faz POST a `/api/automacoes/correr` com `DASHBOARD_USER/PASSWORD`. Se o
serviço web estiver a dormir ou a responder 5xx, os reminders 24h simplesmente não saem e
nada avisa. **Correcção:** alertar ao falhar (o `raise_for_status()` só rebenta no log do
cron) ou correr os jobs num worker que fale directo com a BD.

## 🟡 Menor

- **`.qa/` (8,9 MB), `graphify-out/` (2,7 MB), `.playwright-cli/` (3,8 MB), `test-results/`**
  — 15 MB de artefactos de QA na working tree. `.qa/` e `.playwright-cli/` estão ignorados;
  `graphify-out/`, `test-results/`, `skill-observations/` e os `geo-*.png` aparecem como
  untracked no `git status`. Limpar ou ignorar.
- **`sessoes.db` (380 KB) na pasta** — base local com dados de clientes. Ignorada pelo git,
  mas vale a pena confirmar que não é uma cópia de produção.
- **Sem dependências de desenvolvimento declaradas** — `requirements.txt` tem só 3 pacotes
  (correcto para runtime), mas não há `requirements-dev.txt` com o `pytest`. Quem clonar não
  consegue correr a suite sem adivinhar.
- **Sem CI** — 417 testes que ninguém corre automaticamente. Um GitHub Action de 15 linhas
  apanhava já hoje as 3 falhas.
- **`innerHTML` com interpolação em `static/dashboard/app.js:733`** — o único caso onde a
  string não é 100 % literal; o conteúdo é interno (estado "Em curso"), logo não é
  explorável, mas é o padrão a não deixar crescer. O resto do ficheiro usa `append()` com
  nós — bem feito.
- **Rota pública `/faturas/pdf/<token>`** — desenho correcto (token de 24 bytes via
  `secrets.token_urlsafe`), mas o token não expira nem é revogável. Para faturas, aceitável;
  vale a pena documentar a decisão.

---

## O que está bem feito (e não se deve mexer)

- **Segurança de webhook**: HMAC-SHA256 com `compare_digest`, fail-closed sem `VERIFY_TOKEN`,
  idempotência por `wamid` com máquina de estados claimed/processed/failed no teardown do
  request — retries da Meta não duplicam nem se perdem. É desenho de quem já levou porrada.
- **Auth**: as 45 rotas sensíveis têm `@requer_autenticacao` sem uma única excepção;
  credenciais em falta → 503, nunca acesso aberto.
- **SQL**: zero interpolação de input do utilizador. Todos os f-strings em queries são
  constantes internas (listas de colunas, nomes de tabela em migrações).
- **Migrações versionadas** com `schema_migrations` e ligações que fecham — o comentário no
  topo do `db.py` sobre o que a versão antiga fazia mal é exemplar.
- **Config sem defaults sensíveis**, `render.yaml` com tudo `sync: false`.
- **Comentários que explicam o *porquê*** (o `Procfile` sobre `--workers 1`, o `PYTHON_VERSION`
  redundante de propósito). Raro e valioso.

## Prioridade sugerida

1. Corrigir as datas dos 3 testes + adicionar CI (meia hora, protege tudo o resto)
2. Headers de segurança + rate limiting no painel (uma hora)
3. Apagar `.env.backup-local-cleanup`, limpar artefactos de QA
4. Extrair as rotas do painel do `bot.py` para um Blueprint
5. Planear Postgres antes do volume o exigir
