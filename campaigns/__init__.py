"""
campaigns/ — campanhas WhatsApp de reativação de clientes (P5).

Segmentação (quem recebe) + rascunho/agendamento/envio (campaigns/engine.py),
reaproveitando sempre a infraestrutura já existente:
  • messaging/whatsapp.py — ponto único de saída (proteção DEMO incluída).
  • notifications/jobs.py (automation_jobs) — o MESMO executor genérico do
    P0/P1/P2/P4.1, nunca um scheduler paralelo.
  • core/events.py — a MESMA outbox de eventos de domínio.
  • o fluxo de marcação normal do bot — "Marcar agora" nunca cria uma
    marcação diretamente, só entra no fluxo já existente.

Ver campaigns/engine.py para a lógica; bot.py só regista os handlers e
expõe as rotas /api/campanhas.
"""
