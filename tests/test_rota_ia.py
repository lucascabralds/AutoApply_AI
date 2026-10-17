import pytest
import requests

import rota_ia_client as r


@pytest.fixture(autouse=True)
def _reset(monkeypatch, tmp_path):
    monkeypatch.setattr(r, "_PAUSA_PATH", str(tmp_path / "pausa.json"))
    monkeypatch.setattr(r, "_cache_estado", ((), {}))
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
    assert len(chamadas) == 3
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
    assert r.gateway_disponivel()
    assert r._pausado_ate() == 0.0


def test_erro_http_nao_abre_o_circuito(monkeypatch):
    class _Erro(_Resp):
        status_code = 500

        def raise_for_status(self):
            raise requests.HTTPError("500")

    monkeypatch.setattr(r.requests, "post", lambda *a, **k: _Erro())
    with pytest.raises(requests.HTTPError):
        r.chamar_ia("oi", max_tentativas=1)
    assert r.gateway_disponivel()


def test_um_timeout_nao_pausa_mas_timeouts_seguidos_sim(monkeypatch):
    def lento(*a, **k):
        raise requests.ReadTimeout("provedor demorou")
    monkeypatch.setattr(r.requests, "post", lento)
    with pytest.raises(requests.ReadTimeout):
        r.chamar_ia("oi", max_tentativas=2)
    assert r.gateway_disponivel()          # um digest lento não cala os alertas
    with pytest.raises(requests.ReadTimeout):
        r.chamar_ia("oi", max_tentativas=2)
    assert not r.gateway_disponivel()      # travado de verdade → pausa


def test_sucesso_zera_contagem_de_timeouts(monkeypatch):
    r._gravar_estado({"pausado_ate": 0, "timeouts": 1})
    monkeypatch.setattr(r.requests, "post", lambda *a, **k: _Resp())
    r.chamar_ia("oi")
    assert r._ler_estado() == {}


def test_estado_reescrito_no_mesmo_instante_nao_fica_velho():
    r._gravar_estado({"pausado_ate": 1.0, "timeouts": 0})
    assert r._pausado_ate() == 1.0
    r._gravar_estado({"pausado_ate": 99.0, "timeouts": 0})   # mesmo tamanho, mesmo segundo
    assert r._pausado_ate() == 99.0


def test_200_sem_choices_e_falha_e_nao_fecha_a_pausa(monkeypatch):
    r._gravar_pausa(r.time.time() - 1)   # pausa vencida, arquivo ainda existe

    class _SemChoices(_Resp):
        def json(self):
            return {"error": "todos os provedores falharam"}
    monkeypatch.setattr(r.requests, "post", lambda *a, **k: _SemChoices())
    with pytest.raises(KeyError):
        r.chamar_ia("oi", max_tentativas=1)
    assert r._pausado_ate() != 0.0   # não registrou sucesso
