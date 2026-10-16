import pytest
import requests

import rota_ia_client as r


@pytest.fixture(autouse=True)
def _reset(monkeypatch, tmp_path):
    monkeypatch.setattr(r, "_PAUSA_PATH", str(tmp_path / "pausa.json"))
    monkeypatch.setattr(r, "ROTA_IA_API_KEY", "teste")
    monkeypatch.setattr(r.time, "sleep", lambda s: None)


class _Resp:
    status_code = 200
    text = ""

    def raise_for_status(self):
        pass

    def json(self):
        return {"choices": [{"message": {"content": '{"ok": true}'}}]}


def test_gateway_fora_do_ar_abre_o_circuito(monkeypatch):
    chamadas = []

    def post_falho(*a, **k):
        chamadas.append(1)
        raise requests.ConnectionError("recusado")

    monkeypatch.setattr(r.requests, "post", post_falho)
    with pytest.raises(requests.ConnectionError):
        r.chamar_ia("oi", max_tentativas=3)
    assert len(chamadas) == 3 and not r.ultima_chamada_ok
    assert not r.gateway_disponivel()

    with pytest.raises(r.GatewayIndisponivel):
        r.chamar_ia("oi")
    assert len(chamadas) == 3   # nem tenta a rede durante a pausa


def test_pausa_sobrevive_a_um_novo_processo(monkeypatch):
    r._gravar_pausa(r.time.time() + 60)
    # um processo novo só enxerga o arquivo
    assert not r.gateway_disponivel()


def test_sucesso_fecha_o_circuito(monkeypatch):
    r._gravar_pausa(r.time.time() - 1)   # pausa vencida
    monkeypatch.setattr(r.requests, "post", lambda *a, **k: _Resp())
    assert r.chamar_ia("oi") == '{"ok": true}'
    assert r.ultima_chamada_ok and r.gateway_disponivel()
    assert r._pausado_ate() == 0.0


def test_erro_http_nao_abre_o_circuito(monkeypatch):
    class _Erro(_Resp):
        status_code = 500

        def raise_for_status(self):
            raise requests.HTTPError("500")

    monkeypatch.setattr(r.requests, "post", lambda *a, **k: _Erro())
    with pytest.raises(requests.HTTPError):
        r.chamar_ia("oi", max_tentativas=1)
    assert r.gateway_disponivel() and not r.ultima_chamada_ok
