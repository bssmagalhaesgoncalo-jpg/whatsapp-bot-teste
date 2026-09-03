"""
Estados de uma marcação — vocabulário canónico ÚNICO.

    pending    aguarda aprovação da equipa (só quando BOOKING_REQUIRES_APPROVAL)
    confirmed  marcada e ativa
    cancelled  cancelada (o horário pode ou não voltar ao mercado — ver
               a coluna bloqueia_horario)
    completed  realizada
    no_show    o cliente não compareceu

"reagendado" NÃO é um estado: um reagendamento altera a MESMA marcação
(data/hora) e regista uma linha em agendamento_historico. A marcação continua
`confirmed`.

O código antigo usava português ("confirmado", "cancelado", "concluído",
"reagendado"). `normalizar()` aceita ambos + acentos e devolve sempre o valor
canónico, para nenhuma leitura antiga partir.
"""

from __future__ import annotations

import unicodedata

PENDING = "pending"
CONFIRMED = "confirmed"
CANCELLED = "cancelled"
COMPLETED = "completed"
NO_SHOW = "no_show"

TODOS = (PENDING, CONFIRMED, CANCELLED, COMPLETED, NO_SHOW)

# Um horário fica OCUPADO quando a marcação está num destes estados
# (independentemente da coluna bloqueia_horario). Uma `cancelled` respeita a
# coluna; `no_show` (sempre no passado) não ocupa agenda futura.
BLOQUEIAM_SEMPRE = (CONFIRMED, COMPLETED, PENDING)

# Estados "ativos" para o cliente gerir/ver a sua marcação.
GERIVEIS_PELO_CLIENTE = (CONFIRMED, PENDING)

_LEGADO = {
    "confirmado": CONFIRMED,
    "confirmada": CONFIRMED,
    "pendente": PENDING,
    "cancelado": CANCELLED,
    "cancelada": CANCELLED,
    "concluido": COMPLETED,
    "concluida": COMPLETED,
    "reagendado": CANCELLED,   # a marcação antiga que foi substituída
    "reagendada": CANCELLED,
    "nao compareceu": NO_SHOW,
    "faltou": NO_SHOW,
}

# Rótulos para o painel (idioma de trabalho da equipa = português).
ROTULO_PT = {
    PENDING: "A aprovar",
    CONFIRMED: "Confirmada",
    CANCELLED: "Cancelada",
    COMPLETED: "Concluída",
    NO_SHOW: "Não compareceu",
}


def _sem_acento(texto: str) -> str:
    d = unicodedata.normalize("NFD", texto)
    return "".join(c for c in d if not unicodedata.combining(c))


def normalizar(estado) -> str:
    """Qualquer forma (EN, PT, com acentos, maiúsculas) -> valor canónico.
    Um valor desconhecido é devolvido em minúsculas, sem rebentar."""
    bruto = _sem_acento(str(estado or "")).strip().lower().replace("-", "_")
    if bruto in TODOS:
        return bruto
    return _LEGADO.get(bruto, bruto or CONFIRMED)


def bloqueia_horario(estado, coluna_bloqueia_horario) -> bool:
    """Regra ÚNICA de ocupação de um horário por uma marcação."""
    e = normalizar(estado)
    if e in BLOQUEIAM_SEMPRE:
        return True
    if e == CANCELLED:
        return int(coluna_bloqueia_horario or 0) == 1
    return False
