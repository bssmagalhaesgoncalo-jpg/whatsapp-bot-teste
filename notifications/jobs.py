"""
notifications/jobs.py — EXECUTOR ÚNICO e mínimo de jobs adiados (P0).

Não existia nenhum runner/scheduler no projeto (ver render.yaml: um único
processo gunicorn, sem worker nem cron) — isto é o mínimo necessário para
"criar agora, executar daqui a X minutos", sem Celery/Redis/RabbitMQ: uma
tabela (`automation_jobs`, migração 18) + duas funções.

    enqueue_job(...)       -> grava um job "pending" para `run_at` (idempotente
                              por `idempotency_key`: o mesmo ciclo nunca gera
                              dois jobs).
    process_due_jobs(...)  -> chamado periodicamente (painel autenticado hoje,
                              Render Cron ou equivalente amanhã) — processa
                              todos os jobs "pending" com `run_at` já passado.

O TIPO de job (o que fazer quando chega a vez) é um HANDLER registado por
`registar_handler(tipo, fn)` — o mesmo padrão já usado em core/events.py,
para não inventar um segundo mecanismo de "plugar" lógica. "post_service"
(P0) e "reminder_24h" (P1, ver notifications/reminders.py) já têm handler;
a tabela e o executor continuam prontos para mais tipos no futuro (ex.:
"rebooking_followup") sem mudar nada aqui — só registar mais um handler.

Um handler que rebente NÃO perde o job: fica "pending" outra vez (retry no
próximo process_due_jobs), com `attempts`/`last_error` atualizados. Ao fim de
MAX_TENTATIVAS falhas se torna "failed" (deixa de ser retentado sozinho —
precisa de intervenção)."""

from __future__ import annotations

import json
import logging

import db
import tempo

log = logging.getLogger("notif.jobs")

TYPE_POST_SERVICE = "post_service"
TYPE_REMINDER_24H = "reminder_24h"
TYPE_REBOOKING_FOLLOWUP = "rebooking_followup"

PENDING = "pending"
PROCESSING = "processing"
DONE = "done"
FAILED = "failed"
CANCELLED = "cancelled"

MAX_TENTATIVAS = 5

_HANDLERS: dict[str, callable] = {}


def registar_handler(tipo: str, handler) -> None:
    _HANDLERS[tipo] = handler


_CAMPOS = ("id", "tenant_id", "type", "booking_id", "customer_id", "payload",
           "run_at", "status", "attempts", "last_error", "idempotency_key",
           "created_at", "processed_at")
_SQL_CAMPOS = ", ".join(_CAMPOS)


def _linha(row) -> dict:
    d = dict(zip(_CAMPOS, row))
    try:
        d["payload"] = json.loads(d["payload"] or "{}")
    except (ValueError, TypeError):
        d["payload"] = {}
    return d


def enqueue_job(tipo: str, run_at: str, *, tenant_id: int = 1, booking_id: int | None = None,
                customer_id: int | None = None, payload: dict | None = None,
                idempotency_key: str | None = None) -> dict:
    """Cria o job, ou devolve o já existente do mesmo ciclo (mesma
    `idempotency_key`) — "correr o handler outra vez não duplica o job"."""
    with db.ligacao() as c:
        if idempotency_key:
            existente = c.execute(
                f"SELECT {_SQL_CAMPOS} FROM automation_jobs WHERE tenant_id = ? AND idempotency_key = ?",
                (tenant_id, idempotency_key)).fetchone()
            if existente:
                return _linha(existente)
        agora = tempo.iso_utc()
        cur = c.execute(
            "INSERT INTO automation_jobs (tenant_id, type, booking_id, customer_id, payload, "
            "run_at, status, attempts, idempotency_key, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 'pending', 0, ?, ?)",
            (tenant_id, tipo, booking_id, customer_id,
             json.dumps(payload or {}, ensure_ascii=False), run_at, idempotency_key, agora))
        row = c.execute(f"SELECT {_SQL_CAMPOS} FROM automation_jobs WHERE id = ?",
                        (cur.lastrowid,)).fetchone()
        return _linha(row)


def obter_job(job_id: int, tenant_id: int = 1) -> dict | None:
    """Lookup por id — usado quando um botão do cliente (ex.: o reminder 24h)
    traz o id do job embutido e precisa de o revalidar antes de agir."""
    with db.ligacao() as c:
        row = c.execute(
            f"SELECT {_SQL_CAMPOS} FROM automation_jobs WHERE id = ? AND tenant_id = ?",
            (job_id, tenant_id)).fetchone()
    return _linha(row) if row else None


def reprogramar_ou_criar(tipo: str, run_at: str, *, tenant_id: int = 1, booking_id: int | None = None,
                         customer_id: int | None = None, payload: dict | None = None,
                         idempotency_key: str) -> dict:
    """Como `enqueue_job`, mas para jobs cujo `run_at` pode mudar depois de
    criados — o reminder 24h é recalculado a cada reagendamento (ver
    notifications/reminders.py). Ao contrário de `enqueue_job` (que devolve o
    job já existente tal e qual), aqui uma `idempotency_key` já usada é
    ATUALIZADA no próprio lugar: nunca há duas linhas para a mesma marcação,
    mesmo que a ocorrência anterior já tenha corrido (done/cancelled/failed)
    — esse registo é reaproveitado como a nova ocorrência, com
    attempts/last_error/processed_at repostos."""
    with db.ligacao() as c:
        existente = c.execute(
            "SELECT id FROM automation_jobs WHERE tenant_id = ? AND idempotency_key = ?",
            (tenant_id, idempotency_key)).fetchone()
        payload_json = json.dumps(payload or {}, ensure_ascii=False)
        if existente:
            c.execute(
                "UPDATE automation_jobs SET type = ?, booking_id = ?, customer_id = ?, payload = ?, "
                "run_at = ?, status = 'pending', attempts = 0, last_error = NULL, processed_at = NULL "
                "WHERE id = ?",
                (tipo, booking_id, customer_id, payload_json, run_at, existente[0]))
            job_id = existente[0]
        else:
            agora = tempo.iso_utc()
            cur = c.execute(
                "INSERT INTO automation_jobs (tenant_id, type, booking_id, customer_id, payload, "
                "run_at, status, attempts, idempotency_key, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, 'pending', 0, ?, ?)",
                (tenant_id, tipo, booking_id, customer_id, payload_json, run_at, idempotency_key, agora))
            job_id = cur.lastrowid
        row = c.execute(f"SELECT {_SQL_CAMPOS} FROM automation_jobs WHERE id = ?", (job_id,)).fetchone()
        return _linha(row)


def cancelar_job(idempotency_key: str, tenant_id: int = 1, motivo: str | None = None) -> None:
    """Usado na revalidação: se a marcação deixou de estar 'completed' antes
    do job correr, não faz sentido continuar a tentar — cancela-se de forma
    explícita em vez de deixar "failed" (que sugeriria um erro a corrigir)."""
    with db.ligacao() as c:
        c.execute(
            "UPDATE automation_jobs SET status = 'cancelled', last_error = ?, processed_at = ? "
            "WHERE tenant_id = ? AND idempotency_key = ? AND status IN ('pending', 'processing')",
            (motivo, tempo.iso_utc(), tenant_id, idempotency_key))


def process_due_jobs(tenant_id: int = 1, limite: int = 50, agora: str | None = None) -> dict:
    """Processa os jobs "pending" já devidos. Cada job é "reclamado"
    (status='processing', attempts+1) dentro da MESMA ligação antes de correr
    o handler — evita duas passagens simultâneas mandarem a mesma mensagem
    duas vezes. Devolve um resumo (nunca levanta — um job que falhe fica
    registado, não derruba os outros)."""
    agora = agora or tempo.iso_utc()
    resumo = {"processados": 0, "concluidos": 0, "falharam": 0, "cancelados": 0}
    with db.ligacao() as c:
        pendentes = c.execute(
            f"SELECT {_SQL_CAMPOS} FROM automation_jobs WHERE tenant_id = ? AND status = 'pending' "
            "AND run_at <= ? ORDER BY run_at ASC LIMIT ?", (tenant_id, agora, limite)).fetchall()
        jobs = [_linha(r) for r in pendentes]
        for job in jobs:
            c.execute("UPDATE automation_jobs SET status = 'processing', attempts = attempts + 1 "
                      "WHERE id = ?", (job["id"],))

    for job in jobs:
        job["attempts"] += 1
        resumo["processados"] += 1
        handler = _HANDLERS.get(job["type"])
        if not handler:
            log.warning("job #%s tipo '%s' sem handler registado", job["id"], job["type"])
            _marcar(job["id"], FAILED, "sem handler registado para este tipo")
            resumo["falharam"] += 1
            continue
        try:
            resultado = handler(job)
        except Exception as e:                     # noqa: BLE001 — isolar cada job
            log.exception("job #%s (%s) falhou", job["id"], job["type"])
            if job["attempts"] >= MAX_TENTATIVAS:
                _marcar(job["id"], FAILED, str(e)[:500])
            else:
                _marcar(job["id"], PENDING, str(e)[:500])
            resumo["falharam"] += 1
            continue
        if resultado == CANCELLED:
            _marcar(job["id"], CANCELLED, "revalidação: já não elegível")
            resumo["cancelados"] += 1
        else:
            _marcar(job["id"], DONE, None)
            resumo["concluidos"] += 1
    return resumo


def _marcar(job_id: int, status: str, erro: str | None) -> None:
    with db.ligacao() as c:
        processado = tempo.iso_utc() if status in (DONE, FAILED, CANCELLED) else None
        c.execute("UPDATE automation_jobs SET status = ?, last_error = ?, processed_at = ? WHERE id = ?",
                  (status, erro, processado, job_id))
