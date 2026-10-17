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
    # a mesma vaga vinda em outro e-mail (digest repetido, outra plataforma) é a mesma vaga
    assert a == main._vaga_uid("msg2", "Desenvolvedor Python", "ACME")


def test_vaga_uid_sem_empresa_usa_link_canonico_ou_o_email():
    link1 = "https://www.catho.com.br/vagas/analista-de-sistemas/123/?utm_source=email&origem=alerta#topo"
    link2 = "https://catho.com.br/vagas/analista-de-sistemas/123?utm_source=outro"
    a = main._vaga_uid("m1", "Analista de sistemas", "Não identificada", link1)
    assert a == main._vaga_uid("m2", "Analista de sistemas", None, link2)
    # sem link próprio não dá para saber se é a mesma vaga
    assert main._vaga_uid("m1", "Analista", None) != main._vaga_uid("m2", "Analista", None)
    # o identificador da vaga no Indeed (?jk=) não é rastreio
    assert main._canonizar_link("https://br.indeed.com/viewjob?jk=1&from=ja") != \
        main._canonizar_link("https://br.indeed.com/viewjob?jk=2&from=ja")


def test_vaga_repetida_nao_volta_para_nova():
    sb = _FakeSupabase()
    main.salvar_vaga(sb, _vaga("Dev Python"))
    assert "status_aplicacao" not in sb.gravados[-1][2]   # default da coluna no insert; intocado no update
    main.salvar_vaga(sb, {**_vaga("Dev Python"), "status": "aplicada"})
    assert sb.gravados[-1][2]["status_aplicacao"] == "aplicada"


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


@pytest.mark.parametrize("entrada, esperado", [
    ("3x de R$ 49,90", 49.90),
    ("2 parcelas de R$ 1.000,00", 1000.0),
    ("Total US$ 12.50", 12.50),
])
def test_parsear_valor_prefere_o_numero_da_moeda(entrada, esperado):
    assert main._parsear_valor(entrada) == esperado


@pytest.mark.parametrize("mensagem, eh_schema", [
    ("PGRST204: Could not find the 'empresa' column of 'vagas_extraidas' in the schema cache", True),
    ("42P10: there is no unique or exclusion constraint matching the ON CONFLICT specification", True),
    ('23502: null value in column "cargo" violates not-null constraint', False),
    ("22001: value too long for type character varying(50) in column x", False),
])
def test_erro_de_schema_so_para_migracao_ausente(mensagem, eh_schema):
    assert main._erro_de_schema(Exception(mensagem)) is eh_schema


def test_despesa_sem_valor_nao_e_gravada():
    sb = _FakeSupabase()
    ok = main.salvar_email_geral(sb, {"email_id": "s1", "categoria": "financeiro", "assunto": "Acompanhe seu Score",
                                      "remetente_nome": "Consumidor Positivo", "valor": None})
    assert ok and sb.gravados == []


def test_despesa_com_valor_e_gravada():
    sb = _FakeSupabase()
    main.salvar_email_geral(sb, {"email_id": "s2", "categoria": "financeiro", "subcategoria": "boleto_fatura",
                                 "remetente_nome": "Vivo", "valor": "R$ 99,90", "data_evento": "29/10/2026"})
    tabela, _, registro, _ = sb.gravados[0]
    assert tabela == "despesas_domesticas" and registro["valor"] == 99.90 and registro["data_despesa"] == "2026-10-29"
