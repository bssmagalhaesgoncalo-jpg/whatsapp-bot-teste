# Checkpoint — o processo manual da Daniela está quanto automatizado?
17-09-2026 · Medido contra o objectivo real, não contra a qualidade do código

---

## O dia dela, passo a passo

| # | O que ela faz hoje à mão | Automatizado? | Notas |
|---|---|---|---|
| 1 | Responder a quem pede marcação | ✅ **Sim** | Bot em PT/DE/EN, menu de serviços |
| 2 | Propor horas livres | ✅ **Sim** | Considera duração, horário, pausas, buffers |
| 3 | Evitar marcar duas ao mesmo tempo | ✅ **Sim, testado** | 8 pedidos simultâneos → 1 entra, 7 recusados |
| 4 | Apontar na agenda | ✅ **Sim** | Entra sozinho, visível em `/app` |
| 5 | Confirmar à cliente | ✅ **Sim** | Confirmação imediata |
| 6 | Lembrar na véspera | ⚠️ **Construído, não ligado** | Precisa de template aprovado pela Meta |
| 7 | Remarcar / cancelar | ✅ **Sim** | Pela cliente e pelo painel, com confirmação |
| 8 | Marcar como concluído | ✅ **Sim** | Estado operacional (chegou / em curso / feito) |
| 9 | Passar fatura | ✅ **Sim** | Gerada ao concluir, numeração sem buracos |
| 10 | Enviar a fatura | ⚠️ **Sim, com condição** | PDF por WhatsApp; exige `PUBLIC_BASE_URL` |
| 11 | Registar quanto recebeu | ✅ **Sim (novo)** | Sinal + resto, TWINT/numerário/cartão/transferência |
| 12 | Saber quem ainda deve | ✅ **Sim (novo)** | "Por receber" na lista e na ficha |
| 13 | Guardar fotos antes/depois | ❌ **Não existe** | O bot nem sequer aceita imagens |
| 14 | Notas do atendimento | ❌ **Não existe** | — |
| 15 | Chamar quem não volta há muito | ⚠️ **Construído, não ligado** | Follow-up + campanhas; mesmo bloqueio de template |
| 16 | Ver se o mês foi bom | ⚠️ **Fraco** | Página Resultados é magra; sem facturado/pago/ocupação |

**Contagem: 9 a funcionar · 4 construídos mas por ligar · 3 por fazer.**

O núcleo — **marcar, agendar, cobrar** — está feito e é sólido. O que falta
é a camada que a faz voltar ao sistema todos os dias.

---

## 🔴 Bloqueadores para ela testar

### 1. Os templates da Meta não estão em lado nenhum
`config.py` espera **12 variáveis** de template (reminder, rebooking,
reagendamento e campanhas × PT/DE/EN). Nenhuma está no `render.yaml` nem no
`.env.example`.

Sem elas, tudo o que sai da janela de 24h **fica "failed" em silêncio**:
o reminder da véspera nunca dispara, as campanhas não saem. O código está
certo — recusa-se a fingir um envio — mas ninguém sabe que tem de os criar.

**E isto não se resolve em código**: os templates têm de ser escritos, submetidos
à Meta e **aprovados** — leva dias. É o item de maior prazo de todo o projecto.
Começar já.

### 2. Testar localmente envia WhatsApps a sério
O `flask run` carrega o `.env` sozinho (python-dotenv, instalado em dev mas
ausente do `requirements.txt`). Com o `.env` real presente, cada clique num teste
manda uma mensagem verdadeira para um número verdadeiro.

Foi exactamente o que aconteceu hoje ao simular uma marcação: a app tentou
chamar `graph.facebook.com` com o token real.

**Precisa de um modo de ensaio** — uma variável tipo `WHATSAPP_DRY_RUN=1` que
faça o envio registar no log em vez de sair. Uma hora de trabalho, evita mandar
mensagens à toa para clientes reais durante os testes.

### 3. O que ela vai ver ainda tem arestas
Tudo isto está no bloco 2 e é meio dia de trabalho:
- `/painel` e `/dashboard` antigos ainda vivos (um deles ignora o modo claro)
- 53 nomes em texto corrido no cartão de atenção — duas páginas de scroll no telemóvel
- "Decorrido 10h31 · Atrasado 0 min" no cartão do atendimento em curso
- Linha "Agora" no fuso do browser, não no de Zurique
- Agenda a transbordar no telemóvel

### 4. Sem disco persistente, os dados dela desaparecem
O `render.yaml` já monta um disco em `/var/data` e o plano é pago — mas é preciso
**confirmar que está mesmo activo** no Render. O filesystem do Render é efémero:
sem o disco, cada deploy apaga todas as marcações. Verificar antes, não depois.

---

## 🟠 O que falta para o sistema valer a pena para ela

**Fotos antes/depois (bloco 3).** Numa esteticista isto não é um extra — é o
portfólio e a prova do trabalho. Ela já publica antes/depois no Instagram à mão.
Hoje o bot nem aceita imagens.

**Relatórios (bloco 4).** Sem isto ela não tem motivo para abrir o painel fora
das horas de atendimento. Com o bloco 1 feito, os números já existem — falta
mostrá-los.

**Resumo mensal por WhatsApp (bloco 5).** É o que faz o sistema aparecer sozinho
na vida dela uma vez por mês.

---

## ✅ O que está mesmo bem

- **Concorrência de marcações** — testada a sério, não assumida
- **Webhook** — HMAC, idempotência por `wamid`, retries da Meta não duplicam
- **Autenticação** — 45 rotas, zero excepções, falha fechado
- **Faturação** — numeração sem buracos, snapshots congelados, dinheiro em cêntimos
- **Migrações** — 22 versionadas, correm sozinhas, base recriada de raiz sem erro
- **434 testes** a passar

---

## Caminho até ela testar

**Esta semana** — o que tem prazo externo
1. Escrever e submeter os 12 templates à Meta *(aprovação leva dias — é o caminho crítico)*
2. Confirmar o disco persistente no Render
3. `WHATSAPP_DRY_RUN` para testar sem mandar mensagens reais
4. Documentar as 12 variáveis no `.env.example` e no `render.yaml`

**A seguir** — o que ela vai ver
5. Bloco 2 (limpeza do painel + as 3 datas dos testes)
6. Bloco 3 (fotos antes/depois)

**Antes de lhe entregar**
7. Bloco 4 (relatórios)
8. Ensaio de ponta a ponta com o número dela, com templates já aprovados

**Depois de ela estar a usar**
9. Bloco 5 (resumo mensal)

---

## Uma pergunta que vale a pena responder antes

Ela vai testar **com clientes reais** ou **contigo a fingir de cliente**?

Muda tudo: com clientes reais, os templates aprovados e o disco persistente
deixam de ser recomendações e passam a ser obrigatórios, e o modo de ensaio
tem de existir antes de qualquer teste. A fingir, dá para começar já com o
bloco 2 e deixar os templates a cozinhar em paralelo.

---

# ADENDA — lançamento com clientes reais (link no Instagram)

Confirmado: o link do bot fica na bio do Instagram (1660 seguidores) e as
clientes dela entram directas. Não há ensaio — a primeira impressão é com uma
cliente verdadeira. Isto reordena as prioridades.

## A boa notícia: dá para lançar sem esperar pela Meta

Quando é a **cliente** que escreve primeiro, abre-se a janela de 24h do WhatsApp
e o bot pode responder em texto livre. **Todo o fluxo de marcação — menu, horas,
confirmação, remarcar, cancelar — funciona sem template nenhum.**

O que precisa de template é só o que sai **fora** dessa janela:
- o lembrete da véspera (a cliente marcou há 5 dias — janela fechada)
- o follow-up de reativação
- as campanhas
- a proposta de reagendamento partida do painel

**Portanto: o bot pode ir para o ar com o fluxo de marcação, e o lembrete entra
quando a Meta aprovar os templates.** Submete-os já, mas não esperes por eles
para lançar.

## 🔴 O que passa a ser crítico agora

### 1. O bot pode ficar mudo sem ninguém dar por isso
Em `messaging/whatsapp.py`, quando a Meta devolve 4xx (token expirado, limite
atingido, número suspenso), o `enviar()` **regista um aviso e devolve** — não
levanta excepção. A mensagem da cliente fica marcada como processada e ela
**não recebe nada**. Silêncio absoluto, do lado dela e do teu.

Um token temporário da Meta expira. Quando expirar, o bot morre em silêncio com
o link ainda no Instagram a mandar clientes para lá.

**Duas coisas antes do lançamento:**
- Confirmar que o `WHATSAPP_TOKEN` é de **System User permanente**, não temporário
- Um 4xx da Meta tem de avisar alguém — mensagem para o número dela, ou entrada
  no Attention Center. Silêncio não é aceitável quando há clientes à espera.

### 2. Consentimento de marketing: o bot nunca o pede
`marketing_opt_in` só se liga **à mão no painel**. A cliente nunca é perguntada.

Isso significa que, hoje, para usar as campanhas de reativação ela teria de ir
cliente a cliente marcar uma caixa por pessoas que nunca disseram que sim.
Contra a política da Meta para marketing, e frágil à luz da protecção de dados
suíça (nLPD) — são dados pessoais de clientes reais, tratados por um negócio.

**Antes de qualquer campanha:** o bot pede o consentimento uma vez, no fim da
primeira marcação, e grava a resposta. Sem isso, o bloco de campanhas fica
desligado. *(Não sou jurista — vale a pena ela confirmar as obrigações dela.)*

### 3. O painel tem dados pessoais reais e está exposto
Nomes, telemóveis, histórico e faturas de clientes verdadeiras, atrás de um
HTTP Basic **sem rate limiting e sem headers de segurança**. Enquanto eram dados
de teste era uma anotação de auditoria; com clientes reais é outra conversa.

Sobe para bloqueador de lançamento, não "bloco 2".

### 4. As automações falham em silêncio
O cron do Render faz um POST a `/api/automacoes/correr`. Se falhar — serviço a
dormir, 5xx, password mudada — os lembretes não saem e **nada avisa**. O
`raise_for_status()` só rebenta no log do cron, que ninguém lê.

### 5. O disco persistente deixa de ser recomendação
Sem ele, um deploy apaga as marcações de clientes reais. Confirmar no painel do
Render **antes** de o link ir para o Instagram.

## 🟡 Dia do lançamento

- **Pico inicial.** Link novo na bio de 1660 seguidores = várias conversas ao
  mesmo tempo logo nos primeiros dias. O `BEGIN IMMEDIATE` aguenta (testado),
  mas com 1 worker e SQLite não há folga nenhuma. Vale a pena estar a olhar.
- **Número verificado.** O número de WhatsApp Business tem de estar registado e
  o negócio verificado na Meta antes de aguentar volume a sério.
- **Saída para humano existe** — a cliente escreve HUMANO e a Daniela é avisada.
  Bom: é a válvula de escape quando o bot não percebe. Confirmar que ela sabe
  que isso existe e que lhe chega ao telemóvel.
- **Sem horários livres** está tratado nos três idiomas. Verificado.

## Ordem revista

**Antes de o link ir para o Instagram**
1. Token de System User permanente + aviso quando a Meta devolver 4xx
2. Disco persistente confirmado no Render
3. Rate limiting + headers de segurança no painel
4. Aviso quando o cron das automações falhar
5. Bloco 2 (o que ela e as clientes vão ver)
6. Ensaio completo com o prefixo `4179998` (números demo nunca chegam à Meta —
   já existe esse guarda no código)

**Em paralelo, sem bloquear o lançamento**
7. Submeter os 12 templates à Meta
8. Consentimento de marketing pedido pelo bot

**Depois do bot no ar**
9. Lembrete da véspera ligado (quando os templates forem aprovados)
10. Blocos 3, 4 e 5
