"""
vagas.py — AutoApply AI
========================
Linha de comando para fechar o ciclo das candidaturas.

O funil (nova → aplicada → em processo → resultado) só andava sozinho
quando a plataforma manda e-mail de "candidatura enviada". Candidatura
feita no site da empresa, por indicação ou numa plataforma que não avisa
ficava para sempre como "nova" — e voltava todo dia no "Aplicar hoje" do
briefing. Aqui você marca em um comando, com o número (#id) que aparece
no briefing.

Uso:
    python vagas.py listar                  # vagas novas dos últimos 7 dias, maior score primeiro
    python vagas.py listar --dias 30 --status aplicada
    python vagas.py aplicar 123 124         # já me candidatei
    python vagas.py descartar 125           # não tenho interesse (sai do "Aplicar hoje")
    python vagas.py status 123 em_processo  # nova | aplicada | em_processo | reprovada | aprovada | descartada
"""

import argparse
import sys

from dotenv import load_dotenv

import console
from formatacao import empresa_conhecida

load_dotenv()

STATUS_VALIDOS = ("nova", "aplicada", "em_processo", "reprovada", "aprovada", "descartada")


def atualizar_status(sb, ids: list[int], status: str) -> dict[int, bool]:
    """Muda o status_aplicacao das vagas pelo id. Retorna {id: encontrou?}."""
    if status not in STATUS_VALIDOS:
        raise ValueError(f"status inválido: {status} (use {', '.join(STATUS_VALIDOS)})")
    resultado = {}
    for vaga_id in ids:
        res = sb.table("vagas_extraidas").update({"status_aplicacao": status}).eq("id", vaga_id).execute()
        resultado[vaga_id] = bool(res.data)
    return resultado


def formatar_lista(vagas: list[dict]) -> str:
    if not vagas:
        return "Nenhuma vaga encontrada com esses filtros."
    linhas = []
    for v in vagas:
        nome = v.get("cargo") or "Cargo não identificado"
        if empresa_conhecida(v.get("empresa")):
            nome = f"{nome} @ {v['empresa']}"
        links = v.get("links_candidatura") or []
        link = links[0] if isinstance(links, list) and links else ""
        linhas.append(f"#{v.get('id', '?'):<6} {v.get('score_match') or 0:>3}%  "
                      f"[{v.get('status_aplicacao') or 'nova'}] {nome}")
        motivos = v.get("motivos_match") or []
        if isinstance(motivos, list) and motivos:
            linhas.append(f"         por quê: {', '.join(str(m) for m in motivos[:4])}")
        if link:
            linhas.append(f"         {link}")
    return "\n".join(linhas)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="AutoApply AI — acompanhe e marque suas candidaturas")
    sub = parser.add_subparsers(dest="comando", required=True)

    p_listar = sub.add_parser("listar", help="lista vagas do Supabase")
    p_listar.add_argument("--dias", type=int, default=7)
    p_listar.add_argument("--status", default="nova", help="nova | aplicada | ... | todas")
    p_listar.add_argument("--min", type=int, default=0, help="score mínimo")
    p_listar.add_argument("--limite", type=int, default=20)

    for nome, ajuda in (("aplicar", "marca como aplicada"), ("descartar", "marca como descartada")):
        p = sub.add_parser(nome, help=ajuda)
        p.add_argument("ids", type=int, nargs="+")

    p_status = sub.add_parser("status", help="define um status qualquer")
    p_status.add_argument("id", type=int)
    p_status.add_argument("novo_status", choices=STATUS_VALIDOS)

    args = parser.parse_args(argv)
    console.preparar_console()

    import briefing_diario
    sb = briefing_diario._criar_sb()
    if not sb:
        print("Supabase não configurado (SUPABASE_URL/SUPABASE_KEY no .env).")
        return 1

    if args.comando == "listar":
        vagas = briefing_diario.coletar_vagas(sb, args.dias, limite=1000)
        if args.status != "todas":
            vagas = [v for v in vagas if (v.get("status_aplicacao") or "nova") == args.status]
        vagas = [v for v in vagas if (v.get("score_match") or 0) >= args.min][:args.limite]
        print(formatar_lista(vagas))
        return 0

    if args.comando == "status":
        ids, status = [args.id], args.novo_status
    else:
        ids, status = args.ids, {"aplicar": "aplicada", "descartar": "descartada"}[args.comando]

    falhas = 0
    for vaga_id, achou in atualizar_status(sb, ids, status).items():
        if achou:
            print(f"✔ #{vaga_id} → {status}")
        else:
            print(f"✖ #{vaga_id} não encontrada")
            falhas += 1
    return 1 if falhas else 0


if __name__ == "__main__":
    sys.exit(main())
