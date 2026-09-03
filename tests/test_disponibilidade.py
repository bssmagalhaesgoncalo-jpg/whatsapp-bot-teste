"""Testes 4-7, 11, 12, 15, 16: conflitos, adjacência, reservas temporárias."""

import pytest

import bot
import db
from conftest import marcar, data_pt

DIA = "2026-09-07"          # segunda-feira
DIA_TXT = data_pt(DIA)


def _livre(hhmm, servico_id, ignora=None, telefone="tester"):
    """True se `hhmm` está livre para um serviço, na DIA."""
    servico = db.obter_servico(servico_id)
    ocup = bot.ocupacoes()
    return not bot.conflitos_no_intervalo(
        ocup, DIA, hhmm, bot.catalogo.nome_pt(servico),
        bot.catalogo.duracao_label(servico["duracao_min"]), ignorar_id=ignora)


def test_4_conflito_parcial_inicio(base_dados):
    # Marcação 60min às 10:00; novo serviço 60min às 09:30 -> sobrepõe o início.
    marcar("41790000001", "limpeza_pele", DIA_TXT, "🕘 10:00")
    assert _livre("09:00", "limpeza_pele") is True      # 09:00-10:00 encosta, não sobrepõe
    assert _livre("09:30", "limpeza_pele") is False     # 09:30-10:30 sobrepõe 10:00-11:00


def test_5_conflito_parcial_fim(base_dados):
    marcar("41790000002", "limpeza_pele", DIA_TXT, "🕘 10:00")   # 10:00-11:00
    assert _livre("10:30", "limpeza_pele") is False     # 10:30-11:30 sobrepõe o fim


def test_6_conflito_contido(base_dados):
    marcar("41790000003", "pestanas", DIA_TXT, "🕘 09:00")       # 09:00-11:00 (120min)
    assert _livre("09:30", "design_sobrancelhas") is False       # 09:30-10:00 contido dentro


def test_7_slot_exatamente_adjacente_permitido(base_dados):
    marcar("41790000004", "limpeza_pele", DIA_TXT, "🕘 09:00")   # 09:00-10:00
    # 10:00-11:00 começa exatamente quando o outro acaba -> permitido
    assert _livre("10:00", "limpeza_pele") is True


def test_11_cancelar_liberta_horario(base_dados):
    idag = marcar("41790000005", "limpeza_pele", DIA_TXT, "🕘 14:00")
    assert _livre("14:00", "limpeza_pele") is False
    bot.marcar_agendamento_cancelado(idag, libertar=True)
    assert _livre("14:00", "limpeza_pele") is True


def test_12_cancelada_que_nao_liberta_continua_a_bloquear(base_dados):
    idag = marcar("41790000006", "limpeza_pele", DIA_TXT, "🕘 15:00")
    bot.marcar_agendamento_cancelado(idag, libertar=False)
    ag = bot.obter_agendamento(idag)
    assert bot.chave_estado(ag["estado"]) == "cancelled"
    assert bot.agendamento_bloqueia_horario(ag) is True
    assert _livre("15:00", "limpeza_pele") is False


def test_15_reserva_temporaria_bloqueia(base_dados):
    sessao = {"servico_id": "limpeza_pele", "servico": "Limpeza de pele",
              "duracao": "1h", "duracao_min": 60, "data": DIA_TXT, "hora": "🕘 16:00"}
    bot.reter_horario("41790000007", sessao)
    # outro cliente não vê o 16:00
    assert _livre("16:00", "limpeza_pele", telefone="outro") is False
    # ... mas o próprio continua a poder confirmar (retenção dele é ignorada)
    ocup = bot.ocupacoes(excluir_telefone="41790000007")
    assert not bot.conflitos_no_intervalo(ocup, DIA, "16:00", "Limpeza de pele", "1h")


def test_16_reserva_temporaria_expirada_deixa_de_bloquear(base_dados, monkeypatch):
    import tempo
    from datetime import timedelta
    sessao = {"servico_id": "limpeza_pele", "servico": "Limpeza de pele",
              "duracao": "1h", "duracao_min": 60, "data": DIA_TXT, "hora": "🕘 17:00"}
    bot.reter_horario("41790000008", sessao)
    assert _livre("17:00", "limpeza_pele", telefone="outro") is False
    # Salta 20 minutos à frente (RESERVA_TEMPORARIA_MINUTOS = 15) -> expira.
    futuro = tempo.agora_utc() + timedelta(minutes=20)
    monkeypatch.setattr(tempo, "agora_utc", lambda: futuro)
    monkeypatch.setattr(tempo, "iso_utc", lambda m=None: (m or futuro).isoformat())
    assert _livre("17:00", "limpeza_pele", telefone="outro") is True


def test_disponibilidade_no_whatsapp_reflete_conflitos(base_dados):
    marcar("41790000009", "pestanas", DIA_TXT, "🕘 09:00")   # 09:00-11:00
    sessao = {"fluxo": "beauty", "servico_id": "limpeza_pele", "servico": "Limpeza de pele",
              "duracao": "1h", "duracao_min": 60, "data": DIA_TXT}
    livres = bot.horarios_livres_para_sessao(sessao, telefone="novo")
    assert "🕘 09:00" not in livres and "🕥 10:30" not in livres
    assert "🕐 13:00" in livres
