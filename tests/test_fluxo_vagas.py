"""Fluxo completo do módulo de vagas com Gmail, IA e Supabase simulados."""
import base64

import pytest

import main
from tests.test_supabase import _FakeSupabase


def _b64url(texto: str) -> str:
    return base64.urlsafe_b64encode(texto.encode()).decode().rstrip("=")


HTML_DIGEST = """
<table><tr><td><a href="https://www.catho.com.br/vagas/dev-python/1">Desenvolvedor Python</a></td></tr>
<tr><td><a href="https://www.catho.com.br/vagas/analista-dados/2">Analista de Dados Jr</a></td></tr>
<tr><td><a href="https://www.catho.com.br/unsubscribe">Cancelar inscrição</a></td></tr></table>
"""


class _Exec:
    def __init__(self, valor):
        self.valor = valor

    def execute(self):
        return self.valor


class _FakeGmail:
    def __init__(self):
        self.labels_aplicadas = {}
        self.queries = []
        self.mensagens = {
            "m1": {
                "id": "m1",
                "internalDate": "0",
                "payload": {
                    "headers": [
                        {"name": "Subject", "value": "2 novas vagas para Programador Python"},
                        {"name": "From", "value": "Catho <avisovagas@catho.com.br>"},
                        {"name": "Date", "value": "Fri, 16 Oct 2026 08:00:00 -0300"},
                    ],
                    "mimeType": "multipart/alternative",
                    "parts": [{"mimeType": "text/html", "body": {"data": _b64url(HTML_DIGEST)}}],
                },
            },
            "m2": {
                "id": "m2",
                "payload": {
                    "headers": [
                        {"name": "Subject", "value": "[AutoApply AI] Briefing Matinal — 16/10/2026"},
                        {"name": "From", "value": "eu@gmail.com"},
                    ],
                    "mimeType": "text/plain",
                    "body": {"data": _b64url("vaga vaga vaga")},
                },
            },
        }

    def users(self):
        return self

    def messages(self):
        return self

    def list(self, userId, q, maxResults, pageToken=None):
        self.queries.append(q)
        return _Exec({"messages": [{"id": i} for i in self.mensagens]})

    def get(self, userId, id, format):
        return _Exec(self.mensagens[id])

    def modify(self, userId, id, body):
        self.labels_aplicadas.setdefault(id, []).extend(body["addLabelIds"])
        return _Exec({})


@pytest.fixture
def ambiente(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "_GEMINI_CACHE", {})
    monkeypatch.setattr(main, "_CACHE_PATH", str(tmp_path / "cache.json"))
    monkeypatch.setattr(main, "_BUDGET_PATH", str(tmp_path / "budget.json"))
    monkeypatch.setattr(main, "_SCHEMA_VAGAS_V3", None)
    monkeypatch.setattr(main.time, "sleep", lambda s: None)
    monkeypatch.setattr(main, "telegram_configurado", lambda: False)

    chamadas_ia = []

    def ia_falsa(assunto, remetente, corpo, links_html=None):
        chamadas_ia.append(links_html)
        return [
            {"cargo": "Desenvolvedor Python", "empresa": "BairesDev", "score_match": 85,
             "tecnologias": ["Python", "Django"], "link_candidatura": None, "tipo_email": "nova_vaga"},
            {"cargo": "Analista de Dados Jr", "empresa": "TechCorp", "score_match": 60,
             "tecnologias": ["SQL"], "link_candidatura": None, "tipo_email": "nova_vaga"},
        ]

    monkeypatch.setattr(main, "_gs_extrair_vagas", ia_falsa)
    return chamadas_ia


def test_digest_vira_duas_vagas_com_links_corretos(ambiente):
    gmail, sb = _FakeGmail(), _FakeSupabase(schema_v3=True)
    labels = {"vagas_pai": "L_PAI", "vagas_alta": "L_ALTA", "vagas_baixa": "L_BAIXA"}

    main.rodar_vagas(gmail, labels, sb, "newer_than:1d", dry_run=False)

    # a query exclui o que já foi processado
    assert "-label:AutoApply/Vagas" in gmail.queries[0]

    # a IA recebeu as âncoras reais, sem o link de descadastro
    urls = [a["url"] for a in ambiente[0]]
    assert urls == ["https://www.catho.com.br/vagas/dev-python/1",
                    "https://www.catho.com.br/vagas/analista-dados/2"]

    # duas linhas distintas no Supabase, cada uma com o SEU link
    linhas = {r["cargo"]: r for _, _, r, _ in sb.gravados}
    assert set(linhas) == {"Desenvolvedor Python", "Analista de Dados Jr"}
    assert linhas["Desenvolvedor Python"]["links_candidatura"] == ["https://www.catho.com.br/vagas/dev-python/1"]
    assert linhas["Analista de Dados Jr"]["links_candidatura"] == ["https://www.catho.com.br/vagas/analista-dados/2"]
    assert linhas["Desenvolvedor Python"]["tecnologias"] == ["python", "django"]
    assert linhas["Desenvolvedor Python"]["vaga_uid"] != linhas["Analista de Dados Jr"]["vaga_uid"]

    # e-mail do próprio robô foi ignorado; o digest ganhou label de alta prioridade
    assert gmail.labels_aplicadas == {"m1": ["L_PAI", "L_ALTA"]}


def test_segunda_execucao_usa_cache_e_nao_gasta_ia(ambiente):
    gmail, sb = _FakeGmail(), _FakeSupabase()
    main.rodar_vagas(gmail, {}, sb, "newer_than:1d", dry_run=False)
    main.rodar_vagas(gmail, {}, sb, "newer_than:1d", dry_run=False)
    assert len(ambiente) == 1
