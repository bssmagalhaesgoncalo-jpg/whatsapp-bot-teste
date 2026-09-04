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
