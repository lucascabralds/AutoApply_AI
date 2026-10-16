import datetime as dt

import agendador


def test_proximo_horario_hoje_ou_amanha():
    antes = dt.datetime(2026, 10, 16, 6, 0)
    depois = dt.datetime(2026, 10, 16, 8, 0)
    assert agendador.proximo_horario_diario(antes, "07:30") == dt.datetime(2026, 10, 16, 7, 30)
    assert agendador.proximo_horario_diario(depois, "07:30") == dt.datetime(2026, 10, 17, 7, 30)
    # exatamente no horário → próxima é amanhã (não roda duas vezes)
    assert agendador.proximo_horario_diario(dt.datetime(2026, 10, 16, 7, 30), "07:30") == dt.datetime(2026, 10, 17, 7, 30)


def test_largada_roda_pipeline_e_agenda_o_resto():
    agora = dt.datetime(2026, 10, 16, 6, 0)
    tarefas = agendador.montar_tarefas(agora)
    assert [t.nome for t in agendador.tarefas_vencidas(tarefas, agora)] == ["pipeline", "dashboard"]
    nomes = {t.nome: t for t in tarefas}
    assert nomes["briefing"].proxima == dt.datetime(2026, 10, 16, 7, 30)


def test_reagendar_periodica_e_diaria():
    agora = dt.datetime(2026, 10, 16, 7, 31)
    periodica = agendador.Tarefa("alertas", [], intervalo_min=1)
    diaria = agendador.Tarefa("briefing", [], horario="07:30")
    periodica.reagendar(agora)
    diaria.reagendar(agora)
    assert periodica.proxima == agora + dt.timedelta(minutes=1)
    assert diaria.proxima == dt.datetime(2026, 10, 17, 7, 30)


def test_executar_isola_falhas(monkeypatch, tmp_path):
    script = tmp_path / "falha.py"
    script.write_text("import sys; sys.exit(3)")
    assert agendador.executar(agendador.Tarefa("x", [str(script)], intervalo_min=1)) == 3
