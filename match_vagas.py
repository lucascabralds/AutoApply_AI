"""
match_vagas.py — AutoApply AI
==============================
Score de aderência LOCAL, determinístico e explicável de uma vaga ao perfil
do Lucas (Desenvolvedor Python / Analista de Dados júnior).

Por que existe: o score que vem da IA é uma caixa-preta e falha muito na
prática — sai 0% quando o modelo não preenche o campo, 30% fixo para "vaga
sem tech" (regra do prompt) e, quando a IA cai e entra a heurística, toda
vaga fica com 0%. Com isso a priorização (label Alta_Prioridade, push no
Telegram, follow-up no Tasks, rascunho de candidatura) dependia de sorte.

Aqui o score é calculado com regras simples sobre os campos extraídos
(cargo, tecnologias, requisitos, local, modelo de trabalho) e cada ponto
vem com o MOTIVO ("+50 área-alvo: dados", "-25 nível sênior"...), para o
Lucas entender por que uma vaga foi priorizada — e ajustar as regras aqui
se discordar. O score final combina este cálculo com o da IA (quando a IA
deu um score > 0).

Nada aqui chama rede ou IA: é puro e testável.

Configuração (.env):
  LOCAIS_ACEITOS=São Paulo,SP   regiões onde presencial/híbrido vale a pena
                                (remoto é sempre aceito)
"""

import os
import re
import unicodedata

from dotenv import load_dotenv

load_dotenv()


def normalizar(texto) -> str:
    """Minúsculas, sem acento; c# / .net / c++ viram palavras para não sumirem."""
    if not texto:
        return ""
    if isinstance(texto, (list, tuple)):
        texto = " ".join(str(t.get("nome") or t.get("descricao") or t) if isinstance(t, dict) else str(t)
                         for t in texto if t)
    t = str(texto).lower()
    t = t.replace("c#", " csharp ").replace(".net", " dotnet ").replace("c++", " cpp ")
    t = "".join(c for c in unicodedata.normalize("NFKD", t) if not unicodedata.combining(c))
    t = re.sub(r"[^a-z0-9 ]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def _tem(padrao: str, texto: str) -> re.Match | None:
    """Busca `padrao` (regex sobre texto normalizado) como palavra inteira."""
    return re.search(r"(?<![a-z0-9])(?:" + padrao + r")(?![a-z0-9])", texto)


# ── Área do cargo ────────────────────────────────────────────────────────────
# (padrão, pontos, rótulo). Vale a PRIMEIRA faixa que casar no título.
_AREAS = [
    (r"python",                                                                 55, "cargo Python"),
    (r"dados|data|bi|business intelligence|power bi|etl|analytics|banco de dados"
     r"|automacao|rpa|back ?end",                                               50, "área-alvo: dados/automação/backend"),
    (r"desenvolvedor|desenvolvimento|developer|programador|software|sistemas"
     r"|analista de ti|ti|tecnologia da informacao|full ?stack|devops|qa"
     r"|engenheiro de software|engenheira de software",                        30, "área de TI/desenvolvimento"),
    (r"suporte|help ?desk|service desk|infraestrutura|redes|tecnico de informatica",
                                                                                10, "TI, mas suporte/infra"),
]

# Cargos claramente fora da transição de carreira — só contam se o título
# não casou nenhuma área de TI acima.
_FORA_DO_PERFIL = (
    r"vendedor|vendedora|vendas|comercial|motorista|auxiliar|ajudante|atendente|operador|operadora"
    r"|estoquista|repositor|recepcionista|telemarketing|promotor|promotora|mecanico|eletricista"
    r"|cozinheiro|cozinheira|enfermagem|enfermeiro|limpeza|vigilante|porteiro|caixa|garcom"
    r"|montador|soldador|pedreiro|corretor|corretora|tecnico"
)
_TETO_FORA_DO_PERFIL = 15

# ── Tecnologias do perfil (grupo, padrões, pontos) ──────────────────────────
_TECHS_PERFIL = [
    ("Python",        r"python|pandas|numpy",                                         12),
    ("SQL",           r"sql|postgresql|postgres|mysql|banco de dados|bancos de dados", 8),
    ("Power BI",      r"power ?bi|dax|power query",                                    8),
    ("ETL",           r"etl|data warehouse|pipelines? de dados",                       6),
    ("APIs/FastAPI",  r"fastapi|api|apis|rest|restful",                                5),
    ("automação",     r"automacao|automacoes|rpa|vba|macros?|selenium|sap",            5),
    ("Docker",        r"docker|containers?",                                           4),
    ("IA/LLM",        r"llm|llms|ia generativa|inteligencia artificial|machine learning|gemini|openai",
                                                                                       4),
    ("Excel",         r"excel",                                                        3),
    ("Git",           r"git|github",                                                   2),
    ("Vue",           r"vue|vuejs",                                                    2),
]
_TETO_TECHS = 35

# Stacks em que o Lucas não se posiciona (Java só como diferencial)
_TECHS_FORA = r"java|csharp|dotnet|php|golang|ruby|kotlin|swift|angular|react|flutter|delphi|cobol|abap|salesforce|cpp"
_PENALIDADE_STACK_FORA = 10

# ── Senioridade ──────────────────────────────────────────────────────────────
_NIVEIS = [
    (r"senior|sr|especialista|lead|lider|coordenador|coordenadora|gerente|head|principal|staff|arquiteto|arquiteta",
                                                    -25, "nível sênior/liderança"),
    (r"estagio|estagiario|estagiaria",              -10, "estágio (costuma exigir matrícula)"),
    (r"pleno|pl(?! ?sql)|mid",                                -5, "nível pleno"),
    (r"junior|jr|trainee|iniciante|assistente|entry level", 10, "nível júnior"),
]
_EXPERIENCIA_ANOS = re.compile(r"(\d{1,2})\s*(?:\+|ou mais)?\s*anos? de experiencia")

# ── Local / modelo ───────────────────────────────────────────────────────────
_REMOTO = r"remoto|remota|home office|homeoffice|trabalhe de casa|100 remoto|anywhere"
_LOCAL_INDEFINIDO = {"", "nao informado", "brasil", "brazil", "null", "none", "a combinar"}


def _locais_aceitos() -> list[str]:
    bruto = os.getenv("LOCAIS_ACEITOS", "São Paulo,SP")
    return [normalizar(p) for p in bruto.split(",") if normalizar(p)]


def avaliar(vaga: dict) -> dict:
    """
    Calcula o score local (0-100) de uma vaga. Retorna:
      {"score": int, "motivos": ["+50 área-alvo...", ...], "fora_do_perfil": bool}
    """
    cargo = normalizar(vaga.get("cargo"))
    techs = normalizar(vaga.get("tecnologias"))
    reqs = normalizar(vaga.get("requisitos"))
    texto_tech = f"{cargo} {techs} {reqs}"
    pontos: list[tuple[int, str]] = []

    # Área
    area = None
    for padrao, p, rotulo in _AREAS:
        m = _tem(padrao, cargo)
        if m:
            area = (p, f"{rotulo} ({m.group(0)})")
            break
    fora = area is None and bool(_tem(_FORA_DO_PERFIL, cargo))
    if area:
        pontos.append(area)
    elif fora:
        pontos.append((0, "cargo fora do perfil"))

    # Tecnologias do perfil
    soma_tech, grupos = 0, []
    for grupo, padrao, p in _TECHS_PERFIL:
        if _tem(padrao, texto_tech):
            soma_tech += p
            grupos.append(grupo)
    if grupos:
        pontos.append((min(soma_tech, _TETO_TECHS), "stack do perfil: " + ", ".join(grupos)))
    else:
        stack_fora = sorted({m.group(0) for m in re.finditer(
            r"(?<![a-z0-9])(?:" + _TECHS_FORA + r")(?![a-z0-9])", texto_tech)})
        if stack_fora:
            pontos.append((-_PENALIDADE_STACK_FORA, "stack fora do perfil: " + ", ".join(stack_fora)))

    # Senioridade (título primeiro; requisitos para "X anos de experiência")
    nivel = next(((p, r) for padrao, p, r in _NIVEIS if _tem(padrao, cargo)), None)
    if nivel:
        pontos.append(nivel)
    anos = [int(n) for n in _EXPERIENCIA_ANOS.findall(reqs)]
    if anos and max(anos) >= 4 and not (nivel and nivel[0] < 0):
        pontos.append((-15, f"pede {max(anos)}+ anos de experiência"))

    # Local / modelo de trabalho
    modelo = normalizar(vaga.get("modelo_trabalho"))
    local = normalizar(vaga.get("local"))
    if _tem(_REMOTO, f"{modelo} {cargo} {local}"):
        pontos.append((10, "remoto"))
    elif local not in _LOCAL_INDEFINIDO:
        if any(_tem(re.escape(l), local) for l in _locais_aceitos()):
            pontos.append((5, f"local aceito ({vaga.get('local')})"))
        else:
            pontos.append((-15, f"fora da região ({vaga.get('local')})"))

    score = sum(p for p, _ in pontos)
    if fora:
        score = min(score, _TETO_FORA_DO_PERFIL)
    score = max(0, min(100, score))
    motivos = [f"{p:+d} {r}" for p, r in pontos]
    return {"score": score, "motivos": motivos, "fora_do_perfil": fora}


def combinar(score_local: int, score_ia) -> int:
    """
    Score final usado na priorização. Score da IA ausente ou 0 (o padrão
    quando o modelo não preenche o campo) não conta — vale o local. Com os
    dois, média simples: a IA lê o e-mail inteiro, o local é estável e
    explicável; um corrige o exagero do outro.
    """
    try:
        ia = int(score_ia or 0)
    except (TypeError, ValueError):
        ia = 0
    if ia <= 0:
        return int(score_local)
    return round((int(score_local) + max(0, min(100, ia))) / 2)


def resumo_motivos(motivos: list[str], limite: int = 4) -> str:
    """'+55 cargo Python (python), +10 nível júnior' — para log, push e briefing."""
    return ", ".join((motivos or [])[:limite])
