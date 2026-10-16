import time

import main
from seguranca import redigir_segredo


def test_link_de_recuperacao_vira_so_dominio():
    assert redigir_segredo("https://accounts.spotify.com/reset?token=abc123") == "link: accounts.spotify.com"


def test_codigo_otp_e_mascarado():
    assert redigir_segredo("483920") == "••••20"
    assert redigir_segredo("12") == "••••"
    assert redigir_segredo(None) is None


def test_redigir_e_idempotente():
    for segredo in ("https://site.com/reset?t=1", "483920"):
        uma_vez = redigir_segredo(segredo)
        assert redigir_segredo(uma_vez) == uma_vez


def test_idade_minutos():
    agora_ms = int(time.time() * 1000)
    assert 9.9 < main._idade_minutos({"internalDate": str(agora_ms - 10 * 60_000)}) < 10.1
    assert main._idade_minutos({}) is None


def test_budget_zera_quando_o_dia_muda(monkeypatch):
    monkeypatch.setattr(main, "_GEMINI_BUDGET", {"data": "2000-01-01", "vagas": 999, "geral": 999, "alertas": 999})
    assert main._tem_budget("vagas")
    assert main._GEMINI_BUDGET["vagas"] == 0


def test_notificar_vaga_monta_mensagem(monkeypatch):
    enviadas = []
    monkeypatch.setattr(main, "telegram_configurado", lambda: True)
    monkeypatch.setattr(main, "enviar_telegram", lambda texto: enviadas.append(texto) or True)
    main.notificar_vaga({
        "cargo": "Dev Python <Jr>", "empresa": "ACME", "score_gemini": 88,
        "modelo_trabalho": "Remoto", "local": None, "link_candidatura": "https://x.com/v?a=1&b=2",
    })
    assert "Dev Python &lt;Jr&gt;" in enviadas[0]
    assert 'href="https://x.com/v?a=1&amp;b=2"' in enviadas[0]


def test_tarefa_de_follow_up_da_vaga():
    from services.gmail import add_task

    class _Tasks:
        def __init__(self):
            self.corpos = []

        def tasks(self):
            return self

        def insert(self, tasklist, body):
            self.corpos.append((tasklist, body))
            return self

        def execute(self):
            return {"id": "t1"}

    servico = _Tasks()
    vaga = {"cargo": "Dev Python", "empresa": "ACME", "score_gemini": 90,
            "modelo_trabalho": "Remoto", "link_candidatura": "https://x.com/v"}
    assert add_task.criar_tarefa_vaga(servico, vaga) == "t1"
    lista, corpo = servico.corpos[0]
    assert lista == "@default"
    assert corpo["title"] == "Candidatar: Dev Python @ ACME"
    assert "https://x.com/v" in corpo["notes"]
    assert corpo["due"].endswith("T00:00:00.000Z")
