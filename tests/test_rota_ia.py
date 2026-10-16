import pytest
import requests

import rota_ia_client as r


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    monkeypatch.setattr(r, "_falhas_seguidas", 0)
    monkeypatch.setattr(r, "_pausado_ate", 0.0)
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
    for _ in range(r.FALHAS_PARA_PAUSAR):
        with pytest.raises(requests.ConnectionError):
            r.chamar_ia("oi")
    assert not r.gateway_disponivel()

    antes = len(chamadas)
    with pytest.raises(r.GatewayIndisponivel):
        r.chamar_ia("oi")
    assert len(chamadas) == antes   # nem tenta a rede durante a pausa


def test_sucesso_fecha_o_circuito(monkeypatch):
    monkeypatch.setattr(r, "_falhas_seguidas", 1)
    monkeypatch.setattr(r.requests, "post", lambda *a, **k: _Resp())
    assert r.chamar_ia("oi") == '{"ok": true}'
    assert r._falhas_seguidas == 0 and r.gateway_disponivel()


def test_pausa_expira(monkeypatch):
    monkeypatch.setattr(r, "_pausado_ate", r.time.monotonic() - 1)
    assert r.gateway_disponivel()
