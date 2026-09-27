# whatsapp-bot-render — visão geral

Bot de marcações por WhatsApp + painel de gestão para a **Daniela Beauty**
(Daniela Almeida, estética — brow, lash e skin care, Visp, Suíça).
Cliente real e única. O negócio é dela; ela trabalha sozinha.

- **Stack**: Flask 3 + SQLite (WAL), gunicorn 1 worker / 8 threads, Render.
- **Idiomas**: PT, DE, EN — a cliente escolhe na primeira interação e fica guardado.
- **Fuso**: Europe/Zurich em todo o lado no servidor (`TZ` no render.yaml).
- **Dinheiro**: SEMPRE cêntimos inteiros. Nunca float.

## Superfícies
- `/webhook` — WhatsApp Cloud API (Meta). HMAC-SHA256 obrigatório em produção.
- `/app` — o painel V3 (SPA). **É a única UI viva.**
- `/painel`, `/dashboard` — UIs ANTIGAS, ainda servidas. Para desligar (bloco 2).
- `/faturas/pdf/<token>` — download público por token, sem auth de painel.

## Mapa de módulos
| Onde | O quê |
|---|---|
| `bot.py` | 7.5k linhas: rotas, fluxo conversacional, painel legado. Monólito. |
| `db.py` | Ligação + migrações numeradas + CRM (customers) |
| `billing/` | Faturação e pagamentos (`engine.py`), PDF (`pdf.py`) |
| `campaigns/` | Motor de campanhas WhatsApp |
| `notifications/` | Reminders 24h, follow-up, pós-atendimento, reagendamento, jobs |
| `scheduling/` | Disponibilidade e horários |
| `operations/` | Estado operacional do atendimento (arrived/in_progress/done) |
| `reports/` | Resultados (magro — a expandir no bloco 4) |
| `dashboard/` | Blueprint do painel V3, montado em `/app` |
| `static/dashboard/` | `app.js` (140 KB) + `app.css`. Sem framework, sem build. |

**Código novo nasce nos módulos, nunca no `bot.py`.**
