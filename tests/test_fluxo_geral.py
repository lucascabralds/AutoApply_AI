"""Módulo de e-mails gerais: veículo, despesas e eventos no Calendar."""
import datetime as dt

import pytest

import main
from tests.test_fluxo_vagas import _FakeGmail, _b64url


class _FakeSB:
    def __init__(self):
        self.gravados = []

    def table(self, nome):
        sb = self

        class _Q:
            def upsert(self, registro, on_conflict=None):
                sb.gravados.append((nome, registro))
                return self

            insert = upsert

            def execute(self):
                return self
        return _Q()


class _FakeCalendar:
    def __init__(self):
        self.eventos = []

    def events(self):
        return self

    def insert(self, calendarId, body):
        self.eventos.append(body)
        return self

    def execute(self):
        return {"id": f"ev{len(self.eventos)}"}


def _msg(assunto, remetente, corpo):
    return {"payload": {"headers": [{"name": "Subject", "value": assunto}, {"name": "From", "value": remetente}],
                        "mimeType": "text/plain", "body": {"data": _b64url(corpo)}}}


@pytest.fixture
def ambiente(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "_GEMINI_CACHE", {})
    monkeypatch.setattr(main, "_CACHE_PATH", str(tmp_path / "c.json"))
    monkeypatch.setattr(main, "_BUDGET_PATH", str(tmp_path / "b.json"))
    monkeypatch.setattr(main, "_PENDENTES_IA", set())
    monkeypatch.setattr(main, "gateway_disponivel", lambda: True)
    monkeypatch.setattr(main, "_status_ia", lambda: "ok")
    monkeypatch.setattr(main, "_GEMINI_BUDGET", {"data": dt.date.today().isoformat(),
                                                 "vagas": 0, "geral": 0, "alertas": 0, "cartas": 0})
    vencimento = (dt.date.today() + dt.timedelta(days=10)).strftime("%d/%m/%Y")
    respostas = {
        "ipva": {"categoria": "financeiro", "resumo": "IPVA 2027 1ª parcela", "remetente_nome": "Detran-SP",
                 "data_evento": vencimento, "valor": "3x de R$ 412,50"},
        "aviso": {"categoria": "outro", "resumo": "Mudança de horário da agência"},
    }
    monkeypatch.setattr(main, "_gs_classificar_geral",
                        lambda rem, ass, corpo: respostas["ipva" if "IPVA" in ass else "aviso"])
    gmail = _FakeGmail()
    gmail.mensagens = {
        "v1": _msg("IPVA 2027: boleto disponível", "Detran SP <noreply@detran.sp.gov.br>", "Pague o IPVA do veículo"),
        "c1": _msg("Aviso importante", "Banco <avisos@banco.com>", "Informamos que a agência mudou de horário"),
    }
    return gmail, vencimento


def test_ipva_vira_veiculo_despesa_e_evento(ambiente):
    gmail, vencimento = ambiente
    sb, cal = _FakeSB(), _FakeCalendar()
    labels = {"geral_pai": "G", "geral_veiculo": "G_VEI", "geral_comunicado": "G_COM", "geral_servicos": "G_SRV"}

    main.rodar_emails_gerais(gmail, labels, sb, "newer_than:1d", dry_run=False, calendar_service=cal)
    main.rodar_emails_gerais(gmail, labels, sb, "newer_than:1d", dry_run=False, calendar_service=cal)

    # a IA disse "financeiro", mas a heurística de veículo (Detran/IPVA) prevalece
    assert gmail.labels_aplicadas["v1"][:2] == ["G", "G_VEI"]
    # "outro" da IA não apaga o comunicado da heurística
    assert gmail.labels_aplicadas["c1"][:2] == ["G", "G_COM"]

    despesa = next(r for tabela, r in sb.gravados if tabela == "despesas_domesticas")
    assert despesa["categoria"] == "veiculo_ipva"
    assert despesa["valor"] == 412.50
    assert despesa["estabelecimento"] == "Detran-SP"

    assert len(cal.eventos) == 1   # 2ª execução não duplica o evento
    ev = cal.eventos[0]
    assert ev["summary"] == "🚗 Vence: Detran-SP — R$ 412,50"
    d, m, a = vencimento.split("/")
    assert ev["start"] == {"date": f"{a}-{m}-{d}"}


def test_sem_calendar_configurado_nao_tenta_agendar(ambiente):
    gmail, _ = ambiente
    main.rodar_emails_gerais(gmail, {}, _FakeSB(), "newer_than:1d", dry_run=False, calendar_service=None)
    assert main._cache_get("evento", "v1", str) is None


def test_ia_indisponivel_deixa_vencimento_para_depois(ambiente, monkeypatch):
    gmail, _ = ambiente
    monkeypatch.setattr(main, "gateway_disponivel", lambda: False)
    main.rodar_emails_gerais(gmail, {"geral_pai": "G"}, _FakeSB(), "newer_than:1d", dry_run=False,
                             calendar_service=_FakeCalendar())
    assert "v1" not in gmail.labels_aplicadas   # volta com data/valor quando a IA responder


def test_vistoria_do_detran_vira_agendamento(ambiente, monkeypatch):
    gmail, _ = ambiente
    gmail.mensagens = {"a1": _msg("Vistoria veicular agendada", "Detran <noreply@detran.sp.gov.br>",
                                  "Sua vistoria veicular está marcada")}
    data = (dt.date.today() + dt.timedelta(days=3)).strftime("%d/%m/%Y")
    monkeypatch.setattr(main, "_gs_classificar_geral", lambda *a: {
        "categoria": "agendamento", "remetente_nome": "Detran-SP", "data_evento": f"{data} às 14h"})
    cal = _FakeCalendar()
    main.rodar_emails_gerais(gmail, {"geral_pai": "G", "geral_agendamento": "G_AG"}, _FakeSB(),
                             "newer_than:1d", dry_run=False, calendar_service=cal)
    assert gmail.labels_aplicadas["a1"] == ["G", "G_AG"]
    assert cal.eventos[0]["summary"] == "📅 Detran-SP" and "dateTime" in cal.eventos[0]["start"]
