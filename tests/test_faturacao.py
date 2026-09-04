"""BILLING ENGINE — geração idempotente, numeração transacional, snapshots,
estados, preço-a-confirmar, IVA desligado por omissão."""

import threading

import pytest

import bot
import db
import tempo
from billing import engine as bi
from conftest import data_pt


def _marca(tel, sid, hora, nome="Cliente Teste", dia=None):
    dia = dia or tempo.hoje_zurique()
    s = db.obter_servico(sid)
    sess = {"idioma": "pt", "nome": nome, "servico_id": sid, "servico": s["nome_pt"],
            "duracao_min": s["duracao_min"], "duracao": f"{s['duracao_min']} min",
            "preco_cents": s["preco_cents"],
            "preco": round(s["preco_cents"] / 100, 2) if s["preco_cents"] is not None else None,
            "data": data_pt(dia.isoformat()), "hora": hora}
    return bot.guardar_agendamento(tel, sess)


def test_gerar_fatura_de_marcacao_com_preco(base_dados):
    a = _marca("41790000701", "limpeza_pele", "09:00", "Ana Müller")
    inv = bi.gerar_fatura_de_marcacao(a)
    assert inv["status"] == "draft"
    assert inv["invoice_number"] is None            # número só na emissão
    assert inv["subtotal_cents"] == 8000 and inv["total_cents"] == 8000
    assert inv["tax_cents"] == 0                     # IVA desligado por omissão
    assert inv["business_name_snapshot"] == "Daniela Beauty"
    assert inv["customer_name_snapshot"] == "Ana Müller"
    assert [l["description"] for l in inv["lines"]] == ["Limpeza de pele"]


def test_idempotencia_uma_marcacao_uma_fatura(base_dados):
    a = _marca("41790000702", "limpeza_pele", "09:00")
    i1 = bi.gerar_fatura_de_marcacao(a)
    i2 = bi.gerar_fatura_de_marcacao(a)
    i3 = bi.gerar_fatura_de_marcacao(a)
    assert i1["id"] == i2["id"] == i3["id"]
    with db.ligacao() as c:
        n = c.execute("SELECT COUNT(*) FROM invoices WHERE appointment_id = ?", (a,)).fetchone()[0]
    assert n == 1


def test_preco_a_confirmar_exige_preco(base_dados):
    a = _marca("41790000703", "pestanas", "11:00")     # pestanas = preço NULL
    assert bot.obter_agendamento(a)["preco_cents"] is None
    with pytest.raises(bi.PrecoEmFalta):
        bi.gerar_fatura_de_marcacao(a)
    inv = bi.gerar_fatura_de_marcacao(a, preco_cents=6500)
    assert inv["total_cents"] == 6500
    # o preço fica na marcação, NÃO no catálogo global
    assert bot.obter_agendamento(a)["preco_cents"] == 6500
    assert db.obter_servico("pestanas")["preco_cents"] is None


def test_preco_negativo_recusado(base_dados):
    a = _marca("41790000704", "pestanas", "11:00")
    with pytest.raises(bi.ErroFaturacao):
        bi.gerar_fatura_de_marcacao(a, preco_cents=-1)


def test_numeracao_sequencial_sem_buracos_na_emissao(base_dados):
    ids = [_marca("41790000705", "limpeza_pele", h) for h in ("09:00", "10:30", "12:00")]
    invs = [bi.gerar_fatura_de_marcacao(a) for a in ids]
    nums = [bi.emitir_fatura(inv["id"])["invoice_number"] for inv in invs]
    ano = tempo.hoje_zurique().year
    assert nums == [f"{ano}-0001", f"{ano}-0002", f"{ano}-0003"]


def test_numeracao_concorrente_nunca_repete(base_dados):
    ids = [_marca("41790000706", "limpeza_pele", f"{8+i:02d}:00") for i in range(8)]
    invs = [bi.gerar_fatura_de_marcacao(a)["id"] for a in ids]
    numeros = []
    lock = threading.Lock()

    def emitir(iid):
        n = bi.emitir_fatura(iid)["invoice_number"]
        with lock:
            numeros.append(n)

    ths = [threading.Thread(target=emitir, args=(i,)) for i in invs]
    for t in ths:
        t.start()
    for t in ths:
        t.join()
    assert len(numeros) == len(set(numeros)) == 8      # nenhum número repetido


def test_snapshot_congela_valores_apos_emissao(base_dados):
    a = _marca("41790000707", "limpeza_pele", "09:00")
    inv = bi.emitir_fatura(bi.gerar_fatura_de_marcacao(a)["id"])
    assert inv["total_cents"] == 8000
    # muda o preço GLOBAL do serviço
    db.atualizar_servico("limpeza_pele", {"preco_cents": 9500})
    depois = bi.obter_fatura(inv["id"])
    assert depois["total_cents"] == 8000               # fatura emitida não muda
    assert depois["lines"][0]["unit_price_cents"] == 8000


def test_estados_draft_issued_paid_cancelled(base_dados):
    a = _marca("41790000708", "limpeza_pele", "09:00")
    inv = bi.gerar_fatura_de_marcacao(a)
    assert inv["status"] == "draft"
    inv = bi.emitir_fatura(inv["id"]);   assert inv["status"] == "issued"
    inv = bi.emitir_fatura(inv["id"]);   assert inv["status"] == "issued"   # idempotente
    inv = bi.marcar_paga(inv["id"]);     assert inv["status"] == "paid" and inv["paid_at"]
    with pytest.raises(bi.TransicaoInvalida):
        bi.anular_fatura(inv["id"])                    # paga não se anula


def test_anular_rascunho_liberta_a_marcacao_para_nova_fatura(base_dados):
    a = _marca("41790000709", "limpeza_pele", "09:00")
    i1 = bi.gerar_fatura_de_marcacao(a)
    bi.anular_fatura(i1["id"])
    i2 = bi.gerar_fatura_de_marcacao(a)               # já pode gerar outra
    assert i2["id"] != i1["id"]


def test_iva_quando_ligado(base_dados):
    bi.guardar_definicoes_faturacao({"vat_enabled": True, "vat_rate_bps": 810})
    a = _marca("41790000710", "limpeza_pele", "09:00")
    inv = bi.gerar_fatura_de_marcacao(a)
    assert inv["tax_rate_bps"] == 810
    assert inv["tax_cents"] == round(8000 * 810 / 10000)      # 648
    assert inv["total_cents"] == 8000 + 648


def test_definicoes_nao_inventam_dados(base_dados):
    cfg = bi.definicoes_faturacao()
    assert cfg["vat_enabled"] is False
    assert cfg["iban"] is None and cfg["vat_number"] is None
    assert cfg["legal_name"] == "Daniela Beauty"       # só o que veio do ambiente


def test_desconto_em_rascunho(base_dados):
    a = _marca("41790000711", "limpeza_pele", "09:00")
    inv = bi.gerar_fatura_de_marcacao(a)
    inv = bi.atualizar_rascunho(inv["id"], {"discount_cents": 1000})
    assert inv["discount_cents"] == 1000 and inv["total_cents"] == 7000


def test_fatura_do_cliente_aparece_no_crm(base_dados):
    a = _marca("41790000712", "limpeza_pele", "09:00", "Rita Kern")
    cid = bot.obter_agendamento(a)["customer_id"]
    bi.emitir_fatura(bi.gerar_fatura_de_marcacao(a)["id"])
    fs = bi.faturas_do_cliente(cid)
    assert len(fs) == 1 and fs[0]["status"] == "issued"


# ---- API HTTP ----------------------------------------------------------------
import base64  # noqa: E402

_AUTH = {"Authorization": "Basic " + base64.b64encode(b"painel:painel-pw").decode()}


def test_api_gerar_emitir_pagar(cliente_http, base_dados):
    a = _marca("41790000720", "limpeza_pele", "09:00", "Ana Müller")
    r = cliente_http.post(f"/api/agendamentos/{a}/fatura", headers=_AUTH)
    assert r.status_code == 201
    inv = r.get_json()
    assert inv["status"] == "draft" and inv["total_cents"] == 8000
    # idempotente
    r2 = cliente_http.post(f"/api/agendamentos/{a}/fatura", headers=_AUTH)
    assert r2.get_json()["id"] == inv["id"]
    r = cliente_http.post(f"/api/faturas/{inv['id']}/emitir", headers=_AUTH)
    assert r.status_code == 200 and r.get_json()["invoice_number"]
    r = cliente_http.post(f"/api/faturas/{inv['id']}/pagar", headers=_AUTH)
    assert r.get_json()["status"] == "paid"
    r = cliente_http.get("/api/faturas?estado=paid", headers=_AUTH)
    assert len(r.get_json()) == 1


def test_api_preco_a_confirmar_devolve_409(cliente_http, base_dados):
    a = _marca("41790000721", "pestanas", "11:00")
    r = cliente_http.post(f"/api/agendamentos/{a}/fatura", headers=_AUTH)
    assert r.status_code == 409 and r.get_json()["precisa_preco"] is True
    r = cliente_http.post(f"/api/agendamentos/{a}/fatura",
                          json={"preco_cents": 6500}, headers=_AUTH)
    assert r.status_code == 201 and r.get_json()["total_cents"] == 6500


def test_api_preco_nao_numerico_devolve_400(cliente_http, base_dados):
    a = _marca("41790000722", "pestanas", "11:00")
    r = cliente_http.post(f"/api/agendamentos/{a}/fatura",
                          json={"preco_cents": "x"}, headers=_AUTH)
    assert r.status_code == 400


def test_api_definicoes_faturacao(cliente_http, base_dados):
    r = cliente_http.get("/api/definicoes/faturacao", headers=_AUTH)
    assert r.get_json()["vat_enabled"] is False
    r = cliente_http.put("/api/definicoes/faturacao",
                         json={"vat_enabled": True, "vat_rate_bps": 810, "iban": "CH.."}, headers=_AUTH)
    assert r.get_json()["vat_enabled"] is True and r.get_json()["vat_rate_bps"] == 810


def test_api_fatura_sem_auth(cliente_http, base_dados):
    r = cliente_http.get("/api/faturas")
    assert r.status_code == 401


def test_api_patch_rascunho_e_anular(cliente_http, base_dados):
    a = _marca("41790000723", "limpeza_pele", "09:00")
    inv = cliente_http.post(f"/api/agendamentos/{a}/fatura", headers=_AUTH).get_json()
    r = cliente_http.patch(f"/api/faturas/{inv['id']}",
                           json={"discount_cents": 500, "notes": "obrigada"}, headers=_AUTH)
    assert r.status_code == 200
    corpo = r.get_json()
    assert corpo["discount_cents"] == 500 and corpo["total_cents"] == 7500
    assert corpo["notes"] == "obrigada"
    r = cliente_http.post(f"/api/faturas/{inv['id']}/anular", headers=_AUTH)
    assert r.status_code == 200 and r.get_json()["status"] == "cancelled"


def test_api_accao_invalida_devolve_400(cliente_http, base_dados):
    a = _marca("41790000724", "limpeza_pele", "09:00")
    inv = cliente_http.post(f"/api/agendamentos/{a}/fatura", headers=_AUTH).get_json()
    r = cliente_http.post(f"/api/faturas/{inv['id']}/reabrir", headers=_AUTH)
    assert r.status_code == 400


def test_api_fatura_desconhecida_404(cliente_http, base_dados):
    assert cliente_http.get("/api/faturas/999999", headers=_AUTH).status_code == 404


# ---- core: caminhos de erro, edição de linhas, filtros -----------------------
def test_gerar_fatura_marcacao_inexistente(base_dados):
    with pytest.raises(bi.FaturaNaoEncontrada):
        bi.gerar_fatura_de_marcacao(999999)


def test_obter_fatura_desconhecida_devolve_none(base_dados):
    assert bi.obter_fatura(123456) is None


def test_atualizar_rascunho_substitui_linhas_com_quantidades(base_dados):
    a = _marca("41790000730", "limpeza_pele", "09:00")
    inv = bi.gerar_fatura_de_marcacao(a)
    inv = bi.atualizar_rascunho(inv["id"], {"lines": [
        {"description": "Limpeza", "quantity": 2, "unit_price_cents": 8000},
        {"description": "Produto", "quantity": 3, "unit_price_cents": 1500},
    ]})
    assert [l["line_total_cents"] for l in inv["lines"]] == [16000, 4500]
    assert inv["subtotal_cents"] == 20500 and inv["total_cents"] == 20500


def test_atualizar_rascunho_recusa_linha_negativa(base_dados):
    a = _marca("41790000731", "limpeza_pele", "09:00")
    inv = bi.gerar_fatura_de_marcacao(a)
    with pytest.raises(bi.ErroFaturacao):
        bi.atualizar_rascunho(inv["id"], {"lines": [
            {"description": "X", "quantity": 1, "unit_price_cents": -10}]})


def test_atualizar_rascunho_desconto_nao_passa_o_subtotal(base_dados):
    a = _marca("41790000732", "limpeza_pele", "09:00")
    inv = bi.gerar_fatura_de_marcacao(a)
    inv = bi.atualizar_rascunho(inv["id"], {"discount_cents": 999999})
    assert inv["discount_cents"] == 8000 and inv["total_cents"] == 0


def test_atualizar_rascunho_so_em_draft(base_dados):
    a = _marca("41790000733", "limpeza_pele", "09:00")
    inv = bi.emitir_fatura(bi.gerar_fatura_de_marcacao(a)["id"])
    with pytest.raises(bi.TransicaoInvalida):
        bi.atualizar_rascunho(inv["id"], {"discount_cents": 100})


def test_atualizar_rascunho_fatura_inexistente(base_dados):
    with pytest.raises(bi.FaturaNaoEncontrada):
        bi.atualizar_rascunho(999999, {"notes": "x"})


def test_emitir_fatura_inexistente(base_dados):
    with pytest.raises(bi.FaturaNaoEncontrada):
        bi.emitir_fatura(999999)


def test_marcar_paga_exige_fatura_emitida(base_dados):
    a = _marca("41790000734", "limpeza_pele", "09:00")
    inv = bi.gerar_fatura_de_marcacao(a)
    with pytest.raises(bi.TransicaoInvalida):
        bi.marcar_paga(inv["id"])


def test_marcar_paga_e_anular_sao_idempotentes(base_dados):
    a = _marca("41790000735", "limpeza_pele", "09:00")
    inv = bi.emitir_fatura(bi.gerar_fatura_de_marcacao(a)["id"])
    p1 = bi.marcar_paga(inv["id"])
    p2 = bi.marcar_paga(inv["id"])
    assert p1["paid_at"] == p2["paid_at"] and p2["status"] == "paid"

    b = _marca("41790000736", "limpeza_pele", "10:30")
    inv_b = bi.gerar_fatura_de_marcacao(b)
    c1 = bi.anular_fatura(inv_b["id"])
    c2 = bi.anular_fatura(inv_b["id"])
    assert c1["cancelled_at"] == c2["cancelled_at"] and c2["status"] == "cancelled"


def test_anular_fatura_emitida(base_dados):
    a = _marca("41790000737", "limpeza_pele", "09:00")
    inv = bi.emitir_fatura(bi.gerar_fatura_de_marcacao(a)["id"])
    assert bi.anular_fatura(inv["id"])["status"] == "cancelled"


def test_due_date_usa_prazo_de_pagamento(base_dados):
    from datetime import date, timedelta
    bi.guardar_definicoes_faturacao({"payment_terms_days": 14})
    a = _marca("41790000738", "limpeza_pele", "09:00")
    inv = bi.emitir_fatura(bi.gerar_fatura_de_marcacao(a)["id"])
    esperado = (date.fromisoformat(inv["issue_date"]) + timedelta(days=14)).isoformat()
    assert inv["due_date"] == esperado


def test_business_address_snapshot_congelado(base_dados):
    a = _marca("41790000739", "limpeza_pele", "09:00")
    inv = bi.gerar_fatura_de_marcacao(a)
    assert inv["business_address_snapshot"] == "Rua de Teste 1, Visp"
    bi.guardar_definicoes_faturacao({"address": "Nova Morada 9"})
    assert bi.obter_fatura(inv["id"])["business_address_snapshot"] == "Rua de Teste 1, Visp"


def test_listar_faturas_filtra_por_estado(base_dados):
    ids = [_marca("41790000740", "limpeza_pele", h) for h in ("09:00", "10:30", "12:00")]
    invs = [bi.gerar_fatura_de_marcacao(a) for a in ids]
    bi.emitir_fatura(invs[0]["id"])
    bi.emitir_fatura(invs[1]["id"])
    assert len(bi.listar_faturas(status="draft")) == 1
    assert len(bi.listar_faturas(status="issued")) == 2
    assert len(bi.listar_faturas(status="all")) == 3
    assert len(bi.listar_faturas()) == 3


def test_faturas_do_cliente_ignora_anuladas(base_dados):
    a = _marca("41790000741", "limpeza_pele", "09:00", "Rita Kern")
    cid = bot.obter_agendamento(a)["customer_id"]
    inv = bi.gerar_fatura_de_marcacao(a)
    assert len(bi.faturas_do_cliente(cid)) == 1
    bi.anular_fatura(inv["id"])
    assert bi.faturas_do_cliente(cid) == []


def test_guardar_definicoes_recusa_numero_invalido(base_dados):
    with pytest.raises(bi.ErroFaturacao):
        bi.guardar_definicoes_faturacao({"vat_rate_bps": "oito"})


def test_guardar_definicoes_so_toca_campos_permitidos(base_dados):
    cfg = bi.guardar_definicoes_faturacao({"iban": "CH93 0076", "tenant_id": 99,
                                           "vat_rate_bps": -5})
    assert cfg["iban"] == "CH93 0076"
    assert cfg["vat_rate_bps"] == 0            # negativo -> 0
    assert cfg["currency"] == "CHF"            # intacto
