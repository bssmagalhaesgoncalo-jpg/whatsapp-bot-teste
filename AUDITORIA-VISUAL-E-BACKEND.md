# Auditoria com a app a correr — whatsapp-bot-render
Data: 2026-09-17 · Método: app arrancada com seed demo (131 marcações, 37 clientes,
35 faturas), 16 screenshots via Playwright em desktop 1440×900 e mobile 390×844,
light + dark, mais sondas directas à API.

---

# PARTE 1 — Frontend / visual

O painel V3 (`/app`) é bom. Tipografia, espaçamento, dark mode e os estados por cor
estão ao nível de um SaaS pago. Os problemas são de **densidade de informação** e
**comportamento em mobile**, não de estética.

## 🔴 Crítico

**F1. O cartão "53 preços por confirmar" despeja 53 nomes em texto corrido**
No desktop ocupa 1/4 do ecrã; no mobile ocupa **duas páginas inteiras** de scroll antes
de se chegar à agenda do dia. E o botão "Ver" está posicionado a meio do bloco de texto,
a flutuar sobre os nomes.
→ Mostrar contagem + 3 nomes + "e mais 50". O "Ver" abre a lista.

**F2. `/app` é a única UI viva — `/painel` e `/dashboard` são versões antigas ainda servidas**
- `/painel/hoje`: visual antigo, botões com emojis (✅ ⏰ 👤), só light.
- `/dashboard`: calendário antigo, **só dark** — ignora `prefers-color-scheme` e mostra-se
  escuro mesmo em light mode.
Três UIs para a mesma coisa, todas autenticadas e acessíveis. Quem abrir o link errado
vê um produto pior. O comentário no `dashboard/__init__.py` já diz "mantêm-se até esta
versão estar validada" — está validada.
→ Redireccionar `/painel` e `/dashboard` para `/app`.

**F3. Mobile: a agenda transborda o ecrã**
- Os chips de filtro cortam a meio ("Em curs…") sem scroll horizontal visível.
- A legenda de 6 estados ocupa um ecrã inteiro — no telemóvel devia estar colapsada.
- A grelha do calendário sai pela direita, com os blocos cortados.
Resultado: ~1,5 ecrãs de cromo antes da primeira marcação.

## 🟠 Importante

**F4. "Decorrido 10h31 · Atrasado 0 min" — em cima de "A PASSAR DA HORA"**
Marcação das 09:00→11:00, agora 17:31. O `decorrido_min` não é limitado à duração da
marcação, e o `restante_min` vem clamped a 0, pelo que o cartão diz ao mesmo tempo que
está atrasada e que faltam 0 min. A barra de progresso está sempre a 100 % porque
`total = decorrido + restante` (`app.js:730`).
→ Limitar o decorrido à duração e mostrar o atraso real ("atrasada 6h31").

**F5. A linha "Agora" usa o relógio do browser, não o fuso do negócio**
`app.js:1147` e `:1311` — `new Date().getHours()`. O servidor está preso a
`TZ=Europe/Zurich`, o cliente não. Nos screenshots a linha aparece no topo da grelha
(08:00) em vez da hora real. Um telemóvel noutro fuso desenha a linha no sítio errado.
→ Calcular o "agora" em Europe/Zurich (`Intl.DateTimeFormat` com timeZone).

**F6. Blocos de 30 min com o texto cortado**
Na vista Dia, 13:30 Carolina e 16:00 Helena perdem a linha do serviço — o bloco seguinte
tapa-a. Sem altura mínima nem `overflow` tratado.

## 🟡 Menor

- Desktop 1440: o conteúdo pára aos ~1180 px e deixa uma faixa vazia à direita — a tabela
  de Faturas podia respirar ou mostrar mais colunas.
- `app.js` tem 136 KB e `app.css` 50 KB, servidos sem minificação (o cache-bust por hash
  já está bem feito).
- `innerHTML` com interpolação em `app.js:733` — único caso; conteúdo interno, não
  explorável, mas é o padrão a não deixar crescer.

---

# PARTE 2 — Backend

## 🔴 Crítico

**B1. Duas fontes de verdade para dinheiro — o "Gasto" do cliente mentira**
`db.recalcular_customer()` (db.py:1079) soma `agendamentos.preco_cents` das marcações
concluídas. **Ignora as faturas por completo.**

Prova, com os dados demo:
| Cliente | Faturas na BD | "Gasto" no painel |
|---|---|---|
| Julia Brunner | CHF 50,00 **paga** | **CHF 0,00** |
| Andreia Pinto | CHF 60,00 paga + CHF 80,00 emitida | CHF 80,00 |

O mesmo erro aparece no `/dashboard` antigo: "Receita estimada **CHF 0**" com 18 faturas
pagas na base. Assim que o módulo de faturação entrou, o contador de gasto deixou de
querer dizer alguma coisa.
→ Decidir qual é a fonte de verdade (as faturas) e derivar o gasto de lá, ou mostrar
os dois campos separados ("valor das marcações" vs "facturado/pago").

## ✅ Confirmado a funcionar (testado, não lido)

**Concorrência de marcações**: 8 pedidos POST simultâneos para o mesmo slot →
**1 criado (201), 7 rejeitados (409)**. O `BEGIN IMMEDIATE` faz o que promete. Nenhuma
marcação duplicada.

**Validação de entrada**: serviço inexistente → 400 limpo; slot ocupado → 409 com
mensagem legível. Sem stack traces expostos.

**Migrações**: base criada de raiz e migrada sem erro, seed de 131 marcações
instantâneo.

## 🟠 Confirmado da auditoria estática (mantém-se)

1. **3 testes falham por datas hardcoded** (`test_fluxo_bot`, `test_estados_e_historico`)
2. **Sem rate limiting** no HTTP Basic
3. **Sem headers de segurança** (HSTS, X-Frame-Options, CSP)
4. `bot.py` com 7 504 linhas
5. `.env.backup-local-cleanup` com segredos no disco
6. SQLite + 1 worker
7. Cron das automações via self-call HTTP que falha em silêncio

---

# Prioridade

1. **B1** — o número de gasto está errado no ecrã do cliente (é o que se vê primeiro)
2. **F1** — os 53 nomes tornam o mobile inutilizável
3. **F2** — desligar as duas UIs antigas
4. **F4 + F5** — o cartão "em curso" e a linha "agora" estão ambos errados no tempo
5. **F3** — overflow no mobile
6. Depois, a lista de backend: testes → rate limiting → headers
