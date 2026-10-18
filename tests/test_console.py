"""Saída segura no Windows: emoji em log redirecionado não pode derrubar o pipeline."""
import io
import sys

import pytest

import console
import main


def _stream_cp1252():
    return io.TextIOWrapper(io.BytesIO(), encoding="cp1252")   # stdout redirecionado no Windows


def test_sem_preparar_o_emoji_quebra():
    with pytest.raises(UnicodeEncodeError):
        print("🔥 vaga", file=_stream_cp1252())


def test_saida_redirecionada_vira_utf8(monkeypatch):
    out, err = _stream_cp1252(), _stream_cp1252()
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)
    console.preparar_console()
    print("🔥 Desenvolvedor Python — score 90% → https://x")
    out.flush()
    assert out.buffer.getvalue().decode("utf-8").startswith("🔥 Desenvolvedor Python")


def test_cores_so_em_terminal(monkeypatch):
    assert console.cores_ativas(_stream_cp1252()) is False
    monkeypatch.setenv("NO_COLOR", "1")
    assert console.cores_ativas(sys.__stdout__) is False


def test_log_sem_cores_nao_tem_codigo_ansi(monkeypatch, capsys):
    for attr in ("RESET", "BOLD", "GREEN", "BLUE", "YELLOW", "RED", "CYAN", "GRAY"):
        monkeypatch.setattr(main.Log, attr, getattr(main.Log, attr))   # restaura depois do teste
    main.Log.sem_cores()
    main.Log.ok("teste")
    assert "\033[" not in capsys.readouterr().out
