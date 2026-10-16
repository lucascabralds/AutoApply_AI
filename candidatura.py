"""
candidatura.py — AutoApply AI
==============================
Fase 3 do roadmap, em versão "humano no controle": para vagas de score alto,
a IA escreve uma carta de apresentação baseada no perfil profissional e o
pipeline cria um RASCUNHO no Gmail — com o currículo em PDF anexado e, se o
e-mail do recrutador aparecer na vaga, já endereçado. Nada é enviado
sozinho: você revisa e clica em Enviar.

Quando a vaga só aceita candidatura pela plataforma (Catho, Indeed...), o
rascunho fica sem destinatário e serve de texto pronto para colar no
formulário, junto com o link da vaga.

Configuração (.env):
  RASCUNHOS_CANDIDATURA=true   liga o recurso (padrão: false)
  SCORE_RASCUNHO=80            score mínimo para gerar rascunho
  CURRICULO_PDF=/caminho/cv.pdf  currículo anexado (opcional)
  PERFIL_PATH=...              perfil usado pela IA (padrão:
                               services/perfil_profissional_lucas.md)
"""

import base64
import os
import re
from email.message import EmailMessage

from dotenv import load_dotenv

load_dotenv()

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))

RASCUNHOS_ATIVOS = os.getenv("RASCUNHOS_CANDIDATURA", "false").strip().lower() in ("1", "true", "sim", "yes")
SCORE_RASCUNHO   = int(os.getenv("SCORE_RASCUNHO", "80"))
CURRICULO_PDF    = os.getenv("CURRICULO_PDF", "").strip()
PERFIL_PATH      = os.getenv("PERFIL_PATH") or os.path.join(_BASE_DIR, "services", "perfil_profissional_lucas.md")

# Endereços que nunca são de um recrutador de verdade
_REMETENTES_GENERICOS = re.compile(
    r"(no-?reply|do-?not-?reply|noreply|naoresponda|nao-responda|mailer|notifica|newsletter|"
    r"marketing|news@|alerts?@|avisovagas|jobs-noreply|support|suporte|privacidade|privacy|sac@)",
    re.IGNORECASE,
)
_DOMINIOS_PLATAFORMA = (
    "catho.com.br", "indeed.com", "linkedin.com", "infojobs.com.br", "gupy.io", "vagas.com",
    "jobbol.com.br", "glassdoor.com", "trampos.co", "programathor.com.br", "pandape.com.br",
)
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def carregar_perfil(path: str = PERFIL_PATH) -> str:
    """
    Lê o perfil profissional SEM as seções marcadas como uso interno
    ("Pontos de Atenção"), que nunca podem chegar a um recrutador.
    """
    try:
        with open(path, encoding="utf-8") as f:
            texto = f.read()
    except OSError:
        return ""
    secoes = re.split(r"(?m)^(?=## )", texto)
    publicas = [s for s in secoes if not re.search(r"uso interno|pontos de aten", s.split("\n", 1)[0], re.IGNORECASE)
                and "não deve ser compartilhada" not in s]
    return "".join(publicas).strip()


def extrair_email_recrutador(texto: str) -> str | None:
    """Primeiro e-mail do corpo que parece de uma pessoa/empresa (não de plataforma ou no-reply)."""
    for email in _EMAIL_RE.findall(texto or ""):
        email = email.strip(".").lower()
        dominio = email.rsplit("@", 1)[-1]
        if _REMETENTES_GENERICOS.search(email):
            continue
        if any(dominio == d or dominio.endswith("." + d) for d in _DOMINIOS_PLATAFORMA):
            continue
        if re.search(r"\.(png|jpe?g|gif|webp)$", email):   # "logo@2x.png"
            continue
        return email
    return None


def deve_gerar_rascunho(vaga: dict) -> bool:
    return (
        RASCUNHOS_ATIVOS
        and vaga.get("tipo_email", "nova_vaga") == "nova_vaga"
        and int(vaga.get("score_gemini") or 0) >= SCORE_RASCUNHO
    )


def montar_mensagem(vaga: dict, carta: str, destinatario: str | None,
                    curriculo_pdf: str | None = None) -> EmailMessage:
    msg = EmailMessage()
    empresa = vaga.get("empresa")
    empresa_ok = empresa and not empresa.lower().startswith("não identificada")
    msg["Subject"] = f"Candidatura — {vaga.get('cargo')}" + (f" | {empresa}" if empresa_ok else "")
    if destinatario:
        msg["To"] = destinatario

    corpo = carta.strip()
    if not destinatario and vaga.get("link_candidatura"):
        corpo += (
            "\n\n---\n[Nota do AutoApply AI — apague antes de enviar]\n"
            "Esta vaga é pela plataforma; use o texto acima no formulário:\n"
            f"{vaga['link_candidatura']}"
        )
    msg.set_content(corpo)

    if curriculo_pdf and os.path.isfile(curriculo_pdf):
        with open(curriculo_pdf, "rb") as f:
            msg.add_attachment(f.read(), maintype="application", subtype="pdf",
                               filename=os.path.basename(curriculo_pdf))
    return msg


def criar_rascunho(service, msg: EmailMessage) -> str | None:
    """Cria o rascunho no Gmail (escopo gmail.modify já cobre drafts). Retorna o id."""
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    rascunho = service.users().drafts().create(userId="me", body={"message": {"raw": raw}}).execute()
    return rascunho.get("id")
