"""
crm/consent.py — consentimento de marketing pedido à CLIENTE, uma só vez.

Porquê: `marketing_opt_in` só se ligava à mão no painel e a cliente nunca era
perguntada. As campanhas de reativação ficavam inutilizáveis — obrigavam a
Daniela a marcar caixas por pessoas que nunca disseram que sim.

Regras que este módulo garante (migração 24):
  • Pergunta-se UMA vez, no fim da primeira marcação feita com sucesso.
  • Um "não" é uma RESPOSTA gravada, não a ausência de um "sim" — quem
    recusou nunca mais é incomodado.
  • A pergunta é um extra: não faz parte da marcação e ignorá-la não parte
    nada (a confirmação já foi enviada antes).
  • Quem responde "sim" fica com `marketing_opt_in = 1`, a coluna canónica
    que campaigns/engine.py já lê para excluir quem não consentiu.
"""

from __future__ import annotations

import logging

import db
import tempo

log = logging.getLogger("crm.consent")

# IDs dos botões — o webhook recebe-os de volta tal e qual.
BOTAO_SIM = "consent_marketing_sim"
BOTAO_NAO = "consent_marketing_nao"

ORIGEM_WHATSAPP = "whatsapp_pos_marcacao"

_TEXTOS = {
    "pergunta": {
        "pt": ("Antes de ir: posso enviar-lhe de vez em quando novidades, promoções "
               "e lembretes para voltar a marcar?\n\n"
               "Pode dizer que não — a sua marcação fica na mesma, e as mensagens "
               "sobre as suas marcações continuam a chegar de qualquer forma."),
        "de": ("Bevor Sie gehen: Darf ich Ihnen gelegentlich Neuigkeiten, Aktionen "
               "und Erinnerungen für einen neuen Termin schicken?\n\n"
               "Sie dürfen Nein sagen — Ihr Termin bleibt bestehen, und Nachrichten "
               "zu Ihren Terminen erhalten Sie weiterhin."),
        "en": ("Before you go: may I send you occasional news, offers and reminders "
               "to book again?\n\n"
               "You can say no — your booking stays exactly as it is, and messages "
               "about your own bookings keep coming either way."),
    },
    "botao_sim": {"pt": "Sim, pode", "de": "Ja, gerne", "en": "Yes, please"},
    "botao_nao": {"pt": "Não, obrigado", "de": "Nein, danke", "en": "No, thanks"},
    "obrigado_sim": {
        "pt": "Obrigada! 💛 Vai receber as novidades. Pode dizer «parar» a qualquer momento.",
        "de": "Vielen Dank! 💛 Sie erhalten unsere Neuigkeiten. Sie können jederzeit «stopp» schreiben.",
        "en": "Thank you! 💛 You'll get our news. You can say “stop” at any time.",
    },
    "obrigado_nao": {
        "pt": "Sem problema — não lhe enviamos novidades. 🙂",
        "de": "Kein Problem — wir schicken Ihnen keine Neuigkeiten. 🙂",
        "en": "No problem — we won't send you any news. 🙂",
    },
}


def texto(chave: str, idioma: str = "pt") -> str:
    modelo = _TEXTOS.get(chave, {})
    return modelo.get(idioma) or modelo.get("pt") or ""


def botoes(idioma: str = "pt") -> list[dict]:
    return [{"id": BOTAO_SIM, "titulo": texto("botao_sim", idioma)},
            {"id": BOTAO_NAO, "titulo": texto("botao_nao", idioma)}]


def deve_perguntar(telefone: str, tenant_id: int = 1) -> bool:
    """Só na PRIMEIRA vez, e só a quem nunca respondeu nem foi perguntado."""
    cust = _customer(telefone, tenant_id)
    if not cust:
        return False
    if cust.get("marketing_consent_response") or cust.get("marketing_opt_in"):
        return False
    return not cust.get("marketing_consent_asked_at")


def registar_pergunta(telefone: str, tenant_id: int = 1) -> None:
    """Marca-se ANTES de enviar. Se o envio falhar, não se insiste: uma
    pergunta de consentimento repetida é exatamente o tipo de mensagem que
    faz uma cliente bloquear o número."""
    with db.ligacao() as c:
        c.execute(
            "UPDATE customers SET marketing_consent_asked_at = ?, updated_at = ? "
            "WHERE tenant_id = ? AND phone = ? AND marketing_consent_asked_at IS NULL",
            (tempo.iso_utc(), tempo.iso_utc(), tenant_id, telefone))


def registar_resposta(telefone: str, aceitou: bool, origem: str = ORIGEM_WHATSAPP,
                      tenant_id: int = 1) -> bool:
    """Grava a resposta com data e origem. Devolve False se este telefone não
    tem ficha (não devia acontecer — só se responder a uma pergunta de uma
    ficha entretanto apagada)."""
    agora = tempo.iso_utc()
    with db.ligacao() as c:
        cur = c.execute(
            "UPDATE customers SET marketing_opt_in = ?, marketing_consent_response = ?, "
            "marketing_consent_at = ?, marketing_consent_source = ?, updated_at = ? "
            "WHERE tenant_id = ? AND phone = ?",
            (1 if aceitou else 0, "sim" if aceitou else "nao", agora, origem, agora,
             tenant_id, telefone))
        ok = cur.rowcount > 0
    if ok:
        log.info("consentimento de marketing: %s (origem=%s)", "sim" if aceitou else "nao", origem)
    return ok


def estado(telefone: str, tenant_id: int = 1) -> dict | None:
    """O que está gravado, para o painel e para os testes."""
    cust = _customer(telefone, tenant_id)
    if not cust:
        return None
    return {
        "opt_in": bool(cust.get("marketing_opt_in")),
        "resposta": cust.get("marketing_consent_response"),
        "respondido_em": cust.get("marketing_consent_at"),
        "origem": cust.get("marketing_consent_source"),
        "perguntado_em": cust.get("marketing_consent_asked_at"),
    }


def _customer(telefone: str, tenant_id: int) -> dict | None:
    with db.ligacao() as c:
        r = c.execute(f"SELECT {db._SQL_CUSTOMER} FROM customers "
                      "WHERE tenant_id = ? AND phone = ?", (tenant_id, telefone)).fetchone()
    return db._linha_customer(r) if r else None
