"""
extrator_vagas.py
------------------
Script standalone para extrair e agrupar VAGAS de emprego a partir do Gmail.

Reaproveita a autenticação OAuth2 do `gmail_leitor.py` original e adiciona:
  - Extração do corpo completo do e-mail (text/plain com fallback text/html)
  - Detecção heurística de e-mails que são vagas
  - Extração de links de candidatura
  - Extração de informações relevantes (cargo, empresa, tecnologias, resumo)
  - Agrupamento por fonte (jobbol, indeed, linkedin, etc.) e por empresa

IMPORTANTE:
  - Este script NÃO altera nada no Gmail (escopo readonly).
  - Este script NÃO salva em banco. Apenas IMPRIME o resultado.
  - A função `extrair_e_agrupar_vagas()` já está pronta para ser importada
    e integrada em outro módulo (basta usar o `return` ao invés do print).

Pré-requisitos:
    pip install google-api-python-client google-auth-httplib2 google-auth-oauthlib
    Ter `credentials.json` no mesmo diretório (Google Cloud Console -> OAuth2).
"""

import os.path
import base64
import re
import json
from html import unescape
from collections import defaultdict
from datetime import datetime

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build


# ---------------------------------------------------------------------------
# Configurações
# ---------------------------------------------------------------------------

SCOPES = ['https://www.googleapis.com/auth/gmail.readonly']

# Remetentes conhecidos de plataformas de vagas (fonte -> padrões no e-mail do remetente)
FONTES_VAGAS = {
    'jobbol':     ['jobbol'],
    'indeed':     ['indeed'],
    'linkedin':   ['linkedin'],
    'catho':      ['catho'],
    'infojobs':   ['infojobs'],
    'vagas':      ['vagas.com.br', 'vagas.com'],
    'gupy':       ['gupy.io', 'gupy'],
    'glassdoor':  ['glassdoor'],
    'trampos':    ['trampos.co'],
    'remotar':    ['remotar'],
    'programathor':['programathor'],
}

# Palavras-chave que indicam vaga no assunto/corpo
PALAVRAS_VAGA = [
    'vaga', 'oportunidade', 'processo seletivo', 'candidatura',
    'job opening', 'job alert', 'nova vaga', 'desenvolvedor',
    'developer', 'engenheiro', 'engineer', 'analista', 'estágio',
    'recrutamento', 'we are hiring', 'estamos contratando',
]

# Palavras que indicam que a vaga foi encerrada / congelada
PALAVRAS_ENCERRADA = [
    'congelada', 'congelado', 'finalizada', 'encerrada',
    'não seguiremos', 'nao seguiremos', 'processo encerrado',
]

# Tecnologias comuns que tentaremos detectar no texto da vaga
TECNOLOGIAS = [
    'python', 'java', 'javascript', 'typescript', 'node', 'nodejs', 'node.js',
    'react', 'angular', 'vue', 'next.js', 'nextjs',
    'django', 'flask', 'fastapi', 'spring', 'spring boot',
    'sql', 'mysql', 'postgresql', 'postgres', 'mongodb', 'redis', 'oracle',
    'aws', 'azure', 'gcp', 'google cloud', 'docker', 'kubernetes', 'k8s',
    'git', 'linux', 'jenkins', 'terraform',
    'power bi', 'tableau', 'excel', 'vba',
    'go', 'golang', 'rust', 'php', 'c#', '.net', 'ruby', 'rails',
    'data science', 'machine learning', 'ml', 'deep learning',
    'rpa', 'selenium', 'scraping', 'etl',
]


# ---------------------------------------------------------------------------
# 1) Autenticação (reaproveitada do gmail_leitor.py original)
# ---------------------------------------------------------------------------

def autenticar_gmail():
    """Realiza a autenticação via OAuth2 e retorna o serviço da API Gmail."""
    creds = None
    if os.path.exists('token.json'):
        creds = Credentials.from_authorized_user_file('token.json', SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file('credentials.json', SCOPES)
            creds = flow.run_local_server(port=0)
        with open('token.json', 'w') as token:
            token.write(creds.to_json())

    return build('gmail', 'v1', credentials=creds)


# ---------------------------------------------------------------------------
# 2) Helpers de extração do corpo do e-mail
# ---------------------------------------------------------------------------

def _decode_base64url(data: str) -> str:
    """Decodifica string base64url do Gmail para texto UTF-8."""
    if not data:
        return ''
    # Gmail usa base64url; precisa de padding
    data = data.replace('-', '+').replace('_', '/')
    padding = 4 - (len(data) % 4)
    if padding != 4:
        data += '=' * padding
    try:
        return base64.b64decode(data).decode('utf-8', errors='ignore')
    except Exception:
        return ''


def _extrair_corpo(payload: dict) -> str:
    """
    Extrai recursivamente o corpo do e-mail.
    Prioriza text/plain; se não houver, usa text/html (limpo).
    """
    plain_text = ''
    html_text = ''

    def walk(parts):
        nonlocal plain_text, html_text
        for part in parts:
            mime = part.get('mimeType', '')
            body = part.get('body', {})
            data = body.get('data')

            if mime == 'text/plain' and data and not plain_text:
                plain_text = _decode_base64url(data)
            elif mime == 'text/html' and data and not html_text:
                html_text = _decode_base64url(data)

            # Estrutura aninhada (multipart/alternative, multipart/mixed, etc.)
            if 'parts' in part:
                walk(part['parts'])

    # Caso o payload seja o root e tenha parts
    if 'parts' in payload:
        walk(payload['parts'])
    else:
        # E-mail simples sem múltiplas partes
        mime = payload.get('mimeType', '')
        data = payload.get('body', {}).get('data')
        if mime == 'text/plain' and data:
            plain_text = _decode_base64url(data)
        elif mime == 'text/html' and data:
            html_text = _decode_base64url(data)

    corpo = plain_text or _limpar_html(html_text)
    return _limpar_texto(corpo)


def _limpar_html(html: str) -> str:
    """Remove tags HTML básicas e retorna apenas o texto."""
    if not html:
        return ''
    # Remove <script>, <style> com seu conteúdo
    html = re.sub(r'<(script|style)[^>]*>.*?</\1>', '', html, flags=re.DOTALL | re.IGNORECASE)
    # Substitui <br>, </p>, </div> por quebra de linha
    html = re.sub(r'<\s*(br|/p|/div|/li|/tr)\s*/?>', '\n', html, flags=re.IGNORECASE)
    # Remove qualquer outra tag
    html = re.sub(r'<[^>]+>', '', html)
    # Decodifica entidades HTML (&amp;, &nbsp;, etc.)
    html = unescape(html)
    return html


def _limpar_texto(texto: str) -> str:
    """Remove excesso de quebras de linha e espaços."""
    if not texto:
        return ''
    texto = re.sub(r'\r\n?', '\n', texto)
    texto = re.sub(r'\n{3,}', '\n\n', texto)
    texto = re.sub(r'[ \t]+', ' ', texto)
    return texto.strip()


# ---------------------------------------------------------------------------
# 3) Detecção e parsing de vagas
# ---------------------------------------------------------------------------

def _detectar_fonte(remetente: str) -> str:
    """Identifica de qual plataforma o e-mail veio com base no remetente."""
    rem_lower = remetente.lower()
    for fonte, padroes in FONTES_VAGAS.items():
        if any(p in rem_lower for p in padroes):
            return fonte
    return 'outro'


def _eh_vaga(assunto: str, corpo: str, fonte: str) -> bool:
    """Heurística: o e-mail é uma vaga?"""
    if fonte != 'outro':
        return True
    texto = (assunto + ' ' + corpo[:500]).lower()
    return any(p in texto for p in PALAVRAS_VAGA)


def _vaga_encerrada(assunto: str, corpo: str) -> bool:
    """Detecta se a vaga foi congelada/encerrada."""
    texto = (assunto + ' ' + corpo[:500]).lower()
    return any(p in texto for p in PALAVRAS_ENCERRADA)


def _extrair_links_candidatura(corpo: str) -> list:
    """
    Extrai URLs do corpo do e-mail, priorizando links que pareçam
    ser de candidatura (contém palavras como 'candidatar', 'apply', 'vaga').
    """
    # Pega todas as URLs http(s)
    urls = re.findall(r'https?://[^\s<>"\')]+', corpo)
    # Remove duplicatas mantendo ordem
    urls = list(dict.fromkeys(urls))

    palavras_apply = ['candidatar', 'apply', 'vaga', 'job', 'oportunidade', 'view', 'detail']
    prioridade = [u for u in urls if any(p in u.lower() for p in palavras_apply)]
    outros = [u for u in urls if u not in prioridade]

    # Limita para não poluir o output (top 5 priorizados + 3 outros)
    return prioridade[:5] + outros[:3]


def _extrair_tecnologias(corpo: str) -> list:
    """Detecta tecnologias mencionadas no corpo da vaga."""
    texto = corpo.lower()
    encontradas = []
    for tech in TECNOLOGIAS:
        # \b para palavras inteiras, mas precisa escapar pontos
        pattern = r'(?<![a-z0-9])' + re.escape(tech) + r'(?![a-z0-9])'
        if re.search(pattern, texto):
            encontradas.append(tech)
    # Remove duplicatas mantendo ordem
    return list(dict.fromkeys(encontradas))


def _extrair_cargo_empresa(assunto: str, remetente: str, fonte: str) -> tuple:
    """
    Tenta extrair cargo e empresa do assunto do e-mail.
    Cada plataforma tem um padrão diferente, então isso é heurístico.
    Retorna (cargo, empresa).
    """
    cargo = None
    empresa = None

    # Padrão Jobbol: "Empresa abriu vaga de Cargo"
    m = re.match(r'^(.+?)\s+abriu\s+vaga\s+(?:de|para)\s+(.+)$', assunto, re.IGNORECASE)
    if m:
        empresa = m.group(1).strip()
        cargo = m.group(2).strip()
        return cargo, empresa

    # Padrão LinkedIn: "Cargo at Empresa" ou "vaga de Cargo em Empresa"
    m = re.search(r'(?:vaga\s+de|de)\s+(.+?)\s+(?:em|na|no|at)\s+(.+)$', assunto, re.IGNORECASE)
    if m:
        cargo = m.group(1).strip()
        empresa = m.group(2).strip()
        return cargo, empresa

    # Fallback: o próprio assunto vira o cargo, e o nome do remetente vira a empresa
    cargo = assunto.strip()
    # Remove "<email@dominio.com>" do remetente
    empresa_match = re.match(r'^"?(.+?)"?\s*<', remetente)
    empresa = empresa_match.group(1).strip() if empresa_match else fonte

    return cargo, empresa


def _resumo_descricao(corpo: str, max_chars: int = 300) -> str:
    """Pega as primeiras N caracteres relevantes do corpo como resumo."""
    if not corpo:
        return ''
    # Pula linhas muito curtas (provavelmente cabeçalho/saudação)
    linhas = [ln.strip() for ln in corpo.split('\n') if len(ln.strip()) > 30]
    texto = ' '.join(linhas) if linhas else corpo
    texto = re.sub(r'\s+', ' ', texto).strip()
    return texto[:max_chars] + ('...' if len(texto) > max_chars else '')


# ---------------------------------------------------------------------------
# 4) Função principal: extração + agrupamento
# ---------------------------------------------------------------------------

def extrair_e_agrupar_vagas(service, max_resultados: int = 30, query: str = "newer_than:30d") -> dict:
    """
    Busca e-mails do Gmail, identifica os que são vagas, extrai informações
    relevantes e agrupa por fonte (plataforma) e por empresa.

    Parâmetros:
        service         -> serviço Gmail autenticado
        max_resultados  -> número máximo de e-mails a buscar
        query           -> filtro do Gmail (ex: "newer_than:30d", "is:unread")

    Retorna:
        dict com a estrutura:
        {
            "total_emails_lidos": int,
            "total_vagas_detectadas": int,
            "vagas": [ {dict_vaga}, ... ],
            "agrupado_por_fonte": { "jobbol": [...], "indeed": [...], ... },
            "agrupado_por_empresa": { "Empresa X": [...], ... }
        }
    """
    resultado = {
        "total_emails_lidos": 0,
        "total_vagas_detectadas": 0,
        "vagas": [],
        "agrupado_por_fonte": defaultdict(list),
        "agrupado_por_empresa": defaultdict(list),
    }

    try:
        listagem = service.users().messages().list(
            userId='me', maxResults=max_resultados, q=query
        ).execute()
        mensagens = listagem.get('messages', [])
        resultado["total_emails_lidos"] = len(mensagens)

        for msg in mensagens:
            detalhe = service.users().messages().get(
                userId='me', id=msg['id'], format='full'
            ).execute()

            headers = detalhe['payload']['headers']
            assunto   = next((h['value'] for h in headers if h['name'] == 'Subject'), 'Sem Assunto')
            remetente = next((h['value'] for h in headers if h['name'] == 'From'), 'Desconhecido')
            data      = next((h['value'] for h in headers if h['name'] == 'Date'), '')

            corpo = _extrair_corpo(detalhe['payload'])
            fonte = _detectar_fonte(remetente)

            if not _eh_vaga(assunto, corpo, fonte):
                continue

            cargo, empresa = _extrair_cargo_empresa(assunto, remetente, fonte)
            vaga = {
                "id":          msg['id'],
                "data":        data,
                "remetente":   remetente,
                "fonte":       fonte,
                "assunto":     assunto,
                "cargo":       cargo,
                "empresa":     empresa,
                "encerrada":   _vaga_encerrada(assunto, corpo),
                "tecnologias": _extrair_tecnologias(corpo),
                "links_candidatura": _extrair_links_candidatura(corpo),
                "resumo":      _resumo_descricao(corpo),
            }

            resultado["vagas"].append(vaga)
            resultado["agrupado_por_fonte"][fonte].append(vaga)
            resultado["agrupado_por_empresa"][empresa].append(vaga)

        resultado["total_vagas_detectadas"] = len(resultado["vagas"])

        # Converte defaultdicts em dicts comuns antes de retornar
        resultado["agrupado_por_fonte"]   = dict(resultado["agrupado_por_fonte"])
        resultado["agrupado_por_empresa"] = dict(resultado["agrupado_por_empresa"])

    except Exception as error:
        print(f"[ERRO] Falha ao se comunicar com a API do Gmail: {error}")

    return resultado


# ---------------------------------------------------------------------------
# 5) Print bonito do retorno (apenas para visualização)
# ---------------------------------------------------------------------------

def imprimir_resultado(dados: dict) -> None:
    """Imprime de forma legível o que a função retornaria."""
    print("\n" + "=" * 80)
    print("RESUMO DA EXTRAÇÃO")
    print("=" * 80)
    print(f"  E-mails lidos       : {dados['total_emails_lidos']}")
    print(f"  Vagas detectadas    : {dados['total_vagas_detectadas']}")
    print(f"  Fontes encontradas  : {list(dados['agrupado_por_fonte'].keys())}")
    print(f"  Empresas encontradas: {len(dados['agrupado_por_empresa'])}")

    print("\n" + "=" * 80)
    print("VAGAS DETECTADAS (lista completa)")
    print("=" * 80)
    for i, v in enumerate(dados['vagas'], 1):
        print(f"\n[{i:02d}] {v['cargo']}  @  {v['empresa']}")
        print(f"     Fonte      : {v['fonte']}")
        print(f"     Data       : {v['data'][:25]}")
        print(f"     Encerrada? : {'SIM' if v['encerrada'] else 'não'}")
        print(f"     Tecnologias: {', '.join(v['tecnologias']) if v['tecnologias'] else '—'}")
        print("     Links      :")
        for link in v['links_candidatura']:
            print(f"        - {link}")
        print(f"     Resumo     : {v['resumo']}")

    print("\n" + "=" * 80)
    print("AGRUPAMENTO POR FONTE (plataforma)")
    print("=" * 80)
    for fonte, lista in dados['agrupado_por_fonte'].items():
        print(f"\n  >> {fonte.upper()} ({len(lista)} vaga(s))")
        for v in lista:
            print(f"     - {v['cargo']} @ {v['empresa']}")

    print("\n" + "=" * 80)
    print("AGRUPAMENTO POR EMPRESA")
    print("=" * 80)
    for empresa, lista in dados['agrupado_por_empresa'].items():
        print(f"\n  >> {empresa} ({len(lista)} vaga(s))")
        for v in lista:
            print(f"     - {v['cargo']}  [{v['fonte']}]")

    print("\n" + "=" * 80)
    print("JSON BRUTO (o que a função retorna)")
    print("=" * 80)
    print(json.dumps(dados, ensure_ascii=False, indent=2, default=str))


# ---------------------------------------------------------------------------
# 6) Execução
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    servico = autenticar_gmail()

    # Busca os últimos 30 dias, até 30 e-mails — ajuste como preferir
    dados = extrair_e_agrupar_vagas(
        servico,
        max_resultados=30,
        query="newer_than:30d",
    )

    imprimir_resultado(dados)
