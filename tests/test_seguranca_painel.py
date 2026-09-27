"""
Limite de tentativas no HTTP Basic + headers de segurança (core/seguranca.py).

Atrás do painel estão nomes, telemóveis e faturas de clientes reais.
"""

from __future__ import annotations

import pytest

from core import seguranca

AUTH_OK = ("painel", "painel-pw")
AUTH_MA = ("painel", "errada")


def test_tentativas_falhadas_levam_a_429(cliente_http):
    for _ in range(seguranca.TENTATIVAS_MAX):
        r = cliente_http.get("/api/painel/hoje", auth=AUTH_MA)
        assert r.status_code == 401
    r = cliente_http.get("/api/painel/hoje", auth=AUTH_MA)
    assert r.status_code == 429
    assert r.headers.get("Retry-After")


def test_o_limite_tranca_tambem_quem_sabe_a_password(cliente_http):
    """Deliberado: passado o limite, o IP fica de fora até a janela passar.
    Deixar entrar quem acerta seria um oráculo — o atacante saberia que
    acertou apesar do 429."""
    for _ in range(seguranca.TENTATIVAS_MAX):
        cliente_http.get("/api/painel/hoje", auth=AUTH_MA)
    assert cliente_http.get("/api/painel/hoje", auth=AUTH_OK).status_code == 429


def test_logins_certos_nunca_contam_para_o_limite(cliente_http):
    for _ in range(seguranca.TENTATIVAS_MAX * 2):
        assert cliente_http.get("/api/painel/hoje", auth=AUTH_OK).status_code == 200


def test_o_limite_e_por_ip(cliente_http):
    for _ in range(seguranca.TENTATIVAS_MAX):
        cliente_http.get("/api/painel/hoje", auth=AUTH_MA,
                         headers={"X-Forwarded-For": "203.0.113.9"})
    assert cliente_http.get("/api/painel/hoje", auth=AUTH_MA,
                            headers={"X-Forwarded-For": "203.0.113.9"}).status_code == 429
    # Outro IP não é afetado — um bot a bater à porta não tranca a Daniela fora.
    assert cliente_http.get("/api/painel/hoje", auth=AUTH_OK,
                            headers={"X-Forwarded-For": "198.51.100.4"}).status_code == 200


def test_janela_expira(monkeypatch):
    for _ in range(seguranca.TENTATIVAS_MAX):
        seguranca.registar_falha("1.2.3.4", agora=1000.0)
    assert seguranca.excedeu_tentativas("1.2.3.4", agora=1000.0) is True
    assert seguranca.excedeu_tentativas("1.2.3.4", agora=1000.0 + seguranca.JANELA_SEG + 1) is False


def test_o_painel_tranca_a_porta_da_frente(cliente_http):
    for _ in range(seguranca.TENTATIVAS_MAX):
        assert cliente_http.get("/app", auth=AUTH_MA).status_code == 401
    assert cliente_http.get("/app", auth=AUTH_MA).status_code == 429


# ---------------------------------------------------------------------------
# Headers
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("caminho,auth", [
    ("/api/painel/hoje", AUTH_OK),
    ("/app", AUTH_OK),
    ("/webhook", None),
])
def test_headers_de_seguranca_em_todas_as_respostas(cliente_http, caminho, auth):
    r = cliente_http.get(caminho, auth=auth)
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert r.headers["X-Frame-Options"] == "DENY"
    assert r.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"
    assert "max-age=" in r.headers["Strict-Transport-Security"]
    assert "default-src 'self'" in r.headers["Content-Security-Policy"]


def test_csp_nao_parte_o_painel(cliente_http):
    """O app.js é do próprio domínio (script-src 'self' chega), mas os
    estilos inline e o Google Fonts do shell têm de continuar a passar."""
    csp = cliente_http.get("/app", auth=AUTH_OK).headers["Content-Security-Policy"]
    assert "script-src 'self';" in csp
    assert "'unsafe-inline'" not in csp.split("style-src")[0]     # nunca em script-src
    assert "fonts.googleapis.com" in csp
    assert "fonts.gstatic.com" in csp
    assert "frame-ancestors 'none'" in csp


def test_pdf_publico_da_fatura_continua_a_abrir(cliente_http, base_dados):
    """A rota pública do PDF não pode ser partida pelos headers — é o link
    que a cliente recebe no WhatsApp."""
    import db
    import billing.engine as bi
    from tests.conftest import marcar, data_pt
    from datetime import date, timedelta

    amanha = (date.today() + timedelta(days=3)).isoformat()
    servico = db.listar_servicos()[0]["id"]
    aid = marcar("41791110001", servico, data_pt(amanha), "10:00")
    fatura = bi.gerar_fatura_de_marcacao(aid)
    bi.emitir_fatura(fatura["id"])
    token = bi.garantir_pdf_token(fatura["id"])

    r = cliente_http.get(f"/faturas/pdf/{token}")
    assert r.status_code == 200
    assert r.mimetype == "application/pdf"
    assert r.headers["Content-Disposition"].startswith("inline")
    assert r.headers["X-Content-Type-Options"] == "nosniff"


# ---------------------------------------------------------------------------
# UIs antigas desligadas
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("caminho", ["/painel", "/painel/hoje", "/dashboard"])
def test_uis_antigas_redirecionam_para_app(cliente_http, caminho):
    r = cliente_http.get(caminho, auth=AUTH_OK)
    assert r.status_code == 302
    assert r.headers["Location"].endswith("/app")


@pytest.mark.parametrize("caminho", ["/painel", "/dashboard"])
def test_uis_antigas_continuam_a_exigir_credenciais(cliente_http, caminho):
    """O redirect não pode ser uma porta aberta: sem credenciais, 401 —
    nunca um encaminhamento."""
    assert cliente_http.get(caminho).status_code == 401
