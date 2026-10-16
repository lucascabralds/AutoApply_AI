import pytest

import main


class _Query:
    def __init__(self, tabela, sb):
        self.tabela, self.sb = tabela, sb
        self.op = None

    def upsert(self, registro, on_conflict=None):
        self.op = ("upsert", registro, on_conflict)
        return self

    def insert(self, registro):
        self.op = ("insert", registro, None)
        return self

    def execute(self):
        acao, registro, conflito = self.op
        if self.sb.falhar_se(acao, registro, conflito):
            raise Exception("PGRST204: Could not find the 'empresa' column of 'vagas_extraidas' in the schema cache")
        self.sb.gravados.append((self.tabela, acao, registro, conflito))
        return self


class _FakeSupabase:
    def __init__(self, schema_v3=True):
        self.schema_v3 = schema_v3
        self.gravados = []

    def falhar_se(self, acao, registro, conflito):
        return not self.schema_v3 and "empresa" in registro

    def table(self, nome):
        return _Query(nome, self)


@pytest.fixture(autouse=True)
def _reset_schema_flag(monkeypatch):
    monkeypatch.setattr(main, "_SCHEMA_VAGAS_V3", None)


def _vaga(cargo, empresa="ACME"):
    return {
        "email_id": "msg1", "cargo": cargo, "empresa": empresa, "fonte": "catho",
        "tecnologias": ["python"], "requisitos": ["SQL"], "score_gemini": 82,
        "link_candidatura": "https://catho.com.br/vaga/1", "status": "nova",
    }


def test_vaga_uid_estavel_e_distinto_por_vaga():
    a = main._vaga_uid("msg1", "Desenvolvedor Python", "ACME")
    assert a == main._vaga_uid("msg1", "desenvolvedor  python!", "acme")
    assert a != main._vaga_uid("msg1", "Analista de Dados", "ACME")
    assert a != main._vaga_uid("msg2", "Desenvolvedor Python", "ACME")


def test_digest_com_varias_vagas_gera_uma_linha_por_vaga():
    sb = _FakeSupabase(schema_v3=True)
    assert main.salvar_vaga(sb, _vaga("Dev Python"))
    assert main.salvar_vaga(sb, _vaga("Analista de Dados"))
    uids = {r["vaga_uid"] for _, acao, r, conflito in sb.gravados if conflito == "vaga_uid"}
    assert len(uids) == 2
    registro = sb.gravados[0][2]
    assert registro["score_match"] == 82
    assert registro["tecnologias"] == ["python"]
    assert registro["links_candidatura"] == ["https://catho.com.br/vaga/1"]


def test_sem_migracao_cai_para_formato_antigo():
    sb = _FakeSupabase(schema_v3=False)
    assert main.salvar_vaga(sb, _vaga("Dev Python"))
    assert main._SCHEMA_VAGAS_V3 is False
    _, _, registro, conflito = sb.gravados[-1]
    assert conflito == "gmail_message_id"
    assert "vaga_uid" not in registro
    assert registro["tecnologias"] == '["python"]'


@pytest.mark.parametrize("entrada, esperado", [
    ("R$ 1.234,56", 1234.56),
    ("R$ 49,90", 49.90),
    ("USD 12.50", 12.50),
    ("Total: 1.234", 1234.0),
    ("1,234.56", 1234.56),
    (49.9, 49.9),
    ("sem valor", None),
    (None, None),
])
def test_parsear_valor(entrada, esperado):
    assert main._parsear_valor(entrada) == esperado
