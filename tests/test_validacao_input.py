"""#8 — input malformado no painel devolve 400, nunca 500.
Preço negativo é recusado."""

import base64

AUTH = {"Authorization": "Basic " + base64.b64encode(b"painel:painel-pw").decode()}


def _j(r):
    return r.get_json() or {}


def test_atraso_minutos_nao_numerico_da_400(cliente_http, base_dados):
    r = cliente_http.post("/api/painel/atraso", json={"minutos": "muitos"}, headers=AUTH)
    assert r.status_code == 400
    assert "erro" in _j(r)


def test_servico_preco_negativo_recusado(cliente_http, base_dados):
    r = cliente_http.post("/api/servicos", json={
        "id": "neg", "nome_pt": "Neg", "duracao_min": 30, "preco_cents": -100}, headers=AUTH)
    assert r.status_code == 400
    assert __import__("db").obter_servico("neg") is None


def test_servico_preco_nao_numerico_da_400(cliente_http, base_dados):
    r = cliente_http.post("/api/servicos", json={
        "id": "abc", "nome_pt": "Abc", "duracao_min": 30, "preco_cents": "gratis"}, headers=AUTH)
    assert r.status_code == 400


def test_servico_duracao_fora_de_limites_da_400(cliente_http, base_dados):
    r = cliente_http.post("/api/servicos", json={
        "id": "longo", "nome_pt": "Longo", "duracao_min": 99999}, headers=AUTH)
    assert r.status_code == 400


def test_patch_servico_buffer_invalido_da_400(cliente_http, base_dados):
    import db
    db.criar_servico({"id": "svc1", "nome_pt": "S", "duracao_min": 30, "preco_cents": 1000})
    r = cliente_http.patch("/api/servicos/svc1", json={"buffer_before_min": "abc"}, headers=AUTH)
    assert r.status_code == 400
    r = cliente_http.patch("/api/servicos/svc1", json={"preco_cents": -5}, headers=AUTH)
    assert r.status_code == 400


def test_patch_servico_rebook_days_nao_numerico_da_400(cliente_http, base_dados):
    import db
    db.criar_servico({"id": "svc2", "nome_pt": "S", "duracao_min": 30, "preco_cents": 1000})
    r = cliente_http.patch("/api/servicos/svc2", json={"rebook_days": "quando calhar"}, headers=AUTH)
    assert r.status_code == 400


def test_horarios_grelha_com_entradas_nao_objeto_da_400(cliente_http, base_dados):
    r = cliente_http.put("/api/horarios", json={"grelha": [1, 2, 3, 4, 5, 6, 7]}, headers=AUTH)
    assert r.status_code == 400


def test_excecao_data_invalida_da_400(cliente_http, base_dados):
    r = cliente_http.post("/api/horarios/excecoes",
                          json={"data_inicio": "2026-13-45"}, headers=AUTH)
    assert r.status_code == 400
    r = cliente_http.post("/api/horarios/excecoes",
                          json={"data_inicio": "não é data"}, headers=AUTH)
    assert r.status_code == 400


def test_excecao_intervalo_invertido_da_400(cliente_http, base_dados):
    r = cliente_http.post("/api/horarios/excecoes",
                          json={"data_inicio": "2026-10-10", "data_fim": "2026-10-01"}, headers=AUTH)
    assert r.status_code == 400
