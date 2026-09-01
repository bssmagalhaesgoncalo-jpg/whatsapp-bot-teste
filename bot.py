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
import unicodedata
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

# ---------------------------------------------------------------------------
# IDENTIDADE DO NEGÓCIO — configurável por ambiente, nunca escrita à mão nas
# mensagens. Trocar estas duas variáveis muda o nome e a morada em todo o
# lado: saudação, resumo, confirmação, notificações e painel.
# ---------------------------------------------------------------------------
BUSINESS_NAME = os.environ.get("BUSINESS_NAME", "Daniela Nails (TESTE)")
BUSINESS_ADDRESS = os.environ.get("BUSINESS_ADDRESS", "Visp, Switzerland")

# Nomes antigos mantidos como aliases: há código e testes que os usam, e
# renomeá-los não traria nada — apontam para a mesma identidade.
NOME_OFICINA = BUSINESS_NAME
MORADA_OFICINA = BUSINESS_ADDRESS

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
    "limpeza_tipo_corpo": {"pt": "Passo 1 de 5 — Que serviço deseja para as mãos?",
                            "de": "Schritt 1 von 5 — Welchen Service möchten Sie für die Hände?",
                            "en": "Step 1 of 5 — Which service would you like for your hands?"},
    "limpeza_tipo_seccao": {"pt": "Serviços de mãos", "de": "Handservices", "en": "Hand services"},
    "limpeza_tipo_botao": {"pt": "💅 Escolher", "de": "💅 Wählen", "en": "💅 Choose"},

    "limpeza_tamanho_corpo": {"pt": "Passo 2 de 5 — Que comprimento deseja para as unhas?",
                               "de": "Schritt 2 von 5 — Welche Nagellänge möchten Sie?",
                               "en": "Step 2 of 5 — What nail length would you like?"},
    "tamanho_seccao": {"pt": "Comprimento das unhas", "de": "Nagellänge", "en": "Nail length"},
    "tamanho_botao": {"pt": "📏 Escolher", "de": "📏 Wählen", "en": "📏 Choose"},

    "extra_corpo": {"pt": "Passo 3 de 5 — Deseja acrescentar algum extra?",
                    "de": "Schritt 3 von 5 — Möchten Sie ein Extra dazunehmen?",
                    "en": "Step 3 of 5 — Would you like to add any extra?"},
    "extra_seccao": {"pt": "Extras disponíveis", "de": "Verfügbare Extras", "en": "Available extras"},
    "extra_botao": {"pt": "➕ Escolher", "de": "➕ Wählen", "en": "➕ Choose"},

    # --- Passos: Estética -----------------------------------------------
    "estetica_servico_corpo": {"pt": "Passo 1 de 5 — Que serviço deseja para os pés?",
                                "de": "Schritt 1 von 5 — Welchen Service möchten Sie für die Füsse?",
                                "en": "Step 1 of 5 — Which service would you like for your feet?"},
    "estetica_servico_seccao": {"pt": "Serviços de pés", "de": "Fussservices", "en": "Foot services"},
    "estetica_servico_botao": {"pt": "🦶 Escolher", "de": "🦶 Wählen", "en": "🦶 Choose"},

    "estetica_estado_corpo": {"pt": "Passo 2 de 5 — É necessário remover produto das unhas?",
                               "de": "Schritt 2 von 5 — Muss Produkt von den Nägeln entfernt werden?",
                               "en": "Step 2 of 5 — Does any product need removing from the nails?"},
    "estado_seccao": {"pt": "Remoção de produto", "de": "Produktentfernung", "en": "Product removal"},
    "estado_botao": {"pt": "🧴 Escolher", "de": "🧴 Wählen", "en": "🧴 Choose"},

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
    # Rótulos do RESUMO: respostas diretas à pergunta "confirmamos?", em três
    # botões visíveis de imediato (ver passo_resumo).
    "botao_resumo_sim": {"pt": "✅ Sim, confirmar", "de": "✅ Ja, bestätigen", "en": "✅ Yes, confirm"},
    "botao_resumo_nao": {"pt": "✏️ Não, alterar", "de": "✏️ Nein, ändern", "en": "✏️ No, change"},
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
    "confirmado_instrucao": {"pt": "Por favor, chegue aproximadamente 5 minutos antes da sua marcação.",
                              "de": "Bitte kommen Sie ungefähr 5 Minuten vor Ihrem Termin an.",
                              "en": "Please arrive approximately 5 minutes before your appointment."},
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

    # --- Preços visíveis nas opções + navegação visual -----------------------
    "preco_desde": {"pt": "desde {preco}", "de": "ab {preco}", "en": "from {preco}"},
    "preco_estimado": {"pt": "estimado {preco}", "de": "geschätzt {preco}", "en": "estimated {preco}"},
    "preco_estimativa_desde": {"pt": "estimativa desde {preco}", "de": "Schätzung ab {preco}",
                                "en": "estimate from {preco}"},
    "preco_incluido": {"pt": "Incluído", "de": "Inbegriffen", "en": "Included"},
    "preco_sob_analise_curto": {"pt": "sob análise", "de": "wird geprüft", "en": "under review"},
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
    "acoes_seccao": {"pt": "Ações", "de": "Aktionen", "en": "Actions"},
    "wrap_modo_seccao": {"pt": "Como avançar", "de": "Wie fortfahren", "en": "How to proceed"},
    "wrap_fotos_seccao": {"pt": "Fotografias", "de": "Fotos", "en": "Photos"},
    "carrinho_seccao": {"pt": "Carrinho", "de": "Warenkorb", "en": "Cart"},
    "gerir_seccao": {"pt": "A sua marcação", "de": "Ihre Buchung", "en": "Your booking"},
    "categoria_seccao": {"pt": "Categorias", "de": "Kategorien", "en": "Categories"},
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
    {"id": "lp_gellack", "preco": 55,
     "titulo": {"pt": "Gellack mãos", "de": "Gellack Hände", "en": "Gel polish hands"},
     "descricao": {"pt": "Verniz em gel de longa duração",
                   "de": "Langanhaltender Gellack",
                   "en": "Long-lasting gel polish"},
     "duracao": {"pt": "1h", "de": "1h", "en": "1h"}},
    {"id": "lp_gel", "preco": 90,
     "titulo": {"pt": "Aplicação de gel", "de": "Gelmodellage", "en": "Gel application"},
     "descricao": {"pt": "Construção completa em gel",
                   "de": "Vollständiger Aufbau in Gel",
                   "en": "Full gel build-up"},
     "duracao": {"pt": "1h45", "de": "1h45", "en": "1h 45"}},
    {"id": "lp_reenchimento", "preco": 70,
     "titulo": {"pt": "Reenchimento", "de": "Auffüllen", "en": "Refill"},
     "descricao": {"pt": "Manutenção do gel já aplicado",
                   "de": "Auffrischung des vorhandenen Gels",
                   "en": "Maintenance of existing gel"},
     "duracao": {"pt": "1h30", "de": "1h30", "en": "1h 30"}},
    {"id": "lp_classica", "preco": 40,
     "titulo": {"pt": "Manicure clássica", "de": "Klassische Maniküre", "en": "Classic manicure"},
     "descricao": {"pt": "Corte, lima e cutículas",
                   "de": "Schneiden, Feilen und Nagelhaut",
                   "en": "Cut, file and cuticles"},
     "duracao": {"pt": "45min", "de": "45min", "en": "45min"}},
]

# Comprimento das unhas — reutiliza a estrutura central de modificadores
# (mesmo mecanismo de "fator" partilhado com o carrinho e com os preços).
# Os IDs internos mantêm-se para não partir sessões nem marcações antigas.
TAMANHOS_VEICULO = [
    {"id": "tam_p", "fator": 1.0,
     "titulo": {"pt": "Natural/curto", "de": "Natürlich/kurz", "en": "Natural/short"},
     "descricao": {"pt": "Comprimento natural", "de": "Natürliche Länge", "en": "Natural length"}},
    {"id": "tam_m", "fator": 1.10,
     "titulo": {"pt": "Médio", "de": "Mittel", "en": "Medium"},
     "descricao": {"pt": "Ligeiramente além da ponta do dedo",
                   "de": "Leicht über die Fingerkuppe hinaus",
                   "en": "Slightly beyond the fingertip"}},
    {"id": "tam_g", "fator": 1.20,
     "titulo": {"pt": "Longo", "de": "Lang", "en": "Long"},
     "descricao": {"pt": "Comprimento marcado", "de": "Deutliche Länge", "en": "Noticeable length"}},
    {"id": "tam_xl", "fator": 1.35,
     "titulo": {"pt": "Extra longo", "de": "Extra lang", "en": "Extra long"},
     "descricao": {"pt": "Requer mais tempo de trabalho",
                   "de": "Benötigt mehr Arbeitszeit",
                   "en": "Requires more working time"}},
]

EXTRAS_LIMPEZA = [
    {"id": "ex_nenhum", "preco": 0,
     "titulo": {"pt": "Sem extra", "de": "Kein Extra", "en": "No extra"},
     "descricao": {"pt": "Seguir sem extras", "de": "Ohne Extras fortfahren", "en": "Continue without extras"}},
    {"id": "ex_french", "preco": 10,
     "titulo": {"pt": "French/Babyboomer", "de": "French/Babyboomer", "en": "French/Babyboomer"},
     "descricao": {"pt": "Acabamento degradê ou ponta branca",
                   "de": "Verlauf oder weisse Spitze",
                   "en": "Gradient or white tip finish"}},
    {"id": "ex_nailart", "preco": 15,
     "titulo": {"pt": "Nail Art simples", "de": "Einfache Nailart", "en": "Simple nail art"},
     "descricao": {"pt": "Desenho ou decoração em algumas unhas",
                   "de": "Motiv oder Dekoration auf einigen Nägeln",
                   "en": "Design or decoration on a few nails"}},
    {"id": "ex_reparacao", "preco": 8,
     "titulo": {"pt": "Reparação de uma unha", "de": "Reparatur eines Nagels", "en": "Repair of one nail"},
     "descricao": {"pt": "Correção pontual", "de": "Punktuelle Korrektur", "en": "Single-nail fix"}},
]

ESTETICA_SERVICOS = [
    {"id": "es_pedicure", "preco": 55,
     "titulo": {"pt": "Pedicure clássica", "de": "Klassische Pediküre", "en": "Classic pedicure"},
     "descricao": {"pt": "Corte, lima e cuidado das cutículas",
                   "de": "Schneiden, Feilen und Nagelhautpflege",
                   "en": "Cut, file and cuticle care"},
     "duracao": {"pt": "1h", "de": "1h", "en": "1h"}},
    {"id": "es_pedigel", "preco": 75,
     "titulo": {"pt": "Pedicure + Gellack", "de": "Pediküre + Gellack", "en": "Pedicure + gel polish"},
     "descricao": {"pt": "Pedicure com verniz em gel",
                   "de": "Pediküre mit Gellack",
                   "en": "Pedicure with gel polish"},
     "duracao": {"pt": "1h15", "de": "1h15", "en": "1h 15"}},
    {"id": "es_spa", "preco": 85,
     "titulo": {"pt": "Spa pedicure", "de": "Spa-Pediküre", "en": "Spa pedicure"},
     "descricao": {"pt": "Banho, esfoliação e massagem",
                   "de": "Fussbad, Peeling und Massage",
                   "en": "Soak, scrub and massage"},
     "duracao": {"pt": "1h30", "de": "1h30", "en": "1h 30"}},
]

# Remoção de produto — reutiliza a mesma estrutura central de modificadores
# por "fator". Nenhuma mensagem aqui fala de veículos.
ESTADO_VEICULO = [
    {"id": "est_bom", "fator": 1.0,
     "titulo": {"pt": "Sem remoção", "de": "Ohne Entfernung", "en": "No removal"},
     "descricao": {"pt": "As unhas estão sem produto",
                   "de": "Die Nägel sind ohne Produkt",
                   "en": "Nails have no product on"}},
    {"id": "est_medio", "fator": 1.10,
     "titulo": {"pt": "Remover Gellack", "de": "Gellack entfernen", "en": "Remove gel polish"},
     "descricao": {"pt": "Retirar verniz em gel existente",
                   "de": "Vorhandenen Gellack ablösen",
                   "en": "Take off existing gel polish"}},
    {"id": "est_mau", "fator": 1.20,
     "titulo": {"pt": "Remover gel/acrílico", "de": "Gel/Acryl entfernen", "en": "Remove gel/acrylic"},
     "descricao": {"pt": "Remoção de construção em gel ou acrílico",
                   "de": "Entfernung von Gel- oder Acrylaufbau",
                   "en": "Removal of gel or acrylic build-up"}},
]

EXTRAS_ESTETICA = [
    {"id": "exe_nenhum", "preco": 0,
     "titulo": {"pt": "Sem extra", "de": "Kein Extra", "en": "No extra"},
     "descricao": {"pt": "Seguir sem extras", "de": "Ohne Extras fortfahren", "en": "Continue without extras"}},
    {"id": "exe_french", "preco": 10,
     "titulo": {"pt": "French", "de": "French", "en": "French"},
     "descricao": {"pt": "Acabamento francês", "de": "French-Finish", "en": "French finish"}},
    {"id": "exe_nailart", "preco": 15,
     "titulo": {"pt": "Nail Art", "de": "Nailart", "en": "Nail art"},
     "descricao": {"pt": "Decoração nos dedos dos pés",
                   "de": "Dekoration auf den Zehennägeln",
                   "en": "Decoration on the toenails"}},
    {"id": "exe_calos", "preco": 20,
     # "Tratamento de calosidades" tem 25 caracteres e a API corta os títulos
     # de linha aos 24 (MAX_TITULO_LINHA) — sem o "de" fica igual de claro.
     "titulo": {"pt": "Tratamento calosidades", "de": "Hornhautbehandlung", "en": "Callus treatment"},
     "descricao": {"pt": "Cuidado intensivo da pele dura",
                   "de": "Intensive Pflege der harten Haut",
                   "en": "Intensive care for hard skin"}},
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
    {"id": "mp_gerir",
     "titulo": {"pt": "🗓️ Gerir marcação", "de": "🗓️ Termin verwalten", "en": "🗓️ Manage booking"},
     "descricao": {"pt": "Ver, reagendar ou cancelar", "de": "Ansehen, verschieben oder stornieren",
                   "en": "View, reschedule or cancel"}},
    {"id": "mp_humano",
     "titulo": {"pt": "💬 Falar com a equipa", "de": "💬 Mit dem Team sprechen", "en": "💬 Talk to the team"},
     "descricao": {"pt": "Um humano responde-lhe em breve", "de": "Ein Mitarbeiter meldet sich in Kürze",
                   "en": "A team member will reply shortly"}},
    {"id": "mp_idioma",
     "titulo": {"pt": "🌍 Alterar idioma", "de": "🌍 Sprache ändern", "en": "🌍 Change language"},
     "descricao": {"pt": "Português, Deutsch, English", "de": "Português, Deutsch, English",
                   "en": "Português, Deutsch, English"}},
]

# Categorias VISÍVEIS ao cliente. Os IDs internos mantêm-se (cat_limpeza /
# cat_estetica) de propósito: mudá-los só por causa do texto partiria sessões
# guardadas, marcações antigas e o dispatch do webhook, sem ganho nenhum.
CATEGORIAS_MARCAR = [
    {"id": "cat_limpeza", "titulo": {"pt": "💅 Mãos / Manicure", "de": "💅 Hände / Maniküre",
                                     "en": "💅 Hands / Manicure"}},
    {"id": "cat_estetica", "titulo": {"pt": "🦶 Pés / Pedicure", "de": "🦶 Füsse / Pediküre",
                                      "en": "🦶 Feet / Pedicure"}},
]

# O fluxo Wrap & Proteção NÃO foi apagado: as tabelas, migrations, rotas da
# API e funções continuam todas lá, para não partir bases de dados nem
# pedidos antigos. Só deixou de ter entrada nos menus públicos desta versão.
CATEGORIA_WRAP_OCULTA = {"id": "cat_wrap",
                         "titulo": {"pt": "🎨 Wrap & Proteção", "de": "🎨 Folierung & Schutz",
                                    "en": "🎨 Wrap & Protection"}}

NOME_CATEGORIA = {c["id"]: c["titulo"] for c in CATEGORIAS_MARCAR}
NOME_CATEGORIA[CATEGORIA_WRAP_OCULTA["id"]] = CATEGORIA_WRAP_OCULTA["titulo"]


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
    # Histórico de reagendamentos feitos pelo painel. Tabela NOVA e à parte:
    # a migração é automática e não destrutiva (CREATE TABLE IF NOT EXISTS),
    # por isso bases de dados antigas continuam a funcionar tal e qual.
    conn.execute(
        "CREATE TABLE IF NOT EXISTS agendamento_historico ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "agendamento_id INTEGER NOT NULL, "
        "data_anterior TEXT, "
        "hora_anterior TEXT, "
        "data_nova TEXT, "
        "hora_nova TEXT, "
        "origem TEXT NOT NULL DEFAULT 'dashboard', "
        "alterado_em TEXT NOT NULL)"
    )
    # -----------------------------------------------------------------------
    # bloqueia_horario — separa DEFINITIVAMENTE o estado da marcação da
    # disponibilidade do horário: 0 = horário livre, 1 = horário bloqueado.
    # Uma marcação pode estar cancelada e o negócio decidir na mesma se
    # aquele horário volta ao mercado ou não (ver libertar_horario_ao_cancelar).
    #
    # Migração automática e NÃO destrutiva: a coluna nasce com DEFAULT 1
    # (uma marcação nova ocupa mesmo o horário), mas no instante em que é
    # criada as marcações antigas já canceladas ou reagendadas são postas a
    # 0 — senão horários que hoje estão livres começavam de repente a
    # aparecer bloqueados, sem ninguém ter pedido nada. O UPDATE corre uma
    # única vez: nos arranques seguintes o ALTER falha (coluna já existe) e
    # as escolhas entretanto feitas no painel ficam intactas.
    # -----------------------------------------------------------------------
    try:
        conn.execute("ALTER TABLE agendamentos ADD COLUMN bloqueia_horario INTEGER NOT NULL DEFAULT 1")
        conn.execute("UPDATE agendamentos SET bloqueia_horario = 0 "
                     "WHERE LOWER(COALESCE(estado, '')) IN ('cancelado', 'reagendado')")
        # Fecha já a transação implícita aberta por este UPDATE: quem recebe
        # esta ligação pode precisar de abrir a sua própria transação com
        # BEGIN IMMEDIATE (cancelar/reagendar/gravar marcação) e o SQLite não
        # deixa abrir uma transação dentro de outra.
        conn.commit()
    except sqlite3.OperationalError:
        pass  # coluna já existe — nada a migrar
    # Configurações do negócio editáveis no painel (chave -> valor em texto).
    # Tabela NOVA: CREATE TABLE IF NOT EXISTS chega, bases de dados antigas
    # continuam a funcionar exatamente na mesma e ganham os valores por
    # omissão definidos em CONFIGURACOES_OMISSAO.
    conn.execute(
        "CREATE TABLE IF NOT EXISTS configuracoes ("
        "chave TEXT PRIMARY KEY, "
        "valor TEXT NOT NULL, "
        "atualizado_em TEXT NOT NULL)"
    )
    # Reservas TEMPORÁRIAS: o horário que um cliente acabou de escolher fica
    # retido em nome dele enquanto está a rever e a confirmar a marcação, e
    # deixa de ser oferecido a mais ninguém. Não é uma marcação — expira
    # sozinha (ver RESERVA_TEMPORARIA_MINUTOS) e nunca aparece no calendário
    # nem no painel. Uma linha por número: um cliente só configura uma
    # marcação de cada vez. Tabela nova -> migração automática e inofensiva.
    # Mensagens JÁ PROCESSADAS, pelo id que a Meta atribui a cada uma
    # (wamid...). A Meta reenvia o mesmo webhook quando não recebe o 200 a
    # tempo; sem isto, um reenvio depois de uma confirmação voltava a correr
    # o mesmo passo — e chegava a tentar gravar uma segunda marcação sobre
    # uma sessão já reiniciada. Tabela nova: migração automática e inofensiva.
    conn.execute(
        "CREATE TABLE IF NOT EXISTS mensagens_processadas ("
        "id TEXT PRIMARY KEY, "
        "recebida_em TEXT NOT NULL)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS reservas_temporarias ("
        "telefone TEXT PRIMARY KEY, "
        "data TEXT NOT NULL, "
        "hora TEXT NOT NULL, "
        "servico TEXT, "
        "duracao TEXT, "
        "criado_em TEXT NOT NULL, "
        "expira_em TEXT NOT NULL)"
    )
    return conn


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


def obter_configuracao(chave, omissao=None):
    """Valor guardado de uma configuração, ou o valor por omissão quando
    ainda nunca foi gravada (base de dados antiga, primeira utilização)."""
    with obter_bd() as conn:
        linha = conn.execute("SELECT valor FROM configuracoes WHERE chave = ?", (chave,)).fetchone()
    if linha:
        return linha[0]
    return CONFIGURACOES_OMISSAO.get(chave) if omissao is None else omissao


def guardar_configuracao(chave, valor):
    with obter_bd() as conn:
        conn.execute(
            "INSERT INTO configuracoes (chave, valor, atualizado_em) VALUES (?, ?, ?) "
            "ON CONFLICT(chave) DO UPDATE SET valor = excluded.valor, "
            "atualizado_em = excluded.atualizado_em",
            (chave, str(valor), datetime.utcnow().isoformat()),
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


# Quanto tempo se guarda o id de uma mensagem já tratada. Bem acima da
# janela de reenvios da Meta, e curto o suficiente para a tabela não crescer.
IDEMPOTENCIA_HORAS = 24


def mensagem_ja_processada(id_mensagem):
    """True se esta mensagem JÁ foi tratada — nesse caso não se volta a agir.
    Regista-a atomicamente: o INSERT OR IGNORE só afeta uma linha na primeira
    vez, por isso dois webhooks simultâneos com o mesmo id nunca passam os
    dois. Sem id (mensagens de teste, formatos antigos) segue o fluxo normal."""
    if not id_mensagem:
        return False
    agora = datetime.utcnow()
    with obter_bd() as conn:
        conn.execute("DELETE FROM mensagens_processadas WHERE recebida_em < ?",
                     ((agora - timedelta(hours=IDEMPOTENCIA_HORAS)).isoformat(),))
        cur = conn.execute(
            "INSERT OR IGNORE INTO mensagens_processadas (id, recebida_em) VALUES (?, ?)",
            (str(id_mensagem), agora.isoformat()))
        return cur.rowcount == 0


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


# Colunas de `agendamentos` lidas em todo o lado — uma lista só, para nunca
# haver um SELECT a devolver menos colunas do que o dicionário espera.
CAMPOS_AGENDAMENTO = ["id", "telefone", "nome", "categoria", "servico", "extra", "data", "hora",
                      "preco", "duracao", "estado", "criado_em", "carrinho_json", "bloqueia_horario"]
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

    with obter_bd() as conn:
        conn.execute("BEGIN IMMEDIATE")
        if data_iso and hora:
            # Conta com as marcações gravadas E com os horários que outros
            # clientes escolheram e ainda estão a confirmar (a retenção do
            # próprio é ignorada — é exatamente esta que ele vem confirmar).
            if conflitos_no_intervalo(ocupacoes(telefone, conn), data_iso, hora,
                                      sessao.get("servico"), duracao):
                raise HorarioOcupado(f"{data_iso} {hora}")
        cur = conn.execute(
            "INSERT INTO agendamentos "
            "(telefone, nome, categoria, servico, extra, data, hora, preco, duracao, estado, criado_em, "
            "carrinho_json, bloqueia_horario) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'confirmado', ?, ?, 1)",
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
            "WHERE telefone = ? AND estado = 'confirmado' ORDER BY id DESC LIMIT 1",
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
            "WHERE telefone = ? AND estado = 'confirmado' ORDER BY id DESC",
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


def atualizar_estado_agendamento(id_agendamento, estado, bloqueia_horario=None):
    """Muda o estado de uma marcação. `bloqueia_horario` é OPCIONAL e, quando
    indicado, é gravado na mesma instrução — o estado e a disponibilidade do
    horário nunca ficam por um instante em desacordo.

    Quando não é indicado, aplica-se a regra por omissão do estado: uma
    marcação REAGENDADA (a antiga, que já não vai acontecer) deixa sempre de
    ocupar o horário; concluída e confirmada continuam a ocupá-lo; o
    cancelamento tem caminho próprio (ver marcar_agendamento_cancelado), por
    ser o único caso em que a decisão pertence ao negócio."""
    if bloqueia_horario is None and chave_estado(estado) == "reagendado":
        bloqueia_horario = 0
    with obter_bd() as conn:
        if bloqueia_horario is None:
            conn.execute("UPDATE agendamentos SET estado = ? WHERE id = ?", (estado, id_agendamento))
        else:
            conn.execute("UPDATE agendamentos SET estado = ?, bloqueia_horario = ? WHERE id = ?",
                         (estado, int(bool(bloqueia_horario)), id_agendamento))


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
# Cor de cada SERVIÇO no calendário — mapa central, fácil de editar.
# A chave é sempre o nome CANÓNICO em português (o que fica gravado na coluna
# `servico`), nunca o texto traduzido nem uma cor gerada ao acaso: assim a cor
# de um serviço é estável entre sessões, idiomas e atualizações da página.
# A COR identifica o serviço; o ESTADO (confirmado/concluído/reagendado/
# cancelado) é sempre comunicado à parte, por texto (ver ESTADO_CALENDARIO).
# ---------------------------------------------------------------------------
CORES_SERVICOS = {
    # Mãos / Manicure
    "Gellack mãos": "#d1478f",          # magenta
    "Aplicação de gel": "#a45cc4",      # roxo
    "Reenchimento": "#6f5ae0",          # violeta-azulado
    "Manicure clássica": "#e896c8",     # rosa claro
    # Pés / Pedicure
    "Pedicure clássica": "#20a4b8",     # turquesa
    "Pedicure + Gellack": "#2ea05a",    # verde
    "Spa pedicure": "#e8963c",          # laranja
    # Serviços do catálogo ANTIGO: continuam aqui para as marcações já
    # gravadas não perderem a cor no calendário nem no histórico.
    "Interior": "#3878e8",
    "Exterior": "#3d8f9e",
    "Interior + Exterior": "#5a6fd0",
    "Polimento": "#c08a3c",
    "Proteção cerâmica": "#3f8f5f",
    "Polimento de faróis": "#d4c23a",
    "Wrap total": "#b0538c",
    "Wrap parcial": "#8f63a8",
}
# Só os serviços ATUAIS entram na legenda do painel — os antigos continuam a
# ter cor, mas não enchem a legenda com nomes que já não se podem marcar.
SERVICOS_NA_LEGENDA = ("Gellack mãos", "Aplicação de gel", "Reenchimento", "Manicure clássica",
                       "Pedicure clássica", "Pedicure + Gellack", "Spa pedicure")
COR_SERVICO_OMISSAO = "#8b95a6"         # cinzento-azulado, para serviços desconhecidos


def cor_do_servico(servico_pt):
    """Cor estável de um serviço, a partir do nome canónico em português."""
    return CORES_SERVICOS.get((servico_pt or "").strip(), COR_SERVICO_OMISSAO)


def cores_servicos_legenda():
    """Mapa nome -> cor para a legenda "Cores dos serviços" do painel: os
    serviços atuais, mais a entrada de reserva para tudo o resto (incluindo
    as marcações antigas, que mantêm a sua cor própria em CORES_SERVICOS)."""
    return {**{n: CORES_SERVICOS[n] for n in SERVICOS_NA_LEGENDA},
            "Outro serviço": COR_SERVICO_OMISSAO}


# Estados de uma marcação tal como aparecem no calendário (texto sempre
# visível, além da cor do serviço).
ESTADO_CALENDARIO = {
    "confirmado": "Confirmado",
    "concluido": "Concluído",
    "reagendado": "Reagendado",
    "cancelado": "Cancelado",
}


def chave_estado(estado):
    """"Concluído" -> "concluido". A base de dados guarda o estado como foi
    escrito (com acento); a chave normalizada é a que se usa para comparar,
    filtrar e escolher classes CSS. Mesma regra do chaveEstado() do painel."""
    limpo = unicodedata.normalize("NFD", str(estado or ""))
    return "".join(c for c in limpo if not unicodedata.combining(c)).strip().lower()


# ---------------------------------------------------------------------------
# DISPONIBILIDADE REAL — o estado da marcação e a ocupação do horário são
# duas coisas diferentes (ver a coluna bloqueia_horario):
#   • confirmada ................................. bloqueia
#   • concluída .................................. bloqueia
#   • cancelada com bloqueia_horario = 1 ......... bloqueia
#   • cancelada com bloqueia_horario = 0 ......... NÃO bloqueia
#   • reagendada (a antiga) ...................... NÃO bloqueia
#   • a marcação nova saída do reagendamento fica confirmada -> bloqueia
# Esta é a ÚNICA função que decide se um registo ocupa um horário; tudo o
# resto (calendário, painel, WhatsApp) pergunta-lhe a ela.
# ---------------------------------------------------------------------------
ESTADOS_QUE_BLOQUEIAM_SEMPRE = ("confirmado", "concluido")


def agendamento_bloqueia_horario(agendamento):
    estado = chave_estado((agendamento or {}).get("estado") or "confirmado")
    if estado in ESTADOS_QUE_BLOQUEIAM_SEMPRE:
        return True
    if estado == "cancelado":
        return int((agendamento or {}).get("bloqueia_horario") or 0) == 1
    return False        # reagendada antiga (e qualquer estado desconhecido)


def horario_livre_de_uma_marcacao(agendamento):
    """True quando o registo existe mas o horário está livre — cancelada e
    libertada. Usado para a distinguir visualmente de uma cancelada que
    continua a ocupar o horário."""
    return chave_estado((agendamento or {}).get("estado")) == "cancelado" \
        and not agendamento_bloqueia_horario(agendamento)


def data_iso_de_texto(texto):
    """"02.09.2026 (qua)" -> "2026-09-02". Ignora o dia da semana e qualquer
    texto extra. Devolve None se não houver uma data válida."""
    achado = re.search(r"(\d{1,2})[./-](\d{1,2})[./-](\d{4})", str(texto or ""))
    if not achado:
        return None
    dia, mes, ano = (int(x) for x in achado.groups())
    try:
        return date(ano, mes, dia).isoformat()
    except ValueError:
        return None


def hora_hhmm_de_texto(texto):
    """"🕝 14:30" -> "14:30". Ignora emojis e texto à volta. None se
    não houver uma hora válida."""
    achado = re.search(r"(\d{1,2})[:hH](\d{2})", str(texto or ""))
    if not achado:
        return None
    horas, minutos = int(achado.group(1)), int(achado.group(2))
    if not (0 <= horas <= 23 and 0 <= minutos <= 59):
        return None
    return f"{horas:02d}:{minutos:02d}"


def duracao_para_minutos(texto):
    """Converte a duração guardada em (minutos, dia_inteiro).

    Aceita "45min", "1h", "1h30", "2h", "3h", "aproximadamente 1h", "1 dia"
    e "1 Tag"/"1 day". "1 dia" devolve (duração da grelha, True) — é
    apresentado como serviço de dia inteiro. Devolve (None, False) quando não
    consegue interpretar nada."""
    bruto = str(texto or "").strip().lower()
    if not bruto:
        return None, False
    if re.search(r"\d+\s*(dia|dias|tag|tage|day|days)\b", bruto):
        return DURACAO_DIA_INTEIRO_MIN, True

    # "1h30" / "1h 30" / "2h" (as horas podem trazer minutos colados)
    achado = re.search(r"(\d+)\s*[hH](?:\s*(\d{1,2}))?", bruto)
    if achado:
        minutos = int(achado.group(1)) * 60 + int(achado.group(2) or 0)
        return (minutos, False) if minutos > 0 else (None, False)

    # "45min" / "45 minutos"
    achado = re.search(r"(\d+)\s*(min|minuto|minutos|minuten)\b", bruto)
    if achado:
        minutos = int(achado.group(1))
        return (minutos, False) if minutos > 0 else (None, False)
    return None, False


def evento_calendario(agendamento, pedido=None):
    """Transforma uma marcação num evento de calendário, ou None quando a
    data, a hora OU a duração não forem interpretáveis — o calendário nunca
    inventa um horário nem uma duração. Quem chama conta estes casos e
    mostra um aviso; a marcação continua visível na tabela normal.

    `pedido` é o pedido de orçamento associado (pedidos_orcamento.
    agendamento_id), quando existir: é dele que vêm veículo, ano, wrap,
    acabamento e fotografias, sem duplicar nada na tabela de agendamentos."""
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
        "estado": agendamento.get("estado") or "confirmado",
        "estado_chave": chave_estado(agendamento.get("estado") or "confirmado"),
        # A cor diz o SERVIÇO; estes dois dizem, por texto, o que a cor nunca
        # diz: se o registo ainda ocupa o horário ou se este já está livre.
        "bloqueia_horario": agendamento_bloqueia_horario(agendamento),
        "horario_livre": not agendamento_bloqueia_horario(agendamento),
        "nome": agendamento.get("nome"),
        "primeiro_nome": primeiro_nome(agendamento.get("nome")) or "",
        "telefone": agendamento.get("telefone"),
        "servico": agendamento.get("servico"),
        "extra": agendamento.get("extra"),
        "data": agendamento.get("data"),
        "hora": agendamento.get("hora"),
        "hora_hhmm": hora,
        "duracao": duracao_texto,
        "preco": agendamento.get("preco"),
        "cor": cor_do_servico(agendamento.get("servico")),
        "total_centimos": total_centimos_agendamento(agendamento),
        "carrinho": linhas_carrinho_agendamento(agendamento),
        "criado_em": agendamento.get("criado_em"),
        "pedido": None,
    }
    if pedido:
        evento["pedido"] = {
            "id": pedido["id"],
            "veiculo": pedido.get("veiculo"),
            "ano_veiculo": pedido.get("ano_veiculo"),
            "tipo_wrap": pedido.get("tipo_wrap"),
            "cor_acabamento": pedido.get("cor_acabamento"),
            "estado": pedido.get("estado"),
            "modo_pedido": pedido.get("modo_pedido"),
            "fotografias": listar_fotografias(pedido["id"]),
        }
    return evento


def pedidos_por_agendamento():
    """Mapa agendamento_id -> pedido, para associar sem uma consulta por
    marcação. Só entram pedidos que tenham mesmo agendamento_id preenchido."""
    return {p["agendamento_id"]: p for p in listar_pedidos_orcamento() if p.get("agendamento_id")}


def eventos_calendario(inicio_iso=None, fim_iso=None):
    """Eventos do calendário no intervalo pedido (inclusive), mais a
    contagem de marcações que não foi possível converter. O filtro é feito
    pelo DIA já convertido, porque a coluna `data` guarda texto e não uma
    data comparável em SQL."""
    associados = pedidos_por_agendamento()
    eventos, invalidos = [], 0
    for ag in listar_agendamentos():
        evento = evento_calendario(ag, associados.get(ag["id"]))
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
    fixas = []
    if sessao is not None:
        fixas.append({"id": "ver_carrinho",
                      "title": titulo_linha_carrinho(destinatario, idioma, sessao)[:MAX_TITULO_LINHA]})
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
GRUPO_SERVICO_BASE = "servico_base"        # serviço de Mãos ou de Pés
GRUPO_TAMANHO_VEICULO = "tamanho_veiculo"  # comprimento das unhas (Mãos) ou remoção (Pés)
GRUPO_WRAP_VEICULO = "wrap_veiculo"        # tipo de veículo (Wrap, passo 1)
GRUPO_WRAP_TIPO = "wrap_tipo"              # wrap total / parcial
GRUPO_WRAP_COR = "wrap_cor"                # cor (família + cor, ou personalizada)
GRUPO_ACABAMENTO = "acabamento"            # acabamento do wrap (brilhante, mate, ...)
GRUPO_EXTRA = "extra"                      # extras de Mãos/Pés
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


# ---------------------------------------------------------------------------
# Apresentação central de PREÇOS nas opções dos menus
# ---------------------------------------------------------------------------
# Ponto único onde um preço se transforma em texto visível. Os valores vêm
# SEMPRE das tabelas centrais já existentes (LIMPEZA_TIPOS, ESTETICA_SERVICOS,
# EXTRAS_*, TAMANHOS_VEICULO/ESTADO_VEICULO via fator, WRAP_*_PRECOS_CENTIMOS)
# — nunca são reescritos à mão nos textos nem na lógica dos menus. Alterar um
# preço na tabela central atualiza automaticamente menus, carrinho, resumo e
# notificações, porque todos passam por aqui ou por formatar_centimos().
#
# Estilos:
#   "base"      -> CHF X        (serviço base)
#   "acrescimo" -> +CHF X       (acréscimo/extra; 0 -> "Incluído")
#   "desconto"  -> -CHF X
#   "desde"     -> desde CHF X  (mínimo de uma categoria)
#   "estimado"  -> estimado CHF X (Wrap — o valor final depende da análise)
# ---------------------------------------------------------------------------
SEPARADOR_PRECO = " · "


def rotulo_preco(centimos, idioma, estilo="base"):
    """Texto do preço de uma opção, já traduzido e no MESMO formato monetário
    usado no carrinho (ver formatar_centimos). Devolve "" quando não há preço
    nenhum a mostrar (centimos None)."""
    if centimos is None:
        return ""
    if estilo == "acrescimo":
        if centimos == 0:
            return t("preco_incluido", idioma)
        return f"+{formatar_centimos(centimos, idioma)}"
    if estilo == "desconto":
        return f"-{formatar_centimos(abs(centimos), idioma)}"
    if estilo == "desde":
        return t("preco_desde", idioma, preco=formatar_centimos(centimos, idioma))
    if estilo == "estimado":
        return t("preco_estimado", idioma, preco=formatar_centimos(centimos, idioma))
    return formatar_centimos(centimos, idioma)


def nome_com_preco(nome, centimos, idioma, estilo="base"):
    """Nome traduzido + preço formatado — a forma canónica de apresentar uma
    opção com preço em qualquer menu."""
    rotulo = rotulo_preco(centimos, idioma, estilo)
    return f"{nome}{SEPARADOR_PRECO}{rotulo}" if rotulo else nome


def _encurtar_titulo(nome, limite=None):
    """Encurta um título ao limite da API do WhatsApp sem partir palavras."""
    limite = limite or MAX_TITULO_LINHA
    if len(nome) <= limite:
        return nome
    corte = nome[:limite - 1].rstrip()
    if " " in corte:
        corte = corte[:corte.rfind(" ")].rstrip()
    return corte + "…"


def opcao_com_preco(opcao, centimos, idioma, estilo="base"):
    """Devolve uma CÓPIA da opção de catálogo com o preço visível, sem nunca
    tocar no catálogo original (os nomes canónicos em português, gravados no
    carrinho e na base de dados, têm de continuar sem preço).

    O preço vai no título quando cabe no limite de 24 caracteres da API; se
    não couber, o título fica só com o nome e o preço passa para o início da
    descrição — assim o preço nunca aparece cortado a meio."""
    nome = tx(opcao.get("titulo"), idioma)
    rotulo = rotulo_preco(centimos, idioma, estilo)
    nova = dict(opcao)
    if not rotulo:
        nova["titulo"] = nome
        nova["descricao"] = tx(opcao.get("descricao"), idioma) or None
        return nova
    titulo_com_preco = f"{nome}{SEPARADOR_PRECO}{rotulo}"
    descricao = tx(opcao.get("descricao"), idioma) or ""
    if len(titulo_com_preco) <= MAX_TITULO_LINHA:
        nova["titulo"] = titulo_com_preco
        nova["descricao"] = descricao or None
    else:
        # Nem com o preço fora do título o nome cabe? Então corta-se numa
        # fronteira de palavra e o nome COMPLETO vai para a descrição — nunca
        # se deixa uma palavra partida a meio na lista.
        nova["titulo"] = _encurtar_titulo(nome)
        detalhe = f"{nome}{SEPARADOR_PRECO}" if nova["titulo"] != nome else ""
        nova["descricao"] = f"{detalhe}{rotulo}{SEPARADOR_PRECO}{descricao}".strip() \
            if descricao else f"{detalhe}{rotulo}".strip()
    return nova


def opcoes_com_precos(opcoes, idioma, precos_centimos, estilo="base"):
    """Aplica opcao_com_preco() a um catálogo inteiro. `precos_centimos` é
    uma função id -> cêntimos (ou None, para não mostrar preço nessa opção)."""
    return [opcao_com_preco(o, precos_centimos(o["id"]), idioma, estilo) for o in opcoes]


def _preco_catalogo_centimos(catalogo, item_id):
    """Preço em cêntimos de uma opção de Limpeza/Estética/Extras — os
    catálogos guardam CHF inteiros, tal como carrinho_definir_servico_base()
    e carrinho_definir_extra() fazem a conversão."""
    opcao = encontrar_opcao(catalogo, item_id) or {}
    return int(opcao.get("preco", 0)) * 100


def preco_minimo_categoria_centimos(categoria_id):
    """Preço mais baixo de uma categoria, para o "desde CHF X" do primeiro
    menu. Lido diretamente das tabelas centrais — nunca escrito à mão."""
    if categoria_id == "cat_limpeza":
        return min(int(o["preco"]) * 100 for o in LIMPEZA_TIPOS)
    if categoria_id == "cat_estetica":
        return min(int(o["preco"]) * 100 for o in ESTETICA_SERVICOS)
    if categoria_id == "cat_wrap":
        return min(WRAP_PRECOS_CENTIMOS.values())
    return None


def opcoes_categorias_com_precos(idioma):
    """Categorias do primeiro menu com o preço mínimo de cada uma. O Wrap usa
    o estilo "estimado desde", porque o valor final depende sempre da análise
    das fotografias pela equipa."""
    opcoes = []
    for cat in CATEGORIAS_MARCAR:
        minimo = preco_minimo_categoria_centimos(cat["id"])
        estilo = "estimado" if cat["id"] == "cat_wrap" else "desde"
        if cat["id"] == "cat_wrap" and minimo is not None:
            # "estimativa desde CHF X": no Wrap o total é sempre estimado E o
            # mínimo é apenas um ponto de partida.
            nova = dict(cat)
            nova["titulo"] = tx(cat["titulo"], idioma)
            nova["descricao"] = t("preco_estimativa_desde", idioma,
                                  preco=formatar_centimos(minimo, idioma))
            opcoes.append(nova)
        else:
            opcoes.append(opcao_com_preco(cat, minimo, idioma, estilo))
    return opcoes


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


def delta_modificador_veiculo_centimos(sessao, catalogo, item_id):
    """Acréscimo, em cêntimos, que um comprimento/remoção vai somar ao
    carrinho. É EXATAMENTE a mesma conta de carrinho_definir_modificador_
    veiculo() — partilhada aqui para o preço mostrado na opção ser sempre
    idêntico ao que depois aparece no carrinho e no resumo."""
    opcao = encontrar_opcao(catalogo, item_id) or {"fator": 1.0}
    return round(_preco_servico_base_centimos(sessao) * (opcao.get("fator", 1.0) - 1.0))


def carrinho_definir_servico_base(sessao, catalogo, item_id):
    """Usa os preços já existentes de Limpeza/Estética (guardados em CHF
    inteiros no catálogo) — apenas convertidos para cêntimos aqui."""
    opcao = encontrar_opcao(catalogo, item_id) or {}
    nome_pt = tx(opcao.get("titulo"), "pt")
    preco_centimos = int(opcao.get("preco", 0)) * 100
    carrinho_definir_item(sessao, GRUPO_SERVICO_BASE, item_id, nome_pt, preco_centimos)
    return preco_centimos


def carrinho_definir_modificador_veiculo(sessao, catalogo, item_id):
    """Comprimento das unhas (Mãos) ou remoção de produto (Pés): aplicam um FATOR
    multiplicativo sobre o preço base — aqui é convertido no acréscimo em
    cêntimos correspondente, para poder ser somado como mais uma linha do
    carrinho (nunca se multiplica um total antigo)."""
    opcao = encontrar_opcao(catalogo, item_id) or {"fator": 1.0}
    nome_pt = tx(opcao.get("titulo"), "pt")
    delta_centimos = delta_modificador_veiculo_centimos(sessao, catalogo, item_id)
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
# Todos os passos mostram o preço na própria opção (ver opcoes_com_precos, que
# lê sempre as tabelas centrais) e têm sempre ⬅️ Voltar e ❌ Cancelar
# clicáveis, além do 🛒 Carrinho.
# ---------------------------------------------------------------------------
def passo_limpeza_tipo(de, idioma, sessao=None):
    opcoes = opcoes_com_precos(LIMPEZA_TIPOS, idioma,
                               lambda i: _preco_catalogo_centimos(LIMPEZA_TIPOS, i), "base")
    enviar_lista(de, t("limpeza_tipo_corpo", idioma), t("limpeza_tipo_seccao", idioma), opcoes, idioma,
                 botao=t("limpeza_tipo_botao", idioma), com_voltar=True, rodape=t("rodape_padrao", idioma), sessao=sessao)


def passo_limpeza_tamanho(de, idioma, sessao=None):
    # O acréscimo depende do serviço base já escolhido — usa exatamente a
    # mesma conta que o carrinho (ver delta_modificador_veiculo_centimos).
    opcoes = opcoes_com_precos(TAMANHOS_VEICULO, idioma,
                               lambda i: delta_modificador_veiculo_centimos(sessao or {}, TAMANHOS_VEICULO, i),
                               "acrescimo")
    enviar_lista(de, t("limpeza_tamanho_corpo", idioma), t("tamanho_seccao", idioma), opcoes, idioma,
                 botao=t("tamanho_botao", idioma), com_voltar=True, rodape=t("rodape_padrao", idioma), sessao=sessao)


def passo_limpeza_extra(de, idioma, sessao=None):
    opcoes = opcoes_com_precos(EXTRAS_LIMPEZA, idioma,
                               lambda i: _preco_catalogo_centimos(EXTRAS_LIMPEZA, i), "acrescimo")
    enviar_lista(de, t("extra_corpo", idioma), t("extra_seccao", idioma), opcoes, idioma,
                 botao=t("extra_botao", idioma), com_voltar=True, rodape=t("rodape_padrao", idioma), sessao=sessao)


# ---------------------------------------------------------------------------
# Passos do fluxo "Marcar" — Estética
# ---------------------------------------------------------------------------
def passo_estetica_servico(de, idioma, sessao=None):
    opcoes = opcoes_com_precos(ESTETICA_SERVICOS, idioma,
                               lambda i: _preco_catalogo_centimos(ESTETICA_SERVICOS, i), "base")
    enviar_lista(de, t("estetica_servico_corpo", idioma), t("estetica_servico_seccao", idioma), opcoes, idioma,
                 botao=t("estetica_servico_botao", idioma), com_voltar=True, rodape=t("rodape_padrao", idioma), sessao=sessao)


def passo_estetica_estado(de, idioma, sessao=None):
    opcoes = opcoes_com_precos(ESTADO_VEICULO, idioma,
                               lambda i: delta_modificador_veiculo_centimos(sessao or {}, ESTADO_VEICULO, i),
                               "acrescimo")
    enviar_lista(de, t("estetica_estado_corpo", idioma), t("estado_seccao", idioma), opcoes, idioma,
                 botao=t("estado_botao", idioma), com_voltar=True, rodape=t("rodape_padrao", idioma), sessao=sessao)


def passo_estetica_extra(de, idioma, sessao=None):
    opcoes = opcoes_com_precos(EXTRAS_ESTETICA, idioma,
                               lambda i: _preco_catalogo_centimos(EXTRAS_ESTETICA, i), "acrescimo")
    enviar_lista(de, t("extra_corpo", idioma), t("extra_seccao", idioma), opcoes, idioma,
                 botao=t("extra_botao", idioma), com_voltar=True, rodape=t("rodape_padrao", idioma), sessao=sessao)


# ---------------------------------------------------------------------------
# Data / hora / resumo / confirmação (comuns a limpeza e estética)
# ---------------------------------------------------------------------------
def passo_data(de, idioma, passo_n=4, sessao=None):
    enviar_lista(de, t("data_corpo", idioma, n=passo_n), t("data_seccao", idioma), proximos_dias(idioma), idioma,
                 botao=t("data_botao", idioma), com_voltar=True, rodape=t("rodape_padrao", idioma), sessao=sessao)


def passo_hora(de, idioma, passo_n=5, sessao=None):
    """Mostra só os horários REALMENTE livres na data escolhida. Não aparece
    um horário bloqueado (marcação confirmada, concluída, ou cancelada que o
    negócio decidiu manter ocupado) nem um horário que outro cliente acabou
    de ESCOLHER e ainda está a confirmar. Um horário libertado volta a
    aparecer de imediato, sem nada em cache."""
    livres = horarios_livres_para_sessao(sessao, telefone=de)
    if not livres:
        enviar_texto(de, t("hora_sem_vagas", idioma))
        passo_data(de, idioma, sessao=sessao)
        return
    enviar_lista(de, t("hora_corpo", idioma, n=passo_n), t("hora_seccao", idioma), livres, idioma,
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

    # Exatamente 3 botões = o máximo da API do WhatsApp. Ficam VISÍVEIS de
    # imediato, sem obrigar o cliente a abrir primeiro "Escolher opção" — é o
    # passo em que isso mais custa. Por isso não se junta aqui o ⬅️ Voltar
    # (que promoveria a mensagem a lista): "✏️ Não, alterar" faz o mesmo
    # papel, devolve o horário retido e leva de volta à escolha do serviço.
    enviar_botoes(de, "\n".join(linhas), [
        {"id": "confirmar", "titulo": t("botao_resumo_sim", idioma)},
        {"id": "alterar", "titulo": t("botao_resumo_nao", idioma)},
        {"id": ID_CANCELAR, "titulo": t("botao_cancelar", idioma)},
    ], idioma, rodape=t("rodape_padrao", idioma),
        titulo_seccao=t("resumo_seccao", idioma), botao_lista=t("menu_botao", idioma))


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
        linha = conn.execute("SELECT estado FROM agendamentos WHERE id = ?", (id_agendamento,)).fetchone()
        if not linha:
            raise LookupError("Marcação não encontrada.")
        if exigir_confirmado and linha[0] != "confirmado":
            raise EstadoInvalido(linha[0])
        # O registo NUNCA é apagado: fica no histórico como cancelado, só a
        # ocupação do horário é que muda.
        conn.execute("UPDATE agendamentos SET estado = 'cancelado', bloqueia_horario = ? WHERE id = ?",
                     (bloqueia, id_agendamento))
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
    data/hora NOVAS — usa exatamente a mesma duração do calendário."""
    dia = data_iso or data_iso_de_texto(agendamento.get("data"))
    hhmm = hora or hora_hhmm_de_texto(agendamento.get("hora"))
    if not dia or not hhmm:
        return None, None
    minutos, dia_inteiro = duracao_para_minutos(
        recuperar_duracao(agendamento.get("servico"), agendamento.get("duracao")))
    if minutos is None:
        return None, None
    inicio = datetime.fromisoformat(f"{dia}T{hhmm}:00")
    if dia_inteiro:
        inicio = inicio.replace(hour=CALENDARIO_HORA_INICIO, minute=0)
    return inicio, inicio + timedelta(minutes=minutos)


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
        if data_iso_de_texto(outro.get("data")) != data_iso:
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
    """Marcações que ocupam o horário para onde se quer mover a marcação
    `id_agendamento` (a própria é sempre ignorada)."""
    alvo = obter_agendamento(id_agendamento)
    if not alvo:
        return []
    return conflitos_no_intervalo(
        listar_agendamentos(), data_iso, hora,
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
                 (datetime.utcnow().isoformat(),))


def reter_horario(telefone, sessao):
    """Retém, em nome deste número, o horário que ele acabou de escolher.
    Substitui qualquer retenção anterior do mesmo número — um cliente só
    configura uma marcação de cada vez."""
    data, hora = sessao.get("data"), sessao.get("hora")
    if not data or not hora:
        return False
    _, duracao_pt, servico_pt, _ = calcular_preco_duracao(sessao)
    agora = datetime.utcnow()
    with obter_bd() as conn:
        _limpar_reservas_expiradas(conn)
        conn.execute(
            "INSERT INTO reservas_temporarias (telefone, data, hora, servico, duracao, criado_em, expira_em) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(telefone) DO UPDATE SET "
            "data = excluded.data, hora = excluded.hora, servico = excluded.servico, "
            "duracao = excluded.duracao, criado_em = excluded.criado_em, expira_em = excluded.expira_em",
            (telefone, data, hora, sessao.get("servico") or servico_pt,
             sessao.get("duracao") or duracao_pt, agora.isoformat(),
             (agora + timedelta(minutes=RESERVA_TEMPORARIA_MINUTOS)).isoformat()))
    return True


def libertar_horario_retido(telefone):
    """Devolve o horário ao mercado: chamado ao confirmar (aí passa a ser uma
    marcação a sério), ao cancelar, ao voltar atrás e ao reiniciar a sessão."""
    with obter_bd() as conn:
        _limpar_reservas_expiradas(conn)
        conn.execute("DELETE FROM reservas_temporarias WHERE telefone = ?", (telefone,))


def horarios_retidos(excluir_telefone=None, conn=None):
    """Retenções ainda válidas, no mesmo formato de uma marcação, para a
    verificação de conflitos as tratar exatamente como qualquer outra
    ocupação. A retenção do próprio cliente é sempre ignorada — senão ele
    ficava impedido de confirmar o horário que escolheu."""
    def _ler(c):
        _limpar_reservas_expiradas(c)
        return c.execute(
            "SELECT telefone, data, hora, servico, duracao FROM reservas_temporarias "
            "WHERE expira_em > ?", (datetime.utcnow().isoformat(),)).fetchall()

    if conn is not None:
        linhas = _ler(conn)
    else:
        with obter_bd() as ligacao:
            linhas = _ler(ligacao)
    return [{"id": None, "telefone": tel, "data": data, "hora": hora, "servico": servico,
             "duracao": duracao, "estado": "confirmado", "bloqueia_horario": 1,
             "retencao": True}
            for (tel, data, hora, servico, duracao) in linhas if tel != excluir_telefone]


def ocupacoes(excluir_telefone=None, conn=None):
    """Tudo o que ocupa horários: marcações gravadas + retenções em curso."""
    existentes = _agendamentos_da_conexao(conn) if conn is not None else listar_agendamentos()
    return existentes + horarios_retidos(excluir_telefone, conn)


def horario_esta_livre(data_iso, hora, servico=None, duracao=None, ignorar_id=None,
                       excluir_telefone=None):
    """True quando NADA ocupa esse intervalo — nem uma marcação gravada, nem
    um horário que outro cliente acabou de escolher e ainda está a confirmar."""
    return not conflitos_no_intervalo(
        ocupacoes(excluir_telefone), data_iso, hora, servico, duracao, ignorar_id=ignorar_id)


def horarios_livres_para_sessao(sessao, telefone=None):
    """Dos HORARIOS do catálogo, os que estão mesmo livres na data escolhida
    pelo cliente. É esta a "disponibilidade apresentada no WhatsApp": um
    horário desaparece daqui assim que é ESCOLHIDO por alguém (retenção
    temporária) ou marcado, e volta a aparecer assim que é libertado — sem
    nada em cache. `telefone` é o próprio cliente: a retenção dele não o pode
    impedir de escolher o horário que já tinha escolhido."""
    sessao = sessao or {}
    data_iso = data_iso_de_texto(sessao.get("data"))
    if not data_iso:
        return list(HORARIOS)          # ainda não há data: nada a filtrar
    _, duracao_pt, servico_pt, _ = calcular_preco_duracao(sessao)
    servico = sessao.get("servico") or servico_pt
    duracao = recuperar_duracao(servico, sessao.get("duracao") or duracao_pt)
    existentes = ocupacoes(telefone)
    livres = []
    for etiqueta in HORARIOS:
        hora = hora_hhmm_de_texto(etiqueta)
        if not hora:
            continue
        if not conflitos_no_intervalo(existentes, data_iso, hora, servico, duracao):
            livres.append(etiqueta)
    return livres


def reagendar_agendamento(id_agendamento, data_iso, hora, origem="dashboard"):
    """Move uma marcação CONFIRMADA para uma nova data/hora, preservando
    serviço, extras, duração, preço, carrinho, cliente e fotografias — só as
    colunas `data` e `hora` mudam. Guarda o histórico e tenta avisar o
    cliente. Devolve (agendamento_atualizado, cliente_notificado).

    Levanta EstadoInvalido (marcação já não confirmada) ou HorarioOcupado
    (sobreposição real com outra marcação confirmada)."""
    alvo = obter_agendamento(id_agendamento)
    if not alvo:
        raise LookupError("Marcação não encontrada.")
    if alvo["estado"] != "confirmado":
        raise EstadoInvalido(alvo["estado"])
    if conflitos_de_horario(id_agendamento, data_iso, hora):
        raise HorarioOcupado(f"{data_iso} {hora}")

    d = date.fromisoformat(data_iso)
    dias = DIAS_SEMANA["pt"]
    data_texto = f"{d.strftime('%d.%m.%Y')} ({dias[d.weekday()]})"
    hora_texto = f"🕘 {hora}"
    data_antiga, hora_antiga = alvo.get("data"), alvo.get("hora")

    with obter_bd() as conn:
        conn.execute("BEGIN IMMEDIATE")
        linha = conn.execute("SELECT estado FROM agendamentos WHERE id = ?", (id_agendamento,)).fetchone()
        if not linha or linha[0] != "confirmado":
            raise EstadoInvalido(linha[0] if linha else "inexistente")
        # a marcação continua ATIVA e confirmada, apenas na nova data/hora
        # bloqueia_horario = 1: a marcação nova resultante do reagendamento
        # ocupa o novo horário normalmente.
        conn.execute("UPDATE agendamentos SET data = ?, hora = ?, bloqueia_horario = 1 WHERE id = ?",
                     (data_texto, hora_texto, id_agendamento))
        conn.execute(
            "INSERT INTO agendamento_historico (agendamento_id, data_anterior, hora_anterior, "
            "data_nova, hora_nova, origem, alterado_em) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (id_agendamento, data_antiga, hora_antiga, data_texto, hora_texto, origem, _agora_iso()))

    agendamento = obter_agendamento(id_agendamento)
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
        # Mesma lógica central usada pelo painel (ver cancelar_agendamento).
        # Sem escolha explícita -> aplica a configuração guardada no painel
        # ("Libertar automaticamente o horário").
        try:
            _, notificado, libertado = cancelar_agendamento(id_agendamento)
        except (EstadoInvalido, LookupError):
            _responder_equipa(f"ℹ️ A marcação #{id_agendamento} já não está confirmada "
                              f"(estado atual: {obter_agendamento(id_agendamento)['estado']}).")
            return True
        _responder_equipa(f"❌ Marcação {resumo} cancelada — "
                          + ("cliente avisado." if notificado
                             else "NÃO foi possível avisar o cliente automaticamente.")
                          + ("\n🔓 Horário libertado: volta a estar disponível." if libertado
                             else "\n🔒 Horário mantido ocupado: continua a impedir novas reservas."))
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
    opcoes.append({"id": ACAO_VOLTAR, "titulo": t("botao_voltar", idioma)})
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
    ], idioma, rodape=t("rodape_padrao", idioma), com_voltar=True, com_cancelar=True,
        titulo_seccao=t("wrap_modo_seccao", idioma))


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
    ], idioma, rodape=t("rodape_wrap", idioma), com_voltar=True, com_cancelar=True,
        titulo_seccao=t("wrap_tipo_seccao", idioma))


def passo_rapido_fotos(de, idioma, sessao=None):
    enviar_botoes(de, t("rapido_fotos_corpo", idioma), [
        {"id": "wrap_fotos_sim", "titulo": t("wrap_fotos_sim_botao", idioma)},
        {"id": "wrap_fotos_nao", "titulo": t("wrap_fotos_nao_botao", idioma)},
        {"id": "ver_carrinho", "titulo": t("rapido_ver_pedido_botao", idioma)},
    ], idioma, rodape=t("rodape_wrap", idioma), com_voltar=True, com_cancelar=True,
        titulo_seccao=t("wrap_fotos_seccao", idioma))


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
    ], idioma, rodape=t("rodape_wrap", idioma), com_voltar=True,
        titulo_seccao=t("resumo_seccao", idioma))


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
    # 8 opções + Carrinho + Voltar + Cancelar passam das 10 linhas da API —
    # a lista pagina-se sozinha (ver enviar_lista), sem perder nenhuma saída.
    opcoes = opcoes_com_precos(WRAP_TIPOS_VEICULO, idioma,
                               lambda i: WRAP_VEICULO_PRECOS_CENTIMOS.get(i, 0), "acrescimo")
    enviar_lista(de, t("wrap_veiculo_corpo", idioma), t("wrap_veiculo_seccao", idioma), opcoes, idioma,
                 botao=t("wrap_veiculo_botao", idioma), com_voltar=True, com_cancelar=True,
                 rodape=t("rodape_wrap", idioma), sessao=sessao, com_rapido=True)


def pergunta_texto_livre(de, idioma, corpo, sessao=None, id_voltar=None, rodape=None,
                          titulo_seccao=None, opcoes_extra=None):
    """Pergunta que espera TEXTO LIVRE, mas apresentada como mensagem
    INTERATIVA: o cliente continua a poder escrever normalmente (o webhook
    trata na mesma a mensagem de texto seguinte), e ganha sempre saídas
    clicáveis — ⬅️ Voltar, 🛒 Carrinho (só quando há um processo/carrinho em
    curso) e ❌ Cancelar — em vez de ficar preso sem nenhuma opção.

    `id_voltar` permite um destino de Voltar próprio deste passo; por
    omissão usa o ACAO_VOLTAR normal (ver voltar_um_passo, que já desfaz
    exatamente o passo de texto livre em curso)."""
    opcoes = list(opcoes_extra or [])
    opcoes.append({"id": id_voltar or ACAO_VOLTAR, "titulo": t("botao_voltar", idioma)})
    if sessao is not None:
        opcoes.append({"id": "ver_carrinho", "titulo": t("carrinho_botao_ver", idioma)})
    opcoes.append({"id": ID_CANCELAR, "titulo": t("botao_cancelar", idioma)})
    enviar_botoes(de, corpo, opcoes, idioma, rodape=rodape,
                  titulo_seccao=titulo_seccao or t("acoes_seccao", idioma),
                  botao_lista=t("menu_botao", idioma))


def passo_wrap_veiculo_outro(de, idioma, sessao=None):
    pergunta_texto_livre(de, idioma, t("wrap_veiculo_outro_pedir", idioma), sessao=sessao,
                         rodape=t("rodape_wrap", idioma), titulo_seccao=t("wrap_veiculo_seccao", idioma))


def passo_wrap_ano(de, idioma, sessao=None):
    enviar_lista(de, t("wrap_ano_corpo", idioma), t("wrap_ano_seccao", idioma), opcoes_wrap_ano(idioma), idioma,
                 botao=t("wrap_ano_botao", idioma), com_voltar=True, rodape=t("rodape_wrap", idioma),
                 sessao=sessao, com_rapido=True)


def passo_wrap_ano_outro(de, idioma, sessao=None, corpo=None):
    """`corpo` permite repetir a MESMA pergunta interativa com uma mensagem
    de erro à frente, quando o ano escrito é inválido — nunca só texto."""
    pergunta_texto_livre(de, idioma, corpo or t("wrap_ano_outro_pedir", idioma), sessao=sessao,
                         rodape=t("rodape_wrap", idioma), titulo_seccao=t("wrap_ano_seccao", idioma))


def passo_wrap_tipo(de, idioma, sessao=None):
    opcoes = opcoes_com_precos([
        {"id": "wrap_total", "titulo": t("wrap_total_botao", idioma)},
        {"id": "wrap_parcial", "titulo": t("wrap_parcial_botao", idioma)},
    ], idioma, lambda i: WRAP_PRECOS_CENTIMOS.get(i), "estimado")
    enviar_lista(de, t("wrap_tipo_corpo", idioma), t("wrap_tipo_seccao", idioma), opcoes, idioma,
                 botao=t("wrap_tipo_botao", idioma), com_voltar=True, rodape=t("rodape_wrap", idioma),
                 sessao=sessao, com_rapido=True)


def preco_familia_cor_centimos(familia_id):
    """Só duas famílias definem já um preço nesta lista, e ambos vêm da
    tabela central WRAP_COR_PRECOS_CENTIMOS: "Criar a minha cor" (cor à
    medida, cor_personalizada_livre) e "Transparente/PPF"
    (cor_transparente_ppf, sem acréscimo -> "Incluído"). As restantes
    devolvem None — o preço aparece depois, na lista das cores."""
    if familia_id == "cf_personalizada":
        return WRAP_COR_PRECOS_CENTIMOS["cor_personalizada_livre"]
    if familia_id == "cf_transparente":
        return WRAP_COR_PRECOS_CENTIMOS["cor_transparente_ppf"]
    return None


def passo_wrap_cor_familia(de, idioma, sessao=None):
    # 8 opções (7 famílias + "Criar a minha cor"): com Carrinho/Voltar/
    # Cancelar a lista pagina-se sozinha.
    opcoes = opcoes_com_precos(WRAP_FAMILIAS_COR, idioma, preco_familia_cor_centimos, "acrescimo")
    enviar_lista(de, t("wrap_cor_familia_corpo", idioma), t("wrap_cor_familia_seccao", idioma), opcoes,
                 idioma, botao=t("wrap_cor_familia_botao", idioma), com_voltar=True, com_cancelar=True,
                 rodape=t("rodape_wrap", idioma), sessao=sessao, com_rapido=True)


def passo_wrap_cor(de, idioma, sessao=None):
    familia_id = sessao.get("wrap_cor_familia_id") if sessao else None
    cores = opcoes_com_precos(WRAP_CORES_POR_FAMILIA.get(familia_id, []), idioma,
                              lambda i: WRAP_COR_PRECOS_CENTIMOS.get(i, 0), "acrescimo")
    enviar_lista(de, t("wrap_cor_corpo", idioma), t("wrap_cor_seccao", idioma), cores, idioma,
                 botao=t("wrap_cor_botao", idioma), com_voltar=True, rodape=t("rodape_wrap", idioma),
                 sessao=sessao, com_rapido=True)


def passo_wrap_cor_personalizada(de, idioma, sessao=None):
    pergunta_texto_livre(de, idioma, t("wrap_cor_personalizada_pedir", idioma), sessao=sessao,
                         rodape=t("rodape_wrap", idioma), titulo_seccao=t("wrap_cor_familia_seccao", idioma))


def passo_wrap_acabamento(de, idioma, sessao=None):
    # 8 opções: com Carrinho/Voltar/Cancelar a lista pagina-se sozinha.
    opcoes = opcoes_com_precos(WRAP_ACABAMENTOS, idioma,
                               lambda i: WRAP_ACABAMENTO_PRECOS_CENTIMOS.get(i, 0), "acrescimo")
    enviar_lista(de, t("wrap_acabamento_corpo", idioma), t("wrap_acabamento_seccao", idioma), opcoes,
                 idioma, botao=t("wrap_acabamento_botao", idioma), com_voltar=True, com_cancelar=True,
                 rodape=t("rodape_wrap", idioma), sessao=sessao, com_rapido=True)


def passo_wrap_fotos_pergunta(de, idioma, sessao=None):
    botoes = [
        {"id": "wrap_fotos_sim", "titulo": t("wrap_fotos_sim_botao", idioma)},
        {"id": "wrap_fotos_nao", "titulo": t("wrap_fotos_nao_botao", idioma)},
    ]
    if sessao is not None:
        botoes.append({"id": "ver_carrinho", "titulo": t("carrinho_botao_ver", idioma)})
    enviar_botoes(de, t("wrap_fotos_pergunta_corpo", idioma), botoes, idioma, rodape=t("rodape_wrap", idioma),
                  com_voltar=True, com_cancelar=True, titulo_seccao=t("wrap_fotos_seccao", idioma))


def passo_wrap_fotos_a_receber(de, idioma, sessao=None, corpo=None):
    """Ecrã enquanto se espera por fotografias. Nunca é só texto: o cliente
    tem sempre ✅ Concluir pedido, ⬅️ Voltar, 🛒 Carrinho e ❌ Cancelar
    clicáveis — e continua a poder simplesmente enviar as fotografias.

    ⬅️ Voltar (ACAO_VOLTAR) deixa de aguardar fotografias e regressa à
    pergunta "Deseja enviar fotografias?", preservando as que já chegaram e
    sem criar outro pedido (ver voltar_um_passo)."""
    opcoes = [{"id": "wrap_fotos_concluir", "titulo": t("wrap_fotos_concluir_botao", idioma)},
              {"id": ACAO_VOLTAR, "titulo": t("botao_voltar", idioma)}]
    if sessao is not None:
        opcoes.append({"id": "ver_carrinho", "titulo": t("carrinho_botao_ver", idioma)})
    opcoes.append({"id": ID_CANCELAR, "titulo": t("botao_cancelar", idioma)})
    enviar_botoes(de, corpo or t("wrap_fotos_pedir", idioma), opcoes, idioma,
                  rodape=t("rodape_wrap", idioma), titulo_seccao=t("wrap_fotos_seccao", idioma),
                  botao_lista=t("menu_botao", idioma))


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
    ], idioma, rodape=t("rodape_padrao", idioma), com_voltar=True,
        titulo_seccao=t("resumo_seccao", idioma))


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
                  "rapido_interesse", "_rapido_etapa_resumo",
                  "_pagina_lista", "_pagina_chave"):
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
    # Preço mínimo de cada categoria, lido das tabelas centrais (ver
    # preco_minimo_categoria_centimos). Com Voltar + Cancelar já são 5
    # opções, por isso isto vai como lista.
    enviar_botoes(de, t("categoria_pergunta", idioma), opcoes_categorias_com_precos(idioma), idioma,
                  rodape=t("rodape_padrao", idioma), com_voltar=True, com_cancelar=True,
                  titulo_seccao=t("categoria_seccao", idioma))


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
            {"id": ID_VOLTAR_CARRINHO, "titulo": t("botao_voltar", idioma)},
        ], idioma, rodape=t("rodape_wrap", idioma),
            titulo_seccao=t("carrinho_seccao", idioma), botao_lista=t("menu_botao", idioma))
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

    # ⬅️ Voltar aqui regressa EXATAMENTE ao passo onde o cliente estava
    # antes de abrir o carrinho (ver voltar_um_passo/reenviar_passo_atual).
    enviar_botoes(de, "\n".join(linhas), [
        {"id": "carrinho_continuar", "titulo": t("botao_continuar", idioma)},
        {"id": "carrinho_alterar", "titulo": t("carrinho_botao_alterar", idioma)},
        {"id": "carrinho_esvaziar", "titulo": t("carrinho_botao_esvaziar", idioma)},
        {"id": ID_VOLTAR_CARRINHO, "titulo": t("botao_voltar", idioma)},
    ], idioma, titulo_seccao=t("carrinho_seccao", idioma), botao_lista=t("menu_botao", idioma))


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
                  botoes_marcacao_carrinho(agendamento, idioma), idioma, com_voltar=True,
                  titulo_seccao=t("carrinho_seccao", idioma))


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
                 t("carrinho_marcacoes_seccao", idioma), opcoes, idioma, botao=t("menu_botao", idioma),
                 com_voltar=True, com_cancelar=False)


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

    enviar_lista(de, "\n".join(linhas), t("mais_acoes_seccao", idioma), opcoes, idioma,
                 botao=t("menu_botao", idioma), com_voltar=True, com_cancelar=False)


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
                 botao=t("menu_botao", idioma), com_voltar=True, com_cancelar=True)


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


def passo_orcamento_generico(de, idioma, sessao=None):
    # Voltar aqui regressa ao menu principal (ver voltar_um_passo: o fluxo
    # "orcamento" não tem passo anterior dentro de si).
    pergunta_texto_livre(de, idioma, t("orcamento_pedido", idioma), sessao=sessao,
                         rodape=t("rodape_padrao", idioma), titulo_seccao=t("acoes_seccao", idioma))


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
                {"WWW-Authenticate": 'Basic realm="Painel Daniela Nails"'},
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
    pedido = pedidos_por_agendamento().get(id_agendamento)
    corpo = {
        "ok": True,
        "cliente_notificado": bool(notificado),
        "agendamento": ag,
        "evento": evento_calendario(ag, pedido) if ag else None,
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


@app.route("/media/<path:nome_ficheiro>", methods=["GET"])
@requer_autenticacao
def media(nome_ficheiro):
    return send_from_directory(MEDIA_DIR, nome_ficheiro)


def _escapar_html(texto):
    """Escape mínimo para injetar texto de configuração no HTML do painel."""
    return (str(texto).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;").replace("'", "&#39;"))


def dashboard_html():
    """HTML do painel com a identidade do negócio já substituída. O nome vem
    sempre de BUSINESS_NAME (variável de ambiente), nunca escrito à mão."""
    return DASHBOARD_HTML.replace("{{BUSINESS_NAME}}", _escapar_html(BUSINESS_NAME))


@app.route("/dashboard", methods=["GET"])
@requer_autenticacao
def dashboard():
    return dashboard_html()


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
<title>{{BUSINESS_NAME}} — Agenda de Teste</title>
<style>
  :root{
    --bg:#0d0f12; --panel:#15181d; --panel2:#1b1f26; --border:#262b33;
    /* Cor principal da marca (Daniela Nails). O nome da variável mantém-se
       --gold para não ter de tocar nas ~90 utilizações espalhadas pela CSS
       e pelo JavaScript; o que muda é o valor. */
    --gold:#e454a0; --text:#f2f3f5; --muted:#9aa1ac;
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
  .marca{color:var(--gold);font-weight:700;letter-spacing:.2px;margin-left:0.375rem;text-transform:none;}
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
      <h2>📅 Calendário <span class="marca">{{BUSINESS_NAME}}</span></h2>
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
    <h2>Agendamentos</h2>
    <div id="conteudo"><div class="vazio">A carregar…</div></div>
  </div>

  <!-- Área de pedidos de orçamento (Wrap & Proteção): OCULTA nesta versão.
       O bloco fica no HTML, e a rota /api/pedidos, as tabelas e as migrations
       continuam todas a funcionar — só deixou de ser mostrada. Basta remover
       o atributo hidden para a ter de volta. -->
  <div class="lista" style="margin-top:14px;" id="painel-pedidos-wrap" hidden>
    <h2>Pedidos de orçamento (Wrap &amp; Proteção)</h2>
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

  let html = '<table><thead><tr><th>Cliente</th><th>Serviço</th><th>Data</th><th>Hora</th><th>Preço</th><th>Estado</th><th>Horário</th><th>Recebido em</th></tr></thead><tbody>';
  dados.forEach(d => {
    const criado = d.criado_em ? new Date(d.criado_em).toLocaleString('pt-PT') : '-';
    const classeEstado = d.estado !== 'confirmado' ? 'estado-cancelado' : '';
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
      <td>${d.preco ? 'CHF '+esc(d.preco) : '-'}</td>
      <td class="${classeEstado}">${esc(d.estado)}</td>
      <td>${horario}</td>
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
  {id: 'confirmado', nome: 'Confirmado', cor: '#3878e8', classe: 'est-confirmado'},
  {id: 'concluido',  nome: 'Concluído',  cor: '#2ea05a', classe: 'est-concluido'},
  {id: 'reagendado', nome: 'Reagendado', cor: '#9678c8', classe: 'est-reagendado'},
  {id: 'cancelado',  nome: 'Cancelado',  cor: '#e05252', classe: 'est-cancelado',
   rotuloFiltro: 'Cancelados (horário livre)'},
];
// Cor do indicador "Agora" — laranja quente, deliberadamente DIFERENTE do
// vermelho dos cancelamentos, para nunca se confundirem.
const COR_AGORA = '#ff7a59';

// --- Estado da marcação vs. disponibilidade do horário ---------------------
// São duas coisas distintas e são sempre comunicadas em separado: a cor diz
// o SERVIÇO, o texto diz o ESTADO, e um terceiro texto (com ícone e borda
// próprios) diz se o horário está BLOQUEADO ou LIVRE.
function evCancelado(ev){
  return chaveEstado(ev.estado) === 'cancelado';
}
function evBloqueiaHorario(ev){
  if(typeof ev.bloqueia_horario === 'boolean') return ev.bloqueia_horario;
  const chave = chaveEstado(ev.estado);
  if(chave === 'confirmado' || chave === 'concluido') return true;
  if(chave === 'cancelado') return Number(ev.bloqueia_horario || 0) === 1;
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
let calFiltros = {confirmado: true, concluido: true, reagendado: false, cancelado: false};
let calEventos = [];                              // eventos do intervalo atual
const calPorId = new Map();                       // cache id -> evento (dossiê)

// "concluído" chega da BD com acento; a chave dos filtros/classes não tem.
function chaveEstado(estado){
  const limpo = String(estado || '').normalize('NFD').replace(/[\u0300-\u036f]/g, '').toLowerCase();
  return CAL_ESTADOS.some(e => e.id === limpo) ? limpo : 'confirmado';
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
      '<label class="cal-filtro" title="' + esc(e.id === 'cancelado'
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
  const total = ev.total_centimos ? formatarCentimos(ev.total_centimos) : '';
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
  const podeAgir = chaveEstado(ev.estado) === 'confirmado';
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
  if(chaveEstado(ev.estado) === 'confirmado'){
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
    return jsonify(versao="daniela-v1.0-alpha", negocio=BUSINESS_NAME,
                   fluxos=["maos", "pes"], fluxos_ocultos=["wrap"],
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
            # desfazer a escolha da hora devolve logo o horário ao mercado
            libertar_horario_retido(de)
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
            # Do passo do tamanho/estado, VOLTAR regressa à escolha do
            # SERVIÇO (não salta já para as categorias). Remove a linha do
            # serviço base e, por dependência, o acréscimo de tamanho/estado
            # — que é calculado a partir dela.
            sessao.pop("tipo_id", None)
            carrinho_remover_grupo(sessao, GRUPO_SERVICO_BASE)
            carrinho_remover_grupo(sessao, GRUPO_TAMANHO_VEICULO)
            guardar_sessao(de, sessao)
            (passo_limpeza_tipo if categoria == "cat_limpeza" else passo_estetica_servico)(de, idioma, sessao)
        else:
            # Do passo do SERVIÇO, VOLTAR regressa à escolha da categoria.
            sessao.pop("categoria", None)
            iniciar_escolha_categoria(de, idioma, sessao)
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

        # IDEMPOTÊNCIA: a Meta reenvia o mesmo webhook se não receber o 200 a
        # tempo. Uma mensagem já tratada é reconhecida e devolvida em silêncio
        # — sem repetir o passo, sem responder outra vez ao cliente e, acima
        # de tudo, sem duplicar marcações.
        if mensagem_ja_processada(msg.get("id")):
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
                    # Repete a MESMA pergunta interativa (com Voltar/Cancelar),
                    # nunca só uma mensagem de texto sem saída.
                    passo_wrap_ano_outro(de, idioma, sessao,
                                         corpo=t("wrap_ano_invalido", idioma) + "\n\n"
                                               + t("wrap_ano_outro_pedir", idioma))
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
                    passo_wrap_fotos_a_receber(de, idioma, sessao,
                                               corpo=t("wrap_foto_formato_invalido", idioma))
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
                ACAO_CARRINHO: "ver_carrinho",
                ACAO_CANCELAR: ID_CANCELAR,
                ACAO_RAPIDO: "modo_rapido",
            }.get(id_botao, id_botao)

            if id_botao == ACAO_VOLTAR:
                voltar_um_passo(de, idioma, sessao)
                return jsonify(status="ok"), 200

            # Voltar da descrição livre de "Outra alteração" -> lista de
            # aspetos do MESMO orçamento (verificado ANTES do prefixo
            # genérico "orcamento_alt_", que também lhe serve de prefixo).
            if id_botao.startswith(ID_ALT_VOLTAR):
                sessao.pop("_aguardando_alteracao_orcamento_id", None)
                guardar_sessao(de, sessao)
                try:
                    mostrar_lista_alteracao_orcamento(de, idioma, int(id_botao[len(ID_ALT_VOLTAR):]))
                except ValueError:
                    nao_entendi_com_opcoes(de, idioma, sessao)
                return jsonify(status="ok"), 200

            if id_botao == ID_VOLTAR_CARRINHO:
                # Voltar a partir do carrinho: regressa ao passo onde o
                # cliente estava, sem desfazer nenhuma escolha.
                reenviar_passo_atual(de, idioma, sessao)
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
                if id_botao == CATEGORIA_WRAP_OCULTA["id"]:
                    # Categoria oculta nesta versão (ver CATEGORIA_WRAP_OCULTA).
                    # Continua a existir na base de dados e no painel para as
                    # marcações antigas, mas não se pode entrar nela por aqui.
                    nova = reiniciar_sessao(de)
                    enviar_menu_principal(de, idioma, saudacao=False, sessao=nova)
                    return jsonify(status="ok"), 200
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
                passo_wrap_fotos_a_receber(de, idioma, sessao)
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
                # Um "Confirmar" de uma mensagem antiga pode chegar quando a
                # sessão já foi reiniciada (marcação feita, processo cancelado).
                # Sem serviço, data e hora não há nada para gravar: em vez de
                # rebentar contra a base de dados, volta-se ao menu.
                if not (sessao.get("servico") and sessao.get("data") and sessao.get("hora")):
                    nova = reiniciar_sessao(de)
                    enviar_menu_principal(de, idioma, saudacao=False, sessao=nova)
                    return jsonify(status="ok"), 200
                # Última verificação, atómica com a gravação: entre o resumo e
                # este clique o horário pode ter sido ocupado por outro
                # cliente. Nesse caso nada é gravado e volta-se ao passo da
                # hora, já sem o horário que entretanto desapareceu.
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
                # Em vez de mandar escrever comandos no próprio texto: botões.
                enviar_botoes(de, t("e_agora_pergunta", idioma), [
                    {"id": ACAO_GERIR, "titulo": t("botao_gerir_marcacao", idioma)},
                    {"id": ACAO_MENU, "titulo": t("botao_menu_principal", idioma)},
                ], idioma)
                enviar_notificacao_interna_marcacao(id_ag, mensagem_notificacao_provider(de, sessao, id_ag))
                reiniciar_sessao(de)
                return jsonify(status="ok"), 200

            if id_botao == "alterar":
                libertar_horario_retido(de)     # a hora vai ser reescolhida
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
                # A decisão "libertar ou manter o horário" é do NEGÓCIO: aqui
                # aplica-se em silêncio a configuração guardada no painel e
                # nunca se pergunta nada ao cliente.
                try:
                    libertado = marcar_agendamento_cancelado(id_ag, exigir_confirmado=False)
                except LookupError:
                    libertado = None
                enviar_texto(de, t("cancelado_cliente", idioma))
                if PROVIDER_WHATSAPP:
                    estado_horario = ("🔓 Horário libertado." if libertado
                                      else "🔒 Horário mantido ocupado." if libertado is False else "")
                    enviar_texto(PROVIDER_WHATSAPP,
                                 f"❌ Marcação #{id_ag} cancelada pelo cliente {formatar_telefone(de)}."
                                 + (f"\n{estado_horario}" if estado_horario else ""))
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
                enviar_seletor_idioma(de, idioma)
                return jsonify(status="ok"), 200

            if id_escolhido == ACAO_MAIS:
                mostrar_mais_acoes(de, idioma, sessao)
                return jsonify(status="ok"), 200

            # Menu principal
            if id_escolhido == "mp_marcar":
                iniciar_escolha_categoria(de, idioma, sessao)
                return jsonify(status="ok"), 200
            if id_escolhido == "mp_orcamento":
                # Nesta versão o pedido de orçamento não está no menu público.
                # O ID pode na mesma chegar de uma mensagem antiga que o
                # cliente ainda tenha na conversa: em vez de arrancar um fluxo
                # que já não existe aqui, volta-se em segurança ao menu.
                nova = reiniciar_sessao(de)
                enviar_menu_principal(de, idioma, saudacao=False, sessao=nova)
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
                    # Voltar aqui regressa à LISTA de aspetos do MESMO
                    # orçamento (ver ID_ALT_VOLTAR), nunca ao menu.
                    pergunta_texto_livre(de, idioma, t("alteracao_outra_pedir", idioma),
                                         id_voltar=f"{ID_ALT_VOLTAR}{orcamento_id}",
                                         titulo_seccao=t("alteracao_seccao", idioma))
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
                        passo_wrap_veiculo_outro(de, idioma, sessao)
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
                        passo_wrap_ano_outro(de, idioma, sessao)
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
                        passo_wrap_cor_personalizada(de, idioma, sessao)
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
                    # A partir daqui o horário fica RETIDO em nome deste
                    # cliente: deixa de ser oferecido a mais ninguém enquanto
                    # ele revê e confirma (ver reter_horario).
                    reter_horario(de, sessao)
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
                    # A partir daqui o horário fica RETIDO em nome deste
                    # cliente: deixa de ser oferecido a mais ninguém enquanto
                    # ele revê e confirma (ver reter_horario).
                    reter_horario(de, sessao)
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
                passo_wrap_fotos_a_receber(de, idioma, sessao,
                                           corpo=t("wrap_foto_formato_invalido", idioma))
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
                # Enquanto não chegar às 5, volta sempre a mostrar as mesmas
                # opções clicáveis (Concluir / Voltar / Carrinho / Cancelar).
                corpo = (t("wrap_foto_recebida_contagem", idioma, atual=total_fotos, total=5)
                         + "\n\n" + t("wrap_fotos_mais_ou_concluir", idioma))
                passo_wrap_fotos_a_receber(de, idioma, sessao, corpo=corpo)
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

    # Ecrã de escolha da categoria: é aqui que o cliente estava se abriu o
    # carrinho logo no início (ver ID_VOLTAR_CARRINHO).
    if fluxo == "escolher_categoria" and not categoria:
        enviar_lista(de, t("categoria_pergunta", idioma), t("categoria_seccao", idioma),
                     opcoes_categorias_com_precos(idioma), idioma, botao=t("menu_botao", idioma),
                     com_voltar=True, com_cancelar=True, rodape=t("rodape_padrao", idioma))
        return

    if fluxo == "wrap" and sessao.get("wrap_modo") == MODO_RAPIDO:
        if sessao.get("_rapido_etapa_resumo"):
            passo_rapido_resumo(de, idioma, sessao)
        elif sessao.get("aguardando_fotos"):
            passo_wrap_fotos_a_receber(de, idioma, sessao)
        elif "rapido_interesse" in sessao:
            passo_rapido_fotos(de, idioma, sessao)
        else:
            passo_rapido_interesse(de, idioma, sessao)
        return

    if fluxo == "wrap":
        if sessao.get("_wrap_etapa_resumo"):
            passo_wrap_resumo(de, idioma, sessao)
        elif sessao.get("aguardando_fotos"):
            passo_wrap_fotos_a_receber(de, idioma, sessao)
        elif "wrap_acabamento" in sessao:
            passo_wrap_fotos_pergunta(de, idioma, sessao)
        elif sessao.get("_wrap_aguardando_cor_texto"):
            passo_wrap_cor_personalizada(de, idioma, sessao)
        elif "wrap_cor" in sessao:
            passo_wrap_acabamento(de, idioma, sessao)
        elif "wrap_cor_familia" in sessao:
            passo_wrap_cor(de, idioma, sessao)
        elif "wrap_tipo" in sessao:
            passo_wrap_cor_familia(de, idioma, sessao)
        elif sessao.get("_wrap_aguardando_ano_texto"):
            passo_wrap_ano_outro(de, idioma, sessao)
        elif "wrap_ano" in sessao:
            passo_wrap_tipo(de, idioma, sessao)
        elif sessao.get("_wrap_aguardando_veiculo_texto"):
            passo_wrap_veiculo_outro(de, idioma, sessao)
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
