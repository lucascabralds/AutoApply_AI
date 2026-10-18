"""vagas.py — marcar candidaturas à mão e listar vagas."""
import briefing_diario
import vagas
from tests.test_status_candidatura import _SB


def test_aplicar_e_descartar_mudam_o_status(monkeypatch, capsys):
    sb = _SB([{"id": 1, "cargo": "Dev Python", "status_aplicacao": "nova"},
              {"id": 2, "cargo": "Analista", "status_aplicacao": "nova"}])
    monkeypatch.setattr(briefing_diario, "_criar_sb", lambda: sb)

    assert vagas.main(["aplicar", "1"]) == 0
    assert vagas.main(["descartar", "2"]) == 0
    assert [r["status_aplicacao"] for r in sb.tabela.linhas] == ["aplicada", "descartada"]
    assert vagas.main(["aplicar", "99"]) == 1
    assert "#99 não encontrada" in capsys.readouterr().out


def test_listar_filtra_por_status_e_score(monkeypatch, capsys):
    linhas = [
        {"id": 7, "cargo": "Dev Python", "empresa": "ACME", "score_match": 90, "status_aplicacao": "nova",
         "motivos_match": ["+55 cargo Python"], "links_candidatura": ["https://x/7"]},
        {"id": 8, "cargo": "Aplicada", "score_match": 95, "status_aplicacao": "aplicada"},
        {"id": 9, "cargo": "Fraca", "score_match": 10, "status_aplicacao": "nova"},
    ]
    monkeypatch.setattr(briefing_diario, "_criar_sb", lambda: object())
    monkeypatch.setattr(briefing_diario, "coletar_vagas", lambda sb, dias, limite=100: linhas)
    assert vagas.main(["listar", "--min", "50"]) == 0
    out = capsys.readouterr().out
    assert "#7" in out and "Dev Python @ ACME" in out and "por quê: +55 cargo Python" in out
    assert "#8" not in out and "#9" not in out


def test_briefing_mostra_o_id_e_como_marcar():
    txt, _ = briefing_diario._formatar_aplicar_hoje([{"id": 42, "cargo": "Dev", "score_match": 80}])
    assert "#42 Dev" in txt and "python vagas.py aplicar" in txt


def test_descartada_volta_ao_funil_se_chegar_candidatura_enviada():
    import main
    assert main._ORDEM_STATUS["aplicada"] > main._ORDEM_STATUS["descartada"]
