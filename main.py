"""
main.py — AutoApply AI
======================
Orquestrador principal do pipeline. Roda todos os módulos na sequência correta,
armazena os resultados no Supabase e gerencia as labels do Gmail.

Fluxo de execução:
  1. Autenticação Gmail (escopo modify — necessário para labels)
  2. Autenticação Google Drive (para upload de certificados)
  3. Garante que todas as labels existam no Gmail do usuário
  4. Módulo de vagas  → extrai + analisa com Gemini → salva no Supabase
  5. Módulo de certificados → baixa PDFs/links → faz upload no Google Drive
  6. Módulo de alertas de acesso → extrai OTPs/links de senha → exibe no terminal

Uso:
    python main.py              # roda tudo (e-mails de hoje)
    python main.py --dias 7     # amplia a janela para os últimos N dias
    python main.py --modulo vagas
    python main.py --modulo certificados
    python main.py --modulo alertas
    python main.py --dry-run    # executa sem gravar no Supabase

Correções aplicadas (v2):
  [FIX-1]  Migração google.generativeai → google.genai (package não-deprecated)
  [FIX-2]  analisar_vaga_gemini() agora garante retorno dict mesmo quando Gemini
            devolve lista ou string inesperada → elimina AttributeError linha 573
  [FIX-3]  Filtro de vagas aprimorado: palavras de exclusão (cursos, bootcamps,
            certificados, newsletters) impedem falsos positivos
  [FIX-4]  Labels separadas: vagas reais → 'vagas_processadas';
            cursos/não-vagas → 'nao_vaga' (nova label); certificados → fluxo próprio
  [FIX-5]  Cor da label 'alerta_acesso' corrigida para paleta válida do Gmail
  [FIX-6]  Supabase URL sanitizado (remove barra final) + upsert com on_conflict
  [FIX-7]  'link_candidatura' adicionado ao dict de vaga (extraído do corpo)
  [FIX-8]  Upload de certificados direto no Google Drive via Drive API
  [FIX-9]  Gemini: analisar_vaga_gemini inclui campo 'eh_vaga_real' para o
            próprio modelo distinguir vagas de cursos/newsletters
"""

import os
import re
import sys
import json
import time
import base64
import argparse
import datetime
import urllib.parse
import requests
import io
from html import unescape

from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Google / Gmail / Drive
# ---------------------------------------------------------------------------
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseUpload

# ---------------------------------------------------------------------------
# [FIX-1] Gemini — migrado para google.genai (não-deprecated)
# ---------------------------------------------------------------------------
try:
    from google import genai as google_genai
    from google.genai import types as genai_types
    GENAI_NEW = True
except ImportError:
    # fallback para quem ainda não atualizou o package
    import google.generativeai as genai_legacy
    GENAI_NEW = False

# ---------------------------------------------------------------------------
# Supabase
# ---------------------------------------------------------------------------
from supabase import create_client, Client

load_dotenv()

# ===========================================================================
# CACHE LOCAL DE RESPOSTAS GEMINI + CONTROLE DE BUDGET DIÁRIO
# ===========================================================================
# Estratégia para economizar tokens:
#
#  1. CACHE DE RESPOSTA (cache_gemini.json)
#     Salva o resultado de cada chamada Gemini indexado pelo gmail_message_id.
#     Se o mesmo e-mail aparecer em outra execução, reutiliza a resposta local
#     — zero tokens consumidos.
#
#  2. BUDGET DIÁRIO (gemini_budget.json)
#     Registra quantas chamadas foram feitas hoje.
#     Limites configuráveis via .env:
#       GEMINI_BUDGET_VAGAS=40    (padrão: 40 chamadas para vagas)
#       GEMINI_BUDGET_GERAL=10    (padrão: 10 chamadas para e-mails gerais)
#       GEMINI_BUDGET_ALERTAS=10  (padrão: 10 para alertas)
#     Quando o budget zera, usa heurística local — nunca trava o pipeline.
#
#  3. PRÉ-FILTRO HEURÍSTICO
#     Antes de qualquer chamada Gemini, classifica o e-mail localmente.
#     Só fontes conhecidas (Indeed, Catho…) ou e-mails com palavras-chave
#     de vaga passam para o Gemini. Cursos, newsletters → descartados grátis.

import hashlib

_CACHE_PATH  = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache_gemini.json")
_BUDGET_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gemini_budget.json")

_BUDGET_LIMITE = {
    "vagas":   int(os.getenv("GEMINI_BUDGET_VAGAS",   "40")),
    "geral":   int(os.getenv("GEMINI_BUDGET_GERAL",   "10")),
    "alertas": int(os.getenv("GEMINI_BUDGET_ALERTAS", "10")),
}

# Carrega cache em memória uma vez por execução
def _carregar_cache() -> dict:
    try:
        if os.path.exists(_CACHE_PATH):
            with open(_CACHE_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return {}

def _salvar_cache(cache: dict) -> None:
    try:
        with open(_CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

def _cache_key(tipo: str, msg_id: str) -> str:
    """Chave de cache: tipo + gmail_message_id."""
    return f"{tipo}:{msg_id}"

_GEMINI_CACHE: dict = _carregar_cache()


# ── Budget diário ────────────────────────────────────────────────────────────

def _carregar_budget() -> dict:
    hoje = datetime.date.today().isoformat()
    try:
        if os.path.exists(_BUDGET_PATH):
            with open(_BUDGET_PATH, "r") as f:
                dados = json.load(f)
            if dados.get("data") == hoje:
                return dados
    except Exception:
        pass
    return {"data": hoje, "vagas": 0, "geral": 0, "alertas": 0}

def _salvar_budget(budget: dict) -> None:
    try:
        with open(_BUDGET_PATH, "w") as f:
            json.dump(budget, f)
    except Exception:
        pass

_GEMINI_BUDGET: dict = _carregar_budget()


def _tem_budget(modulo: str) -> bool:
    """Retorna True se ainda há chamadas disponíveis para este módulo hoje."""
    return _GEMINI_BUDGET.get(modulo, 0) < _BUDGET_LIMITE.get(modulo, 10)

def _consumir_budget(modulo: str) -> None:
    """Incrementa o contador de chamadas do módulo e persiste."""
    _GEMINI_BUDGET[modulo] = _GEMINI_BUDGET.get(modulo, 0) + 1
    _salvar_budget(_GEMINI_BUDGET)

def _status_budget() -> str:
    lim = _BUDGET_LIMITE
    b   = _GEMINI_BUDGET
    return (
        f"vagas {b.get('vagas',0)}/{lim['vagas']} | "
        f"geral {b.get('geral',0)}/{lim['geral']} | "
        f"alertas {b.get('alertas',0)}/{lim['alertas']}"
    )

# ===========================================================================
# CONFIGURAÇÃO GLOBAL
# ===========================================================================

# Gmail: escopo modify é obrigatório para criar/aplicar labels
# Drive: scope para upload de arquivos
SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/drive.file",   # upload no Drive
]

_BASE_DIR        = os.path.dirname(os.path.abspath(__file__))
TOKEN_PATH       = os.getenv("GMAIL_TOKEN_PATH",       os.path.join(_BASE_DIR, "token.json"))
CREDENTIALS_PATH = os.getenv("GMAIL_CREDENTIALS_PATH", os.path.join(_BASE_DIR, "credentials.json"))

PASTA_CERTIFICADOS = os.getenv("CERTIFICADOS_LOCAL", os.path.join(_BASE_DIR, "certificados"))
DRIVE_FOLDER_ID    = os.getenv("DRIVE_FOLDER_ID", None)  # ID da pasta no Drive (opcional)

# [FIX-6] Remove barra final da URL do Supabase
SUPABASE_URL = (os.getenv("SUPABASE_URL") or "").rstrip("/")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Hierarquia de labels — cada domínio é isolado, sem cruzamento
#
# Estrutura no Gmail (barra = sub-label nativa):
#
#   AutoApply/Vagas/Alta_Prioridade   score ≥ 75
#   AutoApply/Vagas/Baixa_Prioridade  score < 75
#   AutoApply/Certificados/Extraido   arquivo salvo com sucesso
#   AutoApply/Certificados/Pendente   requer ação manual
#   AutoApply/Alertas/OTP             código numérico / novo login
#   AutoApply/Alertas/Recuperacao     link de recuperação de senha
#   AutoApply/Geral/Agendamento       confirmações de agendamento, reservas
#   AutoApply/Geral/Financeiro        cobranças, faturas, recibos
#   AutoApply/Geral/Servicos          assinaturas, apps, notificações diversas
#
# Regra de isolamento: cada módulo chama _aplicar_labels() apenas com
# as chaves do SEU domínio. Nenhum módulo toca em labels de outro.
# ---------------------------------------------------------------------------
LABEL_TREE = {
    # ── VAGAS ────────────────────────────────────────────────────────────────
    "vagas_pai":    {"nome": "AutoApply/Vagas",                   "cor_bg": "#16a765", "cor_txt": "#ffffff"},
    "vagas_alta":   {"nome": "AutoApply/Vagas/Alta_Prioridade",   "cor_bg": "#16a765", "cor_txt": "#ffffff"},
    "vagas_baixa":  {"nome": "AutoApply/Vagas/Baixa_Prioridade",  "cor_bg": "#b9e4d0", "cor_txt": "#094228"},

    # ── CERTIFICADOS ─────────────────────────────────────────────────────────
    "cert_pai":      {"nome": "AutoApply/Certificados",           "cor_bg": "#3c78d8", "cor_txt": "#ffffff"},
    "cert_extraido": {"nome": "AutoApply/Certificados/Extraido",  "cor_bg": "#3c78d8", "cor_txt": "#ffffff"},
    "cert_pendente": {"nome": "AutoApply/Certificados/Pendente",  "cor_bg": "#c9daf8", "cor_txt": "#1c4587"},

    # ── ALERTAS DE ACESSO ────────────────────────────────────────────────────
    "alerta_pai":          {"nome": "AutoApply/Alertas",                 "cor_bg": "#eaa041", "cor_txt": "#000000"},
    "alerta_otp":          {"nome": "AutoApply/Alertas/OTP",             "cor_bg": "#eaa041", "cor_txt": "#000000"},
    "alerta_recuperacao":  {"nome": "AutoApply/Alertas/Recuperacao",     "cor_bg": "#fce8b3", "cor_txt": "#7a4706"},

    # ── E-MAILS GERAIS (não são vagas/cert/alertas) ──────────────────────────
    "geral_pai":          {"nome": "AutoApply/Geral",                    "cor_bg": "#999999", "cor_txt": "#ffffff"},
    "geral_agendamento":  {"nome": "AutoApply/Geral/Agendamento",        "cor_bg": "#999999", "cor_txt": "#ffffff"},
    "geral_financeiro":   {"nome": "AutoApply/Geral/Financeiro",         "cor_bg": "#666666", "cor_txt": "#ffffff"},
    "geral_comunicado":   {"nome": "AutoApply/Geral/Comunicado",         "cor_bg": "#4a86e8", "cor_txt": "#ffffff"},
    "geral_servicos":     {"nome": "AutoApply/Geral/Servicos",           "cor_bg": "#cccccc", "cor_txt": "#000000"},
}

# Ordem de criação: pais antes dos filhos (obrigatório pela Gmail API)
_LABEL_ORDEM = [
    "vagas_pai",   "vagas_alta",    "vagas_baixa",
    "cert_pai",    "cert_extraido", "cert_pendente",
    "alerta_pai",  "alerta_otp",    "alerta_recuperacao",
    "geral_pai",   "geral_agendamento", "geral_financeiro",
    "geral_comunicado", "geral_servicos",
]

# Paleta de cores válidas do Gmail (backgroundColor)
_PALETA_GMAIL = {
    "#000000","#434343","#666666","#999999","#cccccc","#efefef","#f3f3f3","#ffffff",
    "#fb4c2f","#ffad47","#fad165","#16a765","#43d692","#4a86e8","#a479e2","#f691b3",
    "#f6c5be","#ffe6c7","#fef1d1","#b9e4d0","#c6f3de","#c9daf8","#e4d7f5","#fcdee8",
    "#efa093","#ffd6a2","#fce8b3","#89d3b2","#a0eac9","#a4c2f4","#d0bcf1","#fbc8d9",
    "#e66550","#ffbc6b","#fcda83","#44b984","#68dfa9","#6d9eeb","#b694e8","#f7a7c0",
    "#cc3a21","#eaa041","#f2c960","#149e60","#3dc789","#3c78d8","#8e63ce","#e07798",
    "#ac2b16","#cf8933","#d5ae49","#0b804b","#2a9c68","#285bac","#653e9b","#b65775",
    "#822111","#a46a21","#aa8831",""#076239","#1a764d","#1c4587","#41236d","#83334c",
    "#464646","#e7e7e7","#0d3472","#b6cff5","#0d3b44","#98d7e4","#3d188e","#e3d7ff",
    "#711a36","#fbd3e0","#8a1c0a","#f2b2a8","#7a2e0b","#ffc8af","#7a4706","#ffdeb5",
    "#594c05","#fbe983","#684e07","#fdedc1","#0b4f30","#b3efd3","#04502e","#a2dcc1",
    "#c2c2c2","#4986e7","#2da2bb","#b99aff","#994a64","#f691b2","#ff7537","#ffad46",
    "#662e37","#ebdbde","#cca6ac","#094228","#42d692","#16a765",
}

# ---------------------------------------------------------------------------
# Fontes de vagas conhecidas
# ---------------------------------------------------------------------------
FONTES_VAGAS = {
    "jobbol":       ["jobbol"],
    "indeed":       ["indeed"],
    "linkedin":     ["linkedin"],
    "catho":        ["catho"],
    "infojobs":     ["infojobs"],
    "vagas":        ["vagas.com.br", "vagas.com"],
    "gupy":         ["gupy.io", "gupy"],
    "glassdoor":    ["glassdoor"],
    "trampos":      ["trampos.co"],
    "remotar":      ["remotar"],
    "programathor": ["programathor"],
    "pandape":      ["pandape"],
    "micro1":       ["micro1"],
}

# Palavras que sugerem vaga real
PALAVRAS_VAGA = [
    "vaga", "job opening", "job alert", "nova vaga", "desenvolvedor",
    "developer", "engenheiro", "engineer", "analista", "estágio",
    "recrutamento", "we are hiring", "estamos contratando",
    "processo seletivo aberto", "oportunidade de emprego",
]

# [FIX-3] Palavras que indicam NÃO ser vaga (cursos, newsletters, bootcamps)
PALAVRAS_NAO_VAGA = [
    # Educação e certificações
    "certificado", "curso", "bootcamp", "aula", "módulo",
    "trilha de aprendizado", "material didático", "novo anúncio educacional",
    "instrutor", "aprenda", "aprenda a", "criando aplicativos",
    "do zero ao avançado", "passo a passo", "aula grátis",

    # Plataformas de ensino
    "udemy", "dio.me", "alura", "coursera", "rocketseat", "origamid",
    "b7web", "devmedia", "hackerrank", "tryhackme", "codewars",

    # Newsletters técnicas que parecem vagas
    "pare de criar apenas", "apenas cruds", "projetos pessoais",
    "aprenda apis", "stack tecnológica", "projeto completo do zero",
    "fastify & typescript", "drizzle orm", "agente de ia",

    # Programas de capacitação
    "generation brasil", "lista de interesse", "lista de espera",
    "lista de candidatos",

    # Marketing / promoções
    "oferta do dia", "promoção", "desconto", "newsletter",
    "assinatura", "unsubscribe", "cancelar inscrição",

    # Confirmações que NÃO são de vaga de emprego
    "confirmação de inscrição",  # inscrição em curso/evento, não candidatura de vaga
]

TECNOLOGIAS = [
    "python", "java", "javascript", "typescript", "node", "nodejs",
    "react", "angular", "vue", "next.js", "django", "flask", "fastapi",
    "spring", "sql", "mysql", "postgresql", "postgres", "mongodb", "redis",
    "aws", "azure", "gcp", "docker", "kubernetes", "git", "linux",
    "power bi", "powerbi", "tableau", "excel", "vba", "dax",
    "go", "golang", "rust", "php", "c#", ".net",
    "machine learning", "data science", "rpa", "selenium", "etl",
]

EXTENSOES_RUIM = (
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".ico",
    ".mp4", ".mov", ".css", ".js", ".woff", ".woff2", ".ttf",
)


# ===========================================================================
# LOGGING COLORIDO NO TERMINAL
# ===========================================================================

class Log:
    RESET  = "\033[0m"
    BOLD   = "\033[1m"
    GREEN  = "\033[92m"
    BLUE   = "\033[94m"
    YELLOW = "\033[93m"
    RED    = "\033[91m"
    CYAN   = "\033[96m"
    GRAY   = "\033[90m"

    @staticmethod
    def _ts():
        return datetime.datetime.now().strftime("%H:%M:%S")

    @classmethod
    def info(cls, msg):
        print(f"{cls.GRAY}[{cls._ts()}]{cls.RESET} {cls.BLUE}INFO{cls.RESET}  {msg}")

    @classmethod
    def ok(cls, msg):
        print(f"{cls.GRAY}[{cls._ts()}]{cls.RESET} {cls.GREEN} OK  {cls.RESET}  {msg}")

    @classmethod
    def warn(cls, msg):
        print(f"{cls.GRAY}[{cls._ts()}]{cls.RESET} {cls.YELLOW}WARN{cls.RESET}  {msg}")

    @classmethod
    def error(cls, msg):
        print(f"{cls.GRAY}[{cls._ts()}]{cls.RESET} {cls.RED} ERR {cls.RESET}  {msg}")

    @classmethod
    def section(cls, titulo):
        bar = "═" * 56
        print(f"\n{cls.CYAN}{bar}{cls.RESET}")
        print(f"{cls.CYAN}  {titulo.upper()}{cls.RESET}")
        print(f"{cls.CYAN}{bar}{cls.RESET}\n")


# ===========================================================================
# 1. AUTENTICAÇÃO GMAIL + DRIVE (escopo unificado)
# ===========================================================================

def autenticar_google() -> tuple:
    """
    OAuth2 unificado para Gmail (modify) + Drive (file upload).
    Retorna (gmail_service, drive_service).
    """
    if not os.path.exists(CREDENTIALS_PATH):
        raise FileNotFoundError(
            f"credentials.json não encontrado em: {CREDENTIALS_PATH}\n"
            "Baixe em: console.cloud.google.com → APIs & Services → Credentials\n"
            "Certifique-se de habilitar Gmail API e Google Drive API no projeto."
        )

    creds = None
    if os.path.exists(TOKEN_PATH):
        try:
            creds = Credentials.from_authorized_user_file(TOKEN_PATH, SCOPES)
        except Exception as e:
            Log.warn(f"Token inválido, será recriado: {e}")
            os.remove(TOKEN_PATH)
            creds = None

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            Log.info("Token expirado — fazendo refresh...")
            try:
                creds.refresh(Request())
                Log.ok("Token renovado.")
            except Exception as e:
                Log.warn(f"Refresh falhou ({e}), iniciando novo fluxo OAuth...")
                creds = None

        if not creds:
            Log.info("Abrindo navegador para autenticação OAuth2 (Gmail + Drive)...")
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_PATH, SCOPES)
            creds = flow.run_local_server(port=0)
            Log.ok("Autenticação concluída.")

        with open(TOKEN_PATH, "w") as fh:
            fh.write(creds.to_json())
        Log.info(f"Token salvo em: {TOKEN_PATH}")

    gmail_service = build("gmail", "v1", credentials=creds)
    drive_service = build("drive", "v3", credentials=creds)
    Log.ok("Gmail e Drive autenticados.")
    return gmail_service, drive_service


# ===========================================================================
# 2. GERENCIAMENTO DE LABELS
# ===========================================================================

def garantir_labels(service) -> dict:
    """
    Garante a hierarquia completa de labels do LABEL_TREE no Gmail do usuário.
    Cria na ordem correta (pais antes de filhos). Retorna {chave: gmail_label_id}.
    """
    Log.section("Verificando hierarquia de labels")
    resultado  = service.users().labels().list(userId="me").execute()
    # índice por nome exato (case-sensitive para sub-labels tipo "AutoApply/Vagas")
    existentes = {lb["name"]: lb["id"] for lb in resultado.get("labels", [])}

    mapa = {}
    criadas = 0

    for chave in _LABEL_ORDEM:
        cfg  = LABEL_TREE[chave]
        nome = cfg["nome"]

        if nome in existentes:
            mapa[chave] = existentes[nome]
            Log.info(f"  ✓ Existente : {nome}")
            continue

        body = {
            "name":                  nome,
            "labelListVisibility":   "labelShow",
            "messageListVisibility": "show",
            "color": {
                "backgroundColor": cfg["cor_bg"],
                "textColor":       cfg["cor_txt"],
            },
        }
        try:
            nova = service.users().labels().create(userId="me", body=body).execute()
            mapa[chave]      = nova["id"]
            existentes[nome] = nova["id"]   # cache local para filhos encontrarem o pai
            Log.ok(f"  + Criada    : {nome}")
            criadas += 1
        except HttpError as e:
            Log.warn(f"  ! Falha ao criar '{nome}': {e}")
            mapa[chave] = None

    print()
    total_ok = len([v for v in mapa.values() if v])
    Log.ok(f"Labels prontas: {total_ok}/{len(_LABEL_ORDEM)} ({criadas} criadas agora)")
    return mapa


def _aplicar_label(service, msg_id: str, *label_ids):
    """
    Aplica uma ou mais labels a uma mensagem em chamada única.
    Silencia erros não-críticos (label já aplicada, msg inexistente).
    """
    ids = [lid for lid in label_ids if lid]
    if not ids:
        return
    try:
        service.users().messages().modify(
            userId="me",
            id=msg_id,
            body={"addLabelIds": ids},
        ).execute()
    except HttpError as e:
        Log.warn(f"  Erro ao aplicar label(s) na msg {msg_id}: {e}")


# ===========================================================================
# 3. HELPERS DE DECODIFICAÇÃO DE E-MAIL
# ===========================================================================

def _decode_b64(data: str) -> str:
    if not data:
        return ""
    data = data.replace("-", "+").replace("_", "/")
    pad = 4 - (len(data) % 4)
    if pad != 4:
        data += "=" * pad
    try:
        return base64.b64decode(data).decode("utf-8", errors="ignore")
    except Exception:
        return ""


def _limpar_html(texto: str) -> str:
    if not texto:
        return ""
    texto = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", texto, flags=re.DOTALL | re.IGNORECASE)
    texto = re.sub(r"[a-zA-Z][a-zA-Z0-9_\-\s,.#:>+*]*\{[^{}]{1,500}\}", " ", texto)
    texto = re.sub(r"<\s*(br|/p|/div|/li|/tr|/h\d)\s*/?>", "\n", texto, flags=re.IGNORECASE)
    texto = re.sub(r"<[^>]+>", "", texto)
    texto = unescape(texto)
    texto = re.sub(r"/\*.*?\*/", "", texto, flags=re.DOTALL)
    return texto


def _normalizar(texto: str) -> str:
    if not texto:
        return ""
    texto = re.sub(r"\r\n?", "\n", texto)
    texto = re.sub(r"\n{3,}", "\n\n", texto)
    texto = re.sub(r"[ \t]+", " ", texto)
    return "\n".join(l.rstrip() for l in texto.split("\n")).strip()


def extrair_corpo(payload: dict) -> str:
    plain, html = "", ""

    def walk(parts):
        nonlocal plain, html
        for p in parts:
            mime = p.get("mimeType", "")
            data = p.get("body", {}).get("data", "")
            if mime == "text/plain" and data and not plain:
                plain = _decode_b64(data)
            elif mime == "text/html" and data and not html:
                html = _decode_b64(data)
            if "parts" in p:
                walk(p["parts"])

    if "parts" in payload:
        walk(payload["parts"])
    else:
        mime = payload.get("mimeType", "")
        data = payload.get("body", {}).get("data", "")
        if mime == "text/plain" and data:
            plain = _decode_b64(data)
        elif mime == "text/html" and data:
            html = _decode_b64(data)

    return _normalizar(_limpar_html(plain or html))


def _header(headers: list, name: str, default: str = "") -> str:
    nl = name.lower()
    return next((h["value"] for h in headers if h["name"].lower() == nl), default)


def _detectar_fonte(remetente: str) -> str:
    r = remetente.lower()
    for fonte, padroes in FONTES_VAGAS.items():
        if any(p in r for p in padroes):
            return fonte
    return "outro"


def _extrair_tecnologias(texto: str) -> list:
    t = texto.lower()
    encontradas = []
    for tech in TECNOLOGIAS:
        pattern = r"(?<![a-z0-9])" + re.escape(tech) + r"(?![a-z0-9])"
        if re.search(pattern, t):
            encontradas.append(tech)
    return list(dict.fromkeys(encontradas))


def _extrair_link_candidatura(corpo: str, remetente: str = "") -> str | None:
    """
    Extrai o link mais relevante da vaga no corpo do e-mail.
    Prioridade: link direto da vaga > link de candidatura > primeiro link do corpo.
    Descarta links de rastreamento (tracking pixels, unsubscribe, etc.).
    """
    if not corpo:
        return None

    urls = re.findall(r"https?://[^\s\"'<>\)\]]+", corpo)

    # Domínios que indicam link direto de vaga ou candidatura
    dominios_prioritarios = [
        "jobs", "vagas", "apply", "candidat", "emprego", "oportunidade",
        "gupy", "jobbol", "indeed", "catho", "linkedin", "infojobs",
        "pandape", "micro1", "trampos", "remotar", "programathor",
        "glassdoor", "greenhouse", "lever", "workable", "recrut",
    ]
    # Links que devem ser descartados
    dominios_lixo = [
        "unsubscribe", "optout", "opt-out", "pixel", "track",
        "email-marketing", "mandrill", "mailchimp", "sendgrid",
        "awstrack", "list-manage", "click.notification",
    ]

    candidatos = []
    for url in urls:
        ul = url.lower()
        if any(lixo in ul for lixo in dominios_lixo):
            continue
        if any(p in ul for p in dominios_prioritarios):
            candidatos.insert(0, url)   # prioridade alta — vai para o início
        else:
            candidatos.append(url)

    return candidatos[0] if candidatos else None


# [FIX-3] Classificação de e-mail como vaga ou não-vaga
def _classificar_email(assunto: str, corpo: str, fonte: str) -> str:
    """
    Retorna 'vaga', 'nao_vaga' ou 'ignorar'.
    
    - Fontes conhecidas (indeed, catho…): sempre 'vaga'
    - Fonte 'outro': verifica palavras de exclusão e inclusão
    """
    if fonte != "outro":
        return "vaga"

    texto = (assunto + " " + corpo[:600]).lower()

    # Palavras de exclusão têm prioridade
    for p in PALAVRAS_NAO_VAGA:
        if p in texto:
            return "nao_vaga"

    # Verifica se tem palavras de vaga
    for p in PALAVRAS_VAGA:
        if p in texto:
            return "vaga"

    return "ignorar"


# ===========================================================================
# 4. SUPABASE
# ===========================================================================

def criar_supabase() -> Client | None:
    if not SUPABASE_URL or not SUPABASE_KEY:
        Log.warn("SUPABASE_URL ou SUPABASE_KEY não encontrados no .env — pulando persistência.")
        return None
    try:
        client = create_client(SUPABASE_URL, SUPABASE_KEY)
        Log.ok("Supabase conectado.")
        return client
    except Exception as e:
        Log.error(f"Falha ao conectar no Supabase: {e}")
        return None


def _sb_upsert_insert(sb: Client, tabela: str, registro: dict, conflict_col: str) -> bool:
    """Helper: tenta upsert, cai para insert se constraint não existir."""
    try:
        sb.table(tabela).upsert(registro, on_conflict=conflict_col).execute()
        return True
    except Exception:
        pass
    try:
        sb.table(tabela).insert(registro).execute()
        return True
    except Exception as e:
        Log.warn(f"  Supabase '{tabela}' falhou: {e}")
        return False


def salvar_vaga(sb: Client | None, vaga: dict, dry_run: bool = False) -> bool:
    if dry_run:
        Log.info(f"  [DRY-RUN] vagas_extraidas: {vaga.get('cargo')} @ {vaga.get('empresa')}")
        return True
    if not sb:
        return False
    # Mapeia para o schema real do Supabase (imagem do schema)
    registro = {
        "gmail_message_id":  vaga.get("email_id"),
        "cargo":             vaga.get("cargo"),
        "fonte":             vaga.get("fonte"),
        "tecnologias":       vaga.get("tecnologias"),        # jsonb (já é string JSON)
        "links_candidatura": json.dumps(                     # jsonb — array de links
            [vaga["link_candidatura"]] if vaga.get("link_candidatura") else [],
            ensure_ascii=False,
        ),
        "status_aplicacao":  vaga.get("status", "nova"),
    }
    return _sb_upsert_insert(sb, "vagas_extraidas", registro, "gmail_message_id")


def salvar_certificado(sb: Client | None, cert: dict, dry_run: bool = False) -> bool:
    if dry_run:
        Log.info(f"  [DRY-RUN] certificados_portfolio: {cert.get('nome_curso') or cert.get('assunto')}")
        return True
    if not sb:
        return False
    # Schema: certificados_portfolio
    registro = {
        "gmail_message_id": cert.get("email_id"),
        "plataforma":       cert.get("plataforma"),
        "nome_curso":       cert.get("nome_curso") or cert.get("assunto"),
        "caminho_local":    cert.get("drive_url"),
        "status_extracao":  cert.get("status"),
    }
    return _sb_upsert_insert(sb, "certificados_portfolio", registro, "gmail_message_id")


def salvar_alerta(sb: Client | None, alerta: dict, dry_run: bool = False) -> bool:
    if dry_run:
        Log.info(f"  [DRY-RUN] log_acessos_otp: {alerta.get('plataforma')}")
        return True
    if not sb:
        return False
    # Schema: log_acessos_otp
    registro = {
        "plataforma":      alerta.get("plataforma"),
        "tipo_alerta":     alerta.get("tipo_solicitacao"),
        "codigo_extraido": alerta.get("codigo_ou_link"),
    }
    try:
        sb.table("log_acessos_otp").insert(registro).execute()
        return True
    except Exception as e:
        Log.warn(f"  Supabase log_acessos_otp falhou: {e}")
        return False


def salvar_email_geral(sb: Client | None, registro_raw: dict, dry_run: bool = False) -> bool:
    if dry_run:
        cat = registro_raw.get("categoria", "?")
        Log.info(f"  [DRY-RUN] {cat} — {registro_raw.get('assunto','')[:40]}")
        return True
    if not sb:
        return False

    categoria = registro_raw.get("categoria", "servicos")

    # ── Financeiro → despesas_domesticas ────────────────────────────────────
    if categoria == "financeiro":
        # Só inclui campos que têm valor — evita NOT NULL violation
        # O schema tem: gmail_message_id, estabelecimento, categoria,
        #               valor (nullable), data_despesa (nullable), origem_recurso
        registro: dict = {
            "gmail_message_id": registro_raw.get("email_id"),
            "estabelecimento":  (
                registro_raw.get("remetente_nome")
                or registro_raw.get("remetente", "").split("<")[0].strip()
                or "Desconhecido"
            ),
            "categoria":        registro_raw.get("subcategoria", "boleto_fatura"),
            "origem_recurso":   "email",
        }
        # Só adiciona valor se não for None/vazio
        valor_raw = registro_raw.get("valor")
        if valor_raw:
            registro["valor"] = valor_raw

        # Só adiciona data_despesa se não for None/vazio
        data_raw = registro_raw.get("data_evento") or registro_raw.get("data_despesa")
        if data_raw:
            # Tenta parsear para formato DATE (YYYY-MM-DD) esperado pelo Postgres
            data_limpa = _parsear_data(data_raw)
            if data_limpa:
                registro["data_despesa"] = data_limpa

        return _sb_upsert_insert(sb, "despesas_domesticas", registro, "gmail_message_id")

    # ── Comunicado → sem tabela dedicada ainda, label suficiente ────────────
    # (agendamento, comunicado, servicos)
    return True


def _parsear_data(texto: str) -> str | None:
    """
    Tenta extrair uma data em formato YYYY-MM-DD de strings como:
    '29/06', '29/06/2026', '29 de junho', 'junho 29', '2026-06-29'
    Retorna None se não conseguir parsear.
    """
    if not texto:
        return None
    # Já está no formato correto
    if re.match(r"^\d{4}-\d{2}-\d{2}$", texto.strip()):
        return texto.strip()
    # DD/MM/AAAA ou DD/MM
    m = re.search(r"(\d{1,2})/(\d{1,2})(?:/(\d{4}))?", texto)
    if m:
        d, mo, a = m.group(1), m.group(2), m.group(3) or str(datetime.date.today().year)
        try:
            dt = datetime.date(int(a), int(mo), int(d))
            return dt.isoformat()
        except ValueError:
            pass
    return None


# ===========================================================================
# 5. GOOGLE DRIVE — UPLOAD DE CERTIFICADOS
# ===========================================================================

def upload_drive(
    drive_service,
    conteudo: bytes,
    nome_arquivo: str,
    mime_type: str = "application/pdf",
    folder_id: str | None = None,
) -> str | None:
    """
    [FIX-8] Faz upload de um arquivo para o Google Drive.

    Args:
        drive_service: serviço Drive autenticado.
        conteudo:      bytes do arquivo.
        nome_arquivo:  nome que o arquivo terá no Drive.
        mime_type:     tipo MIME do arquivo.
        folder_id:     ID da pasta de destino no Drive (opcional).

    Returns:
        URL de visualização do arquivo no Drive, ou None em caso de erro.
    """
    metadata = {"name": nome_arquivo}
    if folder_id:
        metadata["parents"] = [folder_id]

    try:
        media = MediaIoBaseUpload(
            io.BytesIO(conteudo),
            mimetype=mime_type,
            resumable=False,
        )
        arquivo = drive_service.files().create(
            body=metadata,
            media_body=media,
            fields="id,webViewLink",
        ).execute()

        file_id = arquivo.get("id")
        url = arquivo.get("webViewLink", f"https://drive.google.com/file/d/{file_id}/view")
        Log.ok(f"  Drive upload OK: {nome_arquivo} → {url[:60]}...")
        return url

    except HttpError as e:
        Log.warn(f"  Drive upload falhou para '{nome_arquivo}': {e}")
        return None


def _nome_seguro(texto: str, ext: str = "") -> str:
    """Gera nome de arquivo seguro a partir de uma string."""
    nome = re.sub(r"[^\w\s\-]", "", texto, flags=re.UNICODE)
    nome = re.sub(r"\s+", "_", nome.strip())[:60]
    return f"{nome}{ext}" if not nome.endswith(ext) else nome


# ===========================================================================
# 6. GEMINI — importado do módulo dedicado
# ===========================================================================
# Toda a lógica de IA está em gemini_service.py.
# Aqui apenas importamos e criamos wrappers de compatibilidade.

try:
    from gemini_service import (
        classificar_email      as _gs_classificar,
        extrair_vagas          as _gs_extrair_vagas,
        analisar_acesso        as _gs_analisar_acesso,
        classificar_email_geral as _gs_classificar_geral,
    )
    _GEMINI_SERVICE_OK = True
    Log.ok("gemini_service.py carregado.")
except ImportError as _e:
    Log.warn(f"gemini_service.py não encontrado ({_e}) — usando fallback interno.")
    _GEMINI_SERVICE_OK = False


# ── Fallback: mantém client interno se gemini_service não estiver disponível ──

_gemini_client = None

def _get_gemini():
    global _gemini_client
    if _gemini_client is not None:
        return _gemini_client
    if not GEMINI_API_KEY:
        raise ValueError("GEMINI_API_KEY não encontrada no .env")
    if GENAI_NEW:
        _gemini_client = google_genai.Client(api_key=GEMINI_API_KEY)
    else:
        genai_legacy.configure(api_key=GEMINI_API_KEY)
        _gemini_client = genai_legacy.GenerativeModel("gemini-2.5-flash")
    return _gemini_client


def _extrair_retry_delay(exc: Exception) -> float:
    txt = str(exc)
    m = re.search(r"'retryDelay':\s*'([0-9.]+)(ms|s)'", txt)
    if m:
        v, u = float(m.group(1)), m.group(2)
        return (v / 1000.0) if u == "ms" else v
    m2 = re.search(r"retry in ([0-9.]+)s", txt)
    if m2:
        return float(m2.group(1))
    return 60.0


def _gerar_gemini(prompt: str, max_tentativas: int = 3) -> str:
    client = _get_gemini()
    ultima_exc = None
    for tentativa in range(1, max_tentativas + 1):
        try:
            if GENAI_NEW:
                resp = client.models.generate_content(
                    model="gemini-2.5-flash",
                    contents=prompt,
                    config=genai_types.GenerateContentConfig(
                        response_mime_type="application/json",
                        temperature=0.1,
                    ),
                )
            else:
                resp = client.generate_content(
                    prompt,
                    generation_config=genai_legacy.GenerationConfig(
                        response_mime_type="application/json",
                        temperature=0.1,
                    ),
                )
            return resp.text
        except Exception as exc:
            ultima_exc = exc
            codigo = str(exc)
            if "429" not in codigo and "503" not in codigo:
                raise
            if tentativa == max_tentativas:
                break
            espera = _extrair_retry_delay(exc)
            Log.warn(f"  Gemini {429 if '429' in codigo else 503} — aguardando {espera:.1f}s...")
            time.sleep(espera + 1)
    raise ultima_exc


def _parse_gemini_json(raw: str, esperado_dict: bool = True) -> dict:
    if not raw:
        return {}
    raw = re.sub(r"```(?:json)?", "", raw).strip(" `\n")
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        Log.warn(f"  Gemini JSON inválido: {raw[:80]}...")
        return {}
    if isinstance(parsed, dict):
        return parsed
    if isinstance(parsed, list):
        for item in parsed:
            if isinstance(item, dict):
                return item
        return {}
    return {}


# ── Funções públicas usadas nos módulos ─────────────────────────────────────

def classificar_email_pipeline(assunto: str, remetente: str, corpo_preview: str,
                               msg_id: str = "") -> str:
    """
    Etapa 1: classifica o e-mail para roteamento.

    Ordem de decisão (mais barata → mais cara):
      1. Cache local   — grátis, instantâneo
      2. Heurística    — grátis, 0 tokens
      3. Gemini        — só se houver budget disponível

    Retorna: "vaga" | "certificado" | "alerta_acesso" | "agendamento" |
             "financeiro" | "servico" | "outro"
    """
    chave = _cache_key("classif", msg_id) if msg_id else ""

    # 1. Cache
    if chave and chave in _GEMINI_CACHE:
        resultado = _GEMINI_CACHE[chave]
        Log.info(f"  💾 Cache hit (classif): {resultado}")
        return resultado

    # 2. Heurística — detecta fonte conhecida de vagas
    fonte = _detectar_fonte(remetente)
    if fonte != "outro":
        if chave:
            _GEMINI_CACHE[chave] = "vaga"
            _salvar_cache(_GEMINI_CACHE)
        return "vaga"

    txt = (assunto + " " + corpo_preview[:400]).lower()

    for p in PALAVRAS_NAO_VAGA:
        if p in txt:
            # Ainda pode ser agendamento ou financeiro — heurística rápida
            if any(w in txt for w in ["agendamento","confirmado","reserva","horário","consulta"]):
                return "agendamento"
            if any(w in txt for w in ["fatura","boleto","cobrança","pagamento","nota fiscal","pix"]):
                return "financeiro"
            return "servico"

    for p in PALAVRAS_VAGA:
        if p in txt:
            if chave:
                _GEMINI_CACHE[chave] = "vaga"
                _salvar_cache(_GEMINI_CACHE)
            return "vaga"

    # Agendamento heurístico
    if any(w in txt for w in ["agendamento","confirmado","reserva","horário","consulta","appointment"]):
        return "agendamento"

    # Financeiro heurístico
    if any(w in txt for w in ["fatura","boleto","cobrança","pagamento","nota fiscal","pix","débito","crédito"]):
        return "financeiro"

    # 3. Gemini — só se tiver budget e gemini_service disponível
    if _GEMINI_SERVICE_OK and _tem_budget("geral"):
        try:
            resultado = _gs_classificar(assunto, remetente, corpo_preview)
            _consumir_budget("geral")
            if chave:
                _GEMINI_CACHE[chave] = resultado
                _salvar_cache(_GEMINI_CACHE)
            return resultado
        except Exception as e:
            Log.warn(f"  Gemini classif falhou, usando heurística: {e}")

    return "outro"


def extrair_vagas_pipeline(assunto: str, remetente: str, corpo: str,
                           msg_id: str = "") -> list[dict]:
    """
    Etapa 2: extrai N vagas de um e-mail.

    Ordem de decisão:
      1. Cache local   — reutiliza resultado anterior
      2. Gemini        — só se houver budget de vagas
      3. Fallback      — heurística simples, retorna 1 item sem score

    Cada item da lista tem: cargo, empresa, local, modelo_trabalho,
    salario, link_candidatura, requisitos, tecnologias, score_match,
    tipo_email, status_candidatura
    """
    chave = _cache_key("vagas", msg_id) if msg_id else ""

    # 1. Cache
    if chave and chave in _GEMINI_CACHE:
        cached = _GEMINI_CACHE[chave]
        if isinstance(cached, list):
            Log.info(f"  💾 Cache hit (vagas): {len(cached)} vaga(s)")
            return cached

    # 2. Gemini com budget
    if _tem_budget("vagas"):
        if _GEMINI_SERVICE_OK:
            try:
                resultado = _gs_extrair_vagas(assunto, remetente, corpo)
                _consumir_budget("vagas")
                if chave and resultado:
                    _GEMINI_CACHE[chave] = resultado
                    _salvar_cache(_GEMINI_CACHE)
                return resultado
            except Exception as e:
                Log.warn(f"  Gemini vagas falhou, tentando fallback: {e}")

        # Fallback interno (sem gemini_service)
        try:
            dados = analisar_vaga_gemini_fallback(corpo or assunto)
            _consumir_budget("vagas")
            if not dados:
                return []
            resultado = [{
                "cargo":              dados.get("cargo")         or assunto,
                "empresa":            dados.get("nome_empresa")  or "Não identificada",
                "local":              dados.get("local"),
                "modelo_trabalho":    dados.get("modelo_trabalho", "Não informado"),
                "salario":            None,
                "requisitos":         dados.get("requisitos_obrigatorios", []),
                "tecnologias":        dados.get("tecnologias", []),
                "score_match":        int(dados.get("score_match") or 0),
                "link_candidatura":   None,
                "tipo_email":         "nova_vaga",
                "status_candidatura": "nova",
            }]
            if chave:
                _GEMINI_CACHE[chave] = resultado
                _salvar_cache(_GEMINI_CACHE)
            return resultado
        except Exception as e:
            Log.warn(f"  Fallback Gemini vagas falhou: {e}")

    else:
        # Budget esgotado — heurística local pura (0 tokens)
        Log.warn(f"  ⚠ Budget Gemini/vagas esgotado ({_GEMINI_BUDGET.get('vagas',0)}/{_BUDGET_LIMITE['vagas']}) — usando heurística")
        techs = _extrair_tecnologias(corpo)
        link  = _extrair_link_candidatura(corpo, remetente)
        return [{
            "cargo":              assunto,
            "empresa":            "Não identificada (sem Gemini)",
            "local":              None,
            "modelo_trabalho":    "Não informado",
            "salario":            None,
            "requisitos":         [],
            "tecnologias":        techs,
            "score_match":        0,
            "link_candidatura":   link,
            "tipo_email":         "nova_vaga",
            "status_candidatura": "nova",
        }]

    return []


def analisar_acesso_pipeline(remetente: str, assunto: str, corpo: str,
                             msg_id: str = "") -> dict:
    """Valida OTP / alertas de login. Usa cache + budget."""
    chave = _cache_key("alerta", msg_id) if msg_id else ""

    # Cache
    if chave and chave in _GEMINI_CACHE:
        cached = _GEMINI_CACHE[chave]
        if isinstance(cached, dict):
            Log.info(f"  💾 Cache hit (alerta)")
            return cached

    if not _tem_budget("alertas"):
        Log.warn(f"  ⚠ Budget Gemini/alertas esgotado — alerta ignorado")
        return {}

    if _GEMINI_SERVICE_OK:
        try:
            resultado = _gs_analisar_acesso(remetente, assunto, corpo)
            _consumir_budget("alertas")
            if chave and resultado:
                _GEMINI_CACHE[chave] = resultado
                _salvar_cache(_GEMINI_CACHE)
            return resultado
        except Exception as e:
            Log.warn(f"  Gemini acesso falhou: {e}")

    try:
        resultado = analisar_acesso_gemini(remetente, assunto, corpo)
        _consumir_budget("alertas")
        if chave and resultado:
            _GEMINI_CACHE[chave] = resultado
            _salvar_cache(_GEMINI_CACHE)
        return resultado
    except Exception as e:
        Log.warn(f"  Gemini acesso fallback falhou: {e}")
        return {}


def classificar_geral_pipeline(remetente: str, assunto: str, corpo: str,
                               msg_id: str = "") -> dict:
    """Extrai info de agendamentos e financeiros. Usa cache + budget geral."""
    chave = _cache_key("geral", msg_id) if msg_id else ""

    if chave and chave in _GEMINI_CACHE:
        cached = _GEMINI_CACHE[chave]
        if isinstance(cached, dict):
            return cached

    if not _tem_budget("geral"):
        return {}

    if _GEMINI_SERVICE_OK:
        try:
            resultado = _gs_classificar_geral(remetente, assunto, corpo)
            _consumir_budget("geral")
            if chave and resultado:
                _GEMINI_CACHE[chave] = resultado
                _salvar_cache(_GEMINI_CACHE)
            return resultado
        except Exception as e:
            Log.warn(f"  Gemini geral falhou: {e}")
    return {}


# ── Funções de fallback (mantidas para compatibilidade) ──────────────────────

def analisar_vaga_gemini_fallback(texto: str) -> dict:
    prompt = f"""
Você é um assistente de RH especializado em tecnologia.
Analise o texto abaixo e responda APENAS em JSON estrito (sem crases):
{{
  "eh_vaga_real": true/false,
  "motivo_classificacao": "vaga de emprego | curso | newsletter | bootcamp | outro",
  "cargo": "string ou null",
  "nome_empresa": "string ou null",
  "local": "string ou null",
  "modelo_trabalho": "Remoto | Híbrido | Presencial | Não informado",
  "requisitos_obrigatorios": ["lista"],
  "requisitos_desejaveis": ["lista"],
  "tecnologias": ["lista"],
  "vaga_python_dados": true/false,
  "score_match": <0-100>
}}
Texto:
{texto[:3000]}
"""
    try:
        raw = _gerar_gemini(prompt)
        return _parse_gemini_json(raw)
    except Exception as e:
        Log.warn(f"  Gemini fallback falhou: {e}")
        return {}


def analisar_acesso_gemini(remetente: str, assunto: str, corpo: str) -> dict:
    prompt = f"""
Você é um assistente de segurança digital.
Analise o e-mail e responda APENAS em JSON estrito (sem crases):
{{
  "eh_email_de_acesso": true/false,
  "plataforma": "nome da empresa",
  "tipo_solicitacao": "otp | recuperacao_senha | alerta_novo_login | irrelevante",
  "codigo_ou_link": "código ou URL ou null",
  "justificativa_curta": "1 frase"
}}
Remetente: {remetente}
Assunto: {assunto}
Corpo: {corpo[:800]}
"""
    try:
        raw = _gerar_gemini(prompt)
        return _parse_gemini_json(raw)
    except Exception as e:
        Log.warn(f"  Gemini acesso falhou: {e}")
        return {}


# ===========================================================================
# 7. MÓDULO DE VAGAS
# ===========================================================================

def rodar_vagas(service, labels: dict, sb: Client | None, query: str, dry_run: bool):
    Log.section("Módulo 1 — Extração de Vagas")

    try:
        listagem = service.users().messages().list(
            userId="me", maxResults=50, q=query
        ).execute()
        msgs = listagem.get("messages", [])
    except HttpError as e:
        Log.error(f"Erro ao listar e-mails de vagas: {e}")
        return

    Log.info(f"E-mails encontrados com a query '{query}': {len(msgs)}")

    total_emails  = 0
    total_vagas   = 0
    total_salvas  = 0
    total_outros  = 0

    for msg in msgs:
        try:
            detalhe = service.users().messages().get(
                userId="me", id=msg["id"], format="full"
            ).execute()
        except HttpError as e:
            Log.warn(f"  Erro ao buscar msg {msg['id']}: {e}")
            continue

        headers   = detalhe["payload"]["headers"]
        assunto   = _header(headers, "Subject", "Sem Assunto")
        remetente = _header(headers, "From",    "Desconhecido")
        data      = _header(headers, "Date",    "")
        corpo     = extrair_corpo(detalhe["payload"])

        # ── Etapa 1: classificar o e-mail (barato, usa heurística primeiro) ──
        categoria = classificar_email_pipeline(assunto, remetente, corpo[:400], msg["id"])

        if categoria != "vaga":
            Log.info(f"  → [{categoria}] será tratado pelo módulo adequado: {assunto[:50]}")
            total_outros += 1
            continue

        # ── Etapa 2: extrair vagas (suporta múltiplas por e-mail) ─────────────
        Log.info(f"  Extraindo vagas de: {assunto[:60]}")
        vagas_extraidas = extrair_vagas_pipeline(assunto, remetente, corpo, msg["id"])

        if not vagas_extraidas:
            Log.warn(f"  Nenhuma vaga extraída de: {assunto[:50]}")
            continue

        Log.info(f"  {len(vagas_extraidas)} vaga(s) encontrada(s) neste e-mail")

        score_max = 0
        for vaga_raw in vagas_extraidas:
            # Enriquece com tecnologias detectadas heuristicamente
            techs_h  = _extrair_tecnologias(corpo)
            techs_g  = [t.lower() for t in vaga_raw.get("tecnologias", [])
                        if isinstance(t, str)]
            techs    = list(dict.fromkeys(techs_h + techs_g))
            score    = vaga_raw.get("score_match", 0) or 0
            score_max = max(score_max, score)

            # Usa o link extraído pelo Gemini; fallback para extração heurística
            link = vaga_raw.get("link_candidatura") or _extrair_link_candidatura(corpo, remetente)

            vaga = {
                "email_id":          msg["id"],
                "assunto":           assunto,
                "remetente":         remetente,
                "fonte":             _detectar_fonte(remetente),
                "cargo":             vaga_raw.get("cargo")    or assunto,
                "empresa":           vaga_raw.get("empresa")  or "Não identificada",
                "local":             vaga_raw.get("local"),
                "modelo_trabalho":   vaga_raw.get("modelo_trabalho", "Não informado"),
                "salario":           vaga_raw.get("salario"),
                "requisitos":        json.dumps(vaga_raw.get("requisitos", []),
                                               ensure_ascii=False),
                "tecnologias":       json.dumps(techs, ensure_ascii=False),
                "score_gemini":      score,
                "tipo_email":        vaga_raw.get("tipo_email",         "nova_vaga"),
                "status_candidatura":vaga_raw.get("status_candidatura", "nova"),
                "link_candidatura":  link,
                "descricao":         corpo[:400] if corpo else "",
                "data_email":        data,
                "status":            "nova",
            }

            salvo = salvar_vaga(sb, vaga, dry_run)
            if salvo:
                total_salvas += 1

            total_vagas += 1
            flag = "🔥" if score >= 75 else ("⚡" if score >= 50 else "·")
            tipo = vaga_raw.get("tipo_email", "nova_vaga")
            lk   = f" → {link[:50]}" if link else ""
            Log.ok(f"    {flag} [{tipo}] {vaga['cargo']} @ {vaga['empresa']} — score {score}%{lk}")

            time.sleep(0.5)  # throttle entre vagas do mesmo e-mail

        # ── Label do e-mail baseada no score máximo das vagas extraídas ───────
        _aplicar_label(
            service, msg["id"],
            labels.get("vagas_pai"),
            labels.get("vagas_alta") if score_max >= 75 else labels.get("vagas_baixa"),
        )

        total_emails += 1
        time.sleep(0.5)

    Log.section(
        f"Vagas: {total_emails} e-mails | {total_vagas} vagas | "
        f"{total_salvas} salvas | {total_outros} outros módulos"
    )


# ===========================================================================
# 8. MÓDULO DE CERTIFICADOS (com upload no Google Drive)
# ===========================================================================

def _seguir_redirect(url: str, timeout: int = 10) -> str:
    """
    Segue redirects de tracking links (AWS SES, SendGrid) e retorna a URL final.
    Não baixa o corpo — apenas HEAD para descobrir o destino.
    """
    try:
        resp = requests.head(
            url,
            headers={"User-Agent": "Mozilla/5.0"},
            allow_redirects=True,
            timeout=timeout,
        )
        return resp.url
    except Exception:
        try:
            # Fallback: GET com stream para não baixar o corpo
            resp = requests.get(
                url,
                headers={"User-Agent": "Mozilla/5.0"},
                allow_redirects=True,
                stream=True,
                timeout=timeout,
            )
            resp.close()
            return resp.url
        except Exception:
            return url


def _url_imagem_dio(url_cert: str) -> str | None:
    """
    Converte uma URL de página de certificado DIO para a URL direta da imagem JPG.

    A DIO usa duas convenções:
      1. https://www.dio.me/certificate/W54MDN2C
         → https://hermes.dio.me/certificates/cover/W54MDN2C.jpg

      2. https://hermes.dio.me/certificates/cover/W54MDN2C.jpg  (já é a imagem)

    Retorna a URL da imagem ou None se não for URL de certificado DIO.
    """
    # Já é a imagem hermes
    if "hermes.dio.me/certificates" in url_cert.lower():
        return url_cert if url_cert.endswith(".jpg") else url_cert + ".jpg"

    # Página de certificado público
    m = re.search(r"dio\.me/+certificate/([A-Z0-9]+)", url_cert, re.IGNORECASE)
    if m:
        codigo = m.group(1).upper()
        return f"https://hermes.dio.me/certificates/cover/{codigo}.jpg"

    return None


def _extrair_links_certificado(html_body: str) -> list[dict]:
    """
    Percorre o HTML bruto do e-mail e retorna lista de dicts:
        {"url": ..., "plataforma": ..., "tipo": "imagem"|"pdf"|"link_web"}

    Fluxo DIO:
      1. E-mail tem href para tracking link da AWS SES
      2. Seguimos o redirect → chega em dio.me/certificate/CODIGO
      3. Derivamos a URL da imagem: hermes.dio.me/certificates/cover/CODIGO.jpg
    """
    hrefs = re.findall(r'href="(https?://[^"]+)"', html_body)
    resultados = []
    vistos = set()

    for href in hrefs:
        href_dec = urllib.parse.unquote(href)

        # ── Regra 1: Link DIO já presente de forma limpa ──────────────────
        img_url = _url_imagem_dio(href_dec)
        if img_url and img_url not in vistos:
            vistos.add(img_url)
            resultados.append({"url": img_url, "plataforma": "DIO", "tipo": "imagem"})
            continue

        # ── Regra 2: Tracking link que pode esconder DIO ou outras plataformas
        if "awstrack.me" in href or "sendgrid" in href or "click." in href:
            url_final = _seguir_redirect(href)
            url_dec2  = urllib.parse.unquote(url_final)

            img_url = _url_imagem_dio(url_dec2)
            if img_url and img_url not in vistos:
                vistos.add(img_url)
                resultados.append({"url": img_url, "plataforma": "DIO", "tipo": "imagem"})
                continue

            # Outros certificados via tracking link
            chaves = ["cert", "credential", "badge", "diploma"]
            if any(c in url_dec2.lower() for c in chaves) and url_final not in vistos:
                vistos.add(url_final)
                resultados.append({"url": url_final, "plataforma": "outro", "tipo": "link_web"})
            continue

        # ── Regra 3: Links diretos de outras plataformas ──────────────────
        chaves = ["cert", "download", "visualizar", "credential", "udemy",
                  "coursera", "alura", "rocketseat", "badge", "diploma"]
        if any(c in href.lower() for c in chaves) and href not in vistos:
            vistos.add(href)
            plat = "udemy" if "udemy" in href.lower() else "outro"
            resultados.append({"url": href, "plataforma": plat, "tipo": "link_web"})

    return resultados


def _baixar_conteudo(url: str) -> tuple[bytes | None, str]:
    """
    Tenta baixar o conteúdo de uma URL.
    Retorna (conteudo_bytes, mime_type) ou (None, '').
    """
    try:
        resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"},
                            allow_redirects=True, timeout=15)
        ct = resp.headers.get("Content-Type", "").lower().split(";")[0].strip()
        if resp.status_code == 200 and len(resp.content) > 500:
            return resp.content, ct
    except Exception as e:
        Log.warn(f"  Falha ao baixar URL: {e}")
    return None, ""


def rodar_certificados(service, drive_service, labels: dict, sb: Client | None,
                       query_dias: int, dry_run: bool):
    Log.section("Módulo 2 — Extração de Certificados")

    query = (
        f"subject:certificado newer_than:{query_dias}d "
        f"-label:AutoApply/Certificados/Extraido "
        f"-label:AutoApply/Certificados/Pendente"
    )
    Log.info(f"Query: '{query}'")

    try:
        res = service.users().messages().list(userId="me", q=query, maxResults=20).execute()
        msgs = res.get("messages", [])
    except HttpError as e:
        Log.error(f"Erro ao listar e-mails de certificados: {e}")
        return

    Log.info(f"E-mails de certificados encontrados: {len(msgs)}")

    for msg in msgs:
        try:
            detalhe = service.users().messages().get(
                userId="me", id=msg["id"], format="full"
            ).execute()
        except HttpError as e:
            Log.warn(f"  Erro ao buscar msg {msg['id']}: {e}")
            continue

        headers  = detalhe["payload"]["headers"]
        assunto  = _header(headers, "Subject", "Sem Assunto")
        Log.info(f"  Processando: {assunto[:60]}")

        partes            = detalhe["payload"].get("parts", [])
        sucesso           = False
        drive_url         = None
        caminho_resultado = ""
        plataforma_cert   = "desconhecido"   # será sobrescrito na extração

        # --- Parte A: anexos diretos ---
        for parte in partes:
            if not parte.get("filename"):
                continue
            dados_b64 = parte["body"].get("data", "")
            if not dados_b64 and parte["body"].get("attachmentId"):
                try:
                    anexo = service.users().messages().attachments().get(
                        userId="me", messageId=msg["id"],
                        id=parte["body"]["attachmentId"]
                    ).execute()
                    dados_b64 = anexo.get("data", "")
                except HttpError:
                    continue

            if not dados_b64:
                continue

            conteudo = base64.urlsafe_b64decode(dados_b64.encode("UTF-8"))
            nome_arq = parte["filename"]
            mime     = parte.get("mimeType", "application/octet-stream")

            # [FIX-8] Upload para o Drive
            if not dry_run:
                drive_url = upload_drive(
                    drive_service, conteudo, nome_arq, mime, DRIVE_FOLDER_ID
                )
            else:
                Log.info(f"  [DRY-RUN] Não faria upload: {nome_arq}")
                drive_url = "dry-run"

            if drive_url:
                sucesso = True
                caminho_resultado = drive_url
                break

        # --- Parte B: links de rastreamento / portais ---
        if not sucesso:
            html_body = ""
            for parte in partes:
                if parte.get("mimeType") == "text/html":
                    html_body = _decode_b64(parte["body"].get("data", ""))

            links = _extrair_links_certificado(html_body)  # lista de dicts
            for item in links:
                url      = item["url"]
                plat     = item["plataforma"]
                tipo_url = item["tipo"]

                Log.info(f"  [{plat}] Tentando: {url[:70]}...")

                if tipo_url == "imagem":
                    # DIO: imagem JPG direta em hermes.dio.me
                    conteudo, ct = _baixar_conteudo(url)
                    if conteudo and ("image/" in ct or url.endswith(".jpg")):
                        ext      = ".jpg" if url.endswith(".jpg") else "." + ct.split("/")[-1]
                        nome_arq = _nome_seguro(assunto, ext)
                        if not dry_run:
                            drive_url = upload_drive(
                                drive_service, conteudo, nome_arq,
                                ct or "image/jpeg", DRIVE_FOLDER_ID
                            )
                        else:
                            drive_url = "dry-run"
                            Log.info(f"  [DRY-RUN] Imagem DIO: {nome_arq}")
                        if drive_url:
                            sucesso = True
                            caminho_resultado = drive_url
                            plataforma_cert   = plat
                            break

                elif tipo_url == "pdf":
                    conteudo, ct = _baixar_conteudo(url)
                    if conteudo and "pdf" in ct:
                        nome_arq = _nome_seguro(assunto, ".pdf")
                        if not dry_run:
                            drive_url = upload_drive(
                                drive_service, conteudo, nome_arq,
                                "application/pdf", DRIVE_FOLDER_ID
                            )
                        else:
                            drive_url = "dry-run"
                        if drive_url:
                            sucesso = True
                            caminho_resultado = drive_url
                            plataforma_cert   = plat
                            break

                else:
                    # link_web — salva referência .txt no Drive
                    conteudo_txt = f"Plataforma: {plat}\nCurso: {assunto}\nLink:\n{url}\n".encode()
                    nome_arq     = _nome_seguro(assunto, "_link.txt")
                    if not dry_run:
                        drive_url = upload_drive(
                            drive_service, conteudo_txt, nome_arq, "text/plain", DRIVE_FOLDER_ID
                        )
                    else:
                        drive_url = "dry-run"
                    if drive_url:
                        sucesso = True
                        caminho_resultado = drive_url
                        plataforma_cert   = plat
                        break

        # --- Parte C: labels e Supabase ---
        # ── Labels de certificados: pai + sub-label por resultado ────────────
        _aplicar_label(
            service, msg["id"],
            labels.get("cert_pai"),
            labels.get("cert_extraido") if sucesso else labels.get("cert_pendente"),
        )
        # ────────────────────────────────────────────────────────────────────

        cert_record = {
            "email_id":      msg["id"],
            "assunto":       assunto,
            "plataforma":    plataforma_cert,
            "nome_curso":    assunto,
            "drive_url":     drive_url,
            "tipo":          "pdf" if (caminho_resultado or "").endswith(".pdf") else "imagem" if any((caminho_resultado or "").endswith(e) for e in [".jpg",".jpeg",".png"]) else "link",
            "status":        "extraido" if sucesso else "pendente",
        }
        salvar_certificado(sb, cert_record, dry_run)

        if sucesso:
            Log.ok(f"  ✓ Certificado → Drive: {caminho_resultado[:70]}")
        else:
            Log.warn(f"  ⚠ Nada extraído — label 'certificados_pendentes' aplicada")

    Log.info("Módulo de certificados concluído.")


# ===========================================================================
# 9. MÓDULO DE ALERTAS DE ACESSO (OTP / Recuperação de Senha)
# ===========================================================================

def rodar_alertas(service, labels: dict, sb: Client | None, query_dias: int, dry_run: bool):
    Log.section("Módulo 3 — Alertas de Acesso (OTP / Senhas)")

    query = (
        "subject:(código OR senha OR login OR password OR pin OR recuperação OR ticket OR otp) "
        f"newer_than:{query_dias}d"
    )
    Log.info(f"Query: '{query}'")

    try:
        res = service.users().messages().list(userId="me", q=query, maxResults=10).execute()
        msgs = res.get("messages", [])
    except HttpError as e:
        Log.error(f"Erro ao listar e-mails de acesso: {e}")
        return

    Log.info(f"E-mails de acesso encontrados: {len(msgs)}")
    total_alertas = 0

    for msg in msgs:
        try:
            detalhe = service.users().messages().get(
                userId="me", id=msg["id"], format="full"
            ).execute()
        except HttpError as e:
            Log.warn(f"  Erro ao buscar msg {msg['id']}: {e}")
            continue

        headers   = detalhe["payload"]["headers"]
        assunto   = _header(headers, "Subject", "Sem Assunto")
        remetente = _header(headers, "From",    "Desconhecido")
        corpo     = extrair_corpo(detalhe["payload"])

        analise = analisar_acesso_pipeline(remetente, assunto, corpo[:800], msg["id"])

        if not analise.get("eh_email_de_acesso"):
            continue

        plataforma = analise.get("plataforma", "?")
        tipo       = analise.get("tipo_solicitacao", "?").upper()
        codigo     = analise.get("codigo_ou_link")

        print(f"\n  {'─'*50}")
        print(f"  🔒 {Log.YELLOW}ALERTA DE ACESSO{Log.RESET} — {plataforma}")
        print(f"  Tipo     : {tipo}")
        if codigo:
            print(f"  {Log.GREEN}🔑 CÓDIGO / LINK: ──►  {codigo}  ◄──{Log.RESET}")
        print(f"  Assunto  : {assunto}")
        print(f"  De       : {remetente}")
        print(f"  {'─'*50}")

        # ── Labels de alertas: pai + sub-label por tipo ─────────────────────
        tipo_label = (
            labels.get("alerta_otp")
            if analise.get("tipo_solicitacao") in ("otp", "alerta_novo_login")
            else labels.get("alerta_recuperacao")
        )
        _aplicar_label(service, msg["id"], labels.get("alerta_pai"), tipo_label)
        # ────────────────────────────────────────────────────────────────────

        alerta_record = {
            "email_id":         msg["id"],
            "plataforma":       plataforma,
            "tipo_solicitacao": analise.get("tipo_solicitacao", ""),
            "codigo_ou_link":   codigo,
            "remetente":        remetente,
            "assunto":          assunto,
        }
        salvar_alerta(sb, alerta_record, dry_run)
        total_alertas += 1
        time.sleep(0.5)

    Log.ok(f"Alertas detectados e processados: {total_alertas}")


# ===========================================================================
# 10. MÓDULO DE E-MAILS GERAIS
#     Captura tudo que não é vaga, certificado ou alerta e classifica por tipo.
#     Cada e-mail recebe: pai (AutoApply/Geral) + sub-label por categoria.
#     Salva na tabela 'emails_gerais' do Supabase.
# ===========================================================================

# Palavras-chave para classificar e-mails gerais por categoria
_PADROES_GERAL = {
    "agendamento": [
        "agendamento", "agendado", "confirmado", "reserva", "confirma sua visita",
        "confirmação de agendamento", "booksy", "scheduling", "appointment",
        "seu horário", "sua consulta", "sua reserva",
    ],
    "financeiro": [
        "fatura", "boleto", "cobrança", "pagamento", "comprovante", "recibo",
        "nota fiscal", "nfe", "extrato", "débito", "crédito", "pix",
        "invoice", "receipt", "payment", "charge", "billing",
        "google play", "assinatura renovada", "renovação automática",
        "seu boleto", "vence amanhã", "vencimento", "valor a pagar",
        "emitido por", "emissão", "sua fatura",
    ],
    # Comunicados: avisos operacionais de empresas/serviços que o usuário usa
    "comunicado": [
        "aviso importante", "comunicado", "informamos que", "alteração",
        "mudança de horário", "horário especial", "funcionamento",
        "manutenção", "atualização do contrato", "nova política",
        "termos de uso", "política de privacidade", "aviso de segurança",
        "alerta de segurança", "smart fit", "academia", "smartfit",
        "copa do mundo", "jogo", "seleção brasileira",
        "nubank", "itaú", "bradesco", "santander",   # bancos tb mandam comunicados
    ],
    "servicos": [
        "atualização", "update", "nova versão", "changelog", "bem-vindo",
        "bem vindo", "sua conta", "verificação", "ativação", "ativou",
        "duolingo", "spotify", "youtube", "netflix", "amazon", "ifood",
        "rappi", "uber", "99", "magazine", "mercado livre",
    ],
}

# Subcategorias para financeiro (usadas no campo categoria do Supabase)
_SUBCATEGORIA_FINANCEIRO = {
    "boleto_fatura": ["boleto", "fatura", "vence", "vencimento", "valor a pagar", "emitido por"],
    "pix":           ["pix", "transferência", "ted", "doc"],
    "recibo":        ["comprovante", "recibo", "nfe", "nota fiscal"],
    "assinatura":    ["assinatura", "renovação", "google play", "renovada"],
    "cobranca":      ["cobrança", "debito", "crédito"],
}

def _detectar_subcategoria_financeiro(texto: str) -> str:
    txt = texto.lower()
    for sub, palavras in _SUBCATEGORIA_FINANCEIRO.items():
        if any(p in txt for p in palavras):
            return sub
    return "boleto_fatura"

# Prompt Gemini para classificar e-mail geral
def _classificar_email_geral_gemini(remetente: str, assunto: str, corpo: str) -> dict:
    return _parse_gemini_json(_gerar_gemini(f"""
Você é um assistente pessoal que organiza e-mails.
Analise o e-mail abaixo e responda APENAS em JSON estrito (sem crases):
{{
  "categoria": "agendamento | financeiro | servicos | outro",
  "resumo": "1 frase resumindo o que o e-mail diz",
  "remetente_nome": "nome amigável do remetente (ex: Barbearia Hermanos)",
  "data_evento": "se for agendamento, data/hora do evento como string, ou null",
  "valor": "se for financeiro, valor como string (ex: R$ 49,90), ou null",
  "link_principal": "URL mais importante do e-mail, ou null",
  "requer_acao": true/false
}}
Remetente: {remetente}
Assunto: {assunto}
Corpo: {corpo[:800]}
"""))


def _categoria_heuristica(assunto: str, corpo: str) -> str:
    """Classificação rápida sem consumir token do Gemini."""
    txt = (assunto + " " + corpo[:300]).lower()
    for cat, padroes in _PADROES_GERAL.items():
        if any(p in txt for p in padroes):
            return cat
    return "servicos"   # fallback


def salvar_email_geral(sb: Client | None, registro_raw: dict, dry_run: bool = False) -> bool:
    if dry_run:
        Log.info(f"  [DRY-RUN] {registro_raw.get('categoria')} — {registro_raw.get('assunto','')[:40]}")
        return True
    if not sb:
        return False
    # Financeiros → despesas_domesticas
    if registro_raw.get("categoria") == "financeiro":
        registro = {
            "gmail_message_id": registro_raw.get("email_id"),
            "estabelecimento":  registro_raw.get("remetente_nome"),
            "categoria":        "servico",
            "valor":            None,
            "data_despesa":     None,
            "origem_recurso":   "email",
        }
        return _sb_upsert_insert(sb, "despesas_domesticas", registro, "gmail_message_id")
    # Demais categorias — label aplicada, sem tabela dedicada ainda
    return True


def rodar_emails_gerais(service, labels: dict, sb: Client | None, query: str, dry_run: bool):
    """
    Processa e-mails que não são vagas, certificados ou alertas.
    Usa heurística rápida para economizar tokens; chama Gemini apenas para
    e-mails onde o resumo/categoria não ficou claro pela heurística.
    Labels: AutoApply/Geral + sub-label por categoria.
    """
    Log.section("Módulo 4 — E-mails Gerais")

    # Exclui e-mails já processados pelos outros módulos
    q_final = (
        f"{query} "
        f"-label:AutoApply/Vagas/Alta_Prioridade "
        f"-label:AutoApply/Vagas/Baixa_Prioridade "
        f"-label:AutoApply/Certificados/Extraido "
        f"-label:AutoApply/Certificados/Pendente "
        f"-label:AutoApply/Alertas/OTP "
        f"-label:AutoApply/Alertas/Recuperacao "
        f"-label:AutoApply/Geral/Agendamento "
        f"-label:AutoApply/Geral/Financeiro "
        f"-label:AutoApply/Geral/Servicos"
    )
    Log.info(f"Query: '{q_final[:90]}...'")

    try:
        listagem = service.users().messages().list(
            userId="me", maxResults=80, q=q_final
        ).execute()
        msgs = listagem.get("messages", [])
    except HttpError as e:
        Log.error(f"Erro ao listar e-mails gerais: {e}")
        return

    Log.info(f"E-mails gerais a processar: {len(msgs)}")

    total, salvas = 0, 0

    for msg in msgs:
        try:
            detalhe = service.users().messages().get(
                userId="me", id=msg["id"], format="full"
            ).execute()
        except HttpError as e:
            Log.warn(f"  Erro msg {msg['id']}: {e}")
            continue

        headers   = detalhe["payload"]["headers"]
        assunto   = _header(headers, "Subject", "Sem Assunto")
        remetente = _header(headers, "From",    "Desconhecido")
        data      = _header(headers, "Date",    "")
        corpo     = extrair_corpo(detalhe["payload"])

        # 1. Classificação heurística (rápida, sem token)
        categoria = _categoria_heuristica(assunto, corpo)

        # Detecta subcategoria para financeiro (ex: boleto_fatura, assinatura, pix)
        subcategoria = (
            _detectar_subcategoria_financeiro(assunto + " " + corpo[:200])
            if categoria == "financeiro" else None
        )

        # 2. Para agendamentos/financeiros/comunicados vale gastar 1 token Gemini
        analise = {}
        if _tem_budget("geral") and categoria in ("agendamento", "financeiro", "comunicado"):
            try:
                analise = classificar_geral_pipeline(remetente, assunto, corpo, msg["id"])
                if analise.get("categoria"):
                    categoria = analise["categoria"]
                # Sobrescreve subcategoria se Gemini identificou melhor
                if analise.get("subcategoria"):
                    subcategoria = analise["subcategoria"]
            except Exception as e:
                Log.warn(f"  Gemini geral falhou: {e}")

        resumo = (
            analise.get("resumo")
            or assunto[:80]
        )

        registro = {
            "email_id":       msg["id"],
            "assunto":        assunto,
            "remetente":      remetente,
            "remetente_nome": analise.get("remetente_nome") or remetente.split("<")[0].strip(),
            "data_email":     data,
            "categoria":      categoria,
            "subcategoria":   subcategoria,
            "resumo":         resumo,
            "data_evento":    analise.get("data_evento"),
            "valor":          analise.get("valor"),
            "link_principal": analise.get("link_principal") or _extrair_link_candidatura(corpo),
            "requer_acao":    bool(analise.get("requer_acao", False)),
            "status":         "novo",
        }

        if salvar_email_geral(sb, registro, dry_run):
            salvas += 1

        # ── Labels de gerais: pai + sub-label por categoria ──────────────────
        sub_label_key = {
            "agendamento": "geral_agendamento",
            "financeiro":  "geral_financeiro",
            "comunicado":  "geral_comunicado",
        }.get(categoria, "geral_servicos")

        _aplicar_label(
            service, msg["id"],
            labels.get("geral_pai"),
            labels.get(sub_label_key),
        )
        # ────────────────────────────────────────────────────────────────────

        total += 1
        icone = {"agendamento": "📅", "financeiro": "💰", "servicos": "🔔"}.get(categoria, "📧")
        Log.ok(f"  {icone} [{categoria}] {remetente.split('<')[0].strip()[:30]} — {resumo[:50]}")

    Log.ok(f"E-mails gerais: {total} processados | {salvas} salvos no Supabase")




def imprimir_sumario(inicio: datetime.datetime, modulos_rodados: list):
    duracao = (datetime.datetime.now() - inicio).total_seconds()
    Log.section("Execução concluída")
    print(f"  {'Módulos executados':<28}: {', '.join(modulos_rodados)}")
    print(f"  {'Duração total':<28}: {duracao:.1f}s")
    print(f"  {'Budget Gemini hoje':<28}: {_status_budget()}")
    print(f"  {'Cache de respostas':<28}: {len(_GEMINI_CACHE)} entradas em {_CACHE_PATH}")
    print(f"  {'Labels no Gmail':<28}: AutoApply/ → Vagas / Certificados / Alertas / Geral")
    print(f"  {'Supabase tabelas':<28}: vagas · certificados · alertas_acesso · emails_gerais")
    print()


# ===========================================================================
# 11. PONTO DE ENTRADA
# ===========================================================================

def main():
    parser = argparse.ArgumentParser(
        description="AutoApply AI — orquestrador principal",
        formatter_class=argparse.RawTextHelpFormatter,
    )
    parser.add_argument(
        "--dias", type=int, default=1,
        help="Janela de busca em dias (padrão: 1 = apenas hoje)",
    )
    parser.add_argument(
        "--modulo",
        choices=["vagas", "certificados", "alertas", "geral", "todos"],
        default="todos",
        help=(
            "Qual módulo rodar:\n"
            "  vagas        — extração e análise de vagas\n"
            "  certificados — extração e upload de certificados\n"
            "  alertas      — OTPs e alertas de acesso\n"
            "  geral        — e-mails gerais (agendamentos, financeiro, serviços)\n"
            "  todos        — todos os módulos (padrão)"
        ),
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Executa sem gravar nada no Supabase/Drive (modo teste)",
    )
    args = parser.parse_args()

    inicio = datetime.datetime.now()
    query  = f"newer_than:{args.dias}d"

    print(f"\n{'='*58}")
    print(f"  AUTOAPPLY AI — Pipeline Principal  v2")
    print(f"  {datetime.datetime.now().strftime('%d/%m/%Y %H:%M:%S')}  |  janela: {args.dias} dia(s)")
    if args.dry_run:
        print(f"  ⚠  MODO DRY-RUN — nenhum dado será gravado")
    print(f"{'='*58}\n")

    try:
        service, drive_service = autenticar_google()
    except FileNotFoundError as e:
        Log.error(str(e))
        sys.exit(1)

    labels = garantir_labels(service)
    sb = None if args.dry_run else criar_supabase()

    modulos_rodados = []
    rodar_todos = args.modulo == "todos"

    if rodar_todos or args.modulo == "vagas":
        rodar_vagas(service, labels, sb, query, args.dry_run)
        modulos_rodados.append("vagas")

    if rodar_todos or args.modulo == "certificados":
        rodar_certificados(service, drive_service, labels, sb, args.dias, args.dry_run)
        modulos_rodados.append("certificados")

    if rodar_todos or args.modulo == "alertas":
        rodar_alertas(service, labels, sb, args.dias, args.dry_run)
        modulos_rodados.append("alertas")

    if rodar_todos or args.modulo == "geral":
        rodar_emails_gerais(service, labels, sb, query, args.dry_run)
        modulos_rodados.append("geral")

    imprimir_sumario(inicio, modulos_rodados)


if __name__ == "__main__":
    main()