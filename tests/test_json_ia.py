import pytest

from gemini_service import _parse_json


def test_remove_cercas_de_markdown_e_prosa():
    raw = 'Aqui está:\n```json\n{"cargo": "Dev"}\n```\nEspero ter ajudado!'
    assert _parse_json(raw) == {"cargo": "Dev"}


def test_virgula_solta():
    assert _parse_json('[{"a": 1,}, ]') == [{"a": 1}]


def test_json_truncado_recupera_o_maximo_possivel():
    raw = '[{"cargo": "Dev Python", "score_match": 80}, {"cargo": "Analista", "empre'
    assert _parse_json(raw) == [{"cargo": "Dev Python", "score_match": 80}, {"cargo": "Analista"}]


def test_irrecuperavel_retorna_vazio():
    assert _parse_json("não sei responder") == {}
    assert _parse_json("") == {}


def test_carta_usa_perfil_e_remove_markdown(monkeypatch):
    import gemini_service

    prompts = []
    monkeypatch.setattr(gemini_service, "_chamar_gemini",
                        lambda p: prompts.append(p) or "```\nOlá,\nCarta.\nLucas\n```")
    carta = gemini_service.gerar_carta_apresentacao(
        {"cargo": "Dev Python", "empresa": "ACME", "tecnologias": ["python", "sql"]},
        "PERFIL: Python, FastAPI",
    )
    assert carta == "Olá,\nCarta.\nLucas"
    assert "PERFIL: Python, FastAPI" in prompts[0] and "Tecnologias: python, sql" in prompts[0]
    assert "NUNCA invente" in prompts[0]


def test_carta_falha_retorna_vazio(monkeypatch):
    import gemini_service

    def falha(p):
        raise RuntimeError("gateway")
    monkeypatch.setattr(gemini_service, "_chamar_gemini", falha)
    assert gemini_service.gerar_carta_apresentacao({"cargo": "X"}, "perfil") == ""


def test_carta_aceita_requisitos_em_formato_inesperado(monkeypatch):
    import gemini_service
    prompts = []
    monkeypatch.setattr(gemini_service, "_chamar_gemini", lambda p: prompts.append(p) or "Olá")
    gemini_service.gerar_carta_apresentacao(
        {"cargo": "Dev", "requisitos": [{"nome": "SQL"}, 3, None], "tecnologias": "python"}, "perfil")
    assert "Requisitos: SQL, 3" in prompts[0] and "Tecnologias: python" in prompts[0]


def test_status_das_funcoes_de_ia(monkeypatch):
    import gemini_service as gs

    # heurística própria do gemini_service: não chama a IA
    monkeypatch.setattr(gs, "_chamar_rota_ia", lambda *a, **k: pytest.fail("não devia chamar a IA"))
    assert gs.classificar_email("Vaga Dev Python", "rh@empresa.com", "") == "vaga"
    assert gs.status_ultima_chamada() == "heuristica"

    # IA respondeu JSON válido
    monkeypatch.setattr(gs, "_chamar_rota_ia", lambda *a, **k: '[{"cargo": "Dev"}]')
    assert gs.extrair_vagas("a", "b", "c")[0]["cargo"] == "Dev"
    assert gs.status_ultima_chamada() == "ok"

    # IA respondeu lixo: nada de vaga fictícia, status falha
    monkeypatch.setattr(gs, "_chamar_rota_ia", lambda *a, **k: "desculpe, não entendi")
    assert gs.extrair_vagas("Assunto qualquer", "b", "c") == []
    assert gs.status_ultima_chamada() == "falha"

    # gateway fora
    def fora(*a, **k):
        raise ConnectionError("recusado")
    monkeypatch.setattr(gs, "_chamar_rota_ia", fora)
    assert gs.analisar_acesso("a", "b", "c") == {}
    assert gs.status_ultima_chamada() == "falha"


@pytest.mark.parametrize("resposta, esperado", [
    ('{"vagas": []}', []),
    ('{"results": []}', []),
    ('{"cargo": "Dev Python"}', ["Dev Python"]),
    ('{"observacao": "não é vaga"}', []),
])
def test_formatos_de_resposta_de_vagas(monkeypatch, resposta, esperado):
    import gemini_service as gs
    monkeypatch.setattr(gs, "_chamar_rota_ia", lambda *a, **k: resposta)
    assert [v["cargo"] for v in gs.extrair_vagas("Assunto", "r", "c")] == esperado
    assert gs.status_ultima_chamada() == "ok"


def test_reparo_de_resposta_cortada_nao_perde_campos_depois_de_lista_interna():
    # O "último ]" desta resposta é o fim de tecnologias — reparar só até ali perdia o link
    raw = '[{"cargo": "Dev", "tecnologias": ["python", "sql"], "link": 0, "score": 80}, {"cargo": "Ana'
    assert _parse_json(raw)[0] == {"cargo": "Dev", "tecnologias": ["python", "sql"], "link": 0, "score": 80}
