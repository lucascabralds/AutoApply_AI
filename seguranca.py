"""
seguranca.py — AutoApply AI
============================
Helpers para nunca persistir/reenviar segredos extraídos dos e-mails.
"""

import urllib.parse


def redigir_segredo(codigo: str | None) -> str | None:
    """
    Versão segura de um OTP / link de acesso para guardar em log:
      - links (recuperação de senha, login mágico) → só o domínio
      - códigos → só os 2 últimos caracteres ("••••42")
    Um link de redefinição de senha guardado no banco (e reenviado no
    briefing por e-mail) é uma credencial válida nas mãos de quem o ler.
    """
    if not codigo:
        return None
    codigo = str(codigo).strip()
    if codigo.startswith(("••••", "link: ")):
        return codigo   # já redigido
    if codigo.lower().startswith(("http://", "https://")):
        host = urllib.parse.urlparse(codigo).netloc or "?"
        return f"link: {host}"
    return "••••" + codigo[-2:] if len(codigo) > 2 else "••••"
