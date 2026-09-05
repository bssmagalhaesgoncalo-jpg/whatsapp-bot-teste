"""
billing/pdf.py — gerador MÍNIMO de PDF da fatura, sem dependências externas.

O projeto não tem nenhuma biblioteca de PDF (ver requirements.txt: só Flask/
gunicorn/requests) — em vez de acrescentar uma dependência nova só para uma
fatura simples, este módulo escreve os bytes do PDF diretamente (sintaxe
mínima válida: catálogo, páginas, uma página A4, um stream de texto,
Helvetica normal/negrito, xref, trailer).

O conteúdo vem SEMPRE da fatura já calculada pelo billing engine — nunca
recalcula nem inventa valores. Nada de IVA/UID/IBAN/QR-Bill que não esteja
configurado em billing_settings.
"""

from __future__ import annotations

import catalogo

_LARGURA_A4 = 595
_ALTURA_A4 = 842
_MARGEM_ESQ = 56


def _escapar(texto) -> str:
    texto = str(texto if texto is not None else "")
    return texto.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")


def _op_texto(texto: str, x: int, y: int, tamanho: int, negrito: bool) -> str:
    fonte = "/F2" if negrito else "/F1"
    return f"BT {fonte} {tamanho} Tf {x} {y} Td ({_escapar(texto)}) Tj ET\n"


def gerar_pdf_fatura(invoice: dict, settings: dict) -> bytes:
    """PDF A4 de uma página com os dados desta fatura (já emitida/paga) e do
    negócio configurado em `settings` (billing.engine.definicoes_faturacao).
    Devolve bytes prontos a servir com mimetype application/pdf."""
    y = _ALTURA_A4 - 64
    linhas: list[str] = []

    def escrever(texto, tamanho=11, negrito=False, salto=16):
        nonlocal y
        linhas.append(_op_texto(texto, _MARGEM_ESQ, y, tamanho, negrito))
        y -= salto

    nome_negocio = (settings or {}).get("legal_name") or "Daniela Beauty"
    escrever(nome_negocio, 17, negrito=True, salto=22)
    if settings.get("address"):
        escrever(settings["address"], 10, salto=13)
    contacto = " · ".join(v for v in (settings.get("phone"), settings.get("email")) if v)
    if contacto:
        escrever(contacto, 10, salto=13)
    y -= 14

    numero = invoice.get("invoice_number") or f"rascunho #{invoice.get('id')}"
    escrever(f"Fatura {numero}", 14, negrito=True, salto=18)
    if invoice.get("issue_date"):
        escrever(f"Data de emissão: {invoice['issue_date']}", 10, salto=13)
    y -= 10

    escrever(f"Cliente: {invoice.get('customer_name_snapshot') or '—'}", 11, salto=15)
    if invoice.get("customer_address_snapshot"):
        escrever(invoice["customer_address_snapshot"], 10, salto=13)
    y -= 10

    escrever("Serviço", 12, negrito=True, salto=16)
    for linha in invoice.get("lines") or []:
        preco = catalogo.formatar_cents(linha.get("line_total_cents") or 0, "pt")
        qtd = linha.get("quantity") or 1
        descricao = f"{linha.get('description') or 'Item'}"
        if qtd != 1:
            descricao += f"  x{qtd}"
        escrever(f"{descricao}  —  {preco}", 10, salto=14)
    y -= 8

    if invoice.get("discount_cents"):
        escrever(f"Desconto: -{catalogo.formatar_cents(invoice['discount_cents'], 'pt')}", 10, salto=14)
    if invoice.get("tax_cents"):
        taxa = (invoice.get("tax_rate_bps") or 0) / 100
        escrever(f"IVA ({taxa:.2f}%): {catalogo.formatar_cents(invoice['tax_cents'], 'pt')}", 10, salto=14)
    escrever(f"Total: {catalogo.formatar_cents(invoice.get('total_cents') or 0, 'pt')}",
             13, negrito=True, salto=20)

    metodo = (invoice.get("payment_method") or "").strip()
    if metodo:
        escrever(f"Método de pagamento: {metodo.capitalize()}", 10, salto=15)

    if invoice.get("notes"):
        y -= 6
        escrever("Nota", 10, negrito=True, salto=13)
        escrever(invoice["notes"], 10, salto=13)

    rodape = (settings or {}).get("invoice_footer")
    if rodape:
        y -= 10
        escrever(rodape, 9, salto=12)

    stream = "".join(linhas).encode("latin-1", errors="replace")
    return _montar_pdf(stream)


def _montar_pdf(stream: bytes) -> bytes:
    objetos = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {_LARGURA_A4} {_ALTURA_A4}] "
         "/Resources << /Font << /F1 5 0 R /F2 6 0 R >> >> /Contents 4 0 R >>").encode("latin-1"),
        f"<< /Length {len(stream)} >>\nstream\n".encode("latin-1") + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold >>",
    ]

    partes = [b"%PDF-1.4\n"]
    offsets = []
    for i, corpo in enumerate(objetos, start=1):
        offsets.append(sum(len(p) for p in partes))
        partes.append(f"{i} 0 obj\n".encode("latin-1") + corpo + b"\nendobj\n")
    xref_pos = sum(len(p) for p in partes)
    n = len(objetos) + 1
    partes.append(f"xref\n0 {n}\n".encode("latin-1"))
    partes.append(b"0000000000 65535 f \n")
    for off in offsets:
        partes.append(f"{off:010d} 00000 n \n".encode("latin-1"))
    partes.append(f"trailer\n<< /Size {n} /Root 1 0 R >>\nstartxref\n{xref_pos}\n%%EOF".encode("latin-1"))
    return b"".join(partes)
