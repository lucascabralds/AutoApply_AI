"""
console.py — AutoApply AI
==========================
Deixa a saída do terminal segura no Windows.

O problema: os scripts imprimem emojis e símbolos (🔥 ⚡ ✔ ▶ →). No
terminal interativo do Windows isso funciona, mas quando a saída vai para
um arquivo ou pipe — `python main.py > log.txt`, Agendador de Tarefas,
o agendador.py rodando os outros scripts — o Python usa a codificação
local (cp1252) e o primeiro emoji derruba o processo com
UnicodeEncodeError no meio do pipeline. As cores ANSI do log também viram
lixo ("←[92m") no arquivo e em consoles antigos.

preparar_console() é chamada no início de cada script:
  - saída redirecionada → UTF-8 (o log abre certo no VS Code/Bloco de Notas);
  - terminal → mantém a codificação, mas troca o que não couber por "?";
  - Windows → liga o suporte a cores ANSI no console.
cores_ativas() diz se vale a pena colorir (terminal de verdade e sem NO_COLOR).
"""

import os
import sys


def _ligar_ansi_no_windows() -> None:
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        for handle_id in (-11, -12):   # STD_OUTPUT_HANDLE, STD_ERROR_HANDLE
            handle = kernel32.GetStdHandle(handle_id)
            modo = ctypes.c_uint32()
            if kernel32.GetConsoleMode(handle, ctypes.byref(modo)):
                kernel32.SetConsoleMode(handle, modo.value | 0x0004)   # ENABLE_VIRTUAL_TERMINAL_PROCESSING
    except Exception:
        pass


def preparar_console() -> None:
    for nome in ("stdout", "stderr"):
        stream = getattr(sys, nome, None)
        if stream is None or not hasattr(stream, "reconfigure"):
            continue
        try:
            if stream.isatty():
                stream.reconfigure(errors="replace")
            else:
                stream.reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass
    if os.name == "nt":
        _ligar_ansi_no_windows()


def cores_ativas(stream=None) -> bool:
    stream = stream or sys.stdout
    if os.getenv("NO_COLOR"):
        return False
    try:
        return stream.isatty()
    except (AttributeError, ValueError):
        return False
