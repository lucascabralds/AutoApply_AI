import datetime as dt

import pytest

import calendario as c

HOJE = dt.date(2026, 10, 16)


@pytest.mark.parametrize("texto, data, hora", [
    ("2026-10-29", dt.date(2026, 10, 29), None),
    ("2026-10-29T14:30:00", dt.date(2026, 10, 29), dt.time(14, 30)),
    ("vence em 29/10/2026", dt.date(2026, 10, 29), None),
    ("28/10 às 14h", dt.date(2026, 10, 28), dt.time(14, 0)),
    ("28/10 14:30", dt.date(2026, 10, 28), dt.time(14, 30)),
    ("sábado, 5 de dezembro às 9h30", dt.date(2026, 12, 5), dt.time(9, 30)),
    ("29 de outubro de 2026", dt.date(2026, 10, 29), None),
    ("05/01", dt.date(2027, 1, 5), None),          # janeiro próximo, não o que passou
    ("10/10/26", dt.date(2026, 10, 10), None),
    ("31/02/2026", None, None),
    ("amanhã", None, None),
    ("2026-11-05T00:00:00", dt.date(2026, 11, 5), None),          # data pura em ISO
    ("05/11 - duração 2h", dt.date(2026, 11, 5), None),           # 2h não é horário
    ("28/10, das 14h às 16h", dt.date(2026, 10, 28), dt.time(14, 0)),
    ("Consulta 28/10/2026 - 08:15", dt.date(2026, 10, 28), dt.time(8, 15)),
    ("28/10/2026 - duração 1:30", dt.date(2026, 10, 28), None),
    ("28/10/2026 as 2h de duração", dt.date(2026, 10, 28), None),
    ("Consulta às 14h do dia 28/10/2026", dt.date(2026, 10, 28), dt.time(14, 0)),
    ("14:30 28/10/2026", dt.date(2026, 10, 28), dt.time(14, 30)),
    ("28/10/2026, horário: 16:45", dt.date(2026, 10, 28), dt.time(16, 45)),
    (None, None, None),
])
def test_parsear_data_hora(texto, data, hora):
    assert c.parsear_data_hora(texto, HOJE) == (data, hora)


def test_evento_de_vencimento_dia_inteiro_com_lembrete():
    ev = c.evento_do_registro({
        "categoria": "financeiro", "subcategoria": "boleto_fatura", "remetente_nome": "Vivo", "data_evento": "29/10/2026",
        "valor_formatado": "R$ 99,90", "resumo": "Fatura de outubro", "link_principal": "https://vivo.com/2via",
    }, HOJE)
    assert ev["summary"] == "💰 Vence: Vivo — R$ 99,90"
    assert ev["start"] == {"date": "2026-10-29"} and ev["end"] == {"date": "2026-10-30"}
    assert ev["reminders"]["overrides"][0]["minutes"] == 2 * 24 * 60 - 9 * 60
    assert "https://vivo.com/2via" in ev["description"]


def test_evento_de_agendamento_com_horario():
    ev = c.evento_do_registro({"categoria": "agendamento", "remetente_nome": "Barbearia Hermanos",
                               "data_evento": "28/10 às 14h"}, HOJE)
    assert ev["summary"] == "📅 Barbearia Hermanos"
    assert ev["start"]["dateTime"] == "2026-10-28T14:00:00"
    assert ev["end"]["dateTime"] == "2026-10-28T15:00:00"
    assert ev["reminders"]["overrides"][0]["minutes"] == 60


def test_veiculo_usa_icone_proprio():
    ev = c.evento_do_registro({"categoria": "veiculo", "subcategoria": "veiculo_ipva", "remetente_nome": "Detran-SP",
                               "data_evento": "2026-11-10"}, HOJE)
    assert ev["summary"].startswith("🚗 Vence: Detran-SP")


@pytest.mark.parametrize("registro", [
    {"categoria": "financeiro", "data_evento": "01/10/2026"},     # já passou
    {"categoria": "financeiro", "data_evento": None},
    {"categoria": "comunicado", "data_evento": "29/10/2026"},     # categoria sem agenda
    {"categoria": "financeiro", "subcategoria": "pix", "data_evento": "16/10/2026"},     # PIX já feito
    {"categoria": "financeiro", "subcategoria": "recibo", "data_evento": "20/10/2026"},
    {"categoria": "veiculo", "subcategoria": "veiculo_pedagio", "data_evento": "20/10/2026"},
])
def test_sem_evento(registro):
    assert c.evento_do_registro(registro, HOJE) is None


def test_recibo_que_pede_acao_vira_evento():
    assert c.evento_do_registro({"categoria": "financeiro", "subcategoria": "recibo", "requer_acao": True,
                                 "data_evento": "20/10/2026"}, HOJE)
