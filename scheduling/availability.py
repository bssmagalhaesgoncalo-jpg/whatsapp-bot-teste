"""
scheduling/availability.py — MOTOR DE DISPONIBILIDADE (BOOKING ENGINE).

`slots()` é a única fonte de verdade da disponibilidade apresentada ao
cliente. Compõe:

    horário de funcionamento (business_hours + exceções)
  + duração do serviço + buffers (antes/depois)
  + marcações ativas que bloqueiam o horário
  + reservas temporárias em curso
  + política (antecedência mínima/máxima, marcação no próprio dia, granularidade)

A prevenção de overlaps ao GRAVAR continua no `guardar_agendamento` /
`reagendar_agendamento` do bot (BEGIN IMMEDIATE) — esta camada é o "que
mostrar", aquela é o "pode-se mesmo escrever". As duas usam a mesma
semântica de sobreposição: [a, b) sobrepõe [c, d)  <=>  a < d and c < b.
"""

from __future__ import annotations

from datetime import date

import db
import tempo
from scheduling import business_hours as bh


def _min_para_hhmm(m: int) -> str:
    return f"{m // 60:02d}:{m % 60:02d}"


def _sobrepoe(a0, a1, b0, b1) -> bool:
    return a0 < b1 and b0 < a1


def slots(servico_id: str, data_iso: str, *, telefone: str | None = None,
          ignorar_id: int | None = None, tenant_id: int = 1,
          staff_id=None) -> list[str]:
    """Horas de início livres (["09:00", "09:15", ...]) para `servico_id` em
    `data_iso`. `telefone`: a retenção do próprio cliente é ignorada.
    `ignorar_id`: a marcação a ser reagendada não conta como conflito."""
    servico = db.obter_servico(servico_id)
    if not servico:
        return []
    dur = int(servico["duracao_min"] or 0)
    if dur <= 0:
        return []
    bb = int(servico.get("buffer_before_min") or 0)
    ba = int(servico.get("buffer_after_min") or 0)

    janelas = bh.janelas_do_dia(data_iso, tenant_id, staff_id)
    if not janelas:
        return []

    pol = bh.politica(tenant_id)
    passo = max(5, int(pol["slot_granularity_min"]))

    # limite inferior por antecedência mínima (só relevante se `data_iso` é hoje)
    agora = tempo.agora_zurique()
    min_inicio = 0
    if date.fromisoformat(data_iso) == agora.date():
        min_inicio = agora.hour * 60 + agora.minute + int(pol["min_notice_min"])

    ocup = db.ocupacao_do_dia(data_iso, tenant_id)
    # intervalos ocupados (com o buffer do serviço já marcado), em minutos
    bloqueados = []
    for it in ocup:
        if ignorar_id is not None and it.get("id") == ignorar_id:
            continue
        if telefone is not None and it.get("retencao") and it.get("telefone") == telefone:
            continue
        i0 = it["inicio_min"] - int(it.get("buffer_before") or 0)
        i1 = it["inicio_min"] + it["dur_min"] + int(it.get("buffer_after") or 0)
        bloqueados.append((i0, i1))

    livres = []
    for (jo, jc) in janelas:
        t = jo
        # alinhar à granularidade a partir da abertura
        while t + dur <= jc:
            if t >= min_inicio:
                cand0 = t - bb
                cand1 = t + dur + ba
                if not any(_sobrepoe(cand0, cand1, b0, b1) for (b0, b1) in bloqueados):
                    livres.append(_min_para_hhmm(t))
            t += passo
    return livres


def primeiro_slot(servico_id: str, tenant_id: int = 1, dias: int = 14) -> tuple[str, str] | None:
    """(data_iso, hora) do próximo horário livre para o serviço, nos próximos
    `dias` dias abertos. Útil para 'ver horários' do rebooking."""
    for d in bh.proximos_dias_abertos(dias, tenant_id):
        livres = slots(servico_id, d, tenant_id=tenant_id)
        if livres:
            return d, livres[0]
    return None
