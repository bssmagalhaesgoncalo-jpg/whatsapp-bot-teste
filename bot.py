"""
Bot "rececionista digital" via WhatsApp Cloud API para a Spotless Car Detail
(oficina fictícia de testes). Menu principal com 4 opções (Marcar / Orçamento /
Gerir marcação / Falar com a equipa), fluxos diferentes por tipo de serviço
(Limpeza / Estética / Wrap), comandos permanentes (MENU, VOLTAR, CANCELAR,
AJUDA, HUMANO, GERIR), recuperação de sessão abandonada, indicador de
progresso, resumo com preço e duração estimada, confirmação final mais
completa, e seleção de idioma (PT/DE/EN) como primeira interação.

Configuração necessária (variáveis de ambiente):
  WHATSAPP_TOKEN       - access token (temporário ou permanente) da Meta
  PHONE_NUMBER_ID      - ID do número de teste/produção (em API Setup)
  VERIFY_TOKEN         - qualquer string à tua escolha, usada na verificação do webhook
  PROVIDER_WHATSAPP    - número do prestador de serviço em formato internacional, ex: 41795886305

Como correr:
  pip install flask requests
  export WHATSAPP_TOKEN=... PHONE_NUMBER_ID=... VERIFY_TOKEN=... PROVIDER_WHATSAPP=...
  python bot.py

Nota sobre idiomas: as mensagens para o CLIENTE existem em português (pt),
alemão (de) e inglês (en) através do sistema central `TEXTOS` + funções
`t()`/`tx()` abaixo. As notificações INTERNAS para o negócio
(PROVIDER_WHATSAPP) mantêm-se sempre em português, por decisão do dono do
negócio. No alemão usa-se sempre "ss", nunca "ß".
"""

import os
import re
import json
import sqlite3
import requests
from functools import wraps
from datetime import date, timedelta, datetime
from flask import Flask, request, jsonify, send_from_directory, Response

app = Flask(__name__)

TOKEN = os.environ.get("WHATSAPP_TOKEN", "")
PHONE_NUMBER_ID = os.environ.get("PHONE_NUMBER_ID", "1052227394639217")
VERIFY_TOKEN = os.environ.get("VERIFY_TOKEN", "teste123")
PROVIDER_WHATSAPP = os.environ.get("PROVIDER_WHATSAPP", "41795886305")

# Pasta (local, configurável) onde as fotografias dos pedidos de orçamento
# são guardadas em disco — nunca dentro do SQLite. Ver guardar_media_local().
MEDIA_DIR = os.environ.get("MEDIA_DIR", "media_pedidos")

# Credenciais de autenticação HTTP Basic do painel/API (falha fechado: sem
# ambas definidas, o acesso é sempre recusado). Ver requer_autenticacao().
DASHBOARD_USER = os.environ.get("DASHBOARD_USER", "")
DASHBOARD_PASSWORD = os.environ.get("DASHBOARD_PASSWORD", "")

GRAPH_URL = f"https://graph.facebook.com/v21.0/{PHONE_NUMBER_ID}/messages"

NOME_OFICINA = "Spotless Car Detail (TESTE)"
MORADA_OFICINA = "Spotless Car Detail, Zermatt"

# IDs usados em botões/listas em todo o fluxo (nunca traduzidos — são
# identificadores internos, não texto visível)
ID_VOLTAR = "voltar"
ID_CANCELAR = "cancelar_processo"

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

# IDs dos botões de seleção de idioma -> código de idioma interno
LANG_IDS = {"lang_pt": "pt", "lang_de": "de", "lang_en": "en"}


# ---------------------------------------------------------------------------
# Sistema central de traduções
# ---------------------------------------------------------------------------
# Mensagem fixa de boas-vindas + seleção de idioma — mostrada sempre nos 3
# idiomas ao mesmo tempo (é a única mensagem que não depende de um idioma já
# escolhido, porque é exatamente isso que ainda não sabemos).
TEXTO_SELETOR_IDIOMA = (
    "👋 Bem-vindo à Spotless Car Detail!\n"
    "Willkommen bei Spotless Car Detail!\n"
    "Welcome to Spotless Car Detail!\n"
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

    # --- Categorias --------------------------------------------------------
    "categoria_pergunta": {"pt": "Que tipo de serviço procura?",
                            "de": "Welche Art von Service suchen Sie?",
                            "en": "What type of service are you looking for?"},

    # --- Rodapé / linhas auxiliares de lista -------------------------------
    "rodape_padrao": {"pt": "Escreva VOLTAR, CANCELAR ou MENU a qualquer momento",
                       "de": "Schreiben Sie jederzeit VOLTAR, CANCELAR oder MENU",
                       "en": "Type VOLTAR, CANCELAR or MENU at any time"},
    "voltar_titulo": {"pt": "⬅️ Voltar", "de": "⬅️ Zurück", "en": "⬅️ Back"},
    "voltar_desc": {"pt": "Passo anterior", "de": "Vorheriger Schritt", "en": "Previous step"},
    "cancelar_titulo": {"pt": "❌ Cancelar processo", "de": "❌ Vorgang abbrechen", "en": "❌ Cancel process"},
    "cancelar_desc": {"pt": "Terminar sem marcar", "de": "Ohne Buchung beenden", "en": "End without booking"},

    # --- Passos: Limpeza -----------------------------------------------
    "limpeza_tipo_corpo": {"pt": "Passo 1 de 5 — Escolha o tipo de limpeza:",
                            "de": "Schritt 1 von 5 — Wählen Sie die Art der Reinigung:",
                            "en": "Step 1 of 5 — Choose the type of cleaning:"},
    "limpeza_tipo_seccao": {"pt": "Tipo de limpeza", "de": "Art der Reinigung", "en": "Cleaning type"},
    "limpeza_tipo_botao": {"pt": "🧼 Escolher", "de": "🧼 Wählen", "en": "🧼 Choose"},

    "limpeza_tamanho_corpo": {"pt": "Passo 2 de 5 — Qual o tamanho do veículo?",
                               "de": "Schritt 2 von 5 — Wie gross ist das Fahrzeug?",
                               "en": "Step 2 of 5 — What is the vehicle size?"},
    "tamanho_seccao": {"pt": "Tamanho do veículo", "de": "Fahrzeuggrösse", "en": "Vehicle size"},
    "tamanho_botao": {"pt": "🚗 Escolher", "de": "🚗 Wählen", "en": "🚗 Choose"},

    "extra_corpo": {"pt": "Passo 3 de 5 — Deseja algum extra?",
                    "de": "Schritt 3 von 5 — Möchten Sie ein Extra?",
                    "en": "Step 3 of 5 — Would you like any extra?"},
    "extra_seccao": {"pt": "Extras disponíveis", "de": "Verfügbare Extras", "en": "Available extras"},
    "extra_botao": {"pt": "➕ Escolher", "de": "➕ Wählen", "en": "➕ Choose"},

    # --- Passos: Estética -----------------------------------------------
    "estetica_servico_corpo": {"pt": "Passo 1 de 5 — Escolha o serviço de estética:",
                                "de": "Schritt 1 von 5 — Wählen Sie den Aufbereitungsservice:",
                                "en": "Step 1 of 5 — Choose the detailing service:"},
    "estetica_servico_seccao": {"pt": "Estética automóvel", "de": "Fahrzeugaufbereitung", "en": "Car detailing"},
    "estetica_servico_botao": {"pt": "✨ Escolher", "de": "✨ Wählen", "en": "✨ Choose"},

    "estetica_estado_corpo": {"pt": "Passo 2 de 5 — Como está o estado atual do veículo?",
                               "de": "Schritt 2 von 5 — Wie ist der aktuelle Zustand des Fahrzeugs?",
                               "en": "Step 2 of 5 — What is the vehicle's current condition?"},
    "estado_seccao": {"pt": "Estado do veículo", "de": "Fahrzeugzustand", "en": "Vehicle condition"},
    "estado_botao": {"pt": "🚗 Escolher", "de": "🚗 Wählen", "en": "🚗 Choose"},

    # --- Data / hora --------------------------------------------------------
    "data_corpo": {"pt": "Passo {n} de 5 — Para que dia gostaria de marcar?",
                   "de": "Schritt {n} von 5 — Für welchen Tag möchten Sie buchen?",
                   "en": "Step {n} of 5 — Which day would you like to book?"},
    "data_seccao": {"pt": "Datas disponíveis", "de": "Verfügbare Termine", "en": "Available dates"},
    "data_botao": {"pt": "📅 Escolher dia", "de": "📅 Tag wählen", "en": "📅 Choose day"},

    "hora_corpo": {"pt": "Passo {n} de 5 — A que horas lhe convém?",
                   "de": "Schritt {n} von 5 — Um wie viel Uhr passt es Ihnen?",
                   "en": "Step {n} of 5 — What time suits you?"},
    "hora_seccao": {"pt": "Horários disponíveis", "de": "Verfügbare Uhrzeiten", "en": "Available times"},
    "hora_botao": {"pt": "⏰ Escolher hora", "de": "⏰ Uhrzeit wählen", "en": "⏰ Choose time"},

    # --- Resumo / confirmação -----------------------------------------------
    "resumo_titulo": {"pt": "📋 *Confirme a sua marcação*", "de": "📋 *Bestätigen Sie Ihre Buchung*",
                       "en": "📋 *Confirm your booking*"},
    "resumo_servico": {"pt": "🔧 Serviço: {servico}", "de": "🔧 Service: {servico}", "en": "🔧 Service: {servico}"},
    "resumo_extra": {"pt": "➕ Extra: {extra}", "de": "➕ Extra: {extra}", "en": "➕ Extra: {extra}"},
    "resumo_data": {"pt": "📅 Data: {data}", "de": "📅 Datum: {data}", "en": "📅 Date: {data}"},
    "resumo_hora": {"pt": "🕒 Hora: {hora}", "de": "🕒 Uhrzeit: {hora}", "en": "🕒 Time: {hora}"},
    "resumo_duracao": {"pt": "⏱️ Duração estimada: {duracao}", "de": "⏱️ Geschätzte Dauer: {duracao}",
                        "en": "⏱️ Estimated duration: {duracao}"},
    "resumo_preco": {"pt": "💰 Preço: {preco}", "de": "💰 Preis: {preco}", "en": "💰 Price: {preco}"},
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
    "confirmado_instrucao": {"pt": "Por favor, retire os seus objetos pessoais do veículo antes da entrega.",
                              "de": "Bitte entfernen Sie Ihre persönlichen Gegenstände vor der Abgabe aus dem Fahrzeug.",
                              "en": "Please remove your personal belongings from the vehicle before drop-off."},
    "confirmado_rodape": {"pt": "Escreva MENU para nova marcação, ou GERIR para consultar/alterar esta.",
                           "de": "Schreiben Sie MENU für eine neue Buchung oder GERIR, um diese anzusehen/zu ändern.",
                           "en": "Type MENU for a new booking, or GERIR to view/change this one."},

    # --- Wrap & Proteção -----------------------------------------------------
    "wrap_passo1": {"pt": "Passo 1 de 4 — Indique marca, modelo e ano do veículo (ex: \"BMW M4, 2022\").",
                    "de": "Schritt 1 von 4 — Geben Sie Marke, Modell und Baujahr des Fahrzeugs an (z.B. \"BMW M4, 2022\").",
                    "en": "Step 1 of 4 — Please provide the vehicle's make, model and year (e.g. \"BMW M4, 2022\")."},
    "wrap_passo2_corpo": {"pt": "Passo 2 de 4 — Pretende wrap total ou parcial?",
                          "de": "Schritt 2 von 4 — Möchten Sie eine Voll- oder Teilfolierung?",
                          "en": "Step 2 of 4 — Would you like a full or partial wrap?"},
    "wrap_total_botao": {"pt": "🚗 Wrap total", "de": "🚗 Vollfolierung", "en": "🚗 Full wrap"},
    "wrap_parcial_botao": {"pt": "🔧 Wrap parcial", "de": "🔧 Teilfolierung", "en": "🔧 Partial wrap"},
    "wrap_passo3": {"pt": "Passo 3 de 4 — Que cor/acabamento pretende? (ex: \"Preto fosco\", \"Verde metalizado\")",
                    "de": "Schritt 3 von 4 — Welche Farbe/welches Finish wünschen Sie? (z.B. \"Mattschwarz\", \"Metallic-Grün\")",
                    "en": "Step 3 of 4 — What colour/finish would you like? (e.g. \"Matte black\", \"Metallic green\")"},
    "wrap_fotos_pergunta_corpo": {"pt": "Passo 4 de 4 — Deseja enviar fotografias do veículo (até 5) para "
                                        "ajudar a equipa a preparar o orçamento?",
                                   "de": "Schritt 4 von 4 — Möchten Sie Fotos des Fahrzeugs (bis zu 5) senden, "
                                        "damit unser Team den Kostenvoranschlag vorbereiten kann?",
                                   "en": "Step 4 of 4 — Would you like to send photos of the vehicle (up to 5) "
                                        "to help our team prepare the quote?"},
    "wrap_fotos_sim_botao": {"pt": "📸 Sim, enviar fotos", "de": "📸 Ja, Fotos senden", "en": "📸 Yes, send photos"},
    "wrap_fotos_nao_botao": {"pt": "➡️ Sem fotos", "de": "➡️ Ohne Fotos", "en": "➡️ No photos"},
    "wrap_fotos_pedir": {"pt": "Pode enviar agora até 5 fotografias do veículo, uma de cada vez, diretamente aqui na conversa.",
                          "de": "Sie können jetzt bis zu 5 Fotos des Fahrzeugs senden, eines nach dem anderen, direkt hier im Chat.",
                          "en": "You can now send up to 5 photos of the vehicle, one at a time, directly here in the chat."},
    "wrap_foto_recebida_contagem": {"pt": "📸 Fotografia {atual} de {total} recebida.",
                                     "de": "📸 Foto {atual} von {total} erhalten.",
                                     "en": "📸 Photo {atual} of {total} received."},
    "wrap_fotos_mais_ou_concluir": {"pt": "Pode enviar mais fotografias ou tocar em \"Concluir pedido\" para terminarmos.",
                                     "de": "Sie können weitere Fotos senden oder auf \"Anfrage abschliessen\" tippen, um fortzufahren.",
                                     "en": "You can send more photos or tap \"Finish request\" to continue."},
    "wrap_fotos_concluir_botao": {"pt": "✅ Concluir pedido", "de": "✅ Anfrage beenden", "en": "✅ Finish request"},
    "wrap_foto_formato_invalido": {"pt": "Só conseguimos aceitar fotografias (imagens). Por favor envie uma fotografia, "
                                          "ou toque em \"Concluir pedido\".",
                                    "de": "Wir können nur Fotos (Bilder) akzeptieren. Bitte senden Sie ein Foto, "
                                          "oder tippen Sie auf \"Anfrage abschliessen\".",
                                    "en": "We can only accept photographs (images). Please send a photo, "
                                          "or tap \"Finish request\"."},
    "wrap_fotos_limite_atingido": {"pt": "✅ Já recebemos o máximo de 5 fotografias. A concluir o seu pedido...",
                                    "de": "✅ Wir haben bereits die maximal 5 Fotos erhalten. Ihre Anfrage wird abgeschlossen...",
                                    "en": "✅ We've already received the maximum of 5 photos. Finishing your request..."},
    "wrap_finalizado_cliente": {"pt": "✅ Pedido de orçamento enviado! A nossa equipa vai analisar os detalhes "
                                      "(e as fotografias, se enviadas) e responde-lhe em breve com o orçamento e "
                                      "disponibilidade para *{veiculo}*.\n\nEscreva MENU para voltar ao início.",
                                 "de": "✅ Kostenvoranschlag-Anfrage gesendet! Unser Team prüft die Details "
                                      "(und die Fotos, falls gesendet) und meldet sich in Kürze mit dem Angebot und "
                                      "der Verfügbarkeit für *{veiculo}*.\n\nSchreiben Sie MENU, um zum Anfang zurückzukehren.",
                                 "en": "✅ Quote request sent! Our team will review the details (and the photos, "
                                      "if sent) and will get back to you shortly with the quote and availability "
                                      "for *{veiculo}*.\n\nType MENU to return to the start."},
    "wrap_veiculo_generico": {"pt": "o seu veículo", "de": "Ihr Fahrzeug", "en": "your vehicle"},

    # --- Orçamento genérico ---------------------------------------------------
    "orcamento_pedido": {"pt": "💰 Sem problema! Descreva em poucas palavras o serviço que pretende e o veículo "
                                "(ex: \"Polimento completo, Audi A4 2019\"). A nossa equipa responde com um orçamento em breve.",
                          "de": "💰 Kein Problem! Beschreiben Sie kurz den gewünschten Service und das Fahrzeug "
                                "(z.B. \"Komplettpolitur, Audi A4 2019\"). Unser Team antwortet Ihnen in Kürze mit einem Kostenvoranschlag.",
                          "en": "💰 No problem! Briefly describe the service you'd like and the vehicle "
                                "(e.g. \"Full polish, Audi A4 2019\"). Our team will reply with a quote shortly."},
    "orcamento_recebido_cliente": {"pt": "✅ Recebido! A equipa vai analisar e responde-lhe em breve.\n\nEscreva MENU para voltar ao início.",
                                    "de": "✅ Erhalten! Das Team prüft die Anfrage und meldet sich in Kürze.\n\nSchreiben Sie MENU, um zum Anfang zurückzukehren.",
                                    "en": "✅ Received! Our team will review it and get back to you shortly.\n\nType MENU to return to the start."},

    # --- Gestão de marcação -----------------------------------------------
    "gerir_sem_marcacao": {"pt": "Não encontrei nenhuma marcação ativa associada a este número.\n\nEscreva MENU para fazer uma nova marcação.",
                            "de": "Ich habe keine aktive Buchung zu dieser Nummer gefunden.\n\nSchreiben Sie MENU, um eine neue Buchung vorzunehmen.",
                            "en": "I couldn't find any active booking for this number.\n\nType MENU to make a new booking."},
    "gerir_corpo": {"pt": "🗓️ A sua marcação #{id}:\n\n🔧 {servico}\n📅 {data} às {hora}\n⏱️ Duração: {duracao}\n💰 {preco}\n\nO que deseja fazer?",
                    "de": "🗓️ Ihre Buchung #{id}:\n\n🔧 {servico}\n📅 {data} um {hora}\n⏱️ Dauer: {duracao}\n💰 {preco}\n\nWas möchten Sie tun?",
                    "en": "🗓️ Your booking #{id}:\n\n🔧 {servico}\n📅 {data} at {hora}\n⏱️ Duration: {duracao}\n💰 {preco}\n\nWhat would you like to do?"},
    "botao_reagendar": {"pt": "✏️ Reagendar", "de": "✏️ Verschieben", "en": "✏️ Reschedule"},
    "botao_cancelar_marcacao": {"pt": "❌ Cancelar", "de": "❌ Stornieren", "en": "❌ Cancel"},
    "botao_nova_marcacao": {"pt": "📅 Nova marcação", "de": "📅 Neue Buchung", "en": "📅 New booking"},
    "reagendar_aviso": {"pt": "Sem problema, vamos criar uma nova marcação. A anterior foi arquivada.",
                         "de": "Kein Problem, wir erstellen eine neue Buchung. Die vorherige wurde archiviert.",
                         "en": "No problem, let's create a new booking. The previous one has been archived."},
    "cancelado_cliente": {"pt": "✅ A sua marcação foi cancelada. Escreva MENU quando quiser marcar novamente.",
                           "de": "✅ Ihre Buchung wurde storniert. Schreiben Sie MENU, wenn Sie erneut buchen möchten.",
                           "en": "✅ Your booking has been cancelled. Type MENU whenever you'd like to book again."},

    # --- Falar com a equipa ------------------------------------------------
    "humano_cliente": {"pt": "💬 Vou avisar já a nossa equipa — em breve alguém entra em contacto consigo por aqui.\n\nEscreva MENU a qualquer momento para voltar ao início.",
                        "de": "💬 Ich informiere unser Team sofort — jemand wird sich in Kürze hier bei Ihnen melden.\n\nSchreiben Sie jederzeit MENU, um zum Anfang zurückzukehren.",
                        "en": "💬 I'll let our team know right away — someone will get in touch with you here shortly.\n\nType MENU at any time to return to the start."},

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

    "nao_entendi": {"pt": "Desculpe, não consegui perceber 😅\n\nEscolha uma opção ou escreva MENU para recomeçar.",
                     "de": "Entschuldigung, das habe ich nicht verstanden 😅\n\nWählen Sie eine Option oder schreiben Sie MENU, um neu zu beginnen.",
                     "en": "Sorry, I didn't understand that 😅\n\nChoose an option or type MENU to start again."},
    "processo_cancelado": {"pt": "❌ Processo cancelado. Escreva MENU quando quiser recomeçar.",
                            "de": "❌ Vorgang abgebrochen. Schreiben Sie MENU, wenn Sie neu beginnen möchten.",
                            "en": "❌ Process cancelled. Type MENU whenever you'd like to start again."},

    "retomar_pergunta": {"pt": "Encontrámos uma marcação que ainda não terminou.\nDeseja continuar ou começar novamente?",
                          "de": "Wir haben eine noch nicht abgeschlossene Buchung gefunden.\nMöchten Sie fortfahren oder neu beginnen?",
                          "en": "We found a booking that wasn't finished.\nWould you like to continue or start again?"},
    "botao_continuar": {"pt": "▶️ Continuar", "de": "▶️ Fortfahren", "en": "▶️ Continue"},
    "botao_recomecar": {"pt": "🔄 Recomeçar", "de": "🔄 Neu beginnen", "en": "🔄 Start again"},

    "preco_a_combinar": {"pt": "a combinar", "de": "auf Anfrage", "en": "on request"},
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


# ---------------------------------------------------------------------------
# Catálogo de serviços, preços e durações (valores fictícios, para testar).
# Preços e fatores nunca mudam com o idioma — só titulo/descricao/duracao são
# multilingues (dict pt/de/en); o "pt" de cada um é sempre o valor canónico
# guardado na base de dados e usado nas notificações internas.
# ---------------------------------------------------------------------------
LIMPEZA_TIPOS = [
    {"id": "lp_int", "preco": 80,
     "titulo": {"pt": "Interior", "de": "Innenreinigung", "en": "Interior"},
     "descricao": {"pt": "Aspiração e higienização completa do habitáculo",
                   "de": "Absaugen und vollständige Hygiene des Innenraums",
                   "en": "Vacuuming and full interior sanitising"},
     "duracao": {"pt": "1h30", "de": "1h30", "en": "1h 30"}},
    {"id": "lp_ext", "preco": 60,
     "titulo": {"pt": "Exterior", "de": "Aussenreinigung", "en": "Exterior"},
     "descricao": {"pt": "Lavagem exterior à mão + secagem",
                   "de": "Handwäsche aussen + Trocknen",
                   "en": "Hand exterior wash + drying"},
     "duracao": {"pt": "1h", "de": "1h", "en": "1h"}},
    {"id": "lp_full", "preco": 130,
     "titulo": {"pt": "Interior + Exterior", "de": "Innen + Aussen", "en": "Interior + Exterior"},
     "descricao": {"pt": "Pacote completo por dentro e por fora",
                   "de": "Komplettpaket innen und aussen",
                   "en": "Complete package inside and out"},
     "duracao": {"pt": "2h", "de": "2h", "en": "2h"}},
]

TAMANHOS_VEICULO = [
    {"id": "tam_p", "fator": 1.0,
     "titulo": {"pt": "Pequeno", "de": "Klein", "en": "Small"},
     "descricao": {"pt": "Ex: Smart, Polo, Corsa", "de": "Z.B. Smart, Polo, Corsa", "en": "E.g. Smart, Polo, Corsa"}},
    {"id": "tam_m", "fator": 1.15,
     "titulo": {"pt": "Médio", "de": "Mittel", "en": "Medium"},
     "descricao": {"pt": "Ex: Golf, Sedan, Berlina", "de": "Z.B. Golf, Limousine", "en": "E.g. Golf, Sedan"}},
    {"id": "tam_g", "fator": 1.35,
     "titulo": {"pt": "Grande", "de": "Gross", "en": "Large"},
     "descricao": {"pt": "Ex: SUV, Van, Pick-up", "de": "Z.B. SUV, Van, Pick-up", "en": "E.g. SUV, Van, Pick-up"}},
]

EXTRAS_LIMPEZA = [
    {"id": "ex_nenhum", "preco": 0,
     "titulo": {"pt": "Nenhum extra", "de": "Kein Extra", "en": "No extra"},
     "descricao": {"pt": "Seguir sem extras", "de": "Ohne Extras fortfahren", "en": "Continue without extras"}},
    {"id": "ex_pelos", "preco": 25,
     "titulo": {"pt": "Remoção de pelos de animal", "de": "Tierhaarentfernung", "en": "Pet hair removal"},
     "descricao": {"pt": "Tratamento específico", "de": "Spezielle Behandlung", "en": "Specific treatment"}},
    {"id": "ex_odores", "preco": 20,
     "titulo": {"pt": "Tratamento de odores", "de": "Geruchsbehandlung", "en": "Odour treatment"},
     "descricao": {"pt": "Ozono / neutralização de cheiros", "de": "Ozon / Geruchsneutralisierung",
                   "en": "Ozone / odour neutralisation"}},
    {"id": "ex_bancos", "preco": 15,
     "titulo": {"pt": "Proteção de bancos", "de": "Sitzschutz", "en": "Seat protection"},
     "descricao": {"pt": "Impermeabilização têxtil/pele", "de": "Imprägnierung Textil/Leder",
                   "en": "Fabric/leather waterproofing"}},
]

ESTETICA_SERVICOS = [
    {"id": "es_polimento", "preco": 150,
     "titulo": {"pt": "Polimento", "de": "Polieren", "en": "Polishing"},
     "descricao": {"pt": "Remove riscos e devolve o brilho", "de": "Entfernt Kratzer und bringt den Glanz zurück",
                   "en": "Removes scratches and restores shine"},
     "duracao": {"pt": "3h", "de": "3h", "en": "3h"}},
    {"id": "es_ceramica", "preco": 350,
     "titulo": {"pt": "Proteção cerâmica", "de": "Keramikversiegelung", "en": "Ceramic coating"},
     "descricao": {"pt": "Proteção de longa duração", "de": "Langfristiger Schutz", "en": "Long-lasting protection"},
     "duracao": {"pt": "1 dia", "de": "1 Tag", "en": "1 day"}},
    {"id": "es_farois", "preco": 60,
     "titulo": {"pt": "Polimento de faróis", "de": "Scheinwerferpolitur", "en": "Headlight polishing"},
     "descricao": {"pt": "Recupera a transparência dos faróis", "de": "Stellt die Transparenz der Scheinwerfer wieder her",
                   "en": "Restores headlight clarity"},
     "duracao": {"pt": "45min", "de": "45min", "en": "45min"}},
]

ESTADO_VEICULO = [
    {"id": "est_bom", "fator": 1.0,
     "titulo": {"pt": "✅ Bom estado", "de": "✅ Guter Zustand", "en": "✅ Good condition"}},
    {"id": "est_medio", "fator": 1.0,
     "titulo": {"pt": "🟡 Estado médio", "de": "🟡 Mittlerer Zustand", "en": "🟡 Average condition"}},
    {"id": "est_mau", "fator": 1.15,
     "titulo": {"pt": "🔴 Precisa de atenção especial", "de": "🔴 Braucht besondere Pflege", "en": "🔴 Needs special attention"}},
]

EXTRAS_ESTETICA = [
    {"id": "exe_nenhum", "preco": 0,
     "titulo": {"pt": "Nenhum extra", "de": "Kein Extra", "en": "No extra"},
     "descricao": {"pt": "Seguir sem extras", "de": "Ohne Extras fortfahren", "en": "Continue without extras"}},
    {"id": "exe_farois", "preco": 60,
     "titulo": {"pt": "Polimento de faróis", "de": "Scheinwerferpolitur", "en": "Headlight polishing"},
     "descricao": {"pt": "Complementar ao serviço principal", "de": "Ergänzend zum Hauptservice",
                   "en": "In addition to the main service"}},
    {"id": "exe_pneus", "preco": 20,
     "titulo": {"pt": "Tratamento de pneus/jantes", "de": "Reifen-/Felgenpflege", "en": "Tyre/rim treatment"},
     "descricao": {"pt": "Acabamento final", "de": "Abschliessende Politur", "en": "Finishing touch"}},
]

HORARIOS = ["🕘 09:00", "🕥 10:30", "🕐 13:00", "🕝 14:30", "🕓 16:00"]  # iguais nos 3 idiomas

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
    {"id": "mp_orcamento",
     "titulo": {"pt": "💰 Pedir orçamento", "de": "💰 Kostenvoranschlag anfragen", "en": "💰 Request a quote"},
     "descricao": {"pt": "Sem compromisso, resposta da equipa", "de": "Unverbindlich, Antwort vom Team",
                   "en": "No obligation, our team will reply"}},
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

CATEGORIAS_MARCAR = [
    {"id": "cat_limpeza", "titulo": {"pt": "🧼 Limpeza", "de": "🧼 Reinigung", "en": "🧼 Cleaning"}},
    {"id": "cat_estetica", "titulo": {"pt": "✨ Estética", "de": "✨ Aufbereitung", "en": "✨ Detailing"}},
    {"id": "cat_wrap", "titulo": {"pt": "🎨 Wrap & Proteção", "de": "🎨 Folierung & Schutz", "en": "🎨 Wrap & Protection"}},
]

NOME_CATEGORIA = {c["id"]: c["titulo"] for c in CATEGORIAS_MARCAR}


# ---------------------------------------------------------------------------
# Persistência em SQLite: sessões em curso + agendamentos confirmados
# (esquema inalterado — o idioma escolhido vive dentro do JSON da sessão,
# tal como "nome", não precisa de coluna própria)
# ---------------------------------------------------------------------------
DB_PATH = os.environ.get("SESSOES_DB", "sessoes.db")

# Estados possíveis de um pedido de orçamento (Wrap & Proteção). Só usados
# internamente/no dashboard — não fazem parte do texto traduzido ao cliente.
ESTADOS_PEDIDO = ("novo", "em análise", "orçamento enviado", "aceite", "recusado", "arquivado")


def obter_bd():
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS sessoes (telefone TEXT PRIMARY KEY, dados TEXT NOT NULL)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS agendamentos ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "telefone TEXT NOT NULL, "
        "nome TEXT, "
        "categoria TEXT, "
        "servico TEXT NOT NULL, "
        "extra TEXT, "
        "data TEXT, "
        "hora TEXT, "
        "preco REAL, "
        "duracao TEXT, "
        "estado TEXT DEFAULT 'confirmado', "
        "criado_em TEXT NOT NULL)"
    )
    # Pedidos de orçamento com fotografias (fluxo Wrap & Proteção). Estrutura
    # separada dos agendamentos, pois um pedido de orçamento ainda não é uma
    # marcação. `agendamento_id` é reservado (nulo por agora) para uma futura
    # funcionalidade de calendário poder associar um pedido a uma marcação,
    # sem duplicar dados — não implementado nesta fase.
    conn.execute(
        "CREATE TABLE IF NOT EXISTS pedidos_orcamento ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "telefone TEXT NOT NULL, "
        "nome TEXT, "
        "veiculo TEXT, "
        "ano_veiculo TEXT, "
        "tipo_wrap TEXT, "
        "cor_acabamento TEXT, "
        "estado TEXT DEFAULT 'novo', "
        "agendamento_id INTEGER, "
        "criado_em TEXT NOT NULL)"
    )
    # Fotografias associadas a um pedido de orçamento. Só o NOME do ficheiro
    # é guardado aqui — o conteúdo binário da imagem vive em disco (pasta
    # MEDIA_DIR), nunca dentro do SQLite.
    conn.execute(
        "CREATE TABLE IF NOT EXISTS fotografias ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "pedido_id INTEGER NOT NULL, "
        "nome_ficheiro TEXT NOT NULL, "
        "mime_tipo TEXT, "
        "criado_em TEXT NOT NULL)"
    )
    return conn


def carregar_sessao(telefone):
    with obter_bd() as conn:
        linha = conn.execute(
            "SELECT dados FROM sessoes WHERE telefone = ?", (telefone,)
        ).fetchone()
    return json.loads(linha[0]) if linha else {}


def guardar_sessao(telefone, sessao):
    with obter_bd() as conn:
        conn.execute(
            "INSERT INTO sessoes (telefone, dados) VALUES (?, ?) "
            "ON CONFLICT(telefone) DO UPDATE SET dados = excluded.dados",
            (telefone, json.dumps(sessao)),
        )


def apagar_sessao(telefone):
    with obter_bd() as conn:
        conn.execute("DELETE FROM sessoes WHERE telefone = ?", (telefone,))


def guardar_agendamento(telefone, sessao):
    with obter_bd() as conn:
        cur = conn.execute(
            "INSERT INTO agendamentos "
            "(telefone, nome, categoria, servico, extra, data, hora, preco, duracao, estado, criado_em) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'confirmado', ?)",
            (
                telefone, sessao.get("nome"), sessao.get("categoria"),
                sessao.get("servico"), sessao.get("extra"),
                sessao.get("data"), sessao.get("hora"),
                sessao.get("preco"), sessao.get("duracao"),
                datetime.utcnow().isoformat(),
            ),
        )
        return cur.lastrowid


def listar_agendamentos():
    with obter_bd() as conn:
        linhas = conn.execute(
            "SELECT id, telefone, nome, categoria, servico, extra, data, hora, preco, duracao, estado, criado_em "
            "FROM agendamentos ORDER BY id DESC"
        ).fetchall()
    campos = ["id", "telefone", "nome", "categoria", "servico", "extra", "data", "hora",
              "preco", "duracao", "estado", "criado_em"]
    return [dict(zip(campos, l)) for l in linhas]


def ultimo_agendamento_ativo(telefone):
    with obter_bd() as conn:
        linha = conn.execute(
            "SELECT id, servico, data, hora, preco, duracao FROM agendamentos "
            "WHERE telefone = ? AND estado = 'confirmado' ORDER BY id DESC LIMIT 1",
            (telefone,),
        ).fetchone()
    if not linha:
        return None
    return {"id": linha[0], "servico": linha[1], "data": linha[2], "hora": linha[3],
            "preco": linha[4], "duracao": recuperar_duracao(linha[1], linha[5])}


def atualizar_estado_agendamento(id_agendamento, estado):
    with obter_bd() as conn:
        conn.execute("UPDATE agendamentos SET estado = ? WHERE id = ?", (estado, id_agendamento))


# ---------------------------------------------------------------------------
# Pedidos de orçamento com fotografias (Wrap & Proteção)
# ---------------------------------------------------------------------------
def criar_pedido_orcamento(telefone, sessao):
    with obter_bd() as conn:
        cur = conn.execute(
            "INSERT INTO pedidos_orcamento "
            "(telefone, nome, veiculo, ano_veiculo, tipo_wrap, cor_acabamento, estado, criado_em) "
            "VALUES (?, ?, ?, ?, ?, ?, 'novo', ?)",
            (
                telefone, sessao.get("nome"), sessao.get("wrap_veiculo"),
                extrair_ano_veiculo(sessao.get("wrap_veiculo")),
                "Wrap total" if sessao.get("wrap_tipo") == "wrap_total" else "Wrap parcial",
                sessao.get("wrap_cor"),
                datetime.utcnow().isoformat(),
            ),
        )
        return cur.lastrowid


def adicionar_fotografia(pedido_id, nome_ficheiro, mime_tipo):
    with obter_bd() as conn:
        conn.execute(
            "INSERT INTO fotografias (pedido_id, nome_ficheiro, mime_tipo, criado_em) VALUES (?, ?, ?, ?)",
            (pedido_id, nome_ficheiro, mime_tipo, datetime.utcnow().isoformat()),
        )


def contar_fotografias(pedido_id):
    if not pedido_id:
        return 0
    with obter_bd() as conn:
        linha = conn.execute(
            "SELECT COUNT(*) FROM fotografias WHERE pedido_id = ?", (pedido_id,)
        ).fetchone()
    return linha[0] if linha else 0


def obter_pedido_orcamento(pedido_id):
    with obter_bd() as conn:
        linha = conn.execute(
            "SELECT id, telefone, nome, veiculo, ano_veiculo, tipo_wrap, cor_acabamento, estado, "
            "agendamento_id, criado_em FROM pedidos_orcamento WHERE id = ?", (pedido_id,)
        ).fetchone()
    if not linha:
        return None
    campos = ["id", "telefone", "nome", "veiculo", "ano_veiculo", "tipo_wrap", "cor_acabamento",
              "estado", "agendamento_id", "criado_em"]
    return dict(zip(campos, linha))


def listar_pedidos_orcamento():
    with obter_bd() as conn:
        linhas = conn.execute(
            "SELECT p.id, p.telefone, p.nome, p.veiculo, p.ano_veiculo, p.tipo_wrap, p.cor_acabamento, "
            "p.estado, p.agendamento_id, p.criado_em, COUNT(f.id) AS num_fotos "
            "FROM pedidos_orcamento p LEFT JOIN fotografias f ON f.pedido_id = p.id "
            "GROUP BY p.id ORDER BY p.id DESC"
        ).fetchall()
    campos = ["id", "telefone", "nome", "veiculo", "ano_veiculo", "tipo_wrap", "cor_acabamento",
              "estado", "agendamento_id", "criado_em", "num_fotos"]
    return [dict(zip(campos, l)) for l in linhas]


def listar_fotografias(pedido_id):
    with obter_bd() as conn:
        linhas = conn.execute(
            "SELECT id, nome_ficheiro, mime_tipo, criado_em FROM fotografias "
            "WHERE pedido_id = ? ORDER BY id ASC", (pedido_id,)
        ).fetchall()
    campos = ["id", "nome_ficheiro", "mime_tipo", "criado_em"]
    return [dict(zip(campos, l)) for l in linhas]


# ---------------------------------------------------------------------------
# Envio de mensagens
# ---------------------------------------------------------------------------
def enviar(payload):
    headers = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}
    r = requests.post(GRAPH_URL, headers=headers, json=payload, timeout=10)
    print("Resposta da Meta:", r.status_code, r.text)
    return r


def enviar_texto(destinatario, texto):
    enviar({
        "messaging_product": "whatsapp",
        "to": destinatario,
        "type": "text",
        "text": {"body": texto},
    })


def enviar_lista(destinatario, corpo, titulo_seccao, opcoes, idioma, botao="👉 Escolher", com_voltar=False, rodape=None):
    """`opcoes`: lista de dicts {"id","titulo","descricao"?} (titulo/descricao
    podem ser strings simples ou dicts multilingues {"pt","de","en"} — são
    sempre resolvidos aqui, para `idioma`) ou strings simples (ex.: horários,
    iguais nos 3 idiomas)."""
    rows = []
    for i, opc in enumerate(opcoes):
        if isinstance(opc, dict):
            titulo = tx(opc["titulo"], idioma)
            row = {"id": opc.get("id", f"opt_{i}"), "title": titulo[:24]}
            desc = tx(opc.get("descricao"), idioma)
            if desc:
                row["description"] = desc[:72]
        else:
            row = {"id": f"opt_{i}", "title": str(opc)[:24]}
        rows.append(row)

    if com_voltar:
        rows.append({"id": ID_VOLTAR, "title": t("voltar_titulo", idioma), "description": t("voltar_desc", idioma)})
        rows.append({"id": ID_CANCELAR, "title": t("cancelar_titulo", idioma), "description": t("cancelar_desc", idioma)})

    interactive = {
        "type": "list",
        "body": {"text": corpo},
        "action": {"button": botao, "sections": [{"title": titulo_seccao, "rows": rows}]},
    }
    if rodape:
        interactive["footer"] = {"text": rodape}

    enviar({
        "messaging_product": "whatsapp", "to": destinatario, "type": "interactive",
        "interactive": interactive,
    })


def enviar_botoes(destinatario, corpo, botoes, idioma, rodape=None):
    interactive = {
        "type": "button",
        "body": {"text": corpo},
        "action": {"buttons": [
            {"type": "reply", "reply": {"id": b["id"], "title": tx(b["titulo"], idioma)[:20]}}
            for b in botoes[:3]
        ]},
    }
    if rodape:
        interactive["footer"] = {"text": rodape}
    enviar({
        "messaging_product": "whatsapp", "to": destinatario, "type": "interactive",
        "interactive": interactive,
    })


def encontrar_opcao(opcoes, id_escolhido):
    for opc in opcoes:
        if isinstance(opc, dict) and opc.get("id") == id_escolhido:
            return opc
    return None


def proximos_dias(idioma, n=5):
    hoje = date.today()
    abreviaturas = DIAS_SEMANA.get(idioma, DIAS_SEMANA["pt"])
    dias = []
    for i in range(1, n + 1):
        d = hoje + timedelta(days=i)
        dias.append(f"{d.strftime('%d.%m.%Y')} ({abreviaturas[d.weekday()]})")
    return dias


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


def preco_formatado(valor, idioma="pt"):
    return f"CHF {valor:.0f}" if valor else t("preco_a_combinar", idioma)


def duracao_valida(duracao):
    """Uma duração só é válida se tiver pelo menos um dígito (ex.: "2h",
    "45min", "1 dia"). Apanha casos antigos inválidos: None, "" ou só a
    unidade sozinha (ex.: "h")."""
    if not duracao:
        return False
    texto = str(duracao).strip()
    return bool(texto) and any(c.isdigit() for c in texto)


def recuperar_duracao(servico, duracao_guardada):
    """Corrige dinamicamente durações antigas inválidas, recuperando o valor
    certo (canónico, em português) a partir do catálogo de serviços pelo
    nome guardado. Não afeta marcações novas, que já guardam sempre uma
    duração válida vinda daqui."""
    if duracao_valida(duracao_guardada):
        return duracao_guardada
    opcao = _procurar_servico_por_nome_pt(servico)
    if opcao:
        return tx(opcao.get("duracao", "-"), "pt")
    return "-"


def _procurar_servico_por_nome_pt(nome_pt):
    """Vai buscar a entrada do catálogo (Limpeza ou Estética) cujo nome em
    português corresponde ao valor canónico guardado em sessao/DB."""
    for catalogo in (LIMPEZA_TIPOS, ESTETICA_SERVICOS):
        opcao = next((o for o in catalogo if o["titulo"]["pt"] == nome_pt), None)
        if opcao:
            return opcao
    return None


def _procurar_extra_por_nome_pt(nome_pt):
    for catalogo in (EXTRAS_LIMPEZA, EXTRAS_ESTETICA):
        opcao = next((o for o in catalogo if o["titulo"]["pt"] == nome_pt), None)
        if opcao:
            return opcao
    return None


def nome_servico_traduzido(servico_pt, idioma):
    """Traduz um nome de serviço canónico (guardado sempre em português) para
    o idioma do cliente, só para apresentação — não altera o que é guardado
    na sessão/base de dados."""
    opcao = _procurar_servico_por_nome_pt(servico_pt)
    return tx(opcao["titulo"], idioma) if opcao else servico_pt


def nome_extra_traduzido(extra_pt, idioma):
    if not extra_pt:
        return None
    opcao = _procurar_extra_por_nome_pt(extra_pt)
    return tx(opcao["titulo"], idioma) if opcao else extra_pt


def duracao_traduzida(servico_pt, duracao_pt, idioma):
    opcao = _procurar_servico_por_nome_pt(servico_pt)
    if opcao:
        return tx(opcao.get("duracao", "-"), idioma)
    return duracao_pt


def extrair_ano_veiculo(texto):
    """Extrai um ano plausível (19xx/20xx) do texto livre do veículo, se
    existir — usado só para preencher o campo "ano" no pedido de orçamento."""
    if not texto:
        return None
    m = re.search(r"\b(19|20)\d{2}\b", texto)
    return m.group(0) if m else None


# ---------------------------------------------------------------------------
# Download e armazenamento de fotografias (pedidos de orçamento Wrap)
# ---------------------------------------------------------------------------
# Só estes formatos de imagem são aceites; qualquer outro tipo é recusado.
MIME_IMAGENS_VALIDAS = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}


def descarregar_media_whatsapp(media_id):
    """Descarrega uma imagem da Cloud API a partir do seu media_id: 1º pede
    os metadados (que incluem um url temporário), depois descarrega o
    conteúdo binário com o mesmo cabeçalho de autenticação. Devolve
    (conteudo_binario, mime_tipo) ou (None, None) se algo falhar."""
    headers = {"Authorization": f"Bearer {TOKEN}"}
    resp_meta = requests.get(f"https://graph.facebook.com/v21.0/{media_id}", headers=headers, timeout=10)
    resp_meta.raise_for_status()
    info = resp_meta.json()
    url = info.get("url")
    mime_tipo = info.get("mime_type", "")
    if not url:
        return None, None
    resp_bin = requests.get(url, headers=headers, timeout=20)
    resp_bin.raise_for_status()
    return resp_bin.content, mime_tipo


def guardar_media_local(pedido_id, media_id, conteudo, mime_tipo):
    """Guarda o ficheiro de imagem em disco (nunca dentro do SQLite), numa
    pasta configurável (MEDIA_DIR). Função isolada e facilmente substituível
    por armazenamento permanente/na nuvem (ex.: S3) mais tarde, sem tocar em
    mais nenhuma parte do código — só esta função precisaria de mudar."""
    os.makedirs(MEDIA_DIR, exist_ok=True)
    extensao = MIME_IMAGENS_VALIDAS.get(mime_tipo, ".jpg")
    nome_ficheiro = f"pedido{pedido_id}_{media_id}{extensao}"
    caminho = os.path.join(MEDIA_DIR, nome_ficheiro)
    with open(caminho, "wb") as f:
        f.write(conteudo)
    return nome_ficheiro


# ---------------------------------------------------------------------------
# Passos do fluxo "Marcar" — Limpeza
# ---------------------------------------------------------------------------
def passo_limpeza_tipo(de, idioma):
    enviar_lista(de, t("limpeza_tipo_corpo", idioma), t("limpeza_tipo_seccao", idioma), LIMPEZA_TIPOS, idioma,
                 botao=t("limpeza_tipo_botao", idioma), com_voltar=True, rodape=t("rodape_padrao", idioma))


def passo_limpeza_tamanho(de, idioma):
    enviar_lista(de, t("limpeza_tamanho_corpo", idioma), t("tamanho_seccao", idioma), TAMANHOS_VEICULO, idioma,
                 botao=t("tamanho_botao", idioma), com_voltar=True, rodape=t("rodape_padrao", idioma))


def passo_limpeza_extra(de, idioma):
    enviar_lista(de, t("extra_corpo", idioma), t("extra_seccao", idioma), EXTRAS_LIMPEZA, idioma,
                 botao=t("extra_botao", idioma), com_voltar=True, rodape=t("rodape_padrao", idioma))


# ---------------------------------------------------------------------------
# Passos do fluxo "Marcar" — Estética
# ---------------------------------------------------------------------------
def passo_estetica_servico(de, idioma):
    enviar_lista(de, t("estetica_servico_corpo", idioma), t("estetica_servico_seccao", idioma), ESTETICA_SERVICOS, idioma,
                 botao=t("estetica_servico_botao", idioma), com_voltar=True, rodape=t("rodape_padrao", idioma))


def passo_estetica_estado(de, idioma):
    enviar_lista(de, t("estetica_estado_corpo", idioma), t("estado_seccao", idioma), ESTADO_VEICULO, idioma,
                 botao=t("estado_botao", idioma), com_voltar=True, rodape=t("rodape_padrao", idioma))


def passo_estetica_extra(de, idioma):
    enviar_lista(de, t("extra_corpo", idioma), t("extra_seccao", idioma), EXTRAS_ESTETICA, idioma,
                 botao=t("extra_botao", idioma), com_voltar=True, rodape=t("rodape_padrao", idioma))


# ---------------------------------------------------------------------------
# Data / hora / resumo / confirmação (comuns a limpeza e estética)
# ---------------------------------------------------------------------------
def passo_data(de, idioma, passo_n=4):
    enviar_lista(de, t("data_corpo", idioma, n=passo_n), t("data_seccao", idioma), proximos_dias(idioma), idioma,
                 botao=t("data_botao", idioma), com_voltar=True, rodape=t("rodape_padrao", idioma))


def passo_hora(de, idioma, passo_n=5):
    enviar_lista(de, t("hora_corpo", idioma, n=passo_n), t("hora_seccao", idioma), HORARIOS, idioma,
                 botao=t("hora_botao", idioma), com_voltar=True, rodape=t("rodape_padrao", idioma))


def calcular_preco_duracao(sessao):
    """Preços e fatores são sempre os mesmos, independentemente do idioma.
    O nome do serviço/extra e a duração devolvidos aqui são sempre o valor
    CANÓNICO em português — é o que fica guardado na sessão e na base de
    dados (dashboard e notificações internas continuam em português). A
    tradução para o idioma do cliente acontece só na apresentação, via
    nome_servico_traduzido()/nome_extra_traduzido()/duracao_traduzida()."""
    if sessao.get("categoria") == "cat_limpeza":
        tipo = encontrar_opcao(LIMPEZA_TIPOS, sessao.get("tipo_id")) or {}
        tamanho = encontrar_opcao(TAMANHOS_VEICULO, sessao.get("tamanho_id")) or {"fator": 1.0}
        extra = encontrar_opcao(EXTRAS_LIMPEZA, sessao.get("extra_id")) or {"preco": 0, "titulo": None}
        preco = tipo.get("preco", 0) * tamanho.get("fator", 1.0) + extra.get("preco", 0)
        return (round(preco), tx(tipo.get("duracao", "-"), "pt"),
                tx(tipo.get("titulo"), "pt"), tx(extra.get("titulo"), "pt"))
    if sessao.get("categoria") == "cat_estetica":
        serv = encontrar_opcao(ESTETICA_SERVICOS, sessao.get("tipo_id")) or {}
        estado = encontrar_opcao(ESTADO_VEICULO, sessao.get("estado_id")) or {"fator": 1.0}
        extra = encontrar_opcao(EXTRAS_ESTETICA, sessao.get("extra_id")) or {"preco": 0, "titulo": None}
        preco = serv.get("preco", 0) * estado.get("fator", 1.0) + extra.get("preco", 0)
        return (round(preco), tx(serv.get("duracao", "-"), "pt"),
                tx(serv.get("titulo"), "pt"), tx(extra.get("titulo"), "pt"))
    return None, None, None, None


def passo_resumo(de, idioma, sessao):
    preco, duracao_pt, servico_pt, extra_pt = calcular_preco_duracao(sessao)
    # canónico (português) — é isto que fica na sessão/DB, tal como antes
    sessao["servico"] = servico_pt
    sessao["extra"] = extra_pt if extra_pt and "nenhum" not in extra_pt.lower() else None
    sessao["preco"] = preco
    sessao["duracao"] = duracao_pt
    guardar_sessao(de, sessao)

    # tradução só para apresentação ao cliente
    servico_disp = nome_servico_traduzido(servico_pt, idioma)
    extra_disp = nome_extra_traduzido(sessao["extra"], idioma)
    duracao_disp = duracao_traduzida(servico_pt, duracao_pt, idioma)

    nome = primeiro_nome(sessao.get("nome"))
    titulo = t("resumo_titulo", idioma) + (f", {nome}" if nome else "")
    linhas = [titulo]
    linhas.append(t("resumo_servico", idioma, servico=servico_disp))
    if extra_disp:
        linhas.append(t("resumo_extra", idioma, extra=extra_disp))
    linhas.append(t("resumo_data", idioma, data=sessao["data"]))
    linhas.append(t("resumo_hora", idioma, hora=sessao["hora"]))
    linhas.append(t("resumo_duracao", idioma, duracao=duracao_disp))
    linhas.append(t("resumo_preco", idioma, preco=preco_formatado(preco, idioma)))
    linhas.append("\n" + t("resumo_pergunta", idioma))

    enviar_botoes(de, "\n".join(linhas), [
        {"id": "confirmar", "titulo": t("botao_confirmar", idioma)},
        {"id": "alterar", "titulo": t("botao_alterar", idioma)},
        {"id": ID_CANCELAR, "titulo": t("botao_cancelar", idioma)},
    ], idioma)


def mensagem_confirmacao_final(sessao, idioma):
    nome = primeiro_nome(sessao.get("nome"))
    saudacao = t("obrigado_nome", idioma, nome=nome) if nome else t("obrigado", idioma)

    servico_disp = nome_servico_traduzido(sessao["servico"], idioma)
    extra_disp = nome_extra_traduzido(sessao.get("extra"), idioma)
    duracao_disp = duracao_traduzida(sessao["servico"], sessao.get("duracao", "-"), idioma)
    hora_curta = sessao["hora"].split(" ")[-1] if " " in sessao["hora"] else sessao["hora"]

    linhas = [t("confirmado_titulo", idioma, saudacao=saudacao), ""]
    linhas.append(f"🔧 {servico_disp}")
    if extra_disp:
        linhas.append(f"➕ {extra_disp}")
    linhas.append(t("confirmado_data_hora", idioma, data=sessao["data"], hora=hora_curta))
    linhas.append(t("confirmado_duracao", idioma, duracao=duracao_disp))
    linhas.append(f"📍 {MORADA_OFICINA}")
    linhas.append(t("resumo_preco", idioma, preco=preco_formatado(sessao.get("preco"), idioma)))
    linhas.append("")
    linhas.append(t("confirmado_instrucao", idioma))
    linhas.append("")
    linhas.append(t("confirmado_rodape", idioma))
    return "\n".join(linhas)


def mensagem_notificacao_provider(de, sessao, id_agendamento):
    """Sempre em português, independentemente do idioma do cliente — é o
    idioma de trabalho da equipa/dono do negócio."""
    linhas = [f"🆕📅 *Novo pedido confirmado (#{id_agendamento})*", ""]
    linhas.append(f"👤 Cliente: {sessao.get('nome') or 'sem nome'}")
    linhas.append(f"📱 Contacto: {formatar_telefone(de)}")
    linhas.append(f"🔧 Serviço: {sessao['servico']}")
    if sessao.get("extra"):
        linhas.append(f"➕ Extra: {sessao['extra']}")
    linhas.append(f"📅 Data: {sessao['data']} às {sessao['hora']}")
    linhas.append(f"💰 Preço: {preco_formatado(sessao.get('preco'))}")
    linhas.append("")
    linhas.append("Responda com: CONTACTAR, REAGENDAR, CANCELAR ou CONCLUIDO seguido do número da marcação.")
    return "\n".join(linhas)


# ---------------------------------------------------------------------------
# Fluxo "Wrap & Proteção" — mais consultivo, termina em pedido de orçamento
# ---------------------------------------------------------------------------
def passo_wrap_veiculo(de, idioma):
    enviar_texto(de, t("wrap_passo1", idioma) + "\n\n" + t("rodape_padrao", idioma))


def passo_wrap_tipo(de, idioma):
    enviar_botoes(de, t("wrap_passo2_corpo", idioma), [
        {"id": "wrap_total", "titulo": t("wrap_total_botao", idioma)},
        {"id": "wrap_parcial", "titulo": t("wrap_parcial_botao", idioma)},
        {"id": ID_CANCELAR, "titulo": t("botao_cancelar", idioma)},
    ], idioma, rodape=t("rodape_padrao", idioma))


def passo_wrap_cor(de, idioma):
    enviar_texto(de, t("wrap_passo3", idioma) + "\n\n" + t("rodape_padrao", idioma))


def passo_wrap_fotos_pergunta(de, idioma):
    enviar_botoes(de, t("wrap_fotos_pergunta_corpo", idioma), [
        {"id": "wrap_fotos_sim", "titulo": t("wrap_fotos_sim_botao", idioma)},
        {"id": "wrap_fotos_nao", "titulo": t("wrap_fotos_nao_botao", idioma)},
    ], idioma, rodape=t("rodape_padrao", idioma))


def finalizar_pedido_wrap(de, idioma, sessao, pedido_id=None):
    tipo_wrap_pt = "Wrap total" if sessao.get("wrap_tipo") == "wrap_total" else "Wrap parcial"
    num_fotos = contar_fotografias(pedido_id)
    linhas = ["📋 *Pedido de orçamento — Wrap & Proteção*", ""]
    if pedido_id:
        linhas.append(f"🆔 Pedido #{pedido_id}")
    linhas.append(f"👤 Cliente: {sessao.get('nome') or 'sem nome'}")
    linhas.append(f"📱 Contacto: {formatar_telefone(de)}")
    linhas.append(f"🚗 Veículo: {sessao.get('wrap_veiculo', '-')}")
    linhas.append(f"🎨 Tipo: {tipo_wrap_pt}")
    linhas.append(f"🖌️ Cor/acabamento: {sessao.get('wrap_cor', '-')}")
    linhas.append(f"📸 Fotografias recebidas: {num_fotos}")
    texto_provider = "\n".join(linhas)  # sempre em português, ver mensagem_notificacao_provider

    veiculo = sessao.get("wrap_veiculo") or t("wrap_veiculo_generico", idioma)
    enviar_texto(de, t("wrap_finalizado_cliente", idioma, veiculo=veiculo))
    if PROVIDER_WHATSAPP:
        enviar_texto(PROVIDER_WHATSAPP, texto_provider + f"\n\n💬 Responda com: CONTACTAR {formatar_telefone(de)}")


# ---------------------------------------------------------------------------
# Menu principal / orçamento genérico / gerir marcação / humano / idioma
# ---------------------------------------------------------------------------
def enviar_menu_principal(de, idioma, saudacao=True):
    corpo = t("menu_corpo", idioma)
    if saudacao:
        nome = primeiro_nome(carregar_sessao(de).get("nome"))
        if nome:
            ola = t("saudacao_volta", idioma, nome=nome, oficina=NOME_OFICINA)
        else:
            ola = t("saudacao_novo", idioma, oficina=NOME_OFICINA)
        corpo = f"{ola}\n\n{corpo}"
    enviar_lista(de, corpo, t("menu_titulo_lista", idioma), MENU_PRINCIPAL, idioma, botao=t("menu_botao", idioma))


def enviar_seletor_idioma(de):
    """Mensagem fixa nos 3 idiomas ao mesmo tempo + botões para escolher —
    não depende de nenhum idioma já escolhido, porque é isso que resolve."""
    enviar_botoes(de, TEXTO_SELETOR_IDIOMA, BOTOES_IDIOMA, "pt")  # idioma aqui só afeta tx(), que já são strings simples


def iniciar_escolha_categoria(de, idioma, sessao):
    """Ponto único que arranca o fluxo 'Marcar': mostra as categorias
    (Limpeza/Estética/Wrap). Reutilizado em todos os sítios que precisam de
    (re)começar a marcação — menu principal, gestão de marcação, voltar."""
    sessao["fluxo"] = "escolher_categoria"
    guardar_sessao(de, sessao)
    enviar_botoes(de, t("categoria_pergunta", idioma), CATEGORIAS_MARCAR, idioma, rodape=t("rodape_padrao", idioma))


def passo_orcamento_generico(de, idioma):
    enviar_texto(de, t("orcamento_pedido", idioma) + "\n\n" + t("rodape_padrao", idioma))


def mostrar_gestao_marcacao(de, idioma):
    ag = ultimo_agendamento_ativo(de)
    if not ag:
        enviar_texto(de, t("gerir_sem_marcacao", idioma))
        return
    servico_disp = nome_servico_traduzido(ag["servico"], idioma)
    duracao_disp = duracao_traduzida(ag["servico"], ag.get("duracao", "-"), idioma)
    corpo = t("gerir_corpo", idioma, id=ag["id"], servico=servico_disp, data=ag["data"], hora=ag["hora"],
              duracao=duracao_disp, preco=preco_formatado(ag.get("preco"), idioma))
    enviar_botoes(de, corpo, [
        {"id": f"reagendar_{ag['id']}", "titulo": t("botao_reagendar", idioma)},
        {"id": f"cancelar_ag_{ag['id']}", "titulo": t("botao_cancelar_marcacao", idioma)},
        {"id": "mp_marcar", "titulo": t("botao_nova_marcacao", idioma)},
    ], idioma)


def falar_com_equipa(de, idioma, sessao):
    enviar_texto(de, t("humano_cliente", idioma))
    if PROVIDER_WHATSAPP:
        nome = sessao.get("nome") or "sem nome"
        enviar_texto(PROVIDER_WHATSAPP, f"💬 *Pedido de contacto direto*\n\n👤 {nome}\n"
                                         f"📱 {formatar_telefone(de)}\n\nResponda com: CONTACTAR {formatar_telefone(de)}")


def mensagem_ajuda(idioma):
    linhas = [t("ajuda_header", idioma), "", t("ajuda_menu", idioma), t("ajuda_voltar", idioma),
              t("ajuda_cancelar", idioma), t("ajuda_gerir", idioma), t("ajuda_ajuda", idioma),
              t("ajuda_humano", idioma), t("ajuda_idioma", idioma)]
    return "\n".join(linhas)


def mensagem_nao_entendi(idioma):
    return t("nao_entendi", idioma)


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
            return Response("Painel não configurado.", 401)
        auth = request.authorization
        if not auth or auth.username != DASHBOARD_USER or auth.password != DASHBOARD_PASSWORD:
            return Response(
                "Autenticação necessária.", 401,
                {"WWW-Authenticate": 'Basic realm="Painel Spotless"'},
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


@app.route("/api/pedidos", methods=["GET"])
@requer_autenticacao
def api_pedidos():
    return jsonify(listar_pedidos_orcamento()), 200


@app.route("/api/pedidos/<int:pedido_id>", methods=["GET"])
@requer_autenticacao
def api_pedido_detalhe(pedido_id):
    pedido = obter_pedido_orcamento(pedido_id)
    if not pedido:
        return jsonify(erro="Pedido não encontrado"), 404
    pedido["fotografias"] = listar_fotografias(pedido_id)
    return jsonify(pedido), 200


@app.route("/media/<path:nome_ficheiro>", methods=["GET"])
@requer_autenticacao
def media(nome_ficheiro):
    return send_from_directory(MEDIA_DIR, nome_ficheiro)


@app.route("/dashboard", methods=["GET"])
@requer_autenticacao
def dashboard():
    return DASHBOARD_HTML


DASHBOARD_HTML = """
<!doctype html>
<html lang="pt">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Painel de Agendamentos</title>
<style>
  :root{
    --bg:#0d0f12; --panel:#15181d; --panel2:#1b1f26; --border:#262b33;
    --gold:#e8b923; --text:#f2f3f5; --muted:#9aa1ac;
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--text);font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;}
  header{padding:24px 28px;border-bottom:1px solid var(--border);display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:12px;}
  header h1{margin:0;font-size:20px;letter-spacing:.3px;}
  header h1 span{color:var(--gold);}
  header .sub{color:var(--muted);font-size:13px;margin-top:4px;}
  .wrap{padding:24px 28px;max-width:1200px;margin:0 auto;}
  .stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:14px;margin-bottom:22px;}
  .card{background:var(--panel);border:1px solid var(--border);border-radius:12px;padding:16px 18px;}
  .card .n{font-size:26px;font-weight:700;color:var(--gold);}
  .card .l{color:var(--muted);font-size:12.5px;margin-top:4px;}
  .lista{background:var(--panel);border:1px solid var(--border);border-radius:12px;overflow:hidden;}
  .lista h2{font-size:15px;margin:0;padding:16px 18px;border-bottom:1px solid var(--border);color:var(--muted);font-weight:600;letter-spacing:.3px;text-transform:uppercase;}
  table{width:100%;border-collapse:collapse;}
  th,td{text-align:left;padding:12px 18px;font-size:14px;border-bottom:1px solid var(--border);}
  th{color:var(--muted);font-weight:600;font-size:12px;text-transform:uppercase;letter-spacing:.4px;}
  tr:last-child td{border-bottom:none;}
  tr:hover td{background:var(--panel2);}
  .tag{display:inline-block;background:rgba(232,185,35,.15);color:var(--gold);padding:3px 9px;border-radius:20px;font-size:12px;font-weight:600;}
  .estado-cancelado{color:#e05252;}
  .vazio{padding:40px 18px;text-align:center;color:var(--muted);}
  .refresh{color:var(--muted);font-size:12px;}
  a.btn{background:var(--gold);color:#1a1400;padding:8px 14px;border-radius:8px;text-decoration:none;font-size:13px;font-weight:700;}
  tr.clicavel{cursor:pointer;}
  .modal-overlay{display:none;position:fixed;inset:0;background:rgba(0,0,0,.6);align-items:center;justify-content:center;z-index:50;}
  .modal-overlay.aberto{display:flex;}
  .modal-caixa{background:var(--panel);border:1px solid var(--border);border-radius:12px;max-width:640px;width:92%;max-height:86vh;overflow-y:auto;}
  .modal-cabecalho{display:flex;justify-content:space-between;align-items:center;padding:16px 18px;border-bottom:1px solid var(--border);}
  .modal-cabecalho h3{margin:0;font-size:16px;}
  .modal-fechar{cursor:pointer;color:var(--muted);font-size:18px;}
  .modal-corpo{padding:18px;font-size:14px;line-height:1.7;}
  .modal-corpo .linha{margin-bottom:6px;}
  .galeria{display:grid;grid-template-columns:repeat(auto-fill,minmax(90px,1fr));gap:8px;margin-top:12px;}
  .galeria img{width:100%;height:90px;object-fit:cover;border-radius:8px;cursor:zoom-in;border:1px solid var(--border);}
  .lightbox{display:none;position:fixed;inset:0;background:rgba(0,0,0,.88);align-items:center;justify-content:center;z-index:60;cursor:zoom-out;}
  .lightbox.aberto{display:flex;}
  .lightbox img{max-width:92vw;max-height:92vh;border-radius:8px;}
</style>
</head>
<body>
<header>
  <div>
    <h1><span>COVER</span>LAB — Painel de Agendamentos</h1>
    <div class="sub">Marcações recebidas automaticamente via WhatsApp</div>
  </div>
  <a class="btn" href="javascript:location.reload()">🔄 Atualizar</a>
</header>

<div class="wrap">
  <div class="stats">
    <div class="card"><div class="n" id="st-total">0</div><div class="l">Total de agendamentos</div></div>
    <div class="card"><div class="n" id="st-hoje">0</div><div class="l">Marcados hoje (por criação)</div></div>
    <div class="card"><div class="n" id="st-clientes">0</div><div class="l">Clientes únicos</div></div>
    <div class="card"><div class="n" id="st-receita">CHF 0</div><div class="l">Receita estimada (confirmados)</div></div>
  </div>

  <div class="lista">
    <h2>Agendamentos</h2>
    <div id="conteudo"><div class="vazio">A carregar…</div></div>
  </div>

  <div class="lista" style="margin-top:22px;">
    <h2>Pedidos de orçamento (Wrap &amp; Proteção)</h2>
    <div id="conteudo-pedidos"><div class="vazio">A carregar…</div></div>
  </div>

  <div class="refresh" style="margin-top:10px;">Atualiza-se sozinho a cada 20 segundos.</div>
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

<div id="lightbox" class="lightbox" onclick="this.classList.remove('aberto')">
  <img id="lightbox-img" src="">
</div>

<script>
async function carregar(){
  const resp = await fetch('/api/agendamentos');
  const dados = await resp.json();

  document.getElementById('st-total').textContent = dados.length;

  const hojeStr = new Date().toISOString().slice(0,10);
  const hoje = dados.filter(d => (d.criado_em||'').slice(0,10) === hojeStr).length;
  document.getElementById('st-hoje').textContent = hoje;

  const clientes = new Set(dados.map(d => d.telefone));
  document.getElementById('st-clientes').textContent = clientes.size;

  const receita = dados.filter(d => d.estado === 'confirmado').reduce((s,d) => s + (d.preco||0), 0);
  document.getElementById('st-receita').textContent = 'CHF ' + receita.toFixed(0);

  const cont = document.getElementById('conteudo');
  if(dados.length === 0){
    cont.innerHTML = '<div class="vazio">Ainda não há marcações. Manda uma mensagem ao bot no WhatsApp para testar 👋</div>';
    return;
  }

  let html = '<table><thead><tr><th>Cliente</th><th>Serviço</th><th>Data</th><th>Hora</th><th>Preço</th><th>Estado</th><th>Recebido em</th></tr></thead><tbody>';
  dados.forEach(d => {
    const criado = d.criado_em ? new Date(d.criado_em).toLocaleString('pt-PT') : '-';
    const classeEstado = d.estado !== 'confirmado' ? 'estado-cancelado' : '';
    html += `<tr>
      <td>${d.nome || d.telefone}<br><span style="color:var(--muted);font-size:12px;">${d.telefone}</span></td>
      <td><span class="tag">${d.servico}</span>${d.extra ? '<br><span style="color:var(--muted);font-size:12px;">+ '+d.extra+'</span>' : ''}</td>
      <td>${d.data || '-'}</td>
      <td>${d.hora || '-'}</td>
      <td>${d.preco ? 'CHF '+d.preco : '-'}</td>
      <td class="${classeEstado}">${d.estado}</td>
      <td style="color:var(--muted);">${criado}</td>
    </tr>`;
  });
  html += '</tbody></table>';
  cont.innerHTML = html;
}

async function carregarPedidos(){
  const resp = await fetch('/api/pedidos');
  if(!resp.ok){ return; }
  const dados = await resp.json();

  const cont = document.getElementById('conteudo-pedidos');
  if(dados.length === 0){
    cont.innerHTML = '<div class="vazio">Ainda não há pedidos de orçamento com fotografias.</div>';
    return;
  }

  let html = '<table><thead><tr><th>Cliente</th><th>Veículo</th><th>Wrap</th><th>Estado</th><th>Fotos</th><th>Pedido em</th></tr></thead><tbody>';
  dados.forEach(p => {
    const criado = p.criado_em ? new Date(p.criado_em).toLocaleString('pt-PT') : '-';
    html += `<tr class="clicavel" onclick="abrirPedido(${p.id})">
      <td>${p.nome || p.telefone}<br><span style="color:var(--muted);font-size:12px;">${p.telefone}</span></td>
      <td>${p.veiculo || '-'}${p.ano_veiculo ? ' ('+p.ano_veiculo+')' : ''}</td>
      <td><span class="tag">${p.tipo_wrap || '-'}</span>${p.cor_acabamento ? '<br><span style="color:var(--muted);font-size:12px;">'+p.cor_acabamento+'</span>' : ''}</td>
      <td>${p.estado}</td>
      <td>${p.num_fotos || 0}</td>
      <td style="color:var(--muted);">${criado}</td>
    </tr>`;
  });
  html += '</tbody></table>';
  cont.innerHTML = html;
}

async function abrirPedido(id){
  const resp = await fetch('/api/pedidos/' + id);
  if(!resp.ok){ return; }
  const p = await resp.json();
  document.getElementById('modal-titulo').textContent = 'Pedido de orçamento #' + p.id;

  let html = '';
  html += `<div class="linha">👤 Cliente: ${p.nome || p.telefone}</div>`;
  html += `<div class="linha">📱 Contacto: ${p.telefone}</div>`;
  html += `<div class="linha">🚗 Veículo: ${p.veiculo || '-'}${p.ano_veiculo ? ' ('+p.ano_veiculo+')' : ''}</div>`;
  html += `<div class="linha">🎨 Tipo: ${p.tipo_wrap || '-'}</div>`;
  html += `<div class="linha">🖌️ Cor/acabamento: ${p.cor_acabamento || '-'}</div>`;
  html += `<div class="linha">📌 Estado: ${p.estado}</div>`;
  html += `<div class="linha">🕓 Pedido em: ${p.criado_em ? new Date(p.criado_em).toLocaleString('pt-PT') : '-'}</div>`;

  if(p.fotografias && p.fotografias.length){
    html += '<div class="linha" style="margin-top:10px;">📸 Fotografias (' + p.fotografias.length + '):</div>';
    html += '<div class="galeria">';
    p.fotografias.forEach(f => {
      html += `<img src="/media/${f.nome_ficheiro}" onclick="abrirLightbox('/media/${f.nome_ficheiro}')">`;
    });
    html += '</div>';
  } else {
    html += '<div class="linha" style="margin-top:10px;color:var(--muted);">Sem fotografias enviadas.</div>';
  }

  document.getElementById('modal-corpo').innerHTML = html;
  document.getElementById('modal-pedido').classList.add('aberto');
}

function fecharModal(){
  document.getElementById('modal-pedido').classList.remove('aberto');
}

function fecharModalSeExterior(event){
  if(event.target.id === 'modal-pedido') fecharModal();
}

function abrirLightbox(src){
  document.getElementById('lightbox-img').src = src;
  document.getElementById('lightbox').classList.add('aberto');
}

carregar();
carregarPedidos();
setInterval(carregar, 20000);
setInterval(carregarPedidos, 20000);
</script>
</body>
</html>
"""


@app.route("/versao", methods=["GET"])
def versao():
    return jsonify(versao="v4-multilingue", fluxos=["limpeza", "estetica", "wrap"], idiomas=list(IDIOMAS_VALIDOS)), 200


@app.route("/webhook", methods=["GET"])
def verificar_webhook():
    if request.args.get("hub.verify_token") == VERIFY_TOKEN:
        return request.args.get("hub.challenge", ""), 200
    return "Token inválido", 403


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
    sessao_antiga = carregar_sessao(de)
    nova = sessao_preservando_perfil(sessao_antiga) if manter_nome else \
        ({"idioma": sessao_antiga["idioma"]} if sessao_antiga.get("idioma") else {})
    guardar_sessao(de, nova)
    return nova


def sessao_em_curso(sessao):
    """Considera-se 'em curso' se já escolheu categoria mas ainda não confirmou."""
    return bool(sessao.get("categoria") or sessao.get("fluxo"))


def processar_comando_texto(de, idioma, sessao, comando):
    if comando in COMANDOS_IDIOMA:
        enviar_seletor_idioma(de)
        return True
    if comando == "menu":
        reiniciar_sessao(de)
        enviar_menu_principal(de, idioma, saudacao=True)
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
        reiniciar_sessao(de)
        enviar_texto(de, t("processo_cancelado", idioma))
        return True
    if comando == "voltar":
        voltar_um_passo(de, idioma, sessao)
        return True
    return False


def voltar_um_passo(de, idioma, sessao):
    fluxo = sessao.get("fluxo")
    categoria = sessao.get("categoria")

    if fluxo == "wrap":
        if sessao.get("aguardando_fotos"):
            sessao.pop("aguardando_fotos", None); guardar_sessao(de, sessao); passo_wrap_fotos_pergunta(de, idioma)
        elif "wrap_cor" in sessao:
            sessao.pop("wrap_cor", None); sessao.pop("pedido_id", None); guardar_sessao(de, sessao); passo_wrap_cor(de, idioma)
        elif "wrap_tipo" in sessao:
            sessao.pop("wrap_tipo", None); guardar_sessao(de, sessao); passo_wrap_tipo(de, idioma)
        elif "wrap_veiculo" in sessao:
            sessao.pop("wrap_veiculo", None); guardar_sessao(de, sessao); passo_wrap_veiculo(de, idioma)
        else:
            reiniciar_sessao(de); enviar_menu_principal(de, idioma, saudacao=False)
        return

    if categoria in ("cat_limpeza", "cat_estetica"):
        if "hora" in sessao:
            sessao.pop("hora", None); guardar_sessao(de, sessao); passo_hora(de, idioma)
        elif "data" in sessao:
            sessao.pop("data", None); guardar_sessao(de, sessao); passo_data(de, idioma)
        elif "extra_id" in sessao:
            sessao.pop("extra_id", None); guardar_sessao(de, sessao)
            (passo_limpeza_extra if categoria == "cat_limpeza" else passo_estetica_extra)(de, idioma)
        elif categoria == "cat_limpeza" and "tamanho_id" in sessao:
            sessao.pop("tamanho_id", None); guardar_sessao(de, sessao); passo_limpeza_tamanho(de, idioma)
        elif categoria == "cat_estetica" and "estado_id" in sessao:
            sessao.pop("estado_id", None); guardar_sessao(de, sessao); passo_estetica_estado(de, idioma)
        elif "tipo_id" in sessao:
            sessao.pop("tipo_id", None); sessao.pop("categoria", None)
            iniciar_escolha_categoria(de, idioma, sessao)
        else:
            reiniciar_sessao(de); enviar_menu_principal(de, idioma, saudacao=False)
        return

    reiniciar_sessao(de)
    enviar_menu_principal(de, idioma, saudacao=False)


@app.route("/webhook", methods=["POST"])
def receber_mensagem():
    data = request.get_json(force=True)
    try:
        entry = data["entry"][0]["changes"][0]["value"]
        if "messages" not in entry:
            return jsonify(status="ignorado"), 200

        msg = entry["messages"][0]
        de = msg["from"]
        sessao = carregar_sessao(de)

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
                enviar_menu_principal(de, novo_idioma, saudacao=True)
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
                    reiniciar_sessao(de)
                    enviar_menu_principal(de, idioma, saudacao=True)
                return jsonify(status="ok"), 200

            if sessao.get("fluxo") == "wrap" and "wrap_veiculo" not in sessao:
                sessao["wrap_veiculo"] = msg["text"]["body"].strip()
                guardar_sessao(de, sessao)
                passo_wrap_tipo(de, idioma)
                return jsonify(status="ok"), 200

            if sessao.get("fluxo") == "wrap" and "wrap_tipo" in sessao and "wrap_cor" not in sessao:
                sessao["wrap_cor"] = msg["text"]["body"].strip()
                pedido_id = criar_pedido_orcamento(de, sessao)
                sessao["pedido_id"] = pedido_id
                guardar_sessao(de, sessao)
                passo_wrap_fotos_pergunta(de, idioma)
                return jsonify(status="ok"), 200

            if sessao.get("fluxo") == "wrap" and sessao.get("aguardando_fotos"):
                if texto == "concluir":
                    finalizar_pedido_wrap(de, idioma, sessao, sessao.get("pedido_id"))
                    reiniciar_sessao(de)
                else:
                    enviar_texto(de, t("wrap_foto_formato_invalido", idioma))
                return jsonify(status="ok"), 200

            if sessao.get("fluxo") == "orcamento":
                enviar_texto(de, t("orcamento_recebido_cliente", idioma))
                if PROVIDER_WHATSAPP:
                    enviar_texto(PROVIDER_WHATSAPP,
                                 f"💰 *Pedido de orçamento genérico*\n\n👤 {sessao.get('nome') or 'sem nome'}\n"
                                 f"📱 {formatar_telefone(de)}\n📝 \"{msg['text']['body'].strip()}\"")
                reiniciar_sessao(de)
                return jsonify(status="ok"), 200

            # sessão em curso (categoria já escolhida, mas mensagem de texto inesperada)
            if sessao_em_curso(sessao):
                sessao["_a_confirmar_retomar"] = True
                guardar_sessao(de, sessao)
                enviar_botoes(de, t("retomar_pergunta", idioma), [
                    {"id": "retomar_continuar", "titulo": t("botao_continuar", idioma)},
                    {"id": "retomar_recomecar", "titulo": t("botao_recomecar", idioma)},
                ], idioma)
                return jsonify(status="ok"), 200

            # primeira mensagem / sem sessão em curso -> menu principal
            enviar_menu_principal(de, idioma, saudacao=True)
            return jsonify(status="ok"), 200

        # --- Botões -----------------------------------------------------
        if tipo == "interactive" and msg["interactive"]["type"] == "button_reply":
            id_botao = msg["interactive"]["button_reply"]["id"]

            if id_botao in LANG_IDS:  # "Alterar idioma" com sessão já ativa
                # Limpa os campos do processo em curso (categoria, passos já
                # escolhidos, etc.) e preserva só o nome — para dados antigos
                # nunca fazerem o bot saltar etapas depois de mudar de idioma.
                novo_idioma = LANG_IDS[id_botao]
                sessao = sessao_preservando_perfil(sessao)
                sessao["idioma"] = novo_idioma
                guardar_sessao(de, sessao)
                enviar_menu_principal(de, novo_idioma, saudacao=True)
                return jsonify(status="ok"), 200

            if id_botao == ID_CANCELAR:
                reiniciar_sessao(de)
                enviar_texto(de, t("processo_cancelado", idioma))
                return jsonify(status="ok"), 200

            if id_botao in ("retomar_continuar", "retomar_recomecar"):
                sessao.pop("_a_confirmar_retomar", None)
                if id_botao == "retomar_continuar":
                    guardar_sessao(de, sessao)
                    reenviar_passo_atual(de, idioma, sessao)
                else:
                    reiniciar_sessao(de)
                    enviar_menu_principal(de, idioma, saudacao=True)
                return jsonify(status="ok"), 200

            if id_botao == "mp_marcar":  # ex.: botão "Nova marcação" em "Gerir a minha marcação"
                iniciar_escolha_categoria(de, idioma, sessao)
                return jsonify(status="ok"), 200

            if id_botao in NOME_CATEGORIA:  # categoria dentro de "Marcar"
                if id_botao == "cat_wrap":
                    sessao.update({"fluxo": "wrap", "categoria": "cat_wrap"})
                    guardar_sessao(de, sessao)
                    passo_wrap_veiculo(de, idioma)
                else:
                    sessao.update({"fluxo": "marcar", "categoria": id_botao})
                    guardar_sessao(de, sessao)
                    (passo_limpeza_tipo if id_botao == "cat_limpeza" else passo_estetica_servico)(de, idioma)
                return jsonify(status="ok"), 200

            if id_botao in ("wrap_total", "wrap_parcial"):
                sessao["wrap_tipo"] = id_botao
                guardar_sessao(de, sessao)
                passo_wrap_cor(de, idioma)
                return jsonify(status="ok"), 200

            if id_botao == "wrap_fotos_sim":
                sessao["aguardando_fotos"] = True
                guardar_sessao(de, sessao)
                enviar_texto(de, t("wrap_fotos_pedir", idioma))
                return jsonify(status="ok"), 200

            if id_botao in ("wrap_fotos_nao", "wrap_fotos_concluir"):
                pedido_id = sessao.get("pedido_id")
                finalizar_pedido_wrap(de, idioma, sessao, pedido_id)
                reiniciar_sessao(de)
                return jsonify(status="ok"), 200

            if id_botao == "confirmar":
                id_ag = guardar_agendamento(de, sessao)
                enviar_texto(de, mensagem_confirmacao_final(sessao, idioma))
                if PROVIDER_WHATSAPP:
                    enviar_texto(PROVIDER_WHATSAPP, mensagem_notificacao_provider(de, sessao, id_ag))
                reiniciar_sessao(de)
                return jsonify(status="ok"), 200

            if id_botao == "alterar":
                categoria = sessao.get("categoria")
                for campo in ("tipo_id", "tamanho_id", "estado_id", "extra_id", "data", "hora",
                              "servico", "extra", "preco", "duracao"):
                    sessao.pop(campo, None)
                guardar_sessao(de, sessao)
                (passo_limpeza_tipo if categoria == "cat_limpeza" else passo_estetica_servico)(de, idioma)
                return jsonify(status="ok"), 200

            if id_botao.startswith("reagendar_"):
                id_ag = int(id_botao.split("_")[-1])
                atualizar_estado_agendamento(id_ag, "reagendado")
                sessao = sessao_preservando_perfil(sessao)
                enviar_texto(de, t("reagendar_aviso", idioma))
                iniciar_escolha_categoria(de, idioma, sessao)
                return jsonify(status="ok"), 200

            if id_botao.startswith("cancelar_ag_"):
                id_ag = int(id_botao.split("_")[-1])
                atualizar_estado_agendamento(id_ag, "cancelado")
                enviar_texto(de, t("cancelado_cliente", idioma))
                if PROVIDER_WHATSAPP:
                    enviar_texto(PROVIDER_WHATSAPP, f"❌ Marcação #{id_ag} cancelada pelo cliente {formatar_telefone(de)}.")
                return jsonify(status="ok"), 200

            enviar_texto(de, mensagem_nao_entendi(idioma))
            return jsonify(status="ok"), 200

        # --- Listas -------------------------------------------------------
        if tipo == "interactive" and msg["interactive"]["type"] == "list_reply":
            id_escolhido = msg["interactive"]["list_reply"]["id"]

            if id_escolhido == ID_CANCELAR:
                reiniciar_sessao(de)
                enviar_texto(de, t("processo_cancelado", idioma))
                return jsonify(status="ok"), 200

            if id_escolhido == ID_VOLTAR:
                voltar_um_passo(de, idioma, sessao)
                return jsonify(status="ok"), 200

            # Menu principal
            if id_escolhido == "mp_marcar":
                iniciar_escolha_categoria(de, idioma, sessao)
                return jsonify(status="ok"), 200
            if id_escolhido == "mp_orcamento":
                sessao["fluxo"] = "orcamento"
                guardar_sessao(de, sessao)
                passo_orcamento_generico(de, idioma)
                return jsonify(status="ok"), 200
            if id_escolhido == "mp_gerir":
                mostrar_gestao_marcacao(de, idioma)
                return jsonify(status="ok"), 200
            if id_escolhido == "mp_humano":
                falar_com_equipa(de, idioma, sessao)
                reiniciar_sessao(de)
                return jsonify(status="ok"), 200
            if id_escolhido == "mp_idioma":
                enviar_seletor_idioma(de)
                return jsonify(status="ok"), 200

            categoria = sessao.get("categoria")

            # Limpeza
            if categoria == "cat_limpeza":
                if "tipo_id" not in sessao:
                    sessao["tipo_id"] = id_escolhido; guardar_sessao(de, sessao); passo_limpeza_tamanho(de, idioma)
                elif "tamanho_id" not in sessao:
                    sessao["tamanho_id"] = id_escolhido; guardar_sessao(de, sessao); passo_limpeza_extra(de, idioma)
                elif "extra_id" not in sessao:
                    sessao["extra_id"] = id_escolhido; guardar_sessao(de, sessao); passo_data(de, idioma)
                elif "data" not in sessao:
                    sessao["data"] = msg["interactive"]["list_reply"]["title"]; guardar_sessao(de, sessao); passo_hora(de, idioma)
                elif "hora" not in sessao:
                    sessao["hora"] = msg["interactive"]["list_reply"]["title"]; guardar_sessao(de, sessao)
                    passo_resumo(de, idioma, sessao)
                return jsonify(status="ok"), 200

            # Estética
            if categoria == "cat_estetica":
                if "tipo_id" not in sessao:
                    sessao["tipo_id"] = id_escolhido; guardar_sessao(de, sessao); passo_estetica_estado(de, idioma)
                elif "estado_id" not in sessao:
                    sessao["estado_id"] = id_escolhido; guardar_sessao(de, sessao); passo_estetica_extra(de, idioma)
                elif "extra_id" not in sessao:
                    sessao["extra_id"] = id_escolhido; guardar_sessao(de, sessao); passo_data(de, idioma)
                elif "data" not in sessao:
                    sessao["data"] = msg["interactive"]["list_reply"]["title"]; guardar_sessao(de, sessao); passo_hora(de, idioma)
                elif "hora" not in sessao:
                    sessao["hora"] = msg["interactive"]["list_reply"]["title"]; guardar_sessao(de, sessao)
                    passo_resumo(de, idioma, sessao)
                return jsonify(status="ok"), 200

            enviar_texto(de, mensagem_nao_entendi(idioma))
            return jsonify(status="ok"), 200

        # --- Fotografias do pedido de orçamento Wrap & Proteção -------------
        if tipo == "image" and sessao.get("fluxo") == "wrap" and sessao.get("aguardando_fotos") and sessao.get("pedido_id"):
            pedido_id = sessao["pedido_id"]
            media_id = msg["image"]["id"]
            mime_tipo = msg["image"].get("mime_type", "")

            conteudo, mime_confirmado = None, None
            if mime_tipo in MIME_IMAGENS_VALIDAS:
                try:
                    conteudo, mime_confirmado = descarregar_media_whatsapp(media_id)
                except requests.RequestException:
                    conteudo = None

            if not conteudo:
                enviar_texto(de, t("wrap_foto_formato_invalido", idioma))
                return jsonify(status="ok"), 200

            nome_ficheiro = guardar_media_local(pedido_id, media_id, conteudo, mime_confirmado or mime_tipo)
            adicionar_fotografia(pedido_id, nome_ficheiro, mime_confirmado or mime_tipo)
            total_fotos = contar_fotografias(pedido_id)

            if total_fotos >= 5:
                enviar_texto(de, t("wrap_foto_recebida_contagem", idioma, atual=total_fotos, total=5))
                enviar_texto(de, t("wrap_fotos_limite_atingido", idioma))
                finalizar_pedido_wrap(de, idioma, sessao, pedido_id)
                reiniciar_sessao(de)
            else:
                enviar_texto(de, t("wrap_foto_recebida_contagem", idioma, atual=total_fotos, total=5))
                enviar_botoes(de, t("wrap_fotos_mais_ou_concluir", idioma), [
                    {"id": "wrap_fotos_concluir", "titulo": t("wrap_fotos_concluir_botao", idioma)},
                ], idioma)
            return jsonify(status="ok"), 200

        # --- Qualquer outro tipo (áudio, imagem fora de contexto, sticker, etc.) ---
        enviar_texto(de, mensagem_nao_entendi(idioma))

    except (KeyError, IndexError):
        pass  # notificações de status (entregue/lido) chegam neste mesmo endpoint — ignora-as

    return jsonify(status="ok"), 200


def reenviar_passo_atual(de, idioma, sessao):
    """Reenvia o ecrã correspondente ao ponto exato onde a sessão ficou."""
    categoria = sessao.get("categoria")
    fluxo = sessao.get("fluxo")

    if fluxo == "wrap":
        if sessao.get("aguardando_fotos"):
            enviar_texto(de, t("wrap_fotos_pedir", idioma))
        elif "wrap_cor" in sessao:
            passo_wrap_fotos_pergunta(de, idioma)
        elif "wrap_tipo" in sessao:
            passo_wrap_cor(de, idioma)
        elif "wrap_veiculo" in sessao:
            passo_wrap_tipo(de, idioma)
        else:
            passo_wrap_veiculo(de, idioma)
        return

    if categoria == "cat_limpeza":
        if "hora" in sessao:
            passo_resumo(de, idioma, sessao)
        elif "data" in sessao:
            passo_hora(de, idioma)
        elif "extra_id" in sessao:
            passo_data(de, idioma)
        elif "tamanho_id" in sessao:
            passo_limpeza_extra(de, idioma)
        elif "tipo_id" in sessao:
            passo_limpeza_tamanho(de, idioma)
        else:
            passo_limpeza_tipo(de, idioma)
        return

    if categoria == "cat_estetica":
        if "hora" in sessao:
            passo_resumo(de, idioma, sessao)
        elif "data" in sessao:
            passo_hora(de, idioma)
        elif "extra_id" in sessao:
            passo_data(de, idioma)
        elif "estado_id" in sessao:
            passo_estetica_extra(de, idioma)
        elif "tipo_id" in sessao:
            passo_estetica_estado(de, idioma)
        else:
            passo_estetica_servico(de, idioma)
        return

    enviar_menu_principal(de, idioma, saudacao=False)


if __name__ == "__main__":
    porta = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=porta, debug=True)
