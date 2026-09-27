"""PAGAMENTOS PARCIAIS — a cliente deixa um sinal e paga o resto depois.

Antes desta funcionalidade uma fatura era paga ou não era, o que obrigava a
mentir em metade dos atendimentos. Aqui garante-se que:
  • o estado da fatura segue o dinheiro sozinho (issued -> partial -> paid)
  • o dinheiro do CLIENTE vem daqui e não do preço da marcação
  • nada aceita dinheiro a mais, valores negativos ou métodos inventados
"""

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


def _fatura_emitida(tel="41790000801", hora="09:00", nome="Ana Müller"):
    a = _marca(tel, "limpeza_pele", hora, nome)
    inv = bi.gerar_fatura_de_marcacao(a)
    return bi.emitir_fatura(inv["id"])


# ---------------------------------------------------------------- ciclo normal
def test_sinal_deixa_a_fatura_parcial(base_dados):
    inv = _fatura_emitida()
    total = inv["total_cents"]

    f = bi.registar_pagamento(inv["id"], total // 4, bi.PAGAMENTO_TWINT)
    assert f["status"] == bi.STATUS_PARCIAL
    assert f["paid_cents"] == total // 4
    assert f["due_cents"] == total - total // 4


def test_resto_fecha_a_fatura(base_dados):
    inv = _fatura_emitida(hora="10:30")
    total = inv["total_cents"]

    bi.registar_pagamento(inv["id"], 1000, bi.PAGAMENTO_TWINT)
    f = bi.registar_pagamento(inv["id"], total - 1000, bi.PAGAMENTO_CASH)

    assert f["status"] == bi.STATUS_PAGA
    assert f["due_cents"] == 0
    assert f["paid_at"] is not None
    assert len(f["payments"]) == 2
    # método principal = o do maior pagamento
    assert f["payment_method"] == bi.PAGAMENTO_CASH


def test_marcar_paga_continua_a_funcionar_de_uma_vez(base_dados):
    inv = _fatura_emitida(hora="11:00")
    f = bi.marcar_paga(inv["id"], metodo=bi.PAGAMENTO_CARTAO)
    assert f["status"] == bi.STATUS_PAGA
    assert f["due_cents"] == 0
    assert len(f["payments"]) == 1
    # idempotente: chamar outra vez não cria um segundo pagamento
    f2 = bi.marcar_paga(inv["id"])
    assert len(f2["payments"]) == 1


def test_marcar_paga_depois_de_um_sinal_paga_so_o_que_falta(base_dados):
    inv = _fatura_emitida(hora="14:00")
    total = inv["total_cents"]
    bi.registar_pagamento(inv["id"], 2000, bi.PAGAMENTO_CASH)
    f = bi.marcar_paga(inv["id"], metodo=bi.PAGAMENTO_TWINT)
    assert f["status"] == bi.STATUS_PAGA
    assert f["paid_cents"] == total
    assert sum(p["amount_cents"] for p in f["payments"]) == total


# ------------------------------------------------------------------- recusas
def test_recusa_dinheiro_a_mais(base_dados):
    inv = _fatura_emitida(hora="15:00")
    with pytest.raises(bi.PagamentoInvalido):
        bi.registar_pagamento(inv["id"], inv["total_cents"] + 1, bi.PAGAMENTO_CASH)


def test_recusa_valor_nao_positivo(base_dados):
    inv = _fatura_emitida(hora="15:30")
    for valor in (0, -100):
        with pytest.raises(bi.PagamentoInvalido):
            bi.registar_pagamento(inv["id"], valor, bi.PAGAMENTO_CASH)


def test_recusa_metodo_desconhecido(base_dados):
    inv = _fatura_emitida(hora="16:00")
    with pytest.raises(bi.PagamentoInvalido):
        bi.registar_pagamento(inv["id"], 1000, "bitcoin")


def test_rascunho_nao_recebe_pagamentos(base_dados):
    a = _marca("41790000802", "limpeza_pele", "16:30")
    inv = bi.gerar_fatura_de_marcacao(a)
    assert inv["status"] == bi.STATUS_RASCUNHO
    with pytest.raises(bi.TransicaoInvalida):
        bi.registar_pagamento(inv["id"], 1000, bi.PAGAMENTO_CASH)


def test_fatura_com_pagamentos_nao_se_anula(base_dados):
    inv = _fatura_emitida(hora="17:00")
    bi.registar_pagamento(inv["id"], 1000, bi.PAGAMENTO_CASH)
    with pytest.raises(bi.TransicaoInvalida):
        bi.anular_fatura(inv["id"])


# ------------------------------------------------------------------- remoção
def test_remover_pagamento_faz_a_fatura_recuar(base_dados):
    inv = _fatura_emitida(hora="17:30")
    bi.registar_pagamento(inv["id"], 1000, bi.PAGAMENTO_TWINT)
    f = bi.marcar_paga(inv["id"])
    assert f["status"] == bi.STATUS_PAGA

    ultimo = max(f["payments"], key=lambda p: p["amount_cents"])
    f = bi.remover_pagamento(ultimo["id"])
    assert f["status"] == bi.STATUS_PARCIAL
    assert f["paid_cents"] == 1000
    assert f["paid_at"] is None                      # deixou de estar paga

    restante = f["payments"][0]
    f = bi.remover_pagamento(restante["id"])
    assert f["status"] == bi.STATUS_EMITIDA
    assert f["paid_cents"] == 0


# ------------------------------------------------------- dinheiro do cliente
def test_pagamento_actualiza_os_contadores_do_cliente(base_dados):
    inv = _fatura_emitida(tel="41790000803", hora="08:00", nome="Recorrente")
    cid = inv["customer_id"]
    total = inv["total_cents"]

    assert db.obter_customer(cid)["billed_cents"] == total
    assert db.obter_customer(cid)["paid_cents"] == 0

    bi.registar_pagamento(inv["id"], 2500, bi.PAGAMENTO_TWINT)
    cust = db.obter_customer(cid)
    assert cust["paid_cents"] == 2500
    assert cust["billed_cents"] == total

    bi.marcar_paga(inv["id"])
    assert db.obter_customer(cid)["paid_cents"] == total


# ------------------------------------------------------------------------ API
_AUTH = {"Authorization": "Basic " + __import__("base64").b64encode(b"painel:painel-pw").decode()}


def test_api_registar_e_remover_pagamento(base_dados, cliente_http):
    inv = _fatura_emitida(tel="41790000804", hora="12:00", nome="API")
    total = inv["total_cents"]

    r = cliente_http.post(f"/api/faturas/{inv['id']}/pagamentos",
                          json={"valor": "20.00", "metodo": "twint"}, headers=_AUTH)
    assert r.status_code == 201
    corpo = r.get_json()
    assert corpo["status"] == "partial"
    assert corpo["paid_cents"] == 2000
    assert corpo["due_cents"] == total - 2000

    pid = corpo["payments"][0]["id"]
    r = cliente_http.delete(f"/api/faturas/{inv['id']}/pagamentos/{pid}", headers=_AUTH)
    assert r.status_code == 200
    assert r.get_json()["status"] == "issued"


def test_api_recusa_valor_a_mais_com_400(base_dados, cliente_http):
    inv = _fatura_emitida(tel="41790000805", hora="13:00")
    r = cliente_http.post(f"/api/faturas/{inv['id']}/pagamentos",
                          json={"amount_cents": inv["total_cents"] + 1}, headers=_AUTH)
    assert r.status_code == 400
    assert "mais" in r.get_json()["erro"]


def test_api_pagamentos_exige_autenticacao(base_dados, cliente_http):
    inv = _fatura_emitida(tel="41790000806", hora="14:30")
    assert cliente_http.post(f"/api/faturas/{inv['id']}/pagamentos",
                             json={"valor": "10"}).status_code == 401


def test_api_lista_faturas_traz_por_receber(base_dados, cliente_http):
    inv = _fatura_emitida(tel="41790000807", hora="15:45")
    cliente_http.post(f"/api/faturas/{inv['id']}/pagamentos",
                      json={"valor": "10.00"}, headers=_AUTH)
    r = cliente_http.get("/api/faturas?estado=partial", headers=_AUTH)
    assert r.status_code == 200
    linha = next(f for f in r.get_json() if f["id"] == inv["id"])
    assert linha["due_cents"] == inv["total_cents"] - 1000


def test_api_metodos_pagamento(base_dados, cliente_http):
    r = cliente_http.get("/api/metodos-pagamento", headers=_AUTH)
    assert r.status_code == 200
    ids = [m["id"] for m in r.get_json()["metodos"]]
    assert "twint" in ids and "cash" in ids
