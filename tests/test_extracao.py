import base64
import json

import main


def _b64url(texto: str) -> str:
    return base64.urlsafe_b64encode(texto.encode()).decode().rstrip("=")


# ── Corpo do e-mail ──────────────────────────────────────────────────────────

def test_extrai_corpo_e_html_de_multipart_aninhado():
    payload = {
        "mimeType": "multipart/mixed",
        "parts": [
            {
                "mimeType": "multipart/alternative",
                "parts": [
                    {"mimeType": "text/plain", "body": {"data": _b64url("Olá Lucas")}},
                    {"mimeType": "text/html", "body": {"data": _b64url('<a href="https://x.com/v">Ver vaga</a>')}},
                ],
            },
            {"mimeType": "application/pdf", "filename": "cert.pdf", "body": {"attachmentId": "abc"}},
        ],
    }
    corpo, html = main._extrair_corpo_e_html(payload)
    assert corpo == "Olá Lucas"
    assert "https://x.com/v" in html


def test_limpar_html_separa_celulas_de_tabela():
    texto = main._normalizar(main._limpar_html("<table><tr><td>Analista</td><td>HIRING</td></tr></table>"))
    assert "Analista\n" in texto and "HIRING" in texto


# ── Links ────────────────────────────────────────────────────────────────────

def test_resolver_link_real_desembrulha_tracking_da_catho():
    destino = "https://www.catho.com.br/vagas/dev-python/123"
    payload = base64.urlsafe_b64encode(json.dumps({"r": destino}).encode()).decode().rstrip("=")
    href = f"https://www.catho.com.br/channel-tracking/v1/clicked?i={payload}"
    assert main._resolver_link_real(href) == destino


def test_extrair_ancoras_ignora_descadastro_e_duplicados():
    html = """
      <a href="https://site.com/vaga/1">Desenvolvedor Python</a>
      <a href="https://site.com/vaga/1">Desenvolvedor Python</a>
      <a href="https://site.com/unsubscribe">Sair</a>
      <a href="https://site.com/x">Cancelar inscrição</a>
      <a href="mailto:a@b.com">mail</a>
    """
    ancoras = main._extrair_ancoras(html)
    assert ancoras == [{"texto": "Desenvolvedor Python", "url": "https://site.com/vaga/1"}]


def test_achar_ancora_por_cargo_nao_reutiliza_link():
    ancoras = [
        {"texto": "Analista de Dados Jr", "url": "https://a"},
        {"texto": "Desenvolvedor Python Pleno", "url": "https://b"},
    ]
    usadas: set = set()
    assert main._achar_ancora_por_cargo("Desenvolvedor Python", ancoras, usadas) == "https://b"
    assert main._achar_ancora_por_cargo("Desenvolvedor Python", ancoras, usadas) is None
    assert main._achar_ancora_por_cargo("Analista de Dados Jr", ancoras, usadas) == "https://a"


def test_link_candidatura_prioriza_texto_de_acao():
    ancoras = [
        {"texto": "Logo", "url": "https://empresa.com"},
        {"texto": "Candidatar-se agora", "url": "https://empresa.com/apply"},
    ]
    assert main._extrair_link_candidatura("", ancoras=ancoras) == "https://empresa.com/apply"


def test_link_candidatura_sem_ancoras_descarta_tracking():
    corpo = "Veja: https://click.mailchimp.com/track/1 e https://gupy.io/jobs/42"
    assert main._extrair_link_candidatura(corpo) == "https://gupy.io/jobs/42"


# ── Heurísticas ──────────────────────────────────────────────────────────────

def test_email_proprio_e_ignorado():
    assert main._eh_email_proprio("[AutoApply AI] Briefing Matinal — 01/01/2026")
    assert not main._eh_email_proprio("Nova vaga: Dev Python")


def test_tecnologias_respeitam_limite_de_palavra():
    techs = main._extrair_tecnologias("Experiência com Python, SQL e Power BI; javascript desejável")
    assert {"python", "sql", "power bi", "javascript"} <= set(techs)
    assert "java" not in techs


def test_parsear_data():
    assert main._parsear_data("2026-06-29") == "2026-06-29"
    assert main._parsear_data("vence em 29/06/2026") == "2026-06-29"
    assert main._parsear_data("31/02/2026") is None
    assert main._parsear_data(None) is None


def test_subcategoria_financeiro():
    assert main._detectar_subcategoria_financeiro("Seu boleto vence amanhã") == "boleto_fatura"
    assert main._detectar_subcategoria_financeiro("Comprovante de Pix") == "pix"
