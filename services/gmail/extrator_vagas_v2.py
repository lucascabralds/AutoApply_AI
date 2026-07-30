

import os.path
import base64
import re
import json
import unicodedata
import urllib.parse
from html import unescape
from collections import defaultdict

from bs4 import BeautifulSoup
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build


# ===========================================================================
# Configurações
# ===========================================================================

SCOPES = ['https://www.googleapis.com/auth/gmail.readonly']

FONTES_VAGAS = {
    'jobbol':       ['jobbol'],
    'indeed':       ['indeed'],
    'linkedin':     ['linkedin'],
    'catho':        ['catho'],
    'infojobs':     ['infojobs'],
    'vagas':        ['vagas.com.br', 'vagas.com'],
    'gupy':         ['gupy.io', 'gupy'],
    'glassdoor':    ['glassdoor'],
    'trampos':      ['trampos.co'],
    'remotar':      ['remotar'],
    'programathor': ['programathor'],
    'pandape':      ['pandape'],
    'micro1':       ['micro1'],
}

PALAVRAS_VAGA = [
    'vaga', 'oportunidade', 'processo seletivo', 'candidatura',
    'job opening', 'job alert', 'nova vaga', 'desenvolvedor',
    'developer', 'engenheiro', 'engineer', 'analista', 'estágio',
    'recrutamento', 'we are hiring', 'estamos contratando',
    'contratando', 'contrata-se', 'application',
]

PALAVRAS_ENCERRADA = [
    'congelada', 'congelado', 'finalizada', 'encerrada',
    'não seguiremos', 'nao seguiremos', 'processo encerrado',
]

TECNOLOGIAS = [
    'python', 'java', 'javascript', 'typescript', 'node', 'nodejs', 'node.js',
    'react', 'angular', 'vue', 'next.js', 'nextjs',
    'django', 'flask', 'fastapi', 'spring', 'spring boot',
    'sql', 'mysql', 'postgresql', 'postgres', 'mongodb', 'redis', 'oracle',
    'aws', 'azure', 'gcp', 'google cloud', 'docker', 'kubernetes', 'k8s',
    'git', 'linux', 'jenkins', 'terraform', 'databricks',
    'power bi', 'powerbi', 'tableau', 'excel', 'vba', 'dax', 'power query',
    'go', 'golang', 'rust', 'php', 'c#', '.net', 'ruby', 'rails',
    'data science', 'machine learning', 'ml', 'deep learning',
    'rpa', 'selenium', 'scraping', 'etl', 'elt',
    'azure data factory',
]

# Extensões/padrões de URL que NUNCA são links de candidatura
EXTENSOES_RUIM = (
    '.png', '.jpg', '.jpeg', '.gif', '.svg', '.webp', '.bmp', '.ico',
    '.mp4', '.mov', '.avi', '.mp3', '.wav',
    '.css', '.js', '.woff', '.woff2', '.ttf', '.eot',
    '.pdf',
)

PADROES_LINK_IGNORAR = [
    'unsubscribe', 'descadastr', 'opt-out', 'optout',
    'preferences', 'manage-preferences', 'manage_preferences',
    'mailto:', 'tel:', 'sms:',
    'mcauto-images', 'sendgrid.net', 'sendgrid.com',
    '/track/', '/pixel', '/beacon', '/open/',
    'view-online', 'view_online', 'view_in_browser', 'web-view',
    'privacy', 'terms-of-service', 'policy',
    'instagram.com', 'facebook.com', 'twitter.com', 'youtube.com',
    'linkedin.com/in/', 'wa.me/',
    'cancelar', 'remover', 'unsub', 'sair', 'alertas', 'notificacoes', 
    'configuracoes', 'ajuda'
]


# ===========================================================================
# 1) Autenticação (reaproveitada do gmail_leitor.py original)
# ===========================================================================

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


# ===========================================================================
# 2) Helpers de extração e limpeza
# ===========================================================================

def _decode_base64url(data: str) -> str:
    if not data:
        return ''
    data = data.replace('-', '+').replace('_', '/')
    padding = 4 - (len(data) % 4)
    if padding != 4:
        data += '=' * padding
    try:
        return base64.b64decode(data).decode('utf-8', errors='ignore')
    except Exception:
        return ''


def _limpar_html(texto: str) -> str:
    """
    Remove tags HTML, scripts, estilos CSS e decodifica entidades.
    Funciona TANTO para HTML puro quanto para text/plain "sujo"
    (que às vezes vem com tags ou blocos CSS inline).
    """
    if not texto:
        return ''
    # 1) Remove <script>...</script> e <style>...</style>
    texto = re.sub(r'<(script|style)[^>]*>.*?</\1>', '', texto, flags=re.DOTALL | re.IGNORECASE)
    # 2) Remove blocos CSS soltos (text/plain às vezes vem com "body { ... }")
    texto = re.sub(r'[a-zA-Z][a-zA-Z0-9_\-\s,.\*#:>\+]*\{[^{}]{1,500}\}', ' ', texto)
    # 3) Converte <br>, <p>/</p>, <div>/</div>, <li>/</li>, <tr>/</tr>,
    #    <td>/</td>, <table>/</table>, <h1..6>/</h1..6> em quebra de linha.
    #    IMPORTANTE: converte tanto abertura quanto fechamento — muitos
    #    e-mails de vaga são montados com <table>/<tr>/<td> (ex.: Catho),
    #    e a tag de ABERTURA de uma nova linha/célula é o que realmente
    #    separa "Cargo" de "Empresa" visualmente. Convertendo só o
    #    fechamento (como antes), cargo e empresa ficavam colados em uma
    #    única string (ex.: "Analista AdministrativoHIRING"), quebrando a
    #    detecção de qual linha é cargo e qual é empresa.
    texto = re.sub(r'<\s*/?\s*(br|p|div|li|tr|td|table|h\d)\b[^>]*>', '\n', texto, flags=re.IGNORECASE)
    # 4) Remove qualquer outra tag
    texto = re.sub(r'<[^>]+>', '', texto)
    # 5) Decodifica entidades HTML
    texto = unescape(texto)
    # 6) Remove comentários de CSS/HTML residuais
    texto = re.sub(r'/\*.*?\*/', '', texto, flags=re.DOTALL)
    return texto


def _limpar_texto(texto: str) -> str:
    """Normaliza espaços e quebras de linha."""
    if not texto:
        return ''
    texto = re.sub(r'\r\n?', '\n', texto)
    texto = re.sub(r'\n{3,}', '\n\n', texto)
    texto = re.sub(r'[ \t]+', ' ', texto)
    texto = '\n'.join(linha.rstrip() for linha in texto.split('\n'))
    return texto.strip()


def _extrair_corpo(payload: dict) -> str:
    """
    Extrai recursivamente o corpo do e-mail.
    Prefere text/plain; cai para text/html quando necessário.
    SEMPRE passa por _limpar_html no final (text/plain às vezes vem sujo).
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

            if 'parts' in part:
                walk(part['parts'])

    if 'parts' in payload:
        walk(payload['parts'])
    else:
        mime = payload.get('mimeType', '')
        data = payload.get('body', {}).get('data')
        if mime == 'text/plain' and data:
            plain_text = _decode_base64url(data)
        elif mime == 'text/html' and data:
            html_text = _decode_base64url(data)

    bruto = plain_text or html_text
    # Sempre limpa — o text/plain do Gmail às vezes vem com tags/CSS embutidos
    return {
        "texto": _limpar_texto(_limpar_html(bruto)),
        # HTML original (com as tags <a href>) — é AQUI que os links de
        # candidatura realmente moram. Depois de _limpar_html, o href já
        # foi removido junto com a tag, então quem precisa do link real
        # (candidatura, "ver vaga" etc.) tem que olhar para este campo,
        # nunca para o texto limpo.
        "html": html_text or None,
    }


# ===========================================================================
# 3) Detecção e parsing
# ===========================================================================

def _detectar_fonte(remetente: str) -> str:
    rem_lower = remetente.lower()
    for fonte, padroes in FONTES_VAGAS.items():
        if any(p in rem_lower for p in padroes):
            return fonte
    return 'outro'


def _eh_vaga(assunto: str, corpo: str, fonte: str) -> bool:
    if fonte != 'outro':
        return True
    texto = (assunto + ' ' + corpo[:500]).lower()
    return any(p in texto for p in PALAVRAS_VAGA)


def _vaga_encerrada(assunto: str, corpo: str) -> bool:
    texto = (assunto + ' ' + corpo[:500]).lower()
    return any(p in texto for p in PALAVRAS_ENCERRADA)


def _link_eh_util(url: str) -> bool:
    """Filtra links de imagem, tracking, unsubscribe, redes sociais."""
    u = url.lower().rstrip(').,;:')
    if any(u.endswith(ext) for ext in EXTENSOES_RUIM):
        return False
    if any(p in u for p in PADROES_LINK_IGNORAR):
        return False
    return True


def _extrair_links(corpo: str) -> list:
    """
    Fallback para e-mails sem HTML (só text/plain de verdade, com URLs
    escritas no próprio texto). Extrai URLs do corpo, filtra ruins e
    prioriza as de candidatura.
    NÃO usar isso como fonte principal de links: a grande maioria dos
    avisos de vaga é HTML, e o link real fica só no atributo href — ver
    _extrair_ancoras.
    """
    urls = re.findall(r'https?://[^\s<>"\')\]\}]+', corpo)
    urls = [u.rstrip('.,;:)\'"') for u in urls]
    urls = list(dict.fromkeys(urls))
    urls = [u for u in urls if _link_eh_util(u)]

    palavras_apply = ['candidatar', 'apply', 'vaga', 'job', 'oportunidade',
                      'view', 'detail', '/rc/clk', '/pagead', '/post/']
    prioridade = [u for u in urls if any(p in u.lower() for p in palavras_apply)]
    outros = [u for u in urls if u not in prioridade]

    return prioridade + outros


def _link_eh_generico_demais(url: str) -> bool:
    """Rejeita links que são só a home do site (sem path específico de vaga)."""
    try:
        p = urllib.parse.urlparse(url)
        return p.path in ('', '/')
    except Exception:
        return False


def _resolver_link_real(href: str) -> str:
    """
    Alguns provedores (ex.: Catho) não linkam direto para a vaga: eles
    embrulham a URL real dentro de um link de tracking próprio, com um
    payload em base64 no parâmetro '?i='. Se não desembrulharmos isso,
    o link salvo é sempre o mesmo redirecionador genérico e pode expirar.
    Aqui tentamos decodificar e devolver a URL de destino real (chave
    "r" do JSON); se não for esse formato, devolve o href original.
    """
    try:
        parsed = urllib.parse.urlparse(href)
        if 'catho.com.br' in parsed.netloc and 'channel-tracking' in parsed.path:
            qs = urllib.parse.parse_qs(parsed.query)
            payload = qs.get('i', [None])[0]
            if payload:
                pad = '=' * (-len(payload) % 4)
                decodificado = base64.urlsafe_b64decode(payload + pad)
                dados = json.loads(decodificado)
                destino = dados.get('r')
                if destino:
                    return destino
    except Exception:
        pass
    return href


def _extrair_ancoras(html: str) -> list:
    if not html:
        return []
    try:
        soup = BeautifulSoup(html, 'html.parser')
    except Exception:
        return []

    ancoras = []
    for a in soup.find_all('a', href=True):
        href = a['href'].strip()
        if not href.startswith(('http://', 'https://')):
            continue
        if not _link_eh_util(href):
            continue
            
        texto = ' '.join(a.get_text(' ', strip=True).split())
        
        # Filtro pelo texto visível do link
        texto_lower = texto.lower()
        palavras_descadastro = [
            'cancelar', 'descadastrar', 'unsubscribe', 
            'parar de receber', 'não receber mais', 'preferências', 'remover'
        ]
        if any(p in texto_lower for p in palavras_descadastro):
            continue
            
        ancoras.append({"texto": texto, "href": href})
    return ancoras


def _extrair_links_de_ancoras(ancoras: list) -> list:
    """Lista de hrefs únicos (na ordem em que aparecem), priorizando os de candidatura."""
    urls = []
    for a in ancoras:
        if a["href"] not in urls:
            urls.append(a["href"])

    palavras_apply = ['candidatar', 'apply', 'vaga', 'job', 'oportunidade',
                      'view', 'detail', '/rc/clk', '/pagead', '/post/']
    prioridade = [u for u in urls if any(p in u.lower() for p in palavras_apply)]
    outros = [u for u in urls if u not in prioridade]
    return prioridade + outros


def _normalizar(texto: str) -> str:
    """Minúsculas, sem acento, sem pontuação — para comparar cargo x texto de link com tolerância."""
    if not texto:
        return ''
    texto = texto.lower().strip()
    texto = ''.join(c for c in unicodedata.normalize('NFKD', texto) if not unicodedata.combining(c))
    texto = re.sub(r'[^a-z0-9 ]', ' ', texto)
    texto = re.sub(r'\s+', ' ', texto).strip()
    return texto


def _achar_link_por_texto(cargo: str, ancoras: list, usadas: set) -> str:
    """
    Encontra, entre as âncoras extraídas do HTML, aquela cujo texto
    visível corresponde ao cargo da vaga — em vez de pegar o link por
    posição/índice, que é o que causava o link errado (ou o mesmo link
    para todas as vagas) quando havia múltiplas ofertas no mesmo e-mail.

    Compara de forma normalizada (sem acento/caixa) e tolera truncamento
    (o Indeed, por exemplo, corta títulos longos com "...").
    """
    alvo = _normalizar(cargo)
    if not alvo:
        return None

    # 1) match exato ou por prefixo (cobre truncamento de título)
    for i, a in enumerate(ancoras):
        if i in usadas:
            continue
        texto_ancora = _normalizar(a['texto'])
        if not texto_ancora:
            continue
        if texto_ancora == alvo or texto_ancora.startswith(alvo) or alvo.startswith(texto_ancora):
            usadas.add(i)
            return a['href']

    # 2) match por substring (uma string contida na outra)
    for i, a in enumerate(ancoras):
        if i in usadas:
            continue
        texto_ancora = _normalizar(a['texto'])
        if texto_ancora and (texto_ancora in alvo or alvo in texto_ancora):
            usadas.add(i)
            return a['href']

    return None


def _extrair_tecnologias(texto: str) -> list:
    """Detecta tecnologias mencionadas no texto."""
    t = texto.lower()
    encontradas = []
    for tech in TECNOLOGIAS:
        pattern = r'(?<![a-z0-9])' + re.escape(tech) + r'(?![a-z0-9])'
        if re.search(pattern, t):
            encontradas.append(tech)
    return list(dict.fromkeys(encontradas))


def _resumo_descricao(corpo: str, max_chars: int = 300) -> str:
    if not corpo:
        return ''
    linhas = [ln.strip() for ln in corpo.split('\n') if len(ln.strip()) > 30]
    texto = ' '.join(linhas) if linhas else corpo
    texto = re.sub(r'\s+', ' ', texto).strip()
    return texto[:max_chars] + ('...' if len(texto) > max_chars else '')


# ---------------------------------------------------------------------------
# 3.1) Extração de MÚLTIPLAS ofertas dentro de um único e-mail
# ---------------------------------------------------------------------------

def _ofertas_indeed(corpo: str, ancoras: list) -> list:
    """
    Indeed envia o e-mail com várias vagas no formato:
        Cargo
        Empresa  rating
        Cidade, UF
        descrição curta...
        há X dias
    Usamos "há X dias/horas/mês" como separador de blocos para achar
    cargo/empresa/local/descrição.

    Para o LINK: o Indeed coloca o próprio título da vaga como texto do
    link (<a>Analista de Dados</a> -> /rc/clk/dl?jk=...), então casamos
    cada cargo extraído do bloco com a âncora de mesmo texto — muito mais
    confiável do que supor que a N-ésima vaga do corpo corresponde ao
    N-ésimo link da lista (que quebra sempre que a ordem ou a contagem
    diverge, e pior: antes nem havia links pra contar, pois vinham só do
    href, que era descartado antes da extração).
    """
    blocos = re.split(r'há\s+\d+\s+(?:dias?|horas?|hora|m[êe]s|meses|min)', corpo, flags=re.IGNORECASE)
    ofertas = []
    ancoras_vaga = [a for a in ancoras if any(
        p in a['href'] for p in ('/rc/clk/', '/pagead/clk/', '/viewjob')
    )]
    usadas = set()

    for bloco in blocos:
        b = bloco.strip()
        if len(b) < 30:
            continue
        linhas = [ln.strip() for ln in b.split('\n') if ln.strip()]
        cargo = next((ln for ln in linhas if 3 < len(ln) < 120
                      and not ln.lower().startswith(('http', 'see ', 'estes ', '30 vagas'))), None)
        if not cargo:
            continue
        try:
            i = linhas.index(cargo)
        except ValueError:
            i = 0
        empresa = linhas[i + 1] if i + 1 < len(linhas) else 'Não identificada'
        empresa = re.sub(r'\s+\d+\.\d+.*$', '', empresa).strip()
        local = next((ln for ln in linhas[i:i+5] if re.search(r',\s*[A-Z]{2}\b', ln)), None)
        descricao = ' '.join(linhas[i+1:])[:300]

        link = _achar_link_por_texto(cargo, ancoras_vaga, usadas)
        if not link:
            # Sem link correspondente = quase certamente não é uma vaga de
            # verdade (é o cabeçalho antes da 1ª vaga ou o rodapé/aviso
            # legal depois da última — ex.: "Não compartilhe este e-mail").
            # No Indeed, toda vaga real tem uma âncora própria no HTML.
            continue

        ofertas.append({
            "cargo":       cargo,
            "empresa":     empresa,
            "local":       local,
            "tecnologias": _extrair_tecnologias(b),
            "link":        link,
            "descricao":   descricao,
        })
    return ofertas


def _ofertas_catho(corpo: str, ancoras: list) -> list:
    """
    Catho envia múltiplas vagas no formato:
        Cargo
        EMPRESA  (geralmente em CAIXA ALTA na linha seguinte)
        ver todas as vagas

    Para o LINK: o Catho embrulha a URL real da vaga em um redirecionador
    de tracking (channel-tracking/v1/email/clicked?i=<payload base64>),
    e o texto visível de cada link é justamente o título do cargo (ou
    "Enviar currículo" logo abaixo, com o MESMO href). Já resolvemos o
    payload em _resolver_link_real, então aqui só precisamos casar o
    cargo com a âncora de texto correspondente — antes o código pegava
    sempre links[0] (o primeiro link do e-mail) para TODAS as vagas, e
    além disso a lista de links vinha sempre vazia, pois o link nunca
    aparece como texto puro no corpo do e-mail, só no href.
    """
    linhas = [ln.strip() for ln in corpo.split('\n') if ln.strip()]
    ofertas = []
    usadas = set()
    for i, ln in enumerate(linhas[:-1]):
        prox = linhas[i + 1]
        eh_empresa = (
            len(prox) >= 3 and len(prox) <= 60
            and (
                'empresa confidencial' in prox.lower()  # comum na Catho, não é caixa alta
                or sum(c.isupper() for c in prox if c.isalpha()) >= max(3, len(re.sub(r'[^a-zA-Z]', '', prox)) * 0.6)
            )
            and 'http' not in prox.lower()
            and 'catho' not in prox.lower()
        )
        eh_cargo = (
            5 < len(ln) < 120
            and re.search(r'(analista|desenvolvedor|programador|engenheiro|trainee|assistente|coordenador|estágio|estagiário|gerente|especialista|consultor)', ln, re.IGNORECASE)
            and 'http' not in ln.lower()
        )
        if eh_cargo and eh_empresa:
            link = _achar_link_por_texto(ln, ancoras, usadas)
            # [FIX] O link achado ainda é o redirecionador de tracking do
            # Catho — resolve para o destino real antes de devolver (antes
            # essa chamada nunca acontecia, _resolver_link_real ficava sem
            # uso em lugar nenhum do arquivo).
            link = _resolver_link_real(link) if link else link
            ofertas.append({
                "cargo":       ln,
                "empresa":     prox,
                "local":       None,
                "tecnologias": _extrair_tecnologias(ln + ' ' + prox),
                "link":        link,
                "descricao":   ' '.join(linhas[i:i+4])[:300],
            })
    return ofertas


def _ofertas_jobbol(corpo: str, assunto: str, links: list, ancoras: list) -> list:
    """Jobbol = 1 vaga por e-mail. Empresa vem no assunto: '🌐 EMPRESA abriu vaga ÁREA'."""
    m = re.search(r'(?:\S+\s+)?(.+?)\s+abriu\s+vaga\s+(.+)', assunto, re.IGNORECASE)
    empresa = m.group(1).strip() if m else 'Não identificada'
    area = m.group(2).strip() if m else assunto
    cargo = area
    for ln in corpo.split('\n'):
        if 'vaga' in ln.lower() and ('home' in ln.lower() or 'híbrida' in ln.lower()
                                      or 'hibrida' in ln.lower() or 'presencial' in ln.lower()):
            cargo = ln.strip()[:120]
            break
    # Jobbol é 1 vaga por e-mail; o link de candidatura é o CTA principal
    # (ex.: "QUERO ESTA VAGA"), que aponta para /jobalert/click.php — não
    # para "/job-" (padrão que não existe nos e-mails reais do Jobbol e
    # fazia esse link cair sempre no fallback links[0], que também não
    # existia porque o link só está no href, nunca no texto do corpo).
    link_principal = next(
        (a['href'] for a in ancoras if 'jobalert/click' in a['href'] or 'quero' in a['texto'].lower()),
        links[0] if links else None
    )
    return [{
        "cargo":       cargo,
        "empresa":     empresa,
        "local":       None,
        "tecnologias": _extrair_tecnologias(corpo),
        "link":        link_principal,
        "descricao":   _resumo_descricao(corpo),
    }]


def _ofertas_infojobs(corpo: str, assunto: str, links: list, ancoras: list) -> list:
    """
    Infojobs: 'Contrata-se com urgência em <Empresa>' ou 'A <Empresa> está contratando'.

    [FIX] O link de candidatura do Infojobs só existe no href de um botão
    (ex.: "Candidatar-se", "Ver vaga"), nunca como texto puro no corpo — por
    isso usar `links` (extraído por regex do texto já limpo) sempre dava
    None. Agora usamos as âncoras do HTML, priorizando o botão de
    candidatura pelo texto visível, com `links` (fallback de e-mails só
    text/plain) como último recurso.
    """
    empresa = 'Não identificada'
    cargo = assunto
    m = re.search(r'(?:em|na|no)\s+(.+)$', assunto, re.IGNORECASE)
    if m:
        empresa = m.group(1).strip()
    m2 = re.search(r'a\s+(.+?)\s+est[áa]\s+contratando', assunto, re.IGNORECASE)
    if m2:
        empresa = m2.group(1).strip()
    m3 = re.search(r'para\s+a\s+vaga\s+([A-ZÁÉÍÓÚÂÊÔÃÕÇ][^.\n]{3,80})', corpo)
    if m3:
        cargo = m3.group(1).strip()

    link = next(
        (a['href'] for a in ancoras if any(
            p in a['texto'].lower() for p in ('candidat', 'ver vaga', 'ver a vaga', 'saiba mais')
        )),
        None,
    )
    if not link:
        link = _achar_link_por_texto(cargo, ancoras, set())
    if not link:
        link = links[0] if links else None

    return [{
        "cargo":       cargo,
        "empresa":     empresa,
        "local":       None,
        "tecnologias": _extrair_tecnologias(corpo),
        "link":        link,
        "descricao":   _resumo_descricao(corpo),
    }]


def _ofertas_generica(corpo: str, assunto: str, remetente: str, links: list, ancoras: list) -> list:
    empresa_match = re.match(r'^"?(.+?)"?\s*<', remetente)
    empresa = empresa_match.group(1).strip() if empresa_match else 'Não identificada'
    
    ofertas = []
    
    if ancoras:
        links_usados = set()
        for a in ancoras:
            texto_link = a['texto'].strip()
            href = a['href']
            texto_lower = texto_link.lower()
            
            if len(texto_link) > 3 and href not in links_usados:
                if len(texto_link.split()) <= 8 or any(p in texto_lower for p in ['vaga', 'candidat', 'apply', 'ver', 'detalh']):
                    cargo = assunto if any(p in texto_lower for p in ['vaga', 'candidat', 'apply', 'ver', 'detalh', 'aqui']) else texto_link
                    
                    ofertas.append({
                        "cargo":       cargo[:120],
                        "empresa":     empresa,
                        "local":       None,
                        "tecnologias": _extrair_tecnologias(corpo),
                        "link":        href,
                        "descricao":   _resumo_descricao(corpo),
                    })
                    links_usados.add(href)
                    
        if ofertas:
            return ofertas

    # Fallback caso não encontre âncoras válidas
    cargo = assunto.strip()
    return [{
        "cargo":       cargo,
        "empresa":     empresa,
        "local":       None,
        "tecnologias": _extrair_tecnologias(corpo),
        "link":        links[0] if links else None,
        "descricao":   _resumo_descricao(corpo),
    }]

def _extrair_ofertas(corpo: str, assunto: str, remetente: str, fonte: str, links: list, ancoras: list) -> list:
    """
    Roteador: chama o parser específico por fonte e retorna lista de ofertas.
    Cada e-mail pode gerar 1 ou N ofertas.

    `ancoras` (texto do link + href real, extraídos do HTML original) é o
    que permite casar cada vaga com o link certo quando há várias no
    mesmo e-mail; `links` é só a lista simples de URLs (derivada das
    próprias âncoras) usada pelos parsers de 1-vaga-por-e-mail.

    [FIX] Esta função estava definida duas vezes no arquivo (a segunda
    sobrescrevia a primeira silenciosamente) e o fallback abaixo chamava
    `_ofertas_generica` com um argumento a menos (faltava `ancoras`), o que
    gerava TypeError sempre que nenhum parser específico encontrava vaga —
    e como o loop inteiro de `extrair_e_agrupar_vagas` está dentro de um
    único try/except, isso interrompia a extração de TODOS os e-mails
    restantes, não só do que deu erro.
    """
    if fonte == 'indeed':
        ofertas = _ofertas_indeed(corpo, ancoras)
    elif fonte == 'catho':
        ofertas = _ofertas_catho(corpo, ancoras)
    elif fonte == 'jobbol':
        ofertas = _ofertas_jobbol(corpo, assunto, links, ancoras)
    elif fonte == 'infojobs':
        ofertas = _ofertas_infojobs(corpo, assunto, links, ancoras)
    else:
        ofertas = []

    if not ofertas:
        ofertas = _ofertas_generica(corpo, assunto, remetente, links, ancoras)
    return ofertas


# ===========================================================================
# 4) Função principal: extração + agrupamento
# ===========================================================================

def extrair_e_agrupar_vagas(service, max_resultados: int = 30, query: str = "newer_than:30d") -> dict:
    """
    Busca e-mails, detecta vagas, extrai sub-ofertas, agrupa por fonte e empresa.
    """
    resultado = {
        "total_emails_lidos":     0,
        "total_emails_com_vagas": 0,
        "total_ofertas":          0,
        "emails":                 [],
        "agrupado_por_fonte":     defaultdict(list),
        "agrupado_por_empresa":   defaultdict(list),
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

            corpo_dados = _extrair_corpo(detalhe['payload'])
            corpo       = corpo_dados["texto"]
            html_bruto  = corpo_dados["html"]
            fonte       = _detectar_fonte(remetente)

            if not _eh_vaga(assunto, corpo, fonte):
                continue

            # Fonte primária de links: âncoras do HTML original (onde o
            # link de candidatura realmente vive, no href). Só cai para o
            # regex em texto puro quando o e-mail não tem parte HTML.

            ancoras = _extrair_ancoras(html_bruto) if html_bruto else []
            links   = _extrair_links(corpo)
            ofertas = _extrair_ofertas(corpo, assunto, remetente, fonte, links, ancoras)
            
            email_obj = {
                "id":        msg['id'],
                "data":      data,
                "remetente": remetente,
                "fonte":     fonte,
                "assunto":   assunto,
                "encerrada": _vaga_encerrada(assunto, corpo),
                "ofertas":   ofertas,
            }

            resultado["emails"].append(email_obj)
            resultado["total_emails_com_vagas"] += 1
            resultado["total_ofertas"] += len(ofertas)

            for of in ofertas:
                of_resumida = {**of, "fonte": fonte, "email_id": msg['id'], "data": data}
                resultado["agrupado_por_fonte"][fonte].append(of_resumida)
                resultado["agrupado_por_empresa"][of["empresa"]].append(of_resumida)

        resultado["agrupado_por_fonte"]   = dict(resultado["agrupado_por_fonte"])
        resultado["agrupado_por_empresa"] = dict(resultado["agrupado_por_empresa"])

    except Exception as error:
        print(f"[ERRO] Falha ao se comunicar com a API do Gmail: {error}")

    return resultado


# ===========================================================================
# 5) Print bonito do retorno (apenas para visualização)
# ===========================================================================

def imprimir_resultado(dados: dict) -> None:
    print("\n" + "=" * 80)
    print("RESUMO DA EXTRAÇÃO")
    print("=" * 80)
    print(f"  E-mails lidos        : {dados['total_emails_lidos']}")
    print(f"  E-mails com vagas    : {dados['total_emails_com_vagas']}")
    print(f"  Ofertas detectadas   : {dados['total_ofertas']}")
    print(f"  Fontes encontradas   : {list(dados['agrupado_por_fonte'].keys())}")
    print(f"  Empresas encontradas : {len(dados['agrupado_por_empresa'])}")

    print("\n" + "=" * 80)
    print("E-MAILS E SUAS OFERTAS")
    print("=" * 80)
    for i, em in enumerate(dados['emails'], 1):
        marca = ' [ENCERRADA]' if em['encerrada'] else ''
        print(f"\n[E-mail {i:02d}] {em['assunto']}{marca}")
        print(f"   Fonte: {em['fonte']}  |  Data: {em['data'][:25]}  |  De: {em['remetente']}")
        print(f"   {len(em['ofertas'])} oferta(s):")
        for j, of in enumerate(em['ofertas'], 1):
            print(f"     {j:02d}. {of['cargo']}")
            print(f"         Empresa    : {of['empresa']}")
            if of.get('local'):
                print(f"         Local      : {of['local']}")
            if of.get('tecnologias'):
                print(f"         Tecnologias: {', '.join(of['tecnologias'])}")
            if of.get('link'):
                print(f"         Link       : {of['link']}")
            print(f"         Descrição  : {of['descricao'][:160]}{'...' if len(of['descricao']) > 160 else ''}")

    print("\n" + "=" * 80)
    print("AGRUPAMENTO POR FONTE (plataforma)")
    print("=" * 80)
    for fonte, lista in dados['agrupado_por_fonte'].items():
        print(f"\n  >> {fonte.upper()} ({len(lista)} oferta(s))")
        for of in lista:
            print(f"     - {of['cargo']} @ {of['empresa']}")

    print("\n" + "=" * 80)
    print("AGRUPAMENTO POR EMPRESA")
    print("=" * 80)
    for empresa, lista in dados['agrupado_por_empresa'].items():
        print(f"\n  >> {empresa} ({len(lista)} oferta(s))")
        for of in lista:
            print(f"     - {of['cargo']}  [{of['fonte']}]")

    print("\n" + "=" * 80)
    print("JSON BRUTO (o que a função retorna)")
    print("=" * 80)
    print(json.dumps(dados, ensure_ascii=False, indent=2, default=str))


# ===========================================================================
# 6) Execução
# ===========================================================================

if __name__ == '__main__':
    servico = autenticar_gmail()
    dados = extrair_e_agrupar_vagas(
        servico,
        max_resultados=30,
        query="newer_than:30d",
    )
    imprimir_resultado(dados)