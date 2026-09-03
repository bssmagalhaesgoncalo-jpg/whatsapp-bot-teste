"""
Catálogo de serviços — FONTE ÚNICA DE VERDADE.

Antes, os serviços viviam espalhados por vários dicionários (LIMPEZA_TIPOS,
ESTETICA_SERVICOS, CORES_SERVICOS, factores de preço, ...). Agora há UMA lista
`SERVICOS_SEED` e uma tabela `servicos` na base de dados semeada a partir dela.

  • O BOT, o RESUMO, a DISPONIBILIDADE, o CALENDÁRIO e o DASHBOARD leem todos
    da tabela `servicos` (via `db.listar_servicos()` / `db.obter_servico()`).
  • `SERVICOS_SEED` é só o valor inicial (semente) e o fallback em memória.
  • A Fase 4 (CRUD no dashboard) altera a tabela; o código não precisa de
    mudar — continua a ler da tabela.

Campos de um serviço:
    id            slug estável, nunca traduzido, nunca muda (chave de tudo)
    nome_pt/de/en nome visível ao cliente em cada idioma
    duracao_min   int — minutos REAIS que bloqueiam a agenda
    preco_cents   int OU None — None = "preço a confirmar" (ver regras abaixo)
    ativo         bool — inativo não aparece ao cliente, mas continua a
                  resolver marcações antigas
    cor           cor estável no calendário do painel

REGRA DO PREÇO A CONFIRMAR (preco_cents = None):
    • cliente vê "Preço a confirmar" (traduzido), nunca "CHF 0"
    • nunca se soma 0 ao total: o total também fica "a confirmar"
    • dashboard mostra "A confirmar"
    • notificação interna diz explicitamente que o preço ainda não foi definido
"""

from __future__ import annotations

MOEDA = "CHF"

# Rótulo de "preço a confirmar" nos 3 idiomas (alemão sempre com "ss").
PRECO_A_CONFIRMAR = {
    "pt": "Preço a confirmar",
    "de": "Preis auf Anfrage",
    "en": "Price on request",
}

# Rótulo curto para o dashboard (sempre PT — idioma de trabalho da equipa).
PRECO_A_CONFIRMAR_PAINEL = "A confirmar"


# ---------------------------------------------------------------------------
# OS 5 SERVIÇOS DA DANIELA BEAUTY — decisões fechadas.
# NÃO inventar os 3 preços em falta: brow_lamination / pestanas / dermaplaning
# têm preco_cents = None de propósito.
# ---------------------------------------------------------------------------
SERVICOS_SEED = [
    {
        "id": "limpeza_pele",
        "nome_pt": "Limpeza de pele",
        "nome_de": "Gesichtsreinigung",
        "nome_en": "Facial cleansing",
        "duracao_min": 60,
        "preco_cents": 8000,          # CHF 80
        "ativo": True,
        "cor": "#d1478f",
    },
    {
        "id": "design_sobrancelhas",
        "nome_pt": "Design de sobrancelhas",
        "nome_de": "Augenbrauen-Design",
        "nome_en": "Eyebrow design",
        "duracao_min": 30,
        "preco_cents": 2500,          # CHF 25
        "ativo": True,
        "cor": "#a45cc4",
    },
    {
        "id": "brow_lamination",
        "nome_pt": "Brow Lamination",
        "nome_de": "Brow Lamination",
        "nome_en": "Brow lamination",
        "duracao_min": 60,
        "preco_cents": None,          # a confirmar
        "ativo": True,
        "cor": "#6f5ae0",
    },
    {
        "id": "pestanas",
        "nome_pt": "Pestanas",
        "nome_de": "Wimpern",
        "nome_en": "Lashes",
        "duracao_min": 120,
        "preco_cents": None,          # a confirmar
        "ativo": True,
        "cor": "#20a4b8",
    },
    {
        "id": "dermaplaning",
        "nome_pt": "Dermaplaning",
        "nome_de": "Dermaplaning",
        "nome_en": "Dermaplaning",
        "duracao_min": 60,
        "preco_cents": None,          # a confirmar
        "ativo": True,
        "cor": "#2ea05a",
    },
]

COR_OMISSAO = "#8b95a6"          # cinzento-azulado, serviços desconhecidos/legados

# IDs válidos (semente). A verdade em runtime é a tabela; isto serve de
# validação rápida e de fallback offline (ex.: testes de catálogo).
IDS_SEED = {s["id"] for s in SERVICOS_SEED}


def _idx(idioma: str) -> str:
    return idioma if idioma in ("pt", "de", "en") else "pt"


def nome(servico: dict, idioma: str) -> str:
    """Nome visível ao cliente. `servico` é uma linha da tabela OU do seed."""
    if not servico:
        return "?"
    return servico.get(f"nome_{_idx(idioma)}") or servico.get("nome_pt") or servico.get("id") or "?"


def nome_pt(servico: dict) -> str:
    """Nome CANÓNICO (português) — o que se grava em `agendamentos.servico`
    para compatibilidade com o calendário/painel legados."""
    return (servico or {}).get("nome_pt") or (servico or {}).get("id") or "?"


def tem_preco(servico: dict) -> bool:
    return servico is not None and servico.get("preco_cents") is not None


def preco_cents(servico: dict):
    return (servico or {}).get("preco_cents")


def formatar_cents(cents, idioma: str = "pt") -> str:
    """00 -> "CHF 80.00" / "CHF 80,00". None -> "Preço a confirmar" traduzido."""
    if cents is None:
        return PRECO_A_CONFIRMAR[_idx(idioma)]
    valor = f"{cents / 100:.2f}"
    if _idx(idioma) == "en":
        return f"{MOEDA} {valor}"
    return f"{MOEDA} {valor.replace('.', ',')}"


def preco_label(servico: dict, idioma: str = "pt") -> str:
    return formatar_cents(preco_cents(servico), idioma)


def preco_label_painel(servico: dict) -> str:
    cents = preco_cents(servico)
    return PRECO_A_CONFIRMAR_PAINEL if cents is None else formatar_cents(cents, "pt")


def duracao_label(minutos, idioma: str = "pt") -> str:
    """90 -> "1h30". 30 -> "30 min". 120 -> "2h". Igual nos 3 idiomas
    (formato compacto, sem palavras)."""
    try:
        minutos = int(minutos)
    except (TypeError, ValueError):
        return "-"
    if minutos <= 0:
        return "-"
    horas, resto = divmod(minutos, 60)
    if horas and resto:
        return f"{horas}h{resto:02d}"
    if horas:
        return f"{horas}h"
    return f"{resto} min"


def cor(servico: dict) -> str:
    return (servico or {}).get("cor") or COR_OMISSAO
