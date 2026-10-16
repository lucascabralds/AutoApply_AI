import pytest

import main


class _Tabela:
    """Fake mínimo do query builder do supabase-py para vagas_extraidas."""

    def __init__(self, linhas):
        self.linhas = linhas
        self.updates = []
        self._filtro = None
        self._update = None

    def select(self, colunas):
        self._update = None
        return self

    def ilike(self, coluna, padrao):
        termo = padrao.strip("%").lower()
        self._filtro = lambda r: termo in (r.get(coluna) or "").lower()
        return self

    def order(self, *a, **k):
        return self

    def limit(self, n):
        return self

    def update(self, valores):
        self._update = valores
        return self

    def eq(self, coluna, valor):
        self._filtro = lambda r: r.get(coluna) == valor
        return self

    def execute(self):
        alvo = [r for r in self.linhas if self._filtro(r)]
        if self._update:
            for r in alvo:
                r.update(self._update)
            self.updates.append(self._update)

        class _R:
            data = alvo
        return _R()


class _SB:
    def __init__(self, linhas):
        self.tabela = _Tabela(linhas)

    def table(self, nome):
        return self.tabela


@pytest.fixture(autouse=True)
def _schema_v3(monkeypatch):
    monkeypatch.setattr(main, "_SCHEMA_VAGAS_V3", True)


@pytest.mark.parametrize("tipo, status, esperado", [
    ("nova_vaga", "nova", "nova"),
    ("candidatura_enviada", "enviada", "aplicada"),
    ("avanco_fase", "avanco", "em_processo"),
    ("retorno", "reprovado", "reprovada"),
    ("retorno", None, "em_processo"),
])
def test_status_do_email(tipo, status, esperado):
    assert main._status_do_email(tipo, status) == esperado


def test_candidatura_enviada_atualiza_vaga_existente():
    sb = _SB([
        {"id": 1, "cargo": "Analista de TI Jr", "empresa": "Minsait Brasil", "status_aplicacao": "nova"},
        {"id": 2, "cargo": "Dev Python", "empresa": "Minsait Brasil", "status_aplicacao": "nova"},
    ])
    vaga = {"cargo": "Analista de TI Jr.", "empresa": "Minsait"}
    assert main.atualizar_status_vaga(sb, vaga, "aplicada")
    assert [r["status_aplicacao"] for r in sb.tabela.linhas] == ["aplicada", "nova"]


def test_status_nunca_regride():
    sb = _SB([{"id": 1, "cargo": "Dev Python", "empresa": "ACME", "status_aplicacao": "em_processo"}])
    assert main.atualizar_status_vaga(sb, {"cargo": "Dev Python", "empresa": "ACME"}, "aplicada")
    assert sb.tabela.updates == []
    assert sb.tabela.linhas[0]["status_aplicacao"] == "em_processo"


def test_sem_vaga_correspondente_retorna_false():
    sb = _SB([{"id": 1, "cargo": "Designer", "empresa": "ACME", "status_aplicacao": "nova"}])
    assert not main.atualizar_status_vaga(sb, {"cargo": "Dev Python", "empresa": "ACME"}, "aplicada")
    assert not main.atualizar_status_vaga(sb, {"cargo": "Dev Python", "empresa": "Não identificada"}, "aplicada")


def test_sem_migracao_nao_tenta_atualizar(monkeypatch):
    monkeypatch.setattr(main, "_SCHEMA_VAGAS_V3", False)
    sb = _SB([{"id": 1, "cargo": "Dev Python", "empresa": "ACME", "status_aplicacao": "nova"}])
    assert not main.atualizar_status_vaga(sb, {"cargo": "Dev Python", "empresa": "ACME"}, "aplicada")
