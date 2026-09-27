"""
billing/engine.py — BILLING ENGINE (faturação integrada).

Regras não-negociáveis
----------------------
* Dinheiro SÓ em cêntimos inteiros. Nunca float.
* Uma marcação -> no máximo UMA fatura viva (idempotência): clicar "Gerar
  fatura" três vezes devolve a MESMA fatura.
* O número da fatura é atribuído na EMISSÃO, não na criação do rascunho — a
  série emitida (2026-0001, 2026-0002, …) fica sem buracos.
* A numeração corre dentro de `BEGIN IMMEDIATE`: dois pedidos simultâneos são
  serializados pelo SQLite e nunca recebem o mesmo número.
* Uma fatura emitida é histórica: nome/morada do cliente, dados do negócio e
  imposto ficam CONGELADOS (snapshot). Mudar o preço do serviço mais tarde
  não altera faturas antigas.
* `preco_cents = NULL` na marcação -> "Gerar fatura" exige um preço explícito
  (PrecoEmFalta). Esse preço é gravado na marcação e no snapshot; o preço
  GLOBAL do serviço nunca é tocado aqui.

Estados: draft -> issued -> partial -> paid ; draft|issued -> cancelled
(uma fatura com dinheiro recebido não se anula).

PAGAMENTOS: cada entrada de dinheiro é uma linha em `payments`, com o seu
método. Uma fatura pode ter vários (sinal + resto). `invoices.paid_cents`
é a soma, mantida por este módulo; a verdade são as linhas. O estado da
fatura segue o dinheiro sozinho: 0 -> issued, parcial -> partial,
total -> paid.
"""

from __future__ import annotations

import secrets

import db
import tempo

STATUS_RASCUNHO = "draft"
STATUS_EMITIDA = "issued"
STATUS_PARCIAL = "partial"
STATUS_PAGA = "paid"
STATUS_ANULADA = "cancelled"

_CAMPOS_INVOICE = (
    "id", "tenant_id", "appointment_id", "customer_id", "year", "seq",
    "invoice_number", "status", "currency", "issue_date", "due_date",
    "subtotal_cents", "discount_cents", "tax_rate_bps", "tax_cents", "total_cents",
    "customer_name_snapshot", "customer_address_snapshot",
    "business_name_snapshot", "business_address_snapshot", "business_vat_snapshot",
    "notes", "created_at", "issued_at", "paid_at", "cancelled_at",
    # P0 — pós-atendimento automático (ver notifications/postservice.py)
    "payment_method", "pdf_token", "pdf_sent_at", "pdf_last_sent_at",
    # pagamentos parciais (migração 21) — soma de `payments`
    "paid_cents",
)
_SQL_INVOICE = ", ".join(_CAMPOS_INVOICE)

_CAMPOS_SETTINGS = (
    "tenant_id", "legal_name", "address", "postal_code", "city", "country",
    "email", "phone", "iban", "vat_enabled", "vat_rate_bps", "vat_number",
    "invoice_prefix", "payment_terms_days", "invoice_footer", "currency", "updated_at",
)
_SQL_SETTINGS = ", ".join(_CAMPOS_SETTINGS)

_SETTINGS_EDITAVEIS = (
    "legal_name", "address", "postal_code", "city", "country", "email", "phone",
    "iban", "vat_enabled", "vat_rate_bps", "vat_number", "invoice_prefix",
    "payment_terms_days", "invoice_footer", "currency",
)


class ErroFaturacao(Exception):
    """Base — o API traduz para 4xx."""


class PrecoEmFalta(ErroFaturacao):
    """A marcação não tem preço e não foi dado um — precisa de confirmação."""


class TransicaoInvalida(ErroFaturacao):
    """Mudança de estado não permitida (ex.: anular uma fatura paga)."""


class PagamentoInvalido(ErroFaturacao):
    """Valor <= 0, método desconhecido, ou dinheiro a mais do que o total."""


class FaturaNaoEncontrada(ErroFaturacao):
    pass


# ---------------------------------------------------------------------------
# Definições de faturação (billing_settings)
# ---------------------------------------------------------------------------
def _linha_settings(row) -> dict:
    d = dict(zip(_CAMPOS_SETTINGS, row))
    d["vat_enabled"] = bool(d["vat_enabled"])
    return d


def definicoes_faturacao(tenant_id: int = 1, conn=None) -> dict:
    def _run(c):
        r = c.execute(f"SELECT {_SQL_SETTINGS} FROM billing_settings WHERE tenant_id = ?",
                      (tenant_id,)).fetchone()
        if not r:
            c.execute("INSERT INTO billing_settings (tenant_id, updated_at) VALUES (?, ?)",
                      (tenant_id, tempo.iso_utc()))
            r = c.execute(f"SELECT {_SQL_SETTINGS} FROM billing_settings WHERE tenant_id = ?",
                          (tenant_id,)).fetchone()
        return _linha_settings(r)

    if conn is not None:
        return _run(conn)
    with db.ligacao() as c:
        return _run(c)


def guardar_definicoes_faturacao(patch: dict, tenant_id: int = 1) -> dict:
    """Atualiza só os campos permitidos. Valida tipos numéricos."""
    campos, valores = [], []
    for k in _SETTINGS_EDITAVEIS:
        if k not in patch:
            continue
        v = patch[k]
        if k == "vat_enabled":
            v = 1 if v in (True, 1, "1", "true", "True") else 0
        elif k in ("vat_rate_bps", "payment_terms_days"):
            try:
                v = max(0, int(v))
            except (TypeError, ValueError):
                raise ErroFaturacao(f"{k} tem de ser um número inteiro >= 0.")
        elif isinstance(v, str):
            v = v.strip() or None
        campos.append(f"{k} = ?")
        valores.append(v)
    with db.ligacao() as c:
        definicoes_faturacao(tenant_id, conn=c)   # garante a linha
        if campos:
            valores += [tempo.iso_utc(), tenant_id]
            c.execute(f"UPDATE billing_settings SET {', '.join(campos)}, updated_at = ? "
                      "WHERE tenant_id = ?", valores)
        return definicoes_faturacao(tenant_id, conn=c)


# ---------------------------------------------------------------------------
# Totais — sempre em cêntimos inteiros
# ---------------------------------------------------------------------------
def _calcular_totais(linhas: list[dict], discount_cents: int, vat_enabled: bool,
                     rate_bps: int) -> dict:
    subtotal = sum(int(l["line_total_cents"]) for l in linhas)
    discount = max(0, min(int(discount_cents or 0), subtotal))
    base = subtotal - discount
    tax = round(base * int(rate_bps) / 10000) if (vat_enabled and rate_bps) else 0
    return {"subtotal_cents": subtotal, "discount_cents": discount,
            "tax_rate_bps": int(rate_bps) if vat_enabled else 0,
            "tax_cents": int(tax), "total_cents": base + int(tax)}


def _linha_invoice(row) -> dict:
    return dict(zip(_CAMPOS_INVOICE, row))


def _linhas_de(c, invoice_id: int) -> list[dict]:
    rows = c.execute(
        "SELECT id, description, quantity, unit_price_cents, line_total_cents, sort_order "
        "FROM invoice_lines WHERE invoice_id = ? ORDER BY sort_order, id", (invoice_id,)
    ).fetchall()
    cols = ("id", "description", "quantity", "unit_price_cents", "line_total_cents", "sort_order")
    return [dict(zip(cols, r)) for r in rows]


def _montar(c, row) -> dict:
    inv = _linha_invoice(row)
    inv["lines"] = _linhas_de(c, inv["id"])
    # Pagamentos: o painel precisa sempre de "recebido" e "por receber" juntos
    # — calcular isto no cliente seria pedir para os dois divergirem.
    recebido, pagamentos = _resumo_pagamentos(c, inv["id"])
    inv["paid_cents"] = recebido
    inv["due_cents"] = max(0, int(inv.get("total_cents") or 0) - recebido)
    inv["payments"] = pagamentos
    return inv


def _reaplicar_totais(c, invoice_id: int, tenant_id: int):
    inv = c.execute(f"SELECT {_SQL_INVOICE} FROM invoices WHERE id = ?", (invoice_id,)).fetchone()
    inv = _linha_invoice(inv)
    cfg = definicoes_faturacao(tenant_id, conn=c)
    t = _calcular_totais(_linhas_de(c, invoice_id), inv["discount_cents"],
                         cfg["vat_enabled"], cfg["vat_rate_bps"])
    c.execute("UPDATE invoices SET subtotal_cents = ?, discount_cents = ?, tax_rate_bps = ?, "
              "tax_cents = ?, total_cents = ? WHERE id = ?",
              (t["subtotal_cents"], t["discount_cents"], t["tax_rate_bps"],
               t["tax_cents"], t["total_cents"], invoice_id))


# ---------------------------------------------------------------------------
# Gerar fatura a partir de uma marcação
# ---------------------------------------------------------------------------
def gerar_fatura_de_marcacao(appointment_id: int, preco_cents: int | None = None,
                             tenant_id: int = 1) -> dict:
    """Idempotente: se já existir uma fatura viva para esta marcação, devolve-a.
    `preco_cents` só é preciso quando a marcação não tem preço definido."""
    import bot  # obter_agendamento / catálogo — import tardio evita ciclo

    ag = bot.obter_agendamento(appointment_id)
    if not ag:
        raise FaturaNaoEncontrada("Marcação não encontrada.")

    preco = preco_cents if preco_cents is not None else ag.get("preco_cents")
    if preco is None:
        preco_leg = bot.total_centimos_agendamento(ag)
        preco = preco_leg if preco_leg else None
    if preco is None:
        raise PrecoEmFalta("Esta marcação não tem preço. Indica o preço deste atendimento.")
    preco = int(preco)
    if preco < 0:
        raise ErroFaturacao("O preço não pode ser negativo.")

    servico_nome = ag.get("servico") or "Serviço"
    sid = ag.get("servico_id")
    if sid:
        s = db.obter_servico(sid)
        if s:
            servico_nome = s["nome_pt"]

    with db.ligacao() as c:
        c.execute("BEGIN IMMEDIATE")
        existe = c.execute(
            f"SELECT {_SQL_INVOICE} FROM invoices WHERE tenant_id = ? AND appointment_id = ? "
            "AND status <> 'cancelled'", (tenant_id, appointment_id)).fetchone()
        if existe:
            return _montar(c, existe)

        # o preço confirmado fica na marcação (NÃO no catálogo global)
        if ag.get("preco_cents") is None and preco_cents is not None:
            c.execute("UPDATE agendamentos SET preco_cents = ? WHERE id = ?",
                      (preco, appointment_id))

        cfg = definicoes_faturacao(tenant_id, conn=c)
        cli_nome = ag.get("nome") or None
        cli_morada = None
        if ag.get("customer_id"):
            cust = c.execute("SELECT name, notes_internal FROM customers WHERE id = ?",
                             (ag["customer_id"],)).fetchone()
            if cust and cust[0]:
                cli_nome = cust[0]

        agora = tempo.iso_utc()
        cur = c.execute(
            "INSERT INTO invoices (tenant_id, appointment_id, customer_id, status, currency, "
            "tax_rate_bps, customer_name_snapshot, customer_address_snapshot, "
            "business_name_snapshot, business_address_snapshot, business_vat_snapshot, created_at) "
            "VALUES (?, ?, ?, 'draft', ?, ?, ?, ?, ?, ?, ?, ?)",
            (tenant_id, appointment_id, ag.get("customer_id"), cfg["currency"],
             cfg["vat_rate_bps"] if cfg["vat_enabled"] else 0,
             cli_nome, cli_morada,
             cfg["legal_name"], cfg["address"],
             cfg["vat_number"] if cfg["vat_enabled"] else None, agora))
        inv_id = cur.lastrowid
        c.execute(
            "INSERT INTO invoice_lines (invoice_id, description, quantity, unit_price_cents, "
            "line_total_cents, sort_order) VALUES (?, ?, 1, ?, ?, 0)",
            (inv_id, servico_nome, preco, preco))
        _reaplicar_totais(c, inv_id, tenant_id)

        db.registar_evento(c, "invoice.created", "invoice", inv_id,
                           {"appointment_id": appointment_id, "total_cents": preco},
                           dedupe_key=f"invoice.created:{inv_id}", tenant_id=tenant_id)

        row = c.execute(f"SELECT {_SQL_INVOICE} FROM invoices WHERE id = ?", (inv_id,)).fetchone()
        return _montar(c, row)


# ---------------------------------------------------------------------------
# Leitura
# ---------------------------------------------------------------------------
def obter_fatura(invoice_id: int, tenant_id: int = 1, conn=None) -> dict | None:
    def _run(c):
        row = c.execute(f"SELECT {_SQL_INVOICE} FROM invoices WHERE id = ? AND tenant_id = ?",
                        (invoice_id, tenant_id)).fetchone()
        return _montar(c, row) if row else None

    if conn is not None:
        return _run(conn)
    with db.ligacao() as c:
        return _run(c)


def listar_faturas(tenant_id: int = 1, status: str | None = None,
                   limite: int = 200) -> list[dict]:
    q = (f"SELECT {_SQL_INVOICE} FROM invoices WHERE tenant_id = ?")
    args = [tenant_id]
    if status and status != "all":
        if status == "overdue":
            # Uma fatura com um sinal pago e o resto por receber também está
            # vencida — o que conta é faltar dinheiro depois do prazo.
            q += (" AND status IN ('issued', 'partial') "
                  "AND due_date IS NOT NULL AND due_date < ?")
            args.append(tempo.hoje_zurique().isoformat())
        else:
            q += " AND status = ?"
            args.append(status)
    q += " ORDER BY COALESCE(issued_at, created_at) DESC, id DESC LIMIT ?"
    args.append(int(limite))
    with db.ligacao() as c:
        rows = c.execute(q, args).fetchall()
        # A lista mostra "por receber" — sem as linhas de pagamento (que só o
        # drawer precisa), para não fazer um SELECT por fatura.
        faturas = []
        for r in rows:
            inv = _linha_invoice(r)
            recebido = int(inv.get("paid_cents") or 0)
            inv["due_cents"] = max(0, int(inv.get("total_cents") or 0) - recebido)
            faturas.append(inv)
        return faturas


# ---------------------------------------------------------------------------
# Edição de rascunho
# ---------------------------------------------------------------------------
def atualizar_rascunho(invoice_id: int, patch: dict, tenant_id: int = 1) -> dict:
    with db.ligacao() as c:
        c.execute("BEGIN IMMEDIATE")
        row = c.execute("SELECT status FROM invoices WHERE id = ? AND tenant_id = ?",
                        (invoice_id, tenant_id)).fetchone()
        if not row:
            raise FaturaNaoEncontrada("Fatura não encontrada.")
        if row[0] != STATUS_RASCUNHO:
            raise TransicaoInvalida("Só um rascunho pode ser editado.")

        if "notes" in patch:
            c.execute("UPDATE invoices SET notes = ? WHERE id = ?",
                      ((patch["notes"] or None), invoice_id))
        if "discount_cents" in patch:
            try:
                d = max(0, int(patch["discount_cents"] or 0))
            except (TypeError, ValueError):
                raise ErroFaturacao("Desconto inválido.")
            c.execute("UPDATE invoices SET discount_cents = ? WHERE id = ?", (d, invoice_id))
        if "lines" in patch and isinstance(patch["lines"], list):
            c.execute("DELETE FROM invoice_lines WHERE invoice_id = ?", (invoice_id,))
            for i, ln in enumerate(patch["lines"]):
                desc = str(ln.get("description") or "").strip() or "Item"
                try:
                    qty = max(1, int(ln.get("quantity", 1)))
                    unit = int(ln.get("unit_price_cents", 0))
                except (TypeError, ValueError):
                    raise ErroFaturacao("Linha da fatura inválida.")
                if unit < 0:
                    raise ErroFaturacao("Preço de linha negativo.")
                c.execute(
                    "INSERT INTO invoice_lines (invoice_id, description, quantity, "
                    "unit_price_cents, line_total_cents, sort_order) VALUES (?, ?, ?, ?, ?, ?)",
                    (invoice_id, desc, qty, unit, qty * unit, i))
        _reaplicar_totais(c, invoice_id, tenant_id)
        row = c.execute(f"SELECT {_SQL_INVOICE} FROM invoices WHERE id = ?", (invoice_id,)).fetchone()
        return _montar(c, row)


# ---------------------------------------------------------------------------
# Transições de estado
# ---------------------------------------------------------------------------
def emitir_fatura(invoice_id: int, tenant_id: int = 1) -> dict:
    """Emite e recalcula o cliente — emitir é o momento em que o valor passa a
    contar como FACTURADO na ficha dela (ver db.recalcular_customer)."""
    fatura = _emitir_fatura(invoice_id, tenant_id)
    _recalcular_cliente_da_fatura(fatura)
    return fatura


def _emitir_fatura(invoice_id: int, tenant_id: int = 1) -> dict:
    """draft -> issued. Atribui o número (série anual sem buracos), congela
    datas e totais. Serializado por BEGIN IMMEDIATE."""
    with db.ligacao() as c:
        c.execute("BEGIN IMMEDIATE")
        row = c.execute(f"SELECT {_SQL_INVOICE} FROM invoices WHERE id = ? AND tenant_id = ?",
                        (invoice_id, tenant_id)).fetchone()
        if not row:
            raise FaturaNaoEncontrada("Fatura não encontrada.")
        inv = _linha_invoice(row)
        if inv["status"] == STATUS_EMITIDA:
            return _montar(c, row)                       # idempotente
        if inv["status"] != STATUS_RASCUNHO:
            raise TransicaoInvalida(f"Não se pode emitir uma fatura '{inv['status']}'.")

        cfg = definicoes_faturacao(tenant_id, conn=c)
        hoje = tempo.hoje_zurique()
        ano = hoje.year
        seq = (c.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM invoices "
                         "WHERE tenant_id = ? AND year = ?", (tenant_id, ano)).fetchone()[0])
        prefixo = (cfg["invoice_prefix"] or "").strip()
        numero = f"{prefixo}{ano}-{seq:04d}"
        from datetime import timedelta
        due = (hoje + timedelta(days=int(cfg["payment_terms_days"] or 0))).isoformat()

        _reaplicar_totais(c, invoice_id, tenant_id)
        c.execute(
            "UPDATE invoices SET status = 'issued', year = ?, seq = ?, invoice_number = ?, "
            "issue_date = ?, due_date = ?, issued_at = ? WHERE id = ?",
            (ano, seq, numero, hoje.isoformat(), due, tempo.iso_utc(), invoice_id))
        db.registar_evento(c, "invoice.issued", "invoice", invoice_id,
                           {"invoice_number": numero}, dedupe_key=f"invoice.issued:{invoice_id}",
                           tenant_id=tenant_id)
        row = c.execute(f"SELECT {_SQL_INVOICE} FROM invoices WHERE id = ?", (invoice_id,)).fetchone()
        return _montar(c, row)


PAGAMENTO_CASH = "cash"
PAGAMENTO_TWINT = "twint"
PAGAMENTO_CARTAO = "card"
PAGAMENTO_TRANSFERENCIA = "transfer"
PAGAMENTO_OUTRO = "other"

# Os meios que se usam mesmo num salão na Suíça. `other` é a válvula de escape
# para o que fugir à lista — melhor do que deixar escrever texto livre e ficar
# com "twint", "Twint" e "TWINT " como três métodos diferentes no relatório.
METODOS_PAGAMENTO = (PAGAMENTO_CASH, PAGAMENTO_TWINT, PAGAMENTO_CARTAO,
                     PAGAMENTO_TRANSFERENCIA, PAGAMENTO_OUTRO)


def _resumo_pagamentos(c, invoice_id: int) -> tuple[int, list[dict]]:
    """(total recebido, linhas de pagamento) desta fatura, mais recente primeiro."""
    rows = c.execute(
        "SELECT id, amount_cents, method, paid_on, notes, created_at "
        "FROM payments WHERE invoice_id = ? ORDER BY COALESCE(paid_on, created_at) DESC, id DESC",
        (invoice_id,)).fetchall()
    pagamentos = [{"id": r[0], "amount_cents": r[1], "method": r[2],
                   "paid_on": r[3], "notes": r[4], "created_at": r[5]} for r in rows]
    return sum(p["amount_cents"] for p in pagamentos), pagamentos


def _sincronizar_estado_pagamento(c, invoice_id: int, tenant_id: int) -> dict:
    """Põe `paid_cents` e o ESTADO da fatura de acordo com os pagamentos que
    ela tem. Chamada depois de cada registo ou remoção.

    issued|partial|paid movem-se sozinhos conforme o dinheiro; `draft` e
    `cancelled` nunca são tocados aqui (uma fatura em rascunho não recebe
    dinheiro, e uma anulada já não é deste mundo). Só regista o evento
    `invoice.paid` na transição para paga — nunca em cada pagamento parcial.
    """
    recebido, _ = _resumo_pagamentos(c, invoice_id)
    row = c.execute("SELECT status, total_cents, paid_at FROM invoices WHERE id = ?",
                    (invoice_id,)).fetchone()
    estado_antes, total, paid_at = row[0], int(row[1] or 0), row[2]

    if estado_antes in (STATUS_RASCUNHO, STATUS_ANULADA):
        c.execute("UPDATE invoices SET paid_cents = ? WHERE id = ?", (recebido, invoice_id))
        return {"status": estado_antes, "paid_cents": recebido}

    if recebido <= 0:
        novo_estado, novo_paid_at = STATUS_EMITIDA, None
    elif recebido < total:
        novo_estado, novo_paid_at = STATUS_PARCIAL, None
    else:
        novo_estado = STATUS_PAGA
        novo_paid_at = paid_at or tempo.iso_utc()

    # O método "principal" mostrado na fatura é o do maior pagamento — com um
    # só pagamento (o caso normal) é exatamente o que ela escolheu.
    metodo = c.execute(
        "SELECT method FROM payments WHERE invoice_id = ? "
        "ORDER BY amount_cents DESC, id ASC LIMIT 1", (invoice_id,)).fetchone()

    c.execute("UPDATE invoices SET paid_cents = ?, status = ?, paid_at = ?, "
              "payment_method = COALESCE(?, payment_method) WHERE id = ?",
              (recebido, novo_estado, novo_paid_at, metodo[0] if metodo else None, invoice_id))

    if novo_estado == STATUS_PAGA and estado_antes != STATUS_PAGA:
        db.registar_evento(c, "invoice.paid", "invoice", invoice_id,
                           {"payment_method": metodo[0] if metodo else None,
                            "paid_cents": recebido},
                           dedupe_key=f"invoice.paid:{invoice_id}", tenant_id=tenant_id)
    return {"status": novo_estado, "paid_cents": recebido}


def registar_pagamento(invoice_id: int, amount_cents: int, metodo: str = PAGAMENTO_CASH,
                       paid_on: str | None = None, notas: str | None = None,
                       tenant_id: int = 1) -> dict:
    """Regista uma entrada de dinheiro numa fatura emitida.

    Recusa: valor <= 0, método fora da lista, fatura em rascunho ou anulada, e
    dinheiro a mais do que falta (um troco não é um pagamento — se o valor
    estiver errado, remove-se e regista-se outra vez).
    """
    try:
        amount_cents = int(amount_cents)
    except (TypeError, ValueError):
        raise PagamentoInvalido("O valor do pagamento tem de ser um número inteiro de cêntimos.")
    if amount_cents <= 0:
        raise PagamentoInvalido("O valor do pagamento tem de ser maior do que zero.")
    metodo = (metodo or PAGAMENTO_CASH).strip().lower()
    if metodo not in METODOS_PAGAMENTO:
        raise PagamentoInvalido(f"Método de pagamento desconhecido: {metodo}")

    with db.ligacao() as c:
        c.execute("BEGIN IMMEDIATE")
        row = c.execute("SELECT status, total_cents FROM invoices WHERE id = ? AND tenant_id = ?",
                        (invoice_id, tenant_id)).fetchone()
        if not row:
            raise FaturaNaoEncontrada("Fatura não encontrada.")
        estado, total = row[0], int(row[1] or 0)
        if estado == STATUS_RASCUNHO:
            raise TransicaoInvalida("Emite a fatura antes de registar pagamentos.")
        if estado == STATUS_ANULADA:
            raise TransicaoInvalida("Uma fatura anulada não recebe pagamentos.")

        recebido, _ = _resumo_pagamentos(c, invoice_id)
        se_falta = total - recebido
        if amount_cents > se_falta:
            raise PagamentoInvalido(
                f"São mais {amount_cents - se_falta} cêntimos do que falta receber.")

        c.execute(
            "INSERT INTO payments (tenant_id, invoice_id, amount_cents, method, paid_on, "
            "notes, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (tenant_id, invoice_id, amount_cents, metodo,
             paid_on or tempo.hoje_zurique().isoformat(), (notas or "").strip() or None,
             tempo.iso_utc()))
        _sincronizar_estado_pagamento(c, invoice_id, tenant_id)
        r = c.execute(f"SELECT {_SQL_INVOICE} FROM invoices WHERE id = ?", (invoice_id,)).fetchone()
        fatura = _montar(c, r)
    _recalcular_cliente_da_fatura(fatura)
    return fatura


def remover_pagamento(payment_id: int, tenant_id: int = 1) -> dict:
    """Apaga um pagamento mal registado e devolve a fatura já reavaliada —
    uma fatura que estava paga volta sozinha a parcial ou a emitida."""
    with db.ligacao() as c:
        c.execute("BEGIN IMMEDIATE")
        row = c.execute("SELECT invoice_id FROM payments WHERE id = ? AND tenant_id = ?",
                        (payment_id, tenant_id)).fetchone()
        if not row:
            raise FaturaNaoEncontrada("Pagamento não encontrado.")
        invoice_id = row[0]
        c.execute("DELETE FROM payments WHERE id = ?", (payment_id,))
        _sincronizar_estado_pagamento(c, invoice_id, tenant_id)
        r = c.execute(f"SELECT {_SQL_INVOICE} FROM invoices WHERE id = ?", (invoice_id,)).fetchone()
        fatura = _montar(c, r)
    _recalcular_cliente_da_fatura(fatura)
    return fatura


def pagamentos_da_fatura(invoice_id: int, tenant_id: int = 1) -> list[dict]:
    with db.ligacao() as c:
        if not c.execute("SELECT 1 FROM invoices WHERE id = ? AND tenant_id = ?",
                         (invoice_id, tenant_id)).fetchone():
            raise FaturaNaoEncontrada("Fatura não encontrada.")
        return _resumo_pagamentos(c, invoice_id)[1]


def _recalcular_cliente_da_fatura(fatura: dict):
    """O dinheiro do cliente vem das faturas (ver db.recalcular_customer), por
    isso qualquer mexida no dinheiro obriga a recalcular. Fora da transação:
    falhar aqui nunca desfaz um pagamento já gravado."""
    cid = (fatura or {}).get("customer_id")
    if not cid:
        return
    try:
        db.recalcular_customer(cid)
    except Exception:                        # noqa: BLE001
        pass


def marcar_paga(invoice_id: int, tenant_id: int = 1, metodo: str = PAGAMENTO_CASH) -> dict:
    """Atalho para o caso normal: a cliente pagou tudo de uma vez. Regista um
    pagamento pelo valor em falta e a fatura passa a paga.

    Idempotente: numa fatura já paga não falta nada, por isso não faz nada.
    """
    with db.ligacao() as c:
        row = c.execute("SELECT status, total_cents, COALESCE(paid_cents, 0) "
                        "FROM invoices WHERE id = ? AND tenant_id = ?",
                        (invoice_id, tenant_id)).fetchone()
        if not row:
            raise FaturaNaoEncontrada("Fatura não encontrada.")
        estado, total, recebido = row[0], int(row[1] or 0), int(row[2] or 0)
    if estado == STATUS_PAGA:
        return obter_fatura(invoice_id, tenant_id)
    if estado not in (STATUS_EMITIDA, STATUS_PARCIAL):
        raise TransicaoInvalida("Só uma fatura emitida pode ser marcada como paga.")
    return registar_pagamento(invoice_id, total - recebido, metodo, tenant_id=tenant_id)


def obter_fatura_por_agendamento(appointment_id: int, tenant_id: int = 1) -> dict | None:
    """A fatura VIVA (não anulada) desta marcação, se existir — mesma
    condição de idempotência usada em gerar_fatura_de_marcacao."""
    with db.ligacao() as c:
        row = c.execute(
            f"SELECT {_SQL_INVOICE} FROM invoices WHERE tenant_id = ? AND appointment_id = ? "
            "AND status <> 'cancelled'", (tenant_id, appointment_id)).fetchone()
        return _montar(c, row) if row else None


def garantir_pdf_token(invoice_id: int, tenant_id: int = 1) -> str:
    """Devolve o token do PDF desta fatura, gerando um na primeira vez
    (idempotente — chamadas seguintes devolvem sempre o mesmo)."""
    with db.ligacao() as c:
        row = c.execute("SELECT pdf_token FROM invoices WHERE id = ? AND tenant_id = ?",
                        (invoice_id, tenant_id)).fetchone()
        if not row:
            raise FaturaNaoEncontrada("Fatura não encontrada.")
        if row[0]:
            return row[0]
        token = secrets.token_urlsafe(24)
        c.execute("UPDATE invoices SET pdf_token = ? WHERE id = ?", (token, invoice_id))
        return token


def obter_fatura_por_token(token: str) -> dict | None:
    """Usado só pela rota pública de download do PDF — o token É a
    autorização (longo, aleatório, imprevisível), por isso não filtra tenant."""
    if not token:
        return None
    with db.ligacao() as c:
        row = c.execute(f"SELECT {_SQL_INVOICE} FROM invoices WHERE pdf_token = ?", (token,)).fetchone()
        return _montar(c, row) if row else None


def marcar_pdf_enviado(invoice_id: int, tenant_id: int = 1, reenvio: bool = False) -> dict:
    """Regista o envio do PDF por WhatsApp. `pdf_sent_at` só é gravado da
    PRIMEIRA vez (idempotente); `pdf_last_sent_at` atualiza sempre — permite
    distinguir "enviado" de "reenviado" sem inventar um segundo campo."""
    with db.ligacao() as c:
        c.execute("BEGIN IMMEDIATE")
        row = c.execute("SELECT id FROM invoices WHERE id = ? AND tenant_id = ?",
                        (invoice_id, tenant_id)).fetchone()
        if not row:
            raise FaturaNaoEncontrada("Fatura não encontrada.")
        agora = tempo.iso_utc()
        c.execute("UPDATE invoices SET pdf_sent_at = COALESCE(pdf_sent_at, ?), "
                  "pdf_last_sent_at = ? WHERE id = ?", (agora, agora, invoice_id))
        db.registar_evento(c, "invoice.pdf_resent" if reenvio else "invoice.pdf_sent",
                           "invoice", invoice_id, {},
                           dedupe_key=None if reenvio else f"invoice.pdf_sent:{invoice_id}",
                           tenant_id=tenant_id)
        r = c.execute(f"SELECT {_SQL_INVOICE} FROM invoices WHERE id = ?", (invoice_id,)).fetchone()
        return _montar(c, r)


def anular_fatura(invoice_id: int, tenant_id: int = 1) -> dict:
    """Anula e recalcula o cliente — o valor deixa de contar como facturado."""
    fatura = _anular_fatura(invoice_id, tenant_id)
    _recalcular_cliente_da_fatura(fatura)
    return fatura


def _anular_fatura(invoice_id: int, tenant_id: int = 1) -> dict:
    """draft|issued -> cancelled. Uma fatura PAGA não se anula (precisa de nota
    de crédito — fora do âmbito desta versão)."""
    with db.ligacao() as c:
        c.execute("BEGIN IMMEDIATE")
        row = c.execute("SELECT status FROM invoices WHERE id = ? AND tenant_id = ?",
                        (invoice_id, tenant_id)).fetchone()
        if not row:
            raise FaturaNaoEncontrada("Fatura não encontrada.")
        if row[0] == STATUS_ANULADA:
            pass
        elif row[0] == STATUS_PAGA:
            raise TransicaoInvalida("Uma fatura paga não pode ser anulada.")
        elif row[0] == STATUS_PARCIAL:
            # Há dinheiro dela em cima da mesa: remove-se o pagamento primeiro,
            # para nunca ficar um recebimento pendurado numa fatura anulada.
            raise TransicaoInvalida(
                "Esta fatura já tem pagamentos. Remove-os antes de a anular.")
        else:
            c.execute("UPDATE invoices SET status = 'cancelled', cancelled_at = ? WHERE id = ?",
                      (tempo.iso_utc(), invoice_id))
            db.registar_evento(c, "invoice.cancelled", "invoice", invoice_id, {},
                               dedupe_key=f"invoice.cancelled:{invoice_id}", tenant_id=tenant_id)
        r = c.execute(f"SELECT {_SQL_INVOICE} FROM invoices WHERE id = ?", (invoice_id,)).fetchone()
        return _montar(c, r)


def faturas_do_cliente(customer_id: int, tenant_id: int = 1) -> list[dict]:
    with db.ligacao() as c:
        rows = c.execute(
            f"SELECT {_SQL_INVOICE} FROM invoices WHERE tenant_id = ? AND customer_id = ? "
            "AND status <> 'cancelled' ORDER BY COALESCE(issued_at, created_at) DESC, id DESC",
            (tenant_id, customer_id)).fetchall()
        return [_linha_invoice(r) for r in rows]
