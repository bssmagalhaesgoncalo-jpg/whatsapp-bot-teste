# Dinheiro: faturas, pagamentos e contadores do cliente

Estado depois do **bloco 1** (17-09-2026). Migrações 21 e 22.

## A regra
**A fatura é a única fonte de verdade do dinheiro.** O preço da marcação
(`agendamentos.preco_cents`) serve para GERAR a fatura e mais nada.

Antes havia duas fontes e divergiam: `recalcular_customer` somava o preço das
marcações concluídas e ignorava as faturas — uma cliente com fatura de CHF 50
PAGA aparecia na ficha com **CHF 0,00**. Era o bug B1 da auditoria.

## Contadores do cliente (`db.recalcular_customer`)
- `billed_cents` — facturado: faturas `issued` + `partial` + `paid`
- `paid_cents` — recebido: soma real dos pagamentos
- `spend_cents` — legado, espelha `paid_cents`. Não usar em código novo.
- Rascunhos e anuladas não contam para nenhum.
- Visitas e datas continuam a vir das MARCAÇÕES — são factos da agenda.

Recalcula-se ao emitir, anular, pagar e remover pagamento. Os wrappers
`emitir_fatura` / `anular_fatura` em `billing/engine.py` tratam disso; as
funções internas chamam-se `_emitir_fatura` / `_anular_fatura`.

## Pagamentos (`payments`)
Uma linha por entrada de dinheiro. Vários por fatura (sinal + resto).
`invoices.paid_cents` é a SOMA, derivada — a verdade são as linhas.

Métodos (lista fechada, `METODOS_PAGAMENTO`):
`cash` · `twint` · `card` · `transfer` · `other`
Fechada de propósito: texto livre daria "twint", "Twint" e "TWINT " como três
métodos diferentes no relatório.

## Estados
```
draft → issued → partial → paid
draft|issued → cancelled
```
O estado SEGUE o dinheiro, nunca se escolhe à mão
(`_sincronizar_estado_pagamento`): 0 → issued, parcial → partial, total → paid.
Remover um pagamento faz a fatura recuar sozinha.

Recusas: valor ≤ 0, método fora da lista, rascunho, anulada, e **dinheiro a
mais do que falta** (um troco não é um pagamento — remove-se e regista-se outra vez).
Uma fatura com pagamentos **não se anula** — senão ficava um recebimento
pendurado numa fatura que já não existe.

## API
- `POST   /api/faturas/<id>/pagamentos` — aceita `amount_cents` ou `valor` (francos)
- `DELETE /api/faturas/<id>/pagamentos/<pid>`
- `GET    /api/metodos-pagamento` — lista com rótulos PT, para o painel não os repetir
- `GET    /api/faturas` — traz `due_cents` por linha
- `POST   /api/faturas/<id>/pagar` — atalho: paga o que falta de uma vez

## UI
`blocoPagamentos()` em `app.js`: Recebido / Por receber + lista + formulário.
O valor sugerido é sempre **o que falta**. CSS `.pay-*` no fim do `app.css`.

## Testes
`tests/test_pagamentos.py` — 16 testes: ciclo, recusas, remoção, contadores, API.
