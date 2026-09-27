# Registo de conversas (migração 25) — o TEXTO das mensagens

Peça nova da arquitectura, feita no bloqueador de 17-09-2026. Antes disto
**nenhuma** tabela guardava o que era dito: `mensagens_processadas` tem o
`wamid` (idempotência do webhook) e `interacoes_cliente` a hora da última
mensagem (janela de 24h). O painel sabia que a Sofia pediu ajuda, não o que
ela perguntou.

## Onde vive
- `db.py` — migração **25** (`registo_de_conversas`): tabela
  `mensagens_conversa` (tenant, telefone, customer_id, direcao, tipo, texto,
  id_interativo, wamid, criado_em) + 2 índices (fio e idade).
- `messaging/conversas.py` — **todo** o comportamento (gravar, ler, listar,
  janela de 24h, pedidos de humano, retenção, seed demo).
- `config.CONVERSAS_RETENCAO_MESES` (12 por omissão; 0 desliga).

## Dois pontos de escrita, os dois já existiam
- **Entrada**: `bot.receber_mensagem`, logo DEPOIS de reclamar o `wamid`
  (um retry da Meta não grava duas vezes) e ANTES de qualquer tratamento
  (há caminhos que devolvem cedo: seletor de idioma, comando de texto…).
- **Saída**: `messaging/whatsapp.py:enviar()` — ponto ÚNICO por onde passa
  texto, botões, listas, documentos, templates e o composer do painel.
  Nunca espalhar chamadas de registo pelo código.

## Três regras que não se negoceiam
1. **Gravar não parte nada** — tudo passa por `conversas._gravar`, que engole
   a excepção e deixa um aviso no log; em `whatsapp.py` há um segundo
   try/except (cobre até um erro de import). Uma cliente nunca fica sem
   resposta por causa de um INSERT.
2. **Guarda-se o que a PESSOA viu** — num toque num botão, `texto` fica com o
   título ("Limpeza de pele") e o id (`opt_1`) vai para `id_interativo`, à
   parte. Um painel cheio de `opt_1` não serve para nada.
3. **Números DEMO gravam-se** (o envio é que é bloqueado antes da Meta) — é
   isso que torna o fluxo testável sem mandar nada a sério.

Não se grava o que sai para `config.PROVIDER_WHATSAPP`: é aviso interno, não
uma conversa com uma cliente.

## Retenção
`limpar_antigas` apaga acima de `CONVERSAS_RETENCAO_MESES` (meses de 30 dias,
de propósito). `limpar_antigas_se_devido` auto-acelera para 1×/dia com o pulso
`conversas.limpeza` (`core/health`) e é chamada no topo de
`notifications/jobs.py:process_due_jobs` — o único executor periódico que
existe. Sem cron novo e sem depender de alguém se lembrar.

## Janela de 24h — o PRAZO, não o sim/não
`conversas.estado_janela_24h` devolve `{aberta, ultima_em, expira_em,
minutos_restantes}`. `bot.dentro_da_janela_24h` é só um invólucro (tem dezenas
de call sites e mocks). O painel mostra SEMPRE o prazo antes de ela escrever
("Podes responder até amanhã às 18:15") — sem isso escrevia-se uma resposta
longa para levar um 409.

## Pedido de HUMANO
A marca vive na **sessão** (`needs_human`, `needs_human_em`,
`needs_human_texto`) porque era ali que `operations/engine.py:attention_items`
já a procurava — faltava quem a escrevesse (era **código morto**: o pedido
nunca chegava ao painel). Escreve-se em `falar_com_equipa`; a pergunta é
`conversas.ultima_pergunta` (última mensagem de TEXTO, ignorando comandos e o
próprio botão de humano) e fica **gravada**, para o aviso por WhatsApp e o
cartão do painel citarem a mesma frase. Responder pelo painel FECHA o pedido
(`fechar_pedido_humano` REMOVE as chaves — `false` continuaria a acusar).
`sessao_preservando_perfil` preserva-as (todos os call sites reiniciam a
sessão logo a seguir).

## O número pessoal da Daniela
`wa_me_link` foi **apagado** (teste a travar o regresso). Todos os caminhos de
equipa — `falar_com_equipa`, "Contactar cliente", "Reagendar" — passam a
`bot.link_conversa`/`_instrucao_responder`, que mandam para
`/app#/conversas/<telefone>`. O aviso por WhatsApp mantém-se (ela está a
trabalhar, não no painel), mas é só: quem pediu, o que perguntou, responde no
painel.

## API + vista
`GET /api/conversas`, `GET /api/conversas/<telefone>` (traz `janela`,
`pedido_humano`, `demo`), `POST /api/conversas/<telefone>/mensagem`.
Indexada por TELEFONE, não por cliente: a ficha só nasce na 1.ª marcação e
quem pede ajuda antes de marcar não tem ficha. `_conversa_ou_404` é o que
permite aceitar o telefone do URL — só se responde a quem já escreveu.
O envio é partilhado com o composer da ficha (`_enviar_mensagem_livre`).
Front-end: `viewConversas` em `static/dashboard/app.js` (+ `.conv-*` no
`app.css`, nav em `shell.html`, rota `#/conversas/<telefone>`).
`conversas.seed_demo` semeia 8 fios demo (histórico longo com pedido aberto,
janela fechada, fio recém-começado) a partir de `/api/dev/seed-dashboard`.
