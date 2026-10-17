import json

import pytest
import requests

import diagnostico as d
import google_auth
import rota_ia_client


def _niveis(resultados):
    return {r.nome: r.nivel for r in resultados}


def test_sem_credentials_e_erro(monkeypatch, tmp_path):
    monkeypatch.setattr(google_auth, "CREDENTIALS_PATH", str(tmp_path / "nao_existe.json"))
    monkeypatch.setattr(google_auth, "TOKEN_PATH", str(tmp_path / "token.json"))
    monkeypatch.setattr(google_auth, "HEADLESS", False)
    n = _niveis(d.checar_google_arquivos())
    assert n["credentials.json"] == d.ERRO and n["token.json"] == d.AVISO


def test_token_sem_escopo_pede_reautorizacao(monkeypatch, tmp_path):
    cred, token = tmp_path / "c.json", tmp_path / "t.json"
    cred.write_text("{}")
    token.write_text(json.dumps({"scopes": ["https://www.googleapis.com/auth/gmail.modify"]}))
    monkeypatch.setattr(google_auth, "CREDENTIALS_PATH", str(cred))
    monkeypatch.setattr(google_auth, "TOKEN_PATH", str(token))
    monkeypatch.setattr(google_auth, "HEADLESS", True)
    res = {r.nome: r for r in d.checar_google_arquivos()}
    assert res["token.json"].nivel == d.ERRO and "drive.file" in res["token.json"].detalhe


def test_rota_ia_fora_do_ar(monkeypatch):
    monkeypatch.setattr(rota_ia_client, "ROTA_IA_API_KEY", "x")

    def recusa(*a, **k):
        raise requests.ConnectionError()
    monkeypatch.setattr(d.requests, "post", recusa)
    r = d.checar_rota_ia()
    assert r.nivel == d.ERRO and "gateway está rodando" in r.detalhe


def test_rota_ia_chave_recusada(monkeypatch):
    monkeypatch.setattr(rota_ia_client, "ROTA_IA_API_KEY", "x")

    class _R:
        status_code, ok, text = 401, False, "unauthorized"
    monkeypatch.setattr(d.requests, "post", lambda *a, **k: _R())
    assert "chave recusada" in d.checar_rota_ia().detalhe


def test_supabase_sem_migracao_e_aviso(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://x.supabase.co")
    monkeypatch.setenv("SUPABASE_KEY", "k")

    class _Q:
        def __init__(self, tabela):
            self.tabela = tabela

        def select(self, colunas):
            self.colunas = colunas
            return self

        def limit(self, n):
            return self

        def execute(self):
            if "vaga_uid" in self.colunas:
                raise Exception("42703: column vagas_extraidas.vaga_uid does not exist")
            return self

    class _SB:
        def table(self, nome):
            return _Q(nome)

    import supabase
    monkeypatch.setattr(supabase, "create_client", lambda url, chave: _SB())
    n = _niveis(d.checar_supabase())
    assert n["Supabase: vagas_extraidas"] == d.AVISO
    assert n["Supabase: despesas_domesticas"] == d.OK


def test_sem_rede_nao_faz_chamadas(monkeypatch):
    monkeypatch.setattr(d.requests, "post", lambda *a, **k: pytest.fail("chamou a rede"))
    monkeypatch.setattr(d.requests, "get", lambda *a, **k: pytest.fail("chamou a rede"))
    assert d.executar(com_rede=False)
