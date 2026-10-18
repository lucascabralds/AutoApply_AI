"""
formatacao.py — AutoApply AI
=============================
Pequenos helpers de apresentação compartilhados por main.py, briefing,
dashboard e candidatura (antes cada um tinha sua cópia).
"""


def formatar_reais(valor) -> str:
    """49.9 → 'R$ 49,90'. Texto livre (registros antigos) é devolvido como veio."""
    if valor in (None, ""):
        return "—"
    try:
        n = float(valor)
    except (TypeError, ValueError):
        return str(valor)
    return "R$ " + f"{n:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


_EMPRESA_ANONIMA = ("não identificada", "nao identificada", "empresa confidencial", "confidencial",
                    "sigilosa", "empresa sigilosa")


def empresa_conhecida(empresa: str | None) -> bool:
    """
    False para vazio, para o placeholder "Não identificada" que o pipeline grava
    e para anúncios anônimos ("Empresa Confidencial" na Catho). Isso importa na
    deduplicação: duas vagas "Assistente de Compras @ Empresa Confidencial" são,
    quase sempre, de empresas diferentes — o link da vaga é que as distingue.
    """
    return bool(empresa) and not empresa.strip().lower().startswith(_EMPRESA_ANONIMA)
