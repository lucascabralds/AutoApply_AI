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
import unicodedata
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
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# Google / Gmail / Drive
# ---------------------------------------------------------------------------
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseUpload

import google_auth
from notificador import enviar_telegram, escapar_html, telegram_configurado
from formatacao import empresa_conhecida, formatar_reais
from seguranca import redigir_segredo
from services.gmail.add_task import criar_tarefa_vaga
import calendario
import candidatura

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

# Pasta do estado local (cache + budget). No Docker aponta para o volume /app/dados.
_DADOS_DIR   = os.getenv("AUTOAPPLY_DADOS_DIR") or os.path.dirname(os.path.abspath(__file__))
_CACHE_PATH  = os.path.join(_DADOS_DIR, "cache_gemini.json")
_BUDGET_PATH = os.path.join(_DADOS_DIR, "gemini_budget.json")

_BUDGET_LIMITE = {
    "vagas":   int(os.getenv("GEMINI_BUDGET_VAGAS",   "40")),
    "geral":   int(os.getenv("GEMINI_BUDGET_GERAL",   "10")),
    "alertas": int(os.getenv("GEMINI_BUDGET_ALERTAS", "10")),
    "cartas":  int(os.getenv("GEMINI_BUDGET_CARTAS",  "5")),
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
    return {"data": hoje, "vagas": 0, "geral": 0, "alertas": 0, "cartas": 0}

def _salvar_budget(budget: dict) -> None:
    try:
        with open(_BUDGET_PATH, "w") as f:
            json.dump(budget, f)
    except Exception:
        pass

_GEMINI_BUDGET: dict = _carregar_budget()


def _virar_dia_do_budget() -> None:
    """Zera os contadores quando o dia muda (importa no modo --intervalo)."""
    hoje = datetime.date.today().isoformat()
    if _GEMINI_BUDGET.get("data") != hoje:
        _GEMINI_BUDGET.clear()
        _GEMINI_BUDGET.update({"data": hoje, "vagas": 0, "geral": 0, "alertas": 0, "cartas": 0})


def _tem_budget(modulo: str) -> bool:
    """Retorna True se ainda há chamadas disponíveis para este módulo hoje."""
    _virar_dia_do_budget()
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
        f"alertas {b.get('alertas',0)}/{lim['alertas']} | "
        f"cartas {b.get('cartas',0)}/{lim['cartas']}"
    )

# ===========================================================================
# CONFIGURAÇÃO GLOBAL
# ===========================================================================

# Escopos, token.json e credentials.json ficam centralizados em google_auth.py
_BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Alertas: só notifica no Telegram códigos de e-mails com até N minutos
# (um OTP de ontem já expirou e só faria barulho no celular).
OTP_MAX_IDADE_MIN = int(os.getenv("OTP_MAX_IDADE_MIN", "15"))
# Vagas: score mínimo para virar notificação no Telegram
SCORE_NOTIFICAR_VAGA = int(os.getenv("SCORE_NOTIFICAR_VAGA", "75"))

# Máximo de e-mails lidos por módulo em cada execução
LIMITE_EMAILS = int(os.getenv("LIMITE_EMAILS_POR_MODULO", "100"))

PASTA_CERTIFICADOS = os.getenv("CERTIFICADOS_LOCAL", os.path.join(_BASE_DIR, "certificados"))
DRIVE_FOLDER_ID    = os.getenv("DRIVE_FOLDER_ID", None)  # ID da pasta no Drive (opcional)

# [FIX-6] Remove barra final da URL do Supabase
SUPABASE_URL = (os.getenv("SUPABASE_URL") or "").rstrip("/")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

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
#   AutoApply/Geral/Veiculo           Detran, IPVA, licenciamento, multas, CNH
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
    "geral_veiculo":      {"nome": "AutoApply/Geral/Veiculo",            "cor_bg": "#ffad47", "cor_txt": "#000000"},
}

# Filtro de busca que exclui e-mails já etiquetados por qualquer módulo.
# Cada módulo aplica sempre a label pai + a sub-label, então basta excluir os pais.
_QUERY_JA_PROCESSADO = " ".join(
    f"-label:{LABEL_TREE[k]['nome']}" for k in ("vagas_pai", "cert_pai", "alerta_pai", "geral_pai")
)

# Ordem de criação: pais antes dos filhos (obrigatório pela Gmail API)
_LABEL_ORDEM = [
    "vagas_pai",   "vagas_alta",    "vagas_baixa",
    "cert_pai",    "cert_extraido", "cert_pendente",
    "alerta_pai",  "alerta_otp",    "alerta_recuperacao",
    "geral_pai",   "geral_agendamento", "geral_financeiro",
    "geral_comunicado", "geral_servicos", "geral_veiculo",
]

# ---------------------------------------------------------------------------
# [FIX] Regra de exclusão — e-mails gerados pelo PRÓPRIO AutoApply AI
# (ex.: o briefing diário que o robô manda para a própria caixa de entrada,
# assunto "[AutoApply AI] Briefing Matinal — dd/mm/aaaa"). Como os módulos
# de vagas/gerais varrem TODA a caixa de entrada recente ("newer_than:Xd"),
# esse e-mail entrava no pipeline e era classificado por IA como qualquer
# outro — poluindo o relatório e gastando budget à toa. Esta checagem roda
# antes de qualquer heurística/IA e é 100% grátis.
# ---------------------------------------------------------------------------
PREFIXOS_EMAIL_PROPRIO = [
    "[autoapply ai]",
]


def _eh_email_proprio(assunto: str, remetente: str = "") -> bool:
    """True se o e-mail foi gerado pelo próprio pipeline AutoApply AI."""
    ass = (assunto or "").strip().lower()
    return any(ass.startswith(p) for p in PREFIXOS_EMAIL_PROPRIO)


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

    # [FIX] Oportunidades de aprendizado de bancos/instituições (ex.: Info
    # Santander / Santander Universidades) — não são vagas, são comunicados
    # sobre cursos/capacitação e devem cair em "Comunicados".
    "info santander", "santander universidades", "universidades santander",
    "trilha de carreira", "capacitação gratuita", "conteúdo gratuito",
    "workshop gratuito", "inscreva-se no curso", "bolsa de estudo",

    # Confirmações que NÃO são de vaga de emprego
    "confirmação de inscrição",  # inscrição em curso/evento, não candidatura de vaga

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
    OAuth2 unificado (ver google_auth.py). Retorna (gmail_service, drive_service).
    """
    gmail_service, drive_service = google_auth.construir_servicos("gmail", "drive", log=Log.info)
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
    # [FIX] Converte tanto ABERTURA quanto fechamento de br/p/div/li/tr/td/
    # table/h1-6 em quebra de linha. Muitos e-mails de vaga (ex.: Catho) são
    # montados com <table>/<tr>/<td>, e é a tag de ABERTURA que realmente
    # separa "Cargo" de "Empresa" visualmente. Convertendo só o fechamento
    # (como antes), cargo e empresa ficavam colados (ex.: "Analista
    # AdministrativoHIRING"), quebrando a extração de cargo/empresa/link.
    texto = re.sub(r"<\s*/?\s*(br|p|div|li|tr|td|table|h\d)\b[^>]*>", "\n", texto, flags=re.IGNORECASE)
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


def _extrair_corpo_e_html(payload: dict) -> tuple[str, str]:
    """
    Retorna (corpo_limpo, html_bruto).

    [FIX] O html_bruto preserva os hrefs originais das tags <a>. Isso é
    essencial: depois que _limpar_html remove as tags, o href some junto
    — então quem consumia só o corpo limpo (Gemini/heurística) nunca tinha
    acesso ao link real de candidatura, e "chutava" URLs (Indeed/Jobbol,
    gerando links quebrados) ou não achava nada (Catho/InfoJobs, que
    escondem a URL real atrás de um botão, nunca em texto puro).
    """
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

    corpo_limpo = _normalizar(_limpar_html(plain or html))
    return corpo_limpo, html


def extrair_corpo(payload: dict) -> str:
    """Mantido por compatibilidade — retorna só o texto limpo."""
    corpo, _ = _extrair_corpo_e_html(payload)
    return corpo


def _header(headers: list, name: str, default: str = "") -> str:
    nl = name.lower()
    return next((h["value"] for h in headers if h["name"].lower() == nl), default)


def _iterar_partes(payload: dict):
    """Percorre recursivamente todas as partes MIME (multipart/mixed → alternative → ...)."""
    for parte in payload.get("parts", []) or []:
        yield parte
        yield from _iterar_partes(parte)


def _eh_inline(parte: dict) -> bool:
    """
    Imagem embutida no corpo (logo, assinatura). A Content-Disposition manda:
    o Gmail põe Content-ID até em anexos de verdade ("attachment"), então o
    Content-ID só decide quando não há disposition.
    """
    headers = {h["name"].lower(): h["value"].lower() for h in parte.get("headers", [])}
    disposicao = headers.get("content-disposition", "").strip()
    if disposicao:
        return disposicao.startswith("inline")
    return "content-id" in headers


def _anexos_certificado(partes: list[dict]) -> list[dict]:
    """
    Anexos candidatos a certificado, na ordem de preferência: PDFs, depois
    imagens/arquivos anexados de verdade. Imagens inline (logo do e-mail
    dentro de multipart/related) ficam de fora — a busca em profundidade
    chegava nelas antes do PDF e a logo ia para o Drive como "certificado".
    """
    anexos = [p for p in partes if p.get("filename")]

    def prioridade(p):
        mime = p.get("mimeType", "")
        if mime == "application/pdf" or p["filename"].lower().endswith(".pdf"):
            return 0
        return 1

    return sorted((p for p in anexos if not _eh_inline(p)), key=prioridade)


def _listar_mensagens(service, query: str, limite: int) -> list[dict]:
    """
    messages.list com paginação. A API devolve no máximo uma página por
    chamada — sem seguir o nextPageToken, e-mails além da 1ª página eram
    silenciosamente ignorados em janelas maiores (--dias 7, por exemplo).
    """
    msgs: list[dict] = []
    token = None
    while len(msgs) < limite:
        resp = service.users().messages().list(
            userId="me", q=query, maxResults=min(100, limite - len(msgs)), pageToken=token,
        ).execute()
        msgs.extend(resp.get("messages", []))
        token = resp.get("nextPageToken")
        if not token:
            break
    return msgs[:limite]


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


# ---------------------------------------------------------------------------
# [FIX] Extração de âncoras reais do HTML (texto visível + href) e
# resolução do redirecionador de tracking da Catho.
# ---------------------------------------------------------------------------

_PADROES_LINK_IGNORAR = [
    "unsubscribe", "descadastr", "opt-out", "optout",
    "preferences", "manage-preferences", "manage_preferences",
    "mailto:", "tel:", "sms:",
    "mcauto-images", "sendgrid.net", "sendgrid.com",
    "/track/", "/pixel", "/beacon", "/open/",
    "view-online", "view_online", "view_in_browser", "web-view",
    "privacy", "terms-of-service", "policy",
    "instagram.com", "facebook.com", "twitter.com", "youtube.com",
    "linkedin.com/in/", "wa.me/",
]

_PALAVRAS_DESCADASTRO_TEXTO = [
    "cancelar", "descadastrar", "unsubscribe",
    "parar de receber", "não receber mais", "preferências", "remover",
]


def _resolver_link_real(href: str) -> str:
    """
    A Catho não linka direto para a vaga: embrulha a URL real dentro de um
    link de tracking próprio (channel-tracking/.../clicked?i=<payload
    base64 com {"r": "<url real>"}>). Sem desembrulhar isso, o link salvo
    é sempre o mesmo redirecionador genérico — que pode nem existir mais
    quando o usuário for clicar depois. Se não for esse formato, devolve
    o href original.
    """
    try:
        parsed = urllib.parse.urlparse(href)
        if "catho.com.br" in parsed.netloc and "channel-tracking" in parsed.path:
            qs = urllib.parse.parse_qs(parsed.query)
            payload = qs.get("i", [None])[0]
            if payload:
                pad = "=" * (-len(payload) % 4)
                decodificado = base64.urlsafe_b64decode(payload + pad)
                dados = json.loads(decodificado)
                destino = dados.get("r")
                if destino:
                    return destino
    except Exception:
        pass
    return href


def _extrair_ancoras(html: str) -> list[dict]:
    """
    [FIX] Extrai pares {"texto", "url"} do HTML ORIGINAL do e-mail — é aqui
    que o link de candidatura de verdade mora. Depois que o corpo passa por
    _limpar_html, os hrefs somem junto com a tag <a>, então o Gemini (e o
    fallback heurístico) ficavam só com o texto puro: para Indeed/Jobbol
    isso gerava links "chutados" e quebrados; para Catho/InfoJobs, que
    escondem a URL real atrás de um botão (nunca aparece como texto puro),
    o resultado era nenhum link.
    """
    if not html:
        return []
    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception:
        return []

    vistos: set[str] = set()
    ancoras: list[dict] = []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href.startswith(("http://", "https://")):
            continue
        if any(p in href.lower() for p in _PADROES_LINK_IGNORAR):
            continue

        texto = " ".join(a.get_text(" ", strip=True).split())
        if any(p in texto.lower() for p in _PALAVRAS_DESCADASTRO_TEXTO):
            continue

        href_real = _resolver_link_real(href)
        if href_real in vistos:
            continue
        vistos.add(href_real)
        ancoras.append({"texto": texto or "(sem texto)", "url": href_real})

    return ancoras


def _normalizar_para_match(texto: str) -> str:
    """Minúsculas, sem acento/pontuação — para comparar cargo x texto de link com tolerância."""
    if not texto:
        return ""
    texto = texto.lower().strip()
    texto = "".join(c for c in unicodedata.normalize("NFKD", texto) if not unicodedata.combining(c))
    texto = re.sub(r"[^a-z0-9 ]", " ", texto)
    return re.sub(r"\s+", " ", texto).strip()


def _achar_ancora_por_cargo(cargo: str, ancoras: list[dict], usadas: set) -> str | None:
    """
    Casa o cargo de uma vaga com a âncora cujo texto visível corresponde a
    ele — em vez de assumir que a N-ésima vaga corresponde ao N-ésimo link
    (que quebra sempre que a ordem/contagem diverge, comum em digests com
    várias vagas no mesmo e-mail, como Indeed e Catho).
    """
    alvo = _normalizar_para_match(cargo)
    if not alvo:
        return None
    for i, a in enumerate(ancoras):
        if i in usadas:
            continue
        texto_a = _normalizar_para_match(a["texto"])
        if not texto_a:
            continue
        if texto_a == alvo or texto_a.startswith(alvo) or alvo.startswith(texto_a):
            usadas.add(i)
            return a["url"]
    for i, a in enumerate(ancoras):
        if i in usadas:
            continue
        texto_a = _normalizar_para_match(a["texto"])
        if texto_a and (texto_a in alvo or alvo in texto_a):
            usadas.add(i)
            return a["url"]
    return None


def _extrair_link_candidatura(corpo: str, remetente: str = "", ancoras: list[dict] | None = None) -> str | None:
    """
    Extrai o link mais relevante da vaga.

    [FIX] Prioriza as âncoras reais do HTML (texto do link + href), que é
    onde o link de candidatura de fato mora — o corpo (já limpo de tags)
    normalmente não contém nenhuma URL para Catho/InfoJobs. Só cai para o
    regex em texto puro (comportamento antigo) quando não há âncoras.
    """
    if ancoras:
        prioridade_texto = [
            "candidatar", "candidatura", "vaga", "apply", "ver vaga",
            "enviar currículo", "enviar curriculo", "quero esta vaga",
            "inscreva-se", "saiba mais", "ver detalhes",
        ]
        for a in ancoras:
            if any(p in a["texto"].lower() for p in prioridade_texto):
                return a["url"]
        return ancoras[0]["url"]

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


def _vaga_uid(email_id: str, cargo: str | None, empresa: str | None) -> str:
    """
    Identificador estável de UMA vaga: mesmo e-mail + mesmo cargo + mesma
    empresa → mesmo uid. Permite upsert por vaga (um digest com 10 vagas
    vira 10 linhas) sem duplicar quando o e-mail é processado de novo.
    """
    chave = "|".join([email_id or "", _normalizar_para_match(cargo or ""), _normalizar_para_match(empresa or "")])
    return hashlib.sha1(chave.encode("utf-8")).hexdigest()[:20]


# None = ainda não testado nesta execução; False = banco sem a migração v3
_SCHEMA_VAGAS_V3: bool | None = None


def _erro_de_schema(exc: Exception) -> bool:
    """
    Só erros que indicam banco SEM a migração v3: coluna inexistente
    (PGRST204 / 42703) ou falta do índice único de vaga_uid (42P10).
    Erros de dado ("null value in column", "value too long for column")
    não podem desligar o formato novo para o resto da execução.
    """
    msg = str(exc).lower()
    return any(p in msg for p in (
        "pgrst204", "schema cache", "42703", "42p10",
        "no unique or exclusion constraint",
    ))


def salvar_vaga(sb: Client | None, vaga: dict, dry_run: bool = False) -> bool:
    global _SCHEMA_VAGAS_V3
    if dry_run:
        Log.info(f"  [DRY-RUN] vagas_extraidas: {vaga.get('cargo')} @ {vaga.get('empresa')}")
        return True
    if not sb:
        return False

    links = [vaga["link_candidatura"]] if vaga.get("link_candidatura") else []

    if _SCHEMA_VAGAS_V3 is not False:
        registro = {
            "vaga_uid":          _vaga_uid(vaga.get("email_id"), vaga.get("cargo"), vaga.get("empresa")),
            "gmail_message_id":  vaga.get("email_id"),
            "cargo":             vaga.get("cargo"),
            "empresa":           vaga.get("empresa"),
            "local":             vaga.get("local"),
            "modelo_trabalho":   vaga.get("modelo_trabalho"),
            "salario":           vaga.get("salario"),
            "score_match":       int(vaga.get("score_gemini") or 0),
            "fonte":             vaga.get("fonte"),
            "tecnologias":       vaga.get("tecnologias") or [],
            "requisitos":        vaga.get("requisitos") or [],
            "links_candidatura": links,
            "tipo_email":        vaga.get("tipo_email"),
            "data_email":        vaga.get("data_email"),
            "status_aplicacao":  vaga.get("status", "nova"),
        }
        try:
            sb.table("vagas_extraidas").upsert(registro, on_conflict="vaga_uid").execute()
            _SCHEMA_VAGAS_V3 = True
            return True
        except Exception as e:
            if _SCHEMA_VAGAS_V3 is None and _erro_de_schema(e):
                _SCHEMA_VAGAS_V3 = False
                Log.warn(
                    "  Tabela vagas_extraidas sem as colunas novas — rode supabase/schema.sql "
                    "no SQL Editor do Supabase. Usando o formato antigo (1 linha por e-mail)."
                )
            else:
                Log.warn(f"  Supabase 'vagas_extraidas' falhou: {e}")
                return False

    # Formato antigo (antes da migração v3)
    registro_legado = {
        "gmail_message_id":  vaga.get("email_id"),
        "cargo":             vaga.get("cargo"),
        "fonte":             vaga.get("fonte"),
        "tecnologias":       json.dumps(vaga.get("tecnologias") or [], ensure_ascii=False),
        "links_candidatura": json.dumps(links, ensure_ascii=False),
        "status_aplicacao":  vaga.get("status", "nova"),
    }
    return _sb_upsert_insert(sb, "vagas_extraidas", registro_legado, "gmail_message_id")


# Funil de candidatura — o status de uma vaga só avança, nunca regride
# (um "retorno" atrasado não pode desfazer um "avançou de fase").
_ORDEM_STATUS = {"nova": 0, "aplicada": 1, "em_processo": 2, "reprovada": 3, "aprovada": 3}


def _status_do_email(tipo_email: str | None, status_candidatura: str | None) -> str:
    """Traduz o tipo de e-mail classificado pela IA para o status da vaga no banco."""
    if tipo_email == "candidatura_enviada":
        return "aplicada"
    if tipo_email == "avanco_fase":
        return "em_processo"
    if tipo_email == "retorno":
        return "reprovada" if status_candidatura == "reprovado" else "em_processo"
    return "nova"


def atualizar_status_vaga(sb: Client | None, vaga: dict, novo_status: str) -> bool:
    """
    E-mails de acompanhamento ("sua candidatura foi enviada", "você avançou
    de fase"...) não são vagas novas: procura a vaga já salva com a mesma
    empresa e cargo parecido e só avança o status_aplicacao dela.
    Retorna True se encontrou a vaga (mesmo que o status já estivesse à frente).
    """
    if not sb or _SCHEMA_VAGAS_V3 is False:
        return False
    empresa, cargo = vaga.get("empresa"), vaga.get("cargo") or ""
    if not empresa_conhecida(empresa):
        return False
    padrao = re.sub(r"[%_*,()]", " ", empresa).strip()
    try:
        res = (
            sb.table("vagas_extraidas")
            .select("id, cargo, empresa, status_aplicacao")
            .ilike("empresa", f"%{padrao}%")
            .order("created_at", desc=True)
            .limit(50)
            .execute()
        )
    except Exception as e:
        Log.warn(f"  Busca da vaga para atualizar status falhou: {e}")
        return False

    def _mesmo_cargo(outro: str | None) -> bool:
        a, b = _normalizar_para_match(cargo), _normalizar_para_match(outro)
        return bool(a and b) and (a in b or b in a)

    candidatas = [r for r in (res.data or []) if _mesmo_cargo(r.get("cargo"))]
    if not candidatas:
        return False

    linha = candidatas[0]   # a mais recente
    atual = linha.get("status_aplicacao") or "nova"
    if _ORDEM_STATUS.get(novo_status, 0) <= _ORDEM_STATUS.get(atual, 0):
        Log.info(f"    ↺ {linha['cargo']} @ {linha['empresa']} já está em '{atual}'")
        return True
    try:
        sb.table("vagas_extraidas").update({"status_aplicacao": novo_status}).eq("id", linha["id"]).execute()
    except Exception as e:
        Log.warn(f"  Atualização de status falhou: {e}")
        return False
    Log.ok(f"    ↗ {linha['cargo']} @ {linha['empresa']}: {atual} → {novo_status}")
    return True


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


def _idade_minutos(detalhe: dict) -> float | None:
    """Minutos desde que o Gmail recebeu a mensagem (campo internalDate, em ms)."""
    try:
        recebido = int(detalhe["internalDate"]) / 1000
    except (KeyError, TypeError, ValueError):
        return None
    return (time.time() - recebido) / 60


def salvar_alerta(sb: Client | None, alerta: dict, dry_run: bool = False) -> bool:
    if dry_run:
        Log.info(f"  [DRY-RUN] log_acessos_otp: {alerta.get('plataforma')}")
        return True
    if not sb:
        return False
    # Schema: log_acessos_otp — o segredo em si nunca vai para o banco
    registro = {
        "plataforma":      alerta.get("plataforma"),
        "tipo_alerta":     alerta.get("tipo_solicitacao"),
        "codigo_extraido": redigir_segredo(alerta.get("codigo_ou_link")),
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

    # ── Financeiro e veículo (IPVA, multa...) → despesas_domesticas ────────
    if categoria in ("financeiro", "veiculo"):
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
            "categoria":        registro_raw.get("subcategoria") or "boleto_fatura",
            "origem_recurso":   "email",
        }
        # Só adiciona valor se for um número reconhecível ("R$ 1.234,56" → 1234.56)
        valor = _parsear_valor(registro_raw.get("valor"))
        if valor is not None:
            registro["valor"] = valor

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


def _parsear_valor(texto) -> float | None:
    """
    Converte valores como 'R$ 1.234,56', '49,90', 'USD 12.50' ou 49.9 em float.
    Retorna None quando não há número reconhecível.
    """
    if texto is None or isinstance(texto, bool):
        return None
    if isinstance(texto, (int, float)):
        return float(texto)
    texto = str(texto)
    # "3x de R$ 49,90" → o valor é o que vem depois do símbolo da moeda
    moeda = re.search(r"(?:R\$|US\$|\$|€|USD|BRL)\s*(\d[\d.,]*)", texto, re.IGNORECASE)
    m = moeda or re.search(r"\d[\d.,]*", texto)
    if not m:
        return None
    num = (moeda.group(1) if moeda else m.group(0)).rstrip(".,")
    if "," in num and "." in num:
        # o separador que aparece por último é o decimal
        if num.rfind(",") > num.rfind("."):
            num = num.replace(".", "").replace(",", ".")
        else:
            num = num.replace(",", "")
    elif "," in num:
        inteiro, _, dec = num.rpartition(",")
        num = f"{inteiro.replace(',', '')}.{dec}" if len(dec) <= 2 else num.replace(",", "")
    elif num.count(".") > 1 or (num.count(".") == 1 and len(num.rpartition(".")[2]) == 3):
        num = num.replace(".", "")   # 1.234 ou 1.234.567 → milhar
    try:
        return round(float(num), 2)
    except ValueError:
        return None


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
# 6. IA — prompts e parse de JSON moram em gemini_service.py
# ===========================================================================
# Aqui ficam só as camadas de economia em volta da IA:
#   cache local (por gmail_message_id) → heurística grátis → budget diário.
# As funções de gemini_service nunca levantam exceção: devolvem {} / [].

from rota_ia_client import gateway_disponivel
from gemini_service import status_ultima_chamada as _status_ia
from gemini_service import (
    classificar_email       as _gs_classificar,
    extrair_vagas           as _gs_extrair_vagas,
    analisar_acesso         as _gs_analisar_acesso,
    classificar_email_geral as _gs_classificar_geral,
    gerar_carta_apresentacao as _gs_gerar_carta,
)

_PALAVRAS_AGENDAMENTO = ["agendamento", "confirmado", "reserva", "horário", "consulta", "appointment"]
_PALAVRAS_FINANCEIRO  = ["fatura", "boleto", "cobrança", "pagamento", "nota fiscal", "pix", "débito", "crédito"]


def _cache_get(tipo: str, msg_id: str, tipo_python: type):
    """Retorna a resposta cacheada (se existir e tiver o tipo esperado) ou None."""
    if not msg_id:
        return None
    valor = _GEMINI_CACHE.get(_cache_key(tipo, msg_id))
    return valor if isinstance(valor, tipo_python) else None


def _cache_set(tipo: str, msg_id: str, valor, permitir_vazio: bool = False) -> None:
    if msg_id and (valor or (permitir_vazio and valor is not None)):
        _GEMINI_CACHE[_cache_key(tipo, msg_id)] = valor
        _salvar_cache(_GEMINI_CACHE)


# E-mails que não deu para classificar agora (IA fora do ar / falhou). O
# módulo de gerais não os etiqueta, para o de vagas tentar de novo depois.
_PENDENTES_IA: set[str] = set()

# Uma falha da IA num e-mail deixa ele para a próxima execução — mas só
# algumas vezes: se a falha for permanente (chave errada, prompt rejeitado,
# resposta sempre ilegível) o e-mail cai para a heurística em vez de ser
# retentado para sempre e sumir da janela de busca sem nunca ser salvo.
MAX_TENTATIVAS_IA = int(os.getenv("MAX_TENTATIVAS_IA", "3"))


def _pode_retentar(tipo: str, msg_id: str) -> bool:
    """
    Registra mais uma falha da IA para (tipo, e-mail). True enquanto vale
    deixar para depois; False quando já falhou MAX_TENTATIVAS_IA vezes
    ou o gateway está pausado e o e-mail não tem id.
    """
    if not msg_id:
        return False
    chave = _cache_key(f"falhas_{tipo}", msg_id)
    falhas = int(_GEMINI_CACHE.get(chave, 0)) + 1
    _GEMINI_CACHE[chave] = falhas
    _salvar_cache(_GEMINI_CACHE)
    return falhas < MAX_TENTATIVAS_IA


def classificar_email_pipeline(assunto: str, remetente: str, corpo_preview: str,
                               msg_id: str = "") -> str:
    """
    Etapa 1: classifica o e-mail para roteamento.

    Ordem de decisão (mais barata → mais cara):
      1. Cache local   — grátis, instantâneo
      2. Heurística    — grátis, 0 tokens
      3. IA            — só se houver budget disponível

    Retorna: "vaga" | "certificado" | "alerta" | "agendamento" |
             "financeiro" | "servico" | "outro"
    """
    cached = _cache_get("classif", msg_id, str)
    if cached:
        Log.info(f"  💾 Cache hit (classif): {cached}")
        return cached

    # Fonte conhecida de vagas (Indeed, Catho...) → sempre vaga
    if _detectar_fonte(remetente) != "outro":
        _cache_set("classif", msg_id, "vaga")
        return "vaga"

    txt = (assunto + " " + corpo_preview[:400]).lower()

    # Palavras de exclusão têm prioridade (cursos, newsletters, promoções...)
    if any(p in txt for p in PALAVRAS_NAO_VAGA):
        if any(w in txt for w in _PALAVRAS_AGENDAMENTO):
            return "agendamento"
        if any(w in txt for w in _PALAVRAS_FINANCEIRO):
            return "financeiro"
        return "servico"

    if any(p in txt for p in PALAVRAS_VAGA):
        _cache_set("classif", msg_id, "vaga")
        return "vaga"

    if any(w in txt for w in _PALAVRAS_AGENDAMENTO):
        return "agendamento"
    if any(w in txt for w in _PALAVRAS_FINANCEIRO):
        return "financeiro"

    if not _tem_budget("geral"):
        return "outro"

    if gateway_disponivel():
        resultado = _gs_classificar(assunto, remetente, corpo_preview)
        status = _status_ia()
        if status != "falha":
            if status == "ok":   # a heurística do gemini_service não chama a IA
                _consumir_budget("geral")
            _cache_set("classif", msg_id, resultado)
            return resultado

    # IA indisponível/falhou: o e-mail pode ser uma vaga — fica pendente
    # (até MAX_TENTATIVAS_IA vezes; depois segue como "outro")
    if _pode_retentar("classif", msg_id):
        _PENDENTES_IA.add(msg_id)
        return "indeterminado"
    return "outro"


def _vaga_heuristica(assunto: str, remetente: str, corpo: str,
                     ancoras: list[dict] | None) -> dict:
    """Vaga 'mínima' montada sem IA — usada quando o budget do dia acabou."""
    return {
        "cargo":              assunto,
        "empresa":            None,
        "local":              None,
        "modelo_trabalho":    "Não informado",
        "salario":            None,
        "requisitos":         [],
        "tecnologias":        _extrair_tecnologias(corpo),
        "score_match":        0,
        "link_candidatura":   _extrair_link_candidatura(corpo, remetente, ancoras),
        "tipo_email":         "nova_vaga",
        "status_candidatura": "nova",
    }


def extrair_vagas_pipeline(assunto: str, remetente: str, corpo: str,
                           msg_id: str = "", ancoras: list[dict] | None = None) -> list[dict] | None:
    """
    Etapa 2: extrai N vagas de um e-mail.

    Ordem de decisão:
      1. Cache local   — reutiliza resultado anterior
      2. IA            — só se houver budget de vagas
      3. Heurística    — budget esgotado: 1 item sem score

    `ancoras` (texto do link + href real, extraído do HTML original) é
    repassado à IA via `links_html` para que ela associe cada vaga ao link
    correto, em vez de "chutar" uma URL a partir do texto puro.

    Cada item da lista tem: cargo, empresa, local, modelo_trabalho,
    salario, link_candidatura, requisitos, tecnologias, score_match,
    tipo_email, status_candidatura

    Retorna [] quando a IA respondeu que não há vaga, e None quando o
    e-mail não pôde ser processado agora (IA fora do ar ou falhou).
    """
    cached = _cache_get("vagas", msg_id, list)
    if cached is not None:
        Log.info(f"  💾 Cache hit (vagas): {len(cached)} vaga(s)")
        return cached

    if not _tem_budget("vagas"):
        Log.warn(f"  ⚠ Budget IA/vagas esgotado ({_GEMINI_BUDGET.get('vagas',0)}/{_BUDGET_LIMITE['vagas']}) — usando heurística")
        return [_vaga_heuristica(assunto, remetente, corpo, ancoras)]

    if gateway_disponivel():
        resultado = _gs_extrair_vagas(assunto, remetente, corpo, links_html=ancoras)
        if _status_ia() == "ok":
            _consumir_budget("vagas")
            # Inclusive [] — senão um aviso sem vaga real ("sua candidatura
            # foi visualizada") voltaria para a IA a cada execução.
            _cache_set("vagas", msg_id, resultado, permitir_vazio=True)
            return resultado

    # Sem IA agora: o e-mail fica sem label e é reprocessado depois (sem gastar budget)
    if _pode_retentar("vagas", msg_id):
        Log.warn("  ⚠ IA indisponível/falhou — e-mail fica para a próxima execução")
        return None
    Log.warn(f"  ⚠ IA falhou {MAX_TENTATIVAS_IA}x neste e-mail — usando heurística")
    return [_vaga_heuristica(assunto, remetente, corpo, ancoras)]


def analisar_acesso_pipeline(remetente: str, assunto: str, corpo: str,
                             msg_id: str = "") -> dict:
    """Valida OTP / alertas de login. Usa cache + budget."""
    cached = _cache_get("alerta", msg_id, dict)
    if cached is not None:
        Log.info("  💾 Cache hit (alerta)")
        return cached

    if not _tem_budget("alertas"):
        Log.warn("  ⚠ Budget IA/alertas esgotado — alerta ignorado")
        return {}
    if not gateway_disponivel():
        return {}

    resultado = _gs_analisar_acesso(remetente, assunto, corpo)
    if _status_ia() == "ok":
        _consumir_budget("alertas")
        _cache_set("alerta", msg_id, resultado)
    return resultado


def classificar_geral_pipeline(remetente: str, assunto: str, corpo: str,
                               msg_id: str = "") -> dict | None:
    """
    Extrai info de agendamentos, vencimentos etc. Usa cache + budget geral.
    Retorna {} quando o budget acabou (segue só com a heurística) e None
    quando a IA está indisponível/falhou (vale tentar de novo depois).
    """
    cached = _cache_get("geral", msg_id, dict)
    if cached is not None:
        return cached

    if not _tem_budget("geral"):
        return {}
    if not gateway_disponivel():
        return None

    resultado = _gs_classificar_geral(remetente, assunto, corpo)
    if _status_ia() != "ok":
        # Falha pontual → tenta de novo depois; falha repetida → segue só com a heurística
        return None if _pode_retentar("geral", msg_id) else {}
    _consumir_budget("geral")
    _cache_set("geral", msg_id, resultado)
    return resultado


# ===========================================================================
# 7. MÓDULO DE VAGAS
# ===========================================================================

def notificar_vaga(vaga: dict) -> bool:
    """Push no Telegram para vagas de alta prioridade (no-op se não configurado)."""
    if not telegram_configurado():
        return False
    linhas = [
        f"🔥 <b>{escapar_html(vaga['cargo'])}</b> — {vaga.get('score_gemini', 0)}%",
        escapar_html(f"{vaga['empresa']} · {vaga.get('modelo_trabalho') or 'Não informado'}"
                     + (f" · {vaga['local']}" if vaga.get("local") else "")),
    ]
    if vaga.get("link_candidatura"):
        linhas.append(f'<a href="{escapar_html(vaga["link_candidatura"])}">Ver vaga</a>')
    return enviar_telegram("\n".join(linhas))


def preparar_rascunho_candidatura(service, vaga: dict, texto_email: str) -> str | None:
    """
    Gera a carta (IA) e cria o rascunho no Gmail para uma vaga de score alto.
    Uma vez por vaga (marcado no cache) e dentro do budget diário "cartas".
    Retorna o id do rascunho ou None.
    """
    uid = _vaga_uid(vaga.get("email_id"), vaga.get("cargo"), vaga.get("empresa"))
    if _cache_get("rascunho", uid, str):
        return None
    if not _tem_budget("cartas") or not gateway_disponivel():
        return None
    perfil = candidatura.carregar_perfil()
    if not perfil:
        Log.warn(f"  Perfil não encontrado em {candidatura.PERFIL_PATH} — rascunho não gerado")
        return None

    carta = _gs_gerar_carta(vaga, perfil)
    if _status_ia() == "ok":
        _consumir_budget("cartas")
    if not carta:
        return None

    msg = candidatura.montar_mensagem(
        vaga, carta,
        destinatario=candidatura.extrair_email_recrutador(texto_email),
        curriculo_pdf=candidatura.CURRICULO_PDF or None,
    )
    try:
        rascunho_id = candidatura.criar_rascunho(service, msg)
    except HttpError as e:
        Log.warn(f"  Falha ao criar rascunho de candidatura: {e}")
        return None
    _cache_set("rascunho", uid, rascunho_id or "ok")
    return rascunho_id


def _uma_vez_chave(acao: str, chave: str, executar) -> None:
    """
    Executa um efeito colateral (push, tarefa, evento...) no máximo uma vez
    por chave — marcado no cache — e sem deixar uma exceção interromper o
    resto do processamento do e-mail (que ainda precisa receber a label).
    """
    if _cache_get(acao, chave, str):
        return
    try:
        if executar():
            _cache_set(acao, chave, "ok")
    except Exception as e:
        Log.warn(f"  {acao} falhou ({chave}): {e}")


def _uma_vez(acao: str, vaga: dict, executar) -> None:
    """_uma_vez_chave() para efeitos de uma vaga específica."""
    _uma_vez_chave(acao, _vaga_uid(vaga.get("email_id"), vaga.get("cargo"), vaga.get("empresa")), executar)


def acoes_vaga_prioritaria(service, tasks_service, vaga: dict, texto_recrutador: str) -> None:
    """Telegram + Google Tasks (score ≥ SCORE_NOTIFICAR_VAGA) e rascunho de candidatura."""
    if vaga.get("tipo_email") != "nova_vaga":
        return
    if int(vaga.get("score_gemini") or 0) >= SCORE_NOTIFICAR_VAGA:
        if telegram_configurado():
            _uma_vez("notif", vaga, lambda: notificar_vaga(vaga))
        if tasks_service:
            def _tarefa():
                if criar_tarefa_vaga(tasks_service, vaga):
                    Log.ok("      ✅ Follow-up criado no Google Tasks")
                    return True
            _uma_vez("tarefa", vaga, _tarefa)
    if candidatura.deve_gerar_rascunho(vaga):
        # preparar_rascunho_candidatura já marca "rascunho" no cache; aqui só isola exceções
        try:
            if preparar_rascunho_candidatura(service, vaga, texto_recrutador):
                Log.ok("      ✉️  Rascunho de candidatura criado no Gmail")
        except Exception as e:
            Log.warn(f"  rascunho falhou para '{vaga.get('cargo')}': {e}")


def rodar_vagas(service, labels: dict, sb: Client | None, query: str, dry_run: bool,
                tasks_service=None):
    Log.section("Módulo 1 — Extração de Vagas")

    # Pula e-mails que qualquer módulo já etiquetou — antes, todo e-mail da
    # janela era relido (e salvo de novo no Supabase) a cada execução.
    query = f"{query} {_QUERY_JA_PROCESSADO}"
    try:
        msgs = _listar_mensagens(service, query, LIMITE_EMAILS)
    except HttpError as e:
        Log.error(f"Erro ao listar e-mails de vagas: {e}")
        return

    Log.info(f"E-mails novos na janela: {len(msgs)}")

    total_emails  = 0
    total_vagas   = 0
    total_salvas  = 0
    total_outros  = 0
    total_atualizadas = 0

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

        # ── [FIX] Ignora 100% e-mails do próprio AutoApply AI (grátis, antes da IA) ──
        if _eh_email_proprio(assunto, remetente):
            Log.info(f"  ⏭ Ignorado (e-mail do próprio AutoApply AI): {assunto[:50]}")
            continue

        corpo, html_bruto = _extrair_corpo_e_html(detalhe["payload"])
        ancoras = _extrair_ancoras(html_bruto)

        # ── Etapa 1: classificar o e-mail (barato, usa heurística primeiro) ──
        categoria = classificar_email_pipeline(assunto, remetente, corpo[:400], msg["id"])

        if categoria != "vaga":
            Log.info(f"  → [{categoria}] será tratado pelo módulo adequado: {assunto[:50]}")
            total_outros += 1
            continue

        # ── Etapa 2: extrair vagas (suporta múltiplas por e-mail) ─────────────
        Log.info(f"  Extraindo vagas de: {assunto[:60]}")
        vagas_extraidas = extrair_vagas_pipeline(assunto, remetente, corpo, msg["id"], ancoras=ancoras)

        if vagas_extraidas is None:
            continue   # IA indisponível — sem label, tenta de novo na próxima execução
        if not vagas_extraidas:
            # A IA leu e não há vaga real: marca como processado para não gastar IA de novo
            Log.info(f"  Nenhuma vaga real em: {assunto[:50]}")
            _aplicar_label(service, msg["id"], labels.get("vagas_pai"))
            total_emails += 1
            continue

        Log.info(f"  {len(vagas_extraidas)} vaga(s) encontrada(s) neste e-mail")

        score_max = 0
        ancoras_usadas: set = set()
        techs_corpo = _extrair_tecnologias(corpo)
        for vaga_raw in vagas_extraidas:
            techs_g  = [t.lower() for t in vaga_raw.get("tecnologias", [])
                        if isinstance(t, str)]
            # Tecnologias achadas no corpo inteiro só valem para e-mails de
            # 1 vaga — num digest elas misturariam as stacks de todas as vagas.
            if len(vagas_extraidas) == 1:
                techs = list(dict.fromkeys(techs_corpo + techs_g))
            else:
                techs = techs_g or techs_corpo
            score    = vaga_raw.get("score_match", 0) or 0
            score_max = max(score_max, score)

            # [FIX] Prioridade do link: 1) o que o Gemini já escolheu entre
            # as âncoras reais fornecidas; 2) casar o cargo desta vaga
            # específica com a âncora de texto correspondente (essencial
            # em digests com N vagas no mesmo e-mail, ex.: Catho/Indeed);
            # 3) fallback heurístico geral.
            link = (
                vaga_raw.get("link_candidatura")
                or _achar_ancora_por_cargo(vaga_raw.get("cargo") or assunto, ancoras, ancoras_usadas)
                or _extrair_link_candidatura(corpo, remetente, ancoras)
            )

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
                "requisitos":        vaga_raw.get("requisitos", []),
                "tecnologias":       techs,
                "score_gemini":      score,
                "tipo_email":        vaga_raw.get("tipo_email",         "nova_vaga"),
                "status_candidatura":vaga_raw.get("status_candidatura", "nova"),
                "link_candidatura":  link,
                "descricao":         corpo[:400] if corpo else "",
                "data_email":        data,
                "status":            _status_do_email(vaga_raw.get("tipo_email"),
                                                      vaga_raw.get("status_candidatura")),
            }

            # E-mail de acompanhamento de uma candidatura já conhecida →
            # atualiza a vaga existente em vez de criar uma duplicada.
            if (vaga["tipo_email"] != "nova_vaga" and not dry_run
                    and atualizar_status_vaga(sb, vaga, vaga["status"])):
                total_atualizadas += 1
                continue

            salvo = salvar_vaga(sb, vaga, dry_run)
            if salvo:
                total_salvas += 1

            total_vagas += 1
            flag = "🔥" if score >= 75 else ("⚡" if score >= 50 else "·")
            tipo = vaga_raw.get("tipo_email", "nova_vaga")
            lk   = f" → {link[:50]}" if link else ""
            Log.ok(f"    {flag} [{tipo}] {vaga['cargo']} @ {vaga['empresa']} — score {score}%{lk}")

            if not dry_run:
                # Num digest o e-mail do recrutador não é confiável por vaga
                mailtos = " ".join(re.findall(r'mailto:([^"?\s>]+)', html_bruto or ""))
                texto_recrutador = f"{corpo} {mailtos}" if len(vagas_extraidas) == 1 else ""
                acoes_vaga_prioritaria(service, tasks_service, vaga, texto_recrutador)

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
        f"{total_salvas} salvas | {total_atualizadas} status atualizados | "
        f"{total_outros} outros módulos"
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
        msgs = _listar_mensagens(service, query, LIMITE_EMAILS)
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

        # [FIX] Percorre partes aninhadas: a maioria dos e-mails com anexo é
        # multipart/mixed → multipart/alternative → text/html, e antes só o
        # 1º nível era lido (o HTML e o PDF ficavam invisíveis).
        partes            = list(_iterar_partes(detalhe["payload"]))
        sucesso           = False
        drive_url         = None
        caminho_resultado = ""
        plataforma_cert   = "desconhecido"   # será sobrescrito na extração

        # --- Parte A: anexos diretos (PDF primeiro; logos inline ignoradas) ---
        for parte in _anexos_certificado(partes):
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
            _, html_body = _extrair_corpo_e_html(detalhe["payload"])

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
        msgs = _listar_mensagens(service, f"{query} -label:{LABEL_TREE['alerta_pai']['nome']}", LIMITE_EMAILS)
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

        idade = _idade_minutos(detalhe)
        if not dry_run and telegram_configurado() and idade is not None and idade <= OTP_MAX_IDADE_MIN:
            linhas = [f"🔒 <b>{escapar_html(plataforma)}</b> — {escapar_html(tipo)}"]
            if codigo:
                linhas.append(f"<code>{escapar_html(codigo)}</code>")
            linhas.append(f"<i>{escapar_html(assunto[:80])}</i>")
            if enviar_telegram("\n".join(linhas)):
                Log.ok("  📲 Enviado para o Telegram")

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
# Veículo usa regex com limite de palavra: "seguro auto" não pode casar com
# "pagamento seguro automático", nem "licenciamento" com licença de software.
_PADROES_VEICULO = re.compile(
    r"\b(detran|ipva|crlv|renavam|dpvat|semparar|conectcar|veloe"
    r"|licenciamento (?:anual|d[eo] ve[ií]culo|\d{4})"
    r"|multa de tr[aâ]nsito|auto de infra[cç][aã]o|notifica[cç][aã]o de autua[cç][aã]o|infra[cç][aã]o de tr[aâ]nsito"
    r"|carteira nacional de habilita[cç][aã]o|renova[cç][aã]o da cnh"
    r"|seguro (?:auto|autom[oó]vel|d[eo] (?:carro|ve[ií]culo|moto))|vistoria veicular)\b"
)

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
_SUBCATEGORIA_VEICULO = {
    "veiculo_ipva":          ["ipva"],
    "veiculo_licenciamento": ["licenciamento", "crlv", "dpvat"],
    "veiculo_multa":         ["multa", "multas", "infração", "infracao", "autuação", "autuacao"],
    "veiculo_cnh":           ["cnh", "habilitação", "habilitacao"],
    "veiculo_seguro":        ["seguro"],
    "veiculo_pedagio":       ["semparar", "conectcar", "veloe", "pedágio", "pedagio"],
}

_SUBCATEGORIA_FINANCEIRO = {
    "boleto_fatura": ["boleto", "fatura", "vence", "vencimento", "valor a pagar", "emitido por", "pagar até"],
    "pix":           ["pix", "transferência", "ted", "doc"],
    "recibo":        ["comprovante", "recibo", "nfe", "nota fiscal"],
    "assinatura":    ["assinatura", "renovação", "google play", "renovada"],
    "cobranca":      ["cobrança", "cobranca", "débito", "debito", "crédito", "pendente"],
}

def _contem_termo(texto: str, termos: list[str]) -> bool:
    """Casa termos como palavra inteira — "doc" não pode casar com "documento", nem "ted" com "conectado"."""
    return any(re.search(r"(?<!\w)" + re.escape(t) + r"(?!\w)", texto) for t in termos)


def _detectar_subcategoria_veiculo(texto: str) -> str:
    txt = texto.lower()
    for sub, palavras in _SUBCATEGORIA_VEICULO.items():
        if _contem_termo(txt, palavras):
            return sub
    return "veiculo_outros"


def _detectar_subcategoria_financeiro(texto: str) -> str:
    txt = texto.lower()
    for sub, palavras in _SUBCATEGORIA_FINANCEIRO.items():
        if _contem_termo(txt, palavras):
            return sub
    return "boleto_fatura"

def _como_bool(valor) -> bool:
    """A IA às vezes manda "false"/"sim" como texto — bool("false") seria True."""
    if isinstance(valor, str):
        return valor.strip().lower() in ("true", "sim", "yes", "1", "s")
    return bool(valor)


_CATEGORIAS_GERAIS = {"agendamento", "financeiro", "comunicado", "servicos", "veiculo"}


def _normalizar_categoria_geral(categoria_ia: str | None, categoria_heuristica: str) -> str:
    """
    A IA às vezes responde no singular ("servico") ou com "outro". Antes a
    resposta sobrescrevia a heurística sem validação — e como o prompt nem
    tinha "comunicado", e-mails que a heurística marcava como comunicado
    nunca recebiam a label Geral/Comunicado.
    """
    cat = (categoria_ia or "").strip().lower()
    cat = {"servico": "servicos", "serviço": "servicos", "serviços": "servicos"}.get(cat, cat)
    return cat if cat in _CATEGORIAS_GERAIS else categoria_heuristica


def _categoria_heuristica(assunto: str, corpo: str) -> str:
    """Classificação rápida sem consumir token do Gemini."""
    txt = (assunto + " " + corpo[:300]).lower()
    # Veículo antes de financeiro: um boleto de IPVA é "veículo"
    if _PADROES_VEICULO.search(txt):
        return "veiculo"
    for cat, padroes in _PADROES_GERAL.items():
        if any(p in txt for p in padroes):
            return cat
    return "servicos"   # fallback


def agendar_no_calendario(calendar_service, msg_id: str, registro: dict) -> None:
    """Vencimento/agendamento com data futura → evento no Google Calendar (uma vez por e-mail)."""
    evento = calendario.evento_do_registro(registro)
    if not evento:
        return

    def _criar():
        if calendario.criar_evento(calendar_service, evento):
            Log.ok(f"      🗓  Evento no Calendar: {evento['summary']}")
            return True

    _uma_vez_chave("evento", msg_id, _criar)


def rodar_emails_gerais(service, labels: dict, sb: Client | None, query: str, dry_run: bool,
                        calendar_service=None):
    """
    Processa e-mails que não são vagas, certificados ou alertas.
    Usa heurística rápida para economizar tokens; chama Gemini apenas para
    e-mails onde o resumo/categoria não ficou claro pela heurística.
    Labels: AutoApply/Geral + sub-label por categoria.
    """
    Log.section("Módulo 4 — E-mails Gerais")

    # Exclui e-mails já processados pelos outros módulos
    # [FIX] A lista antiga esquecia Geral/Comunicado — comunicados eram
    # reclassificados (e gastavam budget de IA) em toda execução.
    q_final = f"{query} {_QUERY_JA_PROCESSADO}"
    Log.info(f"Query: '{q_final[:90]}...'")

    try:
        msgs = _listar_mensagens(service, q_final, LIMITE_EMAILS)
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

        # ── [FIX] Ignora 100% e-mails do próprio AutoApply AI (grátis, antes da IA) ──
        if _eh_email_proprio(assunto, remetente):
            Log.info(f"  ⏭ Ignorado (e-mail do próprio AutoApply AI): {assunto[:50]}")
            continue

        # E-mail de vaga que o módulo de vagas não conseguiu extrair agora
        # (IA fora do ar, budget...) fica sem label para ser tentado de novo
        # — se este módulo o etiquetasse como Geral, a vaga se perderia.
        if (_detectar_fonte(remetente) != "outro" or msg["id"] in _PENDENTES_IA
                or _cache_get("classif", msg["id"], str) == "vaga"):
            continue

        corpo     = extrair_corpo(detalhe["payload"])

        # 1. Classificação heurística (rápida, sem token)
        categoria = _categoria_heuristica(assunto, corpo)

        # 2. Para agendamentos/financeiros/veículo/comunicados vale gastar 1 chamada de IA
        analise = {}
        if _tem_budget("geral") and categoria in ("agendamento", "financeiro", "veiculo", "comunicado"):
            try:
                analise = classificar_geral_pipeline(remetente, assunto, corpo, msg["id"])
                if analise is None:
                    # IA fora agora: sem label, para voltar com data/valor (e evento
                    # no Calendar) quando o gateway responder
                    Log.info(f"  ⏸ IA indisponível — fica para a próxima: {assunto[:50]}")
                    continue
                # A heurística de veículo é bem específica (Detran, IPVA...) e a
                # IA tende a chamar isso só de "financeiro" — mantém veículo,
                # exceto quando é um agendamento (vistoria, exame da CNH).
                categoria_ia = _normalizar_categoria_geral(analise.get("categoria"), categoria)
                if categoria != "veiculo" or categoria_ia == "agendamento":
                    categoria = categoria_ia
            except Exception as e:
                Log.warn(f"  Gemini geral falhou: {e}")

        # Subcategoria (ex.: boleto_fatura, pix, veiculo_ipva) — depois da IA,
        # que pode ter mudado a categoria
        texto_sub = assunto + " " + corpo[:300]
        subcategoria = (
            _detectar_subcategoria_veiculo(texto_sub) if categoria == "veiculo"
            else _detectar_subcategoria_financeiro(texto_sub) if categoria == "financeiro"
            else None
        )

        resumo = (
            analise.get("resumo")
            or assunto[:80]
        )

        valor_num = _parsear_valor(analise.get("valor"))
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
            "valor_formatado": formatar_reais(valor_num) if valor_num is not None else None,
            "link_principal": analise.get("link_principal") or _extrair_link_candidatura(corpo),
            "requer_acao":    _como_bool(analise.get("requer_acao")),
            "status":         "novo",
        }

        if salvar_email_geral(sb, registro, dry_run):
            salvas += 1

        # ── Labels de gerais: pai + sub-label por categoria ──────────────────
        sub_label_key = {
            "agendamento": "geral_agendamento",
            "financeiro":  "geral_financeiro",
            "comunicado":  "geral_comunicado",
            "veiculo":     "geral_veiculo",
        }.get(categoria, "geral_servicos")

        _aplicar_label(
            service, msg["id"],
            labels.get("geral_pai"),
            labels.get(sub_label_key),
        )
        # ────────────────────────────────────────────────────────────────────

        total += 1
        icone = {"agendamento": "📅", "financeiro": "💰", "comunicado": "📢", "servicos": "🔔",
                 "veiculo": "🚗"}.get(categoria, "📧")
        Log.ok(f"  {icone} [{categoria}] {remetente.split('<')[0].strip()[:30]} — {resumo[:50]}")

        if calendar_service and not dry_run:
            agendar_no_calendario(calendar_service, msg["id"], registro)

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
    parser.add_argument(
        "--intervalo", type=int, default=0, metavar="SEGUNDOS",
        help=(
            "Fica rodando em loop, repetindo os módulos a cada N segundos.\n"
            "Ex.: --modulo alertas --intervalo 60  → OTPs no Telegram em ~1 min"
        ),
    )
    args = parser.parse_args()

    inicio = datetime.datetime.now()
    query  = f"newer_than:{args.dias}d"

    print(f"\n{'='*58}")
    print(f"  AUTOAPPLY AI — Pipeline Principal  v3")
    print(f"  {datetime.datetime.now().strftime('%d/%m/%Y %H:%M:%S')}  |  janela: {args.dias} dia(s)")
    if args.dry_run:
        print(f"  ⚠  MODO DRY-RUN — nenhum dado será gravado")
    print(f"{'='*58}\n")

    try:
        service, drive_service = autenticar_google()
    except (FileNotFoundError, RuntimeError) as e:
        Log.error(str(e))
        sys.exit(1)

    labels = garantir_labels(service)
    sb = None if args.dry_run else criar_supabase()
    tasks_service = google_auth.construir_servicos("tasks")[0] if google_auth.TASKS_ENABLED else None
    calendar_service = google_auth.construir_servicos("calendar")[0] if google_auth.CALENDAR_ENABLED else None

    rodar_todos = args.modulo == "todos"

    def executar_ciclo() -> list:
        modulos_rodados = []
        if rodar_todos or args.modulo == "vagas":
            rodar_vagas(service, labels, sb, query, args.dry_run, tasks_service)
            modulos_rodados.append("vagas")

        if rodar_todos or args.modulo == "certificados":
            rodar_certificados(service, drive_service, labels, sb, args.dias, args.dry_run)
            modulos_rodados.append("certificados")

        if rodar_todos or args.modulo == "alertas":
            rodar_alertas(service, labels, sb, args.dias, args.dry_run)
            modulos_rodados.append("alertas")

        if rodar_todos or args.modulo == "geral":
            rodar_emails_gerais(service, labels, sb, query, args.dry_run, calendar_service)
            modulos_rodados.append("geral")
        return modulos_rodados

    if args.intervalo <= 0:
        imprimir_sumario(inicio, executar_ciclo())
        return

    Log.info(f"Modo contínuo: repetindo a cada {args.intervalo}s (Ctrl+C para parar)")
    try:
        while True:
            try:
                executar_ciclo()
            except Exception as e:   # uma falha de rede não deve matar o loop
                Log.error(f"Ciclo falhou: {e}")
            time.sleep(args.intervalo)
    except KeyboardInterrupt:
        imprimir_sumario(inicio, [args.modulo])


if __name__ == "__main__":
    main()