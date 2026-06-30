"""
gemini_service.py — AutoApply AI
==================================
Módulo central de inteligência artificial do pipeline.
Toda chamada ao Gemini passa por aqui — main.py não chama a API diretamente.

Funções exportadas:
  classificar_email(assunto, remetente, corpo_preview)
      → Etapa 1 barata: classifica o e-mail sem gastar tokens com análise completa
      → Retorna: "vaga" | "certificado" | "alerta" | "agendamento" | "financeiro" | "servico" | "outro"

  extrair_vagas(assunto, remetente, corpo)
      → Etapa 2: extrai N vagas de um único e-mail (Catho/Indeed enviam listas)
      → Retorna: list[dict]  — cada dict é uma vaga estruturada

  analisar_acesso(remetente, assunto, corpo)
      → OTPs, links de recuperação de senha, alertas de novo login
      → Retorna: dict

  classificar_email_geral(remetente, assunto, corpo)
      → Para e-mails de agendamento/financeiro: extrai data, valor, link
      → Retorna: dict

Todas as funções:
  - Fazem retry automático com o retryDelay que a própria API devolve (429/503)
  - Retornam {} ou [] em falha — nunca levantam exceção para o chamador
  - Funcionam com google.genai (novo) e google.generativeai (fallback)
"""

import os
import re
import json
import time
import logging

from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Inicialização do cliente (suporta os dois SDKs)
# ---------------------------------------------------------------------------
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
MODELO         = "gemini-2.5-flash"

_client     = None
_GENAI_NEW  = False

try:
    from google import genai as _google_genai
    from google.genai import types as _genai_types
    _GENAI_NEW = True
except ImportError:
    try:
        import google.generativeai as _genai_legacy
    except ImportError:
        pass


def _get_client():
    global _client
    if _client is not None:
        return _client
    if not GEMINI_API_KEY:
        raise ValueError("GEMINI_API_KEY não encontrada no .env")
    if _GENAI_NEW:
        _client = _google_genai.Client(api_key=GEMINI_API_KEY)
    else:
        _genai_legacy.configure(api_key=GEMINI_API_KEY)
        _client = _genai_legacy.GenerativeModel(MODELO)
    return _client


# ---------------------------------------------------------------------------
# Núcleo: chamada com retry automático
# ---------------------------------------------------------------------------

def _extrair_retry_delay(exc: Exception) -> float:
    """Lê o retryDelay do corpo do erro 429/503 da API."""
    txt = str(exc)
    m = re.search(r"'retryDelay':\s*'([0-9.]+)(ms|s)'", txt)
    if m:
        v, u = float(m.group(1)), m.group(2)
        return (v / 1000.0) if u == "ms" else v
    m2 = re.search(r"retry in ([0-9.]+)s", txt)
    if m2:
        return float(m2.group(1))
    return 60.0


def _chamar_gemini(prompt: str, max_tentativas: int = 3) -> str:
    """
    Chama o Gemini com retry automático em 429/503.
    Sempre pede resposta em JSON (application/json).
    Raises a última exceção se todas as tentativas falharem.
    """
    client = _get_client()
    ultima_exc = None

    for tentativa in range(1, max_tentativas + 1):
        try:
            if _GENAI_NEW:
                resp = client.models.generate_content(
                    model=MODELO,
                    contents=prompt,
                    config=_genai_types.GenerateContentConfig(
                        response_mime_type="application/json",
                        temperature=0.1,
                    ),
                )
            else:
                resp = client.generate_content(
                    prompt,
                    generation_config=_genai_legacy.GenerationConfig(
                        response_mime_type="application/json",
                        temperature=0.1,
                    ),
                )
            return resp.text

        except Exception as exc:
            ultima_exc = exc
            codigo = str(exc)

            # Só faz retry em rate-limit e sobrecarga temporária
            if "429" not in codigo and "503" not in codigo:
                raise

            if tentativa == max_tentativas:
                break

            espera = _extrair_retry_delay(exc)
            logger.warning(
                f"Gemini {'429' if '429' in codigo else '503'} — "
                f"aguardando {espera:.1f}s antes da tentativa {tentativa+1}/{max_tentativas}..."
            )
            time.sleep(espera + 1)

    raise ultima_exc


def _parse_json(raw: str) -> dict | list:
    """Converte texto bruto do Gemini em dict ou list de forma segura."""
    if not raw:
        return {}
    # Remove possíveis backticks de markdown
    raw = re.sub(r"```(?:json)?", "", raw).strip(" `\n")
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        logger.warning(f"Gemini JSON inválido: {raw[:100]}")
        return {}


# ---------------------------------------------------------------------------
# 1. CLASSIFICAR E-MAIL  (etapa barata — decide o destino)
# ---------------------------------------------------------------------------

# Mapeamento heurístico (sem gastar tokens do Gemini)
_FONTES_VAGAS = {
    "vaga": [
        "jobbol", "indeed", "linkedin", "catho", "infojobs",
        "vagas.com", "gupy", "glassdoor", "trampos", "remotar",
        "programathor", "pandape", "micro1", "inhire", "trabalha",
        "recrut", "greenhouse", "lever", "workable",
    ],
    "certificado": ["dio.me", "udemy", "coursera", "alura", "rocketseat",
                    "digitalhouse", "digitalinnovation", "linkedin.com/learning"],
    "alerta":      ["accounts.google", "security-noreply", "noreply@google",
                    "cinemark", "spotify", "ticket360", "noreply@apple"],
    "financeiro":  ["google play", "googleplay", "nfe", "nota fiscal",
                    "fatura", "boleto", "pagamento", "nubank", "itau",
                    "bradesco", "santander", "mercadopago"],
    "agendamento": ["booksy", "doctoralia", "calendly", "scheduling",
                    "confirmação de agendamento", "seu agendamento"],
}

_PALAVRAS_VAGA_ASSUNTO = [
    "vaga", "oportunidade", "contratando", "hiring", "job", "emprego",
    "desenvolvedor", "developer", "analista", "engenheiro", "engineer",
    "processo seletivo", "candidatura", "trampolim", "vagas para",
]

_PALAVRAS_NAO_VAGA = [
    "curso", "bootcamp", "treinamento", "mentoria", "webinar",
    "desconto", "promoção", "oferta", "newsletter", "comunidade",
    "você ganhou", "certificado chegou", "ganhou certificado",
]


def _classificar_heuristica(assunto: str, remetente: str, corpo_preview: str) -> str | None:
    """
    Classificação rápida sem Gemini.
    Retorna categoria ou None (quando inconclusivo → chama Gemini).
    """
    rem = remetente.lower()
    ass = assunto.lower()
    txt = ass + " " + corpo_preview[:300].lower()

    for categoria, padroes in _FONTES_VAGAS.items():
        if any(p in rem or p in ass for p in padroes):
            return categoria

    # E-mail de plataforma de vagas → sempre vaga
    if any(p in txt for p in _PALAVRAS_VAGA_ASSUNTO):
        # Mas verifica exclusões primeiro
        if any(p in txt for p in _PALAVRAS_NAO_VAGA):
            return "servico"
        return "vaga"

    return None   # inconclusivo


def classificar_email(assunto: str, remetente: str, corpo_preview: str) -> str:
    """
    Etapa 1: classifica o e-mail para roteamento.
    Usa heurística primeiro; chama Gemini só quando inconclusivo.

    Retorna uma das categorias:
      "vaga" | "certificado" | "alerta" | "agendamento" | "financeiro" | "servico" | "outro"
    """
    # Tenta heurística primeiro (sem custo de token)
    resultado = _classificar_heuristica(assunto, remetente, corpo_preview)
    if resultado:
        return resultado

    # Inconclusivo → Gemini decide
    prompt = f"""
Você é um classificador de e-mails.
Classifique o e-mail abaixo em UMA das categorias:
  vaga        — oferta de emprego real
  certificado — notificação de conclusão de curso / entrega de certificado
  alerta      — código OTP, alerta de login, recuperação de senha
  agendamento — confirmação de horário, reserva, consulta médica
  financeiro  — fatura, boleto, comprovante, NF, assinatura cobrada
  servico     — newsletter, promoção, atualização de app, e-mail de marketing
  outro       — qualquer outro tipo

Responda APENAS em JSON estrito (sem crases):
{{"categoria": "<uma das 7 acima>", "justificativa": "1 frase curta"}}

Remetente: {remetente}
Assunto: {assunto}
Prévia: {corpo_preview[:400]}
"""
    try:
        raw = _chamar_gemini(prompt)
        data = _parse_json(raw)
        if isinstance(data, dict):
            cat = data.get("categoria", "outro").lower().strip()
            # Valida que é uma categoria conhecida
            validas = {"vaga","certificado","alerta","agendamento","financeiro","servico","outro"}
            return cat if cat in validas else "outro"
        return "outro"
    except Exception as e:
        logger.warning(f"classificar_email falhou: {e} — assumindo 'outro'")
        return "outro"


# ---------------------------------------------------------------------------
# 2. EXTRAIR VAGAS  (suporta múltiplas vagas por e-mail)
# ---------------------------------------------------------------------------

def extrair_vagas(assunto: str, remetente: str, corpo: str,
                  links_html: list[dict] | None = None) -> list[dict]:
    """
    Extrai todas as vagas contidas em um único e-mail.

    Suporta:
    - E-mails com 1 vaga (resposta da empresa, Gupy, pandape)
    - E-mails com N vagas (digest da Catho, Indeed, LinkedIn, Infojobs)

    links_html: lista de dicts {"url", "texto", "score"} extraída do HTML bruto.
      Quando fornecida, o prompt instrui o Gemini a associar cada vaga ao seu link.

    Retorna lista de dicts com a estrutura abaixo. Retorna [] em caso de falha.
    """
    # Prepara bloco de links para injetar no prompt
    bloco_links = ""
    if links_html:
        linhas = []
        for item in links_html[:20]:   # máx 20 links para não inflar o prompt
            linhas.append(f'  "{item["texto"][:60]}" → {item["url"][:100]}')
        bloco_links = (
            "\n\nLINKS IDENTIFICADOS NO E-MAIL (texto → URL):\n"
            + "\n".join(linhas)
            + "\n\nIMPORTANTE: use esses links para preencher link_candidatura de cada vaga."
            + " Associe cada vaga ao link cujo texto corresponde ao cargo."
            + " Ignore links de 'cancelar', 'unsubscribe', 'descadastrar'."
        )

    prompt = f"""
Você é um assistente de RH especializado em tecnologia.

O e-mail abaixo pode conter UMA ou VÁRIAS vagas de emprego.
Extraia TODAS as vagas presentes e retorne uma lista JSON.

REGRAS:
- Digest (Catho, Indeed, LinkedIn com múltiplas vagas) → extraia cada vaga individualmente.
- "sua candidatura foi enviada" → tipo_email = "candidatura_enviada".
- "você avançou de fase" → tipo_email = "avanco_fase".
- "retorno do processo seletivo" → tipo_email = "retorno".
- score_match: 0-100 baseado em Python, automação, dados, IA, APIs. Vagas sem tech → score 30.
- link_candidatura: use os links fornecidos abaixo, associando pelo texto do cargo.
  NÃO use links de cancelar inscrição, unsubscribe ou descadastrar.{bloco_links}

Responda APENAS com uma lista JSON (sem crases, sem texto fora da lista):
[
  {{
    "cargo": "string",
    "empresa": "string ou null",
    "local": "cidade/estado ou null",
    "modelo_trabalho": "Remoto | Híbrido | Presencial | Não informado",
    "salario": "string com valor (ex: R$ 4.380,00) ou null",
    "requisitos": ["lista de requisitos ou []"],
    "tecnologias": ["lista de techs identificadas ou []"],
    "score_match": 0,
    "link_candidatura": "URL exata da lista de links acima, ou null",
    "tipo_email": "candidatura_enviada | nova_vaga | retorno | avanco_fase | outro",
    "status_candidatura": "enviada | avanco | reprovado | nova | null"
  }}
]

Remetente: {remetente}
Assunto: {assunto}
Corpo do e-mail:
{corpo[:3500]}
"""
    try:
        raw = _chamar_gemini(prompt)
        data = _parse_json(raw)

        # A resposta pode ser lista diretamente ou dict com chave "vagas"
        if isinstance(data, list):
            vagas = data
        elif isinstance(data, dict):
            # Às vezes o modelo envolve em {"vagas": [...]}
            vagas = data.get("vagas") or data.get("results") or [data]
        else:
            vagas = []

        # Garante que cada item é dict e tem os campos mínimos
        resultado = []
        for item in vagas:
            if not isinstance(item, dict):
                continue
            # Normaliza campos obrigatórios
            item.setdefault("cargo",              assunto[:80])
            item.setdefault("empresa",            None)
            item.setdefault("local",              None)
            item.setdefault("modelo_trabalho",    "Não informado")
            item.setdefault("salario",            None)
            item.setdefault("requisitos",         [])
            item.setdefault("tecnologias",        [])
            item.setdefault("score_match",        0)
            item.setdefault("link_candidatura",   None)
            item.setdefault("tipo_email",         "nova_vaga")
            item.setdefault("status_candidatura", "nova")

            # Garante tipos
            if not isinstance(item["requisitos"],  list): item["requisitos"]  = []
            if not isinstance(item["tecnologias"], list): item["tecnologias"] = []
            if not isinstance(item["score_match"], int):
                try:    item["score_match"] = int(item["score_match"])
                except: item["score_match"] = 0

            resultado.append(item)

        return resultado

    except Exception as e:
        logger.warning(f"extrair_vagas falhou: {e}")
        return []


# ---------------------------------------------------------------------------
# 3. ANALISAR ACESSO (OTP / recuperação de senha)
# ---------------------------------------------------------------------------

def analisar_acesso(remetente: str, assunto: str, corpo: str) -> dict:
    """
    Identifica e extrai código OTP, link de recuperação ou alerta de novo login.

    Retorna:
    {
      "eh_email_de_acesso": bool,
      "plataforma":         str,
      "tipo_solicitacao":   "otp | recuperacao_senha | alerta_novo_login | irrelevante",
      "codigo_ou_link":     str | null,
      "justificativa_curta": str
    }
    """
    prompt = f"""
Você é um assistente de segurança digital.
Analise o e-mail e responda APENAS em JSON estrito (sem crases):
{{
  "eh_email_de_acesso": true/false,
  "plataforma": "nome amigável da empresa (ex: Spotify, Google, Cinemark)",
  "tipo_solicitacao": "otp | recuperacao_senha | alerta_novo_login | irrelevante",
  "codigo_ou_link": "código numérico de 4-8 dígitos OU URL de ação, ou null",
  "justificativa_curta": "1 frase explicando o que o e-mail pede"
}}

Regras:
- Códigos de desconto ou cupons NÃO são OTP → tipo_solicitacao = "irrelevante"
- Só marque eh_email_de_acesso = true se for claramente login/segurança

Remetente: {remetente}
Assunto: {assunto}
Corpo: {corpo[:800]}
"""
    try:
        raw = _chamar_gemini(prompt)
        data = _parse_json(raw)
        return data if isinstance(data, dict) else {}
    except Exception as e:
        logger.warning(f"analisar_acesso falhou: {e}")
        return {}


# ---------------------------------------------------------------------------
# 4. CLASSIFICAR E-MAIL GERAL (agendamentos e financeiros)
# ---------------------------------------------------------------------------

def classificar_email_geral(remetente: str, assunto: str, corpo: str) -> dict:
    """
    Extrai informações relevantes de e-mails gerais (agendamentos, financeiros).

    Retorna:
    {
      "categoria":       "agendamento | financeiro | servico | outro",
      "resumo":          "1 frase",
      "remetente_nome":  str,
      "data_evento":     str | null,
      "valor":           str | null,
      "link_principal":  str | null,
      "requer_acao":     bool
    }
    """
    prompt = f"""
Você é um assistente pessoal que organiza e-mails.
Analise o e-mail e responda APENAS em JSON estrito (sem crases):
{{
  "categoria": "agendamento | financeiro | servico | outro",
  "resumo": "1 frase curta sobre o que o e-mail diz",
  "remetente_nome": "nome amigável (ex: Barbearia Hermanos, Google Play)",
  "data_evento": "data/hora do agendamento como string, ou null",
  "valor": "valor monetário como string (ex: R$ 49,90), ou null",
  "link_principal": "URL mais importante do e-mail, ou null",
  "requer_acao": true/false
}}

Remetente: {remetente}
Assunto: {assunto}
Corpo: {corpo[:800]}
"""
    try:
        raw = _chamar_gemini(prompt)
        data = _parse_json(raw)
        return data if isinstance(data, dict) else {}
    except Exception as e:
        logger.warning(f"classificar_email_geral falhou: {e}")
        return {}


# ---------------------------------------------------------------------------
# Uso standalone — testa todas as funções com dados reais
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")

    print("\n" + "="*60)
    print("  gemini_service.py — teste standalone")
    print("="*60)

    # ── Teste 1: classificar e-mail de vaga Catho ────────────────────────────
    print("\n[1] classificar_email — digest Catho")
    cat = classificar_email(
        assunto="Seu perfil se encaixa com essas 10 vagas para Programador Python",
        remetente="Vagas de Emprego na Catho <avisovagas@catho.com.br>",
        corpo_preview="Você tem 16 novas vagas de Programador Python. Trabalhe de Casa Desenvolvedor Python BAIRESDEV...",
    )
    print(f"  Categoria: {cat}")

    # ── Teste 2: extrair vagas do digest Catho ───────────────────────────────
    print("\n[2] extrair_vagas — digest com múltiplas vagas")
    corpo_catho = """
    Você tem 16 novas vagas de Programador Python.
    
    Trabalhe de Casa Desenvolvedor Python
    BAIRESDEV | São Paulo | Salário: A Combinar
    Link: https://www.catho.com.br/vagas/desenvolvedor-python-bairesdev
    
    Desenvolvedor Python e Inteligência Artificial Junior
    G.V.R. SERVICOS TEMPORARIOS LTDA | São Paulo | Salário: A Combinar
    Link: https://www.catho.com.br/vagas/dev-python-ia-gvr
    
    Analista de Dados Python Jr
    TechCorp | São Paulo | Salário: R$ 4.000 - R$ 6.000
    Link: https://www.catho.com.br/vagas/analista-dados-python-techcorp
    """
    vagas = extrair_vagas(
        assunto="Seu perfil se encaixa com essas 10 vagas para Programador Python",
        remetente="avisovagas@catho.com.br",
        corpo=corpo_catho,
    )
    print(f"  Vagas extraídas: {len(vagas)}")
    for i, v in enumerate(vagas, 1):
        print(f"  [{i}] {v.get('cargo')} @ {v.get('empresa')} — score {v.get('score_match')}%")
        if v.get("link_candidatura"):
            print(f"       → {v['link_candidatura']}")

    # ── Teste 3: e-mail de agendamento ───────────────────────────────────────
    print("\n[3] classificar_email_geral — agendamento barbearia")
    geral = classificar_email_geral(
        remetente="Barbearia Hermanos <noreply@booksy.com>",
        assunto="Seu agendamento foi confirmado",
        corpo="Olá Lucas! Seu horário na Barbearia Hermanos Freguesia do Ó está confirmado para 28/06 às 14h. Endereço: Rua Tal, 123.",
    )
    print(f"  Categoria : {geral.get('categoria')}")
    print(f"  Resumo    : {geral.get('resumo')}")
    print(f"  Data      : {geral.get('data_evento')}")

    # ── Teste 4: candidatura enviada no Infojobs ─────────────────────────────
    print("\n[4] extrair_vagas — candidatura enviada pelo Infojobs")
    vagas2 = extrair_vagas(
        assunto="Sua candidatura foi enviada para a empresa Minsait Brasil",
        remetente="Infojobs <noreply@infojobs.com.br>",
        corpo="Sua candidatura para a vaga de Analista de TI Jr foi enviada com sucesso para Minsait Brasil. Acompanhe o status em infojobs.com.br",
    )
    print(f"  Vagas extraídas: {len(vagas2)}")
    for v in vagas2:
        print(f"  {v.get('tipo_email')} — {v.get('cargo')} @ {v.get('empresa')}")

    print("\n" + "="*60)
    print("  Testes concluídos")
    print("="*60 + "\n")
