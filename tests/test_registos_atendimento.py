"""Bloco 3 — notas e fotos antes/depois por atendimento.

Cobre: migração 26, CRUD de notas, upload de fotos com miniatura, limites e
validações (tipo, formato, tamanho, path traversal), autenticação em todas
as rotas (incluindo /media/atendimentos), a ficha do cliente a devolver os
registos por visita, e o intake de fotos via WhatsApp (download mockado —
nunca chama a Meta)."""

import base64
import io

import pytest
from PIL import Image

import bot
import config
import db
import tempo
from conftest import marcar, data_pt, dias_abertos

AUTH = {"Authorization": "Basic " + base64.b64encode(b"painel:painel-pw").decode()}


def _png_bytes(cor=(200, 30, 90), lado=900):
    img = Image.new("RGB", (lado, lado), cor)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


@pytest.fixture(autouse=True)
def media_em_tmp(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "MEDIA_DIR", str(tmp_path / "media"))
    yield


@pytest.fixture
def marcacao(base_dados):
    dia = dias_abertos(1)[0]
    servico = db.listar_servicos()[0]["id"]
    return marcar("41790005555", servico, data_pt(dia), "10:00")


# ---------------------------------------------------------------------------
# Migração / notas
# ---------------------------------------------------------------------------
def test_migracao_26_cria_tabelas(base_dados):
    with db.ligacao() as conn:
        tabelas = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
    assert "service_notes" in tabelas and "service_photos" in tabelas


def test_nota_crud_completo(cliente_http, marcacao):
    r = cliente_http.post(f"/api/agendamentos/{marcacao}/notas",
                          json={"texto": "Pele sensível na zona T."}, headers=AUTH)
    assert r.status_code == 201
    nota = r.get_json()
    assert nota["texto"] == "Pele sensível na zona T."

    r = cliente_http.patch(f"/api/notas/{nota['id']}",
                           json={"texto": "Pele sensível; evitar ácidos."}, headers=AUTH)
    assert r.status_code == 200
    assert r.get_json()["atualizado_em"]

    r = cliente_http.get(f"/api/agendamentos/{marcacao}/registos", headers=AUTH)
    assert [n["texto"] for n in r.get_json()["notas"]] == ["Pele sensível; evitar ácidos."]

    assert cliente_http.delete(f"/api/notas/{nota['id']}", headers=AUTH).status_code == 200
    r = cliente_http.get(f"/api/agendamentos/{marcacao}/registos", headers=AUTH)
    assert r.get_json()["notas"] == []


def test_nota_vazia_e_marcacao_inexistente(cliente_http, marcacao):
    assert cliente_http.post(f"/api/agendamentos/{marcacao}/notas",
                             json={"texto": "   "}, headers=AUTH).status_code == 400
    assert cliente_http.post("/api/agendamentos/999999/notas",
                             json={"texto": "x"}, headers=AUTH).status_code == 404


# ---------------------------------------------------------------------------
# Fotos
# ---------------------------------------------------------------------------
def _upload(cliente_http, marcacao, tipo="antes", conteudo=None, nome="f.png"):
    return cliente_http.post(
        f"/api/agendamentos/{marcacao}/fotos",
        data={"tipo": tipo, "foto": (io.BytesIO(conteudo or _png_bytes()), nome)},
        content_type="multipart/form-data", headers=AUTH)


def test_upload_gera_miniatura_e_serve_autenticado(cliente_http, marcacao):
    r = _upload(cliente_http, marcacao, "antes")
    assert r.status_code == 201
    foto = r.get_json()
    assert foto["tipo"] == "antes" and foto["thumb"].endswith("_thumb.jpg")

    # miniatura existe, é JPEG e o lado maior é <= 480
    from crm import registos
    thumb_path = registos.caminho_seguro(foto["thumb"])
    assert thumb_path
    with Image.open(thumb_path) as im:
        assert im.format == "JPEG" and max(im.size) <= registos.THUMB_LADO_MAX

    # servir: com auth 200, sem auth 401 (fotos de clientes nunca públicas)
    ok = cliente_http.get(f"/media/atendimentos/{foto['ficheiro']}", headers=AUTH)
    assert ok.status_code == 200
    assert ok.headers["X-Content-Type-Options"] == "nosniff"
    assert cliente_http.get(f"/media/atendimentos/{foto['ficheiro']}").status_code == 401


def test_upload_valida_tipo_formato_e_tamanho(cliente_http, marcacao):
    assert _upload(cliente_http, marcacao, tipo="durante").status_code == 400
    assert _upload(cliente_http, marcacao, conteudo=b"nao sou imagem").status_code == 400
    from crm import registos
    gigante = b"x" * (registos.TAMANHO_MAX_BYTES + 1)
    assert _upload(cliente_http, marcacao, conteudo=gigante).status_code == 400


def test_ordem_por_tipo_e_apagar_remove_ficheiros(cliente_http, marcacao):
    ids = [_upload(cliente_http, marcacao, t).get_json() for t in ("antes", "antes", "depois")]
    r = cliente_http.get(f"/api/agendamentos/{marcacao}/registos", headers=AUTH)
    fotos = r.get_json()["fotos"]
    assert [f["ordem"] for f in fotos["antes"]] == [0, 1]
    assert [f["ordem"] for f in fotos["depois"]] == [0]

    from crm import registos
    alvo = ids[0]
    assert registos.caminho_seguro(alvo["ficheiro"])
    assert cliente_http.delete(f"/api/fotos/{alvo['id']}", headers=AUTH).status_code == 200
    assert registos.caminho_seguro(alvo["ficheiro"]) is None      # original apagado
    assert registos.caminho_seguro(alvo["thumb"]) is None         # miniatura também


def test_media_rejeita_path_traversal(cliente_http, base_dados):
    r = cliente_http.get("/media/atendimentos/..%2F..%2Fetc%2Fpasswd", headers=AUTH)
    assert r.status_code == 404


def test_rotas_exigem_autenticacao(cliente_http, marcacao):
    assert cliente_http.get(f"/api/agendamentos/{marcacao}/registos").status_code == 401
    assert cliente_http.post(f"/api/agendamentos/{marcacao}/notas",
                             json={"texto": "x"}).status_code == 401
    assert cliente_http.post(f"/api/agendamentos/{marcacao}/fotos").status_code == 401


# ---------------------------------------------------------------------------
# Ficha do cliente
# ---------------------------------------------------------------------------
def test_ficha_do_cliente_inclui_registos(cliente_http, marcacao):
    _upload(cliente_http, marcacao, "antes")
    cliente_http.post(f"/api/agendamentos/{marcacao}/notas",
                      json={"texto": "Nota da visita."}, headers=AUTH)
    ag = bot.obter_agendamento(marcacao)
    clientes = cliente_http.get("/api/clientes", headers=AUTH).get_json()
    cid = next(c["id"] for c in clientes if c["phone"] == ag["telefone"])
    ficha = cliente_http.get(f"/api/clientes/{cid}", headers=AUTH).get_json()
    visita = next(v for v in ficha["historico"] if v["id"] == marcacao)
    assert visita["registos"]["notas"][0]["texto"] == "Nota da visita."
    assert len(visita["registos"]["fotos"]["antes"]) == 1


# ---------------------------------------------------------------------------
# Intake via WhatsApp
# ---------------------------------------------------------------------------
def _msg_imagem(telefone="41790005555", media_id="MEDIA123"):
    return {"from": telefone, "type": "image", "image": {"id": media_id}}


def test_foto_whatsapp_anexa_a_marcacao_futura_como_antes(base_dados, marcacao, monkeypatch):
    enviados = []
    monkeypatch.setattr(bot._wa, "enviar_texto", lambda n, t: enviados.append((n, t)))
    monkeypatch.setattr(bot._wa, "descarregar_media",
                        lambda mid: (_png_bytes(), "image/png"))
    tratado = bot.receber_foto_de_atendimento("41790005555", "pt", _msg_imagem())
    assert tratado is True
    from crm import registos
    reg = registos.registos_da_marcacao(marcacao)
    assert len(reg["fotos"]["antes"]) == 1          # marcação é amanhã → 'antes'
    assert reg["fotos"]["antes"][0]["origem"] == "whatsapp"
    assert enviados and "Guardei a tua foto" in enviados[0][1]


def test_foto_whatsapp_sem_marcacao_avisa_e_nao_grava(base_dados, monkeypatch):
    enviados = []
    monkeypatch.setattr(bot._wa, "enviar_texto", lambda n, t: enviados.append((n, t)))
    chamou = []
    monkeypatch.setattr(bot._wa, "descarregar_media",
                        lambda mid: chamou.append(mid) or (_png_bytes(), "image/png"))
    tratado = bot.receber_foto_de_atendimento("41799990000", "pt", _msg_imagem("41799990000"))
    assert tratado is True
    assert chamou == []                              # nem descarrega sem destino
    assert enviados and "não encontrei" in enviados[0][1]


def test_foto_whatsapp_download_falhado_cai_no_fallback(base_dados, marcacao, monkeypatch):
    monkeypatch.setattr(bot._wa, "descarregar_media", lambda mid: None)
    assert bot.receber_foto_de_atendimento("41790005555", "pt", _msg_imagem()) is False
