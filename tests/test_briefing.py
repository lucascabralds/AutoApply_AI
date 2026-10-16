import briefing_diario as b


def test_formatar_reais():
    assert b._formatar_reais(1234.5) == "R$ 1.234,50"
    assert b._formatar_reais(None) == "—"
    assert b._formatar_reais("R$ 10,00") == "R$ 10,00"


def test_vagas_mostram_empresa_score_e_escapam_html():
    vagas = [{
        "cargo": "Dev <script>", "empresa": "ACME", "score_match": 90, "fonte": "catho",
        "links_candidatura": ["https://x.com/?a=1&b=2"], "status_aplicacao": "nova",
    }]
    txt, html = b._formatar_vagas(vagas)
    assert "🔥 Dev <script> @ ACME 90%" in txt
    assert "<script>" not in html
    assert 'href="https://x.com/?a=1&amp;b=2"' in html


def test_vagas_legado_sem_score_continuam_funcionando():
    vagas = [{"cargo": "Analista", "fonte": "indeed", "links_candidatura": '["https://i.com/1"]'}]
    txt, _ = b._formatar_vagas(vagas)
    assert "Analista" in txt and "https://i.com/1" in txt
