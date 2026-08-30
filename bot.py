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

# URL pública onde este serviço está publicado (ex.: https://o-teu-servico.onrender.com).
# Usada só para construir a ligação direta ao dossiê de um pedido no painel,
# enviada na notificação interna (ver link_dossie_pedido()). Opcional: se não
# estiver definida, tenta-se deduzir do próprio pedido HTTP em curso; se isso
# também não for possível (ex.: fora de um pedido Flask), a ligação é omitida.
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")

GRAPH_URL = f"https://graph.facebook.com/v21.0/{PHONE_NUMBER_ID}/messages"

NOME_OFICINA = "Spotless Car Detail (TESTE)"
MORADA_OFICINA = "Spotless Car Detail, Zermatt"

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
    "carrinho", "cart", "warenkorb",
    "rapido", "quick", "schnell",
}
COMANDOS_IDIOMA = {"idioma", "sprache", "language"}
COMANDOS_CARRINHO = {"carrinho", "cart", "warenkorb"}
COMANDOS_RAPIDO = {"rapido", "quick", "schnell"}

# Modos possíveis do fluxo "Wrap & Proteção" (escolhidos logo à entrada).
# Guardados na sessão em "wrap_modo" e na base de dados na coluna
# "modo_pedido", para o painel distinguir cada tipo de pedido.
MODO_RAPIDO = "rapido"
MODO_DETALHE = "detalhe"
MODO_ESPECIALISTA = "especialista"
MODO_NOMES_PT = {
    MODO_RAPIDO: "Pedido rápido",
    MODO_DETALHE: "Configuração detalhada",
    MODO_ESPECIALISTA: "Contacto com especialista",
}

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
    "rodape_padrao": {"pt": "Escreva VOLTAR, CANCELAR, MENU ou CARRINHO",
                       "de": "Schreiben Sie VOLTAR, CANCELAR, MENU oder CARRINHO",
                       "en": "Type VOLTAR, CANCELAR, MENU or CARRINHO"},
    # Rodapé do fluxo Wrap: acrescenta RAPIDO, o comando que muda para o
    # orçamento rápido a qualquer momento. Mantido dentro dos 60 caracteres
    # que a API do WhatsApp aceita num footer (ver enviar_lista/enviar_botoes).
    "rodape_wrap": {"pt": "Escreva VOLTAR, CANCELAR, MENU, CARRINHO ou RAPIDO",
                     "de": "Schreiben Sie VOLTAR, CANCELAR, CARRINHO oder RAPIDO",
                     "en": "Type VOLTAR, CANCELAR, MENU, CARRINHO or RAPIDO"},
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
    "confirmado_instrucao": {"pt": "Por favor, retire os seus objetos pessoais do veículo antes da entrega.",
                              "de": "Bitte entfernen Sie Ihre persönlichen Gegenstände vor der Abgabe aus dem Fahrzeug.",
                              "en": "Please remove your personal belongings from the vehicle before drop-off."},
    # (o antigo "confirmado_rodape", que mandava escrever MENU/GERIR, foi
    # removido — essas ações são agora os botões enviados logo a seguir à
    # confirmação: "🗓️ Gerir marcação" e "🏠 Menu principal".)

    # --- Wrap & Proteção: escolha do modo (entrada do fluxo) -----------------
    "wrap_modo_corpo": {"pt": "🎨 *Wrap & Proteção*\n\nComo prefere avançar?",
                         "de": "🎨 *Folierung & Schutz*\n\nWie möchten Sie fortfahren?",
                         "en": "🎨 *Wrap & Protection*\n\nHow would you like to proceed?"},
    "wrap_modo_rapido_botao": {"pt": "⚡ Orçamento rápido", "de": "⚡ Schnellangebot", "en": "⚡ Quick quote"},
    "wrap_modo_detalhe_botao": {"pt": "🎨 Configurar tudo", "de": "🎨 Alles einstellen", "en": "🎨 Configure in full"},
    "wrap_modo_especialista_botao": {"pt": "💬 Especialista", "de": "💬 Spezialist", "en": "💬 Specialist"},

    # --- Wrap & Proteção: orçamento rápido -----------------------------------
    "rapido_interesse_corpo": {"pt": "⚡ *Orçamento rápido* (1 de 2)\n\nO que está a considerar?",
                                "de": "⚡ *Schnellangebot* (1 von 2)\n\nWoran denken Sie?",
                                "en": "⚡ *Quick quote* (1 of 2)\n\nWhat are you considering?"},
    "rapido_nao_sei_botao": {"pt": "Ainda não sei", "de": "Weiss noch nicht", "en": "Not sure yet"},
    "rapido_fotos_corpo": {"pt": "⚡ *Orçamento rápido* (2 de 2)\n\nDeseja enviar fotografias do veículo "
                                  "(até 5)? Ajuda a equipa a preparar um orçamento mais rigoroso.",
                            "de": "⚡ *Schnellangebot* (2 von 2)\n\nMöchten Sie Fotos des Fahrzeugs "
                                  "(bis zu 5) senden? Das hilft dem Team, ein genaueres Angebot zu erstellen.",
                            "en": "⚡ *Quick quote* (2 of 2)\n\nWould you like to send photos of the vehicle "
                                  "(up to 5)? It helps our team prepare a more accurate quote."},
    "rapido_ver_pedido_botao": {"pt": "🛒 Ver pedido", "de": "🛒 Anfrage ansehen", "en": "🛒 View request"},

    "rapido_resumo_titulo": {"pt": "⚡ *Resumo do pedido rápido*", "de": "⚡ *Zusammenfassung der Schnellanfrage*",
                              "en": "⚡ *Quick request summary*"},
    "rapido_resumo_nome": {"pt": "👤 Nome: {nome}", "de": "👤 Name: {nome}", "en": "👤 Name: {nome}"},
    "rapido_resumo_contacto": {"pt": "📱 Contacto: {contacto}", "de": "📱 Kontakt: {contacto}",
                                "en": "📱 Contact: {contacto}"},
    "rapido_resumo_interesse": {"pt": "🎨 Interesse: {interesse}", "de": "🎨 Interesse: {interesse}",
                                 "en": "🎨 Interest: {interesse}"},
    "rapido_preco_sob_analise": {"pt": "💰 Preço: sob análise da equipa",
                                  "de": "💰 Preis: wird vom Team geprüft",
                                  "en": "💰 Price: under review by our team"},
    "rapido_finalizado_cliente": {"pt": "✅ Pedido rápido enviado! A nossa equipa vai analisar "
                                        "(e as fotografias, se enviadas) e responde-lhe em breve com "
                                        "o orçamento.",
                                   "de": "✅ Schnellanfrage gesendet! Unser Team prüft sie (und die Fotos, "
                                        "falls gesendet) und meldet sich in Kürze mit dem Angebot.",
                                   "en": "✅ Quick request sent! Our team will review it (and the photos, "
                                        "if sent) and will get back to you shortly with the quote."},

    # --- Wrap & Proteção: carrinho no modo rápido ----------------------------
    "carrinho_rapido_titulo": {"pt": "🛒 *Pedido rápido de Wrap*", "de": "🛒 *Schnellanfrage Folierung*",
                                "en": "🛒 *Quick wrap request*"},
    "carrinho_rapido_preferencia": {"pt": "Preferência: {preferencia}", "de": "Präferenz: {preferencia}",
                                     "en": "Preference: {preferencia}"},
    "carrinho_rapido_preco": {"pt": "Preço: sob análise", "de": "Preis: wird geprüft",
                               "en": "Price: under review"},

    # --- Wrap & Proteção: falar com especialista -----------------------------
    "especialista_cliente": {"pt": "💬 Pedido recebido! Um especialista de wrap vai entrar em contacto "
                                    "consigo por aqui em breve, sem compromisso.",
                              "de": "💬 Anfrage erhalten! Ein Folierungs-Spezialist meldet sich in Kürze "
                                    "unverbindlich hier bei Ihnen.",
                              "en": "💬 Request received! A wrap specialist will get in touch with you here "
                                    "shortly, with no obligation."},

    # --- Notificação interna sobre um pedido: reação do cliente (recusa) ----
    "botao_menu_principal": {"pt": "🏠 Menu principal", "de": "🏠 Hauptmenü", "en": "🏠 Main menu"},
    "rapido_recusado_cliente": {
        "pt": "Lamentamos, mas não vamos avançar com este pedido de Wrap & Proteção "
              "neste momento. Obrigado pelo seu interesse!",
        "de": "Es tut uns leid, aber wir werden diese Anfrage für Folierung & Schutz "
              "derzeit nicht weiterverfolgen. Danke für Ihr Interesse!",
        "en": "We're sorry, but we won't be proceeding with this Wrap & Protection "
              "request at this time. Thank you for your interest!"},

    "rapido_linha_lista": {"pt": "⚡ Pedido rápido", "de": "⚡ Schnellanfrage", "en": "⚡ Quick request"},
    "rapido_mudou_modo": {"pt": "⚡ Sem problema — vamos pelo caminho rápido.",
                           "de": "⚡ Kein Problem — nehmen wir den schnellen Weg.",
                           "en": "⚡ No problem — let's take the quick route."},

    # --- Wrap & Proteção -----------------------------------------------------
    "wrap_veiculo_corpo": {"pt": "Passo 1 de 8 — Que tipo de veículo é?",
                            "de": "Schritt 1 von 8 — Um welchen Fahrzeugtyp handelt es sich?",
                            "en": "Step 1 of 8 — What type of vehicle is it?"},
    "wrap_veiculo_seccao": {"pt": "Tipo de veículo", "de": "Fahrzeugtyp", "en": "Vehicle type"},
    "wrap_veiculo_botao": {"pt": "🚗 Escolher", "de": "🚗 Wählen", "en": "🚗 Choose"},
    "wrap_veiculo_outro_pedir": {"pt": "Indique o tipo de veículo (ex: \"Pick-up\").",
                                  "de": "Geben Sie den Fahrzeugtyp an (z.B. \"Pick-up\").",
                                  "en": "Please specify the vehicle type (e.g. \"Pick-up\")."},

    "wrap_ano_corpo": {"pt": "Passo 2 de 8 — Qual o ano do veículo?",
                       "de": "Schritt 2 von 8 — Welches Baujahr hat das Fahrzeug?",
                       "en": "Step 2 of 8 — What year is the vehicle?"},
    "wrap_ano_seccao": {"pt": "Ano do veículo", "de": "Baujahr", "en": "Vehicle year"},
    "wrap_ano_botao": {"pt": "📅 Escolher ano", "de": "📅 Jahr wählen", "en": "📅 Choose year"},
    "wrap_ano_outro_botao": {"pt": "Outro/mais antigo", "de": "Anderes/älter", "en": "Other/older"},
    "wrap_ano_outro_pedir": {"pt": "Indique o ano do veículo, com 4 algarismos (ex: 1998).",
                              "de": "Geben Sie das Baujahr des Fahrzeugs mit 4 Ziffern an (z.B. 1998).",
                              "en": "Please provide the vehicle's year, with 4 digits (e.g. 1998)."},
    "wrap_ano_invalido": {"pt": "Isso não parece um ano válido. Escreva um ano com 4 algarismos (ex: 1998).",
                           "de": "Das scheint kein gültiges Baujahr zu sein. Geben Sie ein Jahr mit 4 Ziffern an (z.B. 1998).",
                           "en": "That doesn't look like a valid year. Please write a 4-digit year (e.g. 1998)."},

    "wrap_tipo_corpo": {"pt": "Passo 3 de 8 — Pretende wrap total ou parcial?",
                        "de": "Schritt 3 von 8 — Möchten Sie eine Voll- oder Teilfolierung?",
                        "en": "Step 3 of 8 — Would you like a full or partial wrap?"},
    "wrap_tipo_seccao": {"pt": "Tipo de wrap", "de": "Folierungsart", "en": "Wrap type"},
    "wrap_tipo_botao": {"pt": "🎨 Escolher", "de": "🎨 Wählen", "en": "🎨 Choose"},
    "wrap_total_botao": {"pt": "🚗 Wrap total", "de": "🚗 Vollfolierung", "en": "🚗 Full wrap"},
    "wrap_parcial_botao": {"pt": "🔧 Wrap parcial", "de": "🔧 Teilfolierung", "en": "🔧 Partial wrap"},

    "wrap_cor_familia_corpo": {"pt": "Passo 4 de 8 — Que família de cor prefere?",
                                "de": "Schritt 4 von 8 — Welche Farbfamilie bevorzugen Sie?",
                                "en": "Step 4 of 8 — Which colour family do you prefer?"},
    "wrap_cor_familia_seccao": {"pt": "Família de cor", "de": "Farbfamilie", "en": "Colour family"},
    "wrap_cor_familia_botao": {"pt": "🎨 Escolher", "de": "🎨 Wählen", "en": "🎨 Choose"},

    "wrap_cor_corpo": {"pt": "Passo 5 de 8 — Escolha a cor:",
                       "de": "Schritt 5 von 8 — Wählen Sie die Farbe:",
                       "en": "Step 5 of 8 — Choose the colour:"},
    "wrap_cor_seccao": {"pt": "Cor", "de": "Farbe", "en": "Colour"},
    "wrap_cor_botao": {"pt": "🎨 Escolher", "de": "🎨 Wählen", "en": "🎨 Choose"},
    "wrap_cor_personalizada_pedir": {"pt": "Descreva a cor que pretende. Ex: \"Azul petróleo com reflexos dourados\".",
                                      "de": "Beschreiben Sie die gewünschte Farbe. Z.B. \"Petrolblau mit goldenen Reflexen\".",
                                      "en": "Describe the colour you'd like. E.g. \"Petrol blue with golden highlights\"."},

    "wrap_acabamento_corpo": {"pt": "Passo 6 de 8 — Que acabamento prefere?",
                               "de": "Schritt 6 von 8 — Welches Finish bevorzugen Sie?",
                               "en": "Step 6 of 8 — Which finish do you prefer?"},
    "wrap_acabamento_seccao": {"pt": "Acabamento", "de": "Finish", "en": "Finish"},
    "wrap_acabamento_botao": {"pt": "✨ Escolher", "de": "✨ Wählen", "en": "✨ Choose"},

    "wrap_fotos_pergunta_corpo": {"pt": "Passo 7 de 8 — Deseja enviar fotografias do veículo (até 5) para "
                                        "ajudar a equipa a preparar o orçamento?",
                                   "de": "Schritt 7 von 8 — Möchten Sie Fotos des Fahrzeugs (bis zu 5) senden, "
                                        "damit unser Team den Kostenvoranschlag vorbereiten kann?",
                                   "en": "Step 7 of 8 — Would you like to send photos of the vehicle (up to 5) "
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
    "wrap_fotos_limite_atingido": {"pt": "✅ Já recebemos o máximo de 5 fotografias. Vamos agora rever o seu pedido.",
                                    "de": "✅ Wir haben bereits die maximal 5 Fotos erhalten. Sehen wir uns nun Ihre Anfrage an.",
                                    "en": "✅ We've already received the maximum of 5 photos. Let's now review your request."},
    "wrap_finalizado_cliente": {"pt": "✅ Pedido de orçamento enviado! A nossa equipa vai analisar os detalhes "
                                      "(e as fotografias, se enviadas) e responde-lhe em breve com o orçamento e "
                                      "disponibilidade para *{veiculo}*.",
                                 "de": "✅ Kostenvoranschlag-Anfrage gesendet! Unser Team prüft die Details "
                                      "(und die Fotos, falls gesendet) und meldet sich in Kürze mit dem Angebot und "
                                      "der Verfügbarkeit für *{veiculo}*.",
                                 "en": "✅ Quote request sent! Our team will review the details (and the photos, "
                                      "if sent) and will get back to you shortly with the quote and availability "
                                      "for *{veiculo}*."},
    "wrap_veiculo_generico": {"pt": "o seu veículo", "de": "Ihr Fahrzeug", "en": "your vehicle"},
    "wrap_total_estimado": {"pt": "💰 Total estimado: {total}\n(o valor final pode variar após a análise das fotografias)",
                             "de": "💰 Geschätzter Gesamtbetrag: {total}\n(der endgültige Betrag kann nach der Analyse der Fotos abweichen)",
                             "en": "💰 Estimated total: {total}\n(the final amount may vary after we review the photos)"},

    "wrap_resumo_titulo": {"pt": "📋 *Resumo do pedido — Wrap & Proteção*",
                            "de": "📋 *Zusammenfassung — Folierung & Schutz*",
                            "en": "📋 *Request summary — Wrap & Protection*"},
    "wrap_resumo_veiculo": {"pt": "🚗 Tipo de veículo: {veiculo}", "de": "🚗 Fahrzeugtyp: {veiculo}",
                             "en": "🚗 Vehicle type: {veiculo}"},
    "wrap_resumo_ano": {"pt": "📅 Ano: {ano}", "de": "📅 Baujahr: {ano}", "en": "📅 Year: {ano}"},
    "wrap_resumo_tipo": {"pt": "🎨 Wrap: {tipo}", "de": "🎨 Folierung: {tipo}", "en": "🎨 Wrap: {tipo}"},
    "wrap_resumo_cor": {"pt": "🖌️ Cor: {cor}", "de": "🖌️ Farbe: {cor}", "en": "🖌️ Colour: {cor}"},
    "wrap_resumo_acabamento": {"pt": "✨ Acabamento: {acabamento}", "de": "✨ Finish: {acabamento}",
                                "en": "✨ Finish: {acabamento}"},
    "wrap_resumo_fotos": {"pt": "📸 Fotografias: {n}", "de": "📸 Fotos: {n}", "en": "📸 Photos: {n}"},

    # --- Orçamento genérico ---------------------------------------------------
    "orcamento_pedido": {"pt": "💰 Sem problema! Descreva em poucas palavras o serviço que pretende e o veículo "
                                "(ex: \"Polimento completo, Audi A4 2019\"). A nossa equipa responde com um orçamento em breve.",
                          "de": "💰 Kein Problem! Beschreiben Sie kurz den gewünschten Service und das Fahrzeug "
                                "(z.B. \"Komplettpolitur, Audi A4 2019\"). Unser Team antwortet Ihnen in Kürze mit einem Kostenvoranschlag.",
                          "en": "💰 No problem! Briefly describe the service you'd like and the vehicle "
                                "(e.g. \"Full polish, Audi A4 2019\"). Our team will reply with a quote shortly."},
    "orcamento_recebido_cliente": {"pt": "✅ Recebido! A equipa vai analisar e responde-lhe em breve.",
                                    "de": "✅ Erhalten! Das Team prüft die Anfrage und meldet sich in Kürze.",
                                    "en": "✅ Received! Our team will review it and get back to you shortly."},

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
    "reagendar_aviso": {"pt": "Sem problema, vamos criar uma nova marcação. A anterior foi arquivada.",
                         "de": "Kein Problem, wir erstellen eine neue Buchung. Die vorherige wurde archiviert.",
                         "en": "No problem, let's create a new booking. The previous one has been archived."},
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
    "ajuda_carrinho": {"pt": "• CARRINHO / CART / WARENKORB — ver o carrinho atual",
                        "de": "• CARRINHO / CART / WARENKORB — aktuellen Warenkorb ansehen",
                        "en": "• CARRINHO / CART / WARENKORB — view your current cart"},
    "ajuda_rapido": {"pt": "• RAPIDO / QUICK / SCHNELL — mudar para o orçamento rápido de wrap",
                      "de": "• RAPIDO / QUICK / SCHNELL — zum Schnellangebot für Folierung wechseln",
                      "en": "• RAPIDO / QUICK / SCHNELL — switch to the quick wrap quote"},

    # --- Carrinho -----------------------------------------------------------
    "carrinho_titulo": {"pt": "🛒 *O seu carrinho*", "de": "🛒 *Ihr Warenkorb*", "en": "🛒 *Your cart*"},
    "carrinho_vazio": {"pt": "🛒 O seu carrinho está vazio.",
                        "de": "🛒 Ihr Warenkorb ist leer.",
                        "en": "🛒 Your cart is empty."},
    "carrinho_subtotal": {"pt": "Subtotal: {subtotal}", "de": "Zwischensumme: {subtotal}", "en": "Subtotal: {subtotal}"},
    "carrinho_total": {"pt": "💰 Total: {total}", "de": "💰 Gesamtbetrag: {total}", "en": "💰 Total: {total}"},
    "carrinho_total_estimado": {"pt": "💰 Total estimado: {total}", "de": "💰 Geschätzter Gesamtbetrag: {total}",
                                 "en": "💰 Estimated total: {total}"},
    "carrinho_botao_alterar": {"pt": "✏️ Alterar item", "de": "✏️ Artikel ändern", "en": "✏️ Change item"},
    "carrinho_botao_esvaziar": {"pt": "🗑️ Esvaziar carrinho", "de": "🗑️ Warenkorb leeren", "en": "🗑️ Empty cart"},
    "carrinho_alterar_pergunta": {"pt": "Qual item deseja alterar ou remover?",
                                   "de": "Welchen Artikel möchten Sie ändern oder entfernen?",
                                   "en": "Which item would you like to change or remove?"},
    "carrinho_item_substituir": {"pt": "🔁 Substituir", "de": "🔁 Ersetzen", "en": "🔁 Replace"},
    "carrinho_item_remover": {"pt": "🗑️ Remover", "de": "🗑️ Entfernen", "en": "🗑️ Remove"},
    "carrinho_item_removido": {"pt": "✅ Item removido do carrinho.", "de": "✅ Artikel aus dem Warenkorb entfernt.",
                                "en": "✅ Item removed from cart."},
    "carrinho_esvaziado": {"pt": "🗑️ Carrinho esvaziado. Vamos recomeçar.",
                            "de": "🗑️ Warenkorb geleert. Fangen wir neu an.",
                            "en": "🗑️ Cart emptied. Let's start again."},
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

    # --- Orçamento enviado pelo painel ao cliente ----------------------------
    "orcamento_cliente_titulo": {"pt": "💰 *Orçamento — Pedido #{pedido}*", "de": "💰 *Angebot — Anfrage #{pedido}*",
                                  "en": "💰 *Quote — Request #{pedido}*"},
    "orcamento_cliente_subtotal": {"pt": "Subtotal: {subtotal}", "de": "Zwischensumme: {subtotal}",
                                    "en": "Subtotal: {subtotal}"},
    "orcamento_cliente_desconto": {"pt": "Desconto: -{desconto}", "de": "Rabatt: -{desconto}",
                                    "en": "Discount: -{desconto}"},
    "orcamento_cliente_total": {"pt": "💰 Total: {total}", "de": "💰 Gesamtbetrag: {total}", "en": "💰 Total: {total}"},
    "orcamento_cliente_observacoes": {"pt": "📝 Observações: {observacoes}", "de": "📝 Anmerkungen: {observacoes}",
                                       "en": "📝 Notes: {observacoes}"},
    "orcamento_cliente_validade": {"pt": "⏳ Válido por {dias} dias", "de": "⏳ Gültig für {dias} Tage",
                                    "en": "⏳ Valid for {dias} days"},
    "botao_orcamento_aceitar": {"pt": "✅ Aceitar orçamento", "de": "✅ Angebot annehmen", "en": "✅ Accept quote"},
    "botao_orcamento_alterar": {"pt": "✏️ Pedir alteração", "de": "✏️ Änderung anfragen", "en": "✏️ Request change"},
    "botao_orcamento_recusar": {"pt": "❌ Recusar", "de": "❌ Ablehnen", "en": "❌ Decline"},
    "orcamento_ja_respondido": {"pt": "Este orçamento já foi respondido anteriormente.",
                                 "de": "Dieses Angebot wurde bereits beantwortet.",
                                 "en": "This quote has already been responded to."},
    "orcamento_aceite_cliente": {"pt": "✅ Ótimo! O seu orçamento foi aceite. A nossa equipa entra em contacto "
                                       "para combinar os detalhes.",
                                  "de": "✅ Grossartig! Ihr Angebot wurde angenommen. Unser Team meldet sich, "
                                       "um die Details zu vereinbaren.",
                                  "en": "✅ Great! Your quote has been accepted. Our team will get in touch "
                                       "to arrange the details."},
    "botao_avancar_agendamento": {"pt": "📅 Marcar agendamento", "de": "📅 Termin buchen", "en": "📅 Book appointment"},
    "orcamento_recusar_confirmar_pergunta": {"pt": "Tem a certeza de que quer recusar este orçamento?",
                                              "de": "Sind Sie sicher, dass Sie dieses Angebot ablehnen möchten?",
                                              "en": "Are you sure you want to decline this quote?"},
    "botao_sim_recusar": {"pt": "❌ Sim, recusar", "de": "❌ Ja, ablehnen", "en": "❌ Yes, decline"},
    "botao_nao_voltar": {"pt": "↩️ Não, voltar", "de": "↩️ Nein, zurück", "en": "↩️ No, go back"},
    "orcamento_recusado_cliente": {"pt": "Sem problema. Obrigado pelo seu tempo — ficamos à disposição "
                                         "para um novo pedido quando quiser.",
                                    "de": "Kein Problem. Danke für Ihre Zeit — wir stehen für eine neue "
                                         "Anfrage jederzeit zur Verfügung.",
                                    "en": "No problem. Thank you for your time — we're happy to help "
                                         "with a new request whenever you'd like."},
    "botao_novo_pedido": {"pt": "📅 Novo pedido", "de": "📅 Neue Anfrage", "en": "📅 New request"},

    # --- Pedir alteração ao orçamento ----------------------------------------
    "alteracao_pergunta": {"pt": "O que gostaria de alterar?", "de": "Was möchten Sie ändern?",
                            "en": "What would you like to change?"},
    "alteracao_seccao": {"pt": "Alterações possíveis", "de": "Mögliche Änderungen", "en": "Possible changes"},
    "alteracao_botao": {"pt": "✏️ Escolher", "de": "✏️ Wählen", "en": "✏️ Choose"},
    "alteracao_opcao_servico": {"pt": "Serviço/tipo de wrap", "de": "Service/Folierungsart",
                                 "en": "Service/wrap type"},
    "alteracao_opcao_veiculo": {"pt": "Veículo", "de": "Fahrzeug", "en": "Vehicle"},
    "alteracao_opcao_cor": {"pt": "Cor/acabamento", "de": "Farbe/Finish", "en": "Colour/finish"},
    "alteracao_opcao_prazo": {"pt": "Prazo/data", "de": "Frist/Termin", "en": "Timeline/date"},
    "alteracao_opcao_outra": {"pt": "Outra alteração", "de": "Andere Änderung", "en": "Other change"},
    "alteracao_opcao_equipa": {"pt": "Falar com a equipa", "de": "Mit dem Team sprechen", "en": "Talk to the team"},
    "alteracao_outra_pedir": {"pt": "Descreva a alteração que pretende. Ex.: \"Gostaria de um prazo mais curto\".",
                               "de": "Beschreiben Sie die gewünschte Änderung. Z.B. \"Ich hätte gerne einen "
                                    "kürzeren Termin\".",
                               "en": "Describe the change you'd like. E.g. \"I'd like a shorter timeline\"."},
    "alteracao_recebida_cliente": {"pt": "✅ Pedido de alteração recebido! A equipa vai rever e envia um novo "
                                         "orçamento em breve.",
                                    "de": "✅ Änderungsanfrage erhalten! Das Team prüft sie und sendet in "
                                         "Kürze ein neues Angebot.",
                                    "en": "✅ Change request received! The team will review it and send a "
                                         "new quote shortly."},

    # --- Notificação interna: ações sobre um novo pedido ---------------------
    "botao_pedido_analisar": {"pt": "🔎 Analisar pedido", "de": "🔎 Anfrage prüfen", "en": "🔎 Review request"},
    "botao_pedido_contactar": {"pt": "💬 Contactar cliente", "de": "💬 Kunde kontaktieren", "en": "💬 Contact client"},
    "botao_pedido_recusar": {"pt": "❌ Recusar pedido", "de": "❌ Anfrage ablehnen", "en": "❌ Decline request"},
    "pedido_em_analise_cliente": {"pt": "✅ O seu pedido foi aceite e está agora em análise pela nossa equipa. "
                                        "Vai receber o orçamento em breve.",
                                   "de": "✅ Ihre Anfrage wurde angenommen und wird nun von unserem Team geprüft. "
                                        "Sie erhalten in Kürze das Angebot.",
                                   "en": "✅ Your request has been accepted and is now under review by our team. "
                                        "You'll receive the quote shortly."},

    # --- Carrinho: pedido pendente persistente -------------------------------
    "carrinho_botao_ver_pendente": {"pt": "🛒 Carrinho · {n} pendente", "de": "🛒 Warenkorb · {n} offen",
                                     "en": "🛒 Cart · {n} pending"},
    "carrinho_pendente_titulo": {"pt": "🛒 *Pedido pendente*", "de": "🛒 *Ausstehende Anfrage*",
                                  "en": "🛒 *Pending request*"},
    "carrinho_pendente_id": {"pt": "🆔 Pedido #{id}", "de": "🆔 Anfrage #{id}", "en": "🆔 Request #{id}"},
    "carrinho_pendente_estado": {"pt": "📌 Estado: {estado}", "de": "📌 Status: {estado}", "en": "📌 Status: {estado}"},
    "carrinho_pendente_preco_sob_analise": {"pt": "💰 Preço: sob análise da equipa",
                                             "de": "💰 Preis: wird vom Team geprüft",
                                             "en": "💰 Price: under review by our team"},
    "botao_ver_pedido_orcamento": {"pt": "🛒 Ver pedido/orçamento", "de": "🛒 Anfrage/Angebot ansehen",
                                    "en": "🛒 View request/quote"},
    "botao_cancelar_pedido_cliente": {"pt": "❌ Cancelar pedido", "de": "❌ Anfrage stornieren",
                                       "en": "❌ Cancel request"},
    "cancelar_pedido_confirmar_pergunta": {"pt": "Tem a certeza de que quer cancelar este pedido?",
                                            "de": "Sind Sie sicher, dass Sie diese Anfrage stornieren möchten?",
                                            "en": "Are you sure you want to cancel this request?"},
    "botao_sim_cancelar": {"pt": "❌ Sim, cancelar", "de": "❌ Ja, stornieren", "en": "❌ Yes, cancel"},
    "pedido_cancelado_cliente": {"pt": "✅ O seu pedido foi cancelado.", "de": "✅ Ihre Anfrage wurde storniert.",
                                  "en": "✅ Your request has been cancelled."},
    "pedido_ja_respondido_cliente": {"pt": "Este pedido já não está ativo.",
                                      "de": "Diese Anfrage ist nicht mehr aktiv.",
                                      "en": "This request is no longer active."},

    # --- Carrinho: marcações confirmadas persistentes ------------------------
    "carrinho_botao_ver_marcacoes": {"pt": "🛒 Carrinho · {n} marcações",
                                      "de": "🛒 Warenkorb · {n} Buchungen",
                                      "en": "🛒 Cart · {n} bookings"},
    "carrinho_marcacao_titulo": {"pt": "🗓️ *Marcação confirmada*", "de": "🗓️ *Bestätigte Buchung*",
                                  "en": "🗓️ *Confirmed booking*"},
    "carrinho_marcacao_id": {"pt": "🆔 Marcação #{id}", "de": "🆔 Buchung #{id}", "en": "🆔 Booking #{id}"},
    "carrinho_marcacao_estado": {"pt": "📌 Estado: Confirmada", "de": "📌 Status: Bestätigt",
                                  "en": "📌 Status: Confirmed"},
    "carrinho_marcacao_servico": {"pt": "🔧 Serviço: {servico}", "de": "🔧 Service: {servico}",
                                   "en": "🔧 Service: {servico}"},
    "carrinho_marcacao_extra": {"pt": "➕ Extras: {extra}", "de": "➕ Extras: {extra}", "en": "➕ Extras: {extra}"},
    "carrinho_marcacao_data": {"pt": "📅 Data: {data}", "de": "📅 Datum: {data}", "en": "📅 Date: {data}"},
    "carrinho_marcacao_hora": {"pt": "🕘 Hora: {hora}", "de": "🕘 Uhrzeit: {hora}", "en": "🕘 Time: {hora}"},
    "carrinho_marcacao_duracao": {"pt": "⏱️ Duração: {duracao}", "de": "⏱️ Dauer: {duracao}",
                                   "en": "⏱️ Duration: {duracao}"},
    "carrinho_marcacao_total": {"pt": "💰 Total: {total}", "de": "💰 Gesamtbetrag: {total}",
                                 "en": "💰 Total: {total}"},
    "carrinho_marcacoes_seccao": {"pt": "Marcações confirmadas", "de": "Bestätigte Buchungen",
                                   "en": "Confirmed bookings"},
    "carrinho_marcacoes_pergunta": {"pt": "🛒 *O seu carrinho*\n\nTem {n} marcações confirmadas. "
                                          "Qual deseja ver?",
                                     "de": "🛒 *Ihr Warenkorb*\n\nSie haben {n} bestätigte Buchungen. "
                                          "Welche möchten Sie ansehen?",
                                     "en": "🛒 *Your cart*\n\nYou have {n} confirmed bookings. "
                                          "Which one would you like to view?"},
    "carrinho_marcacoes_extra_linha": {"pt": "🗓️ Também tem {n} marcação(ões) confirmada(s).",
                                        "de": "🗓️ Sie haben ausserdem {n} bestätigte Buchung(en).",
                                        "en": "🗓️ You also have {n} confirmed booking(s)."},
    "botao_ver_gerir_marcacao": {"pt": "🗓️ Ver/Gerir marcação", "de": "🗓️ Buchung ansehen",
                                  "en": "🗓️ View/Manage booking"},
    "carrinho_marcacao_nao_encontrada": {"pt": "Não encontrei essa marcação confirmada.",
                                          "de": "Diese bestätigte Buchung wurde nicht gefunden.",
                                          "en": "I couldn't find that confirmed booking."},

    # --- Marcação cancelada pela equipa: aviso ao cliente --------------------
    "marcacao_cancelada_equipa_cliente": {
        "pt": "❌ A sua marcação #{id} foi cancelada. Lamentamos o incómodo — estamos à disposição "
              "para marcar uma nova data quando quiser.",
        "de": "❌ Ihre Buchung #{id} wurde storniert. Wir bedauern die Unannehmlichkeiten — gerne "
              "vereinbaren wir jederzeit einen neuen Termin.",
        "en": "❌ Your booking #{id} has been cancelled. We're sorry for the inconvenience — we're "
              "happy to arrange a new date whenever you'd like."},
}

# Nomes traduzidos dos estados de um pedido de orçamento, para apresentação
# ao CLIENTE no carrinho persistente (a base de dados guarda sempre o valor
# canónico em português — ver ESTADOS_PEDIDO).
ESTADO_PEDIDO_NOMES = {
    "novo": {"pt": "recebido", "de": "erhalten", "en": "received"},
    "em análise": {"pt": "em análise", "de": "wird geprüft", "en": "under review"},
    "orçamento enviado": {"pt": "orçamento enviado", "de": "Angebot gesendet", "en": "quote sent"},
    "alteração solicitada": {"pt": "alteração solicitada", "de": "Änderung angefragt", "en": "change requested"},
    "aceite": {"pt": "aceite", "de": "angenommen", "en": "accepted"},
    "contacto solicitado": {"pt": "contacto solicitado", "de": "Kontakt angefragt", "en": "contact requested"},
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

# ---------------------------------------------------------------------------
# Tabela central de preços de DEMONSTRAÇÃO para Wrap & Proteção — claramente
# separada da lógica do fluxo, fácil de alterar sem tocar em mais nada.
# Valores em CÊNTIMOS (CHF) para evitar erros de arredondamento. O preço
# final real depende sempre da análise das fotografias pela equipa, por
# isso este fluxo mostra sempre "Total estimado" ao cliente, nunca "Total".
# ---------------------------------------------------------------------------
WRAP_PRECOS_CENTIMOS = {
    "wrap_total": 180000,    # CHF 1800.00 (demonstração)
    "wrap_parcial": 90000,   # CHF 900.00 (demonstração)
}
WRAP_NOMES = {
    "wrap_total": {"pt": "Wrap total", "de": "Vollfolierung", "en": "Full wrap"},
    "wrap_parcial": {"pt": "Wrap parcial", "de": "Teilfolierung", "en": "Partial wrap"},
}

# Interesse declarado no ORÇAMENTO RÁPIDO. Propositadamente separado de
# WRAP_PRECOS_CENTIMOS: no modo rápido nunca se calcula nem se mostra um
# preço — o valor fica sempre "sob análise da equipa".
WRAP_RAPIDO_INTERESSES = {
    "wrap_total": {"pt": "Wrap total", "de": "Vollfolierung", "en": "Full wrap"},
    "wrap_parcial": {"pt": "Wrap parcial", "de": "Teilfolierung", "en": "Partial wrap"},
    "wrap_nao_sei": {"pt": "Ainda não sei", "de": "Weiss noch nicht", "en": "Not sure yet"},
}

# Valores NEUTROS gravados na base de dados para campos que o cliente ainda
# não escolheu (modo rápido / contacto com especialista). Nunca se assume
# uma escolha que o cliente não fez — em particular, "Ainda não sei" nunca
# é convertido em "Wrap parcial".
WRAP_NEUTRO_VEICULO = "Por indicar"
WRAP_NEUTRO_ANO = ""
WRAP_NEUTRO_COR_ACABAMENTO = "Aconselhamento necessário"
WRAP_NEUTRO_TIPO = "Por indicar"

# Tradução do valor canónico (em português) guardado em "tipo_wrap" — usado
# na apresentação ao cliente do pedido pendente no carrinho persistente (ver
# mostrar_pedido_pendente_carrinho). Cobre "Wrap total"/"Wrap parcial" (modo
# detalhado), "Ainda não sei" (modo rápido) e o valor neutro.
TIPO_WRAP_TEXTO_TRADUZIDO = {
    "Wrap total": {"pt": "Wrap total", "de": "Vollfolierung", "en": "Full wrap"},
    "Wrap parcial": {"pt": "Wrap parcial", "de": "Teilfolierung", "en": "Partial wrap"},
    "Ainda não sei": {"pt": "Ainda não sei", "de": "Weiss noch nicht", "en": "Not sure yet"},
    WRAP_NEUTRO_TIPO: {"pt": WRAP_NEUTRO_TIPO, "de": "Wird noch angegeben", "en": "To be specified"},
}


def texto_tipo_wrap_traduzido(tipo_pt, idioma):
    dic = TIPO_WRAP_TEXTO_TRADUZIDO.get(tipo_pt)
    return tx(dic, idioma) if dic else (tipo_pt or "-")


# Nome traduzido do MODO de um pedido, para apresentação ao cliente (o painel
# usa MODO_NOMES_PT, sempre em português — ver dashboard).
MODO_NOMES_TRADUZIDO = {
    MODO_RAPIDO: {"pt": "Pedido rápido", "de": "Schnellanfrage", "en": "Quick request"},
    MODO_DETALHE: {"pt": "Pedido de orçamento", "de": "Kostenvoranschlag-Anfrage", "en": "Quote request"},
    MODO_ESPECIALISTA: {"pt": "Contacto com especialista", "de": "Kontakt mit Spezialist", "en": "Specialist contact"},
}
MODO_EMOJI = {MODO_RAPIDO: "⚡", MODO_DETALHE: "🎨", MODO_ESPECIALISTA: "💬"}


def _remover_emoji_prefixo(texto):
    """Remove um possível emoji + espaço no início de um título (ex.: "🏎️
    Supercarro" -> "Supercarro"). Usado só para obter o nome CANÓNICO, sem
    emoji, que fica gravado no carrinho e na base de dados — os emojis são
    puramente decoração visual das listas apresentadas ao cliente."""
    if not texto:
        return texto
    partes = texto.split(" ", 1)
    if len(partes) == 2 and not partes[0][0].isalnum():
        return partes[1]
    return texto


def _titulo_sem_emoji(dic):
    return {lingua: _remover_emoji_prefixo(valor) for lingua, valor in dic.items()}


# --- Passo 1: Tipo de veículo ----------------------------------------------
# Só a opção "Outro" permite escrever o tipo de veículo manualmente (ver
# _wrap_aguardando_veiculo_texto no webhook) — todas as restantes são
# escolhidas exclusivamente por lista.
WRAP_TIPOS_VEICULO = [
    {"id": "wv_supercarro", "titulo": {"pt": "🏎️ Supercarro", "de": "🏎️ Supersportwagen", "en": "🏎️ Supercar"}},
    {"id": "wv_desportivo", "titulo": {"pt": "🏁 Desportivo", "de": "🏁 Sportwagen", "en": "🏁 Sports car"}},
    {"id": "wv_luxo", "titulo": {"pt": "👑 Luxo/Premium", "de": "👑 Luxus/Premium", "en": "👑 Luxury/Premium"}},
    {"id": "wv_classico", "titulo": {"pt": "🕰️ Clássico", "de": "🕰️ Oldtimer", "en": "🕰️ Classic"}},
    {"id": "wv_suv", "titulo": {"pt": "🚙 SUV/4x4", "de": "🚙 SUV/4x4", "en": "🚙 SUV/4x4"}},
    {"id": "wv_berlina", "titulo": {"pt": "🚗 Berlina/Coupé", "de": "🚗 Limousine/Coupé", "en": "🚗 Sedan/Coupe"}},
    {"id": "wv_carrinha", "titulo": {"pt": "🚐 Carrinha/Van", "de": "🚐 Kombi/Van", "en": "🚐 Wagon/Van"}},
    {"id": "wv_outro", "titulo": {"pt": "🔹 Outro", "de": "🔹 Andere", "en": "🔹 Other"}},
]

# --- Passo 4/5: Família de cor + cores -------------------------------------
# "Transparente/PPF" é guardado diretamente como cor (sem lista de cores
# própria); "Criar a minha cor" é a única opção que permite texto livre.
WRAP_FAMILIAS_COR = [
    {"id": "cf_neutras", "titulo": {"pt": "Neutras", "de": "Neutral", "en": "Neutrals"}},
    {"id": "cf_quentes", "titulo": {"pt": "Quentes", "de": "Warme Töne", "en": "Warm tones"}},
    {"id": "cf_frias", "titulo": {"pt": "Frias", "de": "Kühle Töne", "en": "Cool tones"}},
    {"id": "cf_vibrantes", "titulo": {"pt": "Vibrantes", "de": "Kräftige Töne", "en": "Vibrant tones"}},
    {"id": "cf_naturais", "titulo": {"pt": "Naturais", "de": "Natürliche Töne", "en": "Natural tones"}},
    {"id": "cf_dourado_bronze", "titulo": {"pt": "Dourado/Bronze", "de": "Gold/Bronze", "en": "Gold/Bronze"}},
    {"id": "cf_transparente", "titulo": {"pt": "Transparente/PPF", "de": "Transparent/PPF", "en": "Transparent/PPF"}},
    {"id": "cf_personalizada", "titulo": {"pt": "🎨 Criar a minha cor", "de": "🎨 Meine eigene Farbe",
                                           "en": "🎨 Create my own colour"}},
]

WRAP_CORES_POR_FAMILIA = {
    "cf_neutras": [
        {"id": "cor_preto", "titulo": {"pt": "Preto", "de": "Schwarz", "en": "Black"}},
        {"id": "cor_branco", "titulo": {"pt": "Branco", "de": "Weiss", "en": "White"}},
        {"id": "cor_cinzento", "titulo": {"pt": "Cinzento", "de": "Grau", "en": "Grey"}},
        {"id": "cor_prateado", "titulo": {"pt": "Prateado", "de": "Silber", "en": "Silver"}},
    ],
    "cf_quentes": [
        {"id": "cor_vermelho", "titulo": {"pt": "Vermelho", "de": "Rot", "en": "Red"}},
        {"id": "cor_laranja", "titulo": {"pt": "Laranja", "de": "Orange", "en": "Orange"}},
        {"id": "cor_amarelo", "titulo": {"pt": "Amarelo", "de": "Gelb", "en": "Yellow"}},
    ],
    "cf_frias": [
        {"id": "cor_azul", "titulo": {"pt": "Azul", "de": "Blau", "en": "Blue"}},
        {"id": "cor_verde", "titulo": {"pt": "Verde", "de": "Grün", "en": "Green"}},
        {"id": "cor_turquesa", "titulo": {"pt": "Turquesa", "de": "Türkis", "en": "Turquoise"}},
    ],
    "cf_vibrantes": [
        {"id": "cor_roxo", "titulo": {"pt": "Roxo", "de": "Violett", "en": "Purple"}},
        {"id": "cor_rosa", "titulo": {"pt": "Rosa", "de": "Rosa", "en": "Pink"}},
    ],
    "cf_naturais": [
        {"id": "cor_castanho", "titulo": {"pt": "Castanho", "de": "Braun", "en": "Brown"}},
        {"id": "cor_bege", "titulo": {"pt": "Bege", "de": "Beige", "en": "Beige"}},
    ],
    "cf_dourado_bronze": [
        {"id": "cor_dourado", "titulo": {"pt": "Dourado", "de": "Gold", "en": "Gold"}},
        {"id": "cor_bronze", "titulo": {"pt": "Bronze", "de": "Bronze", "en": "Bronze"}},
    ],
}

WRAP_COR_TRANSPARENTE_NOME = {"pt": "Transparente/PPF", "de": "Transparent/PPF", "en": "Transparent/PPF"}

# --- Passo 6: Acabamento ----------------------------------------------------
WRAP_ACABAMENTOS = [
    {"id": "wa_brilhante", "titulo": {"pt": "✨ Brilhante", "de": "✨ Glänzend", "en": "✨ Glossy"}},
    {"id": "wa_mate", "titulo": {"pt": "◼️ Mate", "de": "◼️ Matt", "en": "◼️ Matte"}},
    {"id": "wa_satinado", "titulo": {"pt": "🪶 Satinado", "de": "🪶 Satiniert", "en": "🪶 Satin"}},
    {"id": "wa_metalizado", "titulo": {"pt": "🔩 Metalizado", "de": "🔩 Metallic", "en": "🔩 Metallic"}},
    {"id": "wa_perolado", "titulo": {"pt": "🌈 Perolado", "de": "🌈 Perleffekt", "en": "🌈 Pearlescent"}},
    {"id": "wa_cromado", "titulo": {"pt": "🪞 Cromado", "de": "🪞 Verchromt", "en": "🪞 Chrome"}},
    {"id": "wa_fibra_carbono", "titulo": {"pt": "🧵 Fibra de carbono", "de": "🧵 Carbonfaser", "en": "🧵 Carbon fibre"}},
    {"id": "wa_aconselhamento", "titulo": {"pt": "💬 Preciso de conselho", "de": "💬 Ich brauche Beratung",
                                            "en": "💬 I need advice"}},
]

# ---------------------------------------------------------------------------
# Tabela central de preços de DEMONSTRAÇÃO para os modificadores do Wrap &
# Proteção (tipo de veículo, cor, acabamento) — claramente separada da
# lógica do fluxo e fácil de editar. Valores em CÊNTIMOS (CHF). Opções sem
# acréscimo ficam a 0.
# ---------------------------------------------------------------------------
WRAP_VEICULO_PRECOS_CENTIMOS = {
    "wv_supercarro": 60000,   # CHF 600 (demonstração) — maior superfície/complexidade
    "wv_desportivo": 30000,
    "wv_luxo": 40000,
    "wv_classico": 20000,
    "wv_suv": 20000,
    "wv_berlina": 0,
    "wv_carrinha": 30000,
    "wv_outro": 0,
    "wv_outro_livre": 0,
}
WRAP_ACABAMENTO_PRECOS_CENTIMOS = {
    "wa_brilhante": 0,
    "wa_mate": 0,
    "wa_satinado": 10000,
    "wa_metalizado": 15000,
    "wa_perolado": 25000,
    "wa_cromado": 40000,
    "wa_fibra_carbono": 50000,
    "wa_aconselhamento": 0,
}
# Cores de catálogo ficam sem acréscimo por omissão; só a cor personalizada
# (pintura à medida, fora de catálogo) tem um valor de demonstração.
WRAP_COR_PRECOS_CENTIMOS = {
    "cor_transparente_ppf": 0,
    "cor_personalizada_livre": 15000,  # CHF 150 (demonstração) — cor à medida
}

# Dicionários id -> título multilingue (SEM emoji), usados só para traduzir
# nomes já gravados no carrinho — nunca para desenhar as listas (essas usam
# sempre os catálogos acima, com emoji).
WRAP_VEICULO_NOMES = {opt["id"]: _titulo_sem_emoji(opt["titulo"]) for opt in WRAP_TIPOS_VEICULO}
WRAP_ACABAMENTO_NOMES = {opt["id"]: _titulo_sem_emoji(opt["titulo"]) for opt in WRAP_ACABAMENTOS}
WRAP_CORES_NOMES = {c["id"]: c["titulo"] for familia in WRAP_CORES_POR_FAMILIA.values() for c in familia}
WRAP_CORES_NOMES["cor_transparente_ppf"] = WRAP_COR_TRANSPARENTE_NOME


def wrap_familia_tem_lista_propria(familia_id):
    """As famílias "Transparente/PPF" e "Criar a minha cor" não têm uma
    lista de cores própria — a cor fica logo definida no passo da família
    (diretamente, ou por texto livre)."""
    return familia_id not in ("cf_transparente", "cf_personalizada")


def ano_veiculo_valido(texto):
    """Um ano só é aceite com exatamente 4 algarismos e dentro de um
    intervalo plausível (1900 até ao ano atual)."""
    texto = (texto or "").strip()
    if not re.fullmatch(r"\d{4}", texto):
        return None
    ano = int(texto)
    return texto if 1900 <= ano <= date.today().year else None


def opcoes_wrap_ano(idioma):
    ano_atual = date.today().year
    opcoes = [{"id": f"wrap_ano_{a}", "titulo": str(a)} for a in range(ano_atual, ano_atual - 6, -1)]
    opcoes.append({"id": "wrap_ano_outro", "titulo": t("wrap_ano_outro_botao", idioma)})
    return opcoes

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
ESTADOS_PEDIDO = ("rascunho", "novo", "contacto solicitado", "em análise", "orçamento enviado",
                   "alteração solicitada", "aceite", "recusado", "arquivado")
# "rascunho": pedido criado ainda a meio do fluxo Wrap (antes da confirmação
# final do cliente) — nunca deve aparecer como "novo" no painel antes de o
# cliente ter efetivamente confirmado o pedido.

# Estados considerados "ativos" para efeitos do carrinho persistente (ver
# pedido_ativo_por_telefone/mostrar_carrinho): um pedido "aceite" só continua
# ativo enquanto ainda não tiver sido convertido numa marcação (agendamento_id
# continua NULO — o calendário/agendamento avançado não é implementado nesta
# fase, por isso esta condição está sempre para já satisfeita quando "aceite").
ESTADOS_PEDIDO_ATIVOS = ("novo", "em análise", "orçamento enviado", "alteração solicitada", "aceite")


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
    # Migração leve: guarda o carrinho (JSON) junto da marcação/pedido, para
    # futuramente alimentar o dashboard, orçamento, pagamento e calendário,
    # sem duplicar dados. Em bases de dados já existentes (criadas antes
    # desta funcionalidade), a coluna ainda não existe — adiciona-a agora.
    for tabela in ("agendamentos", "pedidos_orcamento"):
        try:
            conn.execute(f"ALTER TABLE {tabela} ADD COLUMN carrinho_json TEXT")
        except sqlite3.OperationalError:
            pass  # coluna já existe
    # Migração leve: distingue no painel um pedido rápido de uma configuração
    # detalhada ou de um pedido de contacto com especialista. Pedidos antigos
    # ficam com a coluna a NULL e são apresentados como "detalhe" (era o único
    # modo existente antes desta funcionalidade).
    try:
        conn.execute("ALTER TABLE pedidos_orcamento ADD COLUMN modo_pedido TEXT")
    except sqlite3.OperationalError:
        pass  # coluna já existe

    # Orçamentos criados no painel para um pedido, e respetivas linhas
    # (descrição + quantidade + preço). Estrutura própria, associada ao ID do
    # pedido — nunca reaproveita as colunas de pedidos_orcamento. Cada edição
    # de um orçamento já ENVIADO cria uma nova "versao" (nunca reescreve a
    # anterior), para preservar sempre o que foi efetivamente enviado ao
    # cliente (ver obter_ou_criar_rascunho_orcamento). Tabelas novas -> não
    # precisam de ALTER TABLE, só de CREATE TABLE IF NOT EXISTS (compatível
    # com bases de dados antigas, que simplesmente ainda não as têm).
    conn.execute(
        "CREATE TABLE IF NOT EXISTS orcamentos ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "pedido_id INTEGER NOT NULL, "
        "versao INTEGER NOT NULL, "
        "estado TEXT NOT NULL DEFAULT 'rascunho', "
        "desconto_centimos INTEGER NOT NULL DEFAULT 0, "
        "observacoes TEXT, "
        "validade_dias INTEGER, "
        "criado_em TEXT NOT NULL, "
        "atualizado_em TEXT NOT NULL, "
        "enviado_em TEXT, "
        "respondido_em TEXT)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS orcamento_linhas ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "orcamento_id INTEGER NOT NULL, "
        "descricao TEXT NOT NULL, "
        "quantidade INTEGER NOT NULL DEFAULT 1, "
        "preco_centimos INTEGER NOT NULL DEFAULT 0, "
        "criado_em TEXT NOT NULL)"
    )
    # Última mensagem recebida de cada cliente — usada só para saber se ainda
    # estamos dentro da janela de 24h de atendimento ao cliente da Meta (fora
    # dela, mensagens iniciadas pelo negócio como o envio de um orçamento têm
    # de usar um template pré-aprovado; ver dentro_da_janela_24h()). É uma
    # tabela à parte da sessão (nunca dentro do JSON de "sessoes"), para nunca
    # interferir com o formato/conteúdo já testado da sessão.
    conn.execute(
        "CREATE TABLE IF NOT EXISTS interacoes_cliente ("
        "telefone TEXT PRIMARY KEY, "
        "ultima_mensagem_em TEXT NOT NULL)"
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
            "(telefone, nome, categoria, servico, extra, data, hora, preco, duracao, estado, criado_em, carrinho_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'confirmado', ?, ?)",
            (
                telefone, sessao.get("nome"), sessao.get("categoria"),
                sessao.get("servico"), sessao.get("extra"),
                sessao.get("data"), sessao.get("hora"),
                sessao.get("preco"), sessao.get("duracao"),
                datetime.utcnow().isoformat(),
                json.dumps(sessao.get("carrinho", [])),
            ),
        )
        return cur.lastrowid


def listar_agendamentos():
    with obter_bd() as conn:
        linhas = conn.execute(
            "SELECT id, telefone, nome, categoria, servico, extra, data, hora, preco, duracao, estado, "
            "criado_em, carrinho_json FROM agendamentos ORDER BY id DESC"
        ).fetchall()
    campos = ["id", "telefone", "nome", "categoria", "servico", "extra", "data", "hora",
              "preco", "duracao", "estado", "criado_em", "carrinho_json"]
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


CAMPOS_AGENDAMENTO = ["id", "telefone", "nome", "categoria", "servico", "extra", "data", "hora",
                       "preco", "duracao", "estado", "criado_em", "carrinho_json"]


def obter_agendamento(id_agendamento):
    with obter_bd() as conn:
        linha = conn.execute(
            "SELECT id, telefone, nome, categoria, servico, extra, data, hora, preco, duracao, estado, "
            "criado_em, carrinho_json FROM agendamentos WHERE id = ?",
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
            "SELECT id, telefone, nome, categoria, servico, extra, data, hora, preco, duracao, estado, "
            "criado_em, carrinho_json FROM agendamentos WHERE telefone = ? AND estado = 'confirmado' "
            "ORDER BY id DESC",
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


def total_centimos_agendamento(agendamento):
    """Total de uma marcação em cêntimos. Usa sempre o carrinho_json guardado
    com a marcação; se não existir (marcações antigas), cai para a coluna
    `preco` (em CHF). Nunca devolve 0 quando há de facto um preço guardado."""
    linhas = linhas_carrinho_agendamento(agendamento)
    if linhas:
        return sum(int(l.get("preco", 0)) * int(l.get("quantidade", 1) or 1) for l in linhas)
    preco = agendamento.get("preco")
    return int(round(float(preco) * 100)) if preco else 0


def atualizar_estado_agendamento(id_agendamento, estado):
    with obter_bd() as conn:
        conn.execute("UPDATE agendamentos SET estado = ? WHERE id = ?", (estado, id_agendamento))


# ---------------------------------------------------------------------------
# Pedidos de orçamento com fotografias (Wrap & Proteção)
# ---------------------------------------------------------------------------
def _wrap_veiculo_nome(sessao):
    """`wrap_veiculo` (coluna "veiculo" na BD) é construído a partir do tipo
    de veículo escolhido no passo 1 — o ano fica à parte, na sua própria
    coluna (ano_veiculo/"wrap_ano" na sessão). Nos modos rápido/especialista,
    onde o cliente não escolhe o veículo, fica um valor neutro."""
    return sessao.get("wrap_categoria_veiculo") or WRAP_NEUTRO_VEICULO


def _wrap_ano_valor(sessao):
    return sessao.get("wrap_ano") or WRAP_NEUTRO_ANO


def _wrap_tipo_nome(sessao):
    """Nome canónico (português) do tipo de wrap para a coluna "tipo_wrap".
    No modo rápido usa o INTERESSE declarado pelo cliente — incluindo
    "Ainda não sei", que nunca é convertido em "Wrap parcial". Quando nada
    foi escolhido (ex.: contacto com especialista) fica um valor neutro."""
    if sessao.get("wrap_modo") == MODO_RAPIDO:
        interesse = sessao.get("rapido_interesse")
        nomes = WRAP_RAPIDO_INTERESSES.get(interesse)
        return nomes["pt"] if nomes else WRAP_NEUTRO_TIPO
    wrap_tipo = sessao.get("wrap_tipo")
    if wrap_tipo in WRAP_NOMES:
        return WRAP_NOMES[wrap_tipo]["pt"]
    return WRAP_NEUTRO_TIPO


def _wrap_cor_acabamento_combinado(sessao):
    """A coluna "cor_acabamento" já existente combina cor + acabamento num
    único campo de texto, para manter compatibilidade com a base de dados
    atual, sem precisar de uma migração de esquema. Sem cor nem acabamento
    escolhidos (modo rápido/especialista), fica um valor neutro."""
    cor = sessao.get("wrap_cor")
    acabamento = sessao.get("wrap_acabamento")
    if cor and acabamento:
        return f"{cor} · {acabamento}"
    return cor or acabamento or WRAP_NEUTRO_COR_ACABAMENTO


def criar_pedido_orcamento(telefone, sessao, estado="rascunho"):
    """Cria um pedido de orçamento NOVO. Começa por omissão em estado
    "rascunho" — só passa a "novo" quando o cliente confirma o resumo final
    (ver finalizar_pedido_wrap/finalizar_pedido_rapido) — para nunca aparecer
    no painel como um pedido novo antes de o cliente o ter efetivamente
    confirmado. O pedido de contacto com especialista é a exceção: nasce logo
    em "contacto solicitado", porque não há mais nada a preencher."""
    with obter_bd() as conn:
        cur = conn.execute(
            "INSERT INTO pedidos_orcamento "
            "(telefone, nome, veiculo, ano_veiculo, tipo_wrap, cor_acabamento, estado, criado_em, "
            "carrinho_json, modo_pedido) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                telefone, sessao.get("nome"), _wrap_veiculo_nome(sessao),
                _wrap_ano_valor(sessao),
                _wrap_tipo_nome(sessao),
                _wrap_cor_acabamento_combinado(sessao),
                estado,
                datetime.utcnow().isoformat(),
                json.dumps(sessao.get("carrinho", [])),
                sessao.get("wrap_modo") or MODO_DETALHE,
            ),
        )
        return cur.lastrowid


def atualizar_pedido_orcamento(pedido_id, sessao):
    """Atualiza os dados de um pedido de orçamento JÁ EXISTENTE com o estado
    mais recente da sessão — usado sempre que o cliente altera uma escolha
    (ou muda de modo) depois de o pedido já ter sido criado, para nunca criar
    um pedido duplicado (ver _garantir_pedido_wrap)."""
    with obter_bd() as conn:
        conn.execute(
            "UPDATE pedidos_orcamento SET nome = ?, veiculo = ?, ano_veiculo = ?, tipo_wrap = ?, "
            "cor_acabamento = ?, carrinho_json = ?, modo_pedido = ? WHERE id = ?",
            (
                sessao.get("nome"), _wrap_veiculo_nome(sessao), _wrap_ano_valor(sessao),
                _wrap_tipo_nome(sessao),
                _wrap_cor_acabamento_combinado(sessao),
                json.dumps(sessao.get("carrinho", [])),
                sessao.get("wrap_modo") or MODO_DETALHE,
                pedido_id,
            ),
        )


def atualizar_estado_pedido(pedido_id, estado):
    with obter_bd() as conn:
        conn.execute("UPDATE pedidos_orcamento SET estado = ? WHERE id = ?", (estado, pedido_id))


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
            "agendamento_id, criado_em, carrinho_json, modo_pedido "
            "FROM pedidos_orcamento WHERE id = ?", (pedido_id,)
        ).fetchone()
    if not linha:
        return None
    campos = ["id", "telefone", "nome", "veiculo", "ano_veiculo", "tipo_wrap", "cor_acabamento",
              "estado", "agendamento_id", "criado_em", "carrinho_json", "modo_pedido"]
    pedido = dict(zip(campos, linha))
    pedido["modo_pedido"] = pedido["modo_pedido"] or MODO_DETALHE  # pedidos anteriores à migração
    return pedido


def listar_pedidos_orcamento():
    with obter_bd() as conn:
        linhas = conn.execute(
            "SELECT p.id, p.telefone, p.nome, p.veiculo, p.ano_veiculo, p.tipo_wrap, p.cor_acabamento, "
            "p.estado, p.agendamento_id, p.criado_em, p.carrinho_json, p.modo_pedido, "
            "COUNT(f.id) AS num_fotos "
            "FROM pedidos_orcamento p LEFT JOIN fotografias f ON f.pedido_id = p.id "
            "GROUP BY p.id ORDER BY p.id DESC"
        ).fetchall()
    campos = ["id", "telefone", "nome", "veiculo", "ano_veiculo", "tipo_wrap", "cor_acabamento",
              "estado", "agendamento_id", "criado_em", "carrinho_json", "modo_pedido", "num_fotos"]
    pedidos = [dict(zip(campos, l)) for l in linhas]
    for p in pedidos:
        p["modo_pedido"] = p["modo_pedido"] or MODO_DETALHE  # pedidos anteriores à migração
    return pedidos


def listar_fotografias(pedido_id):
    with obter_bd() as conn:
        linhas = conn.execute(
            "SELECT id, nome_ficheiro, mime_tipo, criado_em FROM fotografias "
            "WHERE pedido_id = ? ORDER BY id ASC", (pedido_id,)
        ).fetchall()
    campos = ["id", "nome_ficheiro", "mime_tipo", "criado_em"]
    return [dict(zip(campos, l)) for l in linhas]


def pedido_ativo_por_telefone(telefone):
    """Devolve o pedido de orçamento ATIVO mais recente de um número (ou
    None) — usado para o carrinho continuar a mostrar um pedido rápido/
    detalhado confirmado mesmo depois de a sessão ter sido reiniciada. A base
    de dados é sempre a fonte de verdade aqui, nunca a sessão."""
    with obter_bd() as conn:
        marcadores = ",".join("?" for _ in ESTADOS_PEDIDO_ATIVOS)
        linha = conn.execute(
            f"SELECT id FROM pedidos_orcamento WHERE telefone = ? AND estado IN ({marcadores}) "
            f"AND agendamento_id IS NULL ORDER BY id DESC LIMIT 1",
            (telefone, *ESTADOS_PEDIDO_ATIVOS),
        ).fetchone()
    return obter_pedido_orcamento(linha[0]) if linha else None


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
    return datetime.utcnow().isoformat()


def listar_linhas_orcamento(orcamento_id):
    with obter_bd() as conn:
        linhas = conn.execute(
            "SELECT id, orcamento_id, descricao, quantidade, preco_centimos, criado_em "
            "FROM orcamento_linhas WHERE orcamento_id = ? ORDER BY id ASC", (orcamento_id,)
        ).fetchall()
    return [dict(zip(CAMPOS_LINHA_ORCAMENTO, l)) for l in linhas]


def _compor_orcamento(linha_bd):
    orcamento = dict(zip(CAMPOS_ORCAMENTO, linha_bd))
    orcamento["linhas"] = listar_linhas_orcamento(orcamento["id"])
    orcamento["subtotal_centimos"] = sum(l["quantidade"] * l["preco_centimos"] for l in orcamento["linhas"])
    orcamento["total_centimos"] = max(0, orcamento["subtotal_centimos"] - orcamento["desconto_centimos"])
    return orcamento


def obter_orcamento_por_id(orcamento_id):
    with obter_bd() as conn:
        linha = conn.execute(
            "SELECT id, pedido_id, versao, estado, desconto_centimos, observacoes, validade_dias, "
            "criado_em, atualizado_em, enviado_em, respondido_em FROM orcamentos WHERE id = ?",
            (orcamento_id,),
        ).fetchone()
    return _compor_orcamento(linha) if linha else None


def obter_orcamento_atual(pedido_id):
    """Devolve a versão mais recente do orçamento de um pedido (rascunho,
    enviado, ou já respondido), com as respetivas linhas — ou None se ainda
    não existir nenhum orçamento para este pedido."""
    with obter_bd() as conn:
        linha = conn.execute(
            "SELECT id, pedido_id, versao, estado, desconto_centimos, observacoes, validade_dias, "
            "criado_em, atualizado_em, enviado_em, respondido_em "
            "FROM orcamentos WHERE pedido_id = ? ORDER BY versao DESC LIMIT 1",
            (pedido_id,),
        ).fetchone()
    return _compor_orcamento(linha) if linha else None


def listar_versoes_orcamento(pedido_id):
    """Todas as versões de um orçamento, da mais antiga para a mais recente
    — usada apenas para confirmar/consultar que versões anteriores nunca são
    apagadas nem reescritas quando o orçamento é revisto."""
    with obter_bd() as conn:
        linhas = conn.execute(
            "SELECT id, pedido_id, versao, estado, desconto_centimos, observacoes, validade_dias, "
            "criado_em, atualizado_em, enviado_em, respondido_em "
            "FROM orcamentos WHERE pedido_id = ? ORDER BY versao ASC",
            (pedido_id,),
        ).fetchall()
    return [_compor_orcamento(l) for l in linhas]


def obter_ou_criar_rascunho_orcamento(pedido_id):
    """Devolve o orçamento RASCUNHO atual de um pedido, pronto a editar no
    painel. Se a versão mais recente já tiver sido enviada (ou respondida
    pelo cliente), cria uma NOVA versão em rascunho — a versão anterior
    nunca é reescrita, para preservar sempre o que já foi enviado ao
    cliente. Copia as linhas/desconto/observações/validade da versão
    anterior como ponto de partida (uma revisão parte sempre do que já
    existia, não de uma folha em branco)."""
    atual = obter_orcamento_atual(pedido_id)
    if atual and atual["estado"] == "rascunho":
        return atual
    agora = _agora_iso()
    nova_versao = (atual["versao"] + 1) if atual else 1
    with obter_bd() as conn:
        cur = conn.execute(
            "INSERT INTO orcamentos (pedido_id, versao, estado, desconto_centimos, observacoes, "
            "validade_dias, criado_em, atualizado_em) VALUES (?, ?, 'rascunho', ?, ?, ?, ?, ?)",
            (pedido_id, nova_versao,
             atual["desconto_centimos"] if atual else 0,
             atual["observacoes"] if atual else None,
             atual["validade_dias"] if atual else 14,
             agora, agora),
        )
        novo_id = cur.lastrowid
        if atual:
            for l in atual["linhas"]:
                conn.execute(
                    "INSERT INTO orcamento_linhas (orcamento_id, descricao, quantidade, preco_centimos, criado_em) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (novo_id, l["descricao"], l["quantidade"], l["preco_centimos"], agora),
                )
    return obter_orcamento_por_id(novo_id)


def adicionar_linha_orcamento(orcamento_id, descricao, quantidade, preco_centimos):
    with obter_bd() as conn:
        conn.execute(
            "INSERT INTO orcamento_linhas (orcamento_id, descricao, quantidade, preco_centimos, criado_em) "
            "VALUES (?, ?, ?, ?, ?)",
            (orcamento_id, descricao, quantidade, preco_centimos, _agora_iso()),
        )
        conn.execute("UPDATE orcamentos SET atualizado_em = ? WHERE id = ?", (_agora_iso(), orcamento_id))


def editar_linha_orcamento(linha_id, descricao, quantidade, preco_centimos):
    with obter_bd() as conn:
        conn.execute(
            "UPDATE orcamento_linhas SET descricao = ?, quantidade = ?, preco_centimos = ? WHERE id = ?",
            (descricao, quantidade, preco_centimos, linha_id),
        )
        linha = conn.execute("SELECT orcamento_id FROM orcamento_linhas WHERE id = ?", (linha_id,)).fetchone()
        if linha:
            conn.execute("UPDATE orcamentos SET atualizado_em = ? WHERE id = ?", (_agora_iso(), linha[0]))


def remover_linha_orcamento(linha_id):
    with obter_bd() as conn:
        linha = conn.execute("SELECT orcamento_id FROM orcamento_linhas WHERE id = ?", (linha_id,)).fetchone()
        conn.execute("DELETE FROM orcamento_linhas WHERE id = ?", (linha_id,))
        if linha:
            conn.execute("UPDATE orcamentos SET atualizado_em = ? WHERE id = ?", (_agora_iso(), linha[0]))


def obter_linha_orcamento(linha_id):
    with obter_bd() as conn:
        linha = conn.execute(
            "SELECT id, orcamento_id, descricao, quantidade, preco_centimos, criado_em "
            "FROM orcamento_linhas WHERE id = ?", (linha_id,)
        ).fetchone()
    return dict(zip(CAMPOS_LINHA_ORCAMENTO, linha)) if linha else None


def atualizar_campos_orcamento(orcamento_id, desconto_centimos=None, observacoes=None, validade_dias=None):
    campos, valores = [], []
    if desconto_centimos is not None:
        campos.append("desconto_centimos = ?"); valores.append(desconto_centimos)
    if observacoes is not None:
        campos.append("observacoes = ?"); valores.append(observacoes)
    if validade_dias is not None:
        campos.append("validade_dias = ?"); valores.append(validade_dias)
    if not campos:
        return
    campos.append("atualizado_em = ?"); valores.append(_agora_iso())
    valores.append(orcamento_id)
    with obter_bd() as conn:
        conn.execute(f"UPDATE orcamentos SET {', '.join(campos)} WHERE id = ?", valores)


def marcar_orcamento_enviado(orcamento_id):
    agora = _agora_iso()
    with obter_bd() as conn:
        conn.execute(
            "UPDATE orcamentos SET estado = 'enviado', enviado_em = ?, atualizado_em = ? WHERE id = ?",
            (agora, agora, orcamento_id),
        )


def atualizar_estado_orcamento(orcamento_id, estado):
    agora = _agora_iso()
    with obter_bd() as conn:
        conn.execute(
            "UPDATE orcamentos SET estado = ?, respondido_em = ?, atualizado_em = ? WHERE id = ?",
            (estado, agora, agora, orcamento_id),
        )


def registar_interacao_cliente(telefone):
    """Marca "agora" como a última mensagem recebida deste número — usado só
    para saber se ainda estamos dentro da janela de 24h de atendimento ao
    cliente da Meta (ver dentro_da_janela_24h). Tabela à parte da sessão."""
    agora = _agora_iso()
    with obter_bd() as conn:
        conn.execute(
            "INSERT INTO interacoes_cliente (telefone, ultima_mensagem_em) VALUES (?, ?) "
            "ON CONFLICT(telefone) DO UPDATE SET ultima_mensagem_em = excluded.ultima_mensagem_em",
            (telefone, agora),
        )


def dentro_da_janela_24h(telefone):
    with obter_bd() as conn:
        linha = conn.execute(
            "SELECT ultima_mensagem_em FROM interacoes_cliente WHERE telefone = ?", (telefone,)
        ).fetchone()
    if not linha or not linha[0]:
        return False
    try:
        ultima = datetime.fromisoformat(linha[0])
    except ValueError:
        return False
    return (datetime.utcnow() - ultima) < timedelta(hours=24)


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


def titulo_linha_carrinho(telefone, idioma, sessao):
    """Rótulo da linha "🛒 Carrinho" nas listas. NUNCA mostra CHF 0 quando há
    alguma coisa guardada na base de dados: uma configuração em curso na
    sessão mostra o total dessa sessão; sem sessão, um pedido de orçamento
    pendente mostra "1 pendente", uma marcação confirmada mostra o total real
    dessa marcação, e várias marcações mostram a contagem."""
    if sessao.get("carrinho"):
        return f"🛒 Carrinho · {formatar_centimos(carrinho_total_centimos(sessao), idioma)}"

    agendamentos = agendamentos_confirmados_por_telefone(telefone)
    if pedido_ativo_por_telefone(telefone):
        # Um pedido pendente tem sempre prioridade no rótulo (é o que está à
        # espera de resposta); as marcações continuam visíveis dentro do
        # carrinho, ver mostrar_pedido_pendente_carrinho.
        return t("carrinho_botao_ver_pendente", idioma, n=1)
    if len(agendamentos) == 1:
        return f"🛒 Carrinho · {formatar_centimos(total_centimos_agendamento(agendamentos[0]), idioma)}"
    if agendamentos:
        return t("carrinho_botao_ver_marcacoes", idioma, n=len(agendamentos))
    return f"🛒 Carrinho · {formatar_centimos(0, idioma)}"


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
    espaço dentro dessas 10 linhas."""
    if com_cancelar is None:
        com_cancelar = com_voltar
    rows = []
    for i, opc in enumerate(opcoes):
        if isinstance(opc, dict):
            titulo = tx(opc["titulo"], idioma)
            row = {"id": opc.get("id", f"opt_{i}"), "title": titulo[:MAX_TITULO_LINHA]}
            desc = tx(opc.get("descricao"), idioma)
            if desc:
                row["description"] = desc[:72]
        else:
            row = {"id": f"opt_{i}", "title": str(opc)[:MAX_TITULO_LINHA]}
        rows.append(row)

    if sessao is not None:
        rows.append({"id": "ver_carrinho",
                     "title": titulo_linha_carrinho(destinatario, idioma, sessao)[:MAX_TITULO_LINHA]})

    # "⚡ Pedido rápido" só entra quando SOBRA espaço dentro do limite de 10
    # linhas por lista da API do WhatsApp (contando Carrinho, Voltar e
    # Cancelar). Nas listas já cheias, o atalho continua disponível pelo
    # comando RAPIDO indicado no rodapé.
    linhas_finais = (1 if com_voltar else 0) + (1 if com_cancelar else 0)
    if com_rapido and len(rows) + linhas_finais + 1 <= MAX_LINHAS_LISTA:
        rows.append({"id": "modo_rapido", "title": t("rapido_linha_lista", idioma)[:MAX_TITULO_LINHA]})

    if com_voltar:
        rows.append({"id": ID_VOLTAR, "title": t("voltar_titulo", idioma), "description": t("voltar_desc", idioma)})
    if com_cancelar:
        rows.append({"id": ID_CANCELAR, "title": t("cancelar_titulo", idioma), "description": t("cancelar_desc", idioma)})

    # Rede de segurança: a API rejeita listas com mais de 10 linhas.
    rows = rows[:MAX_LINHAS_LISTA]

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


def enviar_botoes(destinatario, corpo, botoes, idioma, rodape=None):
    interactive = {
        "type": "button",
        "body": {"text": corpo},
        "action": {"buttons": [
            {"type": "reply", "reply": {"id": b["id"], "title": tx(b["titulo"], idioma)[:MAX_TITULO_BOTAO]}}
            for b in botoes[:MAX_BOTOES]
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


def wa_me_link(telefone):
    """Ligação segura wa.me para abrir diretamente uma conversa de WhatsApp
    com este número — usada como ALTERNATIVA ao envio pelo próprio bot
    (nunca o método principal), tanto no botão "Contactar cliente" do painel
    como na resposta ao botão "💬 Contactar cliente" da notificação interna."""
    return f"https://wa.me/{telefone.lstrip('+')}"


def link_dossie_pedido(pedido_id):
    """Ligação direta ao dossiê de um pedido no painel (aberta automaticamente
    ao carregar, ver o pequeno script no DASHBOARD_HTML). PUBLIC_BASE_URL tem
    sempre prioridade; sem ela, tenta deduzir-se do próprio pedido HTTP em
    curso (webhook) — e, se isso também não for possível, a ligação é
    simplesmente omitida em vez de rebentar."""
    base = PUBLIC_BASE_URL
    if not base:
        try:
            base = request.url_root.rstrip("/")
        except RuntimeError:
            base = ""
    return f"{base}/dashboard#pedido-{pedido_id}" if base else ""


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
# Sistema central de preços e carrinho — usado por TODOS os fluxos de
# marcação (Limpeza, Estética e Wrap). O carrinho vive dentro da própria
# sessão (sessao["carrinho"]), como lista de linhas. Cada linha tem:
#   id         - identificador interno (id do catálogo, ou fixo p/ wrap_cor)
#   grupo      - um dos GRUPOS_CARRINHO abaixo
#   nome       - nome canónico, sempre em português (tal como o resto do bot)
#   preco      - inteiro, em CÊNTIMOS (CHF), para evitar erros de cálculo
#   quantidade - inteiro (sempre 1 nos fluxos atuais; suportado para o futuro)
# O idioma NUNCA é gravado na linha — é só usado em tempo real, na
# apresentação, via carrinho_nome_traduzido()/formatar_centimos(). O total é
# SEMPRE recalculado a partir das linhas atuais (carrinho_total_centimos),
# nunca somado/subtraído sobre um valor antigo guardado.
# ---------------------------------------------------------------------------
GRUPO_SERVICO_BASE = "servico_base"        # tipo de Limpeza / serviço de Estética
GRUPO_TAMANHO_VEICULO = "tamanho_veiculo"  # tamanho (Limpeza) ou estado (Estética) do veículo
GRUPO_WRAP_VEICULO = "wrap_veiculo"        # tipo de veículo (Wrap, passo 1)
GRUPO_WRAP_TIPO = "wrap_tipo"              # wrap total / parcial
GRUPO_WRAP_COR = "wrap_cor"                # cor (família + cor, ou personalizada)
GRUPO_ACABAMENTO = "acabamento"            # acabamento do wrap (brilhante, mate, ...)
GRUPO_EXTRA = "extra"                      # extras de Limpeza/Estética
GRUPO_DESCONTO = "desconto"                # reservado para futuros descontos/promoções

GRUPOS_CARRINHO = (GRUPO_SERVICO_BASE, GRUPO_TAMANHO_VEICULO, GRUPO_WRAP_VEICULO, GRUPO_WRAP_TIPO,
                    GRUPO_WRAP_COR, GRUPO_ACABAMENTO, GRUPO_EXTRA, GRUPO_DESCONTO)

# Grupos "únicos": escolher um novo item do mesmo grupo substitui sempre o
# anterior (nunca coexistem duas linhas do mesmo grupo único).
GRUPOS_UNICOS = {GRUPO_SERVICO_BASE, GRUPO_TAMANHO_VEICULO, GRUPO_WRAP_VEICULO, GRUPO_WRAP_TIPO,
                  GRUPO_WRAP_COR, GRUPO_ACABAMENTO}

# Grupos que o cliente pode retirar livremente do carrinho (itens opcionais).
# Todos os outros são obrigatórios: só podem ser SUBSTITUÍDOS (o cliente é
# reencaminhado para o passo onde são escolhidos), nunca simplesmente removidos.
GRUPOS_REMOVIVEIS = {GRUPO_EXTRA, GRUPO_DESCONTO}


def carrinho_definir_item(sessao, grupo, item_id, nome_pt, preco_centimos, quantidade=1):
    """Adiciona ou substitui uma linha do carrinho. Para grupos únicos,
    remove qualquer linha anterior do mesmo grupo antes de acrescentar a
    nova (substituição). Para os restantes, substitui apenas uma linha com
    o mesmo id, se existir."""
    carrinho = sessao.setdefault("carrinho", [])
    if grupo in GRUPOS_UNICOS:
        carrinho[:] = [linha for linha in carrinho if linha["grupo"] != grupo]
    else:
        carrinho[:] = [linha for linha in carrinho if linha["id"] != item_id]
    carrinho.append({
        "id": item_id, "grupo": grupo, "nome": nome_pt,
        "preco": int(preco_centimos), "quantidade": quantidade,
    })
    return carrinho


def carrinho_remover_grupo(sessao, grupo):
    sessao["carrinho"] = [l for l in sessao.get("carrinho", []) if l["grupo"] != grupo]


def carrinho_remover_item(sessao, item_id):
    sessao["carrinho"] = [l for l in sessao.get("carrinho", []) if l["id"] != item_id]


def carrinho_esvaziar(sessao):
    sessao["carrinho"] = []


def carrinho_total_centimos(sessao):
    """Soma sempre as linhas ATUAIS do carrinho — nunca acumula sobre um
    total antigo guardado algures."""
    return sum(l["preco"] * l.get("quantidade", 1) for l in sessao.get("carrinho", []))


def carrinho_subtotal_centimos(sessao):
    """Subtotal = total sem descontos (hoje é sempre igual ao total, já que
    ainda não há nenhum fluxo que adicione linhas ao grupo "desconto")."""
    return sum(l["preco"] * l.get("quantidade", 1) for l in sessao.get("carrinho", [])
               if l["grupo"] != GRUPO_DESCONTO)


def formatar_centimos(centimos, idioma="pt"):
    """Formata um valor em cêntimos como CHF — 2 casas decimais só quando o
    valor não corresponde a um número inteiro de CHF, mantendo o estilo
    visual já usado no resto do bot para os casos mais comuns."""
    if centimos is None:
        return t("preco_a_combinar", idioma)
    valor = centimos / 100
    sinal = "-" if valor < 0 else ""
    if centimos % 100 == 0:
        return f"{sinal}CHF {abs(valor):.0f}"
    return f"{sinal}CHF {abs(valor):.2f}"


def _procurar_modificador_veiculo_por_nome_pt(nome_pt):
    for catalogo in (TAMANHOS_VEICULO, ESTADO_VEICULO):
        opcao = next((o for o in catalogo if o["titulo"]["pt"] == nome_pt), None)
        if opcao:
            return opcao
    return None


def carrinho_nome_traduzido(linha, idioma):
    """Traduz o nome canónico (sempre em português) de uma linha do carrinho
    para o idioma do cliente, reutilizando sempre os catálogos e funções de
    tradução já existentes — nunca duplica esses dados nem os fluxos."""
    grupo, nome_pt = linha["grupo"], linha["nome"]
    if grupo == GRUPO_SERVICO_BASE:
        return nome_servico_traduzido(nome_pt, idioma)
    if grupo == GRUPO_TAMANHO_VEICULO:
        opcao = _procurar_modificador_veiculo_por_nome_pt(nome_pt)
        return tx(opcao["titulo"], idioma) if opcao else nome_pt
    if grupo == GRUPO_WRAP_TIPO:
        opcao = next((v for v in WRAP_NOMES.values() if v["pt"] == nome_pt), None)
        return tx(opcao, idioma) if opcao else nome_pt
    if grupo == GRUPO_WRAP_VEICULO:
        dic = WRAP_VEICULO_NOMES.get(linha["id"])
        return tx(dic, idioma) if dic else nome_pt
    if grupo == GRUPO_WRAP_COR:
        dic = WRAP_CORES_NOMES.get(linha["id"])
        return tx(dic, idioma) if dic else nome_pt
    if grupo == GRUPO_ACABAMENTO:
        dic = WRAP_ACABAMENTO_NOMES.get(linha["id"])
        return tx(dic, idioma) if dic else nome_pt
    if grupo == GRUPO_EXTRA:
        return nome_extra_traduzido(nome_pt, idioma)
    # "desconto" ou qualquer id sem catálogo (texto livre, ex.: tipo de
    # veículo "Outro" ou cor personalizada): mostrado tal como foi guardado.
    return nome_pt


def linhas_traduzidas(linhas, idioma):
    """Traduz uma lista de linhas de carrinho (venham da sessão ou do
    carrinho_json guardado com uma marcação — o formato é exatamente o
    mesmo)."""
    return [{**linha, "nome_traduzido": carrinho_nome_traduzido(linha, idioma)}
            for linha in (linhas or [])]


def linhas_carrinho_traduzidas(sessao, idioma):
    """Devolve as linhas do carrinho com o nome já traduzido para
    apresentação (idioma só entra aqui, nunca é gravado na linha)."""
    return linhas_traduzidas(sessao.get("carrinho", []), idioma)


def discriminacao_de_linhas(linhas, idioma):
    """Linhas de texto "• Nome: CHF X" a partir de linhas de carrinho soltas."""
    return [f"• {item['nome_traduzido']}: {formatar_centimos(item['preco'], idioma)}"
            for item in linhas_traduzidas(linhas, idioma)]


def linhas_discriminacao(sessao, idioma):
    """Linhas de texto prontas a mostrar (cliente ou negócio, consoante o
    `idioma` passado — "pt" para as notificações internas)."""
    return discriminacao_de_linhas(sessao.get("carrinho", []), idioma)


def carrinho_nome_traduzido_por_grupo(sessao, grupo, idioma):
    """Nome traduzido da linha do carrinho de um dado grupo (ou None, se o
    grupo ainda não tiver nenhuma linha) — usado no resumo final do Wrap."""
    linha = next((l for l in sessao.get("carrinho", []) if l["grupo"] == grupo), None)
    return carrinho_nome_traduzido(linha, idioma) if linha else None


def _preco_servico_base_centimos(sessao):
    linha = next((l for l in sessao.get("carrinho", []) if l["grupo"] == GRUPO_SERVICO_BASE), None)
    return linha["preco"] if linha else 0


def carrinho_definir_servico_base(sessao, catalogo, item_id):
    """Usa os preços já existentes de Limpeza/Estética (guardados em CHF
    inteiros no catálogo) — apenas convertidos para cêntimos aqui."""
    opcao = encontrar_opcao(catalogo, item_id) or {}
    nome_pt = tx(opcao.get("titulo"), "pt")
    preco_centimos = int(opcao.get("preco", 0)) * 100
    carrinho_definir_item(sessao, GRUPO_SERVICO_BASE, item_id, nome_pt, preco_centimos)
    return preco_centimos


def carrinho_definir_modificador_veiculo(sessao, catalogo, item_id):
    """Tamanho (Limpeza) ou estado (Estética) do veículo: aplicam um FATOR
    multiplicativo sobre o preço base — aqui é convertido no acréscimo em
    cêntimos correspondente, para poder ser somado como mais uma linha do
    carrinho (nunca se multiplica um total antigo)."""
    opcao = encontrar_opcao(catalogo, item_id) or {"fator": 1.0}
    nome_pt = tx(opcao.get("titulo"), "pt")
    fator = opcao.get("fator", 1.0)
    base_centimos = _preco_servico_base_centimos(sessao)
    delta_centimos = round(base_centimos * (fator - 1.0))
    carrinho_definir_item(sessao, GRUPO_TAMANHO_VEICULO, item_id, nome_pt, delta_centimos)


def carrinho_definir_extra(sessao, catalogo, item_id):
    """Extras de Limpeza/Estética. A opção "Nenhum extra" não é uma linha do
    carrinho — apenas remove qualquer extra anteriormente escolhido."""
    opcao = encontrar_opcao(catalogo, item_id) or {}
    nome_pt = tx(opcao.get("titulo"), "pt")
    if not nome_pt or "nenhum" in nome_pt.lower():
        carrinho_remover_grupo(sessao, GRUPO_EXTRA)
        return
    preco_centimos = int(opcao.get("preco", 0)) * 100
    carrinho_definir_item(sessao, GRUPO_EXTRA, item_id, nome_pt, preco_centimos)


def carrinho_definir_wrap_veiculo(sessao, item_id, nome_pt_livre=None):
    """Tipo de veículo (passo 1 do Wrap). `nome_pt_livre` é usado apenas
    quando o cliente escolheu "Outro" e escreveu o tipo manualmente —
    nesse caso o `item_id` usado é sempre "wv_outro_livre" (nunca o
    "wv_outro" do catálogo), para a tradução nunca confundir o texto livre
    do cliente com a opção genérica "Outro" do catálogo."""
    preco_centimos = WRAP_VEICULO_PRECOS_CENTIMOS.get(item_id, 0)
    if nome_pt_livre:
        nome_pt = nome_pt_livre
    else:
        opcao = encontrar_opcao(WRAP_TIPOS_VEICULO, item_id) or {}
        nome_pt = _remover_emoji_prefixo(tx(opcao.get("titulo"), "pt"))
    carrinho_definir_item(sessao, GRUPO_WRAP_VEICULO, item_id, nome_pt, preco_centimos)


def carrinho_definir_wrap_tipo(sessao, wrap_tipo_id):
    """Wrap total/parcial: tabela de preços de demonstração própria
    (WRAP_PRECOS_CENTIMOS), claramente separada dos catálogos de
    Limpeza/Estética e já em cêntimos."""
    nome_pt = WRAP_NOMES[wrap_tipo_id]["pt"]
    preco_centimos = WRAP_PRECOS_CENTIMOS[wrap_tipo_id]
    carrinho_definir_item(sessao, GRUPO_WRAP_TIPO, wrap_tipo_id, nome_pt, preco_centimos)


def carrinho_definir_wrap_cor(sessao, item_id, nome_pt):
    """Cor do wrap (passo 4/5): tabela de preços de demonstração própria
    (WRAP_COR_PRECOS_CENTIMOS) — cores de catálogo ficam sem acréscimo por
    omissão; só a cor personalizada (fora de catálogo) tem um valor
    demonstrativo próprio."""
    preco_centimos = WRAP_COR_PRECOS_CENTIMOS.get(item_id, 0)
    carrinho_definir_item(sessao, GRUPO_WRAP_COR, item_id, nome_pt, preco_centimos)


def carrinho_definir_wrap_acabamento(sessao, item_id):
    """Acabamento do wrap (passo 6): tabela de preços de demonstração
    própria (WRAP_ACABAMENTO_PRECOS_CENTIMOS)."""
    opcao = encontrar_opcao(WRAP_ACABAMENTOS, item_id) or {}
    nome_pt = _remover_emoji_prefixo(tx(opcao.get("titulo"), "pt"))
    preco_centimos = WRAP_ACABAMENTO_PRECOS_CENTIMOS.get(item_id, 0)
    carrinho_definir_item(sessao, GRUPO_ACABAMENTO, item_id, nome_pt, preco_centimos)


# ---------------------------------------------------------------------------
# Passos do fluxo "Marcar" — Limpeza
# ---------------------------------------------------------------------------
def passo_limpeza_tipo(de, idioma, sessao=None):
    enviar_lista(de, t("limpeza_tipo_corpo", idioma), t("limpeza_tipo_seccao", idioma), LIMPEZA_TIPOS, idioma,
                 botao=t("limpeza_tipo_botao", idioma), com_voltar=True, rodape=t("rodape_padrao", idioma), sessao=sessao)


def passo_limpeza_tamanho(de, idioma, sessao=None):
    enviar_lista(de, t("limpeza_tamanho_corpo", idioma), t("tamanho_seccao", idioma), TAMANHOS_VEICULO, idioma,
                 botao=t("tamanho_botao", idioma), com_voltar=True, rodape=t("rodape_padrao", idioma), sessao=sessao)


def passo_limpeza_extra(de, idioma, sessao=None):
    enviar_lista(de, t("extra_corpo", idioma), t("extra_seccao", idioma), EXTRAS_LIMPEZA, idioma,
                 botao=t("extra_botao", idioma), com_voltar=True, rodape=t("rodape_padrao", idioma), sessao=sessao)


# ---------------------------------------------------------------------------
# Passos do fluxo "Marcar" — Estética
# ---------------------------------------------------------------------------
def passo_estetica_servico(de, idioma, sessao=None):
    enviar_lista(de, t("estetica_servico_corpo", idioma), t("estetica_servico_seccao", idioma), ESTETICA_SERVICOS, idioma,
                 botao=t("estetica_servico_botao", idioma), com_voltar=True, rodape=t("rodape_padrao", idioma), sessao=sessao)


def passo_estetica_estado(de, idioma, sessao=None):
    enviar_lista(de, t("estetica_estado_corpo", idioma), t("estado_seccao", idioma), ESTADO_VEICULO, idioma,
                 botao=t("estado_botao", idioma), com_voltar=True, rodape=t("rodape_padrao", idioma), sessao=sessao)


def passo_estetica_extra(de, idioma, sessao=None):
    enviar_lista(de, t("extra_corpo", idioma), t("extra_seccao", idioma), EXTRAS_ESTETICA, idioma,
                 botao=t("extra_botao", idioma), com_voltar=True, rodape=t("rodape_padrao", idioma), sessao=sessao)


# ---------------------------------------------------------------------------
# Data / hora / resumo / confirmação (comuns a limpeza e estética)
# ---------------------------------------------------------------------------
def passo_data(de, idioma, passo_n=4, sessao=None):
    enviar_lista(de, t("data_corpo", idioma, n=passo_n), t("data_seccao", idioma), proximos_dias(idioma), idioma,
                 botao=t("data_botao", idioma), com_voltar=True, rodape=t("rodape_padrao", idioma), sessao=sessao)


def passo_hora(de, idioma, passo_n=5, sessao=None):
    enviar_lista(de, t("hora_corpo", idioma, n=passo_n), t("hora_seccao", idioma), HORARIOS, idioma,
                 botao=t("hora_botao", idioma), com_voltar=True, rodape=t("rodape_padrao", idioma), sessao=sessao)


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
    # calcular_preco_duracao() continua a dar-nos a duração e os nomes
    # canónicos (português); o PREÇO em si vem agora sempre do carrinho — a
    # única fonte de verdade, recalculada a partir das linhas atuais.
    _, duracao_pt, servico_pt, extra_pt = calcular_preco_duracao(sessao)
    total_centimos = carrinho_total_centimos(sessao)

    # canónico (português) — é isto que fica na sessão/DB, tal como antes
    sessao["servico"] = servico_pt
    sessao["extra"] = extra_pt if extra_pt and "nenhum" not in extra_pt.lower() else None
    sessao["preco"] = round(total_centimos / 100, 2)
    sessao["duracao"] = duracao_pt
    guardar_sessao(de, sessao)

    duracao_disp = duracao_traduzida(servico_pt, duracao_pt, idioma)

    nome = primeiro_nome(sessao.get("nome"))
    titulo = t("resumo_titulo", idioma) + (f", {nome}" if nome else "")
    linhas = [titulo]
    linhas.append(t("resumo_data", idioma, data=sessao["data"]))
    linhas.append(t("resumo_hora", idioma, hora=sessao["hora"]))
    linhas.append(t("resumo_duracao", idioma, duracao=duracao_disp))
    linhas.append("")
    linhas.append(t("resumo_discriminacao", idioma))
    linhas.extend(linhas_discriminacao(sessao, idioma))
    linhas.append(t("resumo_total", idioma, total=formatar_centimos(total_centimos, idioma)))
    linhas.append("\n" + t("resumo_pergunta", idioma))

    enviar_botoes(de, "\n".join(linhas), [
        {"id": "confirmar", "titulo": t("botao_confirmar", idioma)},
        {"id": "alterar", "titulo": t("botao_alterar", idioma)},
        {"id": ID_CANCELAR, "titulo": t("botao_cancelar", idioma)},
    ], idioma, rodape=t("rodape_padrao", idioma))


def mensagem_confirmacao_final(sessao, idioma):
    nome = primeiro_nome(sessao.get("nome"))
    saudacao = t("obrigado_nome", idioma, nome=nome) if nome else t("obrigado", idioma)

    duracao_disp = duracao_traduzida(sessao["servico"], sessao.get("duracao", "-"), idioma)
    hora_curta = sessao["hora"].split(" ")[-1] if " " in sessao["hora"] else sessao["hora"]
    total_centimos = carrinho_total_centimos(sessao)

    linhas = [t("confirmado_titulo", idioma, saudacao=saudacao), ""]
    linhas.append(t("confirmado_data_hora", idioma, data=sessao["data"], hora=hora_curta))
    linhas.append(t("confirmado_duracao", idioma, duracao=duracao_disp))
    linhas.append(f"📍 {MORADA_OFICINA}")
    linhas.append("")
    linhas.append(t("resumo_discriminacao", idioma))
    linhas.extend(linhas_discriminacao(sessao, idioma))
    linhas.append(t("resumo_total", idioma, total=formatar_centimos(total_centimos, idioma)))
    linhas.append("")
    linhas.append(t("confirmado_instrucao", idioma))
    return "\n".join(linhas)


def mensagem_notificacao_provider(de, sessao, id_agendamento):
    """Sempre em português, independentemente do idioma do cliente — é o
    idioma de trabalho da equipa/dono do negócio. Inclui sempre a
    discriminação completa do carrinho e o total."""
    linhas = [f"🆕📅 *Novo pedido confirmado (#{id_agendamento})*", ""]
    linhas.append(f"👤 Cliente: {sessao.get('nome') or 'sem nome'}")
    linhas.append(f"📱 Contacto: {formatar_telefone(de)}")
    linhas.append(f"📅 Data: {sessao['data']} às {sessao['hora']}")
    linhas.append("")
    linhas.append("Discriminação:")
    linhas.extend(linhas_discriminacao(sessao, "pt"))
    linhas.append(f"💰 Total: {formatar_centimos(carrinho_total_centimos(sessao), 'pt')}")
    # Sem instruções escritas: as ações da equipa são a lista interativa que
    # acompanha esta mensagem (ver enviar_notificacao_interna_marcacao).
    return "\n".join(linhas)


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


def _avisar_cliente_marcacao_cancelada(agendamento):
    telefone = agendamento["telefone"]
    sessao_cliente = carregar_sessao(telefone)
    idioma = sessao_cliente.get("idioma") if sessao_cliente.get("idioma") in IDIOMAS_VALIDOS else "pt"
    enviar_botoes(telefone, t("marcacao_cancelada_equipa_cliente", idioma, id=agendamento["id"]), [
        {"id": ACAO_NOVA_MARCACAO, "titulo": t("botao_nova_marcacao", idioma)},
        {"id": ACAO_MENU, "titulo": t("botao_menu_principal", idioma)},
        {"id": ACAO_HUMANO, "titulo": t("botao_falar_equipa", idioma)},
    ], idioma)


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
        if ag["estado"] != "confirmado":
            _responder_equipa(f"ℹ️ A marcação #{id_agendamento} já não está confirmada "
                              f"(estado atual: {ag['estado']}).")
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
        if ag["estado"] != "confirmado":
            _responder_equipa(f"ℹ️ A marcação #{id_agendamento} já não está confirmada "
                              f"(estado atual: {ag['estado']}).")
            return True
        atualizar_estado_agendamento(id_agendamento, "cancelado")
        _avisar_cliente_marcacao_cancelada(ag)
        _responder_equipa(f"❌ Marcação {resumo} cancelada — cliente avisado.")
        return True

    if acao == "concluir":
        if ag["estado"] != "confirmado":
            _responder_equipa(f"ℹ️ A marcação #{id_agendamento} já não está confirmada "
                              f"(estado atual: {ag['estado']}).")
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
        if ag["estado"] != "confirmado":
            _responder_equipa(f"ℹ️ A marcação #{id_agendamento} já não está confirmada "
                              f"(estado atual: {ag['estado']}).")
            return True
        # "concluído" não é um estado confirmado, por isso a marcação sai
        # automaticamente do carrinho persistente do cliente (ver
        # agendamentos_confirmados_por_telefone).
        atualizar_estado_agendamento(id_agendamento, "concluído")
        _responder_equipa(f"✅ Marcação {resumo} marcada como concluída.")
        return True

    return True


# ---------------------------------------------------------------------------
# Orçamentos criados no painel — ENVIO ao cliente pelo próprio bot
# ---------------------------------------------------------------------------
# Método PRINCIPAL de comunicar um orçamento: o botão "Contactar cliente" do
# painel (ver wa_me_link) é sempre uma ALTERNATIVA, nunca o caminho normal.
# Dentro da janela de 24h de atendimento ao cliente (ver dentro_da_janela_24h)
# envia-se a mensagem interativa normal; fora da janela, é preciso reabri-la
# com um template Utility pré-aprovado na Meta (ver enviar_orcamento_via_template).
# ---------------------------------------------------------------------------
def linhas_orcamento_texto(orcamento, idioma):
    linhas = []
    for l in orcamento["linhas"]:
        preco_linha = l["preco_centimos"] * l["quantidade"]
        qtd_txt = f" ×{l['quantidade']}" if l["quantidade"] != 1 else ""
        linhas.append(f"• {l['descricao']}{qtd_txt}: {formatar_centimos(preco_linha, idioma)}")
    return linhas


def corpo_mensagem_orcamento(pedido, orcamento, idioma):
    linhas = [t("orcamento_cliente_titulo", idioma, pedido=pedido["id"]), ""]
    linhas.extend(linhas_orcamento_texto(orcamento, idioma))
    linhas.append("")
    linhas.append(t("orcamento_cliente_subtotal", idioma,
                    subtotal=formatar_centimos(orcamento["subtotal_centimos"], idioma)))
    if orcamento["desconto_centimos"]:
        linhas.append(t("orcamento_cliente_desconto", idioma,
                        desconto=formatar_centimos(orcamento["desconto_centimos"], idioma)))
    linhas.append(t("orcamento_cliente_total", idioma, total=formatar_centimos(orcamento["total_centimos"], idioma)))
    if orcamento.get("observacoes"):
        linhas.append(t("orcamento_cliente_observacoes", idioma, observacoes=orcamento["observacoes"]))
    linhas.append(t("orcamento_cliente_validade", idioma, dias=orcamento.get("validade_dias") or 14))
    return "\n".join(linhas)


def enviar_orcamento_via_template(telefone, idioma, pedido):
    """Fallback fora da janela de 24h de atendimento (ver dentro_da_janela_24h):
    a Meta só permite reabrir a conversa com um template Utility já aprovado.
    O botão de resposta rápida do template devolve um button_reply normal com
    o ID "ver_orcamento_<pedido_id>" (ver receber_mensagem), reabrindo a
    janela e disparando o envio da mensagem interativa completa. Os nomes dos
    templates e variáveis a configurar na Meta (PT/DE/EN) são indicados no
    resumo entregue ao cliente — nunca inventados nem enviados sem essa
    configuração prévia."""
    nome_template = f"orcamento_pronto_{idioma if idioma in IDIOMAS_VALIDOS else 'pt'}"
    nome_cliente = primeiro_nome(carregar_sessao(telefone).get("nome")) or "-"
    enviar({
        "messaging_product": "whatsapp",
        "to": telefone,
        "type": "template",
        "template": {
            "name": nome_template,
            "language": {"code": {"pt": "pt_PT", "de": "de", "en": "en"}.get(idioma, "pt_PT")},
            "components": [
                {"type": "body", "parameters": [
                    {"type": "text", "text": nome_cliente},
                    {"type": "text", "text": str(pedido["id"])},
                ]},
                {"type": "button", "sub_type": "quick_reply", "index": "0",
                 "parameters": [{"type": "payload", "payload": f"ver_orcamento_{pedido['id']}"}]},
            ],
        },
    })


def enviar_orcamento_cliente(pedido_id):
    """Envia (ou reenvia) ao cliente o orçamento ATUAL de um pedido — chamada
    tanto pelo painel ("Enviar orçamento") como pelo botão de resposta rápida
    do template de reabertura de janela. Nunca envia um orçamento em
    "rascunho": só depois de marcar_orcamento_enviado()."""
    pedido = obter_pedido_orcamento(pedido_id)
    orcamento = obter_orcamento_atual(pedido_id) if pedido else None
    if not pedido or not orcamento or orcamento["estado"] not in ("enviado", "alteração solicitada"):
        return
    telefone = pedido["telefone"]
    sessao_cliente = carregar_sessao(telefone)
    idioma = sessao_cliente.get("idioma") if sessao_cliente.get("idioma") in IDIOMAS_VALIDOS else "pt"

    if not dentro_da_janela_24h(telefone):
        enviar_orcamento_via_template(telefone, idioma, pedido)
        return

    corpo = corpo_mensagem_orcamento(pedido, orcamento, idioma)
    enviar_botoes(telefone, corpo, [
        {"id": f"orcamento_aceitar_{orcamento['id']}", "titulo": t("botao_orcamento_aceitar", idioma)},
        {"id": f"orcamento_alterar_{orcamento['id']}", "titulo": t("botao_orcamento_alterar", idioma)},
        {"id": f"orcamento_recusar_{orcamento['id']}", "titulo": t("botao_orcamento_recusar", idioma)},
    ], idioma)


# ---------------------------------------------------------------------------
# Orçamentos — resposta do CLIENTE (aceitar / pedir alteração / recusar)
# ---------------------------------------------------------------------------
def _orcamento_e_pedido_de(orcamento_id):
    orcamento = obter_orcamento_por_id(orcamento_id)
    if not orcamento:
        return None, None
    return orcamento, obter_pedido_orcamento(orcamento["pedido_id"])


def responder_orcamento_aceitar(de, idioma, orcamento_id):
    orcamento, pedido = _orcamento_e_pedido_de(orcamento_id)
    if not orcamento or not pedido:
        enviar_texto(de, t("pedido_ja_respondido_cliente", idioma))
        return
    if orcamento["estado"] != "enviado":
        enviar_texto(de, t("orcamento_ja_respondido", idioma))
        return
    atualizar_estado_orcamento(orcamento_id, "aceite")
    atualizar_estado_pedido(pedido["id"], "aceite")
    # "Avançar para agendamento" reaproveita o início do fluxo normal de
    # marcação (sem calendário próprio para orçamentos de Wrap — fora do
    # âmbito desta alteração, ver "Não implementes... calendário").
    enviar_botoes(de, t("orcamento_aceite_cliente", idioma), [
        {"id": ACAO_NOVA_MARCACAO, "titulo": t("botao_avancar_agendamento", idioma)},
        {"id": ACAO_MENU, "titulo": t("botao_menu_principal", idioma)},
    ], idioma)
    if PROVIDER_WHATSAPP:
        enviar_texto(PROVIDER_WHATSAPP, f"✅ Orçamento do pedido #{pedido['id']} foi ACEITE pelo cliente "
                                         f"{formatar_telefone(de)}.")


def mostrar_lista_alteracao_orcamento(de, idioma, orcamento_id):
    opcoes = [
        {"id": f"orcamento_alt_servico_{orcamento_id}", "titulo": t("alteracao_opcao_servico", idioma)},
        {"id": f"orcamento_alt_veiculo_{orcamento_id}", "titulo": t("alteracao_opcao_veiculo", idioma)},
        {"id": f"orcamento_alt_cor_{orcamento_id}", "titulo": t("alteracao_opcao_cor", idioma)},
        {"id": f"orcamento_alt_prazo_{orcamento_id}", "titulo": t("alteracao_opcao_prazo", idioma)},
        {"id": f"orcamento_alt_outra_{orcamento_id}", "titulo": t("alteracao_opcao_outra", idioma)},
        {"id": f"orcamento_alt_equipa_{orcamento_id}", "titulo": t("alteracao_opcao_equipa", idioma)},
    ]
    enviar_lista(de, t("alteracao_pergunta", idioma), t("alteracao_seccao", idioma), opcoes, idioma,
                 botao=t("alteracao_botao", idioma))


def registar_pedido_alteracao(de, idioma, orcamento_id, sessao, aspeto, texto_livre=None):
    orcamento, pedido = _orcamento_e_pedido_de(orcamento_id)
    if not orcamento or not pedido:
        enviar_texto(de, t("pedido_ja_respondido_cliente", idioma))
        return
    if aspeto == "equipa":
        falar_com_equipa(de, idioma, sessao)
        reiniciar_sessao(de)
        return
    if orcamento["estado"] != "enviado":
        enviar_texto(de, t("orcamento_ja_respondido", idioma))
        return
    atualizar_estado_orcamento(orcamento_id, "alteração solicitada")
    atualizar_estado_pedido(pedido["id"], "alteração solicitada")
    enviar_texto(de, t("alteracao_recebida_cliente", idioma))
    if PROVIDER_WHATSAPP:
        nomes_aspeto = {"servico": "Serviço/tipo de wrap", "veiculo": "Veículo", "cor": "Cor/acabamento",
                        "prazo": "Prazo/data", "outra": "Outra alteração"}
        descricao_aspeto = nomes_aspeto.get(aspeto, aspeto)
        texto = (f"✏️ Pedido de alteração ao orçamento do pedido #{pedido['id']} "
                 f"({formatar_telefone(de)})\n\nAspeto: {descricao_aspeto}")
        if texto_livre:
            texto += f"\n\nDescrição do cliente: {texto_livre}"
        link = link_dossie_pedido(pedido["id"])
        if link:
            texto += f"\n\n📋 {link}"
        enviar_texto(PROVIDER_WHATSAPP, texto)


def responder_orcamento_recusar_confirmar(de, idioma, orcamento_id):
    orcamento, _ = _orcamento_e_pedido_de(orcamento_id)
    if not orcamento or orcamento["estado"] != "enviado":
        enviar_texto(de, t("orcamento_ja_respondido", idioma))
        return
    enviar_botoes(de, t("orcamento_recusar_confirmar_pergunta", idioma), [
        {"id": f"orcamento_recusar_sim_{orcamento_id}", "titulo": t("botao_sim_recusar", idioma)},
        {"id": f"orcamento_recusar_nao_{orcamento_id}", "titulo": t("botao_nao_voltar", idioma)},
    ], idioma)


def responder_orcamento_recusar_efetivar(de, idioma, orcamento_id):
    orcamento, pedido = _orcamento_e_pedido_de(orcamento_id)
    if not orcamento or not pedido:
        enviar_texto(de, t("pedido_ja_respondido_cliente", idioma))
        return
    if orcamento["estado"] != "enviado":
        enviar_texto(de, t("orcamento_ja_respondido", idioma))
        return
    atualizar_estado_orcamento(orcamento_id, "recusado")
    atualizar_estado_pedido(pedido["id"], "recusado")
    enviar_botoes(de, t("orcamento_recusado_cliente", idioma), [
        {"id": ACAO_NOVA_MARCACAO, "titulo": t("botao_novo_pedido", idioma)},
        {"id": ACAO_MENU, "titulo": t("botao_menu_principal", idioma)},
    ], idioma)
    if PROVIDER_WHATSAPP:
        enviar_texto(PROVIDER_WHATSAPP, f"❌ Orçamento do pedido #{pedido['id']} foi RECUSADO pelo cliente "
                                         f"{formatar_telefone(de)}.")


# ---------------------------------------------------------------------------
# Pedido pendente — CANCELAMENTO pelo próprio cliente, a partir do carrinho
# ---------------------------------------------------------------------------
def pedido_cliente_cancelar_confirmar(de, idioma, pedido_id):
    pedido = obter_pedido_orcamento(pedido_id)
    if not pedido or pedido["estado"] not in ESTADOS_PEDIDO_ATIVOS:
        enviar_texto(de, t("pedido_ja_respondido_cliente", idioma))
        return
    enviar_botoes(de, t("cancelar_pedido_confirmar_pergunta", idioma), [
        {"id": f"pedido_cancelar_cliente_sim_{pedido_id}", "titulo": t("botao_sim_cancelar", idioma)},
        {"id": f"pedido_cancelar_cliente_nao_{pedido_id}", "titulo": t("botao_nao_voltar", idioma)},
    ], idioma)


def pedido_cliente_cancelar_efetivar(de, idioma, pedido_id):
    pedido = obter_pedido_orcamento(pedido_id)
    if not pedido or pedido["estado"] not in ESTADOS_PEDIDO_ATIVOS:
        enviar_texto(de, t("pedido_ja_respondido_cliente", idioma))
        return
    atualizar_estado_pedido(pedido_id, "recusado")
    enviar_botoes(de, t("pedido_cancelado_cliente", idioma), [
        {"id": ACAO_NOVA_MARCACAO, "titulo": t("botao_nova_marcacao", idioma)},
        {"id": ACAO_MENU, "titulo": t("botao_menu_principal", idioma)},
    ], idioma)
    if PROVIDER_WHATSAPP:
        enviar_texto(PROVIDER_WHATSAPP, f"❌ Pedido #{pedido_id} cancelado pelo próprio cliente "
                                         f"{formatar_telefone(de)}.")


# ---------------------------------------------------------------------------
# Fluxo "Wrap & Proteção" — entrada: escolha do modo
# ---------------------------------------------------------------------------
# O cliente escolhe logo à entrada como quer avançar:
#   • MODO_RAPIDO       — 2 perguntas + resumo, sem preço calculado;
#   • MODO_DETALHE      — o fluxo completo de 8 passos (inalterado);
#   • MODO_ESPECIALISTA — pedido de contacto imediato, sem preencher nada.
# ---------------------------------------------------------------------------
def passo_wrap_modo(de, idioma, sessao=None):
    enviar_botoes(de, t("wrap_modo_corpo", idioma), [
        {"id": "modo_rapido", "titulo": t("wrap_modo_rapido_botao", idioma)},
        {"id": "modo_detalhe", "titulo": t("wrap_modo_detalhe_botao", idioma)},
        {"id": "modo_especialista", "titulo": t("wrap_modo_especialista_botao", idioma)},
    ], idioma, rodape=t("rodape_padrao", idioma))


# ---------------------------------------------------------------------------
# Fluxo "Wrap & Proteção" — ORÇAMENTO RÁPIDO (2 passos + resumo)
# ---------------------------------------------------------------------------
# Para quem não quer preencher todas as opções. Nunca calcula nem mostra um
# preço (nem sequer CHF 0): o valor fica sempre "sob análise da equipa" e a
# sessão guarda preco_sob_analise = True. Por isso este caminho NÃO usa o
# carrinho de linhas/preços — ver mostrar_carrinho(), que tem um ecrã
# próprio para este modo.
# ---------------------------------------------------------------------------
def passo_rapido_interesse(de, idioma, sessao=None):
    enviar_botoes(de, t("rapido_interesse_corpo", idioma), [
        {"id": "rapido_wrap_total", "titulo": t("wrap_total_botao", idioma)},
        {"id": "rapido_wrap_parcial", "titulo": t("wrap_parcial_botao", idioma)},
        {"id": "rapido_nao_sei", "titulo": t("rapido_nao_sei_botao", idioma)},
    ], idioma, rodape=t("rodape_wrap", idioma))


def passo_rapido_fotos(de, idioma, sessao=None):
    enviar_botoes(de, t("rapido_fotos_corpo", idioma), [
        {"id": "wrap_fotos_sim", "titulo": t("wrap_fotos_sim_botao", idioma)},
        {"id": "wrap_fotos_nao", "titulo": t("wrap_fotos_nao_botao", idioma)},
        {"id": "ver_carrinho", "titulo": t("rapido_ver_pedido_botao", idioma)},
    ], idioma, rodape=t("rodape_wrap", idioma))


def rapido_interesse_traduzido(sessao, idioma):
    """Interesse declarado no modo rápido, traduzido para o idioma do cliente
    (na base de dados e nas notificações internas fica sempre o "pt")."""
    nomes = WRAP_RAPIDO_INTERESSES.get(sessao.get("rapido_interesse"))
    return tx(nomes, idioma) if nomes else "-"


def passo_rapido_resumo(de, idioma, sessao):
    """Resumo simples do pedido rápido. Nunca mostra CHF — o preço fica
    sempre "sob análise da equipa". Só após "Confirmar" é que o pedido passa
    a "novo" e é enviado à equipa (ver finalizar_pedido_rapido)."""
    num_fotos = contar_fotografias(sessao.get("pedido_id"))

    linhas = [t("rapido_resumo_titulo", idioma), ""]
    linhas.append(t("rapido_resumo_nome", idioma, nome=sessao.get("nome") or "-"))
    linhas.append(t("rapido_resumo_contacto", idioma, contacto=formatar_telefone(de)))
    linhas.append(t("rapido_resumo_interesse", idioma, interesse=rapido_interesse_traduzido(sessao, idioma)))
    linhas.append(t("wrap_resumo_fotos", idioma, n=num_fotos))
    linhas.append("")
    linhas.append(t("rapido_preco_sob_analise", idioma))
    linhas.append("\n" + t("resumo_pergunta", idioma))

    enviar_botoes(de, "\n".join(linhas), [
        {"id": "rapido_confirmar", "titulo": t("botao_confirmar", idioma)},
        {"id": "rapido_alterar", "titulo": t("botao_alterar", idioma)},
        {"id": ID_CANCELAR, "titulo": t("botao_cancelar", idioma)},
    ], idioma, rodape=t("rodape_wrap", idioma))


def enviar_notificacao_interna_pedido(pedido_id, texto_provider):
    """Notificação interna (sempre em português) para QUALQUER pedido de
    Wrap & Proteção — rápido, detalhado ou de contacto com especialista.
    Em vez de pedir à equipa para escrever um comando de texto, mostra
    sempre 3 botões interativos com o pedido_id embutido no próprio ID
    (ver processar_resposta_interna_pedido, chamada antes do fluxo normal
    da sessão em receber_mensagem, para a resposta da equipa nunca ser
    interpretada como uma mensagem de cliente)."""
    if not PROVIDER_WHATSAPP or not pedido_id:
        return
    enviar_botoes(PROVIDER_WHATSAPP, texto_provider, [
        {"id": f"pedido_analisar_{pedido_id}", "titulo": t("botao_pedido_analisar", "pt")},
        {"id": f"pedido_contactar_{pedido_id}", "titulo": t("botao_pedido_contactar", "pt")},
        {"id": f"pedido_recusar_{pedido_id}", "titulo": t("botao_pedido_recusar", "pt")},
    ], "pt")


def recusar_pedido_e_avisar_cliente(pedido):
    """Lógica partilhada de "recusar pedido" — usada tanto pela notificação
    interna (equipa) como, no futuro, por outras origens. Marca o pedido
    como recusado, avisa o cliente no idioma guardado e oferece sempre as
    duas saídas universais (nunca obriga a escrever um comando)."""
    atualizar_estado_pedido(pedido["id"], "recusado")
    telefone_cliente = pedido["telefone"]
    sessao_cliente = carregar_sessao(telefone_cliente)
    idioma_cliente = sessao_cliente.get("idioma") if sessao_cliente.get("idioma") in IDIOMAS_VALIDOS else "pt"
    enviar_botoes(telefone_cliente, t("rapido_recusado_cliente", idioma_cliente), [
        {"id": ACAO_NOVA_MARCACAO, "titulo": t("botao_nova_marcacao", idioma_cliente)},
        {"id": ACAO_MENU, "titulo": t("botao_menu_principal", idioma_cliente)},
    ], idioma_cliente)


def finalizar_pedido_rapido(de, idioma, sessao, pedido_id=None):
    """Só é chamada depois de o cliente confirmar o resumo do modo rápido —
    é aqui que o pedido passa de "rascunho" a "novo" e é enviado à equipa."""
    if pedido_id:
        atualizar_pedido_orcamento(pedido_id, sessao)
        atualizar_estado_pedido(pedido_id, "novo")
    num_fotos = contar_fotografias(pedido_id)

    linhas = ["⚡ *Pedido rápido — Wrap & Proteção*", ""]
    if pedido_id:
        linhas.append(f"🆔 Pedido #{pedido_id}")
    linhas.append(f"👤 Cliente: {sessao.get('nome') or 'sem nome'}")
    linhas.append(f"📱 Contacto: {formatar_telefone(de)}")
    linhas.append(f"🎨 Interesse: {_wrap_tipo_nome(sessao)}")
    linhas.append(f"📸 Fotografias recebidas: {num_fotos}")
    linhas.append("💰 Preço: sob análise da equipa")
    texto_provider = "\n".join(linhas)  # notificações internas sempre em português

    enviar_texto(de, t("rapido_finalizado_cliente", idioma))
    enviar_notificacao_interna_pedido(pedido_id, texto_provider)


def processar_resposta_interna_pedido(id_botao):
    """Trata os botões "🔎 Analisar pedido" / "💬 Contactar cliente" /
    "❌ Recusar pedido" da notificação interna de um novo pedido de Wrap &
    Proteção (rápido, detalhado ou de contacto com especialista). É chamada
    logo à entrada de receber_mensagem, ANTES de a sessão do remetente ser
    carregada/tratada como uma mensagem de cliente (ver receber_mensagem) —
    assim a resposta da equipa nunca é interpretada como parte do fluxo do
    cliente.

    "Analisar pedido" passa o estado a "em análise" e devolve uma ligação
    direta ao dossiê no painel — só atua enquanto o pedido ainda estiver em
    "novo" (duplo toque ou toque tardio ficam sem efeito). "Contactar
    cliente" nunca muda o estado e pode ser usado quantas vezes forem
    necessárias — devolve sempre uma ligação wa.me segura para o número
    certo, como ALTERNATIVA ao envio do orçamento pelo próprio bot. "Recusar
    pedido" pede confirmação antes de recusar de facto, também protegido
    contra ações duplicadas."""
    if id_botao.startswith("pedido_analisar_"):
        acao, pedido_id_txt = "analisar", id_botao[len("pedido_analisar_"):]
    elif id_botao.startswith("pedido_contactar_"):
        acao, pedido_id_txt = "contactar", id_botao[len("pedido_contactar_"):]
    else:
        acao, pedido_id_txt = "recusar", id_botao[len("pedido_recusar_"):]

    try:
        pedido_id = int(pedido_id_txt)
    except ValueError:
        return

    pedido = obter_pedido_orcamento(pedido_id)
    if not pedido:
        if PROVIDER_WHATSAPP:
            enviar_texto(PROVIDER_WHATSAPP, f"⚠️ Pedido #{pedido_id} não encontrado.")
        return

    if acao == "contactar":
        # Nunca muda o estado do pedido e pode repetir-se sem qualquer
        # restrição — é só uma ligação direta, alternativa ao bot.
        if PROVIDER_WHATSAPP:
            enviar_texto(PROVIDER_WHATSAPP,
                         f"💬 Contacto direto com o cliente do pedido #{pedido_id}: {wa_me_link(pedido['telefone'])}")
        return

    if pedido["estado"] not in ("novo", "contacto solicitado"):
        # Já analisado/recusado anteriormente — impede ações duplicadas.
        # ("contacto solicitado" é o estado inicial de um pedido de contacto
        # com especialista — ver pedido_falar_especialista — e conta aqui
        # como "ainda por processar", tal como "novo".)
        if PROVIDER_WHATSAPP:
            enviar_texto(PROVIDER_WHATSAPP,
                         f"ℹ️ O pedido #{pedido_id} já tinha sido processado "
                         f"(estado atual: {pedido['estado']}).")
        return

    telefone_cliente = pedido["telefone"]
    sessao_cliente = carregar_sessao(telefone_cliente)
    idioma_cliente = sessao_cliente.get("idioma") if sessao_cliente.get("idioma") in IDIOMAS_VALIDOS else "pt"

    if acao == "analisar":
        atualizar_estado_pedido(pedido_id, "em análise")
        enviar_texto(telefone_cliente, t("pedido_em_analise_cliente", idioma_cliente))
        if PROVIDER_WHATSAPP:
            link = link_dossie_pedido(pedido_id)
            aviso = f"✅ Pedido #{pedido_id} em análise — cliente avisado."
            if link:
                aviso += f"\n📋 {link}"
            enviar_texto(PROVIDER_WHATSAPP, aviso)
    else:  # recusar
        recusar_pedido_e_avisar_cliente(pedido)
        if PROVIDER_WHATSAPP:
            enviar_texto(PROVIDER_WHATSAPP, f"❌ Pedido #{pedido_id} recusado — cliente avisado.")


# ---------------------------------------------------------------------------
# Fluxo "Wrap & Proteção" — FALAR COM ESPECIALISTA
# ---------------------------------------------------------------------------
def pedido_falar_especialista(de, idioma, sessao):
    """Confirmação imediata ao cliente + notificação interna à equipa, e um
    pedido no painel em estado "contacto solicitado" — sem preço inventado e
    sem obrigar o cliente a preencher mais nada. Reutiliza um pedido já
    existente (ex.: se o cliente vinha de outro modo), para não duplicar."""
    sessao["wrap_modo"] = MODO_ESPECIALISTA
    sessao["preco_sob_analise"] = True
    carrinho_esvaziar(sessao)

    pedido_id = sessao.get("pedido_id")
    if pedido_id:
        atualizar_pedido_orcamento(pedido_id, sessao)
        atualizar_estado_pedido(pedido_id, "contacto solicitado")
    else:
        pedido_id = criar_pedido_orcamento(de, sessao, estado="contacto solicitado")

    enviar_texto(de, t("especialista_cliente", idioma))

    num_fotos = contar_fotografias(pedido_id)
    linhas = ["💬 *Pedido de contacto — especialista de Wrap*", ""]
    linhas.append(f"🆔 Pedido #{pedido_id}")
    linhas.append(f"👤 Cliente: {sessao.get('nome') or 'sem nome'}")
    linhas.append(f"📱 Contacto: {formatar_telefone(de)}")
    if num_fotos:
        linhas.append(f"📸 Fotografias recebidas: {num_fotos}")
    linhas.append("💰 Preço: sob análise da equipa")
    enviar_notificacao_interna_pedido(pedido_id, "\n".join(linhas))

    reiniciar_sessao(de)


# ---------------------------------------------------------------------------
# Fluxo "Wrap & Proteção" — 8 passos, todos por opções (lista/botões), à
# exceção de "Outro" (tipo de veículo), "Outro/mais antigo" (ano) e "Criar a
# minha cor" (cor), os únicos pontos onde o cliente escreve manualmente.
# Ordem: 1) tipo de veículo, 2) ano, 3) wrap total/parcial, 4) família de
# cor, 5) cor, 6) acabamento, 7) fotografias, 8) resumo e confirmação.
# ---------------------------------------------------------------------------
def passo_wrap_veiculo(de, idioma, sessao=None):
    # 8 opções de catálogo: com o Carrinho, já preenche as 10 linhas
    # possíveis numa lista — por isso aqui só há Voltar (Cancelar continua
    # disponível pelo rodapé, como em qualquer outro passo).
    enviar_lista(de, t("wrap_veiculo_corpo", idioma), t("wrap_veiculo_seccao", idioma), WRAP_TIPOS_VEICULO, idioma,
                 botao=t("wrap_veiculo_botao", idioma), com_voltar=True, com_cancelar=False,
                 rodape=t("rodape_wrap", idioma), sessao=sessao, com_rapido=True)


def passo_wrap_veiculo_outro(de, idioma):
    enviar_texto(de, t("wrap_veiculo_outro_pedir", idioma) + "\n\n" + t("rodape_wrap", idioma))


def passo_wrap_ano(de, idioma, sessao=None):
    enviar_lista(de, t("wrap_ano_corpo", idioma), t("wrap_ano_seccao", idioma), opcoes_wrap_ano(idioma), idioma,
                 botao=t("wrap_ano_botao", idioma), com_voltar=True, rodape=t("rodape_wrap", idioma),
                 sessao=sessao, com_rapido=True)


def passo_wrap_ano_outro(de, idioma):
    enviar_texto(de, t("wrap_ano_outro_pedir", idioma) + "\n\n" + t("rodape_wrap", idioma))


def passo_wrap_tipo(de, idioma, sessao=None):
    opcoes = [
        {"id": "wrap_total", "titulo": t("wrap_total_botao", idioma)},
        {"id": "wrap_parcial", "titulo": t("wrap_parcial_botao", idioma)},
    ]
    enviar_lista(de, t("wrap_tipo_corpo", idioma), t("wrap_tipo_seccao", idioma), opcoes, idioma,
                 botao=t("wrap_tipo_botao", idioma), com_voltar=True, rodape=t("rodape_wrap", idioma),
                 sessao=sessao, com_rapido=True)


def passo_wrap_cor_familia(de, idioma, sessao=None):
    # 8 opções (7 famílias + "Criar a minha cor"): mesma lógica do passo 1 —
    # só Voltar na lista, Cancelar continua disponível pelo rodapé.
    enviar_lista(de, t("wrap_cor_familia_corpo", idioma), t("wrap_cor_familia_seccao", idioma), WRAP_FAMILIAS_COR,
                 idioma, botao=t("wrap_cor_familia_botao", idioma), com_voltar=True, com_cancelar=False,
                 rodape=t("rodape_wrap", idioma), sessao=sessao, com_rapido=True)


def passo_wrap_cor(de, idioma, sessao=None):
    familia_id = sessao.get("wrap_cor_familia_id") if sessao else None
    cores = WRAP_CORES_POR_FAMILIA.get(familia_id, [])
    enviar_lista(de, t("wrap_cor_corpo", idioma), t("wrap_cor_seccao", idioma), cores, idioma,
                 botao=t("wrap_cor_botao", idioma), com_voltar=True, rodape=t("rodape_wrap", idioma),
                 sessao=sessao, com_rapido=True)


def passo_wrap_cor_personalizada(de, idioma):
    enviar_texto(de, t("wrap_cor_personalizada_pedir", idioma) + "\n\n" + t("rodape_wrap", idioma))


def passo_wrap_acabamento(de, idioma, sessao=None):
    # 8 opções: mesma lógica dos passos 1 e 4 — só Voltar na lista.
    enviar_lista(de, t("wrap_acabamento_corpo", idioma), t("wrap_acabamento_seccao", idioma), WRAP_ACABAMENTOS,
                 idioma, botao=t("wrap_acabamento_botao", idioma), com_voltar=True, com_cancelar=False,
                 rodape=t("rodape_wrap", idioma), sessao=sessao, com_rapido=True)


def passo_wrap_fotos_pergunta(de, idioma, sessao=None):
    botoes = [
        {"id": "wrap_fotos_sim", "titulo": t("wrap_fotos_sim_botao", idioma)},
        {"id": "wrap_fotos_nao", "titulo": t("wrap_fotos_nao_botao", idioma)},
    ]
    if sessao is not None:
        botoes.append({"id": "ver_carrinho", "titulo": t("carrinho_botao_ver", idioma)})
    enviar_botoes(de, t("wrap_fotos_pergunta_corpo", idioma), botoes, idioma, rodape=t("rodape_wrap", idioma))


def passo_wrap_resumo(de, idioma, sessao):
    """Passo 8 (final): resumo completo do pedido, com discriminação e total
    estimado, e as opções Confirmar / Alterar / Cancelar. Só depois de
    "Confirmar" é que o pedido fica concluído e é enviado à equipa (ver
    finalizar_pedido_wrap) — mostrar este resumo NUNCA finaliza nada."""
    pedido_id = sessao.get("pedido_id")
    num_fotos = contar_fotografias(pedido_id)
    total_centimos = carrinho_total_centimos(sessao)

    # Nomes SEMPRE traduzidos a partir das linhas do carrinho — a sessão e a
    # base de dados guardam o valor canónico em português, mas ao cliente
    # mostra-se sempre o nome no seu idioma. Texto livre escrito pelo próprio
    # cliente (tipo de veículo "Outro", cor personalizada) não tem entrada nos
    # catálogos, por isso carrinho_nome_traduzido() devolve-o inalterado.
    nome = primeiro_nome(sessao.get("nome"))
    titulo = t("wrap_resumo_titulo", idioma) + (f", {nome}" if nome else "")
    linhas = [titulo, ""]
    linhas.append(t("wrap_resumo_veiculo", idioma,
                    veiculo=carrinho_nome_traduzido_por_grupo(sessao, GRUPO_WRAP_VEICULO, idioma) or "-"))
    linhas.append(t("wrap_resumo_ano", idioma, ano=sessao.get("wrap_ano", "-")))
    linhas.append(t("wrap_resumo_tipo", idioma,
                    tipo=carrinho_nome_traduzido_por_grupo(sessao, GRUPO_WRAP_TIPO, idioma) or "-"))
    linhas.append(t("wrap_resumo_cor", idioma,
                    cor=carrinho_nome_traduzido_por_grupo(sessao, GRUPO_WRAP_COR, idioma) or "-"))
    linhas.append(t("wrap_resumo_acabamento", idioma,
                    acabamento=carrinho_nome_traduzido_por_grupo(sessao, GRUPO_ACABAMENTO, idioma) or "-"))
    linhas.append(t("wrap_resumo_fotos", idioma, n=num_fotos))
    linhas.append("")
    linhas.append(t("resumo_discriminacao", idioma))
    linhas.extend(linhas_discriminacao(sessao, idioma))
    linhas.append(t("wrap_total_estimado", idioma, total=formatar_centimos(total_centimos, idioma)))
    linhas.append("\n" + t("resumo_pergunta", idioma))

    enviar_botoes(de, "\n".join(linhas), [
        {"id": "wrap_confirmar", "titulo": t("botao_confirmar", idioma)},
        {"id": "wrap_alterar", "titulo": t("botao_alterar", idioma)},
        {"id": ID_CANCELAR, "titulo": t("botao_cancelar", idioma)},
    ], idioma, rodape=t("rodape_padrao", idioma))


def finalizar_pedido_wrap(de, idioma, sessao, pedido_id=None):
    """Só é chamada depois de o cliente confirmar o resumo final (passo 8) —
    é aqui que o pedido passa de "rascunho" a "novo" e é, só agora, enviado
    à equipa."""
    if pedido_id:
        atualizar_pedido_orcamento(pedido_id, sessao)
        atualizar_estado_pedido(pedido_id, "novo")
    num_fotos = contar_fotografias(pedido_id)
    total_centimos = carrinho_total_centimos(sessao)

    linhas = ["📋 *Pedido de orçamento — Wrap & Proteção*", ""]
    if pedido_id:
        linhas.append(f"🆔 Pedido #{pedido_id}")
    linhas.append(f"👤 Cliente: {sessao.get('nome') or 'sem nome'}")
    linhas.append(f"📱 Contacto: {formatar_telefone(de)}")
    linhas.append(f"🚗 Tipo de veículo: {sessao.get('wrap_categoria_veiculo', '-')}")
    linhas.append(f"📅 Ano: {sessao.get('wrap_ano', '-')}")
    linhas.append("")
    linhas.append("Discriminação:")
    linhas.extend(linhas_discriminacao(sessao, "pt"))
    linhas.append(f"💰 Total estimado: {formatar_centimos(total_centimos, 'pt')}")
    linhas.append(f"📸 Fotografias recebidas: {num_fotos}")
    texto_provider = "\n".join(linhas)  # sempre em português, ver mensagem_notificacao_provider

    # Mensagem ao CLIENTE: nome do veículo traduzido a partir do carrinho
    # (o texto para o negócio, acima, mantém-se sempre em português).
    veiculo = (carrinho_nome_traduzido_por_grupo(sessao, GRUPO_WRAP_VEICULO, idioma)
               or t("wrap_veiculo_generico", idioma))
    enviar_texto(de, t("wrap_finalizado_cliente", idioma, veiculo=veiculo))

    linhas_cliente = [t("resumo_discriminacao", idioma)]
    linhas_cliente.extend(linhas_discriminacao(sessao, idioma))
    linhas_cliente.append(t("wrap_total_estimado", idioma, total=formatar_centimos(total_centimos, idioma)))
    enviar_texto(de, "\n".join(linhas_cliente))

    enviar_notificacao_interna_pedido(pedido_id, texto_provider)


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


def enviar_seletor_idioma(de):
    """Mensagem fixa nos 3 idiomas ao mesmo tempo + botões para escolher —
    não depende de nenhum idioma já escolhido, porque é isso que resolve."""
    enviar_botoes(de, TEXTO_SELETOR_IDIOMA, BOTOES_IDIOMA, "pt")  # idioma aqui só afeta tx(), que já são strings simples


def _wrap_limpar_escolhas(sessao):
    """Remove todas as escolhas já feitas no fluxo Wrap (passos 1-6) e as
    respetivas linhas do carrinho — usada tanto pelo botão "✏️ Alterar" do
    resumo final como pela substituição de um item obrigatório a partir do
    ecrã "Alterar item" do carrinho. Preserva sempre `pedido_id` e as
    fotografias já enviadas, para nunca criar um pedido duplicado (ver
    _garantir_pedido_wrap) — só os DADOS do pedido são reescritos, quando o
    cliente voltar a chegar ao fim do fluxo."""
    for campo in ("wrap_categoria_veiculo", "wrap_veiculo_id", "wrap_ano", "wrap_tipo",
                  "wrap_cor_familia", "wrap_cor_familia_id", "wrap_cor", "wrap_cor_id",
                  "wrap_acabamento", "wrap_acabamento_id",
                  "_wrap_aguardando_veiculo_texto", "_wrap_aguardando_ano_texto",
                  "_wrap_aguardando_cor_texto", "_wrap_etapa_resumo",
                  "rapido_interesse", "_rapido_etapa_resumo"):
        sessao.pop(campo, None)
    sessao.pop("aguardando_fotos", None)
    for grupo in (GRUPO_WRAP_VEICULO, GRUPO_WRAP_TIPO, GRUPO_WRAP_COR, GRUPO_ACABAMENTO):
        carrinho_remover_grupo(sessao, grupo)


def _garantir_pedido_wrap(de, sessao):
    """Cria o pedido de orçamento na primeira vez que é preciso (mal o
    cliente chega ao passo das fotografias, com todos os dados já
    escolhidos), ou atualiza o mesmo pedido nas vezes seguintes — nunca cria
    um pedido duplicado ao voltar, ao alterar uma escolha ou ao mudar de modo."""
    if sessao.get("pedido_id"):
        atualizar_pedido_orcamento(sessao["pedido_id"], sessao)
    else:
        sessao["pedido_id"] = criar_pedido_orcamento(de, sessao)
    return sessao["pedido_id"]


def arquivar_rascunho_wrap(sessao):
    """Arquiva um pedido de orçamento que tenha ficado em "rascunho" — isto
    é, criado durante o fluxo mas nunca confirmado pelo cliente. Chamada
    sempre que a sessão é abandonada (CANCELAR, MENU, mudança de idioma,
    esvaziar carrinho, recomeçar), para o painel nunca mostrar pedidos
    abandonados como se fossem novos. Pedidos já confirmados ("novo") ou de
    contacto com especialista nunca são tocados."""
    pedido_id = (sessao or {}).get("pedido_id")
    if not pedido_id:
        return
    pedido = obter_pedido_orcamento(pedido_id)
    if pedido and pedido.get("estado") == "rascunho":
        atualizar_estado_pedido(pedido_id, "arquivado")


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


def avancar_para_resumo_wrap(de, idioma, sessao):
    """Leva o cliente ao resumo final correto — o simples (modo rápido) ou o
    completo (modo detalhado). Nenhum deles finaliza o pedido: só a
    confirmação do cliente o faz."""
    if sessao.get("wrap_modo") == MODO_RAPIDO:
        sessao["_rapido_etapa_resumo"] = True
        guardar_sessao(de, sessao)
        passo_rapido_resumo(de, idioma, sessao)
    else:
        sessao["_wrap_etapa_resumo"] = True
        guardar_sessao(de, sessao)
        passo_wrap_resumo(de, idioma, sessao)


def mudar_para_modo_rapido(de, idioma, sessao):
    """Muda para o orçamento rápido a partir de qualquer ponto (comandos
    RAPIDO/QUICK/SCHNELL, ou o atalho "⚡ Pedido rápido" nas listas).
    Reutiliza sempre o mesmo `pedido_id`, se já existir, e preserva as
    fotografias já enviadas — nunca cria um pedido duplicado. As escolhas
    detalhadas e as linhas do carrinho são descartadas, porque neste modo
    não há preço calculado."""
    pedido_id = sessao.get("pedido_id")
    _wrap_limpar_escolhas(sessao)
    carrinho_esvaziar(sessao)
    sessao.update({"fluxo": "wrap", "categoria": "cat_wrap",
                   "wrap_modo": MODO_RAPIDO, "preco_sob_analise": True})
    if pedido_id:
        sessao["pedido_id"] = pedido_id
        atualizar_pedido_orcamento(pedido_id, sessao)
    guardar_sessao(de, sessao)
    enviar_texto(de, t("rapido_mudou_modo", idioma))
    passo_rapido_interesse(de, idioma, sessao)


def iniciar_escolha_categoria(de, idioma, sessao):
    """Ponto único que arranca o fluxo 'Marcar': mostra as categorias
    (Limpeza/Estética/Wrap). Reutilizado em todos os sítios que precisam de
    (re)começar a marcação — menu principal, gestão de marcação, voltar.
    Esvazia sempre o carrinho e quaisquer escolhas Wrap residuais: uma nova
    escolha de categoria é sempre um recomeço, nunca deve arrastar dados de
    uma tentativa anterior (e o rascunho anterior fica arquivado)."""
    arquivar_rascunho_wrap(sessao)
    carrinho_esvaziar(sessao)
    _wrap_limpar_escolhas(sessao)
    sessao.pop("pedido_id", None)
    sessao.pop("wrap_modo", None)
    sessao.pop("preco_sob_analise", None)
    sessao["fluxo"] = "escolher_categoria"
    guardar_sessao(de, sessao)
    enviar_botoes(de, t("categoria_pergunta", idioma), CATEGORIAS_MARCAR, idioma, rodape=t("rodape_padrao", idioma))


def mostrar_carrinho(de, idioma, sessao):
    """Mostra o conteúdo atual do carrinho: cada item e preço, subtotal,
    total (ou "total estimado", no caso do Wrap) e as ações Continuar /
    Alterar item / Esvaziar carrinho. Acessível a qualquer momento pelos
    comandos universais CARRINHO/CART/WARENKORB."""
    # Modo rápido (e contacto com especialista): não há preços calculados, por
    # isso mostra-se um ecrã próprio, sem subtotal nem total — nunca CHF 0.
    if sessao.get("preco_sob_analise"):
        linhas = [t("carrinho_rapido_titulo", idioma), ""]
        linhas.append(t("carrinho_rapido_preferencia", idioma,
                        preferencia=rapido_interesse_traduzido(sessao, idioma)))
        linhas.append(t("carrinho_rapido_preco", idioma))
        enviar_botoes(de, "\n".join(linhas), [
            {"id": "carrinho_continuar", "titulo": t("botao_continuar", idioma)},
            {"id": "carrinho_alterar", "titulo": t("carrinho_botao_alterar", idioma)},
            {"id": "carrinho_esvaziar", "titulo": t("carrinho_botao_esvaziar", idioma)},
        ], idioma, rodape=t("rodape_wrap", idioma))
        return

    if not sessao.get("carrinho"):
        # Sem nenhuma linha na SESSÃO atual — mas o carrinho reúne SEMPRE três
        # tipos de conteúdo, e a base de dados (nunca a sessão) é a fonte de
        # verdade para os dois últimos:
        #   1) a configuração em curso na sessão (o ramo acima);
        #   2) pedidos de orçamento ativos (ver pedido_ativo_por_telefone);
        #   3) marcações confirmadas (ver agendamentos_confirmados_por_telefone).
        # Por isso o carrinho continua a mostrar a marcação depois de a sessão
        # ter sido reiniciada na confirmação — e nunca aparece "CHF 0".
        pedido = pedido_ativo_por_telefone(de)
        agendamentos = agendamentos_confirmados_por_telefone(de)
        if pedido:
            mostrar_pedido_pendente_carrinho(de, idioma, pedido, agendamentos)
        elif len(agendamentos) == 1:
            mostrar_marcacao_carrinho(de, idioma, agendamentos[0])
        elif agendamentos:
            mostrar_lista_marcacoes_carrinho(de, idioma, agendamentos)
        else:
            # Só agora é que o carrinho está MESMO vazio: sem configuração em
            # curso, sem pedido ativo (o que inclui um orçamento aceite ainda
            # sem marcação) e sem nenhuma marcação confirmada.
            enviar_texto(de, t("carrinho_vazio", idioma))
            enviar_botoes(de, t("e_agora_pergunta", idioma), [
                {"id": ACAO_NOVA_MARCACAO, "titulo": t("botao_nova_marcacao", idioma)},
                {"id": ACAO_MENU, "titulo": t("botao_menu_principal", idioma)},
                {"id": ACAO_HUMANO, "titulo": t("botao_falar_equipa", idioma)},
            ], idioma)
        return

    estimado = sessao.get("categoria") == "cat_wrap" or sessao.get("fluxo") == "wrap"
    subtotal_centimos = carrinho_subtotal_centimos(sessao)
    total_centimos = carrinho_total_centimos(sessao)

    linhas = [t("carrinho_titulo", idioma), ""]
    linhas.extend(linhas_discriminacao(sessao, idioma))
    linhas.append("")
    linhas.append(t("carrinho_subtotal", idioma, subtotal=formatar_centimos(subtotal_centimos, idioma)))
    chave_total = "carrinho_total_estimado" if estimado else "carrinho_total"
    linhas.append(t(chave_total, idioma, total=formatar_centimos(total_centimos, idioma)))

    enviar_botoes(de, "\n".join(linhas), [
        {"id": "carrinho_continuar", "titulo": t("botao_continuar", idioma)},
        {"id": "carrinho_alterar", "titulo": t("carrinho_botao_alterar", idioma)},
        {"id": "carrinho_esvaziar", "titulo": t("carrinho_botao_esvaziar", idioma)},
    ], idioma)


def linhas_detalhe_marcacao(agendamento, idioma):
    """Bloco de texto com o dossiê completo de uma marcação confirmada:
    número, estado, serviço com discriminação completa, extras, data, hora,
    duração e total. A discriminação vem do carrinho_json guardado COM a
    marcação; marcações antigas (sem essa coluna) caem para serviço/extra/
    preço, sem nunca mostrar CHF 0 quando há um preço guardado."""
    linhas = [t("carrinho_marcacao_titulo", idioma), ""]
    linhas.append(t("carrinho_marcacao_id", idioma, id=agendamento["id"]))
    linhas.append(t("carrinho_marcacao_estado", idioma))
    linhas.append(t("carrinho_marcacao_servico", idioma,
                    servico=nome_servico_traduzido(agendamento.get("servico"), idioma) or "-"))
    if agendamento.get("extra"):
        linhas.append(t("carrinho_marcacao_extra", idioma,
                        extra=nome_extra_traduzido(agendamento["extra"], idioma)))
    linhas.append(t("carrinho_marcacao_data", idioma, data=agendamento.get("data") or "-"))
    linhas.append(t("carrinho_marcacao_hora", idioma, hora=agendamento.get("hora") or "-"))
    linhas.append(t("carrinho_marcacao_duracao", idioma,
                    duracao=duracao_traduzida(agendamento.get("servico"),
                                              recuperar_duracao(agendamento.get("servico"),
                                                                agendamento.get("duracao")) or "-", idioma)))

    linhas_carrinho = linhas_carrinho_agendamento(agendamento)
    if linhas_carrinho:
        linhas.append("")
        linhas.append(t("resumo_discriminacao", idioma))
        linhas.extend(discriminacao_de_linhas(linhas_carrinho, idioma))
    linhas.append(t("carrinho_marcacao_total", idioma,
                    total=formatar_centimos(total_centimos_agendamento(agendamento), idioma)))
    return linhas


def botoes_marcacao_carrinho(agendamento, idioma):
    return [
        {"id": f"gerir_ag_{agendamento['id']}", "titulo": t("botao_ver_gerir_marcacao", idioma)},
        {"id": ACAO_NOVA_MARCACAO, "titulo": t("botao_nova_marcacao", idioma)},
        {"id": ACAO_MENU, "titulo": t("botao_menu_principal", idioma)},
    ]


def mostrar_marcacao_carrinho(de, idioma, agendamento):
    """Carrinho com UMA marcação confirmada: dossiê completo + as três ações
    (Ver/Gerir marcação, Nova marcação, Menu principal). A marcação NÃO é
    copiada de volta para o carrinho da sessão — seria a forma mais fácil de
    a duplicar; este ecrã lê sempre diretamente da base de dados."""
    enviar_botoes(de, "\n".join(linhas_detalhe_marcacao(agendamento, idioma)),
                  botoes_marcacao_carrinho(agendamento, idioma), idioma)


def mostrar_lista_marcacoes_carrinho(de, idioma, agendamentos):
    """Várias marcações confirmadas: lista para escolher qual ver, com o
    total real de cada uma (nunca CHF 0)."""
    opcoes = []
    for ag in agendamentos[:MAX_LINHAS_LISTA - 2]:
        total = formatar_centimos(total_centimos_agendamento(ag), idioma)
        opcoes.append({
            "id": f"carrinho_marcacao_{ag['id']}",
            "titulo": f"#{ag['id']} · {ag.get('data') or '-'}",
            "descricao": f"{nome_servico_traduzido(ag.get('servico'), idioma)} · {ag.get('hora') or '-'} · {total}",
        })
    opcoes.append({"id": ACAO_NOVA_MARCACAO, "titulo": t("botao_nova_marcacao", idioma)})
    opcoes.append({"id": ACAO_MENU, "titulo": t("botao_menu_principal", idioma)})
    enviar_lista(de, t("carrinho_marcacoes_pergunta", idioma, n=len(agendamentos)),
                 t("carrinho_marcacoes_seccao", idioma), opcoes, idioma, botao=t("menu_botao", idioma))


def abrir_marcacao_do_carrinho(de, idioma, id_agendamento):
    """Abre o dossiê de uma marcação escolhida na lista do carrinho — só se
    ela continuar confirmada e pertencer a este número."""
    ag = obter_agendamento(id_agendamento)
    if not ag or ag["telefone"] != de or ag["estado"] != "confirmado":
        enviar_texto(de, t("carrinho_marcacao_nao_encontrada", idioma))
        return
    mostrar_marcacao_carrinho(de, idioma, ag)


def mostrar_pedido_pendente_carrinho(de, idioma, pedido, agendamentos=None):
    """Ecrã do carrinho para um pedido ATIVO persistente (rápido, detalhado
    ou de contacto com especialista) — mostrado mesmo depois de a sessão ter
    sido reiniciada, porque a base de dados (nunca a sessão) é a fonte de
    verdade aqui. Nunca mostra CHF 0: enquanto não houver orçamento enviado
    pelo painel, o preço aparece sempre como "sob análise"; assim que existe
    um orçamento enviado, mostra a discriminação e o total reais. Se também
    houver marcações confirmadas, elas continuam acessíveis a partir daqui."""
    orcamento = obter_orcamento_atual(pedido["id"])
    tem_orcamento_enviado = bool(orcamento) and orcamento["estado"] in ("enviado", "aceite", "alteração solicitada")

    modo = pedido.get("modo_pedido") or MODO_DETALHE
    emoji = MODO_EMOJI.get(modo, "🎨")
    nome_modo = tx(MODO_NOMES_TRADUZIDO.get(modo), idioma)
    tipo_traduzido = texto_tipo_wrap_traduzido(pedido.get("tipo_wrap"), idioma)
    estado_dic = ESTADO_PEDIDO_NOMES.get(pedido["estado"])
    estado_traduzido = tx(estado_dic, idioma) if estado_dic else pedido["estado"]

    linhas = [f"{emoji} *{nome_modo} — {tipo_traduzido}*"]
    linhas.append(t("carrinho_pendente_id", idioma, id=pedido["id"]))
    linhas.append(t("carrinho_pendente_estado", idioma, estado=estado_traduzido))
    linhas.append("")

    opcoes = []
    if tem_orcamento_enviado:
        for l in orcamento["linhas"]:
            preco_linha = l["preco_centimos"] * l["quantidade"]
            qtd_txt = f" ×{l['quantidade']}" if l["quantidade"] != 1 else ""
            linhas.append(f"• {l['descricao']}{qtd_txt}: {formatar_centimos(preco_linha, idioma)}")
        if orcamento["desconto_centimos"]:
            linhas.append(t("orcamento_cliente_desconto", idioma,
                            desconto=formatar_centimos(orcamento["desconto_centimos"], idioma)))
        linhas.append(t("orcamento_cliente_total", idioma, total=formatar_centimos(orcamento["total_centimos"], idioma)))
        if orcamento["estado"] == "enviado":
            opcoes.append({"id": f"orcamento_aceitar_{orcamento['id']}", "titulo": t("botao_orcamento_aceitar", idioma)})
            opcoes.append({"id": f"orcamento_alterar_{orcamento['id']}", "titulo": t("botao_orcamento_alterar", idioma)})
    else:
        linhas.append(t("carrinho_pendente_preco_sob_analise", idioma))

    if pedido["estado"] in ESTADOS_PEDIDO_ATIVOS:
        opcoes.append({"id": f"pedido_cancelar_cliente_{pedido['id']}", "titulo": t("botao_cancelar_pedido_cliente", idioma)})

    # As marcações confirmadas nunca desaparecem do carrinho só por haver
    # também um pedido pendente — ficam acessíveis aqui.
    agendamentos = agendamentos or []
    if agendamentos:
        linhas.append("")
        linhas.append(t("carrinho_marcacoes_extra_linha", idioma, n=len(agendamentos)))
        for ag in agendamentos[:2]:
            total = formatar_centimos(total_centimos_agendamento(ag), idioma)
            opcoes.append({"id": f"carrinho_marcacao_{ag['id']}",
                           "titulo": f"#{ag['id']} · {ag.get('data') or '-'}",
                           "descricao": f"{nome_servico_traduzido(ag.get('servico'), idioma)} · {total}"})

    opcoes.append({"id": "carrinho_continuar", "titulo": t("botao_continuar", idioma)})
    opcoes.append({"id": ACAO_MENU, "titulo": t("botao_menu_principal", idioma)})

    enviar_lista(de, "\n".join(linhas), t("mais_acoes_seccao", idioma), opcoes, idioma, botao=t("menu_botao", idioma))


def mostrar_alterar_carrinho(de, idioma, sessao):
    """Lista os itens do carrinho para o cliente escolher qual alterar ou
    remover. Itens opcionais (extras/descontos) são removidos diretamente;
    itens obrigatórios só podem ser SUBSTITUÍDOS — a escolha reencaminha
    para o passo onde são escolhidos (ver _reabrir_passo_para_grupo)."""
    # No modo rápido só há uma escolha (o interesse) e nenhuma linha de
    # carrinho — "Alterar" reabre diretamente essa pergunta.
    if sessao.get("preco_sob_analise"):
        passo_rapido_interesse(de, idioma, sessao)
        return
    if not sessao.get("carrinho"):
        enviar_texto(de, t("carrinho_vazio", idioma))
        return
    opcoes = []
    for item in linhas_carrinho_traduzidas(sessao, idioma):
        removivel = item["grupo"] in GRUPOS_REMOVIVEIS
        acao = t("carrinho_item_remover", idioma) if removivel else t("carrinho_item_substituir", idioma)
        # título só com o nome (pode ser truncado a 24 carateres pela lista);
        # preço e ação ficam sempre visíveis na descrição, nunca cortados.
        descricao = f"{formatar_centimos(item['preco'], idioma)} · {acao}"
        opcoes.append({"id": f"carrinho_item_{item['id']}", "titulo": item["nome_traduzido"], "descricao": descricao})
    enviar_lista(de, t("carrinho_alterar_pergunta", idioma), t("carrinho_botao_ver", idioma), opcoes, idioma,
                 botao=t("menu_botao", idioma))


def _reabrir_passo_para_grupo(de, idioma, sessao, grupo):
    """Um item OBRIGATÓRIO do carrinho só pode ser substituído: leva o
    cliente de volta ao passo onde esse item é escolhido, preservando o
    resto da sessão (nome, idioma, etc.)."""
    categoria = sessao.get("categoria")
    if categoria in ("cat_limpeza", "cat_estetica"):
        for campo in ("tipo_id", "tamanho_id", "estado_id", "extra_id", "data", "hora",
                      "servico", "extra", "preco", "duracao"):
            sessao.pop(campo, None)
        carrinho_remover_grupo(sessao, GRUPO_SERVICO_BASE)
        carrinho_remover_grupo(sessao, GRUPO_TAMANHO_VEICULO)
        carrinho_remover_grupo(sessao, GRUPO_EXTRA)
        guardar_sessao(de, sessao)
        (passo_limpeza_tipo if categoria == "cat_limpeza" else passo_estetica_servico)(de, idioma, sessao)
        return
    if sessao.get("fluxo") == "wrap":
        # Tal como em Limpeza/Estética, substituir qualquer item obrigatório
        # do Wrap recomeça o fluxo a partir do passo 1 — mas preserva sempre
        # pedido_id e fotografias já enviadas (nunca cria um pedido duplicado).
        _wrap_limpar_escolhas(sessao)
        guardar_sessao(de, sessao)
        passo_wrap_veiculo(de, idioma, sessao)
        return
    reenviar_passo_atual(de, idioma, sessao)


def passo_orcamento_generico(de, idioma):
    enviar_texto(de, t("orcamento_pedido", idioma) + "\n\n" + t("rodape_padrao", idioma))


def mostrar_gestao_marcacao(de, idioma, id_agendamento=None):
    """Sem `id_agendamento`, gere a marcação ativa mais recente (comportamento
    de sempre). Com um id, gere essa marcação em concreto — usado pelo botão
    "🗓️ Ver/Gerir marcação" do carrinho, quando há mais do que uma."""
    if id_agendamento is not None:
        completo = obter_agendamento(id_agendamento)
        if not completo or completo["telefone"] != de or completo["estado"] != "confirmado":
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
    ], idioma, rodape=t("rodape_padrao", idioma))


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
              t("ajuda_cancelar", idioma), t("ajuda_gerir", idioma), t("ajuda_carrinho", idioma),
              t("ajuda_rapido", idioma), t("ajuda_ajuda", idioma), t("ajuda_humano", idioma),
              t("ajuda_idioma", idioma)]
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


# ---------------------------------------------------------------------------
# Orçamentos — API do painel (secção 1 do pedido do cliente). Protegida pela
# mesma autenticação do resto do painel; validação sempre do lado do
# servidor (nunca confiar só na validação do JavaScript do browser).
# ---------------------------------------------------------------------------
def _validar_linha_orcamento(dados):
    descricao = str((dados or {}).get("descricao", "")).strip()
    if not descricao or len(descricao) > 200:
        return None, "Descrição inválida (obrigatória, até 200 caracteres)."
    try:
        quantidade = int((dados or {}).get("quantidade", 1))
        preco_centimos = int((dados or {}).get("preco_centimos", 0))
    except (TypeError, ValueError):
        return None, "Quantidade ou preço inválidos."
    if quantidade < 1 or quantidade > 999:
        return None, "Quantidade inválida (entre 1 e 999)."
    if preco_centimos < 0 or preco_centimos > 100_000_00:
        return None, "Preço inválido."
    return {"descricao": descricao, "quantidade": quantidade, "preco_centimos": preco_centimos}, None


@app.route("/api/pedidos/<int:pedido_id>/orcamento", methods=["GET"])
@requer_autenticacao
def api_orcamento_atual(pedido_id):
    if not obter_pedido_orcamento(pedido_id):
        return jsonify(erro="Pedido não encontrado"), 404
    return jsonify(orcamento=obter_orcamento_atual(pedido_id), versoes=listar_versoes_orcamento(pedido_id)), 200


@app.route("/api/pedidos/<int:pedido_id>/orcamento/linhas", methods=["POST"])
@requer_autenticacao
def api_orcamento_adicionar_linha(pedido_id):
    if not obter_pedido_orcamento(pedido_id):
        return jsonify(erro="Pedido não encontrado"), 404
    dados, erro = _validar_linha_orcamento(request.get_json(force=True, silent=True))
    if erro:
        return jsonify(erro=erro), 400
    orcamento = obter_ou_criar_rascunho_orcamento(pedido_id)
    adicionar_linha_orcamento(orcamento["id"], dados["descricao"], dados["quantidade"], dados["preco_centimos"])
    return jsonify(obter_orcamento_por_id(orcamento["id"])), 200


@app.route("/api/pedidos/<int:pedido_id>/orcamento/linhas/<int:linha_id>", methods=["PUT", "DELETE"])
@requer_autenticacao
def api_orcamento_linha(pedido_id, linha_id):
    if not obter_pedido_orcamento(pedido_id):
        return jsonify(erro="Pedido não encontrado"), 404
    linha = obter_linha_orcamento(linha_id)
    orcamento_atual = obter_orcamento_atual(pedido_id)
    # Uma linha só pode ser editada/removida enquanto pertencer ao RASCUNHO
    # atual deste pedido — nunca a uma versão já enviada ao cliente.
    if not linha or not orcamento_atual or linha["orcamento_id"] != orcamento_atual["id"] \
            or orcamento_atual["estado"] != "rascunho":
        return jsonify(erro="Linha não encontrada ou já não editável."), 404
    if request.method == "DELETE":
        remover_linha_orcamento(linha_id)
    else:
        dados, erro = _validar_linha_orcamento(request.get_json(force=True, silent=True))
        if erro:
            return jsonify(erro=erro), 400
        editar_linha_orcamento(linha_id, dados["descricao"], dados["quantidade"], dados["preco_centimos"])
    return jsonify(obter_orcamento_por_id(orcamento_atual["id"])), 200


@app.route("/api/pedidos/<int:pedido_id>/orcamento/rascunho", methods=["POST"])
@requer_autenticacao
def api_orcamento_rascunho(pedido_id):
    if not obter_pedido_orcamento(pedido_id):
        return jsonify(erro="Pedido não encontrado"), 404
    dados = request.get_json(force=True, silent=True) or {}
    orcamento = obter_ou_criar_rascunho_orcamento(pedido_id)

    desconto_centimos = None
    if "desconto_centimos" in dados:
        try:
            desconto_centimos = max(0, min(100_000_00, int(dados["desconto_centimos"])))
        except (TypeError, ValueError):
            return jsonify(erro="Desconto inválido."), 400

    observacoes = None
    if "observacoes" in dados:
        observacoes = str(dados["observacoes"] or "").strip()[:500]

    validade_dias = None
    if "validade_dias" in dados:
        try:
            validade_dias = max(1, min(90, int(dados["validade_dias"])))
        except (TypeError, ValueError):
            return jsonify(erro="Validade inválida (entre 1 e 90 dias)."), 400

    atualizar_campos_orcamento(orcamento["id"], desconto_centimos=desconto_centimos,
                                observacoes=observacoes, validade_dias=validade_dias)
    return jsonify(obter_orcamento_por_id(orcamento["id"])), 200


@app.route("/api/pedidos/<int:pedido_id>/orcamento/enviar", methods=["POST"])
@requer_autenticacao
def api_orcamento_enviar(pedido_id):
    if not obter_pedido_orcamento(pedido_id):
        return jsonify(erro="Pedido não encontrado"), 404
    orcamento = obter_orcamento_atual(pedido_id)
    # Impede envios duplicados: só há algo para enviar enquanto a versão mais
    # recente ainda estiver em rascunho.
    if not orcamento or orcamento["estado"] != "rascunho":
        return jsonify(erro="Não há nenhum rascunho de orçamento por enviar para este pedido."), 409
    if not orcamento["linhas"]:
        return jsonify(erro="Adicione pelo menos uma linha antes de enviar o orçamento."), 400
    marcar_orcamento_enviado(orcamento["id"])
    atualizar_estado_pedido(pedido_id, "orçamento enviado")
    enviar_orcamento_cliente(pedido_id)
    return jsonify(obter_orcamento_por_id(orcamento["id"])), 200


@app.route("/api/pedidos/<int:pedido_id>/recusar", methods=["POST"])
@requer_autenticacao
def api_pedido_recusar(pedido_id):
    pedido = obter_pedido_orcamento(pedido_id)
    if not pedido:
        return jsonify(erro="Pedido não encontrado"), 404
    if pedido["estado"] == "recusado":
        return jsonify(erro="Este pedido já tinha sido recusado."), 409
    recusar_pedido_e_avisar_cliente(pedido)
    return jsonify(obter_pedido_orcamento(pedido_id)), 200


@app.route("/media/<path:nome_ficheiro>", methods=["GET"])
@requer_autenticacao
def media(nome_ficheiro):
    return send_from_directory(MEDIA_DIR, nome_ficheiro)


@app.route("/dashboard", methods=["GET"])
@requer_autenticacao
def dashboard():
    return DASHBOARD_HTML


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
  .orc-tabela{width:100%;border-collapse:collapse;margin-top:8px;}
  .orc-tabela th,.orc-tabela td{padding:6px 4px;font-size:13px;border-bottom:1px solid var(--border);}
  .orc-tabela input,.orc-tabela textarea{background:var(--panel2);border:1px solid var(--border);color:var(--text);border-radius:6px;padding:5px 7px;font-size:13px;width:100%;}
  .orc-campo{margin-top:8px;}
  .orc-campo label{display:block;color:var(--muted);font-size:12px;margin-bottom:3px;}
  .orc-total{font-size:15px;margin-top:10px;}
  .orc-acoes{display:flex;gap:8px;flex-wrap:wrap;margin-top:14px;}
  .orc-acoes button,.orc-acoes a{cursor:pointer;border:none;border-radius:8px;padding:8px 12px;font-size:13px;font-weight:700;text-decoration:none;display:inline-block;}
  .btn-primario{background:var(--gold);color:#1a1400;}
  .btn-secundario{background:var(--panel2);color:var(--text);border:1px solid var(--border) !important;}
  .btn-perigo{background:#3a1a1a;color:#f2a3a3;}
  .orc-erro{color:#e05252;font-size:12.5px;margin-top:6px;}
  .orc-mini{background:transparent;border:1px solid var(--border);color:var(--muted);border-radius:6px;padding:3px 8px;font-size:12px;cursor:pointer;}
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
  return 'CHF ' + ((centimos||0)/100).toFixed(2);
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
      <td>${esc(d.nome || d.telefone)}<br><span style="color:var(--muted);font-size:12px;">${esc(d.telefone)}</span></td>
      <td><span class="tag">${esc(d.servico)}</span>${d.extra ? '<br><span style="color:var(--muted);font-size:12px;">+ '+esc(d.extra)+'</span>' : ''}</td>
      <td>${esc(d.data) || '-'}</td>
      <td>${esc(d.hora) || '-'}</td>
      <td>${d.preco ? 'CHF '+esc(d.preco) : '-'}</td>
      <td class="${classeEstado}">${esc(d.estado)}</td>
      <td style="color:var(--muted);">${esc(criado)}</td>
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
    carregarPedidos();
  }
}

async function pedidoRecusar(pedidoId){
  if(!confirm('Recusar este pedido e avisar o cliente?')) return;
  const dados = await orcPedirJson('/api/pedidos/' + pedidoId + '/recusar', {method: 'POST'});
  if(dados){
    document.getElementById('pedido-estado-atual').textContent = dados.estado;
    carregarPedidos();
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

carregar();
carregarPedidos();
setInterval(carregar, 20000);
setInterval(carregarPedidos, 20000);

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
    return jsonify(versao="v5.2-acoes-equipa", fluxos=["limpeza", "estetica", "wrap"],
                   idiomas=list(IDIOMAS_VALIDOS)), 200


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
    """Reinicia a sessão preservando o perfil (nome/idioma). Qualquer pedido
    de orçamento que tenha ficado em "rascunho" é arquivado aqui — este é o
    ponto por onde passam CANCELAR, MENU, HUMANO, esvaziar carrinho e
    recomeçar, pelo que nenhum pedido abandonado fica visível como novo."""
    sessao_antiga = carregar_sessao(de)
    arquivar_rascunho_wrap(sessao_antiga)
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
    if comando in COMANDOS_CARRINHO:
        mostrar_carrinho(de, idioma, sessao)
        return True
    if comando in COMANDOS_RAPIDO:
        mudar_para_modo_rapido(de, idioma, sessao)
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
    categoria = sessao.get("categoria")

    if fluxo == "wrap" and sessao.get("wrap_modo") == MODO_RAPIDO:
        # Cadeia do modo rápido: resumo -> fotografias -> interesse -> modo.
        if sessao.pop("_rapido_etapa_resumo", False):
            guardar_sessao(de, sessao); passo_rapido_fotos(de, idioma, sessao)
        elif sessao.get("aguardando_fotos"):
            sessao.pop("aguardando_fotos", None)
            guardar_sessao(de, sessao); passo_rapido_fotos(de, idioma, sessao)
        elif "rapido_interesse" in sessao:
            sessao.pop("rapido_interesse", None)
            guardar_sessao(de, sessao); passo_rapido_interesse(de, idioma, sessao)
        else:
            sessao.pop("wrap_modo", None); sessao.pop("preco_sob_analise", None)
            guardar_sessao(de, sessao); passo_wrap_modo(de, idioma, sessao)
        return

    if fluxo == "wrap":
        # Cadeia do mais recente para o mais antigo — sempre desfaz APENAS o
        # passo mais recente, preservando as escolhas anteriores.
        if sessao.pop("_wrap_etapa_resumo", False):
            guardar_sessao(de, sessao); passo_wrap_fotos_pergunta(de, idioma, sessao)
        elif sessao.get("aguardando_fotos"):
            sessao.pop("aguardando_fotos", None); guardar_sessao(de, sessao); passo_wrap_fotos_pergunta(de, idioma, sessao)
        elif "wrap_acabamento" in sessao:
            sessao.pop("wrap_acabamento", None); sessao.pop("wrap_acabamento_id", None)
            carrinho_remover_grupo(sessao, GRUPO_ACABAMENTO)
            guardar_sessao(de, sessao); passo_wrap_acabamento(de, idioma, sessao)
        elif sessao.pop("_wrap_aguardando_cor_texto", False):
            sessao.pop("wrap_cor_familia", None); sessao.pop("wrap_cor_familia_id", None)
            guardar_sessao(de, sessao); passo_wrap_cor_familia(de, idioma, sessao)
        elif "wrap_cor" in sessao:
            familia_id = sessao.get("wrap_cor_familia_id")
            sessao.pop("wrap_cor", None); sessao.pop("wrap_cor_id", None)
            carrinho_remover_grupo(sessao, GRUPO_WRAP_COR)
            if wrap_familia_tem_lista_propria(familia_id):
                guardar_sessao(de, sessao); passo_wrap_cor(de, idioma, sessao)
            else:
                sessao.pop("wrap_cor_familia", None); sessao.pop("wrap_cor_familia_id", None)
                guardar_sessao(de, sessao); passo_wrap_cor_familia(de, idioma, sessao)
        elif "wrap_cor_familia" in sessao:
            sessao.pop("wrap_cor_familia", None); sessao.pop("wrap_cor_familia_id", None)
            guardar_sessao(de, sessao); passo_wrap_cor_familia(de, idioma, sessao)
        elif "wrap_tipo" in sessao:
            sessao.pop("wrap_tipo", None)
            carrinho_remover_grupo(sessao, GRUPO_WRAP_TIPO)
            guardar_sessao(de, sessao); passo_wrap_tipo(de, idioma, sessao)
        elif sessao.pop("_wrap_aguardando_ano_texto", False):
            guardar_sessao(de, sessao); passo_wrap_ano(de, idioma, sessao)
        elif "wrap_ano" in sessao:
            sessao.pop("wrap_ano", None)
            guardar_sessao(de, sessao); passo_wrap_ano(de, idioma, sessao)
        elif sessao.pop("_wrap_aguardando_veiculo_texto", False):
            guardar_sessao(de, sessao); passo_wrap_veiculo(de, idioma, sessao)
        elif "wrap_categoria_veiculo" in sessao:
            sessao.pop("wrap_categoria_veiculo", None); sessao.pop("wrap_veiculo_id", None)
            carrinho_remover_grupo(sessao, GRUPO_WRAP_VEICULO)
            guardar_sessao(de, sessao); passo_wrap_veiculo(de, idioma, sessao)
        elif sessao.get("wrap_modo") == MODO_DETALHE:
            # Do 1.º passo, VOLTAR regressa à escolha do modo de pedido.
            sessao.pop("wrap_modo", None)
            guardar_sessao(de, sessao); passo_wrap_modo(de, idioma, sessao)
        else:
            nova = reiniciar_sessao(de); enviar_menu_principal(de, idioma, saudacao=False, sessao=nova)
        return

    if categoria in ("cat_limpeza", "cat_estetica"):
        if "hora" in sessao:
            sessao.pop("hora", None); guardar_sessao(de, sessao); passo_hora(de, idioma, sessao=sessao)
        elif "data" in sessao:
            sessao.pop("data", None); guardar_sessao(de, sessao); passo_data(de, idioma, sessao=sessao)
        elif "extra_id" in sessao:
            sessao.pop("extra_id", None)
            carrinho_remover_grupo(sessao, GRUPO_EXTRA)
            guardar_sessao(de, sessao)
            (passo_limpeza_extra if categoria == "cat_limpeza" else passo_estetica_extra)(de, idioma, sessao)
        elif categoria == "cat_limpeza" and "tamanho_id" in sessao:
            sessao.pop("tamanho_id", None)
            carrinho_remover_grupo(sessao, GRUPO_TAMANHO_VEICULO)
            guardar_sessao(de, sessao); passo_limpeza_tamanho(de, idioma, sessao)
        elif categoria == "cat_estetica" and "estado_id" in sessao:
            sessao.pop("estado_id", None)
            carrinho_remover_grupo(sessao, GRUPO_TAMANHO_VEICULO)
            guardar_sessao(de, sessao); passo_estetica_estado(de, idioma, sessao)
        elif "tipo_id" in sessao:
            sessao.pop("tipo_id", None); sessao.pop("categoria", None)
            iniciar_escolha_categoria(de, idioma, sessao)
        else:
            nova = reiniciar_sessao(de); enviar_menu_principal(de, idioma, saudacao=False, sessao=nova)
        return

    nova = reiniciar_sessao(de)
    enviar_menu_principal(de, idioma, saudacao=False, sessao=nova)


@app.route("/webhook", methods=["POST"])
def receber_mensagem():
    data = request.get_json(force=True)
    try:
        entry = data["entry"][0]["changes"][0]["value"]
        if "messages" not in entry:
            return jsonify(status="ignorado"), 200

        msg = entry["messages"][0]
        de = msg["from"]

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
            if id_interativo.startswith(("pedido_analisar_", "pedido_contactar_", "pedido_recusar_")) \
                    and numero_e_da_equipa(de):
                processar_resposta_interna_pedido(id_interativo)
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

            # --- Orçamento: descrição livre de "Outra alteração" -------------
            if sessao.get("_aguardando_alteracao_orcamento_id"):
                orcamento_id = sessao.pop("_aguardando_alteracao_orcamento_id")
                guardar_sessao(de, sessao)
                registar_pedido_alteracao(de, idioma, orcamento_id, sessao, "outra", texto_livre=msg["text"]["body"].strip())
                return jsonify(status="ok"), 200

            # --- Wrap & Proteção: únicos 3 pontos com texto livre ------------
            if sessao.get("fluxo") == "wrap" and sessao.get("_wrap_aguardando_veiculo_texto"):
                nome_livre = msg["text"]["body"].strip()
                sessao.pop("_wrap_aguardando_veiculo_texto", None)
                sessao["wrap_categoria_veiculo"] = nome_livre
                carrinho_definir_wrap_veiculo(sessao, "wv_outro_livre", nome_pt_livre=nome_livre)
                guardar_sessao(de, sessao)
                passo_wrap_ano(de, idioma, sessao)
                return jsonify(status="ok"), 200

            if sessao.get("fluxo") == "wrap" and sessao.get("_wrap_aguardando_ano_texto"):
                ano = ano_veiculo_valido(msg["text"]["body"])
                if not ano:
                    enviar_texto(de, t("wrap_ano_invalido", idioma))
                    return jsonify(status="ok"), 200
                sessao.pop("_wrap_aguardando_ano_texto", None)
                sessao["wrap_ano"] = ano
                guardar_sessao(de, sessao)
                passo_wrap_tipo(de, idioma, sessao)
                return jsonify(status="ok"), 200

            if sessao.get("fluxo") == "wrap" and sessao.get("_wrap_aguardando_cor_texto"):
                cor_livre = msg["text"]["body"].strip()
                sessao.pop("_wrap_aguardando_cor_texto", None)
                sessao["wrap_cor"] = cor_livre
                carrinho_definir_wrap_cor(sessao, "cor_personalizada_livre", cor_livre)
                guardar_sessao(de, sessao)
                passo_wrap_acabamento(de, idioma, sessao)
                return jsonify(status="ok"), 200

            if sessao.get("fluxo") == "wrap" and sessao.get("aguardando_fotos"):
                if texto == "concluir":
                    sessao.pop("aguardando_fotos", None)
                    avancar_para_resumo_wrap(de, idioma, sessao)
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
                botoes_retomar = [
                    {"id": "retomar_continuar", "titulo": t("botao_continuar", idioma)},
                    {"id": "retomar_recomecar", "titulo": t("botao_recomecar", idioma)},
                ]
                if sessao.get("carrinho"):
                    botoes_retomar.append({"id": "ver_carrinho", "titulo": t("carrinho_botao_ver", idioma)})
                enviar_botoes(de, t("retomar_pergunta", idioma), botoes_retomar, idioma)
                return jsonify(status="ok"), 200

            # primeira mensagem / sem sessão em curso -> menu principal
            enviar_menu_principal(de, idioma, saudacao=True, sessao=sessao)
            return jsonify(status="ok"), 200

        # --- Botões -----------------------------------------------------
        if tipo == "interactive" and msg["interactive"]["type"] == "button_reply":
            id_botao = msg["interactive"]["button_reply"]["id"]

            # --- Aliases dos IDs canónicos "acao_*" (ver constantes ACAO_*) -
            # Botões NOVOS usam sempre estes IDs; os antigos equivalentes
            # (ex.: "menu_principal", "mp_marcar") continuam a funcionar tal e
            # qual, sem qualquer alteração — nunca foram removidos, só deixou
            # de se criar botões novos com eles.
            id_botao = {
                ACAO_MENU: "menu_principal",
                ACAO_NOVA_MARCACAO: "mp_marcar",
                ACAO_CARRINHO: "ver_carrinho",
                ACAO_CANCELAR: ID_CANCELAR,
                ACAO_RAPIDO: "modo_rapido",
            }.get(id_botao, id_botao)

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
                enviar_seletor_idioma(de)
                return jsonify(status="ok"), 200

            if id_botao == ACAO_MAIS:
                mostrar_mais_acoes(de, idioma, sessao)
                return jsonify(status="ok"), 200

            if id_botao in LANG_IDS:  # "Alterar idioma" com sessão já ativa
                # Limpa os campos do processo em curso (categoria, passos já
                # escolhidos, etc.) e preserva só o nome — para dados antigos
                # nunca fazerem o bot saltar etapas depois de mudar de idioma.
                # Um rascunho de pedido Wrap fica arquivado, para não sobrar
                # no painel como se fosse um pedido novo.
                novo_idioma = LANG_IDS[id_botao]
                arquivar_rascunho_wrap(sessao)
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
                iniciar_escolha_categoria(de, idioma, sessao)
                return jsonify(status="ok"), 200

            if id_botao in NOME_CATEGORIA:  # categoria dentro de "Marcar"
                if id_botao == "cat_wrap":
                    # Entrada do Wrap: primeiro pergunta-se COMO avançar.
                    sessao.update({"fluxo": "wrap", "categoria": "cat_wrap"})
                    guardar_sessao(de, sessao)
                    passo_wrap_modo(de, idioma, sessao)
                else:
                    sessao.update({"fluxo": "marcar", "categoria": id_botao})
                    guardar_sessao(de, sessao)
                    (passo_limpeza_tipo if id_botao == "cat_limpeza" else passo_estetica_servico)(de, idioma, sessao)
                return jsonify(status="ok"), 200

            # --- Escolha do modo de pedido Wrap ---------------------------
            if id_botao == "modo_rapido":
                mudar_para_modo_rapido(de, idioma, sessao)
                return jsonify(status="ok"), 200

            if id_botao == "modo_detalhe":
                sessao.update({"fluxo": "wrap", "categoria": "cat_wrap", "wrap_modo": MODO_DETALHE})
                sessao.pop("preco_sob_analise", None)
                guardar_sessao(de, sessao)
                passo_wrap_veiculo(de, idioma, sessao)
                return jsonify(status="ok"), 200

            if id_botao == "modo_especialista":
                sessao.update({"fluxo": "wrap", "categoria": "cat_wrap"})
                pedido_falar_especialista(de, idioma, sessao)
                return jsonify(status="ok"), 200

            # --- Orçamento rápido: interesse declarado ---------------------
            if id_botao in ("rapido_wrap_total", "rapido_wrap_parcial", "rapido_nao_sei"):
                interesse = {"rapido_wrap_total": "wrap_total",
                             "rapido_wrap_parcial": "wrap_parcial",
                             "rapido_nao_sei": "wrap_nao_sei"}[id_botao]
                sessao["rapido_interesse"] = interesse
                sessao["wrap_modo"] = MODO_RAPIDO
                sessao["preco_sob_analise"] = True
                guardar_sessao(de, sessao)
                passo_rapido_fotos(de, idioma, sessao)
                return jsonify(status="ok"), 200

            if id_botao == "wrap_fotos_sim":
                _garantir_pedido_wrap(de, sessao)
                sessao["aguardando_fotos"] = True
                guardar_sessao(de, sessao)
                enviar_texto(de, t("wrap_fotos_pedir", idioma))
                return jsonify(status="ok"), 200

            if id_botao in ("wrap_fotos_nao", "wrap_fotos_concluir"):
                _garantir_pedido_wrap(de, sessao)
                sessao.pop("aguardando_fotos", None)
                avancar_para_resumo_wrap(de, idioma, sessao)
                return jsonify(status="ok"), 200

            if id_botao == "wrap_confirmar":
                pedido_id = sessao.get("pedido_id")
                finalizar_pedido_wrap(de, idioma, sessao, pedido_id)
                reiniciar_sessao(de)
                return jsonify(status="ok"), 200

            if id_botao == "wrap_alterar":
                _wrap_limpar_escolhas(sessao)
                guardar_sessao(de, sessao)
                passo_wrap_veiculo(de, idioma, sessao)
                return jsonify(status="ok"), 200

            # --- Orçamento rápido: confirmar / alterar --------------------
            if id_botao == "rapido_confirmar":
                pedido_id = sessao.get("pedido_id")
                finalizar_pedido_rapido(de, idioma, sessao, pedido_id)
                reiniciar_sessao(de)
                return jsonify(status="ok"), 200

            if id_botao == "rapido_alterar":
                # Preserva pedido_id e fotografias: só a escolha é refeita.
                sessao.pop("rapido_interesse", None)
                sessao.pop("_rapido_etapa_resumo", None)
                guardar_sessao(de, sessao)
                passo_rapido_interesse(de, idioma, sessao)
                return jsonify(status="ok"), 200

            if id_botao == "ver_carrinho":
                mostrar_carrinho(de, idioma, sessao)
                return jsonify(status="ok"), 200

            if id_botao == "carrinho_continuar":
                reenviar_passo_atual(de, idioma, sessao)
                return jsonify(status="ok"), 200

            if id_botao == "carrinho_alterar":
                mostrar_alterar_carrinho(de, idioma, sessao)
                return jsonify(status="ok"), 200

            if id_botao == "carrinho_esvaziar":
                nova = reiniciar_sessao(de)
                enviar_texto(de, t("carrinho_esvaziado", idioma))
                enviar_menu_principal(de, idioma, saudacao=False, sessao=nova)
                return jsonify(status="ok"), 200

            if id_botao == "confirmar":
                id_ag = guardar_agendamento(de, sessao)
                enviar_texto(de, mensagem_confirmacao_final(sessao, idioma))
                # Em vez de mandar escrever comandos no próprio texto: botões.
                enviar_botoes(de, t("e_agora_pergunta", idioma), [
                    {"id": ACAO_GERIR, "titulo": t("botao_gerir_marcacao", idioma)},
                    {"id": ACAO_MENU, "titulo": t("botao_menu_principal", idioma)},
                ], idioma)
                enviar_notificacao_interna_marcacao(id_ag, mensagem_notificacao_provider(de, sessao, id_ag))
                reiniciar_sessao(de)
                return jsonify(status="ok"), 200

            if id_botao == "alterar":
                categoria = sessao.get("categoria")
                for campo in ("tipo_id", "tamanho_id", "estado_id", "extra_id", "data", "hora",
                              "servico", "extra", "preco", "duracao"):
                    sessao.pop(campo, None)
                carrinho_remover_grupo(sessao, GRUPO_SERVICO_BASE)
                carrinho_remover_grupo(sessao, GRUPO_TAMANHO_VEICULO)
                carrinho_remover_grupo(sessao, GRUPO_EXTRA)
                guardar_sessao(de, sessao)
                (passo_limpeza_tipo if categoria == "cat_limpeza" else passo_estetica_servico)(de, idioma, sessao)
                return jsonify(status="ok"), 200

            # --- Marcação confirmada aberta a partir do carrinho -------------
            if id_botao.startswith("gerir_ag_"):
                mostrar_gestao_marcacao(de, idioma, int(id_botao.split("_")[-1]))
                return jsonify(status="ok"), 200

            if id_botao.startswith("carrinho_marcacao_"):
                abrir_marcacao_do_carrinho(de, idioma, int(id_botao.split("_")[-1]))
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

            # --- Orçamento: resposta do cliente (aceitar/alterar/recusar) ---
            # A ordem importa: os sufixos "_sim_"/"_nao_" da confirmação de
            # recusa têm de ser verificados ANTES do prefixo genérico
            # "orcamento_recusar_", que também lhes serve de prefixo.
            if id_botao.startswith("orcamento_recusar_sim_"):
                responder_orcamento_recusar_efetivar(de, idioma, int(id_botao.split("_")[-1]))
                return jsonify(status="ok"), 200

            if id_botao.startswith("orcamento_recusar_nao_"):
                orcamento_id = int(id_botao.split("_")[-1])
                orcamento, pedido = _orcamento_e_pedido_de(orcamento_id)
                if orcamento and pedido:
                    enviar_orcamento_cliente(pedido["id"])
                return jsonify(status="ok"), 200

            if id_botao.startswith("orcamento_aceitar_"):
                responder_orcamento_aceitar(de, idioma, int(id_botao.split("_")[-1]))
                return jsonify(status="ok"), 200

            if id_botao.startswith("orcamento_alterar_"):
                mostrar_lista_alteracao_orcamento(de, idioma, int(id_botao.split("_")[-1]))
                return jsonify(status="ok"), 200

            if id_botao.startswith("orcamento_recusar_"):
                responder_orcamento_recusar_confirmar(de, idioma, int(id_botao.split("_")[-1]))
                return jsonify(status="ok"), 200

            # Botão de resposta rápida do template Utility de reabertura de
            # janela (ver enviar_orcamento_via_template) — reabre a janela de
            # 24h e dispara o envio da mensagem interativa completa.
            if id_botao.startswith("ver_orcamento_"):
                enviar_orcamento_cliente(int(id_botao[len("ver_orcamento_"):]))
                return jsonify(status="ok"), 200

            # --- Pedido pendente: cancelamento pelo próprio cliente ---------
            # Mesma ordem cuidadosa: "_sim_"/"_nao_" antes do prefixo genérico.
            if id_botao.startswith("pedido_cancelar_cliente_sim_"):
                pedido_cliente_cancelar_efetivar(de, idioma, int(id_botao.split("_")[-1]))
                return jsonify(status="ok"), 200

            if id_botao.startswith("pedido_cancelar_cliente_nao_"):
                mostrar_carrinho(de, idioma, sessao)
                return jsonify(status="ok"), 200

            if id_botao.startswith("pedido_cancelar_cliente_"):
                pedido_cliente_cancelar_confirmar(de, idioma, int(id_botao.split("_")[-1]))
                return jsonify(status="ok"), 200

            nao_entendi_com_opcoes(de, idioma, sessao)
            return jsonify(status="ok"), 200

        # --- Listas -------------------------------------------------------
        if tipo == "interactive" and msg["interactive"]["type"] == "list_reply":
            id_escolhido = msg["interactive"]["list_reply"]["id"]

            # --- Aliases dos IDs canónicos "acao_*" — ver o mesmo bloco no
            # dispatch de botões, acima, para a explicação completa.
            id_escolhido = {
                ACAO_MENU: "menu_principal",
                ACAO_NOVA_MARCACAO: "mp_marcar",
                ACAO_CARRINHO: "ver_carrinho",
                ACAO_CANCELAR: ID_CANCELAR,
                ACAO_VOLTAR: ID_VOLTAR,
                ACAO_RAPIDO: "modo_rapido",
            }.get(id_escolhido, id_escolhido)

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
                enviar_seletor_idioma(de)
                return jsonify(status="ok"), 200

            if id_escolhido == ACAO_MAIS:
                mostrar_mais_acoes(de, idioma, sessao)
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

            if id_escolhido == "ver_carrinho":
                mostrar_carrinho(de, idioma, sessao)
                return jsonify(status="ok"), 200

            # Atalho "⚡ Pedido rápido" nas listas do fluxo Wrap detalhado
            if id_escolhido == "modo_rapido":
                mudar_para_modo_rapido(de, idioma, sessao)
                return jsonify(status="ok"), 200

            if id_escolhido.startswith("carrinho_item_"):
                item_id = id_escolhido[len("carrinho_item_"):]
                linha_item = next((l for l in sessao.get("carrinho", []) if l["id"] == item_id), None)
                if not linha_item:
                    nao_entendi_com_opcoes(de, idioma, sessao)
                    return jsonify(status="ok"), 200
                if linha_item["grupo"] in GRUPOS_REMOVIVEIS:
                    carrinho_remover_item(sessao, item_id)
                    guardar_sessao(de, sessao)
                    enviar_texto(de, t("carrinho_item_removido", idioma))
                    mostrar_carrinho(de, idioma, sessao)
                else:
                    _reabrir_passo_para_grupo(de, idioma, sessao, linha_item["grupo"])
                return jsonify(status="ok"), 200

            # --- Marcação confirmada escolhida na lista do carrinho ----------
            if id_escolhido.startswith("carrinho_marcacao_"):
                abrir_marcacao_do_carrinho(de, idioma, int(id_escolhido.split("_")[-1]))
                return jsonify(status="ok"), 200

            if id_escolhido.startswith("gerir_ag_"):
                mostrar_gestao_marcacao(de, idioma, int(id_escolhido.split("_")[-1]))
                return jsonify(status="ok"), 200

            # --- Orçamento: lista de aspetos a alterar -----------------------
            if id_escolhido.startswith("orcamento_alt_"):
                resto = id_escolhido[len("orcamento_alt_"):]
                aspeto, _, orcamento_id_txt = resto.rpartition("_")
                try:
                    orcamento_id = int(orcamento_id_txt)
                except ValueError:
                    nao_entendi_com_opcoes(de, idioma, sessao)
                    return jsonify(status="ok"), 200
                if aspeto == "outra":
                    sessao["_aguardando_alteracao_orcamento_id"] = orcamento_id
                    guardar_sessao(de, sessao)
                    enviar_texto(de, t("alteracao_outra_pedir", idioma))
                else:
                    registar_pedido_alteracao(de, idioma, orcamento_id, sessao, aspeto)
                return jsonify(status="ok"), 200

            # --- Wrap & Proteção: passos 1, 2, 3, 4, 5 e 6 (todos por lista) ---
            # Só no modo detalhado — o modo rápido não tem listas próprias.
            if sessao.get("fluxo") == "wrap" and sessao.get("wrap_modo") != MODO_RAPIDO:
                # Passo 1 — tipo de veículo
                if "wrap_categoria_veiculo" not in sessao and encontrar_opcao(WRAP_TIPOS_VEICULO, id_escolhido):
                    if id_escolhido == "wv_outro":
                        sessao["_wrap_aguardando_veiculo_texto"] = True
                        guardar_sessao(de, sessao)
                        passo_wrap_veiculo_outro(de, idioma)
                    else:
                        opcao = encontrar_opcao(WRAP_TIPOS_VEICULO, id_escolhido)
                        sessao["wrap_veiculo_id"] = id_escolhido
                        sessao["wrap_categoria_veiculo"] = _remover_emoji_prefixo(tx(opcao["titulo"], "pt"))
                        carrinho_definir_wrap_veiculo(sessao, id_escolhido)
                        guardar_sessao(de, sessao)
                        passo_wrap_ano(de, idioma, sessao)
                    return jsonify(status="ok"), 200

                # Passo 2 — ano
                if "wrap_categoria_veiculo" in sessao and "wrap_ano" not in sessao \
                        and (id_escolhido.startswith("wrap_ano_")):
                    if id_escolhido == "wrap_ano_outro":
                        sessao["_wrap_aguardando_ano_texto"] = True
                        guardar_sessao(de, sessao)
                        passo_wrap_ano_outro(de, idioma)
                    else:
                        sessao["wrap_ano"] = id_escolhido[len("wrap_ano_"):]
                        guardar_sessao(de, sessao)
                        passo_wrap_tipo(de, idioma, sessao)
                    return jsonify(status="ok"), 200

                # Passo 3 — wrap total/parcial
                if "wrap_ano" in sessao and "wrap_tipo" not in sessao and id_escolhido in ("wrap_total", "wrap_parcial"):
                    sessao["wrap_tipo"] = id_escolhido
                    carrinho_definir_wrap_tipo(sessao, id_escolhido)
                    guardar_sessao(de, sessao)
                    passo_wrap_cor_familia(de, idioma, sessao)
                    return jsonify(status="ok"), 200

                # Passo 4 — família de cor
                if "wrap_tipo" in sessao and "wrap_cor_familia" not in sessao \
                        and encontrar_opcao(WRAP_FAMILIAS_COR, id_escolhido):
                    opcao = encontrar_opcao(WRAP_FAMILIAS_COR, id_escolhido)
                    sessao["wrap_cor_familia_id"] = id_escolhido
                    sessao["wrap_cor_familia"] = _remover_emoji_prefixo(tx(opcao["titulo"], "pt"))
                    if id_escolhido == "cf_transparente":
                        sessao["wrap_cor"] = WRAP_COR_TRANSPARENTE_NOME["pt"]
                        carrinho_definir_wrap_cor(sessao, "cor_transparente_ppf", WRAP_COR_TRANSPARENTE_NOME["pt"])
                        guardar_sessao(de, sessao)
                        passo_wrap_acabamento(de, idioma, sessao)
                    elif id_escolhido == "cf_personalizada":
                        sessao["_wrap_aguardando_cor_texto"] = True
                        guardar_sessao(de, sessao)
                        passo_wrap_cor_personalizada(de, idioma)
                    else:
                        guardar_sessao(de, sessao)
                        passo_wrap_cor(de, idioma, sessao)
                    return jsonify(status="ok"), 200

                # Passo 5 — cor (dentro da família escolhida)
                if "wrap_cor_familia" in sessao and "wrap_cor" not in sessao:
                    cores_familia = WRAP_CORES_POR_FAMILIA.get(sessao.get("wrap_cor_familia_id"), [])
                    opcao = encontrar_opcao(cores_familia, id_escolhido)
                    if opcao:
                        sessao["wrap_cor_id"] = id_escolhido
                        sessao["wrap_cor"] = tx(opcao["titulo"], "pt")
                        carrinho_definir_wrap_cor(sessao, id_escolhido, sessao["wrap_cor"])
                        guardar_sessao(de, sessao)
                        passo_wrap_acabamento(de, idioma, sessao)
                        return jsonify(status="ok"), 200

                # Passo 6 — acabamento
                if "wrap_cor" in sessao and "wrap_acabamento" not in sessao \
                        and encontrar_opcao(WRAP_ACABAMENTOS, id_escolhido):
                    opcao = encontrar_opcao(WRAP_ACABAMENTOS, id_escolhido)
                    sessao["wrap_acabamento_id"] = id_escolhido
                    sessao["wrap_acabamento"] = _remover_emoji_prefixo(tx(opcao["titulo"], "pt"))
                    carrinho_definir_wrap_acabamento(sessao, id_escolhido)
                    _garantir_pedido_wrap(de, sessao)
                    guardar_sessao(de, sessao)
                    passo_wrap_fotos_pergunta(de, idioma, sessao)
                    return jsonify(status="ok"), 200

                nao_entendi_com_opcoes(de, idioma, sessao)
                return jsonify(status="ok"), 200

            categoria = sessao.get("categoria")

            # Limpeza
            if categoria == "cat_limpeza":
                if "tipo_id" not in sessao:
                    sessao["tipo_id"] = id_escolhido
                    carrinho_definir_servico_base(sessao, LIMPEZA_TIPOS, id_escolhido)
                    guardar_sessao(de, sessao); passo_limpeza_tamanho(de, idioma, sessao)
                elif "tamanho_id" not in sessao:
                    sessao["tamanho_id"] = id_escolhido
                    carrinho_definir_modificador_veiculo(sessao, TAMANHOS_VEICULO, id_escolhido)
                    guardar_sessao(de, sessao); passo_limpeza_extra(de, idioma, sessao)
                elif "extra_id" not in sessao:
                    sessao["extra_id"] = id_escolhido
                    carrinho_definir_extra(sessao, EXTRAS_LIMPEZA, id_escolhido)
                    guardar_sessao(de, sessao); passo_data(de, idioma, sessao=sessao)
                elif "data" not in sessao:
                    sessao["data"] = msg["interactive"]["list_reply"]["title"]; guardar_sessao(de, sessao); passo_hora(de, idioma, sessao=sessao)
                elif "hora" not in sessao:
                    sessao["hora"] = msg["interactive"]["list_reply"]["title"]; guardar_sessao(de, sessao)
                    passo_resumo(de, idioma, sessao)
                return jsonify(status="ok"), 200

            # Estética
            if categoria == "cat_estetica":
                if "tipo_id" not in sessao:
                    sessao["tipo_id"] = id_escolhido
                    carrinho_definir_servico_base(sessao, ESTETICA_SERVICOS, id_escolhido)
                    guardar_sessao(de, sessao); passo_estetica_estado(de, idioma, sessao)
                elif "estado_id" not in sessao:
                    sessao["estado_id"] = id_escolhido
                    carrinho_definir_modificador_veiculo(sessao, ESTADO_VEICULO, id_escolhido)
                    guardar_sessao(de, sessao); passo_estetica_extra(de, idioma, sessao)
                elif "extra_id" not in sessao:
                    sessao["extra_id"] = id_escolhido
                    carrinho_definir_extra(sessao, EXTRAS_ESTETICA, id_escolhido)
                    guardar_sessao(de, sessao); passo_data(de, idioma, sessao=sessao)
                elif "data" not in sessao:
                    sessao["data"] = msg["interactive"]["list_reply"]["title"]; guardar_sessao(de, sessao); passo_hora(de, idioma, sessao=sessao)
                elif "hora" not in sessao:
                    sessao["hora"] = msg["interactive"]["list_reply"]["title"]; guardar_sessao(de, sessao)
                    passo_resumo(de, idioma, sessao)
                return jsonify(status="ok"), 200

            nao_entendi_com_opcoes(de, idioma, sessao)
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
                sessao.pop("aguardando_fotos", None)
                avancar_para_resumo_wrap(de, idioma, sessao)
            else:
                enviar_texto(de, t("wrap_foto_recebida_contagem", idioma, atual=total_fotos, total=5))
                botao_ver = ("rapido_ver_pedido_botao" if sessao.get("wrap_modo") == MODO_RAPIDO
                             else "carrinho_botao_ver")
                enviar_botoes(de, t("wrap_fotos_mais_ou_concluir", idioma), [
                    {"id": "wrap_fotos_concluir", "titulo": t("wrap_fotos_concluir_botao", idioma)},
                    {"id": "ver_carrinho", "titulo": t(botao_ver, idioma)},
                ], idioma)
            return jsonify(status="ok"), 200

        # --- Qualquer outro tipo (áudio, imagem fora de contexto, sticker, etc.) ---
        nao_entendi_com_opcoes(de, idioma, sessao)

    except (KeyError, IndexError):
        pass  # notificações de status (entregue/lido) chegam neste mesmo endpoint — ignora-as

    return jsonify(status="ok"), 200


def reenviar_passo_atual(de, idioma, sessao):
    """Reenvia o ecrã correspondente ao ponto exato onde a sessão ficou."""
    categoria = sessao.get("categoria")
    fluxo = sessao.get("fluxo")

    if fluxo == "wrap" and sessao.get("wrap_modo") == MODO_RAPIDO:
        if sessao.get("_rapido_etapa_resumo"):
            passo_rapido_resumo(de, idioma, sessao)
        elif sessao.get("aguardando_fotos"):
            enviar_texto(de, t("wrap_fotos_pedir", idioma))
        elif "rapido_interesse" in sessao:
            passo_rapido_fotos(de, idioma, sessao)
        else:
            passo_rapido_interesse(de, idioma, sessao)
        return

    if fluxo == "wrap":
        if sessao.get("_wrap_etapa_resumo"):
            passo_wrap_resumo(de, idioma, sessao)
        elif sessao.get("aguardando_fotos"):
            enviar_texto(de, t("wrap_fotos_pedir", idioma))
        elif "wrap_acabamento" in sessao:
            passo_wrap_fotos_pergunta(de, idioma, sessao)
        elif sessao.get("_wrap_aguardando_cor_texto"):
            passo_wrap_cor_personalizada(de, idioma)
        elif "wrap_cor" in sessao:
            passo_wrap_acabamento(de, idioma, sessao)
        elif "wrap_cor_familia" in sessao:
            passo_wrap_cor(de, idioma, sessao)
        elif "wrap_tipo" in sessao:
            passo_wrap_cor_familia(de, idioma, sessao)
        elif sessao.get("_wrap_aguardando_ano_texto"):
            passo_wrap_ano_outro(de, idioma)
        elif "wrap_ano" in sessao:
            passo_wrap_tipo(de, idioma, sessao)
        elif sessao.get("_wrap_aguardando_veiculo_texto"):
            passo_wrap_veiculo_outro(de, idioma)
        elif "wrap_categoria_veiculo" in sessao:
            passo_wrap_ano(de, idioma, sessao)
        elif sessao.get("wrap_modo") == MODO_DETALHE:
            passo_wrap_veiculo(de, idioma, sessao)
        else:
            passo_wrap_modo(de, idioma, sessao)
        return

    if categoria == "cat_limpeza":
        if "hora" in sessao:
            passo_resumo(de, idioma, sessao)
        elif "data" in sessao:
            passo_hora(de, idioma, sessao=sessao)
        elif "extra_id" in sessao:
            passo_data(de, idioma, sessao=sessao)
        elif "tamanho_id" in sessao:
            passo_limpeza_extra(de, idioma, sessao)
        elif "tipo_id" in sessao:
            passo_limpeza_tamanho(de, idioma, sessao)
        else:
            passo_limpeza_tipo(de, idioma, sessao)
        return

    if categoria == "cat_estetica":
        if "hora" in sessao:
            passo_resumo(de, idioma, sessao)
        elif "data" in sessao:
            passo_hora(de, idioma, sessao=sessao)
        elif "extra_id" in sessao:
            passo_data(de, idioma, sessao=sessao)
        elif "estado_id" in sessao:
            passo_estetica_extra(de, idioma, sessao)
        elif "tipo_id" in sessao:
            passo_estetica_estado(de, idioma, sessao)
        else:
            passo_estetica_servico(de, idioma, sessao)
        return

    enviar_menu_principal(de, idioma, saudacao=False, sessao=sessao)


if __name__ == "__main__":
    porta = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=porta, debug=True)
