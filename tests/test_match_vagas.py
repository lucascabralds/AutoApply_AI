"""Score local e explicável de aderência da vaga ao perfil (match_vagas.py)."""
import pytest

import main
import match_vagas as m
from tests.test_supabase import _FakeSupabase


@pytest.mark.parametrize("vaga, minimo, maximo", [
    # alvo principal: Python/dados júnior, remoto ou em SP
    ({"cargo": "Desenvolvedor Python Júnior", "tecnologias": ["Python", "SQL"], "modelo_trabalho": "Remoto"}, 85, 100),
    ({"cargo": "Analista de Dados Jr", "tecnologias": ["SQL", "Power BI"], "local": "São Paulo - SP"}, 75, 100),
    # TI genérico: médio — antes saía 0% quando a IA não preenchia o score
    ({"cargo": "Analista de sistemas"}, 25, 50),
    ({"cargo": "Assistente de Desenvolvimento de Sistemas"}, 35, 60),
    # fora do perfil / sênior / outra região: baixo
    ({"cargo": "Auxiliar Técnico", "local": "Barueri, SP"}, 0, 15),
    ({"cargo": "Vendedor Externo", "requisitos": ["Excel avançado", "Python"]}, 0, 15),
    ({"cargo": "Desenvolvedor Java Sênior", "tecnologias": ["Java", "Spring"]}, 0, 10),
    ({"cargo": "Analista de Suporte Pleno"}, 0, 15),
])
def test_faixas_de_score(vaga, minimo, maximo):
    r = m.avaliar(vaga)
    assert minimo <= r["score"] <= maximo, r


def test_cada_ponto_tem_motivo():
    r = m.avaliar({"cargo": "Analista de Dados Sênior", "tecnologias": ["Python"],
                   "local": "Curitiba - PR", "modelo_trabalho": "Presencial"})
    texto = " | ".join(r["motivos"])
    assert "+50 área-alvo" in texto
    assert "-25 nível sênior" in texto
    assert "fora da região (Curitiba - PR)" in texto
    assert r["score"] == sum(int(x.split()[0]) for x in r["motivos"])


def test_remoto_vale_em_qualquer_lugar_e_local_e_configuravel(monkeypatch):
    remoto = m.avaliar({"cargo": "Analista de Dados", "local": "Recife - PE", "modelo_trabalho": "Remoto"})
    assert any("remoto" in x for x in remoto["motivos"])
    monkeypatch.setenv("LOCAIS_ACEITOS", "Recife,PE")
    assert any("local aceito" in x for x in m.avaliar({"cargo": "Analista de Dados", "local": "Recife - PE"})["motivos"])


def test_pl_sql_nao_e_nivel_pleno_e_anos_de_experiencia_pesam():
    r = m.avaliar({"cargo": "Desenvolvedor PL/SQL", "requisitos": ["5 anos de experiência com Oracle"]})
    assert not any("pleno" in x for x in r["motivos"])
    assert any("5+ anos" in x for x in r["motivos"])


def test_combinar_ignora_score_da_ia_ausente():
    assert m.combinar(40, 0) == 40
    assert m.combinar(40, None) == 40
    assert m.combinar(40, "abc") == 40
    assert m.combinar(60, 90) == 75


def test_entrada_estranha_da_ia_nao_quebra():
    r = m.avaliar({"cargo": None, "tecnologias": [{"nome": "Python"}, 3], "requisitos": "SQL", "local": None})
    assert r["score"] >= 0 and any("Python" in x for x in r["motivos"])


def test_salvar_vaga_grava_motivos_e_cai_sem_as_colunas_novas(monkeypatch):
    monkeypatch.setattr(main, "_SCHEMA_VAGAS_MOTIVOS", None)
    vaga = {"email_id": "e1", "cargo": "Dev Python", "empresa": "ACME", "score_gemini": 80,
            "score_ia": 70, "motivos_match": ["+55 cargo Python"], "status": "nova"}

    sb = _FakeSupabase()
    assert main.salvar_vaga(sb, vaga)
    assert sb.gravados[-1][2]["motivos_match"] == ["+55 cargo Python"]
    assert sb.gravados[-1][2]["score_ia"] == 70

    class _SemMotivos(_FakeSupabase):
        def falhar_se(self, acao, registro, conflito):
            return "motivos_match" in registro

    sb2 = _SemMotivos()
    assert main.salvar_vaga(sb2, vaga)
    registro = sb2.gravados[-1][2]
    assert "motivos_match" not in registro and registro["score_match"] == 80
    assert main._SCHEMA_VAGAS_MOTIVOS is False and main._SCHEMA_VAGAS_V3 is True
