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


def empresa_conhecida(empresa: str | None) -> bool:
    """False para vazio e para o placeholder "Não identificada" que o pipeline grava."""
    return bool(empresa) and not empresa.strip().lower().startswith("não identificada")
