"""
core/health.py — saúde do sistema: o que falha sem ninguém dar por isso.

Duas coisas, a mesma tabela (`system_health`, migração 23):

  • AVARIAS  — o bot ficou mudo e ninguém sabe. O caso que motivou isto: a
    Meta devolve 401 (token expirado) e o código escrevia um aviso no log,
    marcava a mensagem da cliente como processada e seguia. A cliente não
    recebe nada, o link continua na bio do Instagram e ninguém repara.

  • PULSOS   — a última vez que algo que devia correr sozinho correu de
    facto (hoje: o cron das automações). Silêncio prolongado é avaria.

Regra de ouro: o registo no PAINEL acontece SEMPRE e primeiro; o aviso por
WhatsApp é um extra que pode falhar — se o WhatsApp está em baixo, um aviso
por WhatsApp sobre o WhatsApp estar em baixo também não chega a lado nenhum.

Anti-inundação: `avisado_em` faz o papel do `dedupe_key` dos eventos — uma
avaria só volta a gerar aviso passada a janela, mesmo que aconteça a cada
mensagem que entra.
"""

from __future__ import annotations

import logging

import db
import tempo

log = logging.getLogger("health")

# Chaves das avarias. Uma por TIPO de problema, não por ocorrência: se o
# token expirou, todas as mensagens falham — é uma avaria, não duzentas.
CHAVE_WHATSAPP_CONTA = "whatsapp.conta"       # token/permissões/número suspenso
CHAVE_WHATSAPP_LIMITE = "whatsapp.limite"     # limite de envio atingido
CHAVE_AUTOMACOES = "automacoes.pulso"

# Janela entre dois avisos da MESMA avaria. Uma hora: tempo suficiente para
# não inundar o telemóvel dela, curto o bastante para ela perceber que o
# problema continua de pé se não fez nada.
JANELA_AVISO_MIN = 60

# O cron corre de 5 em 5 minutos. Trinta é folga para um deploy, um restart
# ou o serviço a acordar do sleep do Render sem gritar por nada.
SILENCIO_AUTOMACOES_MIN = 30


def registar_avaria(chave: str, titulo: str, detalhe: str = "",
                    tenant_id: int = 1, janela_min: int = JANELA_AVISO_MIN) -> bool:
    """Abre (ou reforça) uma avaria e diz se é altura de AVISAR.

    Devolve True só quando ainda não houve aviso ou já passou a janela — é
    quem chama que decide o que fazer com isso (mandar WhatsApp, por
    exemplo). O registo em si é incondicional: o painel mostra sempre.
    """
    agora = tempo.iso_utc()
    limite = tempo.iso_utc(tempo.agora_utc() - _minutos(janela_min))
    with db.ligacao() as c:
        linha = c.execute(
            "SELECT id, ocorrencias, avisado_em, resolvido_em FROM system_health "
            "WHERE tenant_id = ? AND chave = ?", (tenant_id, chave)).fetchone()
        if linha is None:
            c.execute(
                "INSERT INTO system_health (tenant_id, chave, tipo, titulo, detalhe, "
                "ocorrencias, primeiro_em, ultimo_em) VALUES (?, ?, 'avaria', ?, ?, 1, ?, ?)",
                (tenant_id, chave, titulo, detalhe, agora, agora))
            deve_avisar = True
        else:
            (hid, ocorrencias, avisado_em, resolvido_em) = linha
            # Uma avaria que já tinha sido resolvida volta a contar do zero —
            # se voltou a partir depois de arranjada, ela tem de saber.
            reaberta = resolvido_em is not None
            deve_avisar = reaberta or not avisado_em or avisado_em < limite
            c.execute(
                "UPDATE system_health SET titulo = ?, detalhe = ?, ocorrencias = ?, "
                "ultimo_em = ?, resolvido_em = NULL, primeiro_em = COALESCE(?, primeiro_em) "
                "WHERE id = ?",
                (titulo, detalhe, (0 if reaberta else ocorrencias) + 1, agora,
                 agora if reaberta else None, hid))
        if deve_avisar:
            # Marca-se o aviso ANTES de o tentar enviar: se o envio rebentar,
            # é porque o canal está em baixo — repetir de imediato só somaria
            # tentativas inúteis, e o painel já tem o registo.
            c.execute("UPDATE system_health SET avisado_em = ? WHERE tenant_id = ? AND chave = ?",
                      (agora, tenant_id, chave))
    return deve_avisar


def resolver(chave: str, tenant_id: int = 1) -> None:
    """Fecha uma avaria. Chamado quando a mesma operação volta a correr bem
    — o cartão desaparece do painel sozinho, sem ela ter de o dispensar."""
    with db.ligacao() as c:
        c.execute(
            "UPDATE system_health SET resolvido_em = ?, avisado_em = NULL "
            "WHERE tenant_id = ? AND chave = ? AND resolvido_em IS NULL",
            (tempo.iso_utc(), tenant_id, chave))


def avarias_abertas(tenant_id: int = 1) -> list[dict]:
    with db.ligacao() as c:
        linhas = c.execute(
            "SELECT chave, titulo, detalhe, ocorrencias, primeiro_em, ultimo_em "
            "FROM system_health WHERE tenant_id = ? AND tipo = 'avaria' "
            "AND resolvido_em IS NULL ORDER BY ultimo_em DESC", (tenant_id,)).fetchall()
    campos = ("chave", "titulo", "detalhe", "ocorrencias", "primeiro_em", "ultimo_em")
    return [dict(zip(campos, l)) for l in linhas]


def pulso(chave: str, tenant_id: int = 1) -> None:
    """Regista que algo periódico acabou de correr com SUCESSO."""
    agora = tempo.iso_utc()
    with db.ligacao() as c:
        c.execute(
            "INSERT INTO system_health (tenant_id, chave, tipo, ultimo_em, primeiro_em) "
            "VALUES (?, ?, 'pulso', ?, ?) ON CONFLICT (tenant_id, chave) "
            "DO UPDATE SET ultimo_em = excluded.ultimo_em, resolvido_em = NULL",
            (tenant_id, chave, agora, agora))


def ultimo_pulso(chave: str, tenant_id: int = 1) -> str | None:
    with db.ligacao() as c:
        linha = c.execute(
            "SELECT ultimo_em FROM system_health WHERE tenant_id = ? AND chave = ? "
            "AND tipo = 'pulso'", (tenant_id, chave)).fetchone()
    return linha[0] if linha else None


def minutos_sem_pulso(chave: str, tenant_id: int = 1, agora=None) -> int | None:
    """Minutos desde o último sucesso. None quando nunca correu — e isso não
    é avaria: numa instalação nova ainda ninguém chamou o cron."""
    ultimo = ultimo_pulso(chave, tenant_id)
    if not ultimo:
        return None
    momento = tempo.parse_iso(ultimo)
    if momento is None:
        return None
    return int(((agora or tempo.agora_utc()) - momento).total_seconds() // 60)


def _minutos(n: int):
    from datetime import timedelta
    return timedelta(minutes=n)
