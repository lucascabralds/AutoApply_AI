"""
parsers_vagas.py — AutoApply AI
================================
Leitura DETERMINÍSTICA (sem IA) dos digests de vagas das plataformas cujo
e-mail tem estrutura fixa.

Por quê: a IA lia o digest inteiro e devolvia um JSON grande — que vinha
cortado, com raciocínio no lugar do JSON, ou com poucas vagas (num alerta
do Indeed com 20 vagas, só 3 eram extraídas). O HTML/texto desses e-mails
segue um molde estável, então dá para ler todas as vagas por código: mais
rápido, sem custo de IA, sem limite de tamanho e sem inventar dado.

Plataformas cobertas (moldes vistos em e-mails reais de out/2026):
  - Catho   (avisovagas@catho.com.br): cada vaga é um <a> de rastreamento
    que envolve "Cargo", "EMPRESA | Cidade" e "Salário: ...".
  - Indeed  (alertas de vaga): o texto puro traz blocos "Cargo",
    "Empresa - Cidade, UF", salário opcional, resumo, idade e o link ?jk=.

Quando o molde não é reconhecido (ou não há nenhuma vaga listada, como no
e-mail "empresas destacadas" do InfoJobs), devolve None e o pipeline segue
pelo caminho com IA, como antes.
"""

import re
from typing import Callable

from bs4 import BeautifulSoup

_CATHO_VAGA = re.compile(r"https?://(?:www\.)?catho\.com\.br/vagas/([^/?#]+)/(\d+)", re.IGNORECASE)
_INDEED_JK = re.compile(r"https?://[a-z.]*indeed\.com/\S*?[?&]jk=([0-9a-f]{8,})", re.IGNORECASE)
_IDADE = re.compile(r"^(h[aá] \d+ (dias?|horas?)|rec[eé]m[- ]publicada|hoje|ontem|\d+\+? dias? atr[aá]s)$", re.IGNORECASE)
_REMOTO = re.compile(r"\b(remot[oa]|remote|home ?office|trabalhe de casa|anywhere)\b", re.IGNORECASE)
_CANDIDATURA_FACIL = re.compile(r"^candidate-se facilmente$", re.IGNORECASE)


def _limpo(texto: str) -> str:
    return re.sub(r"\s+", " ", texto or "").strip()


def _modelo(*textos: str) -> str:
    return "Remoto" if any(_REMOTO.search(t or "") for t in textos) else "Não informado"


def _vaga(cargo, empresa, local, salario, link, resumo, modelo, extrair_tecnologias) -> dict:
    return {
        "cargo":              cargo,
        "empresa":            empresa,
        "local":              local,
        "modelo_trabalho":    modelo,
        "salario":            salario,
        "requisitos":         [],
        "tecnologias":        extrair_tecnologias(f"{cargo} {resumo or ''}"),
        "score_match":        0,   # sem IA: o pipeline usa o score local (match_vagas)
        "link_candidatura":   link,
        "tipo_email":         "nova_vaga",
        "status_candidatura": "nova",
        "resumo":             resumo,
        "origem_extracao":    "parser",
    }


# ── Catho ───────────────────────────────────────────────────────────────────

def _catho(html: str, resolver_link: Callable[[str], str], extrair_tecnologias) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    vagas, vistos = [], set()
    for a in soup.find_all("a", href=True):
        real = resolver_link(a["href"].strip())
        m = _CATHO_VAGA.match(real)
        if not m:
            continue
        link = f"https://www.catho.com.br/vagas/{m.group(1)}/{m.group(2)}/"
        if link in vistos:
            continue
        linhas = [_limpo(s) for s in a.stripped_strings]
        linhas = [l for l in linhas if l]
        if not linhas:
            continue
        cargo, empresa, local, salario = linhas[0], None, None, None
        for linha in linhas[1:]:
            if linha.lower().startswith("salário") or linha.lower().startswith("salario"):
                valor = linha.split(":", 1)[-1].strip()
                salario = None if valor.lower() in ("a combinar", "") else valor
            elif "|" in linha and empresa is None:
                empresa, _, local = (p.strip() for p in linha.partition("|"))
        vistos.add(link)
        vagas.append(_vaga(cargo, empresa or None, local or None, salario, link, None,
                           _modelo(cargo), extrair_tecnologias))
    return vagas


# ── Indeed ──────────────────────────────────────────────────────────────────

def _indeed(texto: str, extrair_tecnologias) -> list[dict]:
    vagas, vistos = [], set()
    for bloco in re.split(r"\n\s*\n", texto or ""):
        linhas = [l.strip() for l in bloco.split("\n") if l.strip()]
        jk = next((m.group(1) for l in linhas for m in [_INDEED_JK.search(l)] if m), None)
        if not jk or jk in vistos or len(linhas) < 3:
            continue
        cargo, empresa_local = linhas[0], linhas[1]
        if _INDEED_JK.search(cargo) or _INDEED_JK.search(empresa_local):
            continue   # bloco fora do molde (ex.: "See matching results")
        empresa, sep, local = empresa_local.rpartition(" - ")
        if not sep:
            empresa, local = empresa_local, None
        salario, resumo = None, []
        for linha in linhas[2:]:
            if _INDEED_JK.search(linha) or _IDADE.match(linha) or _CANDIDATURA_FACIL.match(linha):
                continue
            if "R$" in linha and salario is None:
                salario = linha
            else:
                resumo.append(linha)
        resumo_txt = " ".join(resumo)[:300] or None
        vistos.add(jk)
        vagas.append(_vaga(cargo, empresa.strip() or None, (local or "").strip() or None, salario,
                           # link curto e estável, sem os tokens pessoais do e-mail (tk, qd, bb...)
                           f"https://br.indeed.com/viewjob?jk={jk}",
                           resumo_txt, _modelo(cargo, resumo_txt), extrair_tecnologias))
    return vagas


# ── Entrada ─────────────────────────────────────────────────────────────────

def extrair(remetente: str, corpo: str, html: str,
            extrair_tecnologias: Callable[[str], list],
            resolver_link: Callable[[str], str] = lambda u: u) -> list[dict] | None:
    """
    Vagas lidas direto do e-mail, ou None se o molde não for reconhecido
    (aí o pipeline usa a IA). Nunca levanta exceção: um molde que mudou
    não pode derrubar o pipeline — só volta para o caminho com IA.
    """
    rem = (remetente or "").lower()
    try:
        if "catho.com.br" in rem and html:
            vagas = _catho(html, resolver_link, extrair_tecnologias)
        elif "indeed" in rem:
            vagas = _indeed(corpo, extrair_tecnologias)
        else:
            return None
    except Exception:
        return None
    return vagas or None
