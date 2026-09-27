# Convenções do projecto — ler antes de escrever código

## Migrações
- Lista ordenada `MIGRACOES` no fim do `db.py`, com `schema_migrations` a registar.
- **NUNCA editar uma migração já lançada.** Acrescentar sempre uma nova no fim.
- Correm sozinhas à 1ª ligação (`garantir_migracoes`) e num `preDeployCommand`.
- Última aplicada: **25** (`registo_de_conversas`).

## Dinheiro
- Cêntimos inteiros. Nunca float, nunca `round()` sobre francos.
- A **fonte de verdade é a fatura**, não o preço da marcação (ver `dinheiro_e_faturacao`).

## Concorrência
- `BEGIN IMMEDIATE` serializa marcações e numeração de faturas.
- Testado: 8 POST simultâneos ao mesmo slot → 1 criado, 7 × 409. Não mexer nisto.
- 1 worker enquanto for SQLite. Vários workers sobre o mesmo ficheiro = "database is locked".

## Idempotência
- Webhook: `wamid` reclamado numa máquina claimed → processed/failed no teardown.
  Um retry da Meta nunca duplica nem se perde.
- `dedupe_key` nos eventos (outbox transacional em `core/events.py`).

## Segurança
- `@requer_autenticacao` (HTTP Basic) em TODAS as rotas de painel/API. Falha fechado (503 sem credenciais).
- `verificar_assinatura` no webhook: com `APP_SECRET` é obrigatória; sem ele só avisa (dev).
- Nada de segredos no código. `config.py` lê tudo do ambiente, sem defaults sensíveis.
- **Em falta**: rate limiting e headers de segurança (HSTS/CSP/X-Frame). Bloco 2.

## Testes
- `pytest`, base SQLite nova por teste (`conftest.base_dados`).
- O conftest apaga INCONDICIONALMENTE as variáveis reais do ambiente — já houve
  151 falhas fantasma e chamadas reais à Meta por causa disso. Não relaxar.
- Auth nos testes: `painel` / `painel-pw`.

## Estilo
- Comentários explicam o **porquê**, não o quê. É a melhor característica deste código — manter.
- Português nos comentários, mensagens e UI.
