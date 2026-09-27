# Plano — adaptar o painel para a Daniela Beauty
Referência funcional: mockups DetailPro · Base: whatsapp-bot-render (bot já é da Daniela)
Prazo: semanas · Ordem pensada para cada bloco ser entregável sozinho

## Fora de âmbito (do DetailPro, não se aplica)
Veículos e matrículas · "Box 1" / recursos paralelos (ela trabalha sozinha) ·
Multi-tenant a sério (o `tenant_id` fica como está, fixo em 1)

---

## BLOCO 1 — O dinheiro passa a bater certo
*Porquê primeiro: é um bug a sério (o painel mostra CHF 0,00 a quem já pagou) e é a
fundação dos Relatórios do bloco 4. Tudo o resto assenta aqui.*

1.1 **Faturas como fonte única da verdade do dinheiro**
- `db.recalcular_customer()` deixa de somar `agendamentos.preco_cents`
- Passa a derivar de `invoices`: `facturado` (emitidas+pagas) e `pago` (só pagas)
- A ficha do cliente mostra os dois, nunca um número ambíguo chamado "Gasto"
- Migração que recalcula todos os clientes existentes

1.2 **Pagamentos parciais** (mockup: "Recebido CHF 150 · Por receber CHF 500")
- Nova tabela `payments` (invoice_id, cents, método, data, notas)
- `invoices` ganha `paid_cents` derivado; estado `partial` entre `issued` e `paid`
- UI: registar pagamento no drawer da fatura, com o valor em falta sempre visível
- Métodos: numerário, TWINT, cartão, transferência (contexto suíço)

1.3 **Testes**: cenários de pagamento parcial, sobrepagamento, anulação com pagamento feito

## BLOCO 2 — Limpeza do painel
*Rápido, muito visível, tira o risco de a Daniela abrir o link errado.*

2.1 `/painel` e `/dashboard` passam a redirect 302 para `/app` (as duas UIs antigas morrem)
2.2 Cartão "precisa da tua atenção": contagem + 3 nomes + "e mais N", em vez dos 53 nomes
2.3 Mobile: chips com scroll horizontal, legenda colapsada, grelha contida
2.4 Cartão "em curso": decorrido limitado à duração, atraso real em vez de "Atrasado 0 min"
2.5 Linha "Agora" calculada em Europe/Zurich, não no relógio do browser
2.6 Blocos de 30 min com altura mínima e texto legível
2.7 Headers de segurança + rate limiting no login (da auditoria anterior)
2.8 Corrigir as datas hardcoded dos 3 testes que falham

## BLOCO 3 — Fotos e notas por atendimento
*Para estética é o argumento de venda mais forte das mockups — o antes/depois.*

3.1 Tabela `service_notes` (appointment_id, texto, criado_em)
3.2 Tabela `service_photos` (appointment_id, tipo antes|depois, ficheiro, ordem)
3.3 Armazenamento em `MEDIA_DIR` no disco persistente do Render (já montado em /var/data)
3.4 Upload no drawer da marcação + receber fotos que a cliente manda pelo WhatsApp
3.5 Na ficha do cliente: histórico por atendimento com as fotos lado a lado
3.6 Miniaturas geradas no upload (o disco é de 1 GB — não guardar originais gigantes)

## BLOCO 4 — Relatórios
*Só faz sentido depois do bloco 1: sem o dinheiro coerente, os gráficos mentem.*

4.1 Cartões do topo: Facturado · Pago · Em aberto, com seletor de período
4.2 Facturado por semana/mês (barras)
4.3 Receita por serviço (barras horizontais)
4.4 Ocupação: horas ocupadas vs. horas disponíveis do horário dela
4.5 Clientes: novos vs. recorrentes no período
4.6 Métodos de pagamento (vem de graça do bloco 1.2)

## BLOCO 5 — Resumo mensal por WhatsApp
5.1 Job mensal em `notifications/` que junta os números do bloco 4
5.2 Mensagem para o número dela no dia 1 de cada mês
5.3 PDF do relatório em anexo (reaproveita `billing/pdf.py`)
5.4 Ligar ao executor de automações que já existe

---

## Notas técnicas
- Cada bloco entra com migração numerada nova — nunca editar uma já lançada
- Blocos 1 e 3 mexem no schema; correr sempre a suite antes de commit
- O `bot.py` está com 7 504 linhas: o que for novo nasce nos módulos
  (`billing/`, `reports/`, `notifications/`), não lá dentro
