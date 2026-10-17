import datetime as dt
import json

import gerar_dashboard as g

BRUTOS = {
    "vagas": [
        {"cargo": "Dev Python", "empresa": "ACME", "score_match": 90, "modelo_trabalho": "Remoto",
         "tecnologias": ["Python", "SQL"], "links_candidatura": ["https://acme.com/v/1"],
         "status_aplicacao": "aplicada", "fonte": "catho", "created_at": "2026-10-15T12:00:00+00:00"},
        {"cargo": "Analista</script><script>alert(1)</script>", "empresa": "Não identificada",
         "tecnologias": '["sql"]', "links_candidatura": '["https://x.com"]',
         "fonte": "indeed", "created_at": "2026-10-16T12:00:00+00:00"},
        {"cargo": "Estágio TI", "score_match": 40, "fonte": "catho", "created_at": "2026-10-14T12:00:00+00:00"},
    ],
    "despesas": [{"estabelecimento": "Vivo", "categoria": "boleto_fatura", "valor": 99.9, "data_despesa": "2026-10-20"},
                 {"estabelecimento": "Antigo", "valor": "R$ 10,00"}],
    "alertas": [{"plataforma": "Spotify", "tipo_alerta": "otp", "codigo_extraido": "123456",
                 "created_at": "2026-10-16T10:00:00+00:00"}],
    "certificados": [],
}


def test_montar_dados_kpis_e_ordem():
    d = g.montar_dados(BRUTOS, 30, agora=dt.datetime(2026, 10, 16, 9, 0))
    assert [v["cargo"] for v in d["vagas"]][0] == "Dev Python"
    assert d["vagas"][-1]["score"] == 0
    k = d["kpis"]
    assert (k["vagas"], k["vagas_alta"], k["score_medio"], k["candidaturas"]) == (3, 1, 65, 1)
    assert k["despesas_total"] == "R$ 99,90"
    assert d["por_fonte"] == [{"fonte": "catho", "n": 2}, {"fonte": "indeed", "n": 1}]
    assert {"tech": "sql", "n": 2} in d["top_techs"]
    # string JSON antiga e empresa "Não identificada"
    analista = next(v for v in d["vagas"] if v["fonte"] == "indeed")
    assert analista["techs"] == ["sql"] and analista["link"] == "https://x.com" and analista["empresa"] is None


def test_alertas_nunca_levam_o_codigo():
    d = g.montar_dados(BRUTOS, 30)
    assert "123456" not in json.dumps(d)


def test_renderizar_injeta_json_sem_fechar_o_script():
    html = g.renderizar(g.montar_dados(BRUTOS, 30), "<script>const DADOS = /*__DADOS__*/{};</script>")
    assert html.count("</script>") == 1
    assert "<\\/script>" in html


def test_template_real_tem_o_marcador():
    with open(g.TEMPLATE_PATH, encoding="utf-8") as f:
        assert g.MARCADOR in f.read()
