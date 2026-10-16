"""
gerar_dashboard.py — AutoApply AI
==================================
Gera o dashboard com os dados REAIS do Supabase (antes era um protótipo com
dados fictícios): vagas por score, candidaturas, tecnologias mais pedidas,
despesas, certificados e alertas.

O resultado é um único HTML estático (dashboard/index.html) — abre direto
no navegador, sem servidor e sem expor a chave do Supabase. O agendador
regenera o arquivo depois de cada execução do pipeline.

Uso:
    python gerar_dashboard.py              # últimos 30 dias
    python gerar_dashboard.py --dias 90
    python gerar_dashboard.py --abrir      # gera e abre no navegador
"""

import argparse
import collections
import datetime
import json
import os
import sys
import webbrowser

from dotenv import load_dotenv

from briefing_diario import _formatar_reais

load_dotenv()

_BASE_DIR     = os.path.dirname(os.path.abspath(__file__))
TEMPLATE_PATH = os.path.join(_BASE_DIR, "dashboard", "template.html")
SAIDA_PADRAO  = os.getenv("DASHBOARD_SAIDA") or os.path.join(_BASE_DIR, "dashboard", "index.html")
MARCADOR      = "/*__DADOS__*/{}"

_COLUNAS_VAGAS_LEGADO = "cargo, fonte, tecnologias, links_candidatura, status_aplicacao, created_at"
_COLUNAS_VAGAS_V3     = _COLUNAS_VAGAS_LEGADO + ", empresa, local, modelo_trabalho, score_match"


# ── Coleta ───────────────────────────────────────────────────────────────────

def _buscar(sb, tabela: str, colunas: str, desde: str, limite: int) -> list[dict]:
    res = (
        sb.table(tabela).select(colunas)
        .gte("created_at", desde)
        .order("created_at", desc=True)
        .limit(limite)
        .execute()
    )
    return res.data or []


def coletar(sb, dias: int) -> dict:
    """Busca as 4 tabelas. Uma tabela ausente/sem migração vira lista vazia."""
    desde = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=dias)).isoformat()
    dados = {}

    try:
        dados["vagas"] = _buscar(sb, "vagas_extraidas", _COLUNAS_VAGAS_V3, desde, 1000)
    except Exception:
        try:
            dados["vagas"] = _buscar(sb, "vagas_extraidas", _COLUNAS_VAGAS_LEGADO, desde, 1000)
        except Exception as e:
            print(f"[WARN] vagas_extraidas: {e}")
            dados["vagas"] = []

    consultas = {
        "despesas":     ("despesas_domesticas", "estabelecimento, categoria, valor, data_despesa, created_at"),
        "alertas":      ("log_acessos_otp", "plataforma, tipo_alerta, created_at"),
        "certificados": ("certificados_portfolio", "plataforma, nome_curso, caminho_local, status_extracao, created_at"),
    }
    for chave, (tabela, colunas) in consultas.items():
        try:
            dados[chave] = _buscar(sb, tabela, colunas, desde, 300)
        except Exception as e:
            print(f"[WARN] {tabela}: {e}")
            dados[chave] = []
    return dados


# ── Transformação (pura — testável sem Supabase) ────────────────────────────

def _lista_json(valor) -> list:
    """Colunas jsonb antigas guardavam a lista como string JSON."""
    if isinstance(valor, list):
        return valor
    if isinstance(valor, str):
        try:
            convertido = json.loads(valor)
            return convertido if isinstance(convertido, list) else []
        except json.JSONDecodeError:
            return []
    return []


def _data_curta(iso: str | None) -> str:
    if not iso:
        return ""
    try:
        dt = datetime.datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return iso[:10]
    if dt.tzinfo:
        dt = dt.astimezone()
    return dt.strftime("%d/%m %H:%M")


def montar_dados(brutos: dict, dias: int, agora: datetime.datetime | None = None) -> dict:
    agora = agora or datetime.datetime.now()

    vagas = []
    for v in brutos.get("vagas", []):
        links = _lista_json(v.get("links_candidatura"))
        empresa = v.get("empresa")
        if empresa and empresa.lower().startswith("não identificada"):
            empresa = None
        vagas.append({
            "cargo":   v.get("cargo") or "Cargo não identificado",
            "empresa": empresa,
            "local":   v.get("local"),
            "modelo":  v.get("modelo_trabalho") or "Não informado",
            "score":   int(v.get("score_match") or 0),
            "techs":   [str(t).lower() for t in _lista_json(v.get("tecnologias"))],
            "link":    links[0] if links else None,
            "status":  v.get("status_aplicacao") or "nova",
            "fonte":   v.get("fonte") or "outro",
            "data":    _data_curta(v.get("created_at")),
            "_criado": v.get("created_at") or "",
        })
    # Melhores primeiro; empate → mais recente primeiro
    vagas.sort(key=lambda v: v["_criado"], reverse=True)
    vagas.sort(key=lambda v: v["score"], reverse=True)
    for v in vagas:
        del v["_criado"]

    com_score = [v["score"] for v in vagas if v["score"] > 0]
    valores = []
    for d in brutos.get("despesas", []):
        try:
            valores.append(float(d.get("valor")))
        except (TypeError, ValueError):
            pass

    fontes = collections.Counter(v["fonte"] for v in vagas)
    techs = collections.Counter(t for v in vagas for t in v["techs"])

    return {
        "gerado_em": agora.strftime("%d/%m/%Y %H:%M"),
        "dias": dias,
        "kpis": {
            "vagas":          len(vagas),
            "vagas_alta":     sum(1 for s in com_score if s >= 75),
            "score_medio":    round(sum(com_score) / len(com_score)) if com_score else None,
            "candidaturas":   sum(1 for v in vagas if v["status"] != "nova"),
            "em_processo":    sum(1 for v in vagas if v["status"] == "em_processo"),
            "despesas":       len(brutos.get("despesas", [])),
            "despesas_total": _formatar_reais(sum(valores)) if valores else "—",
        },
        "por_fonte": [{"fonte": f, "n": n} for f, n in fontes.most_common()],
        "top_techs": [{"tech": t, "n": n} for t, n in techs.most_common(10)],
        "vagas": vagas,
        # Só plataforma e tipo — nunca o código/link
        "alertas": [
            {"data": _data_curta(a.get("created_at")), "plataforma": a.get("plataforma") or "?",
             "tipo": a.get("tipo_alerta") or "?"}
            for a in brutos.get("alertas", [])[:15]
        ],
        "despesas": [
            {"estabelecimento": d.get("estabelecimento") or "?", "categoria": d.get("categoria") or "",
             "vencimento": d.get("data_despesa"), "valor": _formatar_reais(d.get("valor"))}
            for d in brutos.get("despesas", [])
        ],
        "certificados": [
            {"curso": c.get("nome_curso") or "?", "plataforma": c.get("plataforma") or "?",
             "status": c.get("status_extracao") or "?", "link": c.get("caminho_local"),
             "data": _data_curta(c.get("created_at"))}
            for c in brutos.get("certificados", [])
        ],
    }


def renderizar(dados: dict, template: str) -> str:
    if MARCADOR not in template:
        raise ValueError(f"Marcador {MARCADOR!r} não encontrado no template do dashboard.")
    # "</" escapado impede que um texto vindo de e-mail feche a tag <script>
    payload = json.dumps(dados, ensure_ascii=False).replace("</", "<\\/")
    return template.replace(MARCADOR, payload, 1)


# ── CLI ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="AutoApply AI — gera o dashboard com dados do Supabase")
    parser.add_argument("--dias", type=int, default=30, help="Janela de dados (padrão: 30 dias)")
    parser.add_argument("--saida", default=SAIDA_PADRAO, help="Arquivo HTML gerado")
    parser.add_argument("--abrir", action="store_true", help="Abre o dashboard no navegador ao final")
    args = parser.parse_args()

    url = (os.getenv("SUPABASE_URL") or "").rstrip("/")
    chave = os.getenv("SUPABASE_KEY")
    if not url or not chave:
        print("[ERRO] Preencha SUPABASE_URL e SUPABASE_KEY no .env.")
        sys.exit(1)

    from supabase import create_client
    brutos = coletar(create_client(url, chave), args.dias)

    with open(TEMPLATE_PATH, encoding="utf-8") as f:
        html = renderizar(montar_dados(brutos, args.dias), f.read())

    os.makedirs(os.path.dirname(os.path.abspath(args.saida)), exist_ok=True)
    with open(args.saida, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"✅ Dashboard gerado: {args.saida} ({len(brutos['vagas'])} vagas)")

    if args.abrir:
        webbrowser.open("file://" + os.path.abspath(args.saida))


if __name__ == "__main__":
    main()
