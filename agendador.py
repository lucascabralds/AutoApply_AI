"""
agendador.py — AutoApply AI
============================
Mantém o AutoApply AI rodando sozinho (no Docker ou num servidor), sem cron:

  - alertas (OTP)       a cada ALERTAS_INTERVALO_MIN    (padrão: 1 min)
  - pipeline completo   a cada PIPELINE_INTERVALO_MIN   (padrão: 30 min)
  - dashboard           regenerado logo após cada pipeline
  - briefing diário     todo dia às BRIEFING_HORARIO    (padrão: 07:30; vazio desliga)

As tarefas rodam UMA DE CADA VEZ, cada uma num subprocesso: um erro (ou
travamento) em uma não derruba as outras, e os arquivos de cache/budget
nunca são escritos por dois processos ao mesmo tempo.

Uso:
    python agendador.py
"""

import datetime
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))

ALERTAS_INTERVALO_MIN  = int(os.getenv("ALERTAS_INTERVALO_MIN", "1"))
PIPELINE_INTERVALO_MIN = int(os.getenv("PIPELINE_INTERVALO_MIN", "30"))
BRIEFING_HORARIO       = os.getenv("BRIEFING_HORARIO", "07:30").strip()
TIMEOUT_TAREFA_MIN     = int(os.getenv("TIMEOUT_TAREFA_MIN", "20"))


def proximo_horario_diario(agora: datetime.datetime, horario: str) -> datetime.datetime:
    """Próxima ocorrência de "HH:MM" estritamente depois de `agora`."""
    hora, minuto = (int(p) for p in horario.split(":"))
    alvo = agora.replace(hour=hora, minute=minuto, second=0, microsecond=0)
    return alvo if alvo > agora else alvo + datetime.timedelta(days=1)


@dataclass
class Tarefa:
    nome: str
    argumentos: list[str]
    intervalo_min: int | None = None     # tarefa periódica…
    horario: str | None = None           # …ou diária em HH:MM
    proxima: datetime.datetime = field(default_factory=datetime.datetime.now)

    def reagendar(self, agora: datetime.datetime) -> None:
        if self.horario:
            self.proxima = proximo_horario_diario(agora, self.horario)
        else:
            self.proxima = agora + datetime.timedelta(minutes=self.intervalo_min)


def montar_tarefas(agora: datetime.datetime) -> list[Tarefa]:
    tarefas = [
        # O pipeline completo vem primeiro: na largada ele já cobre os alertas.
        Tarefa("pipeline", ["main.py"], intervalo_min=PIPELINE_INTERVALO_MIN, proxima=agora),
        # Dashboard logo depois do pipeline, com os dados recém-gravados
        Tarefa("dashboard", ["gerar_dashboard.py"], intervalo_min=PIPELINE_INTERVALO_MIN, proxima=agora),
        Tarefa("alertas", ["main.py", "--modulo", "alertas"], intervalo_min=ALERTAS_INTERVALO_MIN,
               proxima=agora + datetime.timedelta(minutes=ALERTAS_INTERVALO_MIN)),
    ]
    if BRIEFING_HORARIO:
        tarefas.append(Tarefa("briefing", ["briefing_diario.py"], horario=BRIEFING_HORARIO,
                              proxima=proximo_horario_diario(agora, BRIEFING_HORARIO)))
    return tarefas


def tarefas_vencidas(tarefas: list[Tarefa], agora: datetime.datetime) -> list[Tarefa]:
    """Tarefas cujo horário já chegou, na ordem em que foram declaradas."""
    return [t for t in tarefas if t.proxima <= agora]


def executar(tarefa: Tarefa) -> int:
    inicio = time.monotonic()
    print(f"[{datetime.datetime.now():%d/%m %H:%M:%S}] ▶ {tarefa.nome}", flush=True)
    try:
        codigo = subprocess.run(
            [sys.executable, *tarefa.argumentos],
            cwd=_BASE_DIR,
            timeout=TIMEOUT_TAREFA_MIN * 60,
        ).returncode
    except subprocess.TimeoutExpired:
        print(f"  ✖ {tarefa.nome} excedeu {TIMEOUT_TAREFA_MIN} min e foi interrompida", flush=True)
        return -1
    status = "✔" if codigo == 0 else f"✖ (código {codigo})"
    print(f"  {status} {tarefa.nome} em {time.monotonic() - inicio:.0f}s", flush=True)
    return codigo


def main() -> None:
    tarefas = montar_tarefas(datetime.datetime.now())
    print("AutoApply AI — agendador iniciado")
    for t in tarefas:
        quando = f"todo dia às {t.horario}" if t.horario else f"a cada {t.intervalo_min} min"
        print(f"  · {t.nome:<9} {quando}")

    while True:
        for tarefa in tarefas_vencidas(tarefas, datetime.datetime.now()):
            executar(tarefa)
            tarefa.reagendar(datetime.datetime.now())
        espera = (min(t.proxima for t in tarefas) - datetime.datetime.now()).total_seconds()
        time.sleep(max(1.0, espera))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nAgendador encerrado.")
