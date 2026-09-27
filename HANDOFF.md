# HANDOFF — whatsapp-bot-render
Última sessão: 27-09-2026 · Próximo passo: **bloco 3 (fotos e notas por atendimento)**

Se estás a retomar este projecto, lê isto e depois `.serena/memories/`.

---

## Em duas linhas
Bot de marcações por WhatsApp + painel para a **Daniela Beauty** (estética, Visp).
Ela vai testar dentro de semanas. O código é sólido; falta polir o que ela vai ver.

## O que aconteceu nesta sessão

**1. Auditoria estática** → `AUDITORIA-2026-09-17.md`
Segurança acima da média (HMAC no webhook, idempotência por `wamid`, auth em todas
as 45 rotas, zero SQL injection, migrações versionadas). Os problemas são de
manutenibilidade.

**2. Auditoria com a app a correr** → `AUDITORIA-VISUAL-E-BACKEND.md`
App arrancada com seed demo, 16 screenshots (desktop/mobile, light/dark), sondas à API.
Descobriu o bug B1 e confirmou que a concorrência de marcações funciona mesmo
(8 POST simultâneos → 1 criado, 7 × 409).

**3. Plano** → `PLANO-DANIELA-V4.md` — 5 blocos, cada um entregável sozinho.

**4. Bloco 1 feito** — dinheiro coerente + pagamentos parciais.
570 linhas em 6 ficheiros + `tests/test_pagamentos.py` novo. Migrações 21 e 22.

### O bug que estava lá (B1)
`db.recalcular_customer` somava `agendamentos.preco_cents` e **ignorava as faturas**.
A Julia Brunner tinha uma fatura de CHF 50 **paga** e o painel mostrava **CHF 0,00**.
Havia duas fontes de verdade para dinheiro. Agora só há uma: a fatura.

---

## Como retomar

```bash
# correr local
export DASHBOARD_USER=... DASHBOARD_PASSWORD=... SESSOES_DB=./sessoes.db ENABLE_DEMO_SEED=1
python3 -m flask --app bot run --port 5055
curl -u user:pw -X POST localhost:5055/api/dev/seed-dashboard   # 131 marcações demo

# testes
python3 -m pytest -q          # 434 passam, 3 falham (datas hardcoded, ver abaixo)
```

O painel vivo é **`/app`**. `/painel` e `/dashboard` são versões antigas ainda
servidas — desligá-las é o primeiro item do bloco 2.

### Ver o painel durante o desenvolvimento
Arrancar o Flask e usar Playwright headless para tirar screenshots em
1440×900 e 390×844, light e dark, e depois OLHAR para os PNG. Foi assim que
se apanhou o "Atrasado 0 min", os 53 nomes e o `/dashboard` preso no escuro —
coisas que nenhum teste apanha.

---

## Bloco 2 — a lista, por ordem

1. `/painel` e `/dashboard` → 302 para `/app`
2. Cartão "atenção": 53 nomes em texto corrido → contagem + 3 nomes + "e mais N"
3. Mobile: chips cortados, legenda a ocupar um ecrã, grelha a transbordar
4. Cartão "em curso": limitar o decorrido à duração, mostrar o atraso real
5. Linha "Agora" em Europe/Zurich, não no relógio do browser (`app.js` ~1147 e ~1311)
6. Blocos de 30 min com altura mínima
7. Headers de segurança + rate limiting no Basic
8. As 3 datas hardcoded dos testes

## Antes de commitar
- [ ] `python3 -m pytest -q` — só as 3 falhas conhecidas (ou zero, depois do item 8)
- [ ] Screenshots das páginas que mexeste, nos dois temas e nos dois tamanhos
- [ ] Migração nova numerada, nunca editar uma já lançada

## Para limpar
- `.env.backup-local-cleanup` — segredos reais no disco
- `_audit-export.tar.gz` — temporário desta sessão
- `graphify-out/` é de **4 de Setembro**, anterior a tudo isto — regenerar

---

## Sessão 27-09-2026 — bloco 2 fechado

- Verificado que 2.1–2.7 já estavam feitos nos commits de 17/09 (redirects,
  "e mais N", mobile, atraso real, fuso Zurique, altura mínima, seguranca.py).
- Novo: raiz `/` → 302 `/app` (dava 404 e parecia o site em baixo) + teste.
- 2.8: os 3 testes do seed falhavam ao domingo/madrugada — não eram datas
  hardcoded, era o relógio real: fixture autouse em test_demo_seed.py congela
  agora_zurique() na próxima terça às 13h. **Suite: 517 passed, 0 failed.**
- Deploy live: https://whatsapp-bot-teste-ch2c.onrender.com (plano Free, SEM
  disco → dados apagam-se em cada deploy; passar a Starter antes da Daniela).
- Limpar do repo: `bmsalgo-src.tgz` (400 KB, entrou por engano no commit 34fa500).
