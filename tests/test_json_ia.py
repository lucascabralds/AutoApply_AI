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
