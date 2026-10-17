"""
diagnostico.py — AutoApply AI
==============================
Confere a configuração inteira antes de rodar o pipeline e diz exatamente
o que falta — sem gravar nada em lugar nenhum.

    python diagnostico.py
    python diagnostico.py --sem-rede     # só arquivos e .env, sem chamadas externas

Cada verificação devolve OK, AVISO (recurso opcional desligado/incompleto)
ou ERRO (o pipeline não vai funcionar). Sai com código 1 se houver ERRO.
"""

import argparse
import json
import os
import sys
from dataclasses import dataclass

import requests
from dotenv import load_dotenv

load_dotenv()

OK, AVISO, ERRO = "OK", "AVISO", "ERRO"


@dataclass
class Resultado:
    nome: str
    nivel: str
    detalhe: str


# ── Verificações locais ──────────────────────────────────────────────────────

def checar_google_arquivos() -> list[Resultado]:
    import google_auth

    res = []
    if os.path.exists(google_auth.CREDENTIALS_PATH):
        res.append(Resultado("credentials.json", OK, google_auth.CREDENTIALS_PATH))
    else:
        res.append(Resultado("credentials.json", ERRO,
                             f"não encontrado em {google_auth.CREDENTIALS_PATH} — baixe no Google Cloud Console"))

    if not os.path.exists(google_auth.TOKEN_PATH):
        nivel = ERRO if google_auth.HEADLESS else AVISO
        res.append(Resultado("token.json", nivel, "ainda não gerado — rode `python google_auth.py`"))
        return res

    faltando = set(google_auth.SCOPES) - google_auth._escopos_do_token(google_auth.TOKEN_PATH)
    if faltando:
        nomes = ", ".join(sorted(s.rsplit("/", 1)[-1] for s in faltando))
        nivel = ERRO if google_auth.HEADLESS else AVISO
        res.append(Resultado("token.json", nivel,
                             f"sem os escopos {nomes} — rode `python google_auth.py` para reautorizar"))
    else:
        res.append(Resultado("token.json", OK, "cobre todos os escopos configurados"))
    return res


def checar_rascunhos() -> list[Resultado]:
    import candidatura

    if not candidatura.RASCUNHOS_ATIVOS:
        return [Resultado("Rascunhos de candidatura", AVISO, "desligado (RASCUNHOS_CANDIDATURA=false)")]
    res = []
    perfil = candidatura.carregar_perfil()
    res.append(Resultado("Perfil profissional", OK if perfil else ERRO,
                         f"{len(perfil)} caracteres (sem a seção de uso interno)" if perfil
                         else f"não encontrado em {candidatura.PERFIL_PATH}"))
    if not candidatura.CURRICULO_PDF:
        res.append(Resultado("Currículo PDF", AVISO, "CURRICULO_PDF vazio — rascunhos sairão sem anexo"))
    elif os.path.isfile(candidatura.CURRICULO_PDF):
        res.append(Resultado("Currículo PDF", OK, candidatura.CURRICULO_PDF))
    else:
        res.append(Resultado("Currículo PDF", ERRO, f"arquivo não existe: {candidatura.CURRICULO_PDF}"))
    return res


# ── Verificações com rede ────────────────────────────────────────────────────

def checar_rota_ia() -> Resultado:
    import rota_ia_client as r

    if not r.ROTA_IA_API_KEY:
        return Resultado("Rota.IA", ERRO, "ROTA_IA_API_KEY vazio no .env")
    try:
        resp = requests.post(
            r.CHAT_COMPLETIONS_ENDPOINT,
            headers={"Authorization": f"Bearer {r.ROTA_IA_API_KEY}"},
            json={"model": r.ROTA_IA_MODEL, "messages": [{"role": "user", "content": "Responda apenas: ok"}],
                  "max_tokens": 5},
            timeout=30,
        )
    except requests.RequestException as e:
        return Resultado("Rota.IA", ERRO, f"sem resposta em {r.ROTA_IA_URL} ({type(e).__name__}) — o gateway está rodando?")
    if resp.status_code in (401, 403):
        return Resultado("Rota.IA", ERRO, "chave recusada — confira ROTA_IA_API_KEY (INTERNAL_API_KEY do painel)")
    if not resp.ok:
        return Resultado("Rota.IA", ERRO, f"HTTP {resp.status_code}: {resp.text[:120]}")
    try:
        resp.json()["choices"][0]["message"]["content"]
    except (ValueError, KeyError, IndexError, TypeError):
        return Resultado("Rota.IA", ERRO, f"resposta fora do formato OpenAI: {resp.text[:120]}")
    return Resultado("Rota.IA", OK, f"{r.ROTA_IA_URL} respondeu (modelo {r.ROTA_IA_MODEL})")


def checar_supabase() -> list[Resultado]:
    url = (os.getenv("SUPABASE_URL") or "").rstrip("/")
    chave = os.getenv("SUPABASE_KEY")
    if not url or not chave:
        return [Resultado("Supabase", AVISO, "SUPABASE_URL/KEY vazios — nada será gravado")]
    try:
        from supabase import create_client
        sb = create_client(url, chave)
    except Exception as e:
        return [Resultado("Supabase", ERRO, f"não conectou: {e}")]

    res = []
    tabelas = {
        "vagas_extraidas": "id, vaga_uid, empresa, score_match",
        "certificados_portfolio": "id",
        "log_acessos_otp": "id",
        "despesas_domesticas": "id",
    }
    for tabela, colunas in tabelas.items():
        try:
            sb.table(tabela).select(colunas).limit(1).execute()
            res.append(Resultado(f"Supabase: {tabela}", OK, "acessível"))
        except Exception as e:
            msg = str(e)
            if tabela == "vagas_extraidas" and ("vaga_uid" in msg or "PGRST204" in msg or "42703" in msg):
                res.append(Resultado(f"Supabase: {tabela}", AVISO,
                                     "sem as colunas da v3 — rode supabase/schema.sql no SQL Editor"))
            else:
                res.append(Resultado(f"Supabase: {tabela}", ERRO, f"{msg[:140]} — rode supabase/schema.sql"))
    return res


def checar_telegram() -> Resultado:
    import notificador

    if not notificador.telegram_configurado():
        return Resultado("Telegram", AVISO, "desligado (TELEGRAM_BOT_TOKEN/CHAT_ID vazios)")
    token = notificador.TELEGRAM_BOT_TOKEN
    try:
        resp = requests.get(f"https://api.telegram.org/bot{token}/getMe", timeout=10)
        dados = resp.json()
    except (requests.RequestException, ValueError) as e:
        # A mensagem de erro do requests traz a URL — e a URL traz o token do bot
        return Resultado("Telegram", ERRO, f"API do Telegram inacessível: {str(e).replace(token, '***')}")
    if not isinstance(dados, dict) or not dados.get("ok"):
        return Resultado("Telegram", ERRO, "token do bot inválido")
    return Resultado("Telegram", OK, f"bot @{dados['result'].get('username')} — teste com `python notificador.py oi`")


def checar_opcionais_google() -> list[Resultado]:
    import google_auth

    return [
        Resultado("Google Tasks", OK if google_auth.TASKS_ENABLED else AVISO,
                  "ligado" if google_auth.TASKS_ENABLED else "desligado (GOOGLE_TASKS_ENABLED=false)"),
        Resultado("Google Calendar", OK if google_auth.CALENDAR_ENABLED else AVISO,
                  "ligado" if google_auth.CALENDAR_ENABLED else "desligado (GOOGLE_CALENDAR_ENABLED=false)"),
    ]


# ── Execução ─────────────────────────────────────────────────────────────────

def executar(com_rede: bool = True) -> list[Resultado]:
    resultados = checar_google_arquivos() + checar_opcionais_google() + checar_rascunhos()
    if com_rede:
        resultados.append(checar_rota_ia())
        resultados += checar_supabase()
        resultados.append(checar_telegram())
    return resultados


def imprimir(resultados: list[Resultado]) -> None:
    cores = {OK: "\033[92m", AVISO: "\033[93m", ERRO: "\033[91m"}
    largura = max(len(r.nome) for r in resultados)
    print("\nAutoApply AI — diagnóstico\n")
    for r in resultados:
        print(f"  {cores[r.nivel]}{r.nivel:<5}\033[0m  {r.nome:<{largura}}  {r.detalhe}")
    erros = sum(r.nivel == ERRO for r in resultados)
    avisos = sum(r.nivel == AVISO for r in resultados)
    print(f"\n  {erros} erro(s), {avisos} aviso(s)\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="Confere a configuração do AutoApply AI")
    parser.add_argument("--sem-rede", action="store_true", help="Só verificações locais")
    parser.add_argument("--json", action="store_true", help="Saída em JSON")
    args = parser.parse_args()

    resultados = executar(com_rede=not args.sem_rede)
    if args.json:
        print(json.dumps([r.__dict__ for r in resultados], ensure_ascii=False, indent=2))
    else:
        imprimir(resultados)
    sys.exit(1 if any(r.nivel == ERRO for r in resultados) else 0)


if __name__ == "__main__":
    main()
