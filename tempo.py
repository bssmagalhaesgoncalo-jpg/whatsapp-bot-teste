"""
Tempo — fonte única de "agora" e de fuso horário do projeto.

Todo o negócio (Daniela Beauty) vive em Zurique. O servidor pode correr em
qualquer fuso (Render usa UTC), por isso NADA no código deve chamar
`datetime.now()` ou `datetime.utcnow()` diretamente: usa-se sempre este
módulo. Assim uma marcação às "14:30" é sempre 14:30 hora da Suíça, e a
mudança de hora (DST) é tratada pela biblioteca padrão `zoneinfo`.

Regras:
  • `agora_zurique()`  -> datetime AWARE no fuso Europe/Zurich (para mostrar
    ao cliente / calcular dias / comparar com horas locais de marcação).
  • `agora_utc()`      -> datetime AWARE em UTC (para GRAVAR carimbos de tempo
    e prazos de expiração: instantes absolutos, sem ambiguidade de DST).
  • `iso_utc()`        -> string ISO-8601 em UTC, com sufixo +00:00, para as
    colunas de texto da base de dados.
  • `parse_iso(s)`     -> datetime aware a partir de uma string gravada.
    Aceita valores LEGADOS gravados sem fuso (assume-se que eram UTC, que é
    o que o código antigo usava via `datetime.utcnow()`).
"""

from __future__ import annotations

from datetime import date, datetime, timezone, timedelta

try:
    from zoneinfo import ZoneInfo
    FUSO_ZURIQUE = ZoneInfo("Europe/Zurich")
except Exception:  # pragma: no cover - zoneinfo é padrão no 3.9+, fallback defensivo
    FUSO_ZURIQUE = timezone(timedelta(hours=1))  # CET aproximado, sem DST

FUSO_UTC = timezone.utc

NOME_FUSO = "Europe/Zurich"


def agora_zurique() -> datetime:
    """Instante atual, AWARE, no fuso do negócio."""
    return datetime.now(FUSO_ZURIQUE)


def agora_utc() -> datetime:
    """Instante atual, AWARE, em UTC — para gravar e para prazos."""
    return datetime.now(FUSO_UTC)


def hoje_zurique() -> date:
    """Data de hoje segundo o relógio de Zurique (não o do servidor)."""
    return agora_zurique().date()


def iso_utc(momento: datetime | None = None) -> str:
    """String ISO-8601 em UTC (sufixo +00:00). Sem argumento, usa agora."""
    momento = momento or agora_utc()
    if momento.tzinfo is None:
        momento = momento.replace(tzinfo=FUSO_UTC)
    return momento.astimezone(FUSO_UTC).isoformat()


def parse_iso(valor: str | None) -> datetime | None:
    """datetime AWARE a partir de uma string gravada.

    Strings novas trazem fuso (+00:00). Strings LEGADAS (código antigo que
    usava datetime.utcnow().isoformat(), sem fuso) são interpretadas como
    UTC — que era exatamente o que significavam."""
    if not valor:
        return None
    try:
        dt = datetime.fromisoformat(str(valor))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=FUSO_UTC)
    return dt


def combinar_local(data_iso: str, hora_hhmm: str) -> datetime | None:
    """"2026-09-02" + "14:30" -> datetime AWARE no fuso de Zurique.

    É a hora de PAREDE do salão. Usa-se para comparar uma marcação com
    "agora" (p.ex. saber se já passou) sem enganos de fuso."""
    try:
        ingenuo = datetime.fromisoformat(f"{data_iso}T{hora_hhmm}:00")
    except ValueError:
        return None
    return ingenuo.replace(tzinfo=FUSO_ZURIQUE)
