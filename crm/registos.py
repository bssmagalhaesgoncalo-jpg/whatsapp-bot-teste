"""
crm/registos.py — notas e fotos antes/depois de cada atendimento (bloco 3).

Camada única de escrita/leitura dos registos de uma visita. As rotas em
bot.py são finas: validam HTTP e delegam aqui; o intake de fotos vindas do
WhatsApp (bot.py, webhook) também acaba em `guardar_foto()`.

Ficheiros: vivem em `config.MEDIA_DIR/atendimentos` (no Render é o disco
persistente /var/data). A BD guarda só NOMES de ficheiro — nunca caminhos —
por isso mudar de máquina não parte nada. Cada upload gera uma MINIATURA
JPEG (lado maior 480px): o disco é de 1 GB e a ficha da cliente carrega
dezenas de fotos de uma vez — a original só é pedida ao abrir uma foto.

Limites defensivos: 8 MB por ficheiro, tipos image/jpeg|png|webp, e o
Pillow revalida o conteúdo (um .exe renomeado para .jpg não passa).
"""

from __future__ import annotations

import io
import os
import re
import uuid

import config
import db
import tempo

try:
    from PIL import Image, ImageOps
except Exception:  # pragma: no cover — Pillow está em requirements.txt
    Image = None

TAMANHO_MAX_BYTES = 8 * 1024 * 1024
THUMB_LADO_MAX = 480
TIPOS = ("antes", "depois")
_MIME_EXT = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}
_NOME_SEGURO = re.compile(r"^[a-f0-9]{32}(_thumb)?\.(jpg|png|webp)$")


class RegistoInvalido(ValueError):
    """Erro de validação com mensagem própria para mostrar no painel."""


# ---------------------------------------------------------------------------
# Ficheiros
# ---------------------------------------------------------------------------
def diretorio() -> str:
    """Pasta das fotos de atendimento, criada à primeira utilização.

    Lê config.MEDIA_DIR a CADA chamada (não ao importar): os testes trocam
    o diretório por um tmp_path via monkeypatch e o módulo tem de o ver."""
    caminho = os.path.join(config.MEDIA_DIR, "atendimentos")
    os.makedirs(caminho, exist_ok=True)
    return caminho


def caminho_seguro(nome_ficheiro: str) -> str | None:
    """Caminho absoluto de um ficheiro NOSSO, ou None.

    Só serve nomes com a forma exata que nós geramos (uuid hex + extensão):
    qualquer tentativa de path traversal ('../…') nem chega ao filesystem."""
    if not _NOME_SEGURO.match(nome_ficheiro or ""):
        return None
    caminho = os.path.join(diretorio(), nome_ficheiro)
    return caminho if os.path.isfile(caminho) else None


def _gravar_imagem(conteudo: bytes, mime: str) -> tuple[str, str]:
    """Valida o conteúdo com o Pillow, grava original + miniatura.
    Devolve (nome_ficheiro, nome_thumb)."""
    if len(conteudo) > TAMANHO_MAX_BYTES:
        raise RegistoInvalido("A foto excede o limite de 8 MB.")
    ext = _MIME_EXT.get((mime or "").lower())
    if not ext:
        raise RegistoInvalido("Formato não suportado — usa JPEG, PNG ou WebP.")
    if Image is None:  # pragma: no cover
        raise RegistoInvalido("Suporte de imagem indisponível no servidor.")
    try:
        img = Image.open(io.BytesIO(conteudo))
        img.load()
    except Exception:
        raise RegistoInvalido("O ficheiro não é uma imagem válida.")

    base = uuid.uuid4().hex
    nome = f"{base}{ext}"
    nome_thumb = f"{base}_thumb.jpg"
    pasta = diretorio()
    with open(os.path.join(pasta, nome), "wb") as f:
        f.write(conteudo)

    # Miniatura: EXIF endireitado (fotos de telemóvel vêm rodadas), RGB
    # (JPEG não aceita alpha), lado maior 480px, qualidade 80.
    thumb = ImageOps.exif_transpose(img)
    thumb.thumbnail((THUMB_LADO_MAX, THUMB_LADO_MAX))
    if thumb.mode not in ("RGB", "L"):
        thumb = thumb.convert("RGB")
    thumb.save(os.path.join(pasta, nome_thumb), "JPEG", quality=80)
    return nome, nome_thumb


def _apagar_ficheiros(*nomes) -> None:
    for nome in nomes:
        caminho = caminho_seguro(nome or "")
        if caminho:
            try:
                os.remove(caminho)
            except OSError:  # pragma: no cover — nunca falhar por um ficheiro
                pass


# ---------------------------------------------------------------------------
# Notas
# ---------------------------------------------------------------------------
NOTA_MAX_CHARS = 2000


def criar_nota(appointment_id: int, texto: str, tenant_id: int = 1) -> dict:
    texto = (texto or "").strip()
    if not texto:
        raise RegistoInvalido("A nota está vazia.")
    if len(texto) > NOTA_MAX_CHARS:
        raise RegistoInvalido(f"A nota excede {NOTA_MAX_CHARS} caracteres.")
    agora = tempo.iso_utc()
    with db.ligacao() as conn:
        cur = conn.execute(
            "INSERT INTO service_notes (tenant_id, appointment_id, texto, criado_em) "
            "VALUES (?, ?, ?, ?)", (tenant_id, appointment_id, texto, agora))
        return {"id": cur.lastrowid, "appointment_id": appointment_id,
                "texto": texto, "criado_em": agora, "atualizado_em": None}


def editar_nota(nota_id: int, texto: str, tenant_id: int = 1) -> dict | None:
    texto = (texto or "").strip()
    if not texto:
        raise RegistoInvalido("A nota está vazia.")
    if len(texto) > NOTA_MAX_CHARS:
        raise RegistoInvalido(f"A nota excede {NOTA_MAX_CHARS} caracteres.")
    agora = tempo.iso_utc()
    with db.ligacao() as conn:
        cur = conn.execute(
            "UPDATE service_notes SET texto = ?, atualizado_em = ? "
            "WHERE id = ? AND tenant_id = ?", (texto, agora, nota_id, tenant_id))
        if cur.rowcount == 0:
            return None
        row = conn.execute(
            "SELECT id, appointment_id, texto, criado_em, atualizado_em "
            "FROM service_notes WHERE id = ?", (nota_id,)).fetchone()
    return dict(zip(("id", "appointment_id", "texto", "criado_em", "atualizado_em"), row))


def apagar_nota(nota_id: int, tenant_id: int = 1) -> bool:
    with db.ligacao() as conn:
        cur = conn.execute("DELETE FROM service_notes WHERE id = ? AND tenant_id = ?",
                           (nota_id, tenant_id))
        return cur.rowcount > 0


# ---------------------------------------------------------------------------
# Fotos
# ---------------------------------------------------------------------------
def guardar_foto(appointment_id: int, conteudo: bytes, mime: str, tipo: str,
                 origem: str = "painel", tenant_id: int = 1) -> dict:
    if tipo not in TIPOS:
        raise RegistoInvalido("O tipo tem de ser 'antes' ou 'depois'.")
    nome, nome_thumb = _gravar_imagem(conteudo, mime)
    agora = tempo.iso_utc()
    with db.ligacao() as conn:
        ordem = conn.execute(
            "SELECT COALESCE(MAX(ordem), -1) + 1 FROM service_photos "
            "WHERE appointment_id = ? AND tipo = ?", (appointment_id, tipo)).fetchone()[0]
        cur = conn.execute(
            "INSERT INTO service_photos (tenant_id, appointment_id, tipo, ficheiro, "
            "thumb, mime, ordem, origem, criado_em) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (tenant_id, appointment_id, tipo, nome, nome_thumb, mime, ordem, origem, agora))
        foto_id = cur.lastrowid
    return {"id": foto_id, "appointment_id": appointment_id, "tipo": tipo,
            "ficheiro": nome, "thumb": nome_thumb, "mime": mime,
            "ordem": ordem, "origem": origem, "criado_em": agora}


def apagar_foto(foto_id: int, tenant_id: int = 1) -> bool:
    with db.ligacao() as conn:
        row = conn.execute(
            "SELECT ficheiro, thumb FROM service_photos WHERE id = ? AND tenant_id = ?",
            (foto_id, tenant_id)).fetchone()
        if not row:
            return False
        conn.execute("DELETE FROM service_photos WHERE id = ?", (foto_id,))
    _apagar_ficheiros(*row)
    return True


# ---------------------------------------------------------------------------
# Leitura
# ---------------------------------------------------------------------------
_CAMPOS_FOTO = ("id", "appointment_id", "tipo", "ficheiro", "thumb", "mime",
                "ordem", "origem", "criado_em")
_CAMPOS_NOTA = ("id", "appointment_id", "texto", "criado_em", "atualizado_em")


def registos_da_marcacao(appointment_id: int, tenant_id: int = 1) -> dict:
    """Tudo o que o drawer da marcação mostra: notas + fotos por tipo."""
    return registos_por_marcacoes([appointment_id], tenant_id).get(
        appointment_id, {"notas": [], "fotos": {"antes": [], "depois": []}})


def registos_por_marcacoes(ids: list[int], tenant_id: int = 1) -> dict[int, dict]:
    """Versão em lote para a ficha do cliente — uma query, não N."""
    if not ids:
        return {}
    marcadores = ",".join("?" * len(ids))
    resultado: dict[int, dict] = {
        i: {"notas": [], "fotos": {"antes": [], "depois": []}} for i in ids}
    with db.ligacao() as conn:
        for row in conn.execute(
                f"SELECT {', '.join(_CAMPOS_NOTA)} FROM service_notes "
                f"WHERE appointment_id IN ({marcadores}) AND tenant_id = ? "
                "ORDER BY criado_em, id", (*ids, tenant_id)):
            nota = dict(zip(_CAMPOS_NOTA, row))
            resultado[nota["appointment_id"]]["notas"].append(nota)
        for row in conn.execute(
                f"SELECT {', '.join(_CAMPOS_FOTO)} FROM service_photos "
                f"WHERE appointment_id IN ({marcadores}) AND tenant_id = ? "
                "ORDER BY tipo, ordem, id", (*ids, tenant_id)):
            foto = dict(zip(_CAMPOS_FOTO, row))
            resultado[foto["appointment_id"]]["fotos"][foto["tipo"]].append(foto)
    return resultado
