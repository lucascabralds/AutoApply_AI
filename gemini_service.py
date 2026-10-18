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

import re
import json
import logging

from dotenv import load_dotenv

from rota_ia_client import chamar_ia as _chamar_rota_ia

load_dotenv()

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Todas as chamadas de IA passam pelo gateway local Rota.IA
# (http://localhost:8787/v1/chat/completions), que decide por trás qual
# provedor cadastrado (Gemini, OpenAI, Claude, Groq, DeepSeek, etc.) atende
# e troca sozinho de provedor se algum falhar. Ver rota_ia_client.py.
# ---------------------------------------------------------------------------

# Status da última função pública deste módulo — o pipeline usa para decidir
# se cobra budget e se pode cachear o resultado:
#   "heuristica" → respondeu sem chamar a IA (nada a cobrar)
#   "ok"         → a IA respondeu e a resposta foi lida
#   "falha"      → gateway fora/erro/JSON irrecuperável (não cachear, tentar depois)
# As funções continuam sem levantar exceção para os scripts antigos.
_status = "heuristica"


def status_ultima_chamada() -> str:
    return _status


def _definir_status(novo: str) -> None:
    global _status
    _status = novo


def _chamar_gemini(prompt: str, max_tentativas: int = 3, max_tokens: int | None = None) -> str:
    """
    Envia o prompt para o gateway Rota.IA com retry automático em 429/503.
    Mantém o nome original da função por compatibilidade com o resto do módulo.
    Raises a última exceção se todas as tentativas falharem.
    """
    _definir_status("falha")   # vira "ok" só se a resposta chegar
    resposta = _chamar_rota_ia(prompt, max_tentativas=max_tentativas, max_tokens=max_tokens)
    _definir_status("ok")
    return resposta


# ---------------------------------------------------------------------------
# [FIX] Parse robusto de JSON vindo da IA.
#
# O erro de parse acontecia por dois motivos: (1) a IA envolve a resposta
# em cercas de markdown ou texto solto ao redor ("Aqui está:"), e (2) a
# resposta é cortada no meio pelo limite de tokens, deixando chaves/
# colchetes sem fechar. Antes, qualquer um dos dois derrubava o
# json.loads e a vaga/e-mail inteiro era descartado.
# ---------------------------------------------------------------------------

INSTRUCAO_JSON_PURO = (
    "\n\nFORMATO DE RESPOSTA — MUITO IMPORTANTE:\n"
    "Responda em RAW JSON — comece a resposta direto com { ou [ e termine "
    "com } ou ]. NÃO use crases (```), NÃO use ```json, NÃO escreva nenhum "
    "texto antes ou depois do JSON (sem \"aqui está\", sem explicações). "
    "Se a lista for longa, prefira encurtar textos (ex.: descrição/resumo) "
    "a deixar o JSON incompleto — a resposta precisa terminar fechada."
)


def _tentar_reparar_json_truncado(texto: str):
    """
    Recupera dados de um JSON cortado no meio (resposta truncada pelo
    limite de tokens): acha pontos onde a estrutura ainda estava
    balanceada, fecha o que ficou pendente e tenta json.loads — dos
    cortes mais tardios (menor perda de dados) para os mais cedo.
    """
    if not texto or texto[0] not in "{[":
        return None

    posicoes_virgula = []
    dentro_string = False
    escape = False
    for i, ch in enumerate(texto):
        if escape:
            escape = False
            continue
        if ch == "\\" and dentro_string:
            escape = True
            continue
        if ch == '"':
            dentro_string = not dentro_string
            continue
        if not dentro_string and ch == ",":
            posicoes_virgula.append(i)

    def _pilha_ate(pos: int) -> list:
        pilha = []
        dentro = False
        esc = False
        for ch in texto[:pos]:
            if esc:
                esc = False
                continue
            if ch == "\\" and dentro:
                esc = True
                continue
            if ch == '"':
                dentro = not dentro
                continue
            if dentro:
                continue
            if ch in "{[":
                pilha.append(ch)
            elif ch in "}]" and pilha:
                pilha.pop()
        return pilha

    fechamentos = {"{": "}", "[": "]"}
    for corte in reversed(posicoes_virgula):
        pilha = _pilha_ate(corte)
        if not pilha:
            continue
        sufixo = "".join(fechamentos[c] for c in reversed(pilha))
        try:
            return json.loads(texto[:corte] + sufixo)
        except json.JSONDecodeError:
            continue
    return None


def _parse_json(raw: str) -> dict | list:
    """
    Converte texto bruto do Gemini em dict ou list de forma segura.
    Resposta vazia ou irrecuperável marca o status como "falha".
    """
    if not raw:
        _definir_status("falha")
        return {}

    texto = raw.strip()
    # Remove cercas de markdown (```json ... ``` ou ``` ... ```)
    texto = re.sub(r"```(?:json)?\s*", "", texto, flags=re.IGNORECASE)
    texto = texto.replace("```", "").strip()

    # Isola do primeiro '{'/'[' ao último '}'/']' — descarta qualquer
    # texto explicativo que a IA tenha colocado antes/depois do JSON.
    inicio = next((i for i, c in enumerate(texto) if c in "{["), None)
    cauda = texto[inicio:] if inicio is not None else texto
    if inicio is not None:
        fecha = "}" if texto[inicio] == "{" else "]"
        fim = texto.rfind(fecha)
        if fim != -1 and fim > inicio:
            texto = texto[inicio:fim + 1]

    def _sem_virgula_solta(t: str) -> str:
        # Remove vírgulas soltas antes de } ou ] (erro comum do modelo)
        return re.sub(r",\s*([}\]])", r"\1", t)

    for candidato in (texto, _sem_virgula_solta(texto)):
        try:
            return json.loads(candidato)
        except json.JSONDecodeError:
            pass

    # Última tentativa: resposta truncada — recupera o que der. O reparo usa
    # a cauda INTEIRA desde o primeiro '['/'{': numa resposta cortada, o
    # "último ]" do recorte acima é o fim de uma lista interna (ex.:
    # tecnologias da 1ª vaga) e reparar só até ali perdia o resto da vaga.
    for candidato in (_sem_virgula_solta(cauda), _sem_virgula_solta(texto)):
        reparado = _tentar_reparar_json_truncado(candidato)
        if reparado is not None:
            logger.warning("JSON da IA veio truncado — dados parciais recuperados")
            return reparado

    logger.warning(f"Gemini JSON inválido (irrecuperável): {raw[:100]}")
    _definir_status("falha")
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
    # [FIX] Oportunidades de aprendizado de bancos/instituições (ex.: Info
    # Santander / Santander Universidades) — são comunicados sobre cursos
    # e capacitação, não vagas de emprego. Sem isso, a heurística deste
    # arquivo (que roda antes do prompt do Gemini) classificava esses
    # e-mails como "vaga" por conterem palavras como "oportunidade".
    "santander universidades", "info santander", "trilha de carreira",
    "trilha de aprendizado", "capacitação gratuita", "bolsa de estudo",
    "inscreva-se no curso",
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
    _definir_status("heuristica")
    # Tenta heurística primeiro (sem custo de token)
    resultado = _classificar_heuristica(assunto, remetente, corpo_preview)
    if resultado:
        return resultado

    # Inconclusivo → Gemini decide
    prompt = f"""
Você é um classificador de e-mails.
Classifique o e-mail abaixo em UMA das categorias:
  vaga        — oferta de emprego real, com cargo/empresa contratando
  certificado — notificação de conclusão de curso / entrega de certificado
  alerta      — código OTP, alerta de login, recuperação de senha
  agendamento — confirmação de horário, reserva, consulta médica
  financeiro  — fatura, boleto, comprovante, NF, assinatura cobrada
  servico     — newsletter, promoção, atualização de app, e-mail de marketing
  outro       — qualquer outro tipo

IMPORTANTE — não confunda oportunidade de APRENDIZADO com oportunidade de
EMPREGO: e-mails de bancos/instituições oferecendo cursos, trilhas de
capacitação, workshops ou bolsas de estudo (ex.: "Info Santander",
"Santander Universidades") NÃO são vaga, mesmo que usem palavras como
"oportunidade" — classifique como "servico" (serão tratados como
Comunicado no restante do pipeline).

Responda APENAS em JSON estrito (sem crases):
{{"categoria": "<uma das 7 acima>", "justificativa": "1 frase curta"}}

Remetente: {remetente}
Assunto: {assunto}
Prévia: {corpo_preview[:400]}
{INSTRUCAO_JSON_PURO}
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
        _definir_status("falha")
        return "outro"


# ---------------------------------------------------------------------------
# 2. EXTRAIR VAGAS  (suporta múltiplas vagas por e-mail)
# ---------------------------------------------------------------------------

# Máximo de vagas pedidas por e-mail: digests como "Melhores vagas da semana"
# trazem centenas; pedir todas estourava o limite de resposta e o JSON vinha
# cortado. As primeiras do e-mail são as que a plataforma destacou.
MAX_VAGAS_POR_EMAIL = 10
MAX_TOKENS_VAGAS    = 4096

# Termos do perfil do Lucas (Python / dados / automação) para o score de
# reserva quando a IA não chega a informar o score (resposta cortada).
_TERMOS_PERFIL = {
    "python": 30, "dados": 20, "data": 20, "sql": 15, "power bi": 15, "powerbi": 15,
    "automação": 15, "automacao": 15, "rpa": 10, "etl": 10, "api": 10, "fastapi": 10,
    "django": 10, "flask": 10, "bi": 10, "analista": 5, "desenvolvedor": 5,
    "developer": 5, "backend": 10, "excel": 5, "vba": 5, "ia": 10, "machine learning": 10,
}


def _score_reserva(cargo: str, tecnologias: list) -> int:
    """
    Score aproximado (0-100) a partir do cargo e das tecnologias, usado só
    quando a IA não devolveu score_match — antes essas vagas saíam com 0%
    e sumiam do topo do briefing mesmo sendo de Python/dados.
    """
    texto = " ".join([cargo or ""] + [str(t) for t in tecnologias or []]).lower()
    pontos = sum(peso for termo, peso in _TERMOS_PERFIL.items()
                 if re.search(r"(?<!\w)" + re.escape(termo) + r"(?!\w)", texto))
    return max(20, min(90, pontos))


def _prompt_vagas(assunto: str, remetente: str, corpo: str, links: list[dict]) -> str:
    bloco_links = ""
    if links:
        linhas = [f'  [{i}] {item["texto"][:70]}' for i, item in enumerate(links)]
        bloco_links = (
            "\nLINKS DO E-MAIL (índice e texto do link):\n" + "\n".join(linhas) + "\n"
            "Em \"link\", coloque o ÍNDICE do link cujo texto corresponde à vaga "
            "(ou null). Nunca escreva a URL.\n"
        )
    return f"""Extraia as vagas de emprego deste e-mail.

Candidato: júnior em Python, dados, SQL/Power BI, automação e APIs.

REGRAS:
- Digest com várias vagas → um item por vaga, no máximo {MAX_VAGAS_POR_EMAIL} (as primeiras do e-mail).
- Sem nenhuma vaga de emprego real (curso, promoção, newsletter) → [].
- tipo: "nova_vaga" | "candidatura_enviada" | "avanco_fase" | "retorno" | "outro".
- score: 0-100 de aderência ao candidato (Python/dados/automação = alto; sem relação = baixo).
- requisitos: no máximo 4 itens curtos; tecnologias: no máximo 6.
- Textos curtos. Responda SÓ o JSON, sem explicações nem raciocínio.
{bloco_links}
Formato (lista JSON):
[{{"cargo": "...", "empresa": null, "local": null, "modelo": "Remoto|Híbrido|Presencial|Não informado", "salario": null, "requisitos": [], "tecnologias": [], "score": 0, "link": null, "tipo": "nova_vaga", "status": null}}]

Remetente: {remetente}
Assunto: {assunto}
Corpo:
{corpo[:3500]}
{INSTRUCAO_JSON_PURO}
"""


def _normalizar_vaga(item: dict, assunto: str, links: list[dict]) -> dict | None:
    """Converte o formato enxuto da resposta para o formato usado pelo pipeline."""
    cargo = item.get("cargo")
    if not cargo:
        return None

    link = item.get("link", item.get("link_candidatura"))
    if isinstance(link, bool):
        link = None
    if isinstance(link, (int, float)) or (isinstance(link, str) and link.strip().isdigit()):
        i = int(link)
        link = links[i]["url"] if 0 <= i < len(links) else None
    elif not (isinstance(link, str) and link.startswith(("http://", "https://"))):
        link = None

    tecnologias = item.get("tecnologias") if isinstance(item.get("tecnologias"), list) else []
    requisitos = item.get("requisitos") if isinstance(item.get("requisitos"), list) else []
    score = item.get("score", item.get("score_match"))
    try:
        score = max(0, min(100, int(score)))
    except (TypeError, ValueError):
        score = _score_reserva(cargo, tecnologias)

    return {
        "cargo":              str(cargo)[:120],
        "empresa":            item.get("empresa"),
        "local":              item.get("local"),
        "modelo_trabalho":    item.get("modelo") or item.get("modelo_trabalho") or "Não informado",
        "salario":            item.get("salario"),
        "requisitos":         requisitos,
        "tecnologias":        tecnologias,
        "score_match":        score,
        "link_candidatura":   link,
        "tipo_email":         item.get("tipo") or item.get("tipo_email") or "nova_vaga",
        "status_candidatura": item.get("status") or item.get("status_candidatura") or "nova",
    }


def extrair_vagas(assunto: str, remetente: str, corpo: str,
                  links_html: list[dict] | None = None) -> list[dict]:
    """
    Extrai as vagas contidas em um único e-mail (1 vaga ou digest com N).

    links_html: lista de dicts {"texto", "url"} extraída do HTML bruto. A IA
      responde o ÍNDICE do link de cada vaga e a URL é preenchida aqui — sem
      repetir URLs longas na resposta (que estourava o limite) e sem a IA
      poder inventar link.

    Retorna lista de dicts no formato do pipeline (cargo, empresa, local,
    modelo_trabalho, salario, requisitos, tecnologias, score_match,
    link_candidatura, tipo_email, status_candidatura). [] em caso de falha.
    """
    _definir_status("heuristica")
    links = (links_html or [])[:25]
    prompt = _prompt_vagas(assunto, remetente, corpo, links)
    try:
        raw = _chamar_gemini(prompt, max_tokens=MAX_TOKENS_VAGAS)
        data = _parse_json(raw)
        if _status == "falha" and raw and raw.strip():
            # Alguns modelos respondem com raciocínio em texto em vez de JSON —
            # uma segunda chance, curta e direta, costuma resolver.
            raw = _chamar_gemini(
                prompt + "\n\nATENÇÃO: sua resposta anterior não era JSON. "
                         "Responda agora APENAS a lista JSON, começando com [",
                max_tentativas=1, max_tokens=MAX_TOKENS_VAGAS,
            )
            data = _parse_json(raw)
        if _status == "falha":
            return []   # sem isto, o {} irrecuperável virava [{}] → uma vaga fictícia

        # A resposta pode ser lista diretamente ou dict com chave "vagas"
        if isinstance(data, list):
            vagas = data
        elif isinstance(data, dict):
            # Às vezes o modelo envolve em {"vagas": [...]} — e {"vagas": []}
            # é uma resposta válida de "nenhuma vaga", não uma vaga vazia.
            if "vagas" in data or "results" in data:
                vagas = data.get("vagas", data.get("results")) or []
            elif data.get("cargo"):
                vagas = [data]   # uma vaga só, sem lista em volta
            else:
                vagas = []
        else:
            vagas = []

        resultado = []
        for item in vagas[:MAX_VAGAS_POR_EMAIL]:
            if isinstance(item, dict):
                vaga = _normalizar_vaga(item, assunto, links)
                if vaga:
                    resultado.append(vaga)
        return resultado

    except Exception as e:
        logger.warning(f"extrair_vagas falhou: {e}")
        _definir_status("falha")
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
    _definir_status("heuristica")
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
{INSTRUCAO_JSON_PURO}
"""
    try:
        raw = _chamar_gemini(prompt)
        data = _parse_json(raw)
        return data if isinstance(data, dict) else {}
    except Exception as e:
        logger.warning(f"analisar_acesso falhou: {e}")
        _definir_status("falha")
        return {}


# ---------------------------------------------------------------------------
# 4. CLASSIFICAR E-MAIL GERAL (agendamentos e financeiros)
# ---------------------------------------------------------------------------

def classificar_email_geral(remetente: str, assunto: str, corpo: str) -> dict:
    """
    Extrai informações relevantes de e-mails gerais (agendamentos, financeiros).

    Retorna:
    {
      "categoria":       "agendamento | financeiro | veiculo | comunicado | servicos | outro",
      "resumo":          "1 frase",
      "remetente_nome":  str,
      "data_evento":     str | null,
      "valor":           str | null,
      "link_principal":  str | null,
      "requer_acao":     bool
    }
    """
    _definir_status("heuristica")
    prompt = f"""
Você é um assistente pessoal que organiza e-mails.
Analise o e-mail e responda APENAS em JSON estrito (sem crases):
{{
  "categoria": "agendamento | financeiro | veiculo | comunicado | servicos | outro",
  "resumo": "1 frase curta sobre o que o e-mail diz",
  "remetente_nome": "nome amigável (ex: Barbearia Hermanos, Google Play)",
  "data_evento": "data/hora do agendamento OU data de vencimento (DD/MM/AAAA, com HH:MM se houver), ou null",
  "valor": "valor monetário como string (ex: R$ 49,90), ou null",
  "link_principal": "URL mais importante do e-mail, ou null",
  "requer_acao": true/false
}}

Categorias:
  agendamento — confirmação de horário, reserva, consulta
  financeiro  — fatura, boleto, cobrança, comprovante, nota fiscal
  veiculo     — Detran, IPVA, licenciamento, multa de trânsito, CNH, seguro auto
  comunicado  — aviso de empresa/banco/serviço que você usa (mudança de
                horário, termos, manutenção, alerta de segurança)
  servicos    — notificações de apps, boas-vindas, newsletters, promoções
  outro       — nenhuma das anteriores

Remetente: {remetente}
Assunto: {assunto}
Corpo: {corpo[:800]}
{INSTRUCAO_JSON_PURO}
"""
    try:
        raw = _chamar_gemini(prompt)
        data = _parse_json(raw)
        return data if isinstance(data, dict) else {}
    except Exception as e:
        logger.warning(f"classificar_email_geral falhou: {e}")
        _definir_status("falha")
        return {}


# ---------------------------------------------------------------------------
# 5. CARTA DE APRESENTAÇÃO (rascunho de candidatura)
# ---------------------------------------------------------------------------

def _juntar(itens) -> str:
    """A IA às vezes devolve requisitos como dicts/números — tudo vira texto."""
    if not isinstance(itens, list):
        return str(itens or "")
    return ", ".join(str(i.get("nome") or i.get("descricao") or i) if isinstance(i, dict) else str(i)
                     for i in itens if i)


def gerar_carta_apresentacao(vaga: dict, perfil: str) -> str:
    """
    Escreve uma carta curta e específica para a vaga, usando SÓ fatos do
    perfil (a IA não pode inventar experiência). Retorna texto puro, ou ""
    em caso de falha.
    """
    _definir_status("heuristica")
    detalhes = "\n".join(
        f"- {rotulo}: {valor}" for rotulo, valor in [
            ("Cargo", vaga.get("cargo")),
            ("Empresa", vaga.get("empresa")),
            ("Local", vaga.get("local")),
            ("Modelo", vaga.get("modelo_trabalho")),
            ("Requisitos", _juntar(vaga.get("requisitos"))),
            ("Tecnologias", _juntar(vaga.get("tecnologias"))),
        ] if valor
    )
    prompt = f"""
Você escreve cartas de apresentação curtas para candidaturas a vagas de tecnologia.

PERFIL DO CANDIDATO (única fonte de fatos permitida):
{perfil[:6000]}

VAGA:
{detalhes}

REGRAS:
- Português do Brasil, tom técnico, direto e sem clichês (siga as diretrizes de comunicação do perfil).
- Máximo de 170 palavras, 3 parágrafos curtos, saudação "Olá," e assinatura com o nome do candidato.
- Conecte 2 ou 3 requisitos/tecnologias da vaga a experiências ou projetos REAIS do perfil.
- NUNCA invente experiência, tempo de carreira, certificação ou tecnologia que não esteja no perfil.
  Se um requisito não aparece no perfil, não o mencione.
- Não use markdown, emojis nem colchetes de preenchimento — o texto sai pronto para enviar.

Responda apenas com o texto da carta.
"""
    try:
        texto = _chamar_gemini(prompt).strip()
    except Exception as e:
        logger.warning(f"gerar_carta_apresentacao falhou: {e}")
        _definir_status("falha")
        return ""
    # Remove cercas de markdown que alguns modelos insistem em usar
    return re.sub(r"^```\w*\s*|\s*```$", "", texto).strip()


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
