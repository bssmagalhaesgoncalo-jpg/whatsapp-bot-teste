"""Testes 1-3 + preço a confirmar: catálogo e durações."""

import catalogo
import db


def test_1_limpeza_pele_60min(base_dados):
    s = db.obter_servico("limpeza_pele")
    assert s["duracao_min"] == 60
    assert s["preco_cents"] == 8000
    assert catalogo.duracao_label(s["duracao_min"]) == "1h"


def test_2_design_sobrancelhas_30min(base_dados):
    s = db.obter_servico("design_sobrancelhas")
    assert s["duracao_min"] == 30
    assert s["preco_cents"] == 2500
    assert catalogo.duracao_label(s["duracao_min"]) == "30 min"


def test_3_pestanas_120min(base_dados):
    s = db.obter_servico("pestanas")
    assert s["duracao_min"] == 120
    assert catalogo.duracao_label(s["duracao_min"]) == "2h"


def test_17_preco_null_nunca_e_chf_zero(base_dados):
    for sid in ("brow_lamination", "pestanas", "dermaplaning"):
        s = db.obter_servico(sid)
        assert s["preco_cents"] is None
        for idioma in ("pt", "de", "en"):
            rotulo = catalogo.preco_label(s, idioma)
            assert "0" not in rotulo
            assert rotulo == catalogo.PRECO_A_CONFIRMAR[idioma]
        assert catalogo.preco_label_painel(s) == "A confirmar"


def test_catalogo_e_a_unica_fonte(base_dados):
    ativos = db.listar_servicos()
    assert {s["id"] for s in ativos} == {
        "limpeza_pele", "design_sobrancelhas", "brow_lamination", "pestanas", "dermaplaning"}
