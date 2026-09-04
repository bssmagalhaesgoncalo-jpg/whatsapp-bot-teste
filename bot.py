"""
Bot "rececionista digital" via WhatsApp Cloud API para a DANIELA BEAUTY.

Fluxo do cliente:
  idioma -> serviço -> dia -> hora disponível -> resumo -> confirmar
  -> marcação criada (o horário fica indisponível) -> confirmação.
O cliente pode ainda consultar, reagendar e cancelar a sua marcação.

Comandos permanentes em texto livre: MENU, VOLTAR, CANCELAR, AJUDA, HUMANO,
GERIR, IDIOMA. Seleção de idioma (PT/DE/EN) como primeira interação.

Configuração: TUDO vem do ambiente — ver `.env.example` e o módulo `config`.
Não há segredos nem números de teste embutidos no código.

Arquitetura (Fase 0 + Fase 1):
  config.py    -> variáveis de ambiente, sem defaults sensíveis
  db.py        -> ligação + migrações versionadas (SQLite agora, Postgres-ready)
  catalogo.py  -> FONTE ÚNICA dos serviços (nome/duração/preço/cor)
  parsing.py   -> interpretação de datas/horas/durações LEGADAS (texto)
  tempo.py     -> "agora" e fuso Europe/Zurich (timezone-aware, trata DST)

Nota sobre idiomas: as mensagens para o CLIENTE existem em pt/de/en via o
sistema central `TEXTOS` + `t()`/`tx()`. As notificações INTERNAS para o
negócio (PROVIDER_WHATSAPP) são sempre em português. No alemão usa-se sempre
"ss", nunca "ß".
"""

import re
import json
import hmac
import hashlib
import logging
from functools import wraps
from datetime import date, timedelta, datetime
from flask import Flask, request, jsonify, Response

import config
import db as bd
import catalogo
import estados
import tempo
from parsing import data_iso_de_texto, hora_hhmm_de_texto, duracao_para_minutos
from messaging import whatsapp as _wa
from core import events as eventos
from notifications import business as notif_negocio
from scheduling import business_hours as bh_mod
from scheduling import availability as av_mod

app = Flask(__name__)

# Painel operacional novo (SaaS shell) — servido de templates/ + static/ pelo
# blueprint dashboard/, montado em /app. /painel e /dashboard antigos mantêm-se.
from dashboard import bp as _dashboard_bp   # noqa: E402
app.register_blueprint(_dashboard_bp)

# --- Aliases retro-compatíveis: o resto do ficheiro (e os testes) continuam a
# usar estes nomes; a verdade única vive em `config`. ----------------------
TOKEN = config.WHATSAPP_TOKEN
PHONE_NUMBER_ID = config.PHONE_NUMBER_ID
VERIFY_TOKEN = config.VERIFY_TOKEN
PROVIDER_WHATSAPP = config.PROVIDER_WHATSAPP
APP_SECRET = config.APP_SECRET
MEDIA_DIR = config.MEDIA_DIR
DASHBOARD_USER = config.DASHBOARD_USER
DASHBOARD_PASSWORD = config.DASHBOARD_PASSWORD
PUBLIC_BASE_URL = config.PUBLIC_BASE_URL

BUSINESS_NAME = config.BUSINESS_NAME
BUSINESS_ADDRESS = config.BUSINESS_ADDRESS
# Nomes históricos, ainda usados em muitas mensagens/painel — apontam para a
# identidade única configurada por ambiente.
NOME_OFICINA = BUSINESS_NAME
MORADA_OFICINA = BUSINESS_ADDRESS


def graph_url():
    """URL do endpoint de envio da Meta, ou None se PHONE_NUMBER_ID faltar."""
    return config.graph_url()


# Compat: código antigo referencia GRAPH_URL como string.
GRAPH_URL = config.graph_url() or ""

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("bot")

# --- AUTOMATION ENGINE: liga o barramento de eventos aos consumidores ------
# V1: a notificação privada ao negócio. (Handlers de reminders/review/
# rebooking entram na Fase K/P via o mesmo `eventos.registar`.)
eventos.registar("*", notif_negocio.handler_evento)


def _notificar_criacao_marcacao(ev):
    """PONTO ÚNICO da notificação privada de uma marcação NOVA: o texto vem do
    formatador único (notif_negocio.render_evento) e é enviado com a lista de
    ações da equipa. Uma marcação -> um evento booking.created/pending -> uma
    notificação. O webhook já NÃO envia diretamente (ver receber_mensagem)."""
    if ev["type"] not in ("booking.created", "booking.pending"):
        return
    texto = notif_negocio.render_evento(ev)
    if texto:
        enviar_notificacao_interna_marcacao(ev.get("entity_id"), texto)


eventos.registar("booking.created", _notificar_criacao_marcacao)
eventos.registar("booking.pending", _notificar_criacao_marcacao)


def disparar_automacoes():
    """Processa a outbox de eventos (síncrono, V1). Chamado no fim de cada
    request que possa ter gravado eventos. Nunca deixa uma exceção escapar —
    um evento por processar é re-tentado no próximo disparo."""
    try:
        eventos.drain()
    except Exception:                        # noqa: BLE001
        log.exception("disparar_automacoes falhou")


# IDs usados em botões/listas em todo o fluxo (nunca traduzidos — são
# identificadores internos, não texto visível)
ID_VOLTAR = "voltar"
ID_CANCELAR = "cancelar_processo"

# IDs canónicos e reutilizáveis para as ações universais (menu, nova
# marcação, voltar, cancelar, carrinho, gerir, idioma, humano, orçamento
# rápido) — usados em todos os novos botões/listas criados por este pedido,
# para nunca obrigar o cliente a escrever um comando. Os IDs "históricos"
# (ID_VOLTAR, ID_CANCELAR, "mp_marcar", "ver_carrinho", "modo_rapido", ...)
# continuam a funcionar exatamente como antes — cada ACAO_* é tratado como
# um SINÓNIMO do respetivo ID histórico no despacho do webhook (nunca uma
# substituição), para não repetir todo o fluxo já testado.
ACAO_MENU = "acao_menu"
ACAO_NOVA_MARCACAO = "acao_nova_marcacao"
ACAO_VOLTAR = "acao_voltar"
ACAO_CANCELAR = "acao_cancelar"
ACAO_CARRINHO = "acao_carrinho"
ACAO_GERIR = "acao_gerir"
ACAO_IDIOMA = "acao_idioma"
ACAO_HUMANO = "acao_humano"
ACAO_RAPIDO = "acao_rapido"
ACAO_MAIS = "acao_mais"  # "⚙️ Mais ações" — submenu quando uma lista está perto do limite
# "⬅️ Voltar" DENTRO do carrinho: o carrinho não é um passo do fluxo, por isso
# aqui Voltar não desfaz nada — regressa exatamente ao ecrã onde o cliente
# estava antes de o abrir (ver reenviar_passo_atual).
ID_VOLTAR_CARRINHO = "carrinho_voltar"
# "⬅️ Voltar" na descrição livre de "Outra alteração": leva o id do orçamento
# embutido, para regressar à LISTA de aspetos desse mesmo orçamento.
ID_ALT_VOLTAR = "orcamento_alt_voltar_"

# Limites impostos pela API do WhatsApp em mensagens interativas. Aplicados
# defensivamente em enviar_lista()/enviar_botoes(), para que nenhum texto
# novo possa provocar um envio recusado pela Meta.
MAX_LINHAS_LISTA = 10
MAX_BOTOES = 3
MAX_TITULO_LINHA = 24
MAX_TITULO_BOTAO = 20
MAX_RODAPE = 60

IDIOMAS_VALIDOS = ("pt", "de", "en")

# Palavras-comando reconhecidas em texto livre, a qualquer momento. Mantidas
# sempre iguais (não traduzidas) para funcionarem como comandos universais,
# à exceção do trio IDIOMA/SPRACHE/LANGUAGE, que existe precisamente para
# permitir voltar à seleção de idioma a partir de qualquer um dos 3 idiomas.
COMANDOS_TEXTO = {
    "menu", "voltar", "cancelar", "ajuda", "humano", "gerir",
    "idioma", "sprache", "language",
}
COMANDOS_IDIOMA = {"idioma", "sprache", "language"}
# LEGADO (Spotless): CARRINHO e RAPIDO davam entrada no fluxo Wrap/orçamento,
# que já não existe para a Daniela Beauty. Mantidos como conjuntos vazios só
# para o código legado que os referencia não rebentar.
COMANDOS_CARRINHO = set()
COMANDOS_RAPIDO = set()

# IDs dos botões de seleção de idioma -> código de idioma interno
LANG_IDS = {"lang_pt": "pt", "lang_de": "de", "lang_en": "en"}


# ---------------------------------------------------------------------------
# Sistema central de traduções
# ---------------------------------------------------------------------------
# Mensagem fixa de boas-vindas + seleção de idioma — mostrada sempre nos 3
# idiomas ao mesmo tempo (é a única mensagem que não depende de um idioma já
# escolhido, porque é exatamente isso que ainda não sabemos).
TEXTO_SELETOR_IDIOMA = (
    f"👋 Bem-vindo à {BUSINESS_NAME}!\n"
    f"Willkommen bei {BUSINESS_NAME}!\n"
    f"Welcome to {BUSINESS_NAME}!\n"
    "Para continuar, escolha o seu idioma.\n"
    "Wählen Sie Ihre Sprache, um fortzufahren.\n"
    "Choose your language to continue."
)

BOTOES_IDIOMA = [
    {"id": "lang_pt", "titulo": "🇵🇹 Português"},
    {"id": "lang_de", "titulo": "🇩🇪 Deutsch"},
    {"id": "lang_en", "titulo": "🇬🇧 English"},
]

TEXTOS = {
    # --- Menu principal / saudação ---------------------------------------
    "saudacao_novo": {"pt": "👋 Olá! Bem-vindo à *{oficina}*.",
                       "de": "👋 Hallo! Willkommen bei *{oficina}*.",
                       "en": "👋 Hello! Welcome to *{oficina}*."},
    "saudacao_volta": {"pt": "👋 Olá, {nome}! Bem-vindo de volta à *{oficina}*.",
                        "de": "👋 Hallo, {nome}! Willkommen zurück bei *{oficina}*.",
                        "en": "👋 Hello, {nome}! Welcome back to *{oficina}*."},
    "menu_corpo": {"pt": "Posso ajudá-lo em menos de 1 minuto. O que deseja fazer?",
                   "de": "Ich kann Ihnen in weniger als 1 Minute helfen. Was möchten Sie tun?",
                   "en": "I can help you in under 1 minute. What would you like to do?"},
    "menu_titulo_lista": {"pt": "Menu principal", "de": "Hauptmenü", "en": "Main menu"},
    "menu_botao": {"pt": "👉 Escolher opção", "de": "👉 Option wählen", "en": "👉 Choose option"},


    # --- Marcação Daniela Beauty: escolha do serviço (passo 1 de 3) --------
    "servico_corpo": {"pt": "Passo 1 de 3 — Que serviço deseja marcar?",
                       "de": "Schritt 1 von 3 — Welche Behandlung möchten Sie buchen?",
                       "en": "Step 1 of 3 — Which treatment would you like to book?"},
    "servico_seccao": {"pt": "Serviços", "de": "Behandlungen", "en": "Treatments"},
    "servico_botao": {"pt": "✨ Escolher serviço", "de": "✨ Behandlung wählen",
                       "en": "✨ Choose treatment"},
    "servico_sem_ativos": {
        "pt": "De momento não há serviços disponíveis para marcação online. "
              "Toque em \"Falar com a equipa\".",
        "de": "Zurzeit sind keine Behandlungen online buchbar. "
              "Tippen Sie auf \"Mit dem Team sprechen\".",
        "en": "There are currently no treatments available to book online. "
              "Tap \"Talk to the team\"."},
    # Preço por definir: NUNCA "CHF 0". Mostrado no serviço, no resumo e no total.
    "preco_a_confirmar": {"pt": "Preço a confirmar", "de": "Preis auf Anfrage",
                           "en": "Price on request"},
    "resumo_total_a_confirmar": {
        "pt": "💰 Total: a confirmar pela equipa",
        "de": "💰 Gesamtbetrag: wird vom Team bestätigt",
        "en": "💰 Total: to be confirmed by the team"},
    "confirmado_preco_a_confirmar": {
        "pt": "ℹ️ O preço deste serviço é confirmado pela equipa antes da marcação.",
        "de": "ℹ️ Der Preis dieser Behandlung wird vom Team vor dem Termin bestätigt.",
        "en": "ℹ️ The price for this treatment is confirmed by the team before your appointment."},

    # --- Rodapé / linhas auxiliares de lista -------------------------------
    "rodape_padrao": {"pt": "Escreva VOLTAR, CANCELAR ou MENU",
                       "de": "Schreiben Sie VOLTAR, CANCELAR oder MENU",
                       "en": "Type VOLTAR, CANCELAR or MENU"},
    "voltar_titulo": {"pt": "⬅️ Voltar", "de": "⬅️ Zurück", "en": "⬅️ Back"},
    "voltar_desc": {"pt": "Passo anterior", "de": "Vorheriger Schritt", "en": "Previous step"},
    "cancelar_titulo": {"pt": "❌ Cancelar processo", "de": "❌ Vorgang abbrechen", "en": "❌ Cancel process"},
    "cancelar_desc": {"pt": "Terminar sem marcar", "de": "Ohne Buchung beenden", "en": "End without booking"},






    # --- Data / hora --------------------------------------------------------
    "data_corpo": {"pt": "Passo {n} de 3 — Para que dia gostaria de marcar?",
                   "de": "Schritt {n} von 3 — Für welchen Tag möchten Sie buchen?",
                   "en": "Step {n} of 3 — Which day would you like to book?"},
    "data_seccao": {"pt": "Datas disponíveis", "de": "Verfügbare Termine", "en": "Available dates"},
    "data_botao": {"pt": "📅 Escolher dia", "de": "📅 Tag wählen", "en": "📅 Choose day"},

    "hora_corpo": {"pt": "Passo {n} de 3 — A que horas lhe convém?",
                   "de": "Schritt {n} von 3 — Um wie viel Uhr passt es Ihnen?",
                   "en": "Step {n} of 3 — What time suits you?"},
    "hora_seccao": {"pt": "Horários disponíveis", "de": "Verfügbare Uhrzeiten", "en": "Available times"},
    "hora_botao": {"pt": "⏰ Escolher hora", "de": "⏰ Uhrzeit wählen", "en": "⏰ Choose time"},
    # A lista de horas mostra só o que está MESMO livre. Quando nada sobra
    # nesse dia, o cliente volta a escolher a data — nunca fica sem saída.
    "hora_sem_vagas": {
        "pt": "😕 Nesse dia já não temos horários livres. Escolha outro dia, por favor.",
        "de": "😕 An diesem Tag haben wir keine freien Uhrzeiten mehr. Bitte wählen Sie einen anderen Tag.",
        "en": "😕 There are no free time slots left on that day. Please choose another day."},
    "hora_entretanto_ocupada": {
        "pt": "😕 Esse horário foi ocupado entretanto. Escolha outro, por favor.",
        "de": "😕 Diese Uhrzeit wurde inzwischen belegt. Bitte wählen Sie eine andere.",
        "en": "😕 That time slot has just been taken. Please choose another one."},

    # --- Resumo / confirmação -----------------------------------------------
    "resumo_titulo": {"pt": "📋 *Confirme a sua marcação*", "de": "📋 *Bestätigen Sie Ihre Buchung*",
                       "en": "📋 *Confirm your booking*"},
    "resumo_data": {"pt": "📅 Data: {data}", "de": "📅 Datum: {data}", "en": "📅 Date: {data}"},
    "resumo_hora": {"pt": "🕒 Hora: {hora}", "de": "🕒 Uhrzeit: {hora}", "en": "🕒 Time: {hora}"},
    "resumo_duracao": {"pt": "⏱️ Duração estimada: {duracao}", "de": "⏱️ Geschätzte Dauer: {duracao}",
                        "en": "⏱️ Estimated duration: {duracao}"},
    "resumo_discriminacao": {"pt": "📊 Discriminação:", "de": "📊 Aufschlüsselung:", "en": "📊 Breakdown:"},
    "resumo_total": {"pt": "💰 Total: {total}", "de": "💰 Gesamtbetrag: {total}", "en": "💰 Total: {total}"},
    "resumo_pergunta": {"pt": "Está tudo correto?", "de": "Ist alles korrekt?", "en": "Is everything correct?"},
    "botao_confirmar": {"pt": "✅ Confirmar", "de": "✅ Bestätigen", "en": "✅ Confirm"},
    "botao_alterar": {"pt": "✏️ Alterar", "de": "✏️ Ändern", "en": "✏️ Change"},
    "botao_cancelar": {"pt": "❌ Cancelar", "de": "❌ Abbrechen", "en": "❌ Cancel"},

    "obrigado_nome": {"pt": "Obrigado, {nome}!", "de": "Danke, {nome}!", "en": "Thank you, {nome}!"},
    "obrigado": {"pt": "Obrigado!", "de": "Danke!", "en": "Thank you!"},
    "confirmado_titulo": {"pt": "🎉 {saudacao} A sua marcação está confirmada!",
                           "de": "🎉 {saudacao} Ihre Buchung ist bestätigt!",
                           "en": "🎉 {saudacao} Your booking is confirmed!"},
    "confirmado_data_hora": {"pt": "📅 {data} às {hora}", "de": "📅 {data} um {hora}", "en": "📅 {data} at {hora}"},
    "confirmado_duracao": {"pt": "⏱️ Duração: aproximadamente {duracao}",
                            "de": "⏱️ Dauer: ungefähr {duracao}",
                            "en": "⏱️ Duration: approximately {duracao}"},
    "confirmado_instrucao": {"pt": "Por favor, chegue cerca de 5 minutos antes da sua marcação.",
                              "de": "Bitte kommen Sie etwa 5 Minuten vor Ihrem Termin an.",
                              "en": "Please arrive about 5 minutes before your appointment."},
    # (o antigo "confirmado_rodape", que mandava escrever MENU/GERIR, foi
    # removido — essas ações são agora os botões enviados logo a seguir à
    # confirmação: "🗓️ Gerir marcação" e "🏠 Menu principal".)






    # --- Notificação interna sobre um pedido: reação do cliente (recusa) ----
    "botao_menu_principal": {"pt": "🏠 Menu principal", "de": "🏠 Hauptmenü", "en": "🏠 Main menu"},

    "rapido_linha_lista": {"pt": "⚡ Pedido rápido", "de": "⚡ Schnellanfrage", "en": "⚡ Quick request"},










    # --- Gestão de marcação -----------------------------------------------
    "gerir_sem_marcacao": {"pt": "Não encontrei nenhuma marcação ativa associada a este número.",
                            "de": "Ich habe keine aktive Buchung zu dieser Nummer gefunden.",
                            "en": "I couldn't find any active booking for this number."},
    "gerir_corpo": {"pt": "🗓️ A sua marcação #{id}:\n\n🔧 {servico}\n📅 {data} às {hora}\n⏱️ Duração: {duracao}\n💰 {preco}\n\nO que deseja fazer?",
                    "de": "🗓️ Ihre Buchung #{id}:\n\n🔧 {servico}\n📅 {data} um {hora}\n⏱️ Dauer: {duracao}\n💰 {preco}\n\nWas möchten Sie tun?",
                    "en": "🗓️ Your booking #{id}:\n\n🔧 {servico}\n📅 {data} at {hora}\n⏱️ Duration: {duracao}\n💰 {preco}\n\nWhat would you like to do?"},
    "botao_reagendar": {"pt": "✏️ Reagendar", "de": "✏️ Verschieben", "en": "✏️ Reschedule"},
    "botao_cancelar_marcacao": {"pt": "❌ Cancelar", "de": "❌ Stornieren", "en": "❌ Cancel"},
    "botao_nova_marcacao": {"pt": "📅 Nova marcação", "de": "📅 Neue Buchung", "en": "📅 New booking"},
    "reagendar_aviso": {
        "pt": "Sem problema. Vamos escolher a nova data e hora — a sua marcação atual "
              "mantém-se até confirmar a nova.",
        "de": "Kein Problem. Wählen wir das neue Datum und die neue Uhrzeit — Ihr aktueller "
              "Termin bleibt bestehen, bis Sie den neuen bestätigen.",
        "en": "No problem. Let's pick the new date and time — your current appointment stays "
              "until you confirm the new one."},
    "reagendar_ocupado": {
        "pt": "😕 Esse horário foi entretanto ocupado. Escolha outro, por favor — a sua "
              "marcação atual continua igual.",
        "de": "😕 Diese Uhrzeit ist inzwischen belegt. Bitte wählen Sie eine andere — Ihr "
              "aktueller Termin bleibt unverändert.",
        "en": "😕 That time was just taken. Please pick another — your current appointment is "
              "unchanged."},
    "reagendar_confirmado": {
        "pt": "✅ Marcação #{id} reagendada para {data} às {hora}.",
        "de": "✅ Buchung #{id} verschoben auf {data} um {hora}.",
        "en": "✅ Booking #{id} rescheduled to {data} at {hora}."},
    "reagendar_ja_nao_valida": {
        "pt": "Esta marcação já não pode ser reagendada. Escreva MENU para começar de novo.",
        "de": "Diese Buchung kann nicht mehr verschoben werden. Schreiben Sie MENU, um neu zu beginnen.",
        "en": "This booking can no longer be rescheduled. Type MENU to start again."},
    "cancelado_cliente": {"pt": "✅ A sua marcação foi cancelada.",
                           "de": "✅ Ihre Buchung wurde storniert.",
                           "en": "✅ Your booking has been cancelled."},

    # --- Falar com a equipa ------------------------------------------------
    "humano_cliente": {"pt": "💬 Vou avisar já a nossa equipa — em breve alguém entra em contacto consigo por aqui.",
                        "de": "💬 Ich informiere unser Team sofort — jemand wird sich in Kürze hier bei Ihnen melden.",
                        "en": "💬 I'll let our team know right away — someone will get in touch with you here shortly."},

    # --- Ajuda / erros / comandos ------------------------------------------
    "ajuda_header": {"pt": "🆘 *Comandos disponíveis, a qualquer momento:*",
                      "de": "🆘 *Jederzeit verfügbare Befehle:*",
                      "en": "🆘 *Commands available at any time:*"},
    "ajuda_menu": {"pt": "• MENU — voltar ao menu principal", "de": "• MENU — zurück zum Hauptmenü",
                   "en": "• MENU — return to the main menu"},
    "ajuda_voltar": {"pt": "• VOLTAR — passo anterior", "de": "• VOLTAR — vorheriger Schritt",
                     "en": "• VOLTAR — previous step"},
    "ajuda_cancelar": {"pt": "• CANCELAR — cancelar o processo atual", "de": "• CANCELAR — aktuellen Vorgang abbrechen",
                        "en": "• CANCELAR — cancel the current process"},
    "ajuda_gerir": {"pt": "• GERIR — ver/alterar a sua marcação", "de": "• GERIR — Ihre Buchung ansehen/ändern",
                     "en": "• GERIR — view/change your booking"},
    "ajuda_ajuda": {"pt": "• AJUDA — mostrar esta mensagem", "de": "• AJUDA — diese Nachricht anzeigen",
                     "en": "• AJUDA — show this message"},
    "ajuda_humano": {"pt": "• HUMANO — falar diretamente com a equipa", "de": "• HUMANO — direkt mit dem Team sprechen",
                      "en": "• HUMANO — talk directly to the team"},
    "ajuda_idioma": {"pt": "• IDIOMA / SPRACHE / LANGUAGE — mudar de idioma",
                      "de": "• IDIOMA / SPRACHE / LANGUAGE — Sprache ändern",
                      "en": "• IDIOMA / SPRACHE / LANGUAGE — change language"},

    "carrinho_botao_ver": {"pt": "🛒 Carrinho", "de": "🛒 Warenkorb", "en": "🛒 Cart"},

    "nao_entendi": {"pt": "Desculpe, não consegui perceber 😅\n\nEscolha uma das opções abaixo.",
                     "de": "Entschuldigung, das habe ich nicht verstanden 😅\n\nWählen Sie eine der Optionen unten.",
                     "en": "Sorry, I didn't understand that 😅\n\nPlease choose one of the options below."},
    "processo_cancelado": {"pt": "❌ Processo cancelado.",
                            "de": "❌ Vorgang abgebrochen.",
                            "en": "❌ Process cancelled."},

    "retomar_pergunta": {"pt": "Encontrámos uma marcação que ainda não terminou.\nDeseja continuar ou começar novamente?",
                          "de": "Wir haben eine noch nicht abgeschlossene Buchung gefunden.\nMöchten Sie fortfahren oder neu beginnen?",
                          "en": "We found a booking that wasn't finished.\nWould you like to continue or start again?"},
    "botao_continuar": {"pt": "▶️ Continuar", "de": "▶️ Fortfahren", "en": "▶️ Continue"},
    "botao_recomecar": {"pt": "🔄 Recomeçar", "de": "🔄 Neu beginnen", "en": "🔄 Start again"},

    "preco_a_combinar": {"pt": "a combinar", "de": "auf Anfrage", "en": "on request"},

    # --- Ações universais / seguimento sem obrigar a escrever comandos -------
    "e_agora_pergunta": {"pt": "O que deseja fazer a seguir?", "de": "Was möchten Sie als Nächstes tun?",
                          "en": "What would you like to do next?"},
    "botao_falar_equipa": {"pt": "💬 Falar com a equipa", "de": "💬 Mit dem Team sprechen",
                            "en": "💬 Talk to the team"},
    "botao_gerir_marcacao": {"pt": "🗓️ Gerir marcação", "de": "🗓️ Termin verwalten", "en": "🗓️ Manage booking"},
    "botao_mais_acoes": {"pt": "⚙️ Mais ações", "de": "⚙️ Weitere Aktionen", "en": "⚙️ More actions"},
    "mais_acoes_pergunta": {"pt": "O que deseja fazer?", "de": "Was möchten Sie tun?", "en": "What would you like to do?"},
    "mais_acoes_seccao": {"pt": "Mais ações", "de": "Weitere Aktionen", "en": "More actions"},
    "nao_entendi_opcoes": {"pt": "Vamos tentar de outra forma. O que deseja fazer?",
                            "de": "Versuchen wir es anders. Was möchten Sie tun?",
                            "en": "Let's try another way. What would you like to do?"},





    "carrinho_marcacao_nao_encontrada": {"pt": "Não encontrei essa marcação confirmada.",
                                          "de": "Diese bestätigte Buchung wurde nicht gefunden.",
                                          "en": "I couldn't find that confirmed booking."},

    "botao_voltar": {"pt": "⬅️ Voltar", "de": "⬅️ Zurück", "en": "⬅️ Back"},
    "pag_mais_opcoes": {"pt": "➡️ Mais opções", "de": "➡️ Weitere Optionen", "en": "➡️ More options"},
    "pag_opcoes_anteriores": {"pt": "⬅️ Opções anteriores", "de": "⬅️ Vorherige Optionen",
                               "en": "⬅️ Previous options"},
    "pag_desc_mais": {"pt": "Ver as opções seguintes", "de": "Nächste Optionen ansehen",
                       "en": "See the next options"},
    "pag_desc_anteriores": {"pt": "Ver as opções anteriores", "de": "Vorherige Optionen ansehen",
                             "en": "See the previous options"},
    "pag_indicador": {"pt": "Página {pagina} de {total}", "de": "Seite {pagina} von {total}",
                       "en": "Page {pagina} of {total}"},
    "resumo_seccao": {"pt": "Resumo do pedido", "de": "Zusammenfassung", "en": "Request summary"},
    "gerir_seccao": {"pt": "A sua marcação", "de": "Ihre Buchung", "en": "Your booking"},
    "idioma_seccao": {"pt": "Idioma", "de": "Sprache", "en": "Language"},

    # --- Marcação reagendada pela equipa: aviso ao cliente -------------------
    "marcacao_reagendada_cliente": {
        "pt": "📅 A sua marcação #{id} foi alterada.\n\nAntes: {antes}\nAgora: *{agora}*\n\n"
              "Se este novo horário não lhe der jeito, é só dizer.",
        "de": "📅 Ihre Buchung #{id} wurde geändert.\n\nVorher: {antes}\nJetzt: *{agora}*\n\n"
              "Falls dieser neue Termin nicht passt, sagen Sie uns einfach Bescheid.",
        "en": "📅 Your booking #{id} has been changed.\n\nBefore: {antes}\nNow: *{agora}*\n\n"
              "If this new time doesn't suit you, just let us know."},

    # --- Marcação cancelada pela equipa: aviso ao cliente --------------------
    "marcacao_cancelada_equipa_cliente": {
        "pt": "❌ A sua marcação #{id} foi cancelada. Lamentamos o incómodo — estamos à disposição "
              "para marcar uma nova data quando quiser.",
        "de": "❌ Ihre Buchung #{id} wurde storniert. Wir bedauern die Unannehmlichkeiten — gerne "
              "vereinbaren wir jederzeit einen neuen Termin.",
        "en": "❌ Your booking #{id} has been cancelled. We're sorry for the inconvenience — we're "
              "happy to arrange a new date whenever you'd like."},
}


def t(chave, idioma, **kwargs):
    """Devolve o texto central traduzido para `idioma` (com fallback para
    português se faltar alguma tradução), já formatado com `kwargs`."""
    modelo = TEXTOS.get(chave, {})
    texto = modelo.get(idioma) or modelo.get("pt") or ""
    return texto.format(**kwargs) if kwargs else texto


def tx(valor, idioma):
    """Resolve um campo possivelmente multilingue (dict {"pt","de","en"})
    para o idioma pedido. Se `valor` já for uma string simples (ex.:
    horários, que são iguais nos 3 idiomas), devolve-a sem alterações."""
    if valor is None:
        return None
    if isinstance(valor, dict):
        return valor.get(idioma) or valor.get("pt") or next(iter(valor.values()), "")
    return valor


DIAS_SEMANA = {
    "pt": ["seg", "ter", "qua", "qui", "sex", "sáb", "dom"],
    "de": ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"],
    "en": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],
}

MENU_PRINCIPAL = [
    {"id": "mp_marcar",
     "titulo": {"pt": "📅 Marcar um serviço", "de": "📅 Termin buchen", "en": "📅 Book a service"},
     "descricao": {"pt": "Escolher serviço, data e hora", "de": "Service, Datum und Uhrzeit wählen",
                   "en": "Choose service, date and time"}},
    {"id": "mp_gerir",
     "titulo": {"pt": "🗓️ Gerir a minha marcação", "de": "🗓️ Meinen Termin verwalten", "en": "🗓️ Manage my booking"},
     "descricao": {"pt": "Ver, reagendar ou cancelar", "de": "Ansehen, verschieben oder stornieren",
                   "en": "View, reschedule or cancel"}},
    {"id": "mp_humano",
     "titulo": {"pt": "💬 Falar com a equipa", "de": "💬 Mit dem Team sprechen", "en": "💬 Talk to the team"},
     "descricao": {"pt": "Um humano responde-lhe em breve", "de": "Ein Mitarbeiter meldet sich in Kürze",
                   "en": "A team member will reply shortly"}},
    {"id": "mp_idioma",
     "titulo": {"pt": "🌐 Alterar idioma", "de": "🌐 Sprache ändern", "en": "🌐 Change language"},
     "descricao": {"pt": "Português, Deutsch, English", "de": "Português, Deutsch, English",
                   "en": "Português, Deutsch, English"}},
]

# ---------------------------------------------------------------------------
# Persistência em SQLite: sessões em curso + agendamentos confirmados
# (esquema inalterado — o idioma escolhido vive dentro do JSON da sessão,
# tal como "nome", não precisa de coluna própria)
# ---------------------------------------------------------------------------
# Compat: caminho do SQLite (a verdade está em config.SQLITE_PATH).
DB_PATH = config.SQLITE_PATH


def obter_bd():
    """Ligação à base de dados — agora um wrapper fino sobre `db.ligacao()`.

    Diferenças face à versão antiga:
      • já NÃO cria tabelas nem corre ALTER TABLE a cada chamada — o schema
        é construído por migrações versionadas (ver db.MIGRACOES), aplicadas
        uma única vez no arranque;
      • a ligação é sempre FECHADA no fim do `with` (antes ficava aberta).

    Continua a usar-se exatamente como antes: `with obter_bd() as conn:`.
    """
    return bd.ligacao()


# ---------------------------------------------------------------------------
# Configurações do negócio — guardadas na base de dados (persistem a um
# refresh do painel E a um reinício do servidor, ao contrário de uma variável
# em memória ou de localStorage no browser).
# ---------------------------------------------------------------------------
CONFIG_LIBERTAR_AO_CANCELAR = "libertar_horario_ao_cancelar"
CONFIGURACOES_OMISSAO = {
    # LIGADO por defeito: ao cancelar, o horário volta a ficar disponível.
    CONFIG_LIBERTAR_AO_CANCELAR: "1",
}


def obter_configuracao(chave, omissao=None, tenant_id=1):
    """Valor guardado de uma configuração, ou o valor por omissão quando
    ainda nunca foi gravada (base de dados antiga, primeira utilização).
    A identidade da linha é (tenant_id, chave) — ver migração 15."""
    with obter_bd() as conn:
        linha = conn.execute("SELECT valor FROM configuracoes WHERE tenant_id = ? AND chave = ?",
                             (tenant_id, chave)).fetchone()
    if linha:
        return linha[0]
    return CONFIGURACOES_OMISSAO.get(chave) if omissao is None else omissao


def guardar_configuracao(chave, valor, tenant_id=1):
    with obter_bd() as conn:
        conn.execute(
            "INSERT INTO configuracoes (tenant_id, chave, valor, atualizado_em) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(tenant_id, chave) DO UPDATE SET valor = excluded.valor, "
            "atualizado_em = excluded.atualizado_em",
            (tenant_id, chave, str(valor), tempo.iso_utc()),
        )
    return str(valor)


def libertar_horario_ao_cancelar():
    """True -> ao cancelar, o horário volta automaticamente a ficar livre
    (a marcação continua no histórico como cancelada). False -> a marcação
    cancelada continua a ocupar o horário e a impedir novas reservas."""
    return str(obter_configuracao(CONFIG_LIBERTAR_AO_CANCELAR)).strip() in ("1", "true", "True")


def configuracoes_atuais():
    """Configurações tal como o painel as consome (já em booleano)."""
    return {CONFIG_LIBERTAR_AO_CANCELAR: libertar_horario_ao_cancelar()}


# Idempotência do webhook: máquina de estados claimed/processed/failed em
# db.py (reclamar_mensagem / confirmar_mensagem / falhar_mensagem). Um retry
# da Meta a seguir a um processamento FALHADO volta a ser processado.


def carregar_sessao(telefone, tenant_id=1):
    with obter_bd() as conn:
        linha = conn.execute(
            "SELECT dados FROM sessoes WHERE tenant_id = ? AND telefone = ?", (tenant_id, telefone)
        ).fetchone()
    return json.loads(linha[0]) if linha else {}


def guardar_sessao(telefone, sessao, tenant_id=1):
    with obter_bd() as conn:
        conn.execute(
            "INSERT INTO sessoes (tenant_id, telefone, dados) VALUES (?, ?, ?) "
            "ON CONFLICT(tenant_id, telefone) DO UPDATE SET dados = excluded.dados",
            (tenant_id, telefone, json.dumps(sessao)),
        )


def apagar_sessao(telefone, tenant_id=1):
    with obter_bd() as conn:
        conn.execute("DELETE FROM sessoes WHERE tenant_id = ? AND telefone = ?", (tenant_id, telefone))


# Colunas de `agendamentos` lidas em todo o lado — uma lista só, para nunca
# haver um SELECT a devolver menos colunas do que o dicionário espera.
CAMPOS_AGENDAMENTO = ["id", "telefone", "nome", "categoria", "servico", "extra", "data", "hora",
                      "preco", "duracao", "estado", "criado_em", "carrinho_json", "bloqueia_horario",
                      # colunas ESTRUTURADAS (migração 4) — a lógica usa estas
                      "servico_id", "data_iso", "hora_hhmm", "duracao_min", "preco_cents",
                      # tenant + CRM (migrações 8-9) + estado operacional (migração 11)
                      "tenant_id", "customer_id",
                      "op_status", "arrived_at", "started_at", "completed_at"]
SQL_COLUNAS_AGENDAMENTO = ", ".join(CAMPOS_AGENDAMENTO)


def guardar_agendamento(telefone, sessao):
    """Grava a marcação CONFIRMADA da sessão. A verificação de conflitos e o
    INSERT correm dentro da MESMA transação de escrita (BEGIN IMMEDIATE):
    dois clientes que confirmem o mesmo horário ao mesmo tempo são
    obrigatoriamente serializados pelo SQLite e o segundo recebe
    HorarioOcupado — nunca ficam as duas marcações gravadas."""
    data_iso = data_iso_de_texto(sessao.get("data"))
    hora = hora_hhmm_de_texto(sessao.get("hora"))
    duracao = recuperar_duracao(sessao.get("servico"), sessao.get("duracao"))

    servico_id = sessao.get("servico_id")
    duracao_min = sessao.get("duracao_min")
    preco_cents = sessao.get("preco_cents")
    if servico_id and (duracao_min is None or preco_cents is None):
        s = bd.obter_servico(servico_id) or {}
        duracao_min = duracao_min if duracao_min is not None else s.get("duracao_min")
        preco_cents = preco_cents if "preco_cents" in sessao else s.get("preco_cents")
    estado = estado_inicial_marcacao()
    tenant_id = sessao.get("tenant_id", 1)

    with obter_bd() as conn:
        conn.execute("BEGIN IMMEDIATE")
        if data_iso and hora:
            # Conta com as marcações gravadas E com os horários que outros
            # clientes escolheram e ainda estão a confirmar (a retenção do
            # próprio é ignorada — é exatamente esta que ele vem confirmar).
            if conflitos_no_intervalo(ocupacoes(telefone, conn), data_iso, hora,
                                      sessao.get("servico"), duracao):
                raise HorarioOcupado(f"{data_iso} {hora}")

        # CRM: cliente (cria se novo) e ligação à marcação — na mesma transação.
        ja_existia = conn.execute(
            "SELECT 1 FROM customers WHERE tenant_id = ? AND phone = ?", (tenant_id, telefone)
        ).fetchone()
        cust = bd.obter_ou_criar_customer(telefone, sessao.get("nome"), sessao.get("idioma"),
                                          tenant_id=tenant_id, conn=conn)

        cur = conn.execute(
            "INSERT INTO agendamentos "
            "(telefone, nome, categoria, servico, extra, data, hora, preco, duracao, estado, criado_em, "
            "carrinho_json, bloqueia_horario, servico_id, data_iso, hora_hhmm, duracao_min, preco_cents, "
            "tenant_id, customer_id, op_status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, 'scheduled')",
            (
                telefone, sessao.get("nome"), sessao.get("categoria"),
                sessao.get("servico"), sessao.get("extra"),
                sessao.get("data"), sessao.get("hora"),
                sessao.get("preco"), sessao.get("duracao"),
                estado,
                tempo.iso_utc(),
                json.dumps(sessao.get("carrinho", [])),
                servico_id, data_iso, hora, duracao_min, preco_cents,
                tenant_id, cust["id"],
            ),
        )
        id_ag = cur.lastrowid

        # OUTBOX: eventos de domínio na MESMA transação do INSERT.
        if not ja_existia:
            bd.registar_evento(conn, "customer.created", "customer", cust["id"],
                               {"nome": sessao.get("nome"), "telefone": telefone},
                               dedupe_key=f"customer.created:{cust['id']}", tenant_id=tenant_id)
        bd.registar_evento(
            conn, "booking.pending" if estado == estados.PENDING else "booking.created",
            "appointment", id_ag,
            {"servico_id": servico_id, "servico": sessao.get("servico"),
             "data": sessao.get("data"), "hora": sessao.get("hora"),
             "duracao_min": duracao_min,
             "preco_cents": preco_cents, "cliente": sessao.get("nome"),
             "telefone": telefone, "customer_id": cust["id"]},
            dedupe_key=f"booking.created:{id_ag}", tenant_id=tenant_id)

        bd.recalcular_customer(cust["id"], conn=conn)
        return id_ag


def _agendamentos_da_conexao(conn):
    """Todas as marcações, lidas por uma conexão JÁ dentro de uma transação
    — usado pela verificação de conflitos que tem de correr atomicamente
    com a escrita."""
    linhas = conn.execute(f"SELECT {SQL_COLUNAS_AGENDAMENTO} FROM agendamentos").fetchall()
    return [dict(zip(CAMPOS_AGENDAMENTO, l)) for l in linhas]


def listar_agendamentos():
    with obter_bd() as conn:
        linhas = conn.execute(
            f"SELECT {SQL_COLUNAS_AGENDAMENTO} FROM agendamentos ORDER BY id DESC"
        ).fetchall()
    return [dict(zip(CAMPOS_AGENDAMENTO, l)) for l in linhas]


def ultimo_agendamento_ativo(telefone):
    with obter_bd() as conn:
        linha = conn.execute(
            "SELECT id, servico, data, hora, preco, duracao FROM agendamentos "
            "WHERE telefone = ? AND estado IN (" + estados.sql_lista(*estados.ATIVOS) + ") ORDER BY id DESC LIMIT 1",
            (telefone,),
        ).fetchone()
    if not linha:
        return None
    return {"id": linha[0], "servico": linha[1], "data": linha[2], "hora": linha[3],
            "preco": linha[4], "duracao": recuperar_duracao(linha[1], linha[5])}


def obter_agendamento(id_agendamento):
    with obter_bd() as conn:
        linha = conn.execute(
            f"SELECT {SQL_COLUNAS_AGENDAMENTO} FROM agendamentos WHERE id = ?",
            (id_agendamento,),
        ).fetchone()
    return dict(zip(CAMPOS_AGENDAMENTO, linha)) if linha else None


def agendamentos_confirmados_por_telefone(telefone):
    """Marcações CONFIRMADAS de um número, da mais recente para a mais antiga
    — a fonte de verdade do carrinho persistente depois de a sessão ser
    reiniciada. Só devolve estado "confirmado": marcações canceladas,
    concluídas, reagendadas ou arquivadas deixam de aparecer no carrinho."""
    with obter_bd() as conn:
        linhas = conn.execute(
            f"SELECT {SQL_COLUNAS_AGENDAMENTO} FROM agendamentos "
            "WHERE telefone = ? AND estado IN (" + estados.sql_lista(*estados.ATIVOS) + ") ORDER BY id DESC",
            (telefone,),
        ).fetchall()
    return [dict(zip(CAMPOS_AGENDAMENTO, l)) for l in linhas]


def linhas_carrinho_agendamento(agendamento):
    """Linhas do carrinho tal como foram guardadas COM a marcação
    (carrinho_json). Marcações antigas, criadas antes de essa coluna existir,
    não têm nenhuma — nesse caso devolve [] e quem chama usa o fallback
    serviço/extra/preço (ver total_centimos_agendamento)."""
    try:
        linhas = json.loads(agendamento.get("carrinho_json") or "[]")
    except (ValueError, TypeError):
        return []
    return linhas if isinstance(linhas, list) else []


def preco_por_confirmar_agendamento(agendamento):
    """True quando a marcação é de um serviço SEM preço definido (preco_cents
    NULL) e não tem carrinho nem preço legado. Nesses casos NÃO se mostra
    "CHF 0" — mostra-se "a confirmar"."""
    ag = agendamento or {}
    if linhas_carrinho_agendamento(ag):
        return False
    if ag.get("preco") not in (None, "", 0, 0.0):
        return False
    if ag.get("preco_cents") is not None:
        return False
    return bool(ag.get("servico_id"))


def total_centimos_agendamento(agendamento):
    """Total de uma marcação em cêntimos, ou None quando o preço está por
    confirmar (serviço sem preço definido). Callers de apresentação devem
    tratar None como "a confirmar" e NUNCA como 0."""
    ag = agendamento or {}
    linhas = linhas_carrinho_agendamento(ag)
    if linhas:
        return sum(int(l.get("preco", 0)) * int(l.get("quantidade", 1) or 1) for l in linhas)
    if ag.get("preco_cents") is not None:
        return int(ag["preco_cents"])
    preco = ag.get("preco")
    if preco not in (None, ""):
        return int(round(float(preco) * 100))
    return None if preco_por_confirmar_agendamento(ag) else 0


def atualizar_estado_agendamento(id_agendamento, estado, bloqueia_horario=None):
    """Muda o estado de uma marcação (valor canónico — ver estados.py).
    `bloqueia_horario` é OPCIONAL e, quando indicado, é gravado na mesma
    instrução, para o estado e a ocupação do horário nunca ficarem em
    desacordo.

    Regra por omissão: `no_show` liberta o horário (o cliente não veio);
    `completed`/`confirmed`/`pending` mantêm-no; `cancelled` tem caminho
    próprio (marcar_agendamento_cancelado — a decisão é do negócio)."""
    canonico = estados.normalizar(estado)
    if bloqueia_horario is None and canonico == estados.NO_SHOW:
        bloqueia_horario = 0
    with obter_bd() as conn:
        prev = conn.execute(
            "SELECT estado, tenant_id, customer_id, servico, servico_id, data, hora "
            "FROM agendamentos WHERE id = ?", (id_agendamento,)).fetchone()
        if bloqueia_horario is None:
            conn.execute("UPDATE agendamentos SET estado = ? WHERE id = ?", (canonico, id_agendamento))
        else:
            conn.execute("UPDATE agendamentos SET estado = ?, bloqueia_horario = ? WHERE id = ?",
                         (canonico, int(bool(bloqueia_horario)), id_agendamento))
        if canonico == estados.COMPLETED:
            conn.execute("UPDATE agendamentos SET op_status = 'done', "
                         "completed_at = COALESCE(completed_at, ?) WHERE id = ?",
                         (tempo.iso_utc(), id_agendamento))
        # OUTBOX — evento de mudança de estado (para automações: review, etc.)
        if prev and canonico in (estados.COMPLETED, estados.NO_SHOW) \
                and chave_estado(prev[0]) != canonico:
            tipo = "booking.completed" if canonico == estados.COMPLETED else "booking.no_show"
            bd.registar_evento(conn, tipo, "appointment", id_agendamento,
                               {"servico": prev[3], "servico_id": prev[4], "data": prev[5],
                                "hora": prev[6], "customer_id": prev[2]},
                               dedupe_key=f"{tipo}:{id_agendamento}", tenant_id=prev[1] or 1)
            if prev[2]:
                bd.recalcular_customer(prev[2], conn=conn)


# ---------------------------------------------------------------------------
# Calendário do painel — conversão dos dados guardados em datas/horas reais
# ---------------------------------------------------------------------------
# A base de dados guarda os valores tal como foram apresentados ao cliente
# ("02.09.2026 (qua)", "🕝 14:30", "1h30", "aproximadamente 1h", "1 dia"),
# porque é isso que o WhatsApp mostra. Estas funções — puras e testáveis —
# são o único sítio onde esses textos são convertidos para o calendário.
# Registos antigos ou inválidos devolvem None em vez de rebentar: quem chama
# conta-os e mostra um aviso discreto, mantendo a linha na tabela normal.
# ---------------------------------------------------------------------------
CALENDARIO_HORA_INICIO = 8      # 08:00 — início da grelha horária
CALENDARIO_HORA_FIM = 19        # 19:00 — fim da grelha horária
CALENDARIO_INTERVALO_MIN = 30   # intervalos de 30 minutos
DURACAO_DIA_INTEIRO_MIN = (CALENDARIO_HORA_FIM - CALENDARIO_HORA_INICIO) * 60

# ---------------------------------------------------------------------------
# Cor de cada SERVIÇO no calendário. A verdade é a coluna `cor` da tabela
# `servicos` (fonte única). Os nomes LEGADOS (Spotless) ficam aqui só para o
# calendário não perder a cor das marcações antigas já gravadas.
# A COR identifica o serviço; o ESTADO é sempre comunicado à parte, por texto.
# ---------------------------------------------------------------------------
CORES_SERVICOS_LEGADAS = {
    "Interior": "#3878e8", "Exterior": "#20a4b8", "Interior + Exterior": "#6f5ae0",
    "Polimento": "#e8963c", "Proteção cerâmica": "#2ea05a", "Polimento de faróis": "#d4c23a",
    "Wrap total": "#d1478f", "Wrap parcial": "#a45cc4",
}
COR_SERVICO_OMISSAO = catalogo.COR_OMISSAO


def _cores_servicos_atuais():
    """{nome_pt: cor} a partir da tabela de serviços (fonte única)."""
    return {s["nome_pt"]: (s.get("cor") or COR_SERVICO_OMISSAO)
            for s in bd.listar_servicos(incluir_inativos=True)}


def cor_do_servico(servico_pt):
    """Cor estável de um serviço pelo nome canónico (português)."""
    nome = (servico_pt or "").strip()
    return _cores_servicos_atuais().get(nome) or CORES_SERVICOS_LEGADAS.get(nome, COR_SERVICO_OMISSAO)


def cores_servicos_legenda():
    """Mapa nome -> cor para a legenda "Cores dos serviços" do painel: só os
    serviços ATIVOS + a entrada de reserva para tudo o resto."""
    return {**{s["nome_pt"]: (s.get("cor") or COR_SERVICO_OMISSAO)
               for s in bd.listar_servicos()},
            "Outro serviço": COR_SERVICO_OMISSAO}


# Compat: código/legado que ainda importa CORES_SERVICOS como dict.
CORES_SERVICOS = CORES_SERVICOS_LEGADAS


# Estados de uma marcação tal como aparecem no calendário (chave CANÓNICA ->
# rótulo em português para a equipa). Ver estados.py.
ESTADO_CALENDARIO = dict(estados.ROTULO_PT)


def chave_estado(estado):
    """Estado (EN/PT/acentos) -> valor CANÓNICO. Mesma regra do chaveEstado()
    do painel. Ver estados.normalizar()."""
    return estados.normalizar(estado)


# ---------------------------------------------------------------------------
# DISPONIBILIDADE REAL — o estado da marcação e a ocupação do horário são
# coisas diferentes (ver a coluna bloqueia_horario):
#   • confirmed / pending / completed ........... bloqueia
#   • cancelled com bloqueia_horario = 1 ........ bloqueia
#   • cancelled com bloqueia_horario = 0 ........ NÃO bloqueia
#   • no_show (sempre no passado) ............... NÃO bloqueia agenda futura
# Um REAGENDAMENTO altera a MESMA marcação (continua confirmed) — não há
# estado "reagendado".
# Esta é a ÚNICA função que decide se um registo ocupa um horário.
# ---------------------------------------------------------------------------
ESTADOS_QUE_BLOQUEIAM_SEMPRE = estados.BLOQUEIAM_SEMPRE


def agendamento_bloqueia_horario(agendamento):
    ag = agendamento or {}
    return estados.bloqueia_horario(ag.get("estado") or estados.CONFIRMED,
                                    ag.get("bloqueia_horario"))


def horario_livre_de_uma_marcacao(agendamento):
    """True quando o registo existe mas o horário está livre — cancelada e
    libertada. Usado para a distinguir visualmente de uma cancelada que
    continua a ocupar o horário."""
    return chave_estado((agendamento or {}).get("estado")) == estados.CANCELLED \
        and not agendamento_bloqueia_horario(agendamento)


# data_iso_de_texto / hora_hhmm_de_texto / duracao_para_minutos vivem agora em
# `parsing.py` (importadas no topo). São a interpretação de datas/horas/durações
# LEGADAS gravadas como texto de apresentação; as marcações novas já gravam
# colunas estruturadas.


def evento_calendario(agendamento, pedido=None):
    """Transforma uma marcação num evento de calendário, ou None quando a
    data, a hora OU a duração não forem interpretáveis — o calendário nunca
    inventa um horário nem uma duração. Quem chama conta estes casos e
    mostra um aviso; a marcação continua visível na tabela normal.

    (`pedido` é um parâmetro legado, sempre None — o dossiê de orçamento
    Spotless foi removido.)"""
    data_iso = data_iso_de_texto(agendamento.get("data"))
    hora = hora_hhmm_de_texto(agendamento.get("hora"))
    if not data_iso or not hora:
        return None

    # recuperar_duracao() ainda recupera a duração de marcações antigas cujo
    # serviço esteja no catálogo; se mesmo assim não der, o evento é rejeitado.
    duracao_texto = recuperar_duracao(agendamento.get("servico"), agendamento.get("duracao"))
    minutos, dia_inteiro = duracao_para_minutos(duracao_texto)
    if minutos is None:
        return None

    inicio = datetime.fromisoformat(f"{data_iso}T{hora}:00")
    if dia_inteiro:
        inicio = inicio.replace(hour=CALENDARIO_HORA_INICIO, minute=0)
    fim = inicio + timedelta(minutes=minutos)

    evento = {
        "id": agendamento["id"],
        "inicio": inicio.isoformat(timespec="minutes"),
        "fim": fim.isoformat(timespec="minutes"),
        "dia": data_iso,
        "dia_inteiro": dia_inteiro,
        "duracao_minutos": minutos,
        "estado": agendamento.get("estado") or estados.CONFIRMED,
        "estado_chave": chave_estado(agendamento.get("estado") or estados.CONFIRMED),
        # A cor diz o SERVIÇO; estes dois dizem, por texto, o que a cor nunca
        # diz: se o registo ainda ocupa o horário ou se este já está livre.
        "bloqueia_horario": agendamento_bloqueia_horario(agendamento),
        "horario_livre": not agendamento_bloqueia_horario(agendamento),
        "nome": agendamento.get("nome"),
        "primeiro_nome": primeiro_nome(agendamento.get("nome")) or "",
        "telefone": agendamento.get("telefone"),
        "customer_id": agendamento.get("customer_id"),
        "op_status": agendamento.get("op_status") or "scheduled",
        "servico": agendamento.get("servico"),
        "servico_id": agendamento.get("servico_id"),
        "extra": agendamento.get("extra"),
        "data": agendamento.get("data"),
        "hora": agendamento.get("hora"),
        "hora_hhmm": hora,
        "duracao": duracao_texto,
        "preco": agendamento.get("preco"),
        "cor": cor_do_servico(agendamento.get("servico")),
        "total_centimos": total_centimos_agendamento(agendamento),
        "preco_por_confirmar": preco_por_confirmar_agendamento(agendamento),
        "carrinho": linhas_carrinho_agendamento(agendamento),
        "criado_em": agendamento.get("criado_em"),
        "pedido": None,
    }
    return evento


def eventos_calendario(inicio_iso=None, fim_iso=None):
    """Eventos do calendário no intervalo pedido (inclusive), mais a
    contagem de marcações que não foi possível converter. O filtro é feito
    pelo DIA já convertido, porque a coluna `data` guarda texto e não uma
    data comparável em SQL."""
    eventos, invalidos = [], 0
    for ag in listar_agendamentos():
        evento = evento_calendario(ag)
        if not evento:
            invalidos += 1
            continue
        if inicio_iso and evento["dia"] < inicio_iso:
            continue
        if fim_iso and evento["dia"] > fim_iso:
            continue
        eventos.append(evento)
    eventos.sort(key=lambda e: (e["inicio"], e["id"]))
    return eventos, invalidos


# ---------------------------------------------------------------------------
# Orçamentos (criados/enviados através do painel) — estrutura própria,
# associada ao ID do pedido (nunca reaproveita colunas de pedidos_orcamento).
# Os preços são sempre inteiros em CÊNTIMOS, tal como no carrinho. O total é
# SEMPRE recalculado a partir das linhas atuais (nunca somado sobre um valor
# antigo). Cada versão enviada ao cliente fica preservada (ver
# obter_ou_criar_rascunho_orcamento) — editar um orçamento já enviado cria
# sempre uma nova versão em rascunho, nunca reescreve a que foi enviada.
# ---------------------------------------------------------------------------
CAMPOS_ORCAMENTO = ["id", "pedido_id", "versao", "estado", "desconto_centimos", "observacoes",
                     "validade_dias", "criado_em", "atualizado_em", "enviado_em", "respondido_em"]
CAMPOS_LINHA_ORCAMENTO = ["id", "orcamento_id", "descricao", "quantidade", "preco_centimos", "criado_em"]


def _agora_iso():
    return tempo.iso_utc()


def registar_interacao_cliente(telefone, tenant_id=1):
    """Marca "agora" como a última mensagem recebida deste número — usado só
    para saber se ainda estamos dentro da janela de 24h de atendimento ao
    cliente da Meta (ver dentro_da_janela_24h). Tabela à parte da sessão."""
    agora = _agora_iso()
    with obter_bd() as conn:
        conn.execute(
            "INSERT INTO interacoes_cliente (tenant_id, telefone, ultima_mensagem_em) VALUES (?, ?, ?) "
            "ON CONFLICT(tenant_id, telefone) DO UPDATE SET ultima_mensagem_em = excluded.ultima_mensagem_em",
            (tenant_id, telefone, agora),
        )


def dentro_da_janela_24h(telefone, tenant_id=1):
    with obter_bd() as conn:
        linha = conn.execute(
            "SELECT ultima_mensagem_em FROM interacoes_cliente WHERE tenant_id = ? AND telefone = ?",
            (tenant_id, telefone)
        ).fetchone()
    if not linha or not linha[0]:
        return False
    ultima_dt = tempo.parse_iso(linha[0])
    if ultima_dt is None:
        return False
    return (tempo.agora_utc() - ultima_dt) < timedelta(hours=24)


# ---------------------------------------------------------------------------
# Envio de mensagens
# ---------------------------------------------------------------------------
# Envio WhatsApp: vive em messaging/whatsapp.py. Mantêm-se estes nomes em
# bot.py porque há centenas de call sites e os testes fazem monkeypatch de
# `bot.enviar`.
def enviar(payload):
    return _wa.enviar(payload)


def enviar_texto(destinatario, texto):
    return _wa.enviar_texto(destinatario, texto)


ID_PAG_SEGUINTE = "pag_seguinte_"
ID_PAG_ANTERIOR = "pag_anterior_"


def _chave_conjunto_opcoes(rows):
    """Assinatura do conjunto de opções de uma lista. Serve só para detetar
    que se mudou de passo e repor a paginação na primeira página."""
    return "|".join(r["id"] for r in rows)[:200]


def _pagina_atual_lista(destinatario, sessao, rows, total_paginas):
    """Página a mostrar. Guardada na sessão, mas reposta a 0 assim que o
    conjunto de opções muda — assim cada passo começa sempre no início, sem
    nenhum call site ter de se lembrar de limpar nada."""
    if sessao is None:
        return 0
    chave = _chave_conjunto_opcoes(rows)
    if sessao.get("_pagina_chave") != chave:
        sessao["_pagina_chave"] = chave
        sessao["_pagina_lista"] = 0
        guardar_sessao(destinatario, sessao)
        return 0
    return max(0, min(int(sessao.get("_pagina_lista", 0) or 0), total_paginas - 1))


def mudar_pagina_lista(de, idioma, sessao, pagina):
    """Handler das linhas "➡️ Mais opções" / "⬅️ Opções anteriores": guarda a
    nova página e volta a desenhar EXATAMENTE o mesmo passo (nunca avança
    nem reinicia o processo)."""
    sessao["_pagina_lista"] = max(0, pagina)
    guardar_sessao(de, sessao)
    reenviar_passo_atual(de, idioma, sessao)


def enviar_lista(destinatario, corpo, titulo_seccao, opcoes, idioma, botao="👉 Escolher", com_voltar=False,
                  com_cancelar=None, rodape=None, sessao=None, com_rapido=False):
    """`opcoes`: lista de dicts {"id","titulo","descricao"?} (titulo/descricao
    podem ser strings simples ou dicts multilingues {"pt","de","en"} — são
    sempre resolvidos aqui, para `idioma`) ou strings simples (ex.: horários,
    iguais nos 3 idiomas). `sessao`, quando passada, acrescenta sempre uma
    linha "🛒 Carrinho · CHF X" com o total atual (ver carrinho_total_centimos()).
    `com_voltar`/`com_cancelar` são independentes (por omissão `com_cancelar`
    segue `com_voltar`, como antes) — útil em listas com muitas opções, onde
    só há espaço para Voltar, mantendo CANCELAR disponível pelo rodapé (ver
    limite de 10 linhas por lista da API do WhatsApp).
    `com_rapido` acrescenta o atalho "⚡ Pedido rápido" apenas se ainda houver
    espaço dentro dessas 10 linhas.

    PAGINAÇÃO: quando as opções mais Carrinho/Voltar/Cancelar ultrapassam as
    10 linhas da API, a lista é dividida em páginas e ganha as linhas
    "➡️ Mais opções" / "⬅️ Opções anteriores" — o cliente navega sempre por
    toque, nunca por comando escrito. A página atual fica guardada na sessão
    e é reposta a zero automaticamente sempre que o conjunto de opções muda
    (ou seja, sempre que se entra noutro passo)."""
    if com_cancelar is None:
        com_cancelar = com_voltar

    def linha_de(opc, i):
        if isinstance(opc, dict):
            titulo = tx(opc["titulo"], idioma)
            row = {"id": opc.get("id", f"opt_{i}"), "title": titulo[:MAX_TITULO_LINHA]}
            desc = tx(opc.get("descricao"), idioma)
            if desc:
                row["description"] = desc[:72]
            return row
        return {"id": f"opt_{i}", "title": str(opc)[:MAX_TITULO_LINHA]}

    todas = [linha_de(opc, i) for i, opc in enumerate(opcoes)]

    # Linhas fixas que ocupam espaço no fim da lista.
    # (A linha "🛒 Carrinho · CHF X" era do fluxo Spotless com carrinho —
    # removida: a Daniela Beauty marca um serviço de cada vez, sem carrinho.)
    fixas = []
    if com_voltar:
        fixas.append({"id": ID_VOLTAR, "title": t("voltar_titulo", idioma), "description": t("voltar_desc", idioma)})
    if com_cancelar:
        fixas.append({"id": ID_CANCELAR, "title": t("cancelar_titulo", idioma),
                      "description": t("cancelar_desc", idioma)})

    # Cabe tudo? Caminho normal, sem paginação (comportamento de sempre).
    espaco_opcoes = MAX_LINHAS_LISTA - len(fixas)
    paginar = len(todas) > espaco_opcoes and espaco_opcoes > 1

    rows, indicador = [], ""
    if not paginar:
        rows = todas[:espaco_opcoes]
        if com_rapido and len(rows) + len(fixas) + 1 <= MAX_LINHAS_LISTA:
            rows.append({"id": "modo_rapido", "title": t("rapido_linha_lista", idioma)[:MAX_TITULO_LINHA]})
        rows.extend(fixas)
    else:
        # Uma linha de navegação no fim (e outra no início, a partir da 2ª
        # página) — por isso o tamanho útil da página desconta-as.
        por_pagina = max(1, espaco_opcoes - 1)
        total_paginas = (len(todas) + por_pagina - 1) // por_pagina
        pagina = _pagina_atual_lista(destinatario, sessao, todas, total_paginas)
        inicio = pagina * por_pagina
        rows = list(todas[inicio:inicio + por_pagina])
        if pagina > 0:
            rows.insert(0, {"id": f"{ID_PAG_ANTERIOR}{pagina - 1}",
                            "title": t("pag_opcoes_anteriores", idioma)[:MAX_TITULO_LINHA],
                            "description": t("pag_desc_anteriores", idioma)})
        if pagina < total_paginas - 1:
            rows.append({"id": f"{ID_PAG_SEGUINTE}{pagina + 1}",
                         "title": t("pag_mais_opcoes", idioma)[:MAX_TITULO_LINHA],
                         "description": t("pag_desc_mais", idioma)})
        rows.extend(fixas)
        indicador = t("pag_indicador", idioma, pagina=pagina + 1, total=total_paginas)

    # Rede de segurança: a API rejeita listas com mais de 10 linhas.
    rows = rows[:MAX_LINHAS_LISTA]
    if indicador:
        corpo = f"{corpo}\n\n{indicador}"

    interactive = {
        "type": "list",
        "body": {"text": corpo},
        "action": {"button": botao, "sections": [{"title": titulo_seccao, "rows": rows}]},
    }
    if rodape:
        # A API do WhatsApp rejeita footers com mais de 60 caracteres (erro 131009).
        # Truncar defensivamente para nunca provocar um envio inválido.
        interactive["footer"] = {"text": rodape[:MAX_RODAPE]}

    enviar({
        "messaging_product": "whatsapp", "to": destinatario, "type": "interactive",
        "interactive": interactive,
    })


def enviar_botoes(destinatario, corpo, botoes, idioma, rodape=None, com_voltar=False, com_cancelar=False,
                   sessao=None, titulo_seccao=None, botao_lista=None):
    """Botões de resposta rápida (máximo de 3, imposto pela API do WhatsApp).

    `com_voltar`/`com_cancelar`/`sessao` acrescentam as saídas visuais Voltar,
    Cancelar e Carrinho. Quando essas saídas já não cabem nos 3 botões, a
    mensagem é PROMOVIDA automaticamente a lista (10 linhas) — é a única
    forma de o cliente ter sempre um ⬅️ Voltar clicável sem perder nenhuma
    das opções do passo. Os IDs são exatamente os mesmos nos dois formatos,
    e o webhook trata botões e listas pela mesma cadeia, por isso a promoção
    é transparente para o resto do fluxo."""
    extras = (1 if com_voltar else 0) + (1 if com_cancelar else 0) + (1 if sessao is not None else 0)
    if len(botoes) + extras > MAX_BOTOES:
        enviar_lista(destinatario, corpo, titulo_seccao or t("mais_acoes_seccao", idioma), botoes, idioma,
                     botao=botao_lista or t("menu_botao", idioma), com_voltar=com_voltar,
                     com_cancelar=com_cancelar, rodape=rodape, sessao=sessao)
        return

    lista_botoes = list(botoes)
    if com_voltar:
        lista_botoes.append({"id": ACAO_VOLTAR, "titulo": t("botao_voltar", idioma)})
    if com_cancelar:
        lista_botoes.append({"id": ACAO_CANCELAR, "titulo": t("botao_cancelar", idioma)})

    interactive = {
        "type": "button",
        "body": {"text": corpo},
        "action": {"buttons": [
            {"type": "reply", "reply": {"id": b["id"], "title": tx(b["titulo"], idioma)[:MAX_TITULO_BOTAO]}}
            for b in lista_botoes[:MAX_BOTOES]
        ]},
    }
    if rodape:
        # A API do WhatsApp rejeita footers com mais de 60 caracteres (erro 131009).
        # Truncar defensivamente para nunca provocar um envio inválido.
        interactive["footer"] = {"text": rodape[:MAX_RODAPE]}
    enviar({
        "messaging_product": "whatsapp", "to": destinatario, "type": "interactive",
        "interactive": interactive,
    })


def primeiro_nome(nome_completo):
    if not nome_completo:
        return None
    return nome_completo.strip().split(" ")[0]


def formatar_telefone(numero):
    """+41 79 588 63 05 em vez de 41795886305."""
    n = numero.lstrip("+")
    if n.startswith("41") and len(n) == 11:
        return f"+41 {n[2:4]} {n[4:7]} {n[7:9]} {n[9:11]}"
    return f"+{n}"


def wa_me_link(telefone):
    """Ligação segura wa.me para abrir diretamente uma conversa de WhatsApp
    com este número — usada como ALTERNATIVA ao envio pelo próprio bot
    (nunca o método principal), tanto no botão "Contactar cliente" do painel
    como na resposta ao botão "💬 Contactar cliente" da notificação interna."""
    return f"https://wa.me/{telefone.lstrip('+')}"


def preco_formatado(valor, idioma="pt"):
    if not valor:
        return t("preco_a_combinar", idioma)
    return f"CHF {valor:.0f}" if float(valor).is_integer() else f"CHF {valor:.2f}"


def duracao_valida(duracao):
    """Uma duração só é válida se tiver pelo menos um dígito (ex.: "2h",
    "45min", "1 dia"). Apanha casos antigos inválidos: None, "" ou só a
    unidade sozinha (ex.: "h")."""
    if not duracao:
        return False
    texto = str(duracao).strip()
    return bool(texto) and any(c.isdigit() for c in texto)


def recuperar_duracao(servico, duracao_guardada):
    """Corrige durações antigas inválidas: se a guardada não tiver dígitos,
    recupera-a do catálogo de serviços pelo nome canónico (português).
    Marcações novas já gravam sempre uma duração válida."""
    if duracao_valida(duracao_guardada):
        return duracao_guardada
    s = bd.servico_por_nome_pt(servico)
    return catalogo.duracao_label(s["duracao_min"]) if s else "-"


def nome_servico_traduzido(servico_pt, idioma):
    """Nome do serviço no idioma do cliente (só para apresentação). Resolve
    pelo catálogo (tabela `servicos`); se não encontrar, devolve o canónico."""
    s = bd.servico_por_nome_pt(servico_pt)
    return catalogo.nome(s, idioma) if s else (servico_pt or "-")


def duracao_traduzida(servico_pt, duracao_pt, idioma):
    """Duração legível — igual nos 3 idiomas. Prefere o catálogo."""
    s = bd.servico_por_nome_pt(servico_pt)
    return catalogo.duracao_label(s["duracao_min"]) if s else (duracao_pt or "-")


# ---------------------------------------------------------------------------
# FLUXO DANIELA BEAUTY — serviço -> dia -> hora -> resumo -> confirmar
# ---------------------------------------------------------------------------
# Simples e sem carrinho: uma marcação = um serviço. Toda a informação
# (nome/duração/preço/cor) vem da FONTE ÚNICA: a tabela `servicos` (db.py).
# ---------------------------------------------------------------------------
def estado_inicial_marcacao():
    """"confirmed" (por agora) ou "pending" quando BOOKING_REQUIRES_APPROVAL
    estiver ligado. Muda o comportamento sem refactor — só a config."""
    return "pending" if config.BOOKING_REQUIRES_APPROVAL else "confirmed"


# IDs de botões/listas dos fluxos LEGADOS (Spotless: Wrap, orçamento rápido,
# carrinho, negociação de orçamento, categorias de detailing). Não existem no
# fluxo Daniela Beauty — se um cliente carregar num destes (mensagem antiga
# ainda na conversa), é levado ao menu. NUNCA apanha os IDs atuais
# (svc_*, mp_*, gerir_ag_*, reagendar_*, cancelar_ag_*, opt_*, lang_*).
_PREFIXOS_LEGADOS = (
    "modo_", "wrap_", "wv_", "cf_", "cor_", "rapido_", "carrinho_", "ver_carrinho",
    "orcamento_", "pedido_cancelar_cliente_", "cat_wrap", "cat_limpeza", "cat_estetica",
    "lp_", "ex_", "es_", "exe_", "tam_", "est_", "modo_rapido", "modo_detalhe",
    "modo_especialista", "mp_orcamento",
)


def _id_legado_spotless(id_botao):
    s = str(id_botao or "")
    return s in ("ver_carrinho", "modo_rapido", "modo_detalhe", "modo_especialista",
                 "cat_wrap", "cat_limpeza", "cat_estetica", "mp_orcamento") \
        or s.startswith(_PREFIXOS_LEGADOS)


def _servico_da_sessao(sessao):
    """Linha do serviço escolhido nesta sessão, da tabela `servicos`."""
    return bd.obter_servico((sessao or {}).get("servico_id"))


def _preco_cents_de_servico(servico):
    return servico.get("preco_cents") if servico else None


def opcoes_servicos_lista(idioma):
    """Serviços ativos como opções de lista, cada um com preço + duração na
    descrição. Preço NULL -> "Preço a confirmar" (nunca "CHF 0")."""
    opcoes = []
    for s in bd.listar_servicos():
        cents = s.get("preco_cents")
        preco = t("preco_a_confirmar", idioma) if cents is None else catalogo.formatar_cents(cents, idioma)
        dur = catalogo.duracao_label(s.get("duracao_min"), idioma)
        opcoes.append({
            "id": f"svc_{s['id']}",
            "titulo": catalogo.nome(s, idioma),
            "descricao": f"{preco} · {dur}",
        })
    return opcoes


def iniciar_escolha_servico(de, idioma, sessao):
    """Passo 1 de 3 — mostra os serviços da Daniela Beauty."""
    sessao = sessao_preservando_perfil(sessao)
    sessao["fluxo"] = "beauty"
    guardar_sessao(de, sessao)
    opcoes = opcoes_servicos_lista(idioma)
    if not opcoes:
        enviar_texto(de, t("servico_sem_ativos", idioma))
        enviar_menu_principal(de, idioma, saudacao=False, sessao=sessao)
        return
    enviar_lista(de, t("servico_corpo", idioma), t("servico_seccao", idioma), opcoes, idioma,
                 botao=t("servico_botao", idioma), com_voltar=True, rodape=t("rodape_padrao", idioma))


def escolher_servico(de, idioma, sessao, servico_id):
    """Guarda o serviço escolhido (com os valores canónicos) e avança para a
    escolha do dia."""
    servico = bd.obter_servico(servico_id)
    if not servico or not servico.get("ativo"):
        nao_entendi_com_opcoes(de, idioma, sessao)
        return
    cents = servico.get("preco_cents")
    sessao["fluxo"] = "beauty"
    sessao["servico_id"] = servico_id
    sessao["servico"] = catalogo.nome_pt(servico)          # canónico (DB/painel)
    sessao["duracao_min"] = servico["duracao_min"]
    sessao["duracao"] = catalogo.duracao_label(servico["duracao_min"])  # rótulo legível p/ conflitos legados
    sessao["preco_cents"] = cents
    sessao["preco"] = round(cents / 100, 2) if cents is not None else None
    sessao["extra"] = None
    sessao.pop("data", None)
    sessao.pop("hora", None)
    guardar_sessao(de, sessao)
    passo_data(de, idioma, sessao=sessao)


# ---------------------------------------------------------------------------
# Data / hora / resumo / confirmação
# ---------------------------------------------------------------------------
def _data_display(data_iso, idioma):
    """'2026-09-07' -> '07.09.2026 (seg)' — texto guardado em sessao["data"]."""
    d = date.fromisoformat(data_iso)
    abrev = DIAS_SEMANA.get(idioma, DIAS_SEMANA["pt"])[d.weekday()]
    return f"{d.strftime('%d.%m.%Y')} ({abrev})"


def dias_para_marcacao(sessao, idioma, n=7):
    """Próximos dias ABERTOS (business_hours + exceções + política), já em
    texto de apresentação. Substitui proximos_dias()."""
    return [_data_display(d, idioma)
            for d in bh_mod.proximos_dias_abertos(n, tenant_id=(sessao or {}).get("tenant_id", 1))]


def passo_data(de, idioma, passo_n=2, sessao=None):
    dias = dias_para_marcacao(sessao, idioma)
    if not dias:
        enviar_texto(de, t("hora_sem_vagas", idioma))
        enviar_menu_principal(de, idioma, saudacao=False, sessao=sessao)
        return
    enviar_lista(de, t("data_corpo", idioma, n=passo_n), t("data_seccao", idioma), dias, idioma,
                 botao=t("data_botao", idioma), com_voltar=True, rodape=t("rodape_padrao", idioma), sessao=sessao)


def passo_hora(de, idioma, passo_n=3, sessao=None):
    """Mostra só os horários REALMENTE livres na data escolhida — via o motor
    de disponibilidade (scheduling.availability.slots): horário de
    funcionamento + duração + buffers + marcações + reservas temporárias +
    antecedência. Um horário libertado reaparece de imediato, sem cache."""
    livres = horarios_livres_para_sessao(sessao, telefone=de)
    if not livres:
        enviar_texto(de, t("hora_sem_vagas", idioma))
        passo_data(de, idioma, sessao=sessao)
        return
    enviar_lista(de, t("hora_corpo", idioma, n=passo_n), t("hora_seccao", idioma), livres, idioma,
                 botao=t("hora_botao", idioma), com_voltar=True, rodape=t("rodape_padrao", idioma), sessao=sessao)


def calcular_preco_duracao(sessao):
    """Devolve (preco_chf_ou_None, duracao_pt, servico_pt, extra_pt).

    Fluxo Daniela Beauty: tudo vem da tabela `servicos`. Preço a confirmar ->
    preco = None (nunca 0). Extra é sempre None (não há extras).

    Fluxos LEGADOS (cat_limpeza / cat_estetica): comportamento inalterado,
    para as sessões antigas ainda a decorrer não partirem."""
    sessao = sessao or {}
    if sessao.get("servico_id"):
        s = bd.obter_servico(sessao["servico_id"]) or {}
        cents = s.get("preco_cents")
        preco = round(cents / 100, 2) if cents is not None else None
        return (preco, catalogo.duracao_label(s.get("duracao_min")),
                catalogo.nome_pt(s) if s else sessao.get("servico"), None)
    return None, None, None, None


def _linha_preco_resumo(idioma, servico):
    """Linha de preço para o resumo/confirmação. Preço a confirmar -> texto
    próprio, NUNCA "CHF 0"."""
    cents = _preco_cents_de_servico(servico)
    if cents is None:
        return t("resumo_total_a_confirmar", idioma)
    return t("resumo_total", idioma, total=catalogo.formatar_cents(cents, idioma))


def passo_resumo(de, idioma, sessao):
    """Passo final antes de confirmar. Um serviço, sem carrinho."""
    servico = bd.obter_servico(sessao.get("servico_id")) or {}
    cents = servico.get("preco_cents")
    # valores canónicos gravados na sessão/DB
    sessao["servico"] = catalogo.nome_pt(servico)
    sessao["duracao_min"] = servico.get("duracao_min")
    sessao["duracao"] = catalogo.duracao_label(servico.get("duracao_min"))
    sessao["preco_cents"] = cents
    sessao["preco"] = round(cents / 100, 2) if cents is not None else None
    sessao["extra"] = None
    guardar_sessao(de, sessao)

    nome = primeiro_nome(sessao.get("nome"))
    titulo = t("resumo_titulo", idioma) + (f", {nome}" if nome else "")
    linhas = [titulo, ""]
    linhas.append(f"✨ {catalogo.nome(servico, idioma)}")
    linhas.append(t("resumo_data", idioma, data=sessao["data"]))
    linhas.append(t("resumo_hora", idioma, hora=sessao["hora"]))
    linhas.append(t("resumo_duracao", idioma, duracao=catalogo.duracao_label(servico.get("duracao_min"), idioma)))
    linhas.append(_linha_preco_resumo(idioma, servico))
    linhas.append("\n" + t("resumo_pergunta", idioma))

    enviar_botoes(de, "\n".join(linhas), [
        {"id": "confirmar", "titulo": t("botao_confirmar", idioma)},
        {"id": "alterar", "titulo": t("botao_alterar", idioma)},
        {"id": ID_CANCELAR, "titulo": t("botao_cancelar", idioma)},
    ], idioma, rodape=t("rodape_padrao", idioma), com_voltar=True,
        titulo_seccao=t("resumo_seccao", idioma), botao_lista=t("menu_botao", idioma))


def mensagem_confirmacao_final(sessao, idioma):
    nome = primeiro_nome(sessao.get("nome"))
    saudacao = t("obrigado_nome", idioma, nome=nome) if nome else t("obrigado", idioma)
    hora_curta = sessao["hora"].split(" ")[-1] if " " in sessao["hora"] else sessao["hora"]
    servico = bd.obter_servico(sessao.get("servico_id")) or {}

    linhas = [t("confirmado_titulo", idioma, saudacao=saudacao), ""]
    linhas.append(f"✨ {catalogo.nome(servico, idioma)}")
    linhas.append(t("confirmado_data_hora", idioma, data=sessao["data"], hora=hora_curta))
    linhas.append(t("confirmado_duracao", idioma,
                    duracao=catalogo.duracao_label(servico.get("duracao_min"), idioma)))
    if MORADA_OFICINA:
        linhas.append(f"📍 {MORADA_OFICINA}")
    linhas.append(_linha_preco_resumo(idioma, servico))
    if _preco_cents_de_servico(servico) is None:
        linhas.append(t("confirmado_preco_a_confirmar", idioma))
    linhas.append("")
    linhas.append(t("confirmado_instrucao", idioma))
    return "\n".join(linhas)


# A notificação privada de criação foi UNIFICADA em
# notifications/business.py::render_evento (formato único de todos os eventos)
# + bot._notificar_criacao_marcacao (envio com a lista de ações da equipa).


# ---------------------------------------------------------------------------
# Ações internas da equipa sobre uma MARCAÇÃO confirmada
# ---------------------------------------------------------------------------
# Apresentadas como lista interativa na notificação interna, em vez de
# comandos escritos. Os IDs levam sempre o id da marcação embutido e SÓ são
# aceites quando quem carregou é o PROVIDER_WHATSAPP (ver
# numero_e_da_equipa/processar_acao_equipa_marcacao) — um cliente nunca
# consegue executá-las, mesmo que envie o ID à mão. São processadas à entrada
# de receber_mensagem, antes de qualquer tratamento de sessão, para a equipa
# nunca receber o menu normal do bot ao carregar numa ação.
# ---------------------------------------------------------------------------
PREFIXOS_ACAO_EQUIPA = (
    "equipa_ag_contactar_", "equipa_ag_reagendar_", "equipa_ag_cancelar_", "equipa_ag_concluir_",
    "equipa_ag_cancelar_sim_", "equipa_ag_cancelar_nao_",
    "equipa_ag_concluir_sim_", "equipa_ag_concluir_nao_",
)


def _so_digitos(numero):
    return "".join(c for c in str(numero or "") if c.isdigit())


def numero_e_da_equipa(numero):
    """Só o número configurado em PROVIDER_WHATSAPP é a equipa. Compara
    apenas os dígitos, para "+41 79..." e "4179..." serem o mesmo número.
    Sem PROVIDER_WHATSAPP configurado, ninguém é equipa (falha fechado)."""
    if not PROVIDER_WHATSAPP:
        return False
    return _so_digitos(numero) == _so_digitos(PROVIDER_WHATSAPP)


def opcoes_acoes_equipa_marcacao(id_agendamento):
    return [
        {"id": f"equipa_ag_contactar_{id_agendamento}", "titulo": "💬 Contactar cliente"},
        {"id": f"equipa_ag_reagendar_{id_agendamento}", "titulo": "📅 Reagendar"},
        {"id": f"equipa_ag_cancelar_{id_agendamento}", "titulo": "❌ Cancelar marcação"},
        {"id": f"equipa_ag_concluir_{id_agendamento}", "titulo": "✅ Marcar concluído"},
    ]


def enviar_notificacao_interna_marcacao(id_agendamento, texto_provider):
    """Notificação interna de uma marcação confirmada: o texto (sempre em
    português) mais a lista interativa com as quatro ações da equipa."""
    if not PROVIDER_WHATSAPP or not id_agendamento:
        return
    enviar_lista(PROVIDER_WHATSAPP, texto_provider, "Ações da marcação",
                 opcoes_acoes_equipa_marcacao(id_agendamento), "pt", botao="⚙️ Ações")


def _responder_equipa(texto):
    if PROVIDER_WHATSAPP:
        enviar_texto(PROVIDER_WHATSAPP, texto)


def idioma_do_cliente(telefone):
    idioma = carregar_sessao(telefone).get("idioma")
    return idioma if idioma in IDIOMAS_VALIDOS else "pt"


def _avisar_cliente_marcacao_cancelada(agendamento):
    """Avisa o cliente do cancelamento, no idioma guardado. Devolve True só
    quando o aviso pôde MESMO seguir: fora da janela de 24h da Meta, e sem
    template aprovado para este caso, a mensagem seria recusada — o painel
    tem de saber disso para nunca dizer que o cliente foi notificado.
    (A tentativa de envio é feita na mesma, para não alterar o comportamento
    já existente do fluxo de WhatsApp.)"""
    telefone = agendamento["telefone"]
    idioma = idioma_do_cliente(telefone)
    dentro_janela = dentro_da_janela_24h(telefone)
    try:
        enviar_botoes(telefone, t("marcacao_cancelada_equipa_cliente", idioma, id=agendamento["id"]), [
            {"id": ACAO_NOVA_MARCACAO, "titulo": t("botao_nova_marcacao", idioma)},
            {"id": ACAO_MENU, "titulo": t("botao_menu_principal", idioma)},
            {"id": ACAO_HUMANO, "titulo": t("botao_falar_equipa", idioma)},
        ], idioma)
    except Exception:
        return False
    return dentro_janela


def _avisar_cliente_marcacao_reagendada(agendamento, antes, agora):
    """Mesma regra do cancelamento: devolve True só quando o aviso podia
    mesmo seguir (dentro da janela de 24h) e o envio não falhou."""
    telefone = agendamento["telefone"]
    idioma = idioma_do_cliente(telefone)
    dentro_janela = dentro_da_janela_24h(telefone)
    try:
        enviar_botoes(telefone, t("marcacao_reagendada_cliente", idioma, id=agendamento["id"],
                                  antes=antes, agora=agora), [
            {"id": ACAO_GERIR, "titulo": t("botao_gerir_marcacao", idioma)},
            {"id": ACAO_MENU, "titulo": t("botao_menu_principal", idioma)},
            {"id": ACAO_HUMANO, "titulo": t("botao_falar_equipa", idioma)},
        ], idioma)
    except Exception:
        return False
    return dentro_janela


# ---------------------------------------------------------------------------
# Cancelar / reagendar uma marcação — lógica CENTRAL, partilhada pelas ações
# internas do WhatsApp e pelas rotas do painel. As duas únicas ações de
# escrita permitidas no calendário (ver v5.5).
# ---------------------------------------------------------------------------
class EstadoInvalido(Exception):
    """A marcação já não está no estado exigido (409 no painel)."""


class HorarioOcupado(Exception):
    """Já existe outra marcação confirmada nesse intervalo (409 no painel)."""


def marcar_agendamento_cancelado(id_agendamento, libertar=None, exigir_confirmado=True):
    """Passa uma marcação a CANCELADA e grava, na MESMA instrução, se o
    horário fica livre ou continua bloqueado.

    `libertar`: True -> horário volta a ficar disponível (bloqueia_horario=0);
    False -> a marcação cancelada continua a ocupar o horário
    (bloqueia_horario=1); None -> aplica a configuração guardada no painel
    (ver libertar_horario_ao_cancelar). É este None que os cancelamentos
    iniciados pelo cliente no WhatsApp usam: a decisão é do negócio e nunca
    lhe é apresentada.

    Devolve True se o horário ficou LIVRE. A verificação do estado e a
    escrita ficam na mesma transação, para dois pedidos simultâneos não
    cancelarem a mesma marcação duas vezes."""
    if libertar is None:
        libertar = libertar_horario_ao_cancelar()
    bloqueia = 0 if libertar else 1
    with obter_bd() as conn:
        conn.execute("BEGIN IMMEDIATE")
        linha = conn.execute(
            "SELECT estado, tenant_id, customer_id, servico, servico_id, data, hora, preco_cents "
            "FROM agendamentos WHERE id = ?", (id_agendamento,)).fetchone()
        if not linha:
            raise LookupError("Marcação não encontrada.")
        if exigir_confirmado and chave_estado(linha[0]) not in estados.GERIVEIS_PELO_CLIENTE:
            raise EstadoInvalido(linha[0])
        # O registo NUNCA é apagado: fica no histórico como cancelled, só a
        # ocupação do horário é que muda.
        conn.execute("UPDATE agendamentos SET estado = ?, bloqueia_horario = ? WHERE id = ?",
                     (estados.CANCELLED, bloqueia, id_agendamento))
        tenant_id = linha[1] or 1
        bd.registar_evento(conn, "booking.cancelled", "appointment", id_agendamento,
                           {"servico": linha[3], "servico_id": linha[4], "data": linha[5],
                            "hora": linha[6], "preco_cents": linha[7],
                            "horario_libertado": bool(libertar), "customer_id": linha[2]},
                           dedupe_key=f"booking.cancelled:{id_agendamento}", tenant_id=tenant_id)
        if linha[2]:
            bd.recalcular_customer(linha[2], conn=conn)
    return bool(libertar)


def cancelar_agendamento(id_agendamento, libertar=None, avisar_cliente=True):
    """Cancela uma marcação CONFIRMADA e tenta avisar o cliente. Devolve
    (agendamento_atualizado, cliente_notificado, horario_libertado). Levanta
    EstadoInvalido se já estiver cancelada, concluída ou reagendada — nunca
    cancela duas vezes."""
    libertado = marcar_agendamento_cancelado(id_agendamento, libertar)
    agendamento = obter_agendamento(id_agendamento)
    notificado = _avisar_cliente_marcacao_cancelada(agendamento) if avisar_cliente else False
    return agendamento, notificado, libertado


def _intervalo_agendamento(agendamento, data_iso=None, hora=None):
    """(início, fim) em datetime de uma marcação, opcionalmente já com a
    data/hora NOVAS.

    Prefere sempre as colunas ESTRUTURADAS (`data_iso`, `hora_hhmm`,
    `duracao_min`); só cai para o texto legado quando essas faltam (marcações
    antigas)."""
    ag = agendamento or {}
    dia = data_iso or ag.get("data_iso") or data_iso_de_texto(ag.get("data"))
    hhmm = hora or ag.get("hora_hhmm") or hora_hhmm_de_texto(ag.get("hora"))
    if not dia or not hhmm:
        return None, None
    minutos = ag.get("duracao_min")
    dia_inteiro = False
    if minutos is None:
        minutos, dia_inteiro = duracao_para_minutos(
            recuperar_duracao(ag.get("servico"), ag.get("duracao")))
    if minutos is None:
        return None, None
    try:
        inicio = datetime.fromisoformat(f"{dia}T{hhmm}:00")
    except ValueError:
        return None, None
    if dia_inteiro:
        inicio = inicio.replace(hour=CALENDARIO_HORA_INICIO, minute=0)
    return inicio, inicio + timedelta(minutes=int(minutos))


def _intervalo_solto(servico, duracao, data_iso, hora):
    """(início, fim) de um horário que ainda NÃO é uma marcação — usado
    quando se está a verificar se um slot está livre antes de gravar."""
    return _intervalo_agendamento({"servico": servico, "duracao": duracao}, data_iso, hora)


def conflitos_no_intervalo(agendamentos, data_iso, hora, servico, duracao, ignorar_id=None):
    """Dos `agendamentos` dados, os que OCUPAM MESMO o intervalo pedido.

    Um registo só entra aqui se agendamento_bloqueia_horario() disser que
    ocupa o horário — uma marcação cancelada e libertada é ignorada, uma
    marcação cancelada que continua a bloquear entra, tal como uma
    confirmada ou concluída. A sobreposição conta com a duração dos dois
    lados, não apenas com a hora de início."""
    novo_inicio, novo_fim = _intervalo_solto(servico, duracao, data_iso, hora)
    if not novo_inicio:
        # Duração desconhecida: NÃO se inventa nenhuma (essa continua a ser a
        # regra do calendário). Mas para a DISPONIBILIDADE não se pode dar o
        # horário como livre só por isso — verifica-se o instante de início,
        # que é o mínimo indiscutível: se já houver algo a decorrer nesse
        # momento, o horário está ocupado.
        if not (data_iso and hora):
            return []
        try:
            novo_inicio = datetime.fromisoformat(f"{data_iso}T{hora}:00")
        except ValueError:
            return []
        novo_fim = novo_inicio
    conflitos = []
    for outro in agendamentos:
        if ignorar_id is not None and outro.get("id") == ignorar_id:
            continue
        if not agendamento_bloqueia_horario(outro):
            continue
        dia_outro = outro.get("data_iso") or data_iso_de_texto(outro.get("data"))
        if dia_outro != data_iso:
            continue
        inicio, fim = _intervalo_agendamento(outro)
        if not inicio:
            continue
        sobrepoe = (inicio <= novo_inicio < fim) if novo_inicio == novo_fim \
            else (novo_inicio < fim and inicio < novo_fim)
        if sobrepoe:
            conflitos.append(outro)
    return conflitos


def conflitos_de_horario(id_agendamento, data_iso, hora):
    """Marcações OU reservas temporárias que ocupam o horário para onde se
    quer mover a marcação `id_agendamento` (a própria é sempre ignorada).
    Inclui as retenções para o painel não colidir com um horário que um
    cliente está a confirmar no WhatsApp nesse instante."""
    alvo = obter_agendamento(id_agendamento)
    if not alvo:
        return []
    ocup = listar_agendamentos() + horarios_retidos(excluir_telefone=alvo.get("telefone"))
    return conflitos_no_intervalo(
        ocup, data_iso, hora,
        alvo.get("servico"), alvo.get("duracao"), ignorar_id=id_agendamento)


# ---------------------------------------------------------------------------
# RESERVAS TEMPORÁRIAS — o horário fica retido assim que é ESCOLHIDO
# ---------------------------------------------------------------------------
# Uma marcação confirmada bloqueia o horário para sempre (ver
# agendamento_bloqueia_horario). Mas entre o momento em que um cliente escolhe
# a hora e o momento em que confirma passam-se minutos, e nesse intervalo o
# mesmo horário não pode continuar a ser oferecido a outra pessoa. É para isso
# que serve esta retenção: dura poucos minutos, expira sozinha e desaparece
# assim que o cliente confirma, cancela ou volta atrás.
# ---------------------------------------------------------------------------
RESERVA_TEMPORARIA_MINUTOS = 15


def _limpar_reservas_expiradas(conn):
    conn.execute("DELETE FROM reservas_temporarias WHERE expira_em <= ?",
                 (tempo.iso_utc(),))


def reter_horario(telefone, sessao, tenant_id=1):
    """Retém, em nome deste número, o horário que ele acabou de escolher.
    Substitui qualquer retenção anterior do mesmo número — um cliente só
    configura uma marcação de cada vez. Identidade da linha: (tenant_id,
    telefone) — ver migração 15."""
    data, hora = sessao.get("data"), sessao.get("hora")
    if not data or not hora:
        return False
    _, duracao_pt, servico_pt, _ = calcular_preco_duracao(sessao)
    agora = tempo.agora_utc()
    with obter_bd() as conn:
        _limpar_reservas_expiradas(conn)
        conn.execute(
            "INSERT INTO reservas_temporarias "
            "(tenant_id, telefone, data, hora, servico, duracao, criado_em, expira_em) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(tenant_id, telefone) DO UPDATE SET "
            "data = excluded.data, hora = excluded.hora, servico = excluded.servico, "
            "duracao = excluded.duracao, criado_em = excluded.criado_em, expira_em = excluded.expira_em",
            (tenant_id, telefone, data, hora, sessao.get("servico") or servico_pt,
             sessao.get("duracao") or duracao_pt, agora.isoformat(),
             (agora + timedelta(minutes=RESERVA_TEMPORARIA_MINUTOS)).isoformat()))
    return True


def libertar_horario_retido(telefone, tenant_id=1):
    """Devolve o horário ao mercado: chamado ao confirmar (aí passa a ser uma
    marcação a sério), ao cancelar, ao voltar atrás e ao reiniciar a sessão."""
    with obter_bd() as conn:
        _limpar_reservas_expiradas(conn)
        conn.execute("DELETE FROM reservas_temporarias WHERE tenant_id = ? AND telefone = ?",
                     (tenant_id, telefone))


def horarios_retidos(excluir_telefone=None, conn=None, tenant_id=1):
    """Retenções ainda válidas, no mesmo formato de uma marcação, para a
    verificação de conflitos as tratar exatamente como qualquer outra
    ocupação. A retenção do próprio cliente é sempre ignorada — senão ele
    ficava impedido de confirmar o horário que escolheu."""
    def _ler(c):
        _limpar_reservas_expiradas(c)
        return c.execute(
            "SELECT telefone, data, hora, servico, duracao FROM reservas_temporarias "
            "WHERE tenant_id = ? AND expira_em > ?", (tenant_id, tempo.iso_utc())).fetchall()

    if conn is not None:
        linhas = _ler(conn)
    else:
        with obter_bd() as ligacao:
            linhas = _ler(ligacao)
    return [{"id": None, "telefone": tel, "data": data, "hora": hora, "servico": servico,
             "duracao": duracao, "estado": estados.CONFIRMED, "bloqueia_horario": 1,
             "retencao": True}
            for (tel, data, hora, servico, duracao) in linhas if tel != excluir_telefone]


def ocupacoes(excluir_telefone=None, conn=None):
    """Tudo o que ocupa horários: marcações gravadas + retenções em curso."""
    existentes = _agendamentos_da_conexao(conn) if conn is not None else listar_agendamentos()
    return existentes + horarios_retidos(excluir_telefone, conn)


def horarios_livres_para_sessao(sessao, telefone=None):
    """Horas livres na data escolhida — delega no motor de disponibilidade
    (scheduling.availability.slots). Um horário desaparece assim que é
    escolhido (retenção) ou marcado, e reaparece assim que é libertado."""
    sessao = sessao or {}
    data_iso = data_iso_de_texto(sessao.get("data"))
    servico_id = sessao.get("servico_id")
    if not data_iso or not servico_id:
        return []
    ignorar_id = None
    if sessao.get("fluxo") == "reagendar" and sessao.get("reagendar_id"):
        try:
            ignorar_id = int(sessao["reagendar_id"])
        except (TypeError, ValueError):
            ignorar_id = None
    return av_mod.slots(servico_id, data_iso, telefone=telefone, ignorar_id=ignorar_id,
                        tenant_id=sessao.get("tenant_id", 1))


def reagendar_agendamento(id_agendamento, data_iso, hora, origem="dashboard", avisar_cliente=True):
    """Move uma marcação ATIVA (confirmed/pending) para nova data/hora,
    preservando serviço, duração, preço, cliente e histórico — a MESMA
    marcação, só `data`/`hora` (e as colunas estruturadas) mudam. Nunca cria
    um registo novo; nunca há um estado "reagendado".

    A verificação final de conflitos corre DENTRO da mesma transação
    `BEGIN IMMEDIATE` que faz o UPDATE, contando também com as reservas
    temporárias: dois reagendamentos concorrentes para o mesmo horário nunca
    ganham os dois; se falhar, a marcação antiga fica intacta.

    Levanta EstadoInvalido, HorarioOcupado ou LookupError. Devolve
    (agendamento_atualizado, cliente_notificado)."""
    alvo = obter_agendamento(id_agendamento)
    if not alvo:
        raise LookupError("Marcação não encontrada.")
    if chave_estado(alvo.get("estado")) not in estados.GERIVEIS_PELO_CLIENTE:
        raise EstadoInvalido(alvo.get("estado"))
    if not data_iso or not hora:
        raise HorarioOcupado(f"{data_iso} {hora}")

    d = date.fromisoformat(data_iso)
    dias = DIAS_SEMANA["pt"]
    data_texto = f"{d.strftime('%d.%m.%Y')} ({dias[d.weekday()]})"
    hora_texto = f"🕘 {hora}"
    data_antiga, hora_antiga = alvo.get("data"), alvo.get("hora")

    with obter_bd() as conn:
        conn.execute("BEGIN IMMEDIATE")
        linha = conn.execute(
            "SELECT estado FROM agendamentos WHERE id = ?", (id_agendamento,)).fetchone()
        if not linha or chave_estado(linha[0]) not in estados.GERIVEIS_PELO_CLIENTE:
            raise EstadoInvalido(linha[0] if linha else "inexistente")
        # CONFLITO revalidado DENTRO da transação: marcações gravadas +
        # reservas temporárias, exceto a própria marcação e a própria retenção.
        ocup = (_agendamentos_da_conexao(conn)
                + horarios_retidos(excluir_telefone=alvo.get("telefone"), conn=conn))
        if conflitos_no_intervalo(ocup, data_iso, hora, alvo.get("servico"),
                                  alvo.get("duracao"), ignorar_id=id_agendamento):
            raise HorarioOcupado(f"{data_iso} {hora}")
        conn.execute(
            "UPDATE agendamentos SET data = ?, hora = ?, data_iso = ?, hora_hhmm = ?, "
            "bloqueia_horario = 1 WHERE id = ?",
            (data_texto, hora_texto, data_iso, hora, id_agendamento))
        cur_hist = conn.execute(
            "INSERT INTO agendamento_historico (agendamento_id, data_anterior, hora_anterior, "
            "data_nova, hora_nova, origem, alterado_em) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (id_agendamento, data_antiga, hora_antiga, data_texto, hora_texto, origem, _agora_iso()))
        historico_id = cur_hist.lastrowid
        # OUTBOX — a dedupe_key é o id da LINHA de histórico, não a data/hora de
        # destino: cada movimento é único mesmo num ciclo A->B->A (que produziria
        # a mesma chave se fosse "id:data:hora"). Estável em retries do drain.
        bd.registar_evento(
            conn, "booking.rescheduled", "appointment", id_agendamento,
            {"servico": alvo.get("servico"), "servico_id": alvo.get("servico_id"),
             "cliente": alvo.get("nome"), "customer_id": alvo.get("customer_id"),
             "historico_id": historico_id,
             "data_antiga": data_antiga, "hora_antiga": hora_antiga,
             "data_nova": data_texto, "hora_nova": hora_texto, "origem": origem},
            dedupe_key=f"booking.rescheduled:{historico_id}",
            tenant_id=alvo.get("tenant_id") or 1)
        if alvo.get("customer_id"):
            bd.recalcular_customer(alvo["customer_id"], conn=conn)

    agendamento = obter_agendamento(id_agendamento)
    notificado = False
    if avisar_cliente:
        notificado = _avisar_cliente_marcacao_reagendada(
            agendamento, f"{data_antiga} {hora_antiga}".strip(), f"{data_texto} {hora_texto}")
    return agendamento, notificado


def historico_agendamento(id_agendamento):
    campos = ["id", "agendamento_id", "data_anterior", "hora_anterior", "data_nova", "hora_nova",
              "origem", "alterado_em"]
    with obter_bd() as conn:
        linhas = conn.execute(
            "SELECT id, agendamento_id, data_anterior, hora_anterior, data_nova, hora_nova, origem, "
            "alterado_em FROM agendamento_historico WHERE agendamento_id = ? ORDER BY id ASC",
            (id_agendamento,)).fetchall()
    return [dict(zip(campos, l)) for l in linhas]


def processar_acao_equipa_marcacao(de, id_botao):
    """Trata as ações internas sobre uma marcação. Devolve True quando a ação
    foi reconhecida e tratada (para receber_mensagem terminar já ali).

    Autorização: só o PROVIDER_WHATSAPP. Se um cliente enviar um destes IDs à
    mão, a ação é simplesmente ignorada — nem é executada, nem lhe é revelado
    que existe (o fluxo normal do cliente segue como se fosse uma opção
    desconhecida).

    "Contactar cliente" e "Reagendar" NUNCA alteram o estado da marcação:
    devolvem só a ligação wa.me para a equipa combinar com o cliente
    ("Reagendar" é um atalho seguro enquanto o reagendamento avançado não
    existir — a marcação original continua confirmada). "Cancelar marcação" e
    "Marcar concluído" pedem sempre confirmação antes de mudar o estado."""
    if not id_botao.startswith(PREFIXOS_ACAO_EQUIPA):
        return False
    if not numero_e_da_equipa(de):
        return False

    # Os sufixos de confirmação (_sim_/_nao_) são verificados primeiro: os
    # prefixos genéricos "equipa_ag_cancelar_"/"equipa_ag_concluir_" também
    # lhes servem de prefixo.
    for prefixo, acao in (
        ("equipa_ag_cancelar_sim_", "cancelar_sim"), ("equipa_ag_cancelar_nao_", "cancelar_nao"),
        ("equipa_ag_concluir_sim_", "concluir_sim"), ("equipa_ag_concluir_nao_", "concluir_nao"),
        ("equipa_ag_contactar_", "contactar"), ("equipa_ag_reagendar_", "reagendar"),
        ("equipa_ag_cancelar_", "cancelar"), ("equipa_ag_concluir_", "concluir"),
    ):
        if id_botao.startswith(prefixo):
            id_txt = id_botao[len(prefixo):]
            break
    else:
        return False

    try:
        id_agendamento = int(id_txt)
    except ValueError:
        return True

    ag = obter_agendamento(id_agendamento)
    if not ag:
        _responder_equipa(f"⚠️ Marcação #{id_agendamento} não encontrada.")
        return True

    resumo = (f"#{ag['id']} — {ag.get('nome') or 'sem nome'} · {ag.get('servico') or '-'} · "
              f"{ag.get('data') or '-'} {ag.get('hora') or ''}".strip())

    if acao == "contactar":
        _responder_equipa(f"💬 Contacto direto com o cliente da marcação {resumo}\n\n{wa_me_link(ag['telefone'])}")
        return True

    if acao == "reagendar":
        # Deliberadamente NÃO muda o estado: a marcação original continua
        # confirmada até a equipa combinar a nova data com o cliente.
        _responder_equipa(f"📅 Reagendamento da marcação {resumo}\n\nA marcação continua *confirmada*. "
                          f"Combine a nova data diretamente com o cliente:\n{wa_me_link(ag['telefone'])}")
        return True

    if acao == "cancelar":
        if chave_estado(ag["estado"]) not in estados.GERIVEIS_PELO_CLIENTE:
            _responder_equipa(f"ℹ️ A marcação #{id_agendamento} já não está ativa "
                              f"(estado atual: {estados.ROTULO_PT.get(chave_estado(ag['estado']), ag['estado'])}).")
            return True
        # "✅ Confirmar cancelamento" tem 24 caracteres e a API do WhatsApp
        # corta os títulos de botão aos 20 (MAX_TITULO_BOTAO) — ficaria
        # "✅ Confirmar cancelam". O corpo da mensagem, logo acima, é que diz
        # exatamente o que está a ser confirmado.
        enviar_botoes(PROVIDER_WHATSAPP, f"Confirma o CANCELAMENTO da marcação {resumo}?", [
            {"id": f"equipa_ag_cancelar_sim_{id_agendamento}", "titulo": "✅ Confirmar"},
            {"id": f"equipa_ag_cancelar_nao_{id_agendamento}", "titulo": "↩️ Manter marcação"},
        ], "pt")
        return True

    if acao == "cancelar_nao":
        _responder_equipa(f"↩️ A marcação {resumo} foi mantida — nada foi alterado.")
        return True

    if acao == "cancelar_sim":
        # Mesma lógica central usada pelo painel (ver cancelar_agendamento).
        # Sem escolha explícita -> aplica a configuração guardada no painel
        # ("Libertar automaticamente o horário").
        try:
            _, notificado, libertado = cancelar_agendamento(id_agendamento)
        except (EstadoInvalido, LookupError):
            _est = chave_estado((obter_agendamento(id_agendamento) or {}).get("estado"))
            _responder_equipa(f"ℹ️ A marcação #{id_agendamento} já não está ativa "
                              f"(estado atual: {estados.ROTULO_PT.get(_est, _est)}).")
            return True
        _responder_equipa(f"❌ Marcação {resumo} cancelada — "
                          + ("cliente avisado." if notificado
                             else "NÃO foi possível avisar o cliente automaticamente.")
                          + ("\n🔓 Horário libertado: volta a estar disponível." if libertado
                             else "\n🔒 Horário mantido ocupado: continua a impedir novas reservas."))
        return True

    if acao == "concluir":
        if chave_estado(ag["estado"]) not in estados.GERIVEIS_PELO_CLIENTE:
            _responder_equipa(f"ℹ️ A marcação #{id_agendamento} já não está ativa "
                              f"(estado atual: {estados.ROTULO_PT.get(chave_estado(ag['estado']), ag['estado'])}).")
            return True
        # Mesma razão do cancelamento: "✅ Confirmar conclusão" tem 21
        # caracteres e seria cortado pela API aos 20.
        enviar_botoes(PROVIDER_WHATSAPP, f"Confirma que a marcação {resumo} foi CONCLUÍDA?", [
            {"id": f"equipa_ag_concluir_sim_{id_agendamento}", "titulo": "✅ Confirmar"},
            {"id": f"equipa_ag_concluir_nao_{id_agendamento}", "titulo": "↩️ Voltar"},
        ], "pt")
        return True

    if acao == "concluir_nao":
        _responder_equipa(f"↩️ Nada foi alterado na marcação {resumo}.")
        return True

    if acao == "concluir_sim":
        if chave_estado(ag["estado"]) not in (estados.CONFIRMED, estados.PENDING):
            _responder_equipa(f"ℹ️ A marcação #{id_agendamento} já não está ativa "
                              f"(estado atual: {estados.ROTULO_PT.get(chave_estado(ag['estado']), ag['estado'])}).")
            return True
        atualizar_estado_agendamento(id_agendamento, estados.COMPLETED)
        _responder_equipa(f"✅ Marcação {resumo} marcada como concluída.")
        return True

    return True


# ---------------------------------------------------------------------------
# Menu principal / orçamento genérico / gerir marcação / humano / idioma
# ---------------------------------------------------------------------------
def enviar_menu_principal(de, idioma, saudacao=True, sessao=None):
    corpo = t("menu_corpo", idioma)
    sessao_atual = sessao if sessao is not None else carregar_sessao(de)
    if saudacao:
        nome = primeiro_nome(sessao_atual.get("nome"))
        if nome:
            ola = t("saudacao_volta", idioma, nome=nome, oficina=NOME_OFICINA)
        else:
            ola = t("saudacao_novo", idioma, oficina=NOME_OFICINA)
        corpo = f"{ola}\n\n{corpo}"
    enviar_lista(de, corpo, t("menu_titulo_lista", idioma), MENU_PRINCIPAL, idioma, botao=t("menu_botao", idioma),
                 sessao=sessao_atual)


def enviar_seletor_idioma(de, idioma_atual=None):
    """Mensagem fixa nos 3 idiomas ao mesmo tempo + opções para escolher —
    não depende de nenhum idioma já escolhido, porque é isso que resolve.

    `idioma_atual` só é passado quando o cliente JÁ tem idioma e está a
    trocá-lo a meio: nesse caso a mensagem ganha um ⬅️ Voltar clicável (e
    passa a lista, para lhe caber). Na PRIMEIRA escolha de um cliente novo
    não há passo anterior, por isso continua a ser só os 3 botões."""
    if idioma_atual in IDIOMAS_VALIDOS:
        opcoes = list(BOTOES_IDIOMA) + [{"id": ACAO_VOLTAR, "titulo": t("botao_voltar", idioma_atual)}]
        enviar_lista(de, TEXTO_SELETOR_IDIOMA, t("idioma_seccao", idioma_atual), opcoes, idioma_atual,
                     botao=t("menu_botao", idioma_atual))
        return
    enviar_botoes(de, TEXTO_SELETOR_IDIOMA, BOTOES_IDIOMA, "pt")  # idioma aqui só afeta tx(), que já são strings simples


def cancelar_processo(de, idioma, sessao):
    """Cancela o processo em curso. Qualquer rascunho de pedido Wrap é
    arquivado por reiniciar_sessao(), para nunca ficar visível no painel
    como "novo" sem o cliente ter efetivamente confirmado."""
    reiniciar_sessao(de)
    enviar_texto(de, t("processo_cancelado", idioma))
    enviar_botoes(de, t("e_agora_pergunta", idioma), [
        {"id": ACAO_NOVA_MARCACAO, "titulo": t("botao_nova_marcacao", idioma)},
        {"id": ACAO_MENU, "titulo": t("botao_menu_principal", idioma)},
    ], idioma)


def iniciar_escolha_categoria(de, idioma, sessao):
    """Compat: o fluxo Daniela Beauty escolhe o serviço diretamente."""
    for chave in ("categoria", "tipo_id", "tamanho_id", "estado_id", "extra_id"):
        sessao.pop(chave, None)
    iniciar_escolha_servico(de, idioma, sessao)


def mostrar_gestao_marcacao(de, idioma, id_agendamento=None):
    """Sem `id_agendamento`, gere a marcação ativa mais recente (comportamento
    de sempre). Com um id, gere essa marcação em concreto — usado pelo botão
    "🗓️ Ver/Gerir marcação" do carrinho, quando há mais do que uma."""
    if id_agendamento is not None:
        completo = obter_agendamento(id_agendamento)
        if (not completo or completo["telefone"] != de
                or chave_estado(completo["estado"]) not in estados.GERIVEIS_PELO_CLIENTE):
            enviar_texto(de, t("carrinho_marcacao_nao_encontrada", idioma))
            return
        ag = {"id": completo["id"], "servico": completo["servico"], "data": completo["data"],
              "hora": completo["hora"], "preco": completo["preco"],
              "duracao": recuperar_duracao(completo["servico"], completo["duracao"])}
    else:
        ag = ultimo_agendamento_ativo(de)
    if not ag:
        # Sem marcação ativa: nunca "escreva MENU" — sempre botões.
        enviar_texto(de, t("gerir_sem_marcacao", idioma))
        enviar_botoes(de, t("e_agora_pergunta", idioma), [
            {"id": ACAO_NOVA_MARCACAO, "titulo": t("botao_nova_marcacao", idioma)},
            {"id": ACAO_MENU, "titulo": t("botao_menu_principal", idioma)},
            {"id": ACAO_HUMANO, "titulo": t("botao_falar_equipa", idioma)},
        ], idioma)
        return
    servico_disp = nome_servico_traduzido(ag["servico"], idioma)
    duracao_disp = duracao_traduzida(ag["servico"], ag.get("duracao", "-"), idioma)
    corpo = t("gerir_corpo", idioma, id=ag["id"], servico=servico_disp, data=ag["data"], hora=ag["hora"],
              duracao=duracao_disp, preco=preco_formatado(ag.get("preco"), idioma))
    enviar_botoes(de, corpo, [
        {"id": f"reagendar_{ag['id']}", "titulo": t("botao_reagendar", idioma)},
        {"id": f"cancelar_ag_{ag['id']}", "titulo": t("botao_cancelar_marcacao", idioma)},
        {"id": "mp_marcar", "titulo": t("botao_nova_marcacao", idioma)},
    ], idioma, rodape=t("rodape_padrao", idioma), com_voltar=True,
        titulo_seccao=t("gerir_seccao", idioma))


def mostrar_mais_acoes(de, idioma, sessao):
    """Submenu "⚙️ Mais ações" (ACAO_MAIS) — usado quando uma lista já está
    perto do limite de 10 linhas da API do WhatsApp e não sobra espaço para
    todas as saídas universais na própria lista (ver enviar_lista)."""
    opcoes = [
        {"id": ID_VOLTAR, "titulo": t("voltar_titulo", idioma)},
        {"id": "menu_principal", "titulo": t("botao_menu_principal", idioma)},
        {"id": "ver_carrinho", "titulo": t("carrinho_botao_ver", idioma)},
        {"id": ID_CANCELAR, "titulo": t("cancelar_titulo", idioma)},
    ]
    enviar_lista(de, t("mais_acoes_pergunta", idioma), t("mais_acoes_seccao", idioma), opcoes, idioma,
                 botao=t("botao_mais_acoes", idioma))


def falar_com_equipa(de, idioma, sessao):
    enviar_texto(de, t("humano_cliente", idioma))
    if PROVIDER_WHATSAPP:
        nome = sessao.get("nome") or "sem nome"
        # Ligação wa.me em vez de um comando de texto — abre diretamente a
        # conversa com o número certo (ver wa_me_link).
        enviar_texto(PROVIDER_WHATSAPP, f"💬 *Pedido de contacto direto*\n\n👤 {nome}\n"
                                         f"📱 {formatar_telefone(de)}\n\n{wa_me_link(de)}")


def mensagem_ajuda(idioma):
    linhas = [t("ajuda_header", idioma), "", t("ajuda_menu", idioma), t("ajuda_voltar", idioma),
              t("ajuda_cancelar", idioma), t("ajuda_gerir", idioma),
              t("ajuda_ajuda", idioma), t("ajuda_humano", idioma), t("ajuda_idioma", idioma)]
    return "\n".join(linhas)


def mensagem_nao_entendi(idioma):
    return t("nao_entendi", idioma)


def nao_entendi_com_opcoes(de, idioma, sessao):
    """Nunca responde só com "não percebi, escreva MENU": repete o passo
    atual com os seus próprios botões/lista (se houver um processo em
    curso) e mostra sempre as duas saídas universais — Menu principal e
    Falar com a equipa — para o cliente nunca ficar sem opção por toque."""
    enviar_texto(de, t("nao_entendi_opcoes", idioma))
    if sessao.get("categoria") or sessao.get("fluxo"):
        reenviar_passo_atual(de, idioma, sessao)
    enviar_botoes(de, t("e_agora_pergunta", idioma), [
        {"id": ACAO_MENU, "titulo": t("botao_menu_principal", idioma)},
        {"id": ACAO_HUMANO, "titulo": t("botao_falar_equipa", idioma)},
    ], idioma)


# ---------------------------------------------------------------------------
# Autenticação HTTP Basic do painel/API (nunca expor dados de clientes ou
# fotografias publicamente)
# ---------------------------------------------------------------------------
def requer_autenticacao(func):
    """Protege uma rota com autenticação HTTP Basic, usando
    DASHBOARD_USER/DASHBOARD_PASSWORD. Falha sempre fechado: se as
    credenciais não estiverem configuradas no ambiente, o acesso é
    recusado, mesmo que o pedido não traga nenhuma autenticação."""
    @wraps(func)
    def wrapper(*args, **kwargs):
        if not DASHBOARD_USER or not DASHBOARD_PASSWORD:
            return Response("Painel não configurado.", 503)
        auth = request.authorization
        if (not auth or not hmac.compare_digest(auth.username or "", DASHBOARD_USER)
                or not hmac.compare_digest(auth.password or "", DASHBOARD_PASSWORD)):
            # O valor de um header HTTP tem de ser ASCII — realm fixo e simples.
            return Response(
                "Autenticacao necessaria.", 401,
                {"WWW-Authenticate": 'Basic realm="Painel", charset="UTF-8"'},
            )
        return func(*args, **kwargs)
    return wrapper


# ---------------------------------------------------------------------------
# Webhook
# ---------------------------------------------------------------------------
@app.route("/api/agendamentos", methods=["GET"])
@requer_autenticacao
def api_agendamentos():
    return jsonify(listar_agendamentos()), 200


@app.route("/api/agendamentos/<int:id_agendamento>", methods=["GET"])
@requer_autenticacao
def api_agendamento_detalhe(id_agendamento):
    """Detalhe completo de uma marcação — usado pelo drawer do painel."""
    ag = obter_agendamento(id_agendamento)
    if not ag or ag.get("tenant_id", 1) != _TENANT:
        return jsonify(erro="Marcação não encontrada."), 404
    corpo = dict(ag)
    corpo["total_centimos"] = total_centimos_agendamento(ag)
    corpo["preco_por_confirmar"] = preco_por_confirmar_agendamento(ag)
    corpo["historico"] = historico_agendamento(id_agendamento)
    if ag.get("customer_id"):
        cust = bd.obter_customer(ag["customer_id"])
        if cust:
            corpo["cliente_resumo"] = {
                "id": cust["id"], "name": cust["name"],
                "visits_count": cust["visits_count"], "spend_cents": cust["spend_cents"],
                "no_show_count": cust["no_show_count"], "last_visit": cust["last_visit"],
            }
    with obter_bd() as c:
        fr = c.execute("SELECT id, status, invoice_number, total_cents FROM invoices "
                       "WHERE appointment_id = ? AND status <> 'cancelled' LIMIT 1",
                       (id_agendamento,)).fetchone()
    corpo["fatura"] = (dict(zip(("id", "status", "invoice_number", "total_cents"), fr))
                       if fr else None)
    return jsonify(corpo), 200


@app.route("/api/calendario", methods=["GET"])
@requer_autenticacao
def api_calendario():
    """Eventos do calendário no intervalo pedido (?inicio=&fim=, em
    YYYY-MM-DD; ambos opcionais). Protegido pela mesma autenticação HTTP
    Basic do resto do painel — nunca expõe dados de clientes publicamente."""
    def data_pedida(nome):
        valor = (request.args.get(nome) or "").strip()
        return valor if re.fullmatch(r"\d{4}-\d{2}-\d{2}", valor) else None

    inicio, fim = data_pedida("inicio"), data_pedida("fim")
    eventos, invalidos = eventos_calendario(inicio, fim)
    return jsonify(
        eventos=eventos,
        invalidos=invalidos,
        inicio=inicio,
        fim=fim,
        grelha={"hora_inicio": CALENDARIO_HORA_INICIO, "hora_fim": CALENDARIO_HORA_FIM,
                "intervalo_min": CALENDARIO_INTERVALO_MIN},
        cores_servicos=cores_servicos_legenda(),
        cor_omissao=COR_SERVICO_OMISSAO,
        estados=ESTADO_CALENDARIO,
        configuracoes=configuracoes_atuais(),
    ), 200


# ---------------------------------------------------------------------------
# Configurações do painel — leitura e gravação (mesma autenticação de tudo o
# resto). O valor vem do corpo JSON, mas NUNCA é aceite tal e qual: só é
# gravado depois de convertido para "1"/"0" aqui no servidor.
# ---------------------------------------------------------------------------
def _booleano_do_pedido(valor):
    """Aceita true/false, "1"/"0", "sim"/"nao", 1/0. Devolve None quando o
    valor não é reconhecido — quem chama responde 400 em vez de adivinhar."""
    if isinstance(valor, bool):
        return valor
    texto = str(valor).strip().lower()
    if texto in ("1", "true", "sim", "on", "yes"):
        return True
    if texto in ("0", "false", "nao", "não", "off", "no"):
        return False
    return None


@app.route("/api/configuracoes", methods=["GET", "POST"])
@requer_autenticacao
def api_configuracoes():
    if request.method == "POST":
        dados = request.get_json(force=True, silent=True) or {}
        if CONFIG_LIBERTAR_AO_CANCELAR not in dados:
            return jsonify(erro="Nada para gravar."), 400
        valor = _booleano_do_pedido(dados.get(CONFIG_LIBERTAR_AO_CANCELAR))
        if valor is None:
            return jsonify(erro="Valor inválido (esperado verdadeiro/falso)."), 400
        guardar_configuracao(CONFIG_LIBERTAR_AO_CANCELAR, "1" if valor else "0")
    return jsonify(ok=True, configuracoes=configuracoes_atuais()), 200


# ---------------------------------------------------------------------------
# Ações de escrita do calendário — só CANCELAR e REAGENDAR (ver v5.5).
# Ambas protegidas pela autenticação do painel e revalidadas no servidor: o
# frontend nunca é fonte de verdade para o estado nem para o horário.
# ---------------------------------------------------------------------------
def _resposta_evento(id_agendamento, notificado, extra=None):
    ag = obter_agendamento(id_agendamento)
    corpo = {
        "ok": True,
        "cliente_notificado": bool(notificado),
        "agendamento": ag,
        "evento": evento_calendario(ag) if ag else None,
        "historico": historico_agendamento(id_agendamento),
    }
    if extra:
        corpo.update(extra)
    return jsonify(corpo), 200


@app.route("/api/agendamentos/<int:id_agendamento>/cancelar", methods=["POST"])
@requer_autenticacao
def api_agendamento_cancelar(id_agendamento):
    """Cancela uma marcação a partir do painel. Só aceita marcações ainda
    CONFIRMADAS — um segundo pedido devolve 409, nunca cancela duas vezes.

    Corpo JSON (tudo opcional):
      libertar        -> true: o horário volta a ficar livre;
                         false: a marcação cancelada continua a ocupá-lo;
                         ausente: aplica a configuração guardada.
      guardar_padrao  -> true: passa essa escolha a configuração por omissão.

    Nada disto é aceite tal e qual: `libertar` é convertido para booleano
    aqui no servidor e a escolha só é gravada depois de validada."""
    dados = request.get_json(force=True, silent=True) or {}
    libertar = None
    if "libertar" in dados:
        libertar = _booleano_do_pedido(dados.get("libertar"))
        if libertar is None:
            return jsonify(erro="Escolha de horário inválida (esperado verdadeiro/falso)."), 400
    guardar_padrao = _booleano_do_pedido(dados.get("guardar_padrao")) or False

    try:
        _, notificado, libertado = cancelar_agendamento(id_agendamento, libertar)
    except LookupError:
        return jsonify(erro="Marcação não encontrada."), 404
    except EstadoInvalido as e:
        return jsonify(erro=f"Esta marcação já não está confirmada (estado atual: {e}).",
                       estado=str(e)), 409

    # Só depois de o cancelamento ter mesmo corrido é que a escolha passa a
    # padrão — nunca se grava uma preferência a partir de um pedido falhado.
    if guardar_padrao and libertar is not None:
        guardar_configuracao(CONFIG_LIBERTAR_AO_CANCELAR, "1" if libertar else "0")

    return _resposta_evento(id_agendamento, notificado, extra={
        "horario_libertado": bool(libertado),
        "configuracoes": configuracoes_atuais(),
    })


@app.route("/api/agendamentos/<int:id_agendamento>/estado", methods=["POST"])
@requer_autenticacao
def api_agendamento_estado(id_agendamento):
    """Marca uma marcação como CONCLUÍDA (completed) ou NÃO COMPARECEU
    (no_show) a partir do painel. Só a partir de uma marcação ativa
    (confirmed/pending). Cancelar e reagendar têm rotas próprias."""
    dados = request.get_json(force=True, silent=True) or {}
    novo = estados.normalizar(dados.get("estado"))
    if novo not in (estados.COMPLETED, estados.NO_SHOW):
        return jsonify(erro="Estado inválido (esperado 'completed' ou 'no_show')."), 400
    ag = obter_agendamento(id_agendamento)
    if not ag:
        return jsonify(erro="Marcação não encontrada."), 404
    if chave_estado(ag.get("estado")) not in (estados.CONFIRMED, estados.PENDING):
        return jsonify(erro=f"Esta marcação já não está ativa (estado atual: {ag.get('estado')}).",
                       estado=ag.get("estado")), 409
    # Regra temporal: concluir / marcar falta numa marcação AINDA no futuro é
    # absurdo (o serviço não aconteceu). Aceite só com confirmação explícita.
    inicio = tempo.combinar_local(ag.get("data_iso") or "", ag.get("hora_hhmm") or "")
    if inicio and inicio > tempo.agora_zurique() and not dados.get("confirmar"):
        return jsonify(precisa_confirmacao=True,
                       erro=f"A marcação #{id_agendamento} é no futuro "
                            f"({ag.get('data_iso')} {ag.get('hora_hhmm')}). "
                            "Confirme para a marcar como '{}'.".format(novo)), 409
    atualizar_estado_agendamento(id_agendamento, novo)
    return _resposta_evento(id_agendamento, False)


@app.route("/api/agendamentos/<int:id_agendamento>/reagendar", methods=["POST"])
@requer_autenticacao
def api_agendamento_reagendar(id_agendamento):
    """Move uma marcação confirmada para outra data/hora. A data e a hora são
    sempre revalidadas aqui, e os conflitos são verificados contando com a
    duração — o que vier do frontend nunca é aceite sem validação."""
    dados = request.get_json(force=True, silent=True) or {}
    data_iso = str(dados.get("data") or "").strip()
    hora = str(dados.get("hora") or "").strip()

    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", data_iso):
        return jsonify(erro="Data inválida (esperado YYYY-MM-DD)."), 400
    try:
        date.fromisoformat(data_iso)
    except ValueError:
        return jsonify(erro="Data inexistente no calendário."), 400
    if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", hora):
        return jsonify(erro="Hora inválida (esperado HH:MM entre 00:00 e 23:59)."), 400

    try:
        _, notificado = reagendar_agendamento(id_agendamento, data_iso, hora, origem="dashboard")
    except LookupError:
        return jsonify(erro="Marcação não encontrada."), 404
    except EstadoInvalido as e:
        return jsonify(erro=f"Esta marcação já não está confirmada (estado atual: {e}).",
                       estado=str(e)), 409
    except HorarioOcupado:
        ocupados = conflitos_de_horario(id_agendamento, data_iso, hora)
        nomes = ", ".join(f"#{o['id']} {o.get('nome') or ''}".strip() for o in ocupados)
        return jsonify(erro=f"Esse horário já está ocupado ({nomes}).", conflitos=nomes), 409
    return _resposta_evento(id_agendamento, notificado)


def _escapar_html(texto):
    return (str(texto).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;").replace("'", "&#39;"))


# ===========================================================================
# API do PAINEL OPERACIONAL (novo) — cockpit, serviços, horários, clientes.
# O painel /dashboard antigo continua a funcionar durante a migração.
# ===========================================================================
_TENANT = 1  # V1: single-tenant. resolve_tenant(request) chega na V2.


@app.route("/api/painel/hoje", methods=["GET"])
@requer_autenticacao
def api_painel_hoje():
    from operations import engine as op
    return jsonify(
        cartao=op.cartao_operacional(_TENANT),
        atencao=op.attention_items(_TENANT),
        resumo=op.resumo_hoje(_TENANT),
        agenda=[op._resumo(m, _TENANT) | {"hora": m["hhmm"]}
                for m in op._marcacoes_de_hoje(_TENANT) if m["hhmm"]],
    ), 200


@app.route("/api/agendamentos/<int:id_agendamento>/op", methods=["POST"])
@requer_autenticacao
def api_agendamento_op(id_agendamento):
    """Transição operacional: arrived / in_progress / done (Fase E3-E5)."""
    from operations import engine as op
    d = request.get_json(silent=True) or {}
    novo = d.get("op") or ""
    try:
        cartao = op.transicao_operacional(id_agendamento, novo, _TENANT,
                                          forcar=bool(d.get("confirmar")))
    except op.TransicaoAbsurda as e:
        return jsonify(precisa_confirmacao=True, erro=str(e)), 409
    except ValueError:
        return jsonify(erro="Transição inválida (arrived / in_progress / done)."), 400
    except LookupError:
        return jsonify(erro="Marcação não encontrada."), 404
    disparar_automacoes()
    return jsonify(ok=True, cartao=cartao), 200


class _EntradaInvalida(ValueError):
    """Erro de validação de input do painel -> HTTP 400 (nunca 500)."""


def _inteiro(valor, *, minimo=None, maximo=None, permite_none=False, campo="valor"):
    """Converte input do utilizador para int com limites. Levanta
    _EntradaInvalida (->400) em vez de deixar rebentar um int()/500."""
    if valor in (None, "", "null"):
        if permite_none:
            return None
        raise _EntradaInvalida(f"{campo} é obrigatório.")
    try:
        n = int(valor)
    except (TypeError, ValueError):
        raise _EntradaInvalida(f"{campo} tem de ser um número inteiro.")
    if minimo is not None and n < minimo:
        raise _EntradaInvalida(f"{campo} não pode ser inferior a {minimo}.")
    if maximo is not None and n > maximo:
        raise _EntradaInvalida(f"{campo} não pode ser superior a {maximo}.")
    return n


@app.route("/api/painel/atraso", methods=["POST"])
@requer_autenticacao
def api_painel_atraso():
    """Pré-visualização de um atraso: quem fica afetado. NÃO envia nada
    (Fase E6 — nunca avisar automaticamente sem confirmação)."""
    from operations import engine as op
    try:
        minutos = _inteiro((request.get_json(silent=True) or {}).get("minutos"),
                           minimo=1, maximo=240, campo="minutos")
    except _EntradaInvalida as e:
        return jsonify(erro=str(e)), 400
    if minutos <= 0 or minutos > 240:
        return jsonify(erro="Minutos fora do intervalo (1-240)."), 400
    return jsonify(op.marcacoes_afetadas_por_atraso(minutos, _TENANT)), 200


# --- Serviços (CRUD) -------------------------------------------------------
_ID_RE = re.compile(r"^[a-z][a-z0-9_]{1,40}$")


@app.route("/api/servicos", methods=["GET", "POST"])
@requer_autenticacao
def api_servicos():
    if request.method == "GET":
        return jsonify(bd.listar_servicos(incluir_inativos=True)), 200
    d = request.get_json(silent=True) or {}
    sid = str(d.get("id") or "").strip().lower()
    if not _ID_RE.match(sid):
        return jsonify(erro="ID inválido (minúsculas, dígitos e _; começa por letra)."), 400
    if bd.obter_servico(sid):
        return jsonify(erro="Já existe um serviço com esse ID."), 409
    if not str(d.get("nome_pt") or "").strip():
        return jsonify(erro="Nome (PT) obrigatório."), 400
    try:
        dur = _inteiro(d.get("duracao_min"), minimo=5, maximo=600, campo="Duração")
        pc = _inteiro(d.get("preco_cents"), minimo=0, permite_none=True, campo="Preço")
    except _EntradaInvalida as e:
        return jsonify(erro=str(e)), 400
    bd.criar_servico({"id": sid, "nome_pt": d["nome_pt"], "nome_de": d.get("nome_de"),
                      "nome_en": d.get("nome_en"), "duracao_min": dur, "preco_cents": pc,
                      "ativo": bool(d.get("ativo", True)), "cor": d.get("cor"),
                      "ordem": d.get("ordem", 99)})
    return jsonify(ok=True, servico=bd.obter_servico(sid)), 201


@app.route("/api/servicos/<servico_id>", methods=["PATCH"])
@requer_autenticacao
def api_servico_editar(servico_id):
    if not bd.obter_servico(servico_id):
        return jsonify(erro="Serviço não encontrado."), 404
    d = request.get_json(silent=True) or {}
    patch = {}
    for k in ("nome_pt", "nome_de", "nome_en", "cor"):
        if k in d:
            patch[k] = d[k]
    try:
        if "duracao_min" in d:
            patch["duracao_min"] = _inteiro(d["duracao_min"], minimo=5, maximo=600, campo="Duração")
        if "preco_cents" in d:
            patch["preco_cents"] = _inteiro(d["preco_cents"], minimo=0, permite_none=True, campo="Preço")
        if "rebook_days" in d:
            rd = _inteiro(d["rebook_days"], minimo=0, maximo=3650,
                          permite_none=True, campo="Dias de reagendamento")
            patch["rebook_days"] = rd or None      # 0 == desativado
        if "buffer_before_min" in d:
            patch["buffer_before_min"] = _inteiro(d["buffer_before_min"] or 0, minimo=0,
                                                  maximo=240, campo="Buffer antes")
        if "buffer_after_min" in d:
            patch["buffer_after_min"] = _inteiro(d["buffer_after_min"] or 0, minimo=0,
                                                 maximo=240, campo="Buffer depois")
    except _EntradaInvalida as e:
        return jsonify(erro=str(e)), 400
    if "ativo" in d:
        patch["ativo"] = bool(d["ativo"])
    bd.atualizar_servico(servico_id, patch)
    return jsonify(ok=True, servico=bd.obter_servico(servico_id)), 200


# --- Horários / política -------------------------------------------------
@app.route("/api/horarios", methods=["GET", "PUT"])
@requer_autenticacao
def api_horarios():
    if request.method == "GET":
        return jsonify(grelha=bh_mod.grelha_semanal(_TENANT),
                       excecoes=bh_mod.listar_excecoes(_TENANT),
                       politica=bh_mod.politica(_TENANT)), 200
    d = request.get_json(silent=True) or {}
    dias = d.get("grelha")
    if not isinstance(dias, list) or len(dias) != 7:
        return jsonify(erro="grelha tem de ter 7 dias."), 400
    hhmm = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
    for x in dias:
        if not isinstance(x, dict):
            return jsonify(erro="Cada dia da grelha tem de ser um objeto."), 400
        for campo in ("opens", "closes", "break_start", "break_end"):
            v = x.get(campo)
            if v and (not isinstance(v, str) or not hhmm.match(v)):
                return jsonify(erro=f"Hora inválida em {campo}: {v}"), 400
    bh_mod.definir_grelha(_TENANT, dias)
    return jsonify(ok=True, grelha=bh_mod.grelha_semanal(_TENANT)), 200


@app.route("/api/horarios/excecoes", methods=["POST"])
@requer_autenticacao
def api_excecao_criar():
    d = request.get_json(silent=True) or {}
    di = str(d.get("data_inicio") or "").strip()
    df = str(d.get("data_fim") or "").strip() or None
    try:
        dd_ini = date.fromisoformat(di)
        d1 = date.fromisoformat(df) if df else dd_ini
    except ValueError:
        return jsonify(erro="Data inválida (formato YYYY-MM-DD)."), 400
    if d1 < dd_ini:
        return jsonify(erro="data_fim é anterior a data_inicio."), 400
    # Fase W: marcações afetadas — informar, nunca cancelar automaticamente.
    afetadas = []
    with obter_bd() as conn:
        dd = dd_ini
        while dd <= d1:
            dmy = f"{dd.strftime('%d.%m.%Y')}"
            for (aid, nome, servico, hora) in conn.execute(
                    "SELECT id, nome, servico, hora FROM agendamentos WHERE tenant_id = ? "
                    "AND LOWER(estado) IN (" + estados.sql_lista(*estados.ATIVOS) + ") AND (data_iso = ? OR data LIKE ?)",
                    (_TENANT, dd.isoformat(), f"%{dmy}%")).fetchall():
                afetadas.append({"id": aid, "cliente": nome, "servico": servico,
                                 "data": dd.isoformat(), "hora": hora})
            dd += timedelta(days=1)
    if afetadas and not d.get("confirmar"):
        return jsonify(precisa_confirmacao=True, afetadas=afetadas), 200
    criadas = bh_mod.adicionar_excecao(
        _TENANT, di, df, closed=bool(d.get("closed", True)),
        opens=d.get("opens"), closes=d.get("closes"), reason=d.get("reason"))
    return jsonify(ok=True, datas=criadas, afetadas=afetadas), 201


@app.route("/api/horarios/excecoes/<int:excecao_id>", methods=["DELETE"])
@requer_autenticacao
def api_excecao_remover(excecao_id):
    bh_mod.remover_excecao(_TENANT, excecao_id)
    return jsonify(ok=True), 200


# --- Clientes -----------------------------------------------------------
@app.route("/api/clientes", methods=["GET"])
@requer_autenticacao
def api_clientes():
    return jsonify(bd.listar_customers(_TENANT)), 200


@app.route("/api/clientes/<int:customer_id>", methods=["GET", "PATCH"])
@requer_autenticacao
def api_cliente(customer_id):
    cust = bd.obter_customer(customer_id)
    if not cust or cust["tenant_id"] != _TENANT:
        return jsonify(erro="Cliente não encontrado."), 404
    if request.method == "PATCH":
        d = request.get_json(silent=True) or {}
        campos, vals = [], []
        if "notes_internal" in d:
            campos.append("notes_internal = ?"); vals.append(d["notes_internal"])
        if "vip" in d:
            campos.append("vip = ?"); vals.append(1 if d["vip"] else 0)
        if "tags" in d and isinstance(d["tags"], list):
            campos.append("tags = ?"); vals.append(json.dumps(d["tags"], ensure_ascii=False))
        if campos:
            campos.append("updated_at = ?"); vals.append(tempo.iso_utc()); vals.append(customer_id)
            with obter_bd() as c:
                c.execute(f"UPDATE customers SET {', '.join(campos)} WHERE id = ?", vals)
        cust = bd.obter_customer(customer_id)
    # histórico de marcações
    with obter_bd() as c:
        marc = c.execute(
            "SELECT id, servico, data, hora, data_iso, hora_hhmm, estado, preco_cents, op_status "
            "FROM agendamentos WHERE customer_id = ? ORDER BY COALESCE(data_iso,'') DESC, id DESC",
            (customer_id,)).fetchall()
    historico = [dict(zip(("id", "servico", "data", "hora", "data_iso", "hora_hhmm",
                           "estado", "preco_cents", "op_status"), m)) for m in marc]
    from billing import engine as _bi
    faturas = _bi.faturas_do_cliente(customer_id, _TENANT)
    return jsonify(cliente=cust, historico=historico, faturas=faturas), 200


# --- Faturação --------------------------------------------------------------
_FATURA_ERRO_HTTP = {"PrecoEmFalta": 409, "TransicaoInvalida": 409,
                     "FaturaNaoEncontrada": 404, "ErroFaturacao": 400}


def _faturas_engine():
    from billing import engine as _bi
    return _bi


def _resp_erro_fatura(e):
    from billing import engine as _bi
    codigo = _FATURA_ERRO_HTTP.get(type(e).__name__, 400)
    corpo = {"erro": str(e)}
    if isinstance(e, _bi.PrecoEmFalta):
        corpo["precisa_preco"] = True
    return jsonify(corpo), codigo


@app.route("/api/agendamentos/<int:id_agendamento>/fatura", methods=["POST"])
@requer_autenticacao
def api_agendamento_fatura(id_agendamento):
    """Gera (ou devolve, se já existir) a fatura desta marcação. Se a marcação
    não tem preço, o corpo tem de trazer preco_cents."""
    bi = _faturas_engine()
    d = request.get_json(silent=True) or {}
    preco = d.get("preco_cents")
    if preco is not None:
        try:
            preco = int(preco)
        except (TypeError, ValueError):
            return jsonify(erro="preco_cents tem de ser um número inteiro."), 400
    try:
        inv = bi.gerar_fatura_de_marcacao(id_agendamento, preco_cents=preco, tenant_id=_TENANT)
    except bi.ErroFaturacao as e:
        return _resp_erro_fatura(e)
    return jsonify(inv), 201


@app.route("/api/faturas", methods=["GET"])
@requer_autenticacao
def api_faturas():
    bi = _faturas_engine()
    return jsonify(bi.listar_faturas(_TENANT, status=request.args.get("estado"))), 200


@app.route("/api/faturas/<int:invoice_id>", methods=["GET", "PATCH"])
@requer_autenticacao
def api_fatura(invoice_id):
    bi = _faturas_engine()
    if request.method == "PATCH":
        try:
            inv = bi.atualizar_rascunho(invoice_id, request.get_json(silent=True) or {}, _TENANT)
        except bi.ErroFaturacao as e:
            return _resp_erro_fatura(e)
        return jsonify(inv), 200
    inv = bi.obter_fatura(invoice_id, _TENANT)
    if not inv:
        return jsonify(erro="Fatura não encontrada."), 404
    return jsonify(inv), 200


@app.route("/api/faturas/<int:invoice_id>/<accao>", methods=["POST"])
@requer_autenticacao
def api_fatura_accao(invoice_id, accao):
    bi = _faturas_engine()
    fn = {"emitir": bi.emitir_fatura, "pagar": bi.marcar_paga, "anular": bi.anular_fatura}.get(accao)
    if not fn:
        return jsonify(erro="Ação inválida (emitir / pagar / anular)."), 400
    try:
        return jsonify(fn(invoice_id, _TENANT)), 200
    except bi.ErroFaturacao as e:
        return _resp_erro_fatura(e)


@app.route("/api/definicoes/faturacao", methods=["GET", "PUT"])
@requer_autenticacao
def api_definicoes_faturacao():
    bi = _faturas_engine()
    if request.method == "PUT":
        try:
            cfg = bi.guardar_definicoes_faturacao(request.get_json(silent=True) or {}, _TENANT)
        except bi.ErroFaturacao as e:
            return jsonify(erro=str(e)), 400
        return jsonify(cfg), 200
    return jsonify(bi.definicoes_faturacao(_TENANT)), 200


@app.route("/painel", methods=["GET"])
@app.route("/painel/hoje", methods=["GET"])
@requer_autenticacao
def painel_hoje():
    return PAINEL_HOJE_HTML.replace("{{BUSINESS_NAME}}", _escapar_html(BUSINESS_NAME))


PAINEL_HOJE_HTML = r"""<!doctype html>
<html lang="pt">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{{BUSINESS_NAME}} — Hoje</title>
<style>
  :root{
    --bg:#f7f4f3; --surface:#fff; --surface-2:#faf6f7; --line:#ebe2e4;
    --ink:#241f24; --ink-2:#6b616a; --ink-3:#978c95;
    --accent:#a83d76; --accent-soft:#f5e6ef;
    --live:#1f9d55; --live-soft:#e6f3ec; --next:#b06a1c; --next-soft:#f6ecdd;
    --crit:#c1443b; --crit-soft:#f7e6e4; --info:#3f6493;
  }
  @media (prefers-color-scheme:dark){:root:not([data-theme="light"]){
    --bg:#151117; --surface:#1e1920; --surface-2:#251f27; --line:#322a35;
    --ink:#ece3ea; --ink-2:#ab9fa9; --ink-3:#7d7280;
    --accent:#d97cae; --accent-soft:#33202f;
    --live:#5fc088; --live-soft:#1e2c24; --next:#d6a04a; --next-soft:#2c2620;
    --crit:#e0645f; --crit-soft:#2e211f; --info:#8fb0d6;
  }}
  :root[data-theme="dark"]{
    --bg:#151117; --surface:#1e1920; --surface-2:#251f27; --line:#322a35;
    --ink:#ece3ea; --ink-2:#ab9fa9; --ink-3:#7d7280;
    --accent:#d97cae; --accent-soft:#33202f;
    --live:#5fc088; --live-soft:#1e2c24; --next:#d6a04a; --next-soft:#2c2620;
    --crit:#e0645f; --crit-soft:#2e211f; --info:#8fb0d6;
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--ink);
    font-family:"Segoe UI",system-ui,-apple-system,sans-serif;font-size:15px;line-height:1.5;}
  .wrap{max-width:820px;margin:0 auto;padding:18px 16px 80px;}
  header.top{display:flex;justify-content:space-between;align-items:baseline;margin:4px 0 18px;}
  header.top h1{font-size:1.05rem;margin:0;font-weight:600;letter-spacing:.2px;}
  header.top .marca{color:var(--accent);}
  header.top a{color:var(--ink-3);text-decoration:none;font-size:.82rem;}
  header.top a:hover{color:var(--accent);}
  .card{background:var(--surface);border:1px solid var(--line);border-radius:14px;padding:18px;}

  /* cockpit */
  #cockpit{margin-bottom:16px;position:relative;overflow:hidden;}
  #cockpit .badge{display:inline-flex;align-items:center;gap:7px;font-size:.72rem;font-weight:700;
    text-transform:uppercase;letter-spacing:.09em;padding:.28em .7em;border-radius:999px;}
  #cockpit.k-in_progress{border-color:color-mix(in srgb,var(--live) 45%,var(--line));}
  #cockpit.k-in_progress .badge{background:var(--live-soft);color:var(--live);}
  #cockpit.k-next .badge{background:var(--next-soft);color:var(--next);}
  #cockpit.k-done .badge{background:var(--accent-soft);color:var(--accent);}
  #cockpit .dot{width:9px;height:9px;border-radius:50%;background:currentColor;
    animation:pulse 1.8s ease-in-out infinite;}
  @keyframes pulse{0%,100%{opacity:1}50%{opacity:.35}}
  @media (prefers-reduced-motion:reduce){#cockpit .dot{animation:none}}
  #cockpit h2{font-size:1.5rem;margin:.5rem 0 .1rem;font-weight:600;letter-spacing:-.01em;}
  #cockpit .sub{color:var(--ink-2);font-size:.95rem;}
  #cockpit .timeline{margin:14px 0 4px;height:8px;background:var(--surface-2);border-radius:999px;overflow:hidden;}
  #cockpit .timeline > i{display:block;height:100%;background:var(--live);border-radius:999px;transition:width .6s;}
  #cockpit .meta{display:flex;flex-wrap:wrap;gap:6px 18px;color:var(--ink-2);font-size:.9rem;margin-top:10px;}
  #cockpit .meta b{color:var(--ink);font-weight:600;font-variant-numeric:tabular-nums;}
  #cockpit .acoes{display:flex;flex-wrap:wrap;gap:8px;margin-top:16px;}
  button{font:inherit;cursor:pointer;border-radius:9px;border:1px solid var(--line);
    background:var(--surface-2);color:var(--ink);padding:.55em .9em;font-size:.9rem;font-weight:500;}
  button:hover{border-color:var(--accent);color:var(--accent);}
  button.primary{background:var(--accent);border-color:var(--accent);color:#fff;}
  button.primary:hover{filter:brightness(1.06);color:#fff;}
  button:disabled{opacity:.45;cursor:default;}
  button:focus-visible{outline:2px solid var(--accent);outline-offset:2px;}

  h3.sec{font-size:.74rem;text-transform:uppercase;letter-spacing:.1em;color:var(--ink-3);
    margin:26px 4px 10px;font-weight:600;}
  .atencao{display:flex;flex-direction:column;gap:8px;}
  .att{display:flex;gap:12px;align-items:flex-start;background:var(--surface);border:1px solid var(--line);
    border-left:3px solid var(--ink-3);border-radius:10px;padding:12px 14px;}
  .att.n-agora{border-left-color:var(--crit);}
  .att.n-hoje{border-left-color:var(--next);}
  .att .txt{flex:1;min-width:0;}
  .att .titulo{font-weight:600;font-size:.92rem;}
  .att .detalhe{color:var(--ink-2);font-size:.85rem;}
  .att button{padding:.35em .7em;font-size:.82rem;white-space:nowrap;}
  .vazio{color:var(--ink-3);font-size:.9rem;padding:6px 4px;}

  .stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(96px,1fr));gap:10px;margin-bottom:6px;}
  .stat{background:var(--surface);border:1px solid var(--line);border-radius:10px;padding:11px 12px;}
  .stat .n{font-size:1.15rem;font-weight:700;font-variant-numeric:tabular-nums;}
  .stat .l{color:var(--ink-3);font-size:.72rem;text-transform:uppercase;letter-spacing:.05em;}

  .agenda{display:flex;flex-direction:column;}
  .ag{display:flex;gap:14px;align-items:center;padding:11px 6px;border-bottom:1px solid var(--line);}
  .ag:last-child{border-bottom:none;}
  .ag .h{font-variant-numeric:tabular-nums;font-weight:600;color:var(--ink-2);width:44px;flex:none;}
  .ag .barra{width:3px;align-self:stretch;border-radius:2px;background:var(--accent);flex:none;}
  .ag .info{flex:1;min-width:0;}
  .ag .cli{font-weight:600;font-size:.92rem;}
  .ag .srv{color:var(--ink-2);font-size:.84rem;}
  .ag .pill{font-size:.7rem;padding:.2em .55em;border-radius:999px;background:var(--surface-2);color:var(--ink-3);white-space:nowrap;}
  .ag .pill.done{background:var(--live-soft);color:var(--live);}
  .ag .pill.here{background:var(--next-soft);color:var(--next);}
  #erro{position:fixed;left:50%;bottom:18px;transform:translateX(-50%);background:var(--crit);color:#fff;
    padding:.7em 1.1em;border-radius:10px;font-size:.88rem;display:none;box-shadow:0 6px 24px rgba(0,0,0,.25);}
  dialog{border:1px solid var(--line);border-radius:14px;background:var(--surface);color:var(--ink);
    padding:20px;max-width:420px;width:92vw;}
  dialog::backdrop{background:rgba(0,0,0,.45);}
  dialog h3{margin:0 0 12px;font-size:1.05rem;}
  dialog .afet{background:var(--surface-2);border-radius:8px;padding:10px 12px;margin:10px 0;font-size:.88rem;}
</style>
</head>
<body>
<div class="wrap">
  <header class="top">
    <h1>Hoje <span class="marca">{{BUSINESS_NAME}}</span></h1>
    <a href="/dashboard">Calendário completo →</a>
  </header>

  <div id="cockpit" class="card k-done">
    <span class="badge"><span class="dot"></span><span id="ck-badge">A carregar…</span></span>
    <h2 id="ck-titulo">…</h2>
    <div class="sub" id="ck-sub"></div>
    <div class="timeline" id="ck-tl" hidden><i id="ck-tl-fill" style="width:0"></i></div>
    <div class="meta" id="ck-meta"></div>
    <div class="acoes" id="ck-acoes"></div>
  </div>

  <h3 class="sec">Precisa da tua atenção</h3>
  <div class="atencao" id="atencao"><div class="vazio">A carregar…</div></div>

  <h3 class="sec">Hoje</h3>
  <div class="stats" id="stats"></div>

  <h3 class="sec">Agenda de hoje</h3>
  <div class="card"><div class="agenda" id="agenda"><div class="vazio">A carregar…</div></div></div>
</div>

<div id="erro"></div>

<dialog id="dlg-atraso">
  <h3>Atraso — quem fica afetado?</h3>
  <div id="atraso-corpo">A calcular…</div>
  <div style="display:flex;gap:8px;justify-content:flex-end;margin-top:14px;">
    <button onclick="document.getElementById('dlg-atraso').close()">Fechar</button>
  </div>
</dialog>

<script>
const $ = s => document.querySelector(s);
const esc = s => String(s==null?'':s).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
function erro(m){ const e=$('#erro'); e.textContent=m; e.style.display='block'; setTimeout(()=>e.style.display='none',4000); }

async function api(url, opts){
  const r = await fetch(url, opts);
  if(!r.ok){ const j = await r.json().catch(()=>({})); throw new Error(j.erro || ('Erro '+r.status)); }
  return r.json();
}

function fmtMin(m){
  m = Math.round(m);
  if(m < 60) return m + ' min';
  const h = Math.floor(m/60), r = m%60;
  return r ? `${h}h${String(r).padStart(2,'0')}` : `${h}h`;
}

function renderCockpit(ck){
  const c = $('#cockpit');
  c.className = 'card k-' + ck.kind;
  const acoes = $('#ck-acoes'); acoes.innerHTML = '';
  $('#ck-tl').hidden = true; $('#ck-meta').innerHTML = '';

  if(ck.kind === 'done'){
    $('#ck-badge').textContent = 'Agenda concluída';
    $('#ck-titulo').textContent = ck.marcacoes_hoje ? 'Está tudo feito por hoje ✨' : 'Hoje sem marcações';
    $('#ck-sub').textContent = ck.marcacoes_hoje ? `${ck.marcacoes_hoje} marcação(ões) concluída(s).` : '';
    return;
  }

  const m = ck.marcacao;
  const btnCliente = m.customer_id ? `<button onclick="location.href='/dashboard#ag-${m.id}'">👤 Cliente</button>` : '';
  const btnAtraso = `<button onclick="abrirAtraso()">⏰ Atraso</button>`;

  if(ck.kind === 'in_progress'){
    $('#ck-badge').textContent = ck.atrasado ? 'Em curso · a passar da hora' : 'Serviço em curso';
    $('#ck-titulo').textContent = m.cliente;
    $('#ck-sub').innerHTML = esc(m.servico) + (m.preco_por_confirmar ? ' · <em>preço a confirmar</em>' : '');
    const total = ck.decorrido_min + ck.restante_min || 1;
    $('#ck-tl').hidden = false;
    $('#ck-tl-fill').style.width = Math.min(100, 100*ck.decorrido_min/total) + '%';
    $('#ck-meta').innerHTML =
      `<span>${esc(ck.inicio)} → ${esc(ck.fim_previsto)}</span>` +
      `<span>Começou há <b>${fmtMin(ck.decorrido_min)}</b></span>` +
      `<span>Faltam <b>~${fmtMin(ck.restante_min)}</b></span>`;
    acoes.innerHTML =
      `<button class="primary" onclick="op(${m.id},'done')">✅ Concluir</button>` +
      btnAtraso + btnCliente;
    return;
  }

  // next
  $('#ck-badge').textContent = 'Próxima cliente';
  $('#ck-titulo').textContent = m.cliente;
  $('#ck-sub').innerHTML = esc(m.servico) + ' · ' + fmtMin(m.duracao_min || 0) +
    (m.preco_por_confirmar ? ' · <em>preço a confirmar</em>' : ' · ' + esc(m.preco_label));
  const bits = [`<span>Às <b>${esc(ck.hora)}</b></span>`,
    `<span>${ck.faltam_min >= 0 ? 'Daqui a <b>'+fmtMin(ck.faltam_min)+'</b>' : 'Já devia ter começado'}</span>`];
  if(m.cliente_visitas != null) bits.push(`<span><b>${m.cliente_visitas}</b> visita(s)</span>`);
  if(m.cliente_no_shows) bits.push(`<span style="color:var(--crit)"><b>${m.cliente_no_shows}</b> no-show</span>`);
  if(m.ultima_do_servico) bits.push(`<span>Último ${esc(m.servico)}: ${esc(m.ultima_do_servico)}</span>`);
  $('#ck-meta').innerHTML = bits.join('');
  if(m.notas) $('#ck-meta').innerHTML += `<span style="flex-basis:100%">🗒️ ${esc(m.notas)}</span>`;
  acoes.innerHTML =
    (ck.chegou
      ? `<button class="primary" onclick="op(${m.id},'in_progress')">▶️ Iniciar</button>`
      : `<button class="primary" onclick="op(${m.id},'arrived')">✅ Chegou</button>`) +
    btnAtraso + btnCliente;
}

function renderAtencao(itens){
  const box = $('#atencao');
  if(!itens.length){ box.innerHTML = '<div class="vazio">Está tudo tratado. ✨</div>'; return; }
  box.innerHTML = itens.map(i => `
    <div class="att n-${esc(i.nivel)}">
      <div class="txt">
        <div class="titulo">${esc(i.titulo)}</div>
        <div class="detalhe">${esc(i.detalhe||'')}</div>
      </div>
      ${i.appointment_id ? `<button onclick="location.href='/dashboard#ag-${i.appointment_id}'">Abrir</button>` : ''}
    </div>`).join('');
}

function renderStats(r){
  const receita = r.receita_por_confirmar
    ? `CHF ${(r.receita_cents/100).toFixed(0)}+`
    : `CHF ${(r.receita_cents/100).toFixed(0)}`;
  $('#stats').innerHTML = [
    ['Marcações', r.marcacoes], ['Concluídas', r.concluidas], ['Receita', receita],
    ['Novos clientes', r.novos_clientes], ['Cancelamentos', r.cancelamentos],
  ].map(([l,n]) => `<div class="stat"><div class="n">${esc(n)}</div><div class="l">${esc(l)}</div></div>`).join('');
}

function renderAgenda(ag){
  const box = $('#agenda');
  if(!ag.length){ box.innerHTML = '<div class="vazio">Sem marcações hoje.</div>'; return; }
  box.innerHTML = ag.map(m => {
    const pill = m.op_status === 'done' ? '<span class="pill done">concluída</span>'
      : m.op_status === 'arrived' ? '<span class="pill here">chegou</span>'
      : m.op_status === 'in_progress' ? '<span class="pill here">a decorrer</span>'
      : m.estado === 'no_show' ? '<span class="pill">não veio</span>' : '';
    return `<div class="ag">
      <span class="h">${esc(m.hora)}</span>
      <span class="barra"></span>
      <div class="info"><div class="cli">${esc(m.cliente)}</div>
        <div class="srv">${esc(m.servico)} · ${m.preco_por_confirmar ? 'a confirmar' : esc(m.preco_label)}</div></div>
      ${pill}
    </div>`;
  }).join('');
}

async function op(id, novo){
  document.querySelectorAll('#ck-acoes button').forEach(b => b.disabled = true);
  try{ const j = await api(`/api/agendamentos/${id}/op`, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({op:novo})});
    renderCockpit(j.cartao); carregar();
  }catch(e){ erro(e.message); carregar(); }
}

async function abrirAtraso(){
  const dlg = $('#dlg-atraso'); $('#atraso-corpo').innerHTML = `
    <p style="color:var(--ink-2);font-size:.9rem;margin-top:0">Quanto tempo de atraso?</p>
    <div style="display:flex;gap:6px;flex-wrap:wrap">
      ${[5,10,15,30].map(n => `<button onclick="calcAtraso(${n})">+${n} min</button>`).join('')}
    </div>
    <div id="atraso-res"></div>`;
  dlg.showModal();
}
async function calcAtraso(min){
  const res = $('#atraso-res'); res.innerHTML = '<p style="color:var(--ink-3)">A calcular…</p>';
  try{
    const j = await api('/api/painel/atraso', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({minutos:min})});
    if(!j.afetadas.length){ res.innerHTML = `<div class="afet">✅ Um atraso de ${min} min não afeta nenhuma marcação de hoje.</div>`; return; }
    res.innerHTML = `<div class="afet"><b>${j.afetadas.length}</b> marcação(ões) afetada(s):</div>` +
      j.afetadas.map(a => `<div class="afet">${esc(a.hora_original)} → ~${esc(a.hora_estimada)} · ${esc(a.cliente)} (${esc(a.servico)})</div>`).join('') +
      `<p style="color:var(--ink-3);font-size:.82rem">As mensagens só são enviadas quando confirmares — nunca automaticamente.</p>`;
  }catch(e){ res.innerHTML = `<div class="afet" style="color:var(--crit)">${esc(e.message)}</div>`; }
}

async function carregar(){
  try{
    const j = await api('/api/painel/hoje');
    renderCockpit(j.cartao); renderAtencao(j.atencao); renderStats(j.resumo); renderAgenda(j.agenda);
  }catch(e){ erro(e.message); }
}
carregar();
setInterval(carregar, 60000);
</script>
</body>
</html>
"""


@app.route("/dashboard", methods=["GET"])
@requer_autenticacao
def dashboard():
    """HTML do painel com a identidade do negócio já substituída — o nome vem
    sempre de BUSINESS_NAME (ambiente), nunca escrito à mão."""
    return DASHBOARD_HTML.replace("{{BUSINESS_NAME}}", _escapar_html(BUSINESS_NAME))


# String RAW (r"""), para o Python não tentar interpretar sequências de escape
# que pertencem ao JavaScript — ex.: o \d da expressão regular que abre
# automaticamente #pedido-<id> (ver abrirPedidoPeloHash, no fim do script).
# Sem o r, o Python emite "SyntaxWarning: invalid escape sequence '\d'".
DASHBOARD_HTML = r"""
<!doctype html>
<html lang="pt">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{{BUSINESS_NAME}} — Painel</title>
<style>
  :root{
    --bg:#0d0f12; --panel:#15181d; --panel2:#1b1f26; --border:#262b33;
    --gold:#e8b923; --text:#f2f3f5; --muted:#9aa1ac;
    /* laranja do indicador "Agora" — deliberadamente diferente do vermelho
       dos cancelamentos, para os dois nunca se confundirem */
    --agora:#ff7a59;
  }
  /* ---------------------------------------------------------------------
     ESCALA FLUIDA — tudo no painel (texto, botões, cartões, espaçamentos,
     colunas) está em `rem`, e o rem acompanha a LARGURA do browser. Num
     ecrã de 1280px o rem vale ~15px; num de 1990px vale ~19,5px; a partir
     de ~2270px pára nos 21px para não ficar gigante. Assim a página cresce
     e encolhe com a janela em vez de ficar minúscula num ecrã grande.
     --------------------------------------------------------------------- */
  html{font-size:clamp(14px, 0.636vw + 6.84px, 21px);}
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--text);font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;}
  /* Sem cabeçalho próprio: esta página é apresentada dentro de outra
     aplicação, que já tem o seu. O conteúdo começa logo no calendário. */
  /* usa TODA a largura que a página lhe der — sem max-width a limitar */
  .wrap{padding:0.5rem 0.625rem 1.25rem;max-width:none;margin:0;}

  /* --- Topo: calendário (flexível) + coluna de reservas (fixa) ----------- */
  /* O bloco do topo ocupa a ALTURA da janela e as duas colunas têm
     sempre a mesma altura (align-items:stretch): a coluna de reservas
     deixa de ser um cartão curto com um vazio enorme à volta, e o
     calendário deixa de sobrar espaço por baixo. */
  .topo{display:grid;grid-template-columns:minmax(0,1fr) clamp(16rem,20vw,24rem);gap:0.625rem;
        align-items:stretch;min-height:calc(100vh - 1.25rem);}
  .topo > .lista{min-width:0;min-height:0;display:flex;flex-direction:column;}

  /* estatísticas compactas, no topo da coluna de reservas */
  .stats{display:grid;grid-template-columns:1fr 1fr;gap:0.0625rem;background:var(--border);
         border-bottom:1px solid var(--border);}
  .card{background:var(--panel);padding:0.5rem 0.625rem;}
  .card .n{font-size:1.0625rem;font-weight:700;color:var(--gold);line-height:1.2;}
  .card .l{color:var(--muted);font-size:0.6562rem;margin-top:0.0625rem;line-height:1.25;}

  /* coluna de reservas em cascata */
  .col-reservas{display:flex;flex-direction:column;min-height:0;}
  .col-reservas h2{flex:0 0 auto;}
  .cascata{flex:1 1 auto;overflow-y:auto;min-height:0;padding:0.625rem 0.75rem 0.875rem;}
  .cascata .cal-agenda-dia{margin-bottom:0.625rem;}
  .cascata .cal-agenda-dia h4{position:sticky;top:-10px;background:var(--panel);padding:0.25rem 0;margin:0 0 0.3125rem;z-index:1;}
  .cal-nota{padding:1.625rem 1rem;text-align:center;color:var(--muted);font-size:0.7812rem;line-height:1.6;}
  .lista{background:var(--panel);border:1px solid var(--border);border-radius:0.75rem;overflow:hidden;}
  .lista h2{font-size:0.7812rem;margin:0;padding:0.5625rem 0.75rem;border-bottom:1px solid var(--border);color:var(--muted);font-weight:600;letter-spacing:.4px;text-transform:uppercase;}
  table{width:100%;border-collapse:collapse;}
  th,td{text-align:left;padding:0.75rem 1.125rem;font-size:0.875rem;border-bottom:1px solid var(--border);}
  th{color:var(--muted);font-weight:600;font-size:0.75rem;text-transform:uppercase;letter-spacing:.4px;}
  tr:last-child td{border-bottom:none;}
  tr:hover td{background:var(--panel2);}
  .tag{display:inline-block;background:rgba(232,185,35,.15);color:var(--gold);padding:0.1875rem 0.5625rem;border-radius:1.25rem;font-size:0.75rem;font-weight:600;}
  .estado-cancelado{color:#e05252;}
  .vazio{padding:2.5rem 1.125rem;text-align:center;color:var(--muted);}
  .refresh{color:var(--muted);font-size:0.75rem;}
  a.btn{background:var(--gold);color:#1a1400;padding:0.5rem 0.875rem;border-radius:0.5rem;text-decoration:none;font-size:0.8125rem;font-weight:700;}
  tr.clicavel{cursor:pointer;}
  .modal-overlay{display:none;position:fixed;inset:0;background:rgba(0,0,0,.6);align-items:center;justify-content:center;z-index:50;}
  .modal-overlay.aberto{display:flex;}
  .modal-caixa{background:var(--panel);border:1px solid var(--border);border-radius:0.75rem;max-width:40rem;width:92%;max-height:86vh;overflow-y:auto;}
  .modal-cabecalho{display:flex;justify-content:space-between;align-items:center;padding:1rem 1.125rem;border-bottom:1px solid var(--border);}
  .modal-cabecalho h3{margin:0;font-size:1rem;}
  .modal-fechar{cursor:pointer;color:var(--muted);font-size:1.125rem;}
  .modal-corpo{padding:1.125rem;font-size:0.875rem;line-height:1.7;}
  .modal-corpo .linha{margin-bottom:0.375rem;}
  .galeria{display:grid;grid-template-columns:repeat(auto-fill,minmax(90px,1fr));gap:0.5rem;margin-top:0.75rem;}
  .galeria img{width:100%;height:90px;object-fit:cover;border-radius:0.5rem;cursor:zoom-in;border:1px solid var(--border);}
  .lightbox{display:none;position:fixed;inset:0;background:rgba(0,0,0,.88);align-items:center;justify-content:center;z-index:60;cursor:zoom-out;}
  .lightbox.aberto{display:flex;}
  .lightbox img{max-width:92vw;max-height:92vh;border-radius:0.5rem;}
  .orc-tabela{width:100%;border-collapse:collapse;margin-top:0.5rem;}
  .orc-tabela th,.orc-tabela td{padding:0.375rem 0.25rem;font-size:0.8125rem;border-bottom:1px solid var(--border);}
  .orc-tabela input,.orc-tabela textarea{background:var(--panel2);border:1px solid var(--border);color:var(--text);border-radius:0.375rem;padding:0.3125rem 0.4375rem;font-size:0.8125rem;width:100%;}
  .orc-campo{margin-top:0.5rem;}
  .orc-campo label{display:block;color:var(--muted);font-size:0.75rem;margin-bottom:0.1875rem;}
  .orc-total{font-size:0.9375rem;margin-top:0.625rem;}
  .orc-acoes{display:flex;gap:0.5rem;flex-wrap:wrap;margin-top:0.875rem;}
  .orc-acoes button,.orc-acoes a{cursor:pointer;border:none;border-radius:0.5rem;padding:0.5rem 0.75rem;font-size:0.8125rem;font-weight:700;text-decoration:none;display:inline-block;}
  .btn-primario{background:var(--gold);color:#1a1400;}
  .btn-secundario{background:var(--panel2);color:var(--text);border:1px solid var(--border) !important;}
  .btn-perigo{background:#3a1a1a;color:#f2a3a3;}
  .orc-erro{color:#e05252;font-size:0.7812rem;margin-top:0.375rem;}
  .orc-mini{background:transparent;border:1px solid var(--border);color:var(--muted);border-radius:0.375rem;padding:0.1875rem 0.5rem;font-size:0.75rem;cursor:pointer;}

  /* --- Calendário ------------------------------------------------------- */
  .cal-barra{display:flex;flex-wrap:wrap;gap:0.5rem;align-items:center;padding:0.5rem 0.75rem;border-bottom:1px solid var(--border);}
  .cal-barra-filtros{gap:0.875rem;}
  .cal-grupo{display:flex;gap:0.375rem;flex-wrap:wrap;}
  .cal-btn{background:var(--panel2);border:1px solid var(--border);color:var(--text);border-radius:0.5rem;
           padding:0.375rem 0.6875rem;font-size:0.8125rem;cursor:pointer;white-space:nowrap;}
  .cal-btn:hover{border-color:var(--gold);}
  .cal-btn.ativo{background:var(--gold);color:#1a1400;border-color:var(--gold);font-weight:700;}
  .cal-periodo{margin-left:auto;color:var(--text);font-size:0.875rem;font-weight:600;}
  .cal-filtro{display:inline-flex;align-items:center;gap:0.375rem;font-size:0.7812rem;color:var(--muted);cursor:pointer;
              border:1px solid var(--border);border-radius:1.25rem;padding:0.25rem 0.625rem;}
  .cal-filtro input{accent-color:var(--gold);margin:0;}
  .cal-legenda{display:flex;gap:0.75rem;flex-wrap:wrap;margin-left:auto;font-size:0.75rem;color:var(--muted);}
  .cal-legenda span{display:inline-flex;align-items:center;gap:0.3125rem;}
  .cal-ponto{width:.625rem;height:.625rem;border-radius:0.1875rem;display:inline-block;}
  .cal-aviso{margin:0.625rem 1.125rem 0;padding:0.5rem 0.75rem;border-radius:0.5rem;font-size:0.7812rem;
             background:rgba(232,185,35,.12);color:var(--gold);border:1px solid rgba(232,185,35,.35);}
  .cal-erro{margin:0.625rem 1.125rem 0;padding:0.5rem 0.75rem;border-radius:0.5rem;font-size:0.7812rem;
            background:rgba(224,82,82,.12);color:#e88;border:1px solid rgba(224,82,82,.35);}
  .cal-conteudo{padding:0.625rem 0.75rem 0.75rem;overflow-x:auto;overflow-y:auto;flex:1 1 auto;min-height:0;}
  .cal-carregando{color:var(--muted);font-size:0.7812rem;padding:0.375rem 0;}

  /* A COR do evento vem do SERVIÇO (inline, ver cor_do_servico em bot.py).
     Estas classes tratam só do ESTADO — nunca da cor identificadora. */
  .est-reagendado{opacity:.62;}
  .est-reagendado .ev-t{text-decoration:line-through;}
  .ev-badge{display:inline-block;padding:0 0.25rem;border-radius:0.25rem;font-size:0.5938rem;font-weight:700;
            background:rgba(255,255,255,.16);color:var(--text);margin-left:0.25rem;vertical-align:middle;
            letter-spacing:.2px;max-width:100%;overflow:hidden;text-overflow:ellipsis;}

  /* --- Cancelado: bloqueado vs. livre -----------------------------------
     A informação NUNCA é passada só pela cor. Cada um destes casos tem, ao
     mesmo tempo: fundo próprio, borda própria (sólida ou tracejada), um
     ícone (🔒 / 🔓) e a frase escrita "Horário bloqueado"/"Horário livre".
     A cor do SERVIÇO continua visível numa faixa lateral (--cor-servico),
     por isso continua a dar para ler serviço + estado + disponibilidade num
     relance. */
  /* NOTA: estas regras NÃO podem mexer no `position` — .cal-evento é
     position:absolute e é isso que o coloca na hora certa da grelha; um
     position:relative aqui (mais específico) atirava o cartão para o fundo
     da coluna. O ::before já se apoia nesse position:absolute. Só o
     .cal-mes-ev (que vive no fluxo normal) precisa de position:relative. */
  .cal-evento.bloqueado,.cal-mes-ev.bloqueado{
      background:#3d1414 !important;border:1px solid #e05252 !important;border-left:none !important;
      color:#f4c9c9;padding:0.125rem 0.375rem 0.125rem 0.6875rem;}
  .cal-evento.livre,.cal-mes-ev.livre{
      background:rgba(224,82,82,.09) !important;border:1px dashed #e05252 !important;border-left:none !important;
      color:#e7b3b3;padding:0.125rem 0.375rem 0.125rem 0.6875rem;}
  .cal-mes-ev.bloqueado,.cal-mes-ev.livre{position:relative;}
  /* faixa com a cor original do serviço, à esquerda do cartão vermelho */
  .cal-evento.bloqueado::before,.cal-evento.livre::before,
  .cal-mes-ev.bloqueado::before,.cal-mes-ev.livre::before{
      content:'';position:absolute;left:2px;top:3px;bottom:3px;width:4px;border-radius:0.1875rem;
      background:var(--cor-servico,#8b95a6);}
  .cal-evento.livre .ev-t,.cal-evento.bloqueado .ev-t{text-decoration:line-through;}
  .cal-evento.livre .ev-disp,.cal-evento.bloqueado .ev-disp{text-decoration:none;font-weight:700;}
  .ev-disp{display:block;font-size:0.5938rem;letter-spacing:0;line-height:1.2;}
  /* texto mais compacto nos cartões cancelados: assim o nome do serviço cabe
     sempre, mesmo num bloco de 45 minutos */
  .cal-evento.bloqueado,.cal-evento.livre{font-size:0.6562rem;line-height:1.22;}
  .cal-evento.bloqueado .ev-s,.cal-evento.livre .ev-s{color:inherit;opacity:.85;}
  /* uma marcação cancelada e LIBERTADA não pode parecer que ocupa o horário:
     encosta-se à direita, estreita e por cima, deixando o slot visualmente
     vazio para uma reserva nova. */
  .cal-evento.livre{opacity:.9;}

  /* grelha semana/dia */
  .cal-grelha{display:grid;position:relative;min-width:42.5rem;border:1px solid var(--border);border-radius:0.625rem;overflow:hidden;}
  .cal-grelha.dia{min-width:20rem;}
  .cal-cab{background:var(--panel2);padding:0.3125rem 0.375rem;text-align:center;font-size:0.75rem;border-bottom:1px solid var(--border);
           position:sticky;top:0;z-index:2;line-height:1.25;}
  .cal-cab.hoje{color:var(--gold);font-weight:700;}
  .cal-cab-hora{background:var(--panel2);border-bottom:1px solid var(--border);border-right:1px solid var(--border);}
  .cal-horas{border-right:1px solid var(--border);position:relative;}
  /* --faixa é recalculada em JavaScript para a semana inteira caber no ecrã
     sem scroll (ver calAjustarAlturaFaixa). O 28px é só o valor de arranque. */
  .cal-hora{height:var(--faixa,28px);font-size:0.6562rem;color:var(--muted);text-align:right;padding-right:0.375rem;
            border-bottom:1px dashed rgba(255,255,255,.05);box-sizing:border-box;
            overflow:hidden;line-height:1.1;}
  .cal-coluna{position:relative;border-right:1px solid var(--border);}
  .cal-coluna:last-child{border-right:none;}
  .cal-faixa{height:var(--faixa,28px);border-bottom:1px dashed rgba(255,255,255,.05);box-sizing:border-box;}
  .cal-faixa.hora-cheia{border-bottom-color:rgba(255,255,255,.12);}
  .cal-evento{position:absolute;border-radius:0.375rem;padding:0.1875rem 0.375rem;font-size:0.7188rem;line-height:1.28;overflow:hidden;
              cursor:pointer;box-sizing:border-box;color:var(--text);}
  .cal-evento:hover{filter:brightness(1.25);}
  .cal-evento .ev-t{font-weight:700;}
  .cal-evento .ev-s{color:var(--muted);}
  /* com a grelha comprimida o texto encurta com reticências — nunca fica
     cortado a meio de uma palavra nem transborda o cartão */
  .cal-evento .ev-t,.cal-evento .ev-s,.cal-evento .ev-disp{
      white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}
  /* Prioridade quando o cartão é estreito (marcações sobrepostas): a HORA
     nunca desaparece, o crachá do estado encolhe a seguir, e o nome é o
     primeiro a ser encurtado. Nenhum dos dois pode ficar a zero. */
  .cal-evento .ev-t{display:flex;align-items:baseline;gap:0.25rem;}
  .cal-evento .ev-quem{flex:1 1 auto;min-width:5.5ch;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
  .cal-evento .ev-badge{flex:0 1 auto;min-width:0;margin-left:0;overflow:hidden;text-overflow:ellipsis;}
  /* cartão estreito: o estado ganha uma linha só para ele */
  .cal-evento .ev-estado{line-height:1.3;margin:0.0625rem 0;}
  .cal-evento .ev-estado .ev-badge{margin-left:0;}
  .cal-agenda-ev .ev-t{display:block;}
  .cal-agenda-ev .ev-quem{white-space:normal;}
  .cal-evento.compacto{padding:0.0625rem 0.3125rem;line-height:1.18;}
  .cal-evento.compacto .ev-t{font-size:0.625rem;}
  .cal-agenda-ev .ev-t,.cal-agenda-ev .ev-s,.cal-agenda-ev .ev-disp{white-space:normal;}
  .cal-dia-inteiro{margin:0 0 0.375rem;}
  /* --- Indicador da hora atual ------------------------------------------
     Linha FINA a toda a largura da coluna do dia de hoje, com uma etiqueta
     "Agora · HH:MM" — nunca um traço vermelho solto que parece um elemento
     partido, e nunca confundível com uma marcação (não tem fundo de cartão,
     não recebe cliques e não entra na disposição dos eventos). */
  .cal-agora{position:absolute;left:0;right:0;height:0;z-index:6;pointer-events:none;
             border-top:1px solid var(--agora,#ff7a59);}
  .cal-agora .agora-etiqueta{position:absolute;left:.1875rem;top:-.56rem;padding:0.0625rem 0.375rem;border-radius:0.5625rem;
             font-size:0.5938rem;font-weight:700;line-height:1.5;white-space:nowrap;letter-spacing:.2px;
             background:var(--agora,#ff7a59);color:#25120c;box-shadow:0 1px 5px rgba(0,0,0,.45);}
  /* cabeçalho do dia de hoje: destaque DISCRETO (não grita, mas vê-se) */
  .cal-cab.hoje{color:var(--agora,#ff7a59);font-weight:700;
                background:linear-gradient(180deg,rgba(255,122,89,.16),rgba(255,122,89,0));
                box-shadow:inset 0 -2px 0 var(--agora,#ff7a59);}
  .cal-cab .cab-hoje{display:block;font-size:0.5625rem;letter-spacing:.6px;text-transform:uppercase;opacity:.85;}
  /* telemóvel (agenda vertical): a mesma informação, sem grelha horária */
  .cal-agora-agenda{display:flex;align-items:center;gap:0.5rem;margin:0.125rem 0 0.5rem;
                    font-size:0.6562rem;font-weight:700;color:var(--agora,#ff7a59);}
  .cal-agora-agenda::after{content:'';flex:1;height:1px;background:var(--agora,#ff7a59);opacity:.55;}

  /* mês */
  .cal-mes{display:grid;grid-template-columns:repeat(7,minmax(90px,1fr));gap:0.0625rem;background:var(--border);
           border:1px solid var(--border);border-radius:0.625rem;overflow:hidden;min-width:41.25rem;}
  .cal-mes-cab{background:var(--panel2);padding:0.4375rem 0.25rem;text-align:center;font-size:0.75rem;color:var(--muted);}
  .cal-mes-cel{background:var(--panel);min-height:6rem;padding:0.3125rem;}
  .cal-mes-cel.fora{opacity:.45;}
  .cal-mes-cel.hoje{outline:1px solid var(--gold);outline-offset:-1px;}
  .cal-mes-num{font-size:0.7188rem;color:var(--muted);margin-bottom:0.25rem;}
  .cal-mes-cel.hoje .cal-mes-num{color:var(--gold);font-weight:700;}
  .cal-mes-ev{border-radius:0.3125rem;padding:0.125rem 0.3125rem;font-size:0.6875rem;margin-bottom:0.1875rem;cursor:pointer;
              white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}
  .cal-mes-mais{font-size:0.6562rem;color:var(--muted);}

  /* agenda vertical (telemóvel) */
  .cal-agenda-dia{margin-bottom:0.875rem;}
  .cal-agenda-dia h4{margin:0 0 0.375rem;font-size:0.8125rem;color:var(--gold);font-weight:600;}
  .cal-agenda-ev{border-radius:0.5rem;padding:0.5rem 0.625rem;margin-bottom:0.375rem;font-size:0.8125rem;cursor:pointer;}

  /* pré-visualização (hover, só em ecrãs com rato) */
  /* pointer-events:auto -> os botões da pré-visualização são mesmo clicáveis */
  .cal-preview{position:fixed;z-index:70;max-width:18.75rem;background:var(--panel);border:1px solid var(--border);
               border-radius:0.625rem;padding:0.6875rem 0.8125rem;font-size:0.7812rem;line-height:1.55;
               box-shadow:0 10px 30px rgba(0,0,0,.55);pointer-events:auto;}
  .cal-preview img{width:100%;height:4.875rem;object-fit:cover;border-radius:0.375rem;margin-top:0.4375rem;}
  .cal-preview .pv-t{font-weight:700;margin-bottom:0.1875rem;}
  .pv-acoes{display:flex;gap:0.375rem;flex-wrap:wrap;margin-top:0.625rem;padding-top:0.5625rem;border-top:1px solid var(--border);}
  .pv-acoes button{cursor:pointer;border:1px solid var(--border);background:var(--panel2);color:var(--text);
                   border-radius:0.4375rem;padding:0.3125rem 0.5625rem;font-size:0.7188rem;white-space:nowrap;}
  .pv-acoes button:hover{border-color:var(--gold);}
  .pv-acoes button.perigo{background:#3a1a1a;color:#f2a3a3;border-color:#5a2a2a;}
  .cal-legenda-servicos{display:flex;gap:0.5625rem;flex-wrap:wrap;font-size:0.6875rem;color:var(--muted);
                        padding:0.4375rem 0.75rem;border-bottom:1px solid var(--border);}
  /* diálogo de confirmação (cancelar / reagendar) */
  .dlg-fundo{display:none;position:fixed;inset:0;background:rgba(0,0,0,.62);z-index:90;
             align-items:center;justify-content:center;padding:1rem;}
  .dlg-fundo.aberto{display:flex;}
  .dlg{background:var(--panel);border:1px solid var(--border);border-radius:0.75rem;max-width:27.5rem;width:100%;
       max-height:88vh;overflow-y:auto;}
  .dlg-cab{display:flex;justify-content:space-between;align-items:center;padding:0.875rem 1.125rem;
           border-bottom:1px solid var(--border);}
  .dlg-cab h3{margin:0;font-size:0.9375rem;}
  .dlg-corpo{padding:1rem 1.125rem;font-size:0.8438rem;line-height:1.7;}
  .dlg-corpo .linha{margin-bottom:0.3125rem;}
  .dlg-corpo label{display:block;color:var(--muted);font-size:0.75rem;margin:0.625rem 0 0.25rem;}
  .dlg-corpo input{background:var(--panel2);border:1px solid var(--border);color:var(--text);
                   border-radius:0.4375rem;padding:0.4375rem 0.5625rem;font-size:0.875rem;width:100%;}
  .dlg-aviso{margin-top:0.75rem;padding:0.5rem 0.6875rem;border-radius:0.5rem;font-size:0.7812rem;
             background:rgba(232,185,35,.12);color:var(--gold);border:1px solid rgba(232,185,35,.35);}
  /* escolha "libertar / manter" dentro do diálogo de cancelamento */
  .dlg-escolha{margin-top:0.875rem;padding-top:0.75rem;border-top:1px solid var(--border);}
  .dlg-escolha > strong{display:block;font-size:0.8125rem;margin-bottom:0.5rem;}
  .dlg-opcao{display:flex;gap:0.5625rem;align-items:flex-start;padding:0.5625rem 0.6875rem;margin-bottom:0.4375rem;cursor:pointer;
             border:1px solid var(--border);border-radius:0.5625rem;background:var(--panel2);font-size:0.8125rem;}
  .dlg-opcao:hover{border-color:var(--gold);}
  .dlg-opcao input{width:auto;margin:0.1875rem 0 0;accent-color:var(--gold);flex:0 0 auto;}
  .dlg-opcao .op-desc{display:block;color:var(--muted);font-size:0.7188rem;margin-top:0.125rem;line-height:1.45;}
  .dlg-opcao.escolhida{border-color:var(--gold);background:rgba(232,185,35,.08);}
  .dlg-padrao{display:flex;gap:0.5rem;align-items:center;font-size:0.7812rem;color:var(--muted);margin-top:0.25rem;cursor:pointer;}
  .dlg-padrao input{width:auto;margin:0;accent-color:var(--gold);}

  /* --- Definições do painel --------------------------------------------- */
  .def-linha{display:flex;gap:0.875rem;align-items:flex-start;justify-content:space-between;
             flex-wrap:wrap;padding:0.875rem 1.125rem;}
  .def-texto{max-width:40rem;}
  .def-texto strong{display:block;font-size:0.8438rem;margin-bottom:0.1875rem;}
  .def-texto span{color:var(--muted);font-size:0.7812rem;line-height:1.6;}
  .interruptor{display:inline-flex;align-items:center;gap:0.625rem;cursor:pointer;font-size:0.7812rem;
               color:var(--muted);white-space:nowrap;}
  .interruptor input{position:absolute;opacity:0;width:0;height:0;}
  .interruptor .calha{width:2.75rem;height:1.5rem;border-radius:1.25rem;background:#3a3f4a;position:relative;
                      transition:background .15s ease;flex:0 0 auto;}
  .interruptor .calha::after{content:'';position:absolute;top:.1875rem;left:.1875rem;width:1.125rem;height:1.125rem;
                      border-radius:50%;background:#c9ced8;transition:transform .15s ease,background .15s ease;}
  .interruptor input:checked + .calha{background:var(--gold);}
  .interruptor input:checked + .calha::after{transform:translateX(1.25rem);background:#1a1400;}
  .interruptor input:focus-visible + .calha{outline:2px solid var(--gold);outline-offset:2px;}
  .def-estado{font-size:0.75rem;padding:0 1.125rem 0.875rem;color:var(--muted);}
  .dlg-erro{margin-top:0.625rem;color:#e88;font-size:0.7812rem;}
  .dlg-acoes{display:flex;gap:0.5rem;justify-content:flex-end;padding:0 1.125rem 1.125rem;flex-wrap:wrap;}
  .dlg-acoes button{cursor:pointer;border:1px solid var(--border);border-radius:0.5rem;padding:0.5rem 0.875rem;
                    font-size:0.8125rem;background:var(--panel2);color:var(--text);}
  .dlg-acoes button.principal{background:var(--gold);color:#1a1400;border-color:var(--gold);font-weight:700;}
  .dlg-acoes button.perigo{background:#e05252;color:#fff;border-color:#e05252;font-weight:700;}
  .toast{position:fixed;left:50%;bottom:22px;transform:translateX(-50%);z-index:95;padding:0.6875rem 1rem;
         border-radius:0.625rem;font-size:0.8125rem;max-width:92vw;box-shadow:0 8px 26px rgba(0,0,0,.5);}
  .toast.ok{background:#173a26;color:#9fe0b8;border:1px solid #2ea05a;}
  .toast.aviso{background:#3a3417;color:#ecd98a;border:1px solid #e8b923;}
  .toast.erro{background:#3a1a1a;color:#f2a3a3;border:1px solid #e05252;}

  /* painel lateral (dossiê da marcação) */
  .painel-fundo{display:none;position:fixed;inset:0;background:rgba(0,0,0,.55);z-index:80;}
  .painel-fundo.aberto{display:block;}
  .painel{position:fixed;top:0;right:0;height:100%;width:26rem;max-width:94vw;background:var(--panel);
          border-left:1px solid var(--border);z-index:81;transform:translateX(102%);transition:transform .18s ease;
          display:flex;flex-direction:column;}
  .painel.aberto{transform:translateX(0);}
  .painel-cabecalho{display:flex;justify-content:space-between;align-items:center;padding:0.9375rem 1.125rem;
                    border-bottom:1px solid var(--border);}
  .painel-cabecalho h3{margin:0;font-size:0.9375rem;}
  .painel-corpo{padding:1rem 1.125rem 1.625rem;overflow-y:auto;font-size:0.8438rem;line-height:1.7;}
  .painel-corpo .linha{margin-bottom:0.3125rem;}
  .painel-corpo h4{margin:1rem 0 0.375rem;font-size:0.75rem;text-transform:uppercase;letter-spacing:.4px;color:var(--muted);}
  .painel-corpo a{color:var(--gold);}
  .painel-acoes{display:flex;gap:0.5rem;flex-wrap:wrap;margin:0.75rem 0 0.25rem;}
  .painel-acoes a{background:var(--gold);color:#1a1400;padding:0.4375rem 0.75rem;border-radius:0.5rem;text-decoration:none;
                  font-size:0.7812rem;font-weight:700;}
  .estado-chip{display:inline-block;padding:0.125rem 0.5625rem;border-radius:1.25rem;font-size:0.7188rem;font-weight:700;}

  /* Abaixo de 1100px passa a uma coluna só: primeiro o calendário, logo a
     seguir a coluna de reservas em cascata. */
  @media (max-width: 1100px){
    .topo{grid-template-columns:minmax(0,1fr);min-height:0;align-items:start;}
    .col-reservas{max-height:none;}
    .cascata{max-height:70vh;}
    .stats{grid-template-columns:repeat(4,1fr);}
  }
  @media (max-width: 720px){
    .wrap{padding:0.625rem 0.625rem 1.25rem;}
    .cal-periodo{margin-left:0;width:100%;}
    .cal-legenda{margin-left:0;}
    .cal-conteudo{padding:0.625rem;}
    .stats{grid-template-columns:1fr 1fr;}
    .painel{width:100%;}
  }
</style>
</head>
<body>
<div class="wrap">
  <div class="topo">
    <div class="lista">
      <h2>📅 {{BUSINESS_NAME}} — Calendário</h2>
      <div class="cal-barra">
        <div class="cal-grupo">
          <button class="cal-btn" onclick="calHoje()">Hoje</button>
          <button class="cal-btn" onclick="calNavegar(-1)" aria-label="Anterior">‹ Anterior</button>
          <button class="cal-btn" onclick="calNavegar(1)" aria-label="Seguinte">Seguinte ›</button>
        </div>
        <div class="cal-grupo" id="cal-vistas">
          <button class="cal-btn" data-vista="dia" onclick="calMudarVista('dia')">Dia</button>
          <button class="cal-btn" data-vista="semana" onclick="calMudarVista('semana')">Semana</button>
          <button class="cal-btn" data-vista="mes" onclick="calMudarVista('mes')">Mês</button>
        </div>
        <div class="cal-grupo">
          <button class="cal-btn" onclick="calCarregar(true)">🔄 Atualizar</button>
        </div>
        <div class="cal-periodo" id="cal-periodo">—</div>
      </div>

      <div class="cal-barra cal-barra-filtros">
        <div class="cal-grupo" id="cal-filtros"></div>
        <div class="cal-legenda" id="cal-legenda"></div>
      </div>
      <div class="cal-legenda-servicos" id="cal-legenda-servicos"></div>

      <div id="cal-aviso" class="cal-aviso" hidden></div>
      <div id="cal-erro" class="cal-erro" hidden></div>
      <div id="cal-conteudo" class="cal-conteudo"><div class="vazio">A carregar…</div></div>
    </div>

    <aside class="lista col-reservas">
      <div class="stats">
        <div class="card"><div class="n" id="st-total">0</div><div class="l">Agendamentos</div></div>
        <div class="card"><div class="n" id="st-hoje">0</div><div class="l">Marcados hoje</div></div>
        <div class="card"><div class="n" id="st-clientes">0</div><div class="l">Clientes únicos</div></div>
        <div class="card"><div class="n" id="st-receita">CHF 0</div><div class="l">Receita estimada</div></div>
      </div>
      <h2>🧾 Reservas do período</h2>
      <div id="cascata" class="cascata"><div class="vazio">A carregar…</div></div>
    </aside>
  </div>

  <div class="lista" style="margin-top:14px;">
    <h2>⚙️ Definições</h2>
    <div class="def-linha">
      <div class="def-texto">
        <strong>Ao cancelar uma marcação</strong>
        <span id="def-descricao">Com esta opção <em>ligada</em>, a marcação fica guardada no histórico como
        cancelada mas o horário volta automaticamente a ficar disponível para novas reservas.
        Desligada, a marcação cancelada continua a ocupar o horário e a impedir novas reservas.</span>
      </div>
      <label class="interruptor" for="def-libertar">
        <input type="checkbox" id="def-libertar">
        <span class="calha"></span>
        <span>Libertar automaticamente o horário</span>
      </label>
    </div>
    <div class="def-estado" id="def-estado">A carregar as definições…</div>
  </div>

  <div class="lista" style="margin-top:14px;">
    <h2>Marcações</h2>
    <div id="conteudo"><div class="vazio">A carregar…</div></div>
  </div>

  <!-- Bloco legado (pedidos de orcamento). Oculto: a rota /api/pedidos e as
       tabelas continuam a existir apenas para dados antigos. -->
  <div class="lista" style="margin-top:14px;" hidden>
    <h2>Pedidos de orçamento (legado)</h2>
    <div id="conteudo-pedidos"><div class="vazio">A carregar…</div></div>
  </div>

  <div class="refresh" style="margin-top:10px;">Atualiza-se sozinho a cada 20 segundos (calendário a cada 30).</div>
</div>

<div id="modal-pedido" class="modal-overlay" onclick="fecharModalSeExterior(event)">
  <div class="modal-caixa">
    <div class="modal-cabecalho">
      <h3 id="modal-titulo">Pedido de orçamento</h3>
      <span class="modal-fechar" onclick="fecharModal()">✕</span>
    </div>
    <div id="modal-corpo" class="modal-corpo"></div>
  </div>
</div>

<div id="painel-fundo" class="painel-fundo" onclick="fecharPainel()"></div>
<aside id="painel-ag" class="painel" aria-hidden="true">
  <div class="painel-cabecalho">
    <h3 id="painel-titulo">Marcação</h3>
    <span class="modal-fechar" onclick="fecharPainel()">✕</span>
  </div>
  <div id="painel-corpo" class="painel-corpo"></div>
</aside>

<div id="cal-preview" class="cal-preview" hidden></div>

<div id="dlg-fundo" class="dlg-fundo" onclick="fecharDialogoSeExterior(event)">
  <div class="dlg">
    <div class="dlg-cab">
      <h3 id="dlg-titulo">Confirmar</h3>
      <span class="modal-fechar" onclick="fecharDialogo()">✕</span>
    </div>
    <div id="dlg-corpo" class="dlg-corpo"></div>
    <div id="dlg-acoes" class="dlg-acoes"></div>
  </div>
</div>

<div id="lightbox" class="lightbox" onclick="this.classList.remove('aberto')">
  <img id="lightbox-img" src="">
</div>

<script>
// Escapa qualquer valor antes de o colocar em innerHTML — nunca confiar em
// dados vindos da base de dados (nome, veículo, observações, etc.) como se
// fossem HTML seguro.
function esc(valor){
  if(valor === null || valor === undefined) return '';
  return String(valor).replace(/[&<>"']/g, c => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));
}

function formatarCentimos(centimos){
  // null/undefined = serviço sem preço definido -> "A confirmar" (nunca CHF 0).
  if(centimos == null) return 'A confirmar';
  return 'CHF ' + (centimos/100).toFixed(2);
}

async function carregar(){
  const resp = await fetch('/api/agendamentos');
  const dados = await resp.json();

  document.getElementById('st-total').textContent = dados.length;

  const hojeStr = new Date().toISOString().slice(0,10);
  const hoje = dados.filter(d => (d.criado_em||'').slice(0,10) === hojeStr).length;
  document.getElementById('st-hoje').textContent = hoje;

  const clientes = new Set(dados.map(d => d.telefone));
  document.getElementById('st-clientes').textContent = clientes.size;

  // Receita estimada: só marcações ativas/realizadas COM preço definido
  // (as de "preço a confirmar" não entram — nunca se soma 0 artificial).
  const RECEITA_ESTADOS = ['confirmed', 'pending', 'completed'];
  const receita = dados
    .filter(d => RECEITA_ESTADOS.includes(chaveEstado(d.estado)) && d.preco != null)
    .reduce((s,d) => s + Number(d.preco || 0), 0);
  document.getElementById('st-receita').textContent = 'CHF ' + receita.toFixed(0);

  const cont = document.getElementById('conteudo');
  if(dados.length === 0){
    cont.innerHTML = '<div class="vazio">Ainda não há marcações. Manda uma mensagem ao bot no WhatsApp para testar 👋</div>';
    return;
  }

  let html = '<table><thead><tr><th>Cliente</th><th>Serviço</th><th>Data</th><th>Hora</th><th>Preço</th><th>Estado</th><th>Horário</th><th>Recebido em</th></tr></thead><tbody>';
  dados.forEach(d => {
    const criado = d.criado_em ? new Date(d.criado_em).toLocaleString('pt-PT') : '-';
    const ce = chaveEstado(d.estado);
    const classeEstado = (ce === 'confirmed' || ce === 'pending') ? '' : 'estado-cancelado';
    const estadoRotulo = (infoEstado(d.estado) || {}).nome || d.estado;
    // A tabela diz, por texto e ícone, se o registo ainda ocupa o horário.
    const bloqueia = evBloqueiaHorario(d);
    const horario = bloqueia
      ? '<span style="color:#f2a3a3;">🔒 Bloqueado</span>'
      : '<span style="color:#9fe0b8;">🔓 Livre</span>';
    html += `<tr class="clicavel" onclick="abrirPainelAgendamento(${parseInt(d.id, 10)})">
      <td>${esc(d.nome || d.telefone)}<br><span style="color:var(--muted);font-size:12px;">${esc(d.telefone)}</span></td>
      <td><span class="tag">${esc(d.servico)}</span>${d.extra ? '<br><span style="color:var(--muted);font-size:12px;">+ '+esc(d.extra)+'</span>' : ''}</td>
      <td>${esc(d.data) || '-'}</td>
      <td>${esc(d.hora) || '-'}</td>
      <td>${d.preco != null ? 'CHF '+esc(d.preco) : '<span style="color:var(--muted);">A confirmar</span>'}</td>
      <td class="${classeEstado}">${esc(estadoRotulo)}</td>
      <td>${horario}</td>
      <td style="color:var(--muted);">${esc(criado)}</td>
    </tr>`;
  });
  html += '</tbody></table>';
  cont.innerHTML = html;
}

async function carregarPedidos(){
  return;  /* legado removido — /api/pedidos já não existe */
  const resp = await fetch('/api/pedidos');
  if(!resp.ok){ return; }
  const dados = await resp.json();

  const cont = document.getElementById('conteudo-pedidos');
  if(dados.length === 0){
    cont.innerHTML = '<div class="vazio">Ainda não há pedidos de orçamento com fotografias.</div>';
    return;
  }

  let html = '<table><thead><tr><th>Cliente</th><th>Modo</th><th>Veículo</th><th>Wrap</th><th>Preço</th><th>Estado</th><th>Fotos</th><th>Pedido em</th></tr></thead><tbody>';
  dados.forEach(p => {
    const criado = p.criado_em ? new Date(p.criado_em).toLocaleString('pt-PT') : '-';
    html += `<tr class="clicavel" onclick="abrirPedido(${parseInt(p.id, 10)})">
      <td>${esc(p.nome || p.telefone)}<br><span style="color:var(--muted);font-size:12px;">${esc(p.telefone)}</span></td>
      <td>${esc(nomeModo(p.modo_pedido))}</td>
      <td>${esc(p.veiculo) || '-'}${p.ano_veiculo ? ' ('+esc(p.ano_veiculo)+')' : ''}</td>
      <td><span class="tag">${esc(p.tipo_wrap) || '-'}</span>${p.cor_acabamento ? '<br><span style="color:var(--muted);font-size:12px;">'+esc(p.cor_acabamento)+'</span>' : ''}</td>
      <td>${precoPedido(p)}</td>
      <td>${esc(p.estado)}</td>
      <td>${p.num_fotos || 0}</td>
      <td style="color:var(--muted);">${esc(criado)}</td>
    </tr>`;
  });
  html += '</tbody></table>';
  cont.innerHTML = html;
}

// Nome legível do modo de pedido (pedidos antigos, anteriores à coluna
// modo_pedido, são apresentados como configuração detalhada).
function nomeModo(modo){
  const nomes = {
    'rapido': '⚡ Pedido rápido',
    'detalhe': '🎨 Configuração detalhada',
    'especialista': '💬 Contacto com especialista',
  };
  return nomes[modo || 'detalhe'] || modo;
}

// Pedidos rápidos e de contacto com especialista nunca têm preço calculado.
function precoPedido(p){
  let carrinho = [];
  try { carrinho = p.carrinho_json ? JSON.parse(p.carrinho_json) : []; } catch(e) { carrinho = []; }
  if(!carrinho.length){ return '<span style="color:var(--muted);">Sob análise</span>'; }
  const total = carrinho.reduce((s,l) => s + (l.preco||0)*(l.quantidade||1), 0);
  return 'CHF ' + (total/100).toFixed(2);
}

let pedidoAtualId = null;
let pedidoAtualTelefone = null;

async function abrirPedido(id){
  const resp = await fetch('/api/pedidos/' + id);
  if(!resp.ok){ return; }
  const p = await resp.json();
  pedidoAtualId = p.id;
  pedidoAtualTelefone = p.telefone;
  document.getElementById('modal-titulo').textContent = 'Pedido de orçamento #' + esc(p.id);

  let html = '';
  html += `<div class="linha">👤 Cliente: ${esc(p.nome || p.telefone)}</div>`;
  html += `<div class="linha">📱 Contacto: ${esc(p.telefone)}</div>`;
  html += `<div class="linha">🧭 Modo: ${esc(nomeModo(p.modo_pedido))}</div>`;
  html += `<div class="linha">🚗 Veículo: ${esc(p.veiculo) || '-'}${p.ano_veiculo ? ' ('+esc(p.ano_veiculo)+')' : ''}</div>`;
  html += `<div class="linha">🎨 Tipo: ${esc(p.tipo_wrap) || '-'}</div>`;
  html += `<div class="linha">🖌️ Cor/acabamento: ${esc(p.cor_acabamento) || '-'}</div>`;
  html += `<div class="linha">📌 Estado: <span id="pedido-estado-atual">${esc(p.estado)}</span></div>`;
  html += `<div class="linha">🕓 Pedido em: ${esc(p.criado_em ? new Date(p.criado_em).toLocaleString('pt-PT') : '-')}</div>`;

  let carrinho = [];
  try { carrinho = p.carrinho_json ? JSON.parse(p.carrinho_json) : []; } catch(e) { carrinho = []; }
  if(carrinho.length){
    html += '<div class="linha" style="margin-top:10px;">🧾 Carrinho:</div>';
    let total = 0;
    carrinho.forEach(l => {
      const preco = (l.preco||0) * (l.quantidade||1);
      total += preco;
      html += `<div class="linha" style="color:var(--muted);">• ${esc(l.nome)}: ${formatarCentimos(preco)}</div>`;
    });
    html += `<div class="linha"><strong>💰 Total estimado: ${formatarCentimos(total)}</strong></div>`;
  } else {
    html += '<div class="linha" style="margin-top:10px;"><strong>💰 Preço: Sob análise</strong></div>';
  }

  if(p.fotografias && p.fotografias.length){
    html += '<div class="linha" style="margin-top:10px;">📸 Fotografias (' + p.fotografias.length + '):</div>';
    html += '<div class="galeria">';
    p.fotografias.forEach(f => {
      const src = '/media/' + encodeURIComponent(f.nome_ficheiro);
      html += `<img src="${src}" onclick="abrirLightbox('${src}')">`;
    });
    html += '</div>';
  } else {
    html += '<div class="linha" style="margin-top:10px;color:var(--muted);">Sem fotografias enviadas.</div>';
  }

  html += '<div id="orc-secao" style="margin-top:16px;border-top:1px solid var(--border);padding-top:14px;">A carregar orçamento…</div>';

  document.getElementById('modal-corpo').innerHTML = html;
  document.getElementById('modal-pedido').classList.add('aberto');

  await carregarOrcamento(p.id, p.telefone);
}

async function carregarOrcamento(pedidoId, telefone){
  const secao = document.getElementById('orc-secao');
  const resp = await fetch('/api/pedidos/' + pedidoId + '/orcamento');
  if(!resp.ok){ secao.innerHTML = '<div class="orc-erro">Não foi possível carregar o orçamento.</div>'; return; }
  const dados = await resp.json();
  renderizarOrcamento(pedidoId, telefone, dados.orcamento);
}

function renderizarOrcamento(pedidoId, telefone, orcamento){
  const secao = document.getElementById('orc-secao');
  const linhas = orcamento ? orcamento.linhas : [];
  const editavel = !orcamento || orcamento.estado === 'rascunho';
  const waLink = 'https://wa.me/' + String(telefone || '').replace(/[^0-9]/g, '');

  let html = '<h4 style="margin:0 0 4px;">💰 Orçamento</h4>';
  if(orcamento && orcamento.estado !== 'rascunho'){
    html += `<div class="linha" style="color:var(--muted);">Versão ${esc(orcamento.versao)} — estado: ${esc(orcamento.estado)}`
          + (orcamento.enviado_em ? ' — enviado em ' + esc(new Date(orcamento.enviado_em).toLocaleString('pt-PT')) : '') + '</div>';
  }

  html += '<table class="orc-tabela"><thead><tr><th>Descrição</th><th style="width:70px;">Qtd</th><th style="width:100px;">Preço (CHF)</th><th></th></tr></thead><tbody>';
  linhas.forEach(l => {
    const precoChf = (l.preco_centimos/100).toFixed(2);
    if(editavel){
      html += `<tr data-linha-id="${l.id}">
        <td><input type="text" class="orc-desc" value="${esc(l.descricao)}"></td>
        <td><input type="number" min="1" step="1" class="orc-qtd" value="${esc(l.quantidade)}"></td>
        <td><input type="number" min="0" step="0.05" class="orc-preco" value="${esc(precoChf)}"></td>
        <td><button class="orc-mini" onclick="orcGuardarLinha(${pedidoId}, ${l.id})">💾</button>
            <button class="orc-mini" onclick="orcRemoverLinha(${pedidoId}, ${l.id})">🗑️</button></td>
      </tr>`;
    } else {
      html += `<tr><td>${esc(l.descricao)}</td><td>${esc(l.quantidade)}</td><td>${esc(precoChf)}</td><td></td></tr>`;
    }
  });
  if(editavel){
    html += `<tr>
      <td><input type="text" id="orc-nova-desc" placeholder="Ex.: Wrap total, mate"></td>
      <td><input type="number" id="orc-nova-qtd" min="1" step="1" value="1"></td>
      <td><input type="number" id="orc-nova-preco" min="0" step="0.05" value="0"></td>
      <td><button class="orc-mini" onclick="orcAdicionarLinha(${pedidoId})">➕</button></td>
    </tr>`;
  }
  html += '</tbody></table>';

  const descontoChf = orcamento ? (orcamento.desconto_centimos/100).toFixed(2) : '0.00';
  const observacoes = orcamento ? (orcamento.observacoes || '') : '';
  const validade = orcamento ? (orcamento.validade_dias || 14) : 14;

  if(editavel){
    html += `<div class="orc-campo"><label>Desconto (CHF)</label><input type="number" id="orc-desconto" min="0" step="0.05" value="${esc(descontoChf)}"></div>`;
    html += `<div class="orc-campo"><label>Observações</label><textarea id="orc-observacoes" rows="2">${esc(observacoes)}</textarea></div>`;
    html += `<div class="orc-campo"><label>Validade do orçamento (dias)</label><input type="number" id="orc-validade" min="1" max="90" step="1" value="${esc(validade)}"></div>`;
  } else if(orcamento){
    if(orcamento.desconto_centimos){ html += `<div class="linha">Desconto: -${formatarCentimos(orcamento.desconto_centimos)}</div>`; }
    if(observacoes){ html += `<div class="linha">Observações: ${esc(observacoes)}</div>`; }
    html += `<div class="linha">Validade: ${esc(validade)} dias</div>`;
  }

  const total = orcamento ? orcamento.total_centimos : 0;
  html += `<div class="orc-total"><strong>Total: ${formatarCentimos(total)}</strong></div>`;
  html += '<div id="orc-erro" class="orc-erro"></div>';

  html += '<div class="orc-acoes">';
  if(editavel){
    html += `<button class="btn-secundario" onclick="orcGuardarRascunho(${pedidoId})">💾 Guardar rascunho</button>`;
    html += `<button class="btn-primario" onclick="orcEnviar(${pedidoId})">📤 Enviar orçamento</button>`;
  }
  html += `<a class="btn-secundario" href="${waLink}" target="_blank" rel="noopener">💬 Contactar cliente</a>`;
  html += `<button class="btn-perigo" onclick="pedidoRecusar(${pedidoId})">❌ Recusar pedido</button>`;
  html += '</div>';
  html += '<div class="linha" style="color:var(--muted);font-size:12px;margin-top:8px;">"Contactar cliente" abre o WhatsApp diretamente — é sempre uma alternativa; o envio do orçamento pelo botão acima é o método principal.</div>';

  secao.innerHTML = html;
}

function orcErro(msg){
  const el = document.getElementById('orc-erro');
  if(el) el.textContent = msg || '';
}

async function orcPedirJson(url, opcoes){
  try {
    const resp = await fetch(url, opcoes);
    const dados = await resp.json().catch(() => ({}));
    if(!resp.ok){ orcErro(dados.erro || 'Ocorreu um erro.'); return null; }
    return dados;
  } catch(e){ orcErro('Falha de rede.'); return null; }
}

async function orcAdicionarLinha(pedidoId){
  const descricao = document.getElementById('orc-nova-desc').value.trim();
  const quantidade = parseInt(document.getElementById('orc-nova-qtd').value, 10);
  const precoChf = parseFloat(document.getElementById('orc-nova-preco').value);
  if(!descricao){ orcErro('Indique uma descrição.'); return; }
  const dados = await orcPedirJson('/api/pedidos/' + pedidoId + '/orcamento/linhas', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({descricao, quantidade, preco_centimos: Math.round((precoChf||0)*100)}),
  });
  if(dados) await carregarOrcamento(pedidoId, pedidoAtualTelefone);
}

async function orcGuardarLinha(pedidoId, linhaId){
  const tr = document.querySelector(`tr[data-linha-id="${linhaId}"]`);
  const descricao = tr.querySelector('.orc-desc').value.trim();
  const quantidade = parseInt(tr.querySelector('.orc-qtd').value, 10);
  const precoChf = parseFloat(tr.querySelector('.orc-preco').value);
  if(!descricao){ orcErro('Indique uma descrição.'); return; }
  const dados = await orcPedirJson('/api/pedidos/' + pedidoId + '/orcamento/linhas/' + linhaId, {
    method: 'PUT', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({descricao, quantidade, preco_centimos: Math.round((precoChf||0)*100)}),
  });
  if(dados) await carregarOrcamento(pedidoId, pedidoAtualTelefone);
}

async function orcRemoverLinha(pedidoId, linhaId){
  const dados = await orcPedirJson('/api/pedidos/' + pedidoId + '/orcamento/linhas/' + linhaId, {method: 'DELETE'});
  if(dados) await carregarOrcamento(pedidoId, pedidoAtualTelefone);
}

async function orcGuardarRascunho(pedidoId){
  const descontoChf = parseFloat(document.getElementById('orc-desconto').value) || 0;
  const observacoes = document.getElementById('orc-observacoes').value;
  const validade = parseInt(document.getElementById('orc-validade').value, 10) || 14;
  const dados = await orcPedirJson('/api/pedidos/' + pedidoId + '/orcamento/rascunho', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({desconto_centimos: Math.round(descontoChf*100), observacoes, validade_dias: validade}),
  });
  if(dados) await carregarOrcamento(pedidoId, pedidoAtualTelefone);
}

async function orcEnviar(pedidoId){
  if(!confirm('Enviar este orçamento ao cliente agora, pelo WhatsApp?')) return;
  const dados = await orcPedirJson('/api/pedidos/' + pedidoId + '/orcamento/enviar', {method: 'POST'});
  if(dados){
    await carregarOrcamento(pedidoId, pedidoAtualTelefone);
    document.getElementById('pedido-estado-atual').textContent = 'orçamento enviado';
    }
}

async function pedidoRecusar(pedidoId){
  if(!confirm('Recusar este pedido e avisar o cliente?')) return;
  const dados = await orcPedirJson('/api/pedidos/' + pedidoId + '/recusar', {method: 'POST'});
  if(dados){
    document.getElementById('pedido-estado-atual').textContent = dados.estado;
    }
}

function fecharModal(){
  document.getElementById('modal-pedido').classList.remove('aberto');
  pedidoAtualId = null;
  if(location.hash.startsWith('#pedido-')) history.replaceState(null, '', location.pathname);
}

function fecharModalSeExterior(event){
  if(event.target.id === 'modal-pedido') fecharModal();
}

function abrirLightbox(src){
  document.getElementById('lightbox-img').src = src;
  document.getElementById('lightbox').classList.add('aberto');
}

// ===========================================================================
// CALENDÁRIO (só consulta) — vanilla JS, sem dependências nem CDNs
// ===========================================================================
// Os valores da grelha vêm do servidor (CALENDARIO_HORA_INICIO/FIM/INTERVALO
// em bot.py), com estes como reserva caso a API ainda não tenha respondido.
let CAL_INICIO = 8, CAL_FIM = 19, CAL_PASSO = 30;

// ---------------------------------------------------------------------------
// Altura de cada intervalo da grelha — JÁ NÃO É FIXA.
// A semana inteira (08:00–19:00, de segunda a domingo) tem de caber no ecrã
// sem obrigar a fazer scroll: a altura de cada faixa é calculada a partir do
// espaço que sobra realmente abaixo da barra do calendário, e recalculada
// sempre que a janela muda de tamanho ou se muda de vista.
// ---------------------------------------------------------------------------
// Estes valores estão em "pixels a 16px de rem". A grelha é desenhada em JS,
// por isso não herda o `rem` da folha de estilos automaticamente: escalaRem()
// lê a escala atual (que acompanha a largura do browser, ver html{font-size})
// e multiplica-os, para a grelha crescer e encolher como o resto da página.
const CAL_ALTURA_FAIXA_OMISSAO = 28;   // valor de arranque, antes de medir
const CAL_ALTURA_FAIXA_MIN = 14;       // abaixo disto deixa de ser legível
const CAL_ALTURA_FAIXA_MAX = 34;       // acima disto só ficava esticado
let CAL_ALTURA_FAIXA = CAL_ALTURA_FAIXA_OMISSAO;

function escalaRem(){
  const base = parseFloat(getComputedStyle(document.documentElement).fontSize);
  return (base && isFinite(base) ? base : 16) / 16;
}
const emEscala = px => px * escalaRem();
const alturaPorMinuto = () => CAL_ALTURA_FAIXA / CAL_PASSO;
const calNumeroDeFaixas = () => Math.round((CAL_FIM - CAL_INICIO) * 60 / CAL_PASSO);

// Ajusta a altura das faixas ao espaço disponível. Devolve true quando a
// grelha teve mesmo de ficar mais apertada do que o mínimo legível — nesse
// caso (janela muito baixa) é a GRELHA que ganha scroll interno, nunca a
// página inteira é que passa a ser preciso percorrer para ver a semana.
function calAjustarAlturaFaixa(){
  const cont = document.getElementById('cal-conteudo');
  if(!cont){ return false; }
  if(calVista === 'mes'){
    CAL_ALTURA_FAIXA = emEscala(CAL_ALTURA_FAIXA_OMISSAO);
    cont.style.maxHeight = '';
    document.documentElement.style.setProperty('--faixa', CAL_ALTURA_FAIXA + 'px');
    return false;
  }
  const faixas = calNumeroDeFaixas();
  const topo = cont.getBoundingClientRect().top;      // já em coordenadas do ecrã
  const cabecalhoDias = emEscala(28);                 // linha "seg 31/08 · hoje"
  const respiro = emEscala(24);                       // padding do cartão + folga
  const disponivel = window.innerHeight - topo - cabecalhoDias - respiro;
  const alvo = Math.floor(disponivel / faixas);
  const minimo = emEscala(CAL_ALTURA_FAIXA_MIN), maximo = emEscala(CAL_ALTURA_FAIXA_MAX);
  const apertado = alvo < minimo;
  CAL_ALTURA_FAIXA = Math.max(minimo, Math.min(maximo, alvo || emEscala(CAL_ALTURA_FAIXA_OMISSAO)));
  document.documentElement.style.setProperty('--faixa', CAL_ALTURA_FAIXA.toFixed(2) + 'px');
  // Só quando nem o mínimo cabe é que a grelha passa a ter scroll próprio.
  cont.style.maxHeight = apertado ? Math.max(emEscala(220), disponivel + cabecalhoDias) + 'px' : '';
  return apertado;
}

// O ESTADO nunca é comunicado só pela cor (a cor identifica o SERVIÇO):
// cada evento leva sempre o nome do estado em texto, em todas as vistas.
const CAL_ESTADOS = [
  {id: 'confirmed', nome: 'Confirmada',     cor: '#3878e8', classe: 'est-confirmado'},
  {id: 'pending',   nome: 'A aprovar',      cor: '#d4a017', classe: 'est-confirmado'},
  {id: 'completed', nome: 'Concluída',      cor: '#2ea05a', classe: 'est-concluido'},
  {id: 'no_show',   nome: 'Não compareceu', cor: '#9678c8', classe: 'est-reagendado'},
  {id: 'cancelled', nome: 'Cancelada',      cor: '#e05252', classe: 'est-cancelado',
   rotuloFiltro: 'Canceladas (horário livre)'},
];
// Mapa de estados LEGADOS (português) -> canónico, para dados antigos em cache.
const ESTADO_LEGADO = {confirmado:'confirmed', confirmada:'confirmed', pendente:'pending',
  concluido:'completed', concluida:'completed', cancelado:'cancelled', cancelada:'cancelled',
  reagendado:'cancelled', reagendada:'cancelled'};
// Cor do indicador "Agora" — laranja quente, deliberadamente DIFERENTE do
// vermelho dos cancelamentos, para nunca se confundirem.
const COR_AGORA = '#ff7a59';

// --- Estado da marcação vs. disponibilidade do horário ---------------------
// São duas coisas distintas e são sempre comunicadas em separado: a cor diz
// o SERVIÇO, o texto diz o ESTADO, e um terceiro texto (com ícone e borda
// próprios) diz se o horário está BLOQUEADO ou LIVRE.
function evCancelado(ev){
  return chaveEstado(ev.estado) === 'cancelled';
}
function evBloqueiaHorario(ev){
  if(typeof ev.bloqueia_horario === 'boolean') return ev.bloqueia_horario;
  const chave = chaveEstado(ev.estado);
  if(chave === 'confirmed' || chave === 'completed' || chave === 'pending') return true;
  if(chave === 'cancelled') return Number(ev.bloqueia_horario || 0) === 1;
  return false;
}
// '' (marcação ativa normal) | 'bloqueado' | 'livre'
function classeDisponibilidade(ev){
  if(!evCancelado(ev)) return '';
  return evBloqueiaHorario(ev) ? 'bloqueado' : 'livre';
}
// Frase completa — usada onde há espaço: tooltip, pré-visualização, painel
// de detalhes, cartões em cascata e vista de mês.
function textoDisponibilidade(ev){
  const classe = classeDisponibilidade(ev);
  if(classe === 'bloqueado') return '🔒 Cancelado · Horário bloqueado';
  if(classe === 'livre')     return '🔓 Cancelado · Horário livre';
  return '';
}
// Forma curta para o cartão da grelha: um bloco de 45min/1h não tem altura
// para a frase inteira e cortá-la a meio era pior. O "Cancelado" continua
// ali mesmo por cima, no crachá de estado, por isso o cartão diz na mesma
// as duas coisas — estado e disponibilidade — sem nada truncado.
function textoDisponibilidadeCurto(ev){
  const classe = classeDisponibilidade(ev);
  if(classe === 'bloqueado') return '🔒 Horário bloqueado';
  if(classe === 'livre')     return '🔓 Horário livre';
  return '';
}
// Preenchido a partir da API (mapa central CORES_SERVICOS em bot.py) — nunca
// gerado ao acaso, por isso a cor de um serviço é sempre a mesma.
let CAL_CORES_SERVICOS = {};
let CAL_COR_OMISSAO = '#8b95a6';
function corDoEvento(ev){
  return ev.cor || CAL_CORES_SERVICOS[ev.servico] || CAL_COR_OMISSAO;
}
// fundo translúcido + barra sólida: bom contraste no tema escuro, com o
// texto (hora, cliente, serviço, preço) sempre legível por cima.
function estiloCorEvento(ev){
  const cor = corDoEvento(ev);
  // Cancelado: o cartão passa a vermelho (bloqueado) ou a vermelho tracejado
  // (livre) pelas classes CSS, mas a cor do SERVIÇO continua visível numa
  // faixa lateral alimentada por esta variável — nunca se perde.
  if(evCancelado(ev)) return '--cor-servico:' + cor + ';';
  return 'background:' + cor + '33;border-left:3px solid ' + cor + ';';
}
const DIAS_CURTOS = ['seg', 'ter', 'qua', 'qui', 'sex', 'sáb', 'dom'];
const MESES = ['janeiro','fevereiro','março','abril','maio','junho',
               'julho','agosto','setembro','outubro','novembro','dezembro'];

let calVista = 'semana';                          // semana (por defeito) | dia | mes
let calAncora = new Date();
let calFiltros = {confirmed: true, pending: true, completed: true, no_show: false, cancelled: false};
let calEventos = [];                              // eventos do intervalo atual
const calPorId = new Map();                       // cache id -> evento (dossiê)

// Aceita canónico (EN), legado (PT) e acentos; devolve sempre o canónico.
function chaveEstado(estado){
  let limpo = String(estado || '').normalize('NFD').replace(/[\u0300-\u036f]/g, '')
    .trim().toLowerCase().replace(/-/g, '_');
  if(ESTADO_LEGADO[limpo]) limpo = ESTADO_LEGADO[limpo];
  return CAL_ESTADOS.some(e => e.id === limpo) ? limpo : 'confirmed';
}
function infoEstado(estado){
  const chave = chaveEstado(estado);
  return CAL_ESTADOS.find(e => e.id === chave);
}

function ymd(d){
  return d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0')
         + '-' + String(d.getDate()).padStart(2, '0');
}
function deYmd(s){
  const [a, m, d] = String(s).split('-').map(Number);
  return new Date(a, m - 1, d);
}
function maisDias(d, n){
  const nova = new Date(d.getTime());
  nova.setDate(nova.getDate() + n);
  return nova;
}
// A semana começa sempre à SEGUNDA-feira.
function inicioSemana(d){
  const nova = new Date(d.getFullYear(), d.getMonth(), d.getDate());
  const desvio = (nova.getDay() + 6) % 7;
  return maisDias(nova, -desvio);
}
function minutosDeIso(iso){
  const hm = String(iso).split('T')[1] || '00:00';
  const [h, m] = hm.split(':').map(Number);
  return h * 60 + m;
}
function hhmmDeIso(iso){
  return (String(iso).split('T')[1] || '').slice(0, 5);
}
function ecraPequeno(){
  return window.matchMedia('(max-width: 720px)').matches;
}
function temHover(){
  return window.matchMedia('(hover: hover) and (pointer: fine)').matches;
}

// --- intervalo pedido à API, conforme a vista atual ------------------------
function calIntervalo(){
  if(calVista === 'dia'){
    const d = ymd(calAncora);
    return {inicio: d, fim: d};
  }
  if(calVista === 'semana'){
    const ini = inicioSemana(calAncora);
    return {inicio: ymd(ini), fim: ymd(maisDias(ini, 6))};
  }
  const primeiro = new Date(calAncora.getFullYear(), calAncora.getMonth(), 1);
  const ultimo = new Date(calAncora.getFullYear(), calAncora.getMonth() + 1, 0);
  // a grelha do mês mostra semanas completas (segunda a domingo)
  return {inicio: ymd(inicioSemana(primeiro)), fim: ymd(maisDias(inicioSemana(ultimo), 6))};
}

function calTextoPeriodo(){
  if(calVista === 'dia'){
    return DIAS_CURTOS[(calAncora.getDay() + 6) % 7] + ', ' + calAncora.getDate() + ' de '
           + MESES[calAncora.getMonth()] + ' ' + calAncora.getFullYear();
  }
  if(calVista === 'semana'){
    const ini = inicioSemana(calAncora), fim = maisDias(ini, 6);
    const mesmoMes = ini.getMonth() === fim.getMonth();
    return ini.getDate() + (mesmoMes ? '' : ' ' + MESES[ini.getMonth()]) + ' – '
           + fim.getDate() + ' ' + MESES[fim.getMonth()] + ' ' + fim.getFullYear();
  }
  return MESES[calAncora.getMonth()][0].toUpperCase() + MESES[calAncora.getMonth()].slice(1)
         + ' ' + calAncora.getFullYear();
}

// --- controlos -------------------------------------------------------------
function calMudarVista(vista){
  if(calVista === vista) return;
  calVista = vista;
  calCarregar();
}
function calNavegar(sentido){
  if(calVista === 'dia') calAncora = maisDias(calAncora, sentido);
  else if(calVista === 'semana') calAncora = maisDias(calAncora, 7 * sentido);
  else calAncora = new Date(calAncora.getFullYear(), calAncora.getMonth() + sentido, 1);
  calCarregar();
}
function calHoje(){
  calAncora = new Date();
  calCarregar();
}
function calAlternarFiltro(estado, ligado){
  calFiltros[estado] = ligado;
  calDesenhar();                                  // filtrar não precisa de ir à API
}

function calDesenharControlos(){
  document.querySelectorAll('#cal-vistas .cal-btn').forEach(b => {
    b.classList.toggle('ativo', b.dataset.vista === calVista);
  });
  document.getElementById('cal-periodo').textContent = calTextoPeriodo();

  const filtros = document.getElementById('cal-filtros');
  if(!filtros.dataset.pronto){
    filtros.innerHTML = CAL_ESTADOS.map(e =>
      '<label class="cal-filtro" title="' + esc(e.id === 'cancelled'
          ? 'As marcações canceladas que CONTINUAM a bloquear o horário aparecem sempre — se não '
            + 'aparecessem, o calendário mostrava como livre um horário que está ocupado.'
          : 'Mostrar/ocultar marcações neste estado.')
      + '"><input type="checkbox" data-estado="' + e.id + '"'
      + (calFiltros[e.id] ? ' checked' : '') + '> ' + esc(e.rotuloFiltro || e.nome) + '</label>').join('');
    filtros.querySelectorAll('input').forEach(inp => {
      inp.addEventListener('change', () => calAlternarFiltro(inp.dataset.estado, inp.checked));
    });
    filtros.dataset.pronto = '1';
    document.getElementById('cal-legenda').innerHTML = CAL_ESTADOS.map(e =>
      '<span><i class="cal-ponto" style="background:' + e.cor + '"></i>' + esc(e.nome) + '</span>').join('')
      + '<span title="Cancelada, mas o horário continua ocupado">🔒 Horário bloqueado</span>'
      + '<span title="Cancelada e o horário voltou a ficar disponível">🔓 Horário livre</span>'
      + '<span><i class="cal-ponto" style="background:' + COR_AGORA + '"></i>Agora</span>';
  }
}

function desenharLegendaServicos(){
  const alvo = document.getElementById('cal-legenda-servicos');
  const nomes = Object.keys(CAL_CORES_SERVICOS);
  if(!nomes.length){ alvo.innerHTML = ''; return; }
  alvo.innerHTML = '<strong style="color:var(--text);">Cores dos serviços:</strong> '
    + nomes.map(nome => '<span><i class="cal-ponto" style="background:'
        + esc(CAL_CORES_SERVICOS[nome]) + '"></i>' + esc(nome) + '</span>').join('');
}

// ===========================================================================
// DEFINIÇÕES — "Ao cancelar uma marcação: libertar automaticamente o horário"
// ===========================================================================
// O valor vive na base de dados (tabela `configuracoes`), por isso sobrevive
// a um refresh do painel e a um reinício do servidor. Aqui em JavaScript é
// só um espelho do que o servidor respondeu — nunca a fonte de verdade.
let defLibertarAoCancelar = true;      // mesmo valor por omissão do servidor

function aplicarConfiguracoes(cfg){
  if(!cfg) return;
  if(typeof cfg.libertar_horario_ao_cancelar === 'boolean'){
    defLibertarAoCancelar = cfg.libertar_horario_ao_cancelar;
  }
  const campo = document.getElementById('def-libertar');
  if(campo) campo.checked = defLibertarAoCancelar;
  const estado = document.getElementById('def-estado');
  if(estado){
    estado.textContent = defLibertarAoCancelar
      ? '✅ Ligado — ao cancelar, o horário fica automaticamente livre para novas reservas.'
      : '🔒 Desligado — ao cancelar, o horário continua ocupado até ser libertado à mão.';
  }
}

async function carregarConfiguracoes(){
  try {
    const resp = await fetch('/api/configuracoes');
    if(!resp.ok) throw new Error('HTTP ' + resp.status);
    const dados = await resp.json();
    aplicarConfiguracoes(dados.configuracoes);
  } catch(e){
    const estado = document.getElementById('def-estado');
    if(estado) estado.textContent = '❌ Não foi possível carregar as definições (' + e.message + ').';
  }
}

async function guardarDefinicaoLibertar(valor){
  const campo = document.getElementById('def-libertar');
  const estado = document.getElementById('def-estado');
  if(estado) estado.textContent = 'A guardar…';
  try {
    const resp = await fetch('/api/configuracoes', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({libertar_horario_ao_cancelar: valor}),
    });
    const dados = await resp.json().catch(() => ({}));
    if(!resp.ok) throw new Error(dados.erro || ('HTTP ' + resp.status));
    aplicarConfiguracoes(dados.configuracoes);
    mostrarToast('Definição guardada.', 'ok');
  } catch(e){
    // Reverte o interruptor: o que manda é o servidor, não o browser.
    if(campo) campo.checked = defLibertarAoCancelar;
    if(estado) estado.textContent = '❌ Não foi possível guardar (' + e.message + ').';
    mostrarToast('Não foi possível guardar a definição.', 'erro');
  }
}

(function ligarDefinicoes(){
  const campo = document.getElementById('def-libertar');
  if(campo) campo.addEventListener('change', () => guardarDefinicaoLibertar(campo.checked));
})();

// --- carregamento ----------------------------------------------------------
// Uma navegação NUNCA é ignorada por já haver outro pedido a decorrer: cada
// pedido leva um número de sequência e o anterior é abortado. Só a resposta
// mais recente pode escrever no título, nos eventos, na grelha, no calPorId
// e no painel — assim o refresh automático nunca substitui uma navegação
// mais recente por dados antigos.
let calSequencia = 0;
let calPedidoEmCurso = null;

async function calCarregar(manual){
  calDesenharControlos();          // o título muda já, sem esperar pela rede
  const meuNumero = ++calSequencia;
  if(calPedidoEmCurso) calPedidoEmCurso.abort();
  const controlador = new AbortController();
  calPedidoEmCurso = controlador;

  const conteudo = document.getElementById('cal-conteudo');
  const erro = document.getElementById('cal-erro');
  if(manual || !calEventos.length){
    conteudo.innerHTML = '<div class="cal-carregando">A carregar o calendário…</div>';
  }
  const {inicio, fim} = calIntervalo();
  try {
    const resp = await fetch('/api/calendario?inicio=' + inicio + '&fim=' + fim,
                             {signal: controlador.signal});
    if(!resp.ok) throw new Error('HTTP ' + resp.status);
    const dados = await resp.json();
    if(meuNumero !== calSequencia) return;      // chegou tarde: já há navegação mais recente
    if(dados.grelha){
      CAL_INICIO = dados.grelha.hora_inicio;
      CAL_FIM = dados.grelha.hora_fim;
      CAL_PASSO = dados.grelha.intervalo_min;
    }
    if(dados.configuracoes) aplicarConfiguracoes(dados.configuracoes);
    if(dados.cores_servicos){
      CAL_CORES_SERVICOS = dados.cores_servicos;
      CAL_COR_OMISSAO = dados.cor_omissao || CAL_COR_OMISSAO;
      desenharLegendaServicos();
    }
    // substitui sempre a lista inteira -> nunca duplica ao atualizar
    calEventos = dados.eventos || [];
    calEventos.forEach(ev => calPorId.set(String(ev.id), ev));
    erro.hidden = true;
    const aviso = document.getElementById('cal-aviso');
    if(dados.invalidos){
      aviso.textContent = '⚠️ ' + dados.invalidos + ' marcação(ões) com data, hora ou duração inválida não '
                        + 'foi possível colocar no calendário — continuam visíveis na tabela de agendamentos.';
      aviso.hidden = false;
    } else {
      aviso.hidden = true;
    }
    calDesenhar();
  } catch(e){
    if(e.name === 'AbortError' || meuNumero !== calSequencia) return;   // substituído: silêncio
    erro.textContent = '❌ Não foi possível carregar o calendário (' + e.message + '). Tente Atualizar.';
    erro.hidden = false;
    if(!calEventos.length) conteudo.innerHTML = '<div class="vazio">Sem dados para mostrar.</div>';
  } finally {
    if(calPedidoEmCurso === controlador) calPedidoEmCurso = null;
  }
}

function calEventosVisiveis(){
  return calEventos.filter(ev => {
    // Uma marcação cancelada que CONTINUA a bloquear o horário aparece
    // sempre: esconde-la seria mostrar como livre um horário que não está.
    if(evCancelado(ev) && evBloqueiaHorario(ev)) return true;
    // Cancelada e libertada: fora da vista por defeito, só com o filtro.
    return calFiltros[chaveEstado(ev.estado)];
  });
}

function calDesenhar(){
  calDesenharControlos();
  const conteudo = document.getElementById('cal-conteudo');
  const visiveis = calEventosVisiveis();
  if(calVista === 'mes'){
    calAjustarAlturaFaixa();
    conteudo.innerHTML = calHtmlMes(visiveis);
  } else if(ecraPequeno() && calVista === 'semana'){
    // No telemóvel a semana em grelha seria ilegível — e já não é preciso
    // repeti-la aqui em lista, porque a coluna de reservas (que num ecrã
    // estreito fica logo por baixo) mostra exatamente as mesmas marcações,
    // em cascata. Evita-se assim ter a mesma lista duas vezes na página.
    conteudo.style.maxHeight = '';
    conteudo.innerHTML = '<div class="cal-nota">Semana em lista, na coluna de reservas aqui abaixo.<br>'
                       + 'Toque em <strong>Dia</strong> para ver a grelha horária.</div>';
  } else {
    calAjustarAlturaFaixa();
    conteudo.innerHTML = calHtmlGrelha(visiveis);
  }
  desenharCascata(visiveis);
  calLigarEventos();
  calDesenharLinhaAgora();
}

// --- coluna de reservas (cartões em cascata), sempre presente --------------
// Mesmos eventos, mesmos filtros e as mesmas regras visuais da grelha: cor do
// serviço nas ativas, cartão vermelho com cadeado nas canceladas que
// bloqueiam, cartão vermelho tracejado nas libertadas. O scroll é interno à
// coluna — a página nunca cresce por causa dela.
function desenharCascata(visiveis){
  const alvo = document.getElementById('cascata');
  if(!alvo) return;
  const posicao = alvo.scrollTop;        // não perder o sítio ao redesenhar
  alvo.innerHTML = calHtmlAgenda(visiveis || calEventosVisiveis());
  alvo.scrollTop = posicao;
}

// --- vista semana / dia (grelha horária) -----------------------------------
function diasDaVista(){
  if(calVista === 'dia') return [new Date(calAncora.getFullYear(), calAncora.getMonth(), calAncora.getDate())];
  const ini = inicioSemana(calAncora);
  return Array.from({length: 7}, (_, i) => maisDias(ini, i));
}

// Reparte eventos sobrepostos por colunas, para nenhum tapar o outro.
function calDisporSobrepostos(eventos){
  const ordenados = eventos.slice().sort((a, b) => minutosDeIso(a.inicio) - minutosDeIso(b.inicio));
  const colunas = [];   // fim (min) de cada coluna
  const postos = [];
  let grupo = [], grupoFim = -1;
  const fecharGrupo = () => {
    const total = Math.max(1, colunas.length);
    grupo.forEach(p => { p.total = total; });
    grupo = []; colunas.length = 0; grupoFim = -1;
  };
  ordenados.forEach(ev => {
    const ini = minutosDeIso(ev.inicio);
    const fim = Math.max(ini + 15, minutosDeIso(ev.fim) || (ini + 60));
    if(grupo.length && ini >= grupoFim) fecharGrupo();
    let coluna = colunas.findIndex(f => f <= ini);
    if(coluna === -1){ colunas.push(fim); coluna = colunas.length - 1; }
    else { colunas[coluna] = fim; }
    const posto = {ev, ini, fim, coluna, total: 1};
    postos.push(posto); grupo.push(posto);
    grupoFim = Math.max(grupoFim, fim);
  });
  if(grupo.length) fecharGrupo();
  return postos;
}

// `altura` é a altura real do cartão em px na grelha (undefined na cascata,
// onde a altura é livre). Com a grelha comprimida para a semana caber no
// ecrã, um bloco de 45min pode ficar com poucos pixels: em vez de deixar o
// texto transbordar ou ser cortado a meio, o cartão mostra menos linhas —
// mas a HORA, o NOME e o SERVIÇO estão sempre lá, nem que seja tudo na
// mesma linha.
function calHtmlEvento(ev, estilo, classeExtra, altura, estreito){
  const info = infoEstado(ev.estado);
  const disp = classeDisponibilidade(ev);
  // nos cartões em cascata a altura é livre -> cabe a frase toda
  const cascata = (classeExtra || '').indexOf('cal-agenda-ev') !== -1;
  const total = ev.total_centimos != null ? formatarCentimos(ev.total_centimos)
              : (ev.preco_por_confirmar ? 'A confirmar' : '');
  const hora = esc(ev.dia_inteiro ? 'Dia inteiro' : hhmmDeIso(ev.inicio));
  const quem = esc(ev.primeiro_nome || ev.telefone || '');
  const servico = esc(ev.servico || '');
  const linha2 = [servico, esc(ev.duracao || '')].filter(Boolean).join(' · ');
  const cadeado = disp === 'bloqueado' ? '🔒 ' : disp === 'livre' ? '🔓 ' : '';

  const abre = '<div class="cal-evento ' + info.classe + ' ' + disp + ' ' + (classeExtra || '')
       + (!cascata && altura !== undefined && altura < emEscala(34) ? ' compacto' : '') + '" style="'
       + estiloCorEvento(ev) + (estilo || '') + '"'
       + ' data-id="' + esc(ev.id) + '" tabindex="0" role="button"'
       + ' title="' + esc(hhmmDeIso(ev.inicio) + ' · ' + (ev.nome || ev.telefone || '') + ' · '
                          + (ev.servico || '') + ' · ' + (ev.duracao || '') + ' · ' + info.nome
                          + (disp ? ' · ' + textoDisponibilidade(ev).replace(/^\S+\s/, '') : '')) + '">';

  // muito baixo: uma linha só, com hora + nome + serviço
  if(!cascata && altura !== undefined && altura < emEscala(34)){
    return abre + '<div class="ev-t">' + cadeado + hora + ' · ' + quem
         + (servico ? ' · ' + servico : '') + '</div></div>';
  }
  // baixo: hora + nome (com estado) e o serviço
  // Num cartão ESTREITO (marcações sobrepostas na mesma hora) a hora, o nome
  // e o estado não cabem na mesma linha sem cortar a hora a meio — por isso
  // o crachá do estado passa para uma linha própria, em vez de espremer
  // tudo. Nos cartões de largura normal fica tudo na primeira linha.
  const crachar = '<span class="ev-badge">' + esc(info.nome) + '</span>';
  const cabeca = estreito
    ? '<div class="ev-t"><span class="ev-quem">' + hora + ' · ' + quem + '</span></div>'
      + '<div class="ev-estado">' + crachar + '</div>'
    : '<div class="ev-t"><span class="ev-quem">' + hora + ' · ' + quem + '</span>' + crachar + '</div>';
  const corpo = cabeca
       + '<div class="ev-s">' + (cadeado && altura !== undefined && altura < emEscala(52) ? cadeado : '') + linha2 + '</div>';
  if(!cascata && altura !== undefined && altura < emEscala(52)){
    return abre + corpo + '</div>';
  }
  // altura normal: o SERVIÇO vem sempre antes da linha de disponibilidade, e
  // num cartão cancelado o total é omitido — continua no painel de detalhes
  // e na pré-visualização.
  return abre + corpo
       + (disp ? '<div class="ev-disp">' + esc(cascata ? textoDisponibilidade(ev)
                                                       : textoDisponibilidadeCurto(ev)) + '</div>' : '')
       + (total && !disp ? '<div class="ev-s">' + esc(total) + '</div>' : '')
       + '</div>';
}

function calHtmlGrelha(eventos){
  const dias = diasDaVista();
  const faixas = Math.round((CAL_FIM - CAL_INICIO) * 60 / CAL_PASSO);
  let horas = '<div class="cal-horas">';
  for(let i = 0; i < faixas; i++){
    const minuto = CAL_INICIO * 60 + i * CAL_PASSO;
    const rotulo = (minuto % 60 === 0)
      ? String(Math.floor(minuto / 60)).padStart(2, '0') + ':00' : '';
    horas += '<div class="cal-hora">' + rotulo + '</div>';
  }
  horas += '</div>';

  const hojeYmd = ymd(new Date());
  let cabecalhos = '<div class="cal-cab cal-cab-hora"></div>';
  let colunas = '';
  dias.forEach(d => {
    const chave = ymd(d);
    const eHoje = (chave === hojeYmd);
    cabecalhos += '<div class="cal-cab' + (eHoje ? ' hoje' : '') + '" data-dia="' + chave + '">'
                + DIAS_CURTOS[(d.getDay() + 6) % 7] + ' ' + d.getDate() + '/'
                + String(d.getMonth() + 1).padStart(2, '0')
                + (eHoje ? '<span class="cab-hoje">hoje</span>' : '') + '</div>';

    // Os eventos de dia inteiro já vêm do servidor a começar no início da
    // grelha e com a duração dela toda (ver evento_calendario) — por isso
    // são posicionados exatamente como os outros, ocupando a coluna inteira.
    const doDia = eventos.filter(ev => ev.dia === chave);
    // Uma marcação cancelada cujo horário foi LIBERTADO não pode ocupar o
    // slot como uma reserva ativa: fica de fora da disposição em colunas
    // (não comprime nem empurra nada) e é desenhada como uma tira estreita
    // encostada à direita, deixando o horário visualmente livre.
    const ocupam = doDia.filter(ev => classeDisponibilidade(ev) !== 'livre');
    const libertados = doDia.filter(ev => classeDisponibilidade(ev) === 'livre');

    let celulas = '';
    for(let i = 0; i < faixas; i++){
      const cheia = ((CAL_INICIO * 60 + i * CAL_PASSO) % 60 === 0);
      celulas += '<div class="cal-faixa' + (cheia ? ' hora-cheia' : '') + '"></div>';
    }
    const geometria = (ini, fim) => {
      const topo = Math.max(0, (ini - CAL_INICIO * 60)) * alturaPorMinuto();
      const alturaMax = faixas * CAL_ALTURA_FAIXA - topo;
      const altura = Math.max(Math.min(emEscala(24), CAL_ALTURA_FAIXA),
        Math.min((fim - Math.max(ini, CAL_INICIO * 60)) * alturaPorMinuto(), alturaMax));
      return {topo: topo, altura: altura};
    };
    let blocos = '';
    calDisporSobrepostos(ocupam).forEach(p => {
      const g = geometria(p.ini, p.fim);
      const largura = 100 / p.total;
      blocos += calHtmlEvento(p.ev,
        'top:' + g.topo.toFixed(1) + 'px;height:' + g.altura.toFixed(1) + 'px;left:calc(' + (largura * p.coluna).toFixed(3)
        + '% + 2px);width:calc(' + largura.toFixed(3) + '% - 4px);', '', g.altura, p.total > 1);
    });
    libertados.forEach(ev => {
      const ini = minutosDeIso(ev.inicio);
      const g = geometria(ini, Math.max(ini + 15, minutosDeIso(ev.fim)));
      blocos += calHtmlEvento(ev, 'top:' + g.topo.toFixed(1) + 'px;height:' + g.altura.toFixed(1)
        + 'px;right:2px;width:44%;z-index:4;', '', g.altura, true);
    });
    colunas += '<div class="cal-coluna" data-dia="' + chave + '">' + celulas + blocos + '</div>';
  });

  const largura = calVista === 'dia' ? '58px 1fr' : '58px repeat(7, minmax(84px, 1fr))';
  return '<div class="cal-grelha ' + (calVista === 'dia' ? 'dia' : '') + '" '
       + 'style="grid-template-columns:' + largura + ';grid-auto-rows:min-content;">'
       + cabecalhos + horas + colunas + '</div>'
       + (eventos.length ? '' : '<div class="vazio">Sem marcações neste período.</div>');
}

// --- vista mês -------------------------------------------------------------
function calHtmlMes(eventos){
  const primeiro = new Date(calAncora.getFullYear(), calAncora.getMonth(), 1);
  const inicio = inicioSemana(primeiro);
  const hojeYmd = ymd(new Date());
  const porDia = {};
  eventos.forEach(ev => { (porDia[ev.dia] = porDia[ev.dia] || []).push(ev); });

  let html = '<div class="cal-mes">' + DIAS_CURTOS.map(d =>
    '<div class="cal-mes-cab">' + d + '</div>').join('');
  for(let i = 0; i < 42; i++){
    const d = maisDias(inicio, i);
    const chave = ymd(d);
    const fora = d.getMonth() !== calAncora.getMonth();
    const doDia = (porDia[chave] || []).slice().sort((a, b) => a.inicio.localeCompare(b.inicio));
    const visiveis = doDia.slice(0, 3);
    html += '<div class="cal-mes-cel' + (fora ? ' fora' : '') + (chave === hojeYmd ? ' hoje' : '') + '">'
          + '<div class="cal-mes-num">' + d.getDate() + '</div>'
          + visiveis.map(ev => {
              const disp = classeDisponibilidade(ev);
              const marca = disp === 'bloqueado' ? '🔒 ' : disp === 'livre' ? '🔓 ' : '';
              return '<div class="cal-mes-ev ' + infoEstado(ev.estado).classe + ' ' + disp + '" style="'
              + estiloCorEvento(ev) + '" data-id="' + esc(ev.id) + '" tabindex="0" role="button" title="'
              + esc((ev.servico || '') + ' · ' + infoEstado(ev.estado).nome
                    + (disp ? ' · ' + (disp === 'bloqueado' ? 'Horário bloqueado' : 'Horário livre') : '')) + '">'
              + marca + esc(ev.dia_inteiro ? '' : hhmmDeIso(ev.inicio) + ' ')
              + esc(ev.primeiro_nome || ev.telefone || '')
              + '<span class="ev-badge">' + esc(infoEstado(ev.estado).nome) + '</span></div>';
            }).join('')
          + (doDia.length > 3 ? '<div class="cal-mes-mais">+' + (doDia.length - 3) + '</div>' : '')
          + '</div>';
  }
  html += '</div>';
  return html + (eventos.length ? '' : '<div class="vazio">Sem marcações neste mês.</div>');
}

// --- coluna de reservas: cartões em cascata (telemóvel / agenda vertical) --
// Exatamente as mesmas regras da grelha: serviço ativo com a cor
// predefinida do serviço; cancelado e bloqueado como cartão vermelho com
// cadeado; cancelado e livre como cartão vermelho transparente e tracejado;
// e sempre a frase "Horário livre"/"Horário bloqueado" escrita no cartão.
// Respeita os mesmos filtros do calendário (recebe já calEventosVisiveis()).
function calHtmlAgenda(eventos){
  if(!eventos.length) return '<div class="vazio">Sem marcações neste período.</div>';
  const porDia = {};
  eventos.forEach(ev => { (porDia[ev.dia] = porDia[ev.dia] || []).push(ev); });
  const hojeYmd = ymd(new Date());
  return Object.keys(porDia).sort().map(dia => {
    const d = deYmd(dia);
    const lista = porDia[dia].slice().sort((a, b) => a.inicio.localeCompare(b.inicio));
    const eHoje = (dia === hojeYmd);
    let cartoes = '';
    let agoraColocado = !(eHoje && agoraDentroDaGrelha());
    const minutosAgora = minutosDoDiaAgora();
    lista.forEach(ev => {
      // No telemóvel não há grelha horária: o "Agora" entra como separador
      // discreto, exatamente entre o cartão anterior e o seguinte.
      if(!agoraColocado && minutosDeIso(ev.inicio) >= minutosAgora){
        cartoes += htmlAgoraAgenda();
        agoraColocado = true;
      }
      cartoes += calHtmlEvento(ev, 'position:relative;', 'cal-agenda-ev');
    });
    if(!agoraColocado) cartoes += htmlAgoraAgenda();
    return '<div class="cal-agenda-dia"><h4>' + DIAS_CURTOS[(d.getDay() + 6) % 7] + ' · '
         + d.getDate() + ' ' + MESES[d.getMonth()] + (eHoje ? ' · hoje' : '') + '</h4>'
         + cartoes + '</div>';
  }).join('');
}

// ===========================================================================
// INDICADOR DA HORA ATUAL — "Agora · HH:MM"
// ===========================================================================
// Uma linha fina a toda a largura da coluna do dia de hoje, com uma etiqueta
// que diz a horas são. Regras:
//   • só existe na coluna do DIA DE HOJE;
//   • desaparece por completo quando a semana/dia/mês em vista não contém
//     hoje, e quando a hora atual está fora do horário da grelha;
//   • nunca deixa pontos nem traços soltos: é sempre removida antes de ser
//     redesenhada, e não é desenhada de todo quando não se aplica;
//   • não se confunde com uma marcação (sem fundo de cartão, sem cliques,
//     cor laranja própria, fora da disposição dos eventos);
//   • atualiza-se sozinha a cada minuto, sem recarregar a página.
function minutosDoDiaAgora(){
  const agora = new Date();
  return agora.getHours() * 60 + agora.getMinutes();
}
function agoraDentroDaGrelha(){
  const minutos = minutosDoDiaAgora();
  return minutos >= CAL_INICIO * 60 && minutos <= CAL_FIM * 60;
}
function horaAgoraTexto(){
  const agora = new Date();
  return String(agora.getHours()).padStart(2, '0') + ':' + String(agora.getMinutes()).padStart(2, '0');
}
function htmlAgoraAgenda(){
  return '<div class="cal-agora-agenda">Agora · ' + esc(horaAgoraTexto()) + '</div>';
}
function limparLinhaAgora(){
  document.querySelectorAll('#cal-conteudo .cal-agora, #cascata .cal-agora').forEach(el => el.remove());
}

function calDesenharLinhaAgora(){
  limparLinhaAgora();                       // nunca fica um traço antigo para trás
  if(calVista === 'mes' || ecraPequeno()) return;   // o mês e o telemóvel não têm grelha horária
  if(!agoraDentroDaGrelha()) return;                // fora do horário mostrado
  const coluna = document.querySelector('.cal-coluna[data-dia="' + ymd(new Date()) + '"]');
  if(!coluna) return;                               // a vista atual não contém hoje
  const linha = document.createElement('div');
  linha.className = 'cal-agora';
  linha.setAttribute('aria-hidden', 'true');
  linha.style.top = ((minutosDoDiaAgora() - CAL_INICIO * 60) * alturaPorMinuto()).toFixed(1) + 'px';
  const etiqueta = document.createElement('span');
  etiqueta.className = 'agora-etiqueta';
  etiqueta.textContent = 'Agora · ' + horaAgoraTexto();
  linha.appendChild(etiqueta);
  coluna.appendChild(linha);
}

// A cada minuto: reposiciona a linha (sem recarregar a página nem ir à rede)
// e, no telemóvel, redesenha a agenda para o separador "Agora" acompanhar.
function calTicAgora(){
  calDesenharLinhaAgora();                 // grelha (não faz nada se não se aplica)
  // A coluna de reservas leva o mesmo separador "Agora · HH:MM"; só se
  // redesenha quando há mesmo um "Agora" em jogo — ou já lá está um
  // separador (que pode ter de sair), ou hoje está à vista e dentro do
  // horário. Caso contrário não se mexe em nada.
  const marcador = document.querySelector('.cal-agora-agenda');
  const hojeAVista = agoraDentroDaGrelha()
    && calEventosVisiveis().some(ev => ev.dia === ymd(new Date()));
  if(marcador || hojeAVista) desenharCascata();
}
setInterval(calTicAgora, 60000);

// --- interação: hover (computador) e clique/toque ---------------------------
function calLigarEventos(){
  document.querySelectorAll('#cal-conteudo [data-id], #cascata [data-id]').forEach(el => {
    el.addEventListener('click', () => abrirPainelAgendamento(el.dataset.id));
    el.addEventListener('keydown', e => {
      if(e.key === 'Enter' || e.key === ' '){ e.preventDefault(); abrirPainelAgendamento(el.dataset.id); }
    });
    if(temHover()){
      el.addEventListener('mouseenter', () => mostrarPreview(el.dataset.id, el));
      el.addEventListener('mouseleave', fecharPreviewComAtraso);
    }
  });
}

// A pré-visualização é clicável (pointer-events:auto no CSS) e faz "ponte":
// só fecha com um pequeno atraso DEPOIS de o cursor sair do evento E da
// própria janela — caso contrário era impossível chegar aos botões.
let previewTemporizador = null;
let previewId = null;

function cancelarFechoPreview(){
  if(previewTemporizador){ clearTimeout(previewTemporizador); previewTemporizador = null; }
}
function fecharPreviewComAtraso(){
  cancelarFechoPreview();
  previewTemporizador = setTimeout(esconderPreview, 320);
}
function esconderPreview(){
  cancelarFechoPreview();
  previewId = null;
  document.getElementById('cal-preview').hidden = true;
}

function mostrarPreview(id, elemento){
  const ev = calPorId.get(String(id));
  if(!ev) return;
  cancelarFechoPreview();
  previewId = String(id);
  const caixa = document.getElementById('cal-preview');
  const info = infoEstado(ev.estado);
  const cor = corDoEvento(ev);
  const p = ev.pedido || {};
  const foto = (p.fotografias && p.fotografias.length)
    ? '<img src="/media/' + encodeURIComponent(p.fotografias[0].nome_ficheiro) + '" alt="">' : '';
  const veiculo = p.veiculo
    ? '<div>🚗 ' + esc(p.veiculo) + (p.ano_veiculo ? ' (' + esc(p.ano_veiculo) + ')' : '') + '</div>' : '';
  const podeAgir = (chaveEstado(ev.estado) === 'confirmed' || chaveEstado(ev.estado) === 'pending');
  caixa.innerHTML =
      '<div class="pv-t"><i class="cal-ponto" style="background:' + esc(cor) + '"></i> '
    + esc(ev.nome || ev.telefone || '') + '</div>'
    + '<div>' + esc(ev.servico || '-') + (ev.extra ? ' + ' + esc(ev.extra) : '') + '</div>'
    + '<div>📅 ' + esc(ev.data || '') + ' · ' + esc(ev.hora_hhmm || ev.hora || '') + '</div>'
    + '<div>⏱️ ' + esc(ev.duracao || '-') + '</div>'
    + '<div>💰 ' + esc(formatarCentimos(ev.total_centimos)) + '</div>'
    + '<div><span class="estado-chip" style="background:' + info.cor + '33;color:' + info.cor + '">'
    + esc(info.nome) + '</span>' + (textoDisponibilidade(ev)
        ? ' <span class="estado-chip" style="background:rgba(224,82,82,.18);color:#f2a3a3">'
          + esc(textoDisponibilidade(ev)) + '</span>' : '')
    + '</div>' + veiculo + foto
    + '<div class="pv-acoes">'
    + '<button data-acao="detalhes">📋 Ver detalhes</button>'
    + (podeAgir ? '<button data-acao="reagendar">✏️ Alterar/Reagendar</button>'
                + '<button class="perigo" data-acao="cancelar">❌ Cancelar marcação</button>' : '')
    + '</div>';
  // Os botões param a propagação para não dispararem o clique do evento.
  caixa.querySelectorAll('.pv-acoes button').forEach(b => {
    b.addEventListener('click', e => {
      e.preventDefault(); e.stopPropagation();
      const acao = b.dataset.acao;
      esconderPreview();
      if(acao === 'detalhes') abrirPainelAgendamento(id);
      else if(acao === 'reagendar') abrirDialogoReagendar(id);
      else if(acao === 'cancelar') abrirDialogoCancelar(id);
    });
  });
  caixa.hidden = false;
  posicionarPreview(elemento);
}

// Posicionada JUNTO ao evento (não segue o cursor), para o rato conseguir
// chegar lá sem a janela fugir.
function posicionarPreview(elemento){
  const caixa = document.getElementById('cal-preview');
  const r = elemento.getBoundingClientRect();
  const margem = emEscala(10);
  const largura = caixa.offsetWidth || emEscala(300), altura = caixa.offsetHeight || emEscala(200);
  let x = r.right + margem, y = r.top;
  if(x + largura > window.innerWidth - 8) x = Math.max(8, r.left - largura - margem);
  if(y + altura > window.innerHeight - 8) y = Math.max(8, window.innerHeight - altura - 8);
  caixa.style.left = x + 'px';
  caixa.style.top = y + 'px';
}

(function ligarPonteHoverPreview(){
  const caixa = document.getElementById('cal-preview');
  caixa.addEventListener('mouseenter', cancelarFechoPreview);
  caixa.addEventListener('mouseleave', fecharPreviewComAtraso);
})();

// Fecha com Escape ou com um clique fora (a rede de segurança do scroll
// mantém-se: a janela nunca pode ficar presa no ecrã).
document.addEventListener('click', e => {
  const caixa = document.getElementById('cal-preview');
  if(caixa.hidden) return;
  if(!caixa.contains(e.target)
     && !e.target.closest('#cal-conteudo [data-id], #cascata [data-id]')) esconderPreview();
});
document.addEventListener('scroll', esconderPreview, true);
window.addEventListener('blur', esconderPreview);

// ===========================================================================
// Dossiê da marcação — painel lateral ÚNICO, usado pelo calendário e pela
// tabela de agendamentos (nada de HTML nem lógica duplicada)
// ===========================================================================
async function obterEvento(id){
  const chave = String(id);
  if(calPorId.has(chave)) return calPorId.get(chave);
  // pode ser uma marcação fora do intervalo em vista (ex.: vinda da tabela)
  const resp = await fetch('/api/calendario');
  if(!resp.ok) return null;
  const dados = await resp.json();
  (dados.eventos || []).forEach(ev => calPorId.set(String(ev.id), ev));
  return calPorId.get(chave) || null;
}

async function abrirPainelAgendamento(id){
  esconderPreview();
  const painel = document.getElementById('painel-ag');
  const corpo = document.getElementById('painel-corpo');
  document.getElementById('painel-titulo').textContent = 'Marcação #' + id;
  corpo.innerHTML = '<div class="cal-carregando">A carregar…</div>';
  painel.classList.add('aberto');
  painel.setAttribute('aria-hidden', 'false');
  document.getElementById('painel-fundo').classList.add('aberto');
  if(location.hash !== '#agendamento-' + id) history.replaceState(null, '', '#agendamento-' + id);

  const ev = await obterEvento(id);
  if(!ev){
    corpo.innerHTML = '<div class="orc-erro">Marcação não encontrada.</div>';
    return;
  }
  const info = infoEstado(ev.estado);
  const tel = String(ev.telefone || '').replace(/[^0-9]/g, '');
  const p = ev.pedido;

  let html = '<div class="linha"><span class="estado-chip" style="background:' + info.cor + '33;color:'
           + info.cor + '">' + esc(info.nome) + '</span>'
           + (textoDisponibilidade(ev)
               ? ' <span class="estado-chip" style="background:rgba(224,82,82,.18);color:#f2a3a3">'
                 + esc(textoDisponibilidade(ev)) + '</span>' : '')
           + '</div>';
  html += '<div class="linha">👤 ' + esc(ev.nome || 'sem nome') + '</div>';
  html += '<div class="linha">📱 <a href="tel:+' + esc(tel) + '">+' + esc(tel) + '</a></div>';
  html += '<div class="painel-acoes"><a href="https://wa.me/' + esc(tel) + '" target="_blank" rel="noopener">'
        + '💬 Contactar no WhatsApp</a></div>';
  // As MESMAS ações da pré-visualização, aqui sempre disponíveis — é assim
  // que telemóvel e tablet lhes chegam, sem depender de hover.
  if((chaveEstado(ev.estado) === 'confirmed' || chaveEstado(ev.estado) === 'pending')){
    html += '<div class="pv-acoes" id="painel-acoes-marcacao">'
          + '<button data-acao="reagendar">✏️ Alterar/Reagendar</button>'
          + '<button class="perigo" data-acao="cancelar">❌ Cancelar marcação</button>'
          + '</div>';
  }
  html += '<h4>Marcação</h4>';
  html += '<div class="linha">📅 ' + esc(ev.data || '-') + '</div>';
  html += '<div class="linha">🕘 ' + esc(ev.hora_hhmm || ev.hora || '-') + '</div>';
  html += '<div class="linha">⏱️ ' + esc(ev.duracao || '-') + (ev.dia_inteiro ? ' (dia inteiro)' : '') + '</div>';
  html += '<div class="linha">🔧 ' + esc(ev.servico || '-') + '</div>';
  if(ev.extra) html += '<div class="linha">➕ ' + esc(ev.extra) + '</div>';

  if(ev.carrinho && ev.carrinho.length){
    html += '<h4>Discriminação</h4>';
    ev.carrinho.forEach(l => {
      const preco = (l.preco || 0) * (l.quantidade || 1);
      html += '<div class="linha" style="color:var(--muted);">• ' + esc(l.nome) + ': '
            + esc(formatarCentimos(preco)) + '</div>';
    });
  }
  html += '<div class="linha"><strong>💰 Total: ' + esc(formatarCentimos(ev.total_centimos)) + '</strong></div>';
  html += '<div class="linha" style="color:var(--muted);">🕓 Criado em '
        + esc(ev.criado_em ? new Date(ev.criado_em).toLocaleString('pt-PT') : '-') + '</div>';

  // Secções do pedido associado — escondidas por completo quando não existem.
  if(p && (p.veiculo || p.ano_veiculo || p.tipo_wrap || p.cor_acabamento)){
    html += '<h4>Veículo e Wrap (pedido #' + esc(p.id) + ')</h4>';
    if(p.veiculo) html += '<div class="linha">🚗 ' + esc(p.veiculo)
                        + (p.ano_veiculo ? ' (' + esc(p.ano_veiculo) + ')' : '') + '</div>';
    if(p.tipo_wrap) html += '<div class="linha">🎨 ' + esc(p.tipo_wrap) + '</div>';
    if(p.cor_acabamento) html += '<div class="linha">🖌️ ' + esc(p.cor_acabamento) + '</div>';
  }
  if(p && p.fotografias && p.fotografias.length){
    html += '<h4>Fotografias (' + p.fotografias.length + ')</h4><div class="galeria">';
    p.fotografias.forEach(f => {
      const src = '/media/' + encodeURIComponent(f.nome_ficheiro);
      html += '<img src="' + src + '" onclick="abrirLightbox(\'' + src + '\')">';
    });
    html += '</div>';
  }
  corpo.innerHTML = html;
  const acoes = document.getElementById('painel-acoes-marcacao');
  if(acoes){
    acoes.querySelectorAll('button').forEach(b => b.addEventListener('click', () => {
      if(b.dataset.acao === 'reagendar') abrirDialogoReagendar(ev.id);
      else abrirDialogoCancelar(ev.id);
    }));
  }
}

function fecharPainel(){
  const painel = document.getElementById('painel-ag');
  painel.classList.remove('aberto');
  painel.setAttribute('aria-hidden', 'true');
  document.getElementById('painel-fundo').classList.remove('aberto');
  if(location.hash.indexOf('#agendamento-') === 0) history.replaceState(null, '', location.pathname);
}
document.addEventListener('keydown', e => {
  if(e.key === 'Escape'){ esconderPreview(); fecharDialogo(); fecharPainel(); }
});

// ===========================================================================
// AÇÕES DO PAINEL — cancelar e reagendar (as duas únicas ações de escrita)
// ===========================================================================
// Nenhuma delas atua logo: abrem primeiro um diálogo de confirmação com os
// dados da marcação. O servidor revalida sempre estado, data, hora e
// conflitos (ver /api/agendamentos/<id>/cancelar e .../reagendar).
function fecharDialogo(){
  document.getElementById('dlg-fundo').classList.remove('aberto');
}
function fecharDialogoSeExterior(e){
  if(e.target.id === 'dlg-fundo') fecharDialogo();
}
function abrirDialogo(titulo, corpoHtml, acoesHtml){
  document.getElementById('dlg-titulo').textContent = titulo;
  document.getElementById('dlg-corpo').innerHTML = corpoHtml;
  document.getElementById('dlg-acoes').innerHTML = acoesHtml;
  document.getElementById('dlg-fundo').classList.add('aberto');
}
function erroDialogo(msg){
  const el = document.getElementById('dlg-erro-msg');
  if(el) el.textContent = msg || '';
}

function mostrarToast(texto, tipo){
  const antigo = document.querySelector('.toast');
  if(antigo) antigo.remove();
  const el = document.createElement('div');
  el.className = 'toast ' + (tipo || 'ok');
  el.textContent = texto;
  document.body.appendChild(el);
  setTimeout(() => el.remove(), 7000);
}

// Depois de cancelar/reagendar, atualiza TUDO o que mostra este horário, sem
// recarregar a página inteira: a grelha do calendário, os cartões em cascata
// (a mesma função calDesenhar desenha os dois), a tabela de agendamentos e o
// painel de detalhes, se estiver aberto. A disponibilidade que o WhatsApp
// apresenta não precisa de ser "atualizada" aqui: é calculada no servidor a
// cada passo (ver horarios_livres_para_sessao), a partir desta mesma
// verificação — assim que o horário é libertado, volta a ser oferecido.
async function atualizarTudoApos(id, evento){
  if(evento) calPorId.set(String(evento.id), evento);
  await calCarregar(false);         // calendário + coluna de reservas em cascata
  await carregar();                 // tabela de agendamentos
  const painel = document.getElementById('painel-ag');
  if(painel.classList.contains('aberto')) await abrirPainelAgendamento(id);
}

// --- Cancelar --------------------------------------------------------------
// O cancelamento NUNCA acontece ao abrir este diálogo: primeiro pergunta-se
// o que fazer ao horário (pré-selecionado com a configuração guardada) e só
// depois de "Confirmar cancelamento" é que alguma coisa muda.
async function abrirDialogoCancelar(id){
  const ev = await obterEvento(id);
  if(!ev){ mostrarToast('Marcação não encontrada.', 'erro'); return; }
  const libertarPorOmissao = defLibertarAoCancelar;
  abrirDialogo('Cancelar marcação #' + esc(ev.id),
    '<div class="linha">👤 ' + esc(ev.nome || ev.telefone || '') + '</div>'
    + '<div class="linha">🔧 ' + esc(ev.servico || '-') + '</div>'
    + '<div class="linha">📅 ' + esc(ev.data || '-') + '</div>'
    + '<div class="linha">🕘 ' + esc(ev.hora_hhmm || ev.hora || '-') + '</div>'
    + '<div class="linha">🆔 Marcação #' + esc(ev.id) + '</div>'
    + '<div class="dlg-escolha">'
    +   '<strong>O que deseja fazer com este horário?</strong>'
    +   '<label class="dlg-opcao' + (libertarPorOmissao ? ' escolhida' : '') + '" data-valor="1">'
    +     '<input type="radio" name="dlg-horario" value="1"' + (libertarPorOmissao ? ' checked' : '') + '>'
    +     '<span>✅ Libertar o horário'
    +       '<span class="op-desc">A marcação fica no histórico como cancelada e o horário volta a '
    +       'ficar disponível para novas reservas.</span></span>'
    +   '</label>'
    +   '<label class="dlg-opcao' + (libertarPorOmissao ? '' : ' escolhida') + '" data-valor="0">'
    +     '<input type="radio" name="dlg-horario" value="0"' + (libertarPorOmissao ? '' : ' checked') + '>'
    +     '<span>🔒 Manter o horário ocupado'
    +       '<span class="op-desc">A marcação fica cancelada mas continua a ocupar este horário e a '
    +       'impedir novas reservas.</span></span>'
    +   '</label>'
    +   '<label class="dlg-padrao"><input type="checkbox" id="dlg-guardar-padrao"> '
    +     'Guardar esta escolha como padrão</label>'
    + '</div>'
    + '<div class="dlg-aviso">O cliente será notificado, se ainda for possível enviar-lhe mensagem. '
    + 'A marcação nunca é apagada: fica sempre no histórico.</div>'
    + '<div class="dlg-erro" id="dlg-erro-msg"></div>',
    '<button onclick="fecharDialogo()">Voltar</button>'
    + '<button class="perigo" id="dlg-confirmar">Confirmar cancelamento</button>');

  const opcoes = document.querySelectorAll('#dlg-corpo .dlg-opcao');
  opcoes.forEach(op => op.addEventListener('change', () => {
    opcoes.forEach(o => o.classList.toggle('escolhida', o.querySelector('input').checked));
  }));
  document.getElementById('dlg-confirmar').addEventListener('click', () => confirmarCancelamento(ev.id));
}

async function confirmarCancelamento(id){
  const botao = document.getElementById('dlg-confirmar');
  const escolhido = document.querySelector('#dlg-corpo input[name="dlg-horario"]:checked');
  const libertar = escolhido ? escolhido.value === '1' : defLibertarAoCancelar;
  const guardarPadrao = !!(document.getElementById('dlg-guardar-padrao')
                        && document.getElementById('dlg-guardar-padrao').checked);
  botao.disabled = true; botao.textContent = 'A cancelar…';
  try {
    const resp = await fetch('/api/agendamentos/' + encodeURIComponent(id) + '/cancelar', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({libertar: libertar, guardar_padrao: guardarPadrao}),
    });
    const dados = await resp.json().catch(() => ({}));
    if(!resp.ok){
      erroDialogo(dados.erro || ('Não foi possível cancelar (HTTP ' + resp.status + ').'));
      botao.disabled = false; botao.textContent = 'Confirmar cancelamento';
      return;
    }
    fecharDialogo();
    // O servidor é que diz o que realmente aconteceu ao horário — nunca se
    // anuncia ao utilizador aquilo que só se pediu.
    const horario = dados.horario_libertado
      ? '🔓 O horário voltou a ficar disponível.' : '🔒 O horário continua ocupado.';
    const aviso = dados.cliente_notificado ? null
      : 'Marcação cancelada, mas não foi possível notificar automaticamente o cliente.';
    mostrarToast((aviso || 'Marcação cancelada e cliente notificado.') + ' ' + horario,
                 aviso ? 'aviso' : 'ok');
    if(dados.configuracoes) aplicarConfiguracoes(dados.configuracoes);
    await atualizarTudoApos(id, dados.evento);
  } catch(e){
    erroDialogo('Falha de rede: ' + e.message);
    botao.disabled = false; botao.textContent = 'Confirmar cancelamento';
  }
}

// --- Alterar / Reagendar ---------------------------------------------------
async function abrirDialogoReagendar(id){
  const ev = await obterEvento(id);
  if(!ev){ mostrarToast('Marcação não encontrada.', 'erro'); return; }
  abrirDialogo('Alterar/Reagendar #' + esc(ev.id),
    '<div class="linha">👤 ' + esc(ev.nome || ev.telefone || '') + '</div>'
    + '<div class="linha">🔧 ' + esc(ev.servico || '-') + ' · ⏱️ ' + esc(ev.duracao || '-') + '</div>'
    + '<div class="linha">📅 Atual: <strong>' + esc(ev.data || '-') + ' · '
    + esc(ev.hora_hhmm || ev.hora || '-') + '</strong></div>'
    + '<label for="dlg-data">Nova data</label>'
    + '<input type="date" id="dlg-data" value="' + esc(ev.dia) + '">'
    + '<label for="dlg-hora">Nova hora</label>'
    + '<input type="time" id="dlg-hora" step="300" value="' + esc(ev.hora_hhmm || '') + '">'
    + '<div class="dlg-aviso" id="dlg-resumo"></div>'
    + '<div class="dlg-erro" id="dlg-erro-msg"></div>',
    '<button onclick="fecharDialogo()">Voltar</button>'
    + '<button class="principal" id="dlg-confirmar">Confirmar alteração</button>');

  const campoData = document.getElementById('dlg-data');
  const campoHora = document.getElementById('dlg-hora');
  const resumo = document.getElementById('dlg-resumo');
  const atualizarResumo = () => {
    resumo.innerHTML = 'Antes: <strong>' + esc(ev.data || '-') + ' · '
      + esc(ev.hora_hhmm || '-') + '</strong><br>Depois: <strong>'
      + esc(campoData.value || '—') + ' · ' + esc(campoHora.value || '—') + '</strong><br>'
      + esc(ev.nome || '') + ' · ' + esc(ev.servico || '') + ' · ' + esc(ev.duracao || '')
      + '<br><em>A marcação só muda depois de confirmar.</em>';
  };
  campoData.addEventListener('input', atualizarResumo);
  campoHora.addEventListener('input', atualizarResumo);
  atualizarResumo();
  document.getElementById('dlg-confirmar').addEventListener('click',
    () => confirmarReagendamento(ev.id, campoData.value, campoHora.value));
}

async function confirmarReagendamento(id, data, hora){
  const botao = document.getElementById('dlg-confirmar');
  if(!data || !hora){ erroDialogo('Indique a nova data e a nova hora.'); return; }
  botao.disabled = true; botao.textContent = 'A alterar…';
  try {
    const resp = await fetch('/api/agendamentos/' + encodeURIComponent(id) + '/reagendar', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({data: data, hora: hora}),
    });
    const dados = await resp.json().catch(() => ({}));
    if(!resp.ok){
      erroDialogo(dados.erro || ('Não foi possível alterar (HTTP ' + resp.status + ').'));
      botao.disabled = false; botao.textContent = 'Confirmar alteração';
      return;
    }
    fecharDialogo();
    const aviso = dados.cliente_notificado ? null
      : 'Marcação alterada, mas não foi possível notificar automaticamente o cliente.';
    mostrarToast(aviso || 'Marcação alterada e cliente notificado.', aviso ? 'aviso' : 'ok');
    await atualizarTudoApos(id, dados.evento);
  } catch(e){
    erroDialogo('Falha de rede: ' + e.message);
    botao.disabled = false; botao.textContent = 'Confirmar alteração';
  }
}

// Abertura direta por /dashboard#agendamento-ID
function abrirAgendamentoPeloHash(){
  const m = /^#agendamento-(\d+)$/.exec(location.hash);
  if(m) abrirPainelAgendamento(m[1]);
}
window.addEventListener('hashchange', abrirAgendamentoPeloHash);

carregar();
carregarPedidos();
carregarConfiguracoes();
calCarregar(true).then(abrirAgendamentoPeloHash);
setInterval(carregar, 20000);
setInterval(carregarPedidos, 20000);
setInterval(() => calCarregar(false), 30000);
// Ao redimensionar, a altura das faixas é recalculada para a semana continuar
// a caber no ecrã (e o layout muda entre grelha e coluna única). Com um
// pequeno atraso, para não redesenhar a cada pixel arrastado.
let calRedimensionar = null;
window.addEventListener('resize', () => {
  clearTimeout(calRedimensionar);
  calRedimensionar = setTimeout(() => calDesenhar(), 120);
});

// Abre automaticamente o dossiê de um pedido quando se chega a esta página
// por uma ligação #pedido-<id> (ver link_dossie_pedido, usado na notificação
// interna "🔎 Analisar pedido").
function abrirPedidoPeloHash(){
  const m = /^#pedido-(\d+)$/.exec(location.hash);
  if(m) abrirPedido(parseInt(m[1], 10));
}
window.addEventListener('hashchange', abrirPedidoPeloHash);
window.addEventListener('load', abrirPedidoPeloHash);
abrirPedidoPeloHash();
</script>
</body>
</html>
"""


@app.route("/versao", methods=["GET"])
def versao():
    return jsonify(versao="daniela-beauty-v1.0", negocio=BUSINESS_NAME,
                   fluxo="servico->dia->hora->confirmar",
                   idiomas=list(IDIOMAS_VALIDOS),
                   servicos=[s["id"] for s in bd.listar_servicos()]), 200


@app.route("/webhook", methods=["GET"])
def verificar_webhook():
    """Handshake de verificação da Meta. Sem VERIFY_TOKEN configurado, recusa
    (falha fechado — nunca ecoa o challenge às cegas)."""
    if not VERIFY_TOKEN:
        return "VERIFY_TOKEN não configurado", 503
    if (request.args.get("hub.mode") == "subscribe"
            and request.args.get("hub.verify_token") == VERIFY_TOKEN):
        return request.args.get("hub.challenge", ""), 200
    return "Token inválido", 403


def verificar_assinatura(corpo_bruto: bytes) -> bool:
    """Valida o header X-Hub-Signature-256 (HMAC-SHA256 do corpo com APP_SECRET).

    • APP_SECRET definido -> assinatura é OBRIGATÓRIA e verificada
      (comparação em tempo constante). É a postura de produção.
    • APP_SECRET ausente -> aceita, mas AVISA no log. Só aceitável em
      desenvolvimento local.
    """
    if not APP_SECRET:
        print("[webhook] APP_SECRET não configurado — assinatura NÃO verificada (ok só em dev)")
        return True
    recebida = request.headers.get("X-Hub-Signature-256", "")
    if not recebida.startswith("sha256="):
        return False
    esperada = "sha256=" + hmac.new(APP_SECRET.encode(), corpo_bruto, hashlib.sha256).hexdigest()
    return hmac.compare_digest(esperada, recebida)


def sessao_preservando_perfil(sessao):
    """Novo dicionário de sessão limpo, mas mantendo nome e idioma do
    cliente — usado sempre que se reinicia/arquiva uma sessão, para nunca
    perder a personalização nem obrigar a escolher o idioma outra vez."""
    nova = {}
    if sessao.get("nome"):
        nova["nome"] = sessao["nome"]
    if sessao.get("idioma"):
        nova["idioma"] = sessao["idioma"]
    return nova


def reiniciar_sessao(de, manter_nome=True):
    """Reinicia a sessão preservando o perfil (nome/idioma). Qualquer pedido
    de orçamento que tenha ficado em "rascunho" é arquivado aqui — este é o
    ponto por onde passam CANCELAR, MENU, HUMANO, esvaziar carrinho e
    recomeçar, pelo que nenhum pedido abandonado fica visível como novo."""
    sessao_antiga = carregar_sessao(de)
    # cancelar, voltar ao menu, falar com a equipa ou esvaziar o carrinho
    # devolvem imediatamente ao mercado o horário que estivesse retido
    libertar_horario_retido(de)
    nova = sessao_preservando_perfil(sessao_antiga) if manter_nome else \
        ({"idioma": sessao_antiga["idioma"]} if sessao_antiga.get("idioma") else {})
    guardar_sessao(de, nova)
    return nova


def sessao_em_curso(sessao):
    """Considera-se 'em curso' se já escolheu categoria mas ainda não confirmou."""
    return bool(sessao.get("categoria") or sessao.get("fluxo"))


def processar_comando_texto(de, idioma, sessao, comando):
    if comando in COMANDOS_IDIOMA:
        enviar_seletor_idioma(de, idioma)
        return True
    if comando == "menu":
        nova = reiniciar_sessao(de)
        enviar_menu_principal(de, idioma, saudacao=True, sessao=nova)
        return True
    if comando == "ajuda":
        enviar_texto(de, mensagem_ajuda(idioma))
        return True
    if comando == "humano":
        falar_com_equipa(de, idioma, sessao)
        reiniciar_sessao(de)
        return True
    if comando == "gerir":
        mostrar_gestao_marcacao(de, idioma)
        return True
    if comando == "cancelar":
        cancelar_processo(de, idioma, sessao)
        return True
    if comando == "voltar":
        voltar_um_passo(de, idioma, sessao)
        return True
    return False


def voltar_um_passo(de, idioma, sessao):
    fluxo = sessao.get("fluxo")

    # --- FLUXO DANIELA BEAUTY: serviço -> dia -> hora -> resumo -----------
    if fluxo in ("beauty", "reagendar") and sessao.get("servico_id"):
        if "hora" in sessao:
            libertar_horario_retido(de)      # desfazer a hora liberta o horário
            sessao.pop("hora", None)
            guardar_sessao(de, sessao)
            passo_hora(de, idioma, sessao=sessao)
        elif "data" in sessao:
            sessao.pop("data", None)
            guardar_sessao(de, sessao)
            passo_data(de, idioma, sessao=sessao)
        elif fluxo == "reagendar":
            # Do 1.º passo do reagendamento, VOLTAR desiste (marcação intacta).
            enviar_texto(de, t("reagendar_ja_nao_valida", idioma))
            nova = reiniciar_sessao(de)
            enviar_menu_principal(de, idioma, saudacao=False, sessao=nova)
        else:
            # Do passo do serviço, VOLTAR regressa à lista de serviços.
            for c in ("servico_id", "servico", "duracao", "duracao_min", "preco", "preco_cents"):
                sessao.pop(c, None)
            guardar_sessao(de, sessao)
            iniciar_escolha_servico(de, idioma, sessao)
        return

    nova = reiniciar_sessao(de)
    enviar_menu_principal(de, idioma, saudacao=False, sessao=nova)


@app.after_request
def _drenar_eventos_apos_escrita(resposta):
    """Processa a outbox de eventos após qualquer request que possa ter
    gravado eventos (webhook, ações de escrita do painel). Síncrono em V1;
    passa a cron worker em V1.5. Nunca deixa uma exceção afetar a resposta."""
    try:
        p = request.path or ""
        if p == "/webhook" or (request.method == "POST" and (
                p.startswith("/api/agendamentos/") or p.startswith("/api/faturas"))):
            disparar_automacoes()
    except Exception:                        # noqa: BLE001
        log.exception("_drenar_eventos_apos_escrita")
    return resposta


@app.teardown_request
def _finalizar_idempotencia_webhook(exc):
    """Fecha a máquina de estados do wamid: sucesso -> 'processed' (retry
    descartado); exceção não tratada -> 'failed' (retry da Meta VOLTA a
    processar, sem se perder)."""
    wamid = request.environ.get("_webhook_wamid") if request else None
    if not wamid:
        return
    try:
        if exc is None:
            bd.confirmar_mensagem(wamid)
        else:
            bd.falhar_mensagem(wamid)
    except Exception:                        # noqa: BLE001
        log.exception("_finalizar_idempotencia_webhook")


@app.route("/webhook", methods=["POST"])
def receber_mensagem():
    corpo_bruto = request.get_data()
    if not verificar_assinatura(corpo_bruto):
        # Não revela porquê; a Meta nunca deve chegar aqui com APP_SECRET certo.
        return jsonify(status="assinatura invalida"), 403
    try:
        data = json.loads(corpo_bruto.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return jsonify(status="ignorado"), 200
    try:
        entry = data["entry"][0]["changes"][0]["value"]
        if "messages" not in entry:
            return jsonify(status="ignorado"), 200

        msg = entry["messages"][0]
        de = msg["from"]

        # IDEMPOTÊNCIA: reclama-se o wamid. 'duplicada' = já processada, ou a
        # ser processada agora por outro webhook -> ignora em silêncio. 'nova'
        # inclui um RETRY de um processamento que falhou antes (não se perde).
        # O resultado (processed/failed) é gravado no teardown do request.
        wamid = msg.get("id")
        request.environ["_webhook_wamid"] = wamid
        if wamid and bd.reclamar_mensagem(wamid) == "duplicada":
            request.environ["_webhook_wamid"] = None    # não confirmar de novo
            return jsonify(status="repetida"), 200

        # --- Ações INTERNAS da equipa ---------------------------------------
        # Processadas antes de qualquer carregamento/tratamento de sessão,
        # para a equipa nunca receber o menu normal do bot ao carregar numa
        # ação, e para a resposta dela nunca ser lida como mensagem de
        # cliente. Só são aceites vindas de PROVIDER_WHATSAPP: um cliente que
        # envie um destes IDs à mão não executa nada (as funções abaixo
        # verificam sempre numero_e_da_equipa e devolvem sem agir).
        id_interativo = None
        if msg.get("type") == "interactive":
            tipo_interativo = msg.get("interactive", {}).get("type")
            if tipo_interativo == "button_reply":
                id_interativo = msg["interactive"]["button_reply"]["id"]
            elif tipo_interativo == "list_reply":
                id_interativo = msg["interactive"]["list_reply"]["id"]

        if id_interativo:
            if processar_acao_equipa_marcacao(de, id_interativo):
                return jsonify(status="ok"), 200

        sessao = carregar_sessao(de)

        # Regista esta interação como "última mensagem do cliente" — só serve
        # para saber se ainda estamos dentro da janela de 24h de atendimento
        # da Meta (ver dentro_da_janela_24h), nunca para nada relacionado com
        # a resposta da equipa (já tratada e devolvida acima).
        registar_interacao_cliente(de)

        try:
            nome_perfil = entry["contacts"][0]["profile"]["name"]
            if nome_perfil:
                sessao["nome"] = nome_perfil
                guardar_sessao(de, sessao)
        except (KeyError, IndexError):
            pass

        tipo = msg.get("type")

        # --- Seleção de idioma: primeira interação obrigatória -------------
        # Enquanto o cliente não tiver um idioma guardado, nenhum menu ou
        # serviço é apresentado — só o seletor de idioma (nos 3 idiomas).
        # Clientes recorrentes (idioma já guardado na sessão) saltam isto.
        if sessao.get("idioma") not in IDIOMAS_VALIDOS:
            if tipo == "interactive" and msg["interactive"]["type"] == "button_reply" \
                    and msg["interactive"]["button_reply"]["id"] in LANG_IDS:
                novo_idioma = LANG_IDS[msg["interactive"]["button_reply"]["id"]]
                sessao["idioma"] = novo_idioma
                guardar_sessao(de, sessao)
                enviar_menu_principal(de, novo_idioma, saudacao=True, sessao=sessao)
            else:
                enviar_seletor_idioma(de)
            return jsonify(status="ok"), 200

        idioma = sessao["idioma"]

        # --- Texto livre: comandos permanentes, retomar sessão, ou 1ª msg ---
        if tipo == "text":
            texto = msg["text"]["body"].strip().lower()

            if texto in COMANDOS_TEXTO:
                processar_comando_texto(de, idioma, sessao, texto)
                return jsonify(status="ok"), 200

            if sessao.get("_a_confirmar_retomar"):
                sessao.pop("_a_confirmar_retomar", None)
                if texto in ("continuar", "sim"):
                    guardar_sessao(de, sessao)
                    reenviar_passo_atual(de, idioma, sessao)
                else:
                    nova = reiniciar_sessao(de)
                    enviar_menu_principal(de, idioma, saudacao=True, sessao=nova)
                return jsonify(status="ok"), 200

            # sessão em curso (categoria já escolhida, mas mensagem de texto inesperada)
            if sessao_em_curso(sessao):
                sessao["_a_confirmar_retomar"] = True
                guardar_sessao(de, sessao)
                botoes_retomar = [
                    {"id": "retomar_continuar", "titulo": t("botao_continuar", idioma)},
                    {"id": "retomar_recomecar", "titulo": t("botao_recomecar", idioma)},
                ]
                enviar_botoes(de, t("retomar_pergunta", idioma), botoes_retomar, idioma)
                return jsonify(status="ok"), 200

            # primeira mensagem / sem sessão em curso -> menu principal
            enviar_menu_principal(de, idioma, saudacao=True, sessao=sessao)
            return jsonify(status="ok"), 200

        # --- Respostas interativas (botões E listas) ----------------------
        # Uma só cadeia para os dois formatos: qualquer ID funciona tanto num
        # botão como numa linha de lista. É isto que permite promover um ecrã
        # de botões a lista (ver enviar_botoes) para lhe caber o ⬅️ Voltar,
        # sem ter de duplicar handlers. Um ID de lista que não seja
        # reconhecido aqui segue para a cadeia das listas, mais abaixo, que
        # trata os passos dependentes da posição no fluxo.
        if tipo == "interactive" and id_interativo is not None:
            id_botao = id_interativo

            # --- Aliases dos IDs canónicos "acao_*" (ver constantes ACAO_*) -
            # Botões NOVOS usam sempre estes IDs; os antigos equivalentes
            # (ex.: "menu_principal", "mp_marcar") continuam a funcionar tal e
            # qual, sem qualquer alteração — nunca foram removidos, só deixou
            # de se criar botões novos com eles.
            id_botao = {
                ACAO_MENU: "menu_principal",
                ACAO_NOVA_MARCACAO: "mp_marcar",
                ACAO_CANCELAR: ID_CANCELAR,
            }.get(id_botao, id_botao)

            # LEGADO (Spotless): botões dos fluxos Wrap / orçamento rápido /
            # negociação de orçamento. Já não existem para a Daniela Beauty.
            # Um cliente com uma mensagem antiga na conversa que carregue num
            # destes é levado em segurança ao menu — os handlers legados
            # abaixo ficam inalcançáveis de propósito.
            if _id_legado_spotless(id_botao):
                nova = reiniciar_sessao(de)
                enviar_menu_principal(de, idioma, saudacao=False, sessao=nova)
                return jsonify(status="ok"), 200

            if id_botao == ACAO_VOLTAR:
                voltar_um_passo(de, idioma, sessao)
                return jsonify(status="ok"), 200

            if id_botao == ACAO_GERIR:
                mostrar_gestao_marcacao(de, idioma)
                return jsonify(status="ok"), 200

            if id_botao == ACAO_HUMANO:
                falar_com_equipa(de, idioma, sessao)
                reiniciar_sessao(de)
                return jsonify(status="ok"), 200

            if id_botao == ACAO_IDIOMA:
                enviar_seletor_idioma(de, idioma)
                return jsonify(status="ok"), 200

            if id_botao == ACAO_MAIS:
                mostrar_mais_acoes(de, idioma, sessao)
                return jsonify(status="ok"), 200

            # --- Paginação visual de uma lista longa ----------------------
            if id_botao.startswith((ID_PAG_SEGUINTE, ID_PAG_ANTERIOR)):
                prefixo = ID_PAG_SEGUINTE if id_botao.startswith(ID_PAG_SEGUINTE) else ID_PAG_ANTERIOR
                try:
                    mudar_pagina_lista(de, idioma, sessao, int(id_botao[len(prefixo):]))
                except ValueError:
                    nao_entendi_com_opcoes(de, idioma, sessao)
                return jsonify(status="ok"), 200

            if id_botao in LANG_IDS:  # "Alterar idioma" com sessão já ativa
                # Limpa os campos do processo em curso (categoria, passos já
                # escolhidos, etc.) e preserva só o nome — para dados antigos
                # nunca fazerem o bot saltar etapas depois de mudar de idioma.
                # Um rascunho de pedido Wrap fica arquivado, para não sobrar
                # no painel como se fosse um pedido novo.
                novo_idioma = LANG_IDS[id_botao]
                sessao = sessao_preservando_perfil(sessao)
                sessao["idioma"] = novo_idioma
                guardar_sessao(de, sessao)
                enviar_menu_principal(de, novo_idioma, saudacao=True, sessao=sessao)
                return jsonify(status="ok"), 200

            if id_botao == ID_CANCELAR:
                cancelar_processo(de, idioma, sessao)
                return jsonify(status="ok"), 200

            if id_botao == "menu_principal":  # botão "Voltar ao menu" após um pedido recusado
                nova = reiniciar_sessao(de)
                enviar_menu_principal(de, idioma, saudacao=False, sessao=nova)
                return jsonify(status="ok"), 200

            if id_botao in ("retomar_continuar", "retomar_recomecar"):
                sessao.pop("_a_confirmar_retomar", None)
                if id_botao == "retomar_continuar":
                    guardar_sessao(de, sessao)
                    reenviar_passo_atual(de, idioma, sessao)
                else:
                    nova = reiniciar_sessao(de)
                    enviar_menu_principal(de, idioma, saudacao=True, sessao=nova)
                return jsonify(status="ok"), 200

            if id_botao == "mp_marcar":  # ex.: botão "Nova marcação" em "Gerir a minha marcação"
                iniciar_escolha_servico(de, idioma, sessao)
                return jsonify(status="ok"), 200

            # Escolha de um serviço Daniela Beauty (svc_<id>) — pode chegar como
            # botão OU como linha de lista; tratada nos dois sítios.
            if id_botao.startswith("svc_"):
                escolher_servico(de, idioma, sessao, id_botao[len("svc_"):])
                return jsonify(status="ok"), 200

            if id_botao == "confirmar":
                # Sessão obsoleta (marcação já feita / processo cancelado): sem
                # dados essenciais não há nada para gravar — volta ao menu.
                if not (sessao.get("data") and sessao.get("hora")
                        and (sessao.get("servico_id") or sessao.get("servico"))):
                    nova = reiniciar_sessao(de)
                    enviar_menu_principal(de, idioma, saudacao=False, sessao=nova)
                    return jsonify(status="ok"), 200

                # --- MODO REAGENDAMENTO — move a MESMA marcação -------------
                # A marcação atual mantém-se INTACTA até este ponto. Só aqui,
                # dentro de reagendar_agendamento (transação atómica, conflito
                # revalidado lá dentro), é que data/hora mudam. Se falhar, a
                # marcação antiga fica exatamente como estava.
                if sessao.get("fluxo") == "reagendar" and sessao.get("reagendar_id"):
                    id_ag = int(sessao["reagendar_id"])
                    d_iso = data_iso_de_texto(sessao.get("data"))
                    h_hhmm = hora_hhmm_de_texto(sessao.get("hora"))
                    try:
                        reagendar_agendamento(id_ag, d_iso, h_hhmm, origem="cliente",
                                              avisar_cliente=False)
                    except HorarioOcupado:
                        libertar_horario_retido(de)
                        sessao.pop("hora", None)
                        guardar_sessao(de, sessao)
                        enviar_texto(de, t("reagendar_ocupado", idioma))
                        passo_hora(de, idioma, sessao=sessao)
                        return jsonify(status="ok"), 200
                    except (EstadoInvalido, LookupError):
                        libertar_horario_retido(de)
                        reiniciar_sessao(de)
                        enviar_texto(de, t("reagendar_ja_nao_valida", idioma))
                        return jsonify(status="ok"), 200
                    libertar_horario_retido(de)
                    enviar_texto(de, t("reagendar_confirmado", idioma, id=id_ag,
                                       data=sessao["data"], hora=sessao["hora"]))
                    enviar_botoes(de, t("e_agora_pergunta", idioma), [
                        {"id": ACAO_GERIR, "titulo": t("botao_gerir_marcacao", idioma)},
                        {"id": ACAO_MENU, "titulo": t("botao_menu_principal", idioma)},
                    ], idioma)
                    # A notificação ao negócio é o evento booking.rescheduled
                    # (ver notifications.business.handler_evento) — drenado no
                    # after_request. Não se envia aqui, para não duplicar.
                    reiniciar_sessao(de)
                    return jsonify(status="ok"), 200

                # --- MODO NORMAL — nova marcação --------------------------
                # Última verificação, atómica com a gravação: entre o resumo e
                # este clique o horário pode ter sido ocupado por outro cliente.
                try:
                    id_ag = guardar_agendamento(de, sessao)
                except HorarioOcupado:
                    libertar_horario_retido(de)
                    sessao.pop("hora", None)
                    guardar_sessao(de, sessao)
                    enviar_texto(de, t("hora_entretanto_ocupada", idioma))
                    passo_hora(de, idioma, sessao=sessao)
                    return jsonify(status="ok"), 200
                # a retenção cumpriu o seu papel: agora há uma marcação a sério
                libertar_horario_retido(de)
                enviar_texto(de, mensagem_confirmacao_final(sessao, idioma))
                enviar_botoes(de, t("e_agora_pergunta", idioma), [
                    {"id": ACAO_GERIR, "titulo": t("botao_gerir_marcacao", idioma)},
                    {"id": ACAO_MENU, "titulo": t("botao_menu_principal", idioma)},
                ], idioma)
                # A notificação privada ao negócio é o evento booking.created
                # (ver _notificar_criacao_marcacao), drenado no after_request.
                # NÃO se envia aqui, para a marcação gerar exatamente UMA.
                reiniciar_sessao(de)
                return jsonify(status="ok"), 200

            if id_botao == "alterar":
                libertar_horario_retido(de)     # a hora vai ser reescolhida
                em_reagendamento = sessao.get("fluxo") == "reagendar" and sessao.get("reagendar_id")
                for campo in ("data", "hora", "extra"):
                    sessao.pop(campo, None)
                if not em_reagendamento:
                    for campo in ("servico_id", "servico", "preco", "preco_cents",
                                  "duracao", "duracao_min", "categoria"):
                        sessao.pop(campo, None)
                guardar_sessao(de, sessao)
                if em_reagendamento:
                    passo_data(de, idioma, sessao=sessao)      # mesmo serviço, nova data
                else:
                    iniciar_escolha_servico(de, idioma, sessao)
                return jsonify(status="ok"), 200

            # --- Marcação confirmada aberta a partir do carrinho -------------
            if id_botao.startswith("gerir_ag_"):
                mostrar_gestao_marcacao(de, idioma, int(id_botao.split("_")[-1]))
                return jsonify(status="ok"), 200

            if id_botao.startswith("reagendar_"):
                id_ag = int(id_botao.split("_")[-1])
                alvo = obter_agendamento(id_ag)
                if (not alvo or alvo.get("telefone") != de
                        or chave_estado(alvo.get("estado")) not in estados.GERIVEIS_PELO_CLIENTE):
                    enviar_texto(de, t("reagendar_ja_nao_valida", idioma))
                    enviar_menu_principal(de, idioma, saudacao=False, sessao=sessao)
                    return jsonify(status="ok"), 200
                # A marcação atual NÃO é tocada. Só escolhemos a nova data/hora;
                # o movimento acontece (atómico) ao confirmar (ver id_botao=="confirmar").
                sessao = sessao_preservando_perfil(sessao)
                sessao["fluxo"] = "reagendar"
                sessao["reagendar_id"] = id_ag
                sessao["servico_id"] = alvo.get("servico_id")
                sessao["servico"] = alvo.get("servico")
                sessao["duracao_min"] = alvo.get("duracao_min")
                sessao["duracao"] = (alvo.get("duracao")
                                     or catalogo.duracao_label(alvo.get("duracao_min")))
                sessao["preco_cents"] = alvo.get("preco_cents")
                sessao["preco"] = alvo.get("preco")
                sessao.pop("data", None)
                sessao.pop("hora", None)
                guardar_sessao(de, sessao)
                enviar_texto(de, t("reagendar_aviso", idioma))
                passo_data(de, idioma, sessao=sessao)
                return jsonify(status="ok"), 200

            if id_botao.startswith("cancelar_ag_"):
                id_ag = int(id_botao.split("_")[-1])
                # A decisão "libertar ou manter o horário" é do NEGÓCIO: aqui
                # aplica-se em silêncio a configuração guardada no painel.
                # A notificação ao negócio é o evento booking.cancelled.
                try:
                    marcar_agendamento_cancelado(id_ag, exigir_confirmado=False)
                except LookupError:
                    pass
                enviar_texto(de, t("cancelado_cliente", idioma))
                return jsonify(status="ok"), 200

            # Um BOTÃO que chegue aqui é mesmo desconhecido. Uma LISTA segue
            # para a cadeia seguinte, que trata os passos do fluxo (onde o
            # significado do ID depende do ponto em que a sessão está).
            if msg["interactive"]["type"] == "button_reply":
                nao_entendi_com_opcoes(de, idioma, sessao)
                return jsonify(status="ok"), 200

        # --- Listas: passos do fluxo (dependentes da posição na sessão) -----
        if tipo == "interactive" and msg["interactive"]["type"] == "list_reply":
            id_escolhido = msg["interactive"]["list_reply"]["id"]

            # --- Aliases dos IDs canónicos "acao_*" — ver o mesmo bloco no
            # dispatch de botões, acima, para a explicação completa.
            id_escolhido = {
                ACAO_MENU: "menu_principal",
                ACAO_NOVA_MARCACAO: "mp_marcar",
                ACAO_CANCELAR: ID_CANCELAR,
                ACAO_VOLTAR: ID_VOLTAR,
            }.get(id_escolhido, id_escolhido)

            # LEGADO (Spotless): linhas de lista dos fluxos Wrap/orçamento.
            if _id_legado_spotless(id_escolhido):
                nova = reiniciar_sessao(de)
                enviar_menu_principal(de, idioma, saudacao=False, sessao=nova)
                return jsonify(status="ok"), 200

            if id_escolhido == ID_CANCELAR:
                cancelar_processo(de, idioma, sessao)
                return jsonify(status="ok"), 200

            if id_escolhido == ID_VOLTAR:
                voltar_um_passo(de, idioma, sessao)
                return jsonify(status="ok"), 200

            if id_escolhido == "menu_principal":
                nova = reiniciar_sessao(de)
                enviar_menu_principal(de, idioma, saudacao=False, sessao=nova)
                return jsonify(status="ok"), 200

            if id_escolhido == ACAO_GERIR:
                mostrar_gestao_marcacao(de, idioma)
                return jsonify(status="ok"), 200

            if id_escolhido == ACAO_HUMANO:
                falar_com_equipa(de, idioma, sessao)
                reiniciar_sessao(de)
                return jsonify(status="ok"), 200

            if id_escolhido == ACAO_IDIOMA:
                enviar_seletor_idioma(de, idioma)
                return jsonify(status="ok"), 200

            if id_escolhido == ACAO_MAIS:
                mostrar_mais_acoes(de, idioma, sessao)
                return jsonify(status="ok"), 200

            # Menu principal
            if id_escolhido == "mp_marcar":
                iniciar_escolha_servico(de, idioma, sessao)
                return jsonify(status="ok"), 200
            if id_escolhido == "mp_gerir":
                mostrar_gestao_marcacao(de, idioma)
                return jsonify(status="ok"), 200
            if id_escolhido == "mp_humano":
                falar_com_equipa(de, idioma, sessao)
                reiniciar_sessao(de)
                return jsonify(status="ok"), 200
            if id_escolhido == "mp_idioma":
                enviar_seletor_idioma(de, idioma)
                return jsonify(status="ok"), 200

            if id_escolhido.startswith("gerir_ag_"):
                mostrar_gestao_marcacao(de, idioma, int(id_escolhido.split("_")[-1]))
                return jsonify(status="ok"), 200

            # --- FLUXO DANIELA BEAUTY (serviço -> dia -> hora) --------------
            # Escolha do serviço (chega como linha de lista: svc_<id>).
            if id_escolhido.startswith("svc_"):
                escolher_servico(de, idioma, sessao, id_escolhido[len("svc_"):])
                return jsonify(status="ok"), 200

            if sessao.get("fluxo") in ("beauty", "reagendar") and sessao.get("servico_id"):
                if "data" not in sessao:
                    sessao["data"] = msg["interactive"]["list_reply"]["title"]
                    guardar_sessao(de, sessao)
                    passo_hora(de, idioma, sessao=sessao)
                    return jsonify(status="ok"), 200
                if "hora" not in sessao:
                    sessao["hora"] = msg["interactive"]["list_reply"]["title"]
                    guardar_sessao(de, sessao)
                    # Horário RETIDO em nome deste cliente até confirmar.
                    reter_horario(de, sessao)
                    passo_resumo(de, idioma, sessao)
                    return jsonify(status="ok"), 200

            nao_entendi_com_opcoes(de, idioma, sessao)
            return jsonify(status="ok"), 200

        # --- Qualquer outro tipo (áudio, imagem fora de contexto, sticker, etc.) ---
        nao_entendi_com_opcoes(de, idioma, sessao)

    except (KeyError, IndexError):
        pass  # notificações de status (entregue/lido) chegam neste mesmo endpoint — ignora-as

    return jsonify(status="ok"), 200


def reenviar_passo_atual(de, idioma, sessao):
    """Reenvia o ecrã correspondente ao ponto exato onde a sessão ficou."""
    fluxo = sessao.get("fluxo")

    # --- FLUXO DANIELA BEAUTY -------------------------------------------
    if fluxo in ("beauty", "reagendar"):
        if not sessao.get("servico_id"):
            iniciar_escolha_servico(de, idioma, sessao)
        elif "hora" in sessao:
            passo_resumo(de, idioma, sessao)
        elif "data" in sessao:
            passo_hora(de, idioma, sessao=sessao)
        else:
            passo_data(de, idioma, sessao=sessao)
        return

    # Sem fluxo beauty em curso: leva ao menu.
    nova = reiniciar_sessao(de)
    enviar_menu_principal(de, idioma, saudacao=False, sessao=nova)
