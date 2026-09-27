# Prompt para o Claude Code — bloqueadores de lançamento

Copia tudo o que está abaixo da linha para uma sessão nova na pasta do projecto.

---

Trabalhas no `whatsapp-bot-render`: bot de marcações por WhatsApp + painel Flask
para a Daniela Beauty (estética, Visp, Suíça). Lê primeiro `.serena/memories/`
(são quatro ficheiros) e o `HANDOFF.md` — têm a arquitectura, as convenções e o
estado actual.

**Contexto que muda tudo:** o link do bot vai para a bio do Instagram dela (1660
seguidores) e entram clientes REAIS. Não há fase de ensaio. As cinco tarefas
abaixo são bloqueadores de lançamento — sem elas o bot não deve ir para o ar.

## Regras do projecto (não negociáveis)
- Dinheiro em cêntimos inteiros, nunca float.
- Migrações numeradas no fim de `db.py`; **nunca editar uma já lançada**, sempre
  acrescentar. A última é a 22.
- Código novo nasce nos módulos (`notifications/`, `billing/`, `messaging/`…),
  **nunca** no `bot.py` — já tem 7 500 linhas.
- Comentários explicam o **porquê**, não o quê. É a melhor característica deste
  código; mantém-na.
- Português nos comentários, mensagens e UI.
- `python3 -m pytest -q` está em **434 passam / 3 falham**. As 3 falhas são datas
  hardcoded e são a tarefa 5. Não deixes a contagem piorar.

Faz as tarefas **por esta ordem** e corre a suite entre cada uma. Se alguma te
parecer errada quando lá chegares, diz porquê em vez de a fazeres à força.

---

## Tarefa 1 — O bot não pode ficar mudo em silêncio

**Problema:** em `messaging/whatsapp.py`, `enviar()` levanta excepção em falha de
rede (bom: a Meta repete e a mensagem sai), mas quando a Meta devolve **4xx**
apenas escreve um aviso no log e devolve a resposta. A mensagem da cliente fica
marcada como processada e ela **não recebe nada**. Ninguém dá por isso.

Isto acontece quando o token expira, quando o número é suspenso ou quando se
atinge um limite — e o link continua no Instagram a mandar clientes para lá.

**O que fazer:**
- Distinguir os 4xx que são falha de configuração/conta (401, 403, 190 token
  inválido, número suspenso, limite atingido) de um 4xx de payload inválido de
  uma mensagem específica.
- Nos primeiros, avisar a Daniela: mensagem WhatsApp para `PROVIDER_WHATSAPP`
  **e** uma entrada visível no Attention Center do painel. Cuidado com o ciclo
  óbvio — se o WhatsApp está em baixo, o aviso por WhatsApp também falha; o
  registo no painel tem de acontecer de qualquer maneira.
- Não inundar: um aviso por tipo de problema por período (guarda o estado, à
  maneira do `dedupe_key` que já existe nos eventos).

**Feito quando:** um teste simula a Meta a devolver 401 e prova que fica registo
visível no painel, e que não há um segundo aviso para o mesmo problema seguido.

## Tarefa 2 — Avisar quando as automações não correm

**Problema:** o cron do Render faz POST a `/api/automacoes/correr`. Se falhar —
serviço a dormir, 5xx, password mudada — os lembretes não saem e **nada avisa**.
O `raise_for_status()` só rebenta no log do cron, que ninguém lê.

**O que fazer:** guardar a hora da última execução com sucesso e mostrar no
painel quando passou demasiado tempo (o cron corre de 5 em 5 minutos; mais de
~30 min sem correr é sinal de avaria). Um cartão no Attention Center chega —
é onde ela já olha.

**Feito quando:** com o relógio adiantado no teste, o painel mostra o aviso.

## Tarefa 3 — Rate limiting e headers de segurança

**Problema:** o painel tem nomes, telemóveis, histórico e faturas de clientes
reais, atrás de um HTTP Basic sem limite de tentativas e sem um único header de
segurança.

**O que fazer:**
- Limitar as tentativas de autenticação falhadas por IP (algo como 10/min; ao
  ser excedido, 429). Sem dependências novas se der: um contador em memória
  chega para 1 worker, mas deixa comentado porque é que é suficiente **agora** e
  o que muda quando forem vários workers.
- No `@app.after_request` que já existe: `Strict-Transport-Security`,
  `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`,
  `Referrer-Policy: strict-origin-when-cross-origin` e um `Content-Security-Policy`
  que não parta o painel (o `app.js` é servido do próprio domínio, não há CDN).
- **Não** metas headers que quebrem a rota pública do PDF da fatura.

**Feito quando:** um teste confirma o 429 depois de N tentativas e a presença
dos headers; a suite toda continua verde.

## Tarefa 4 — O bot pede consentimento de marketing

**Problema:** `marketing_opt_in` só se liga à mão no painel. A cliente nunca é
perguntada. Assim, as campanhas de reativação são inutilizáveis: obrigariam a
Daniela a marcar à mão caixas por pessoas que nunca disseram que sim — contra a
política da Meta e frágil face à protecção de dados suíça.

**O que fazer:**
- **Uma vez**, no fim da primeira marcação concluída com sucesso, perguntar à
  cliente se aceita receber mensagens sobre novidades e lembretes de reagendar.
  Nos três idiomas.
- Gravar a resposta com data e origem (migração nova — a última é a 22).
  Um "não" é uma resposta gravada, não a ausência de um "sim": nunca voltar a
  perguntar a quem já respondeu.
- Não atrapalhar a marcação. A confirmação vem primeiro; a pergunta é um extra
  no fim e ignorá-la não parte nada.
- Confirmar que `campaigns/engine.py` continua a recusar quem não tem opt-in
  (já recusa — não partir isso).

**Feito quando:** testes provam que se pergunta uma só vez, que um "não" é
persistente, e que a marcação funciona na mesma se a cliente nunca responder.

## Tarefa 5 — Limpeza do painel (o que ela e as clientes vão ver)

Detalhe completo em `.serena/memories/estado_e_proximos_passos.md`, secção
"Bloco 2". Resumo:

1. `/painel` e `/dashboard` → redirect 302 para `/app`. São UIs antigas ainda
   servidas; `/dashboard` ignora o modo claro e aparece sempre escuro.
2. Cartão "precisa da tua atenção": despeja **53 nomes** em texto corrido (duas
   páginas de scroll no telemóvel) e o botão "Ver" flutua por cima do texto.
   Passar a contagem + 3 nomes + "e mais N".
3. Mobile: chips de filtro cortados sem scroll visível, legenda de 6 estados a
   ocupar um ecrã inteiro, grelha da agenda a sair pela direita.
4. Cartão "em curso" diz **"Decorrido 10h31 · Atrasado 0 min"** ao mesmo tempo
   que "A PASSAR DA HORA". O `decorrido_min` não é limitado à duração e o
   `restante_min` vem clamped a 0; a barra está sempre a 100% porque
   `total = decorrido + restante` (`app.js` ~730). Mostrar o atraso real.
5. Linha "Agora" usa `new Date().getHours()` (`app.js` ~1147 e ~1311) — relógio
   do BROWSER, não Europe/Zurich como o servidor. Corrigir com `Intl.DateTimeFormat`.
6. Blocos de 30 min com o texto tapado pelo bloco seguinte: altura mínima e
   overflow tratado.
7. As **3 datas hardcoded** dos testes (`tests/test_fluxo_bot.py`,
   `tests/test_estados_e_historico.py`): `2026-09-14/15` já passaram. Datas
   relativas ou congelar o relógio. Depois disto a suite fica a **zero falhas**.

---

## Como verificar o que é visual

Testes não apanham nada disto. Depois de mexeres na UI:

```bash
export DASHBOARD_USER=x DASHBOARD_PASSWORD=y SESSOES_DB=./teste.db ENABLE_DEMO_SEED=1
python3 -m flask --app bot run --port 5055
curl -u x:y -X POST localhost:5055/api/dev/seed-dashboard   # 131 marcações demo
```

Depois usa Playwright headless para tirar screenshots de `/app` em **1440×900 e
390×844, light e dark**, e **olha para os PNG**. Foi assim que se apanhou o
"Atrasado 0 min" e o `/dashboard` preso no escuro.

**Atenção:** o `flask run` carrega o `.env` sozinho (python-dotenv). Com o `.env`
real presente, um teste manda WhatsApps **verdadeiros** para números
**verdadeiros**. Usa um `.env` de teste, ou números com o prefixo `4179998` —
`messaging/whatsapp.py` nunca os deixa chegar à Meta.

## No fim
- `python3 -m pytest -q` a zero falhas.
- Diz o que mudaste por tarefa, e o que decidiste diferente do que está aqui e porquê.
- Actualiza `.serena/memories/estado_e_proximos_passos.md` com o estado novo.

## Fora de âmbito
Não mexas na faturação nem nos pagamentos (acabaram de ser feitos), não faças
refactor do `bot.py`, não migres para Postgres, e não toques nos 12 templates da
Meta — esses são tratados fora do código.
