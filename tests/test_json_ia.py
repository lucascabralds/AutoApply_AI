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
