"""
google_auth.py — AutoApply AI
==============================
Autenticação OAuth2 única para todos os scripts do projeto.

Antes, cada script (main.py, briefing_diario.py, add_task.py...) pedia um
conjunto DIFERENTE de escopos, mas todos gravavam no MESMO token.json. O
resultado era um "cabo de guerra": rodar o briefing recriava o token sem o
escopo do Drive (quebrando o upload de certificados no main.py), e o token
do main.py não tinha permissão de envio (quebrando o briefing).

Agora existe uma lista única de escopos e o token só é reaproveitado se
cobrir TODOS eles — caso contrário o fluxo OAuth roda de novo, uma vez.

Configuração (.env):
  GMAIL_CREDENTIALS_PATH  — caminho do credentials.json (padrão: raiz do projeto)
  GMAIL_TOKEN_PATH        — caminho do token.json      (padrão: raiz do projeto)
  GOOGLE_TASKS_ENABLED    — "true" para incluir o escopo do Google Tasks
                            (follow-ups automáticos de vagas)
  AUTOAPPLY_HEADLESS      — "true" em servidor/Docker: nunca tenta abrir o
                            navegador; falha com instrução clara se o token
                            não servir

Para gerar/renovar o token.json (ex.: antes de subir o Docker):
    python google_auth.py
"""

import json
import os

from dotenv import load_dotenv
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

load_dotenv()

_BASE_DIR        = os.path.dirname(os.path.abspath(__file__))
TOKEN_PATH       = os.getenv("GMAIL_TOKEN_PATH",       os.path.join(_BASE_DIR, "token.json"))
CREDENTIALS_PATH = os.getenv("GMAIL_CREDENTIALS_PATH", os.path.join(_BASE_DIR, "credentials.json"))

HEADLESS      = os.getenv("AUTOAPPLY_HEADLESS", "false").strip().lower() in ("1", "true", "sim", "yes")
TASKS_ENABLED = os.getenv("GOOGLE_TASKS_ENABLED", "false").strip().lower() in ("1", "true", "sim", "yes")

# gmail.modify cobre leitura, labels E envio (messages.send) — não é preciso
# pedir gmail.readonly/gmail.send separadamente.
SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/drive.file",
]
if TASKS_ENABLED:
    SCOPES.append("https://www.googleapis.com/auth/tasks")


def _escopos_do_token(path: str) -> set:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return set(json.load(f).get("scopes") or [])
    except Exception:
        return set()


def obter_credenciais(log=print) -> Credentials:
    """
    Retorna credenciais válidas para todos os SCOPES do projeto.
    Reaproveita o token.json quando ele cobre todos os escopos; faz refresh
    quando expirado; e só abre o navegador quando realmente necessário.
    """
    if not os.path.exists(CREDENTIALS_PATH):
        raise FileNotFoundError(
            f"credentials.json não encontrado em: {CREDENTIALS_PATH}\n"
            "Baixe em: console.cloud.google.com → APIs & Services → Credentials\n"
            "Certifique-se de habilitar Gmail API e Google Drive API no projeto"
            + (" (e Google Tasks API)." if TASKS_ENABLED else ".")
        )

    creds = None
    if os.path.exists(TOKEN_PATH):
        faltando = set(SCOPES) - _escopos_do_token(TOKEN_PATH)
        if faltando:
            log(f"token.json não cobre os escopos {sorted(faltando)} — será recriado.")
        else:
            try:
                creds = Credentials.from_authorized_user_file(TOKEN_PATH, SCOPES)
            except Exception as e:
                log(f"Token inválido, será recriado: {e}")

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except Exception as e:
            log(f"Refresh do token falhou ({e}), iniciando novo fluxo OAuth...")
            creds = None
    else:
        creds = None

    if not creds and HEADLESS:
        raise RuntimeError(
            "token.json ausente, inválido ou sem todos os escopos e AUTOAPPLY_HEADLESS=true "
            "(não há navegador aqui). Rode `python google_auth.py` na sua máquina e copie o "
            "token.json gerado para este ambiente."
        )

    if not creds:
        log("Abrindo navegador para autenticação OAuth2...")
        flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_PATH, SCOPES)
        creds = flow.run_local_server(port=0)

    with open(TOKEN_PATH, "w", encoding="utf-8") as fh:
        fh.write(creds.to_json())
    return creds


def construir_servicos(*apis: str, log=print) -> tuple:
    """
    Constrói os clientes pedidos, ex.: construir_servicos("gmail", "drive").
    Retorna uma tupla na mesma ordem.
    """
    versoes = {"gmail": "v1", "drive": "v3", "tasks": "v1"}
    creds = obter_credenciais(log=log)
    return tuple(build(api, versoes[api], credentials=creds) for api in apis)


if __name__ == "__main__":
    obter_credenciais()
    print(f"✅ token.json pronto em {TOKEN_PATH}")
    print(f"   Escopos: {', '.join(s.rsplit('/', 1)[-1] for s in SCOPES)}")
