"""Extração de vagas pela IA: formato enxuto, link por índice, retry e score de reserva."""
import json

import gemini_service as gs
import rota_ia_client

LINKS = [
    {"texto": "Desenvolvedor Python Jr", "url": "https://www.catho.com.br/vagas/dev-python/111?utm=muito-longo"},
    {"texto": "Analista de Dados", "url": "https://br.indeed.com/rc/clk/dl?jk=85ef6f5a799ff9f"},
    {"texto": "Auxiliar Técnico", "url": "https://www.infojobs.com.br/vaga-de-auxiliar-tecnico"},
]


def _ia(respostas, chamadas):
    def falsa(prompt, max_tentativas=3, max_tokens=None, **k):
        chamadas.append({"prompt": prompt, "max_tokens": max_tokens})
        return respostas.pop(0)
    return falsa


def test_prompt_manda_indices_e_nao_urls(monkeypatch):
    chamadas = []
    monkeypatch.setattr(gs, "_chamar_rota_ia", _ia(["[]"], chamadas))
    gs.extrair_vagas("3 vagas", "catho", "corpo", LINKS)
    prompt = chamadas[0]["prompt"]
    assert "[0] Desenvolvedor Python Jr" in prompt and "[2] Auxiliar Técnico" in prompt
    assert "https://" not in prompt.split("Corpo:")[0]      # nenhuma URL nas instruções
    assert chamadas[0]["max_tokens"] == gs.MAX_TOKENS_VAGAS


def test_link_por_indice_vira_url_real(monkeypatch):
    resposta = json.dumps([
        {"cargo": "Desenvolvedor Python Jr", "empresa": "ACME", "score": 90, "link": 0, "tipo": "nova_vaga"},
        {"cargo": "Analista de Dados", "score": "70", "link": "1"},
        {"cargo": "Auxiliar", "score": 20, "link": 9},                       # índice inexistente
        {"cargo": "Inventada", "score": 50, "link": "www.site-sem-http.com"},  # não é URL válida
    ])
    monkeypatch.setattr(gs, "_chamar_rota_ia", _ia([resposta], []))
    vagas = gs.extrair_vagas("a", "b", "c", LINKS)
    assert [v["link_candidatura"] for v in vagas] == [LINKS[0]["url"], LINKS[1]["url"], None, None]
    assert vagas[0]["score_match"] == 90 and vagas[1]["score_match"] == 70
    assert vagas[0]["empresa"] == "ACME" and vagas[0]["modelo_trabalho"] == "Não informado"
    assert gs.status_ultima_chamada() == "ok"


def test_resposta_cortada_usa_score_de_reserva_em_vez_de_zero(monkeypatch):
    cortada = '[{"cargo": "Desenvolvedor Python Jr", "tecnologias": ["python", "sql"], "link": 0}, {"cargo": "Anal'
    monkeypatch.setattr(gs, "_chamar_rota_ia", _ia([cortada], []))
    vagas = gs.extrair_vagas("a", "b", "c", LINKS)
    assert vagas[0]["cargo"] == "Desenvolvedor Python Jr"
    assert vagas[0]["score_match"] >= 45            # python + sql + desenvolvedor
    assert vagas[0]["link_candidatura"] == LINKS[0]["url"]


def test_texto_de_raciocinio_ganha_uma_segunda_chance(monkeypatch):
    chamadas = []
    respostas = ['/techs. "MIS" or empty if none explicitly mentioned', '[{"cargo": "Analista de BD", "score": 60}]']
    monkeypatch.setattr(gs, "_chamar_rota_ia", _ia(respostas, chamadas))
    vagas = gs.extrair_vagas("a", "b", "c", [])
    assert len(chamadas) == 2 and "não era JSON" in chamadas[1]["prompt"]
    assert vagas[0]["cargo"] == "Analista de BD" and gs.status_ultima_chamada() == "ok"


def test_segunda_resposta_ruim_vira_falha(monkeypatch):
    monkeypatch.setattr(gs, "_chamar_rota_ia", _ia(["blá", "blá de novo"], []))
    assert gs.extrair_vagas("a", "b", "c", []) == []
    assert gs.status_ultima_chamada() == "falha"


def test_limite_de_vagas_por_email(monkeypatch):
    muitas = json.dumps([{"cargo": f"Vaga {i}", "score": 50} for i in range(30)])
    monkeypatch.setattr(gs, "_chamar_rota_ia", _ia([muitas], []))
    assert len(gs.extrair_vagas("369 novas vagas", "catho", "c", [])) == gs.MAX_VAGAS_POR_EMAIL


def test_item_sem_cargo_e_descartado(monkeypatch):
    monkeypatch.setattr(gs, "_chamar_rota_ia", _ia(['[{"empresa": "X"}, {"cargo": "Dev", "score": 40}]'], []))
    assert [v["cargo"] for v in gs.extrair_vagas("a", "b", "c", [])] == ["Dev"]


def test_score_reserva():
    assert gs._score_reserva("Desenvolvedor Python", ["sql"]) >= 45
    assert gs._score_reserva("Auxiliar de Limpeza", []) == 20


def test_cliente_envia_max_tokens(monkeypatch, tmp_path):
    monkeypatch.setattr(rota_ia_client, "_PAUSA_PATH", str(tmp_path / "p.json"))
    monkeypatch.setattr(rota_ia_client, "ROTA_IA_API_KEY", "x")
    enviados = []

    class _R:
        status_code = 200
        text = ""

        def raise_for_status(self):
            pass

        def json(self):
            return {"choices": [{"message": {"content": "[]"}}]}

    monkeypatch.setattr(rota_ia_client.requests, "post", lambda url, **k: enviados.append(k["json"]) or _R())
    rota_ia_client.chamar_ia("oi")
    rota_ia_client.chamar_ia("oi", max_tokens=4096)
    assert enviados[0]["max_tokens"] == rota_ia_client.ROTA_IA_MAX_TOKENS
    assert enviados[1]["max_tokens"] == 4096
