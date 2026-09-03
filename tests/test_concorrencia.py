"""Testes 8, 9, 10: confirmações e reagendamentos concorrentes; reagendamento
falhado mantém a marcação original."""

import threading

import pytest

import bot
import db
from conftest import marcar, data_pt

DIA = "2026-09-08"
DIA_TXT = data_pt(DIA)
DIA2 = "2026-09-09"
DIA2_TXT = data_pt(DIA2)


def _sessao(servico_id, data_txt, hora_txt, telefone):
    s = db.obter_servico(servico_id)
    return {
        "idioma": "pt", "nome": "C " + telefone[-3:], "servico_id": servico_id,
        "servico": bot.catalogo.nome_pt(s), "duracao": bot.catalogo.duracao_label(s["duracao_min"]),
        "duracao_min": s["duracao_min"], "preco_cents": s["preco_cents"],
        "preco": None if s["preco_cents"] is None else s["preco_cents"] / 100,
        "data": data_txt, "hora": hora_txt,
    }


def test_8_duas_confirmacoes_concorrentes_so_uma_vence(base_dados):
    barreira = threading.Barrier(2)
    resultados = {}

    def confirmar(nome, telefone):
        barreira.wait()
        try:
            resultados[nome] = ("ok", marcar(telefone, "limpeza_pele", DIA_TXT, "🕘 09:00"))
        except bot.HorarioOcupado:
            resultados[nome] = ("ocupado", None)
        except Exception as e:                       # pragma: no cover
            resultados[nome] = ("erro", repr(e))

    t1 = threading.Thread(target=confirmar, args=("a", "41790000101"))
    t2 = threading.Thread(target=confirmar, args=("b", "41790000102"))
    t1.start(); t2.start(); t1.join(); t2.join()

    estados_res = sorted(v[0] for v in resultados.values())
    assert estados_res == ["ocupado", "ok"], resultados
    # e SÓ uma marcação ficou gravada nesse horário
    ativos = [a for a in bot.listar_agendamentos()
              if a["data_iso"] == DIA and a["hora_hhmm"] == "09:00"
              and bot.agendamento_bloqueia_horario(a)]
    assert len(ativos) == 1


def test_9_dois_reagendamentos_concorrentes_so_um_vence(base_dados):
    a = marcar("41790000111", "limpeza_pele", DIA_TXT, "🕘 09:00")
    b = marcar("41790000112", "limpeza_pele", DIA_TXT, "🕝 14:30")
    # ambos tentam ir para o MESMO horário novo (DIA2 11:00) ao mesmo tempo
    barreira = threading.Barrier(2)
    resultados = {}

    def mover(nome, idag):
        barreira.wait()
        try:
            bot.reagendar_agendamento(idag, DIA2, "11:00", origem="teste", avisar_cliente=False)
            resultados[nome] = "ok"
        except bot.HorarioOcupado:
            resultados[nome] = "ocupado"
        except Exception as e:                       # pragma: no cover
            resultados[nome] = "erro:" + repr(e)

    t1 = threading.Thread(target=mover, args=("a", a))
    t2 = threading.Thread(target=mover, args=("b", b))
    t1.start(); t2.start(); t1.join(); t2.join()

    assert sorted(resultados.values()) == ["ocupado", "ok"], resultados
    no_slot = [x for x in bot.listar_agendamentos()
               if x["data_iso"] == DIA2 and x["hora_hhmm"] == "11:00"]
    assert len(no_slot) == 1


def test_10_reagendamento_falhado_mantem_marcacao_original(base_dados):
    a = marcar("41790000121", "limpeza_pele", DIA_TXT, "🕘 09:00")
    # ocupa o destino
    marcar("41790000122", "limpeza_pele", DIA2_TXT, "🕘 09:00")
    antes = bot.obter_agendamento(a)
    with pytest.raises(bot.HorarioOcupado):
        bot.reagendar_agendamento(a, DIA2, "09:00", origem="teste", avisar_cliente=False)
    depois = bot.obter_agendamento(a)
    assert depois["data_iso"] == antes["data_iso"] == DIA
    assert depois["hora_hhmm"] == antes["hora_hhmm"] == "09:00"
    assert bot.chave_estado(depois["estado"]) == "confirmed"
    assert not bot.historico_agendamento(a)          # nada foi registado


def test_10b_reagendamento_bem_sucedido_e_a_mesma_marcacao(base_dados):
    a = marcar("41790000131", "pestanas", DIA_TXT, "🕘 09:00")
    ag2, _ = bot.reagendar_agendamento(a, DIA2, "13:00", origem="teste", avisar_cliente=False)
    assert ag2["id"] == a                            # MESMO registo
    assert ag2["data_iso"] == DIA2 and ag2["hora_hhmm"] == "13:00"
    assert bot.chave_estado(ag2["estado"]) == "confirmed"
    hist = bot.historico_agendamento(a)
    assert len(hist) == 1 and hist[0]["data_nova"].startswith("09.09.2026")
    # o horário antigo ficou livre
    s = db.obter_servico("pestanas")
    ocup = bot.ocupacoes()
    assert not bot.conflitos_no_intervalo(ocup, DIA, "09:00", "Pestanas", "2h")
