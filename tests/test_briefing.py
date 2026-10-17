import briefing_diario as b
from formatacao import formatar_reais


def test_formatar_reais():
    assert formatar_reais(1234.5) == "R$ 1.234,50"
    assert formatar_reais(None) == "—"
    assert formatar_reais("R$ 10,00") == "R$ 10,00"


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


import datetime as _dt

_AGORA = _dt.datetime(2026, 10, 17, 10, 0, tzinfo=_dt.timezone.utc)


def _v(cargo, score, status="nova", dias=1, tipo="nova_vaga", empresa="ACME", **extra):
    criado = (_AGORA - _dt.timedelta(days=dias)).isoformat()
    return {"cargo": cargo, "empresa": empresa, "score_match": score, "status_aplicacao": status,
            "tipo_email": tipo, "created_at": criado, **extra}


def test_aplicar_hoje_so_vagas_novas_recentes_e_com_score():
    vagas = [
        _v("Dev Python", 90, motivos_match=["+55 cargo Python"], links_candidatura=["https://x/1"]),
        _v("Dev Python", 88),                                  # mesma vaga (cargo+empresa) repetida
        _v("Analista de Dados", 70, dias=3),
        _v("Já aplicada", 95, status="aplicada"),
        _v("Acompanhamento", 95, tipo="avanco_fase"),
        _v("Antiga", 99, dias=20),
        _v("Fraca", 20),
    ]
    escolhidas = b.selecionar_para_aplicar(vagas, agora=_AGORA, score_min=60, dias=7)
    assert [v["cargo"] for v in escolhidas] == ["Dev Python", "Analista de Dados"]

    txt, html = b._formatar_aplicar_hoje(escolhidas)
    assert "por quê: +55 cargo Python" in txt and "https://x/1" in txt
    assert 'href="https://x/1"' in html


def test_funil_conta_etapas_e_aponta_candidaturas_paradas():
    vagas = [_v("A", 50), _v("B", 50, status="aplicada", dias=15), _v("C", 50, status="aplicada", dias=2),
             _v("D", 50, status="em_processo"), _v("E", 50, status="reprovada")]
    f = b.resumir_funil(vagas, agora=_AGORA, dias_sem_retorno=10)
    assert f["contagem"]["nova"] == 1 and f["contagem"]["aplicada"] == 2
    assert f["candidaturas"] == 4 and f["taxa_resposta"] == 50
    assert [v["cargo"] for v in f["sem_retorno"]] == ["B"]
    txt, _ = b._formatar_funil(f)
    assert "B @ ACME (15 dias)" in txt


def test_briefing_mostra_aplicar_hoje_antes_das_vagas():
    texto, html = b.montar_briefing([], [], [], [], [], [], 1,
                                    para_aplicar=[_v("Dev Python", 90)],
                                    funil=b.resumir_funil([], agora=_AGORA))
    assert texto.index("APLICAR HOJE") < texto.index("VAGAS\n")
    assert "🎯 Aplicar hoje       : 1" in texto
    assert "Nenhuma candidatura registrada" in texto
    # sem os parâmetros novos o briefing continua como antes
    texto_antigo, _ = b.montar_briefing([], [], [], [], [], [], 1)
    assert "APLICAR HOJE" not in texto_antigo
