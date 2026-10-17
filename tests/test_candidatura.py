import base64
import email

import pytest

import candidatura
import main

PERFIL = """# Perfil

## 1. Diretrizes
Tom técnico.

## 3. Hard Skills
- Python, FastAPI, SQL

## 7. Pontos de Atenção (Uso Interno — Autoavaliação)
> *Esta seção é para uso próprio e não deve ser compartilhada com recrutadores.*
- Inglês (A2)

## 8. Frases de Impacto
Desenvolvi um pipeline com Python.
"""


def test_perfil_remove_secao_de_uso_interno(tmp_path):
    p = tmp_path / "perfil.md"
    p.write_text(PERFIL, encoding="utf-8")
    texto = candidatura.carregar_perfil(str(p))
    assert "Inglês (A2)" not in texto and "Pontos de Atenção" not in texto
    assert "FastAPI" in texto and "Frases de Impacto" in texto


def test_perfil_real_do_repositorio_nao_vaza_autoavaliacao():
    texto = candidatura.carregar_perfil()
    assert texto and "Autoavaliação" not in texto and "Projetos de Destaque" in texto


@pytest.mark.parametrize("texto, esperado", [
    ("Envie seu CV para rh@acme.com.br até sexta", "rh@acme.com.br"),
    ("noreply@catho.com.br e vagas@empresa.com", "vagas@empresa.com"),
    ("Dúvidas: suporte@indeed.com", None),
    ("logo@2x.png talentos@startup.io.", "talentos@startup.io"),
    ("sem e-mail aqui", None),
])
def test_extrair_email_recrutador(texto, esperado):
    assert candidatura.extrair_email_recrutador(texto) == esperado


def test_montar_mensagem_com_curriculo_e_destinatario(tmp_path):
    cv = tmp_path / "Lucas_CV.pdf"
    cv.write_bytes(b"%PDF-1.4 teste")
    vaga = {"cargo": "Dev Python", "empresa": "ACME", "link_candidatura": "https://acme.com/v"}
    msg = candidatura.montar_mensagem(vaga, "Olá,\nCarta.", "rh@acme.com", str(cv))
    assert msg["To"] == "rh@acme.com"
    assert msg["Subject"] == "Candidatura — Dev Python | ACME"
    anexos = [p.get_filename() for p in msg.iter_attachments()]
    assert anexos == ["Lucas_CV.pdf"]
    assert "Nota do AutoApply" not in msg.get_body().get_content()


def test_sem_destinatario_inclui_link_da_plataforma():
    vaga = {"cargo": "Dev", "empresa": "Não identificada", "link_candidatura": "https://catho.com.br/v/1"}
    msg = candidatura.montar_mensagem(vaga, "Olá", None)
    assert msg["To"] is None and msg["Subject"] == "Candidatura — Dev"
    assert "https://catho.com.br/v/1" in msg.get_content()


class _Drafts:
    def __init__(self):
        self.criados = []

    def users(self):
        return self

    def drafts(self):
        return self

    def create(self, userId, body):
        self.criados.append(body)
        return self

    def execute(self):
        return {"id": f"r{len(self.criados)}"}


@pytest.fixture
def ambiente(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "_GEMINI_CACHE", {})
    monkeypatch.setattr(main, "_CACHE_PATH", str(tmp_path / "c.json"))
    monkeypatch.setattr(main, "_BUDGET_PATH", str(tmp_path / "b.json"))
    monkeypatch.setattr(main, "gateway_disponivel", lambda: True)
    monkeypatch.setattr(main, "_status_ia", lambda: "ok")
    monkeypatch.setattr(main, "_GEMINI_BUDGET", {"data": main.datetime.date.today().isoformat(),
                                                 "vagas": 0, "geral": 0, "alertas": 0, "cartas": 0})
    cartas = []
    monkeypatch.setattr(main, "_gs_gerar_carta", lambda vaga, perfil: cartas.append(perfil) or "Olá,\nCarta.\nLucas")
    return cartas


def test_rascunho_criado_uma_unica_vez_por_vaga(ambiente):
    gmail = _Drafts()
    vaga = {"email_id": "m1", "cargo": "Dev Python", "empresa": "ACME", "score_gemini": 90,
            "link_candidatura": "https://acme.com/v"}
    assert main.preparar_rascunho_candidatura(gmail, vaga, "Envie para rh@acme.com") == "r1"
    assert main.preparar_rascunho_candidatura(gmail, vaga, "Envie para rh@acme.com") is None
    assert len(gmail.criados) == 1
    raw = base64.urlsafe_b64decode(gmail.criados[0]["message"]["raw"])
    assert email.message_from_bytes(raw)["To"] == "rh@acme.com"
    assert "Autoavaliação" not in ambiente[0]   # perfil enviado à IA sem a seção interna


def test_rascunho_respeita_budget(ambiente, monkeypatch):
    monkeypatch.setattr(main, "_BUDGET_LIMITE", {**main._BUDGET_LIMITE, "cartas": 0})
    assert main.preparar_rascunho_candidatura(_Drafts(), {"email_id": "m2", "cargo": "X"}, "") is None
    assert ambiente == []


def test_deve_gerar_rascunho(monkeypatch):
    monkeypatch.setattr(candidatura, "RASCUNHOS_ATIVOS", True)
    monkeypatch.setattr(candidatura, "SCORE_RASCUNHO", 80)
    assert candidatura.deve_gerar_rascunho({"score_gemini": 85, "tipo_email": "nova_vaga"})
    assert not candidatura.deve_gerar_rascunho({"score_gemini": 70, "tipo_email": "nova_vaga"})
    assert not candidatura.deve_gerar_rascunho({"score_gemini": 95, "tipo_email": "candidatura_enviada"})
    monkeypatch.setattr(candidatura, "RASCUNHOS_ATIVOS", False)
    assert not candidatura.deve_gerar_rascunho({"score_gemini": 95})
