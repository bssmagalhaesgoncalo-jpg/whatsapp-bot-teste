"""
Interpretação de datas / horas / durações guardadas como TEXTO de apresentação.

Historicamente a base de dados guardou os valores tal como apareciam no
WhatsApp ("02.09.2026 (qua)", "🕝 14:30", "1h30", "aproximadamente 1h",
"1 dia"). Estas funções — puras e testáveis — são o único sítio onde esse
texto legado é convertido para valores estruturados.

As marcações NOVAS já gravam colunas estruturadas (`data_iso`, `hora_hhmm`,
`duracao_min`); estas funções continuam a servir os registos antigos e a
retrocompatibilidade do calendário.
"""

from __future__ import annotations

import re
from datetime import date

# Duração assumida para um serviço marcado como "1 dia" (grelha 08:00-19:00).
DURACAO_DIA_INTEIRO_MIN = (19 - 8) * 60


def data_iso_de_texto(texto):
    """"02.09.2026 (qua)" -> "2026-09-02". None se não houver data válida."""
    achado = re.search(r"(\d{1,2})[./-](\d{1,2})[./-](\d{4})", str(texto or ""))
    if not achado:
        return None
    dia, mes, ano = (int(x) for x in achado.groups())
    try:
        return date(ano, mes, dia).isoformat()
    except ValueError:
        return None


def hora_hhmm_de_texto(texto):
    """"🕝 14:30" -> "14:30". Ignora emojis e texto à volta. None se inválida."""
    achado = re.search(r"(\d{1,2})[:hH](\d{2})", str(texto or ""))
    if not achado:
        return None
    horas, minutos = int(achado.group(1)), int(achado.group(2))
    if not (0 <= horas <= 23 and 0 <= minutos <= 59):
        return None
    return f"{horas:02d}:{minutos:02d}"


def duracao_para_minutos(texto):
    """Duração guardada -> (minutos, dia_inteiro).

    Aceita "45min", "1h", "1h30", "2h", "aproximadamente 1h", "1 dia",
    "1 Tag"/"1 day". Devolve (None, False) quando nada é interpretável."""
    bruto = str(texto or "").strip().lower()
    if not bruto:
        return None, False
    if re.search(r"\d+\s*(dia|dias|tag|tage|day|days)\b", bruto):
        return DURACAO_DIA_INTEIRO_MIN, True

    achado = re.search(r"(\d+)\s*[hH](?:\s*(\d{1,2}))?", bruto)
    if achado:
        minutos = int(achado.group(1)) * 60 + int(achado.group(2) or 0)
        return (minutos, False) if minutos > 0 else (None, False)

    achado = re.search(r"(\d+)\s*(min|minuto|minutos|minuten)\b", bruto)
    if achado:
        minutos = int(achado.group(1))
        return (minutos, False) if minutos > 0 else (None, False)
    return None, False


def minutos_de_duracao_texto(texto):
    """Só os minutos (int) ou None — atalho para quando o "dia inteiro" não
    interessa."""
    minutos, _ = duracao_para_minutos(texto)
    return minutos
