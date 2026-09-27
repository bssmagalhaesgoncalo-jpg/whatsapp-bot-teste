# Estado actual e o que vem a seguir

Actualizado: 17-09-2026 (fim do dia — bloqueadores de lançamento + registo de conversas)

## Contexto
A Daniela vai **testar** o painel dentro de semanas. Há tempo para construir,
mas o que ela vir tem de estar coerente.

As mockups "DetailPro Car Detailing" que aparecem no projecto são de um cliente
de car detailing que NÃO avançou. Servem só de **referência funcional**.
Fora de âmbito: veículos, matrículas, "Box 1" (recursos paralelos), multi-tenant
a sério (`tenant_id` fica fixo em 1).

## Feito
- **Auditoria** completa — ver `AUDITORIA-2026-09-17.md` e `AUDITORIA-VISUAL-E-BACKEND.md`
- **Bloco 1** — dinheiro coerente + pagamentos parciais (ver `dinheiro_e_faturacao`)
- **Bloqueadores do lançamento (17-09-2026)** — os 5 pontos abaixo, todos fechados.
- **Último bloqueador (17-09-2026)** — registo de conversas + vista Conversas no
  painel + fim do `wa.me` (ver `registo_de_conversas`).

## Bloqueadores do lançamento — FEITOS (17-09-2026)

O link entra na bio do Instagram: chegam clientes reais, sem ensaio.

1. **O bot não pode ficar mudo sem se dar por isso** — `core/health.py` (tabela
   `system_health`, migração 23). `messaging/whatsapp.py` classifica os 4xx da
   Meta: códigos de CONTA/LIMITE (190, 102, 10, 200, 131031, 131042, 368 /
   4, 80007, 130429, 131048, 131056) viram avaria; payload inválido de uma
   mensagem só não alarma. O registo no painel acontece SEMPRE e ANTES do aviso
   por WhatsApp — se o canal está em baixo, o aviso também falha, o painel não.
   Anti-inundação: 1 aviso por tipo de problema por hora (`JANELA_AVISO_MIN`),
   guarda `_a_avisar` contra recursão. O 1.º envio OK depois de um restart
   fecha avarias antigas (`_talvez_avariado`).
2. **Automações paradas** — `/api/automacoes/correr` grava um pulso
   (`health.pulso`); `attention_items()` mostra cartão "agora" quando passam
   mais de 30 min (cron é de 5 em 5). Nunca correu = sem alarme (não se acusa
   uma avaria que pode ser só o 1.º arranque).
3. **Segurança** — `core/seguranca.py`, sem dependências novas: 10 tentativas
   falhadas por IP por minuto → 429 (contador em memória; comentado no ficheiro
   porque chega com 1 worker e o que muda com vários). Headers no
   `@app.after_request` existente: HSTS, nosniff, DENY, Referrer-Policy e CSP.
   A rota pública do PDF da fatura continua a abrir (teste a cobrir).
4. **Consentimento de marketing** — `crm/consent.py` + migração 24 (4 colunas em
   `customers`). Pergunta UMA vez, no fim da 1.ª marcação confirmada, nos 3
   idiomas; "não" é resposta gravada; a marcação nunca depende da resposta.
   `campaigns/engine.py` continua a recusar quem não tem opt-in.
5. **Limpeza do painel** — `/painel`, `/painel/hoje` e `/dashboard` → 302 para
   `/app`; cartão de atenção com contagem + 3 nomes (1 no telemóvel) + "e mais
   N"; "em curso" mostra o atraso real (`duracao_min`/`atraso_min` do backend);
   relógio do painel passa a Europe/Zurich via `Intl.DateTimeFormat`, nunca o do
   browser; chips e legenda arrumados no telemóvel; blocos de 30 min deixaram de
   ser esticados para 44px (era isso que os fazia tapar o bloco seguinte).

## Último bloqueador — registo de conversas (17-09-2026)

O número pessoal da Daniela deixou de aparecer a qualquer cliente, e o painel
passou a ter o TEXTO das mensagens. Detalhe todo em `registo_de_conversas`.

- **Migração 25** (`mensagens_conversa`) + `messaging/conversas.py`. Dois pontos
  de escrita, ambos já únicos: `bot.receber_mensagem` (entrada) e
  `messaging/whatsapp.py:enviar()` (saída). Gravar nunca parte nada: falha =
  aviso no log, o envio segue.
- Botões/listas guardam o **título** (o que ela viu) e o id à parte — nunca
  `opt_1` sozinho.
- **Retenção de 12 meses** (`CONVERSAS_RETENCAO_MESES`, 0 desliga), 1×/dia,
  dentro do `notifications/jobs.py:process_due_jobs` — não há cron novo.
- **Vista Conversas** em `#/conversas` (lista + fio + caixa de resposta).
  Mostra SEMPRE o prazo da janela de 24h antes de ela escrever
  ("Podes responder até amanhã às 18:15") — sem isso era uma armadilha.
- **`wa_me_link` apagado** (teste a travar o regresso). O aviso por WhatsApp
  mantém-se, mas é só: quem pediu, o que perguntou, "responde no painel".
- O pedido de humano entra no Centro de Atenção **com a pergunta da cliente** —
  a flag `needs_human` era código morto que ninguém escrevia.
- Capturas em `capturas-conversas/` (1440×900 e 390×844, claro e escuro).

## Estado da suite
**520 passam**, 0 falham, 1 skip. Datas de teste passaram a ser relativas
(`tests/conftest.py:dias_abertos`) — nunca mais escrever uma data à mão.

## Decisões diferentes do pedido
- **Duas migrações novas (23 e 24)**, não uma: saúde do sistema e consentimento
  são assuntos independentes.
- **CSP com Google Fonts**: o `templates/dashboard/shell.html` carrega fontes de
  CDN (fonts.googleapis/gstatic), e o `app.js` escreve `style="..."` inline —
  `style-src` precisa de `'unsafe-inline'`. `script-src` fica `'self'` puro.
- `PAINEL_HOJE_HTML`/`DASHBOARD_HTML` ficaram no `bot.py` (código morto
  assinalado) — apagá-los era mexer no monólito, fora de âmbito. São eles que
  ainda contêm duas strings `wa.me` (nunca servidas a ninguém).
- **Conversas indexadas por telefone, não por cliente**: quem pede ajuda antes
  de marcar não tem ficha. Daí `POST /api/conversas/<telefone>/mensagem` a par
  do composer da ficha, com o envio partilhado (`_enviar_mensagem_livre`).
- A API diz se o número é `demo` em vez de o painel conhecer o prefixo
  `4179998` — a regra vive num sítio só e o QA responde com a janela fechada.

## O que vem a seguir (plano `PLANO-DANIELA-V4.md`)
3. Fotos e notas por atendimento (antes/depois)
4. Relatórios
5. Resumo mensal por WhatsApp

## Dívida conhecida
- `bot.py` com 7.5k linhas — extrair as rotas do painel para um Blueprint.
- SQLite + 1 worker: tecto de escala. `MIGRATION.md` tem o caminho para Postgres.
- Cron das automações faz self-call HTTP com as credenciais do painel. Já avisa
  (ponto 2 acima), mas continua a ser um self-call.
- `.env.backup-local-cleanup` com segredos reais no disco — apagar.
- `_audit-export.tar.gz` na raiz — temporário, apagar.
