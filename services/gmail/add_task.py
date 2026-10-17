"""
Módulo de Integração com Google Tasks - AutoApply_AI

Cria tarefas de follow-up na lista padrão (@default) do Google Tasks:
  - vagas de alta prioridade → "Candidatar: <cargo> @ <empresa>" com o link
    e prazo para o dia seguinte

O main.py chama criar_tarefa_vaga() automaticamente quando
GOOGLE_TASKS_ENABLED=true no .env. A autenticação é a mesma do resto do
projeto (google_auth.py) — na primeira vez com a opção ligada, o navegador
abre de novo para autorizar o escopo do Tasks.

Pré-requisitos:
- A API do Google Tasks ativada no projeto do Google Cloud.
- GOOGLE_TASKS_ENABLED=true no .env.

Teste rápido (cria uma tarefa de exemplo):
    python services/gmail/add_task.py
"""

import datetime
import os
import sys

from googleapiclient.errors import HttpError

sys.path.append(os.path.join(os.path.dirname(__file__), "..", ".."))
import google_auth  # noqa: E402


def construir_servico():
    """Cliente do Google Tasks, ou None se a integração estiver desligada no .env."""
    if not google_auth.TASKS_ENABLED:
        return None
    (service,) = google_auth.construir_servicos("tasks")
    return service


def criar_tarefa(service, titulo: str, notas: str = "", prazo: datetime.date | None = None) -> str | None:
    """Insere uma tarefa na lista padrão. Retorna o id criado, ou None em erro."""
    corpo = {"title": titulo[:1024], "notes": notas[:8000]}
    if prazo:
        # A API só considera a data; o horário é ignorado mas é obrigatório no formato.
        corpo["due"] = f"{prazo.isoformat()}T00:00:00.000Z"
    try:
        resultado = service.tasks().insert(tasklist="@default", body=corpo).execute()
        return resultado.get("id")
    except HttpError as e:
        print(f"[WARN] Google Tasks recusou a tarefa '{titulo[:40]}': {e}")
        return None


def criar_tarefa_vaga(service, vaga: dict) -> str | None:
    """Follow-up de candidatura para uma vaga de alta prioridade."""
    empresa = vaga.get("empresa") or "empresa não identificada"
    titulo = f"Candidatar: {vaga.get('cargo')} @ {empresa}"
    notas = "\n".join(filter(None, [
        f"Score de match: {vaga.get('score_gemini', 0)}%",
        f"Modelo: {vaga.get('modelo_trabalho')}" if vaga.get("modelo_trabalho") else None,
        f"Local: {vaga.get('local')}" if vaga.get("local") else None,
        f"Link: {vaga.get('link_candidatura')}" if vaga.get("link_candidatura") else None,
        "Criado automaticamente pelo AutoApply AI.",
    ]))
    amanha = datetime.date.today() + datetime.timedelta(days=1)
    return criar_tarefa(service, titulo, notas, prazo=amanha)


if __name__ == "__main__":
    servico = construir_servico()
    if not servico:
        print("Defina GOOGLE_TASKS_ENABLED=true no .env para usar o Google Tasks.")
        sys.exit(1)
    tarefa_id = criar_tarefa(servico, "Teste AutoApply AI", "Adicionado automaticamente via script.")
    print(f"Tarefa criada! ID: {tarefa_id}" if tarefa_id else "Falhou — veja o aviso acima.")
