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
    monkeypatch.setattr(main, "_SCHEMA_VAGAS_MOTIVOS", None)
    monkeypatch.setattr(main.time, "sleep", lambda s: None)
    monkeypatch.setattr(main, "telegram_configurado", lambda: False)
    monkeypatch.setattr(main, "_status_ia", lambda: "ok")

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

    # score final = regras do perfil + IA, com o porquê gravado junto
    dev = linhas["Desenvolvedor Python"]
    assert dev["score_ia"] == 85
    assert any("cargo Python" in mot for mot in dev["motivos_match"])
    assert dev["score_match"] >= 75

    # e-mail do próprio robô foi ignorado; o digest ganhou label de alta prioridade
    assert gmail.labels_aplicadas == {"m1": ["L_PAI", "L_ALTA"]}


def test_segunda_execucao_usa_cache_e_nao_gasta_ia(ambiente):
    gmail, sb = _FakeGmail(), _FakeSupabase()
    main.rodar_vagas(gmail, {}, sb, "newer_than:1d", dry_run=False)
    main.rodar_vagas(gmail, {}, sb, "newer_than:1d", dry_run=False)
    assert len(ambiente) == 1


def test_gateway_fora_nao_gasta_budget_nem_perde_o_email(ambiente, monkeypatch):
    monkeypatch.setattr(main, "gateway_disponivel", lambda: False)
    monkeypatch.setattr(main, "_GEMINI_BUDGET", {"data": main.datetime.date.today().isoformat(),
                                                 "vagas": 0, "geral": 0, "alertas": 0})
    gmail, sb = _FakeGmail(), _FakeSupabase()
    main.rodar_vagas(gmail, {}, sb, "newer_than:1d", dry_run=False)
    assert ambiente == []                      # IA não foi chamada
    assert main._GEMINI_BUDGET["vagas"] == 0   # budget intacto
    assert gmail.labels_aplicadas == {}        # sem label → tenta de novo depois

    # e o módulo de gerais não "rouba" o e-mail de vaga
    main.rodar_emails_gerais(gmail, {"geral_pai": "G"}, sb, "newer_than:1d", dry_run=False)
    assert "m1" not in gmail.labels_aplicadas


def test_email_sem_vaga_real_e_marcado_e_nao_volta_para_a_ia(ambiente, monkeypatch):
    monkeypatch.setattr(main, "_gs_extrair_vagas", lambda *a, **k: ambiente.append(1) or [])
    gmail, sb = _FakeGmail(), _FakeSupabase()
    main.rodar_vagas(gmail, {"vagas_pai": "L_PAI"}, sb, "newer_than:1d", dry_run=False)
    main.rodar_vagas(gmail, {"vagas_pai": "L_PAI"}, sb, "newer_than:1d", dry_run=False)
    assert ambiente == [1]                         # 2ª execução veio do cache
    assert gmail.labels_aplicadas["m1"][0] == "L_PAI"
    assert sb.gravados == []


def test_falha_da_ia_nao_gasta_budget_nem_cacheia(ambiente, monkeypatch):
    monkeypatch.setattr(main, "_status_ia", lambda: "falha")
    monkeypatch.setattr(main, "_GEMINI_BUDGET", {"data": main.datetime.date.today().isoformat(),
                                                 "vagas": 0, "geral": 0, "alertas": 0, "cartas": 0})
    gmail = _FakeGmail()
    main.rodar_vagas(gmail, {}, _FakeSupabase(), "newer_than:1d", dry_run=False)
    assert main._GEMINI_BUDGET["vagas"] == 0
    assert main._cache_get("vagas", "m1", list) is None
    assert gmail.labels_aplicadas == {}


def test_classificacao_indeterminada_nao_e_etiquetada_como_geral(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "_GEMINI_CACHE", {})
    monkeypatch.setattr(main, "_PENDENTES_IA", set())
    monkeypatch.setattr(main, "gateway_disponivel", lambda: False)
    monkeypatch.setattr(main, "_GEMINI_BUDGET", {"data": main.datetime.date.today().isoformat(),
                                                 "vagas": 0, "geral": 0, "alertas": 0, "cartas": 0})
    # e-mail direto de recrutador, sem palavra-chave nenhuma
    cat = main.classificar_email_pipeline("Conversa sobre oportunidade", "maria@startup.io", "Oi Lucas, tudo bem?", "x9")
    assert cat == "indeterminado" and "x9" in main._PENDENTES_IA


def test_acoes_de_vaga_prioritaria_rodam_uma_vez_e_isolam_erros(ambiente, monkeypatch):
    enviados, tarefas = [], []
    monkeypatch.setattr(main, "telegram_configurado", lambda: True)
    monkeypatch.setattr(main, "notificar_vaga", lambda v: enviados.append(v["cargo"]) or True)

    def tarefa_que_quebra(servico, vaga):
        tarefas.append(1)
        raise TypeError("dict no lugar de str")

    monkeypatch.setattr(main, "criar_tarefa_vaga", tarefa_que_quebra)
    vaga = {"email_id": "m1", "cargo": "Dev", "empresa": "ACME", "score_gemini": 90, "tipo_email": "nova_vaga"}
    main.acoes_vaga_prioritaria(None, object(), vaga, "")   # não levanta
    main.acoes_vaga_prioritaria(None, object(), vaga, "")
    assert enviados == ["Dev"]          # push só uma vez
    assert tarefas == [1, 1]            # falhou → tenta de novo na próxima, sem derrubar nada


def test_acoes_ignoram_vaga_de_acompanhamento(ambiente, monkeypatch):
    enviados = []
    monkeypatch.setattr(main, "telegram_configurado", lambda: True)
    monkeypatch.setattr(main, "notificar_vaga", lambda v: enviados.append(1) or True)
    main.acoes_vaga_prioritaria(None, None, {"cargo": "X", "score_gemini": 99, "tipo_email": "retorno"}, "")
    assert enviados == []


def test_mesmo_digest_em_dois_emails_nao_duplica_vaga_nem_push(ambiente, monkeypatch):
    enviados = []
    monkeypatch.setattr(main, "telegram_configurado", lambda: True)
    monkeypatch.setattr(main, "notificar_vaga", lambda v: enviados.append(v["cargo"]) or True)
    gmail, sb = _FakeGmail(), _FakeSupabase()
    copia = dict(gmail.mensagens["m1"], id="m3")
    gmail.mensagens["m3"] = copia          # o log real mostra o mesmo digest chegando 2x

    main.rodar_vagas(gmail, {"vagas_pai": "P", "vagas_alta": "A", "vagas_baixa": "B"}, sb,
                     "newer_than:1d", dry_run=False)

    assert len(sb.gravados) == 2                       # 2 vagas, não 4
    assert enviados == ["Desenvolvedor Python"]        # push uma vez só
    assert set(gmail.labels_aplicadas) == {"m1", "m3"}  # os dois e-mails saem da fila
