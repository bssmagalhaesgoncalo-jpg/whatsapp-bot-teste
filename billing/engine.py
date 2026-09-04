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

Estados: draft -> issued -> paid ; draft|issued -> cancelled (paid não).
"""

from __future__ import annotations

import db
import tempo

STATUS_RASCUNHO = "draft"
STATUS_EMITIDA = "issued"
STATUS_PAGA = "paid"
STATUS_ANULADA = "cancelled"

_CAMPOS_INVOICE = (
    "id", "tenant_id", "appointment_id", "customer_id", "year", "seq",
    "invoice_number", "status", "currency", "issue_date", "due_date",
    "subtotal_cents", "discount_cents", "tax_rate_bps", "tax_cents", "total_cents",
    "customer_name_snapshot", "customer_address_snapshot",
    "business_name_snapshot", "business_address_snapshot", "business_vat_snapshot",
    "notes", "created_at", "issued_at", "paid_at", "cancelled_at",
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
            q += (" AND status = 'issued' AND due_date IS NOT NULL AND due_date < ?")
            args.append(tempo.hoje_zurique().isoformat())
        else:
            q += " AND status = ?"
            args.append(status)
    q += " ORDER BY COALESCE(issued_at, created_at) DESC, id DESC LIMIT ?"
    args.append(int(limite))
    with db.ligacao() as c:
        rows = c.execute(q, args).fetchall()
        return [_linha_invoice(r) for r in rows]


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


def marcar_paga(invoice_id: int, tenant_id: int = 1) -> dict:
    with db.ligacao() as c:
        c.execute("BEGIN IMMEDIATE")
        row = c.execute("SELECT status FROM invoices WHERE id = ? AND tenant_id = ?",
                        (invoice_id, tenant_id)).fetchone()
        if not row:
            raise FaturaNaoEncontrada("Fatura não encontrada.")
        if row[0] == STATUS_PAGA:
            pass
        elif row[0] != STATUS_EMITIDA:
            raise TransicaoInvalida("Só uma fatura emitida pode ser marcada como paga.")
        else:
            c.execute("UPDATE invoices SET status = 'paid', paid_at = ? WHERE id = ?",
                      (tempo.iso_utc(), invoice_id))
            db.registar_evento(c, "invoice.paid", "invoice", invoice_id, {},
                               dedupe_key=f"invoice.paid:{invoice_id}", tenant_id=tenant_id)
        r = c.execute(f"SELECT {_SQL_INVOICE} FROM invoices WHERE id = ?", (invoice_id,)).fetchone()
        return _montar(c, r)


def anular_fatura(invoice_id: int, tenant_id: int = 1) -> dict:
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
