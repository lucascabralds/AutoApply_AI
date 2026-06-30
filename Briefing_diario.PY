"""
briefing_diario.py — AutoApply AI
===================================
Gera e envia o briefing matinal do dia: resume tudo que o pipeline processou
nas últimas 24h e envia como e-mail para o próprio Gmail do usuário.

Conteúdo do briefing:
  📋 Vagas        — quantas chegaram, quais têm score alto, links diretos
  📅 Agendamentos — confirmações de horário do dia
  💰 Financeiro   — boletos, faturas, valores e datas de vencimento
  📢 Comunicados  — avisos de empresas/serviços (Smart Fit, bancos, etc.)
  🔒 Alertas      — OTPs e alertas de segurança da noite anterior
  🎓 Certificados — novos certificados extraídos

Uso:
    python briefing_diario.py              # envia para o próprio e-mail
    python briefing_diario.py --preview    # apenas imprime no terminal
    python briefing_diario.py --dias 2     # cobre as últimas 48h (virada de fim de semana)
"""

import os
import sys
import json
import argparse
import datetime
import smtplib
import textwrap
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from dotenv import load_dotenv
load_dotenv()

# ---------------------------------------------------------------------------
# Google / Gmail (para envio via API, sem SMTP)
# ---------------------------------------------------------------------------
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
import base64

# ---------------------------------------------------------------------------
# Supabase
# ---------------------------------------------------------------------------
from supabase import create_client, Client

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
SUPABASE_URL   = (os.getenv("SUPABASE_URL") or "").rstrip("/")
SUPABASE_KEY   = os.getenv("SUPABASE_KEY")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

_BASE_DIR        = os.path.dirname(os.path.abspath(__file__))
TOKEN_PATH       = os.getenv("GMAIL_TOKEN_PATH",       os.path.join(_BASE_DIR, "token.json"))
CREDENTIALS_PATH = os.getenv("GMAIL_CREDENTIALS_PATH", os.path.join(_BASE_DIR, "credentials.json"))

SCOPES_SEND = [
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.modify",
]

# Destinatário do briefing — padrão: próprio Gmail
DESTINATARIO = os.getenv("BRIEFING_DESTINATARIO", "me")

# Ícones das seções
ICONES = {
    "vagas":        "💼",
    "agendamentos": "📅",
    "financeiro":   "💰",
    "comunicados":  "📢",
    "alertas":      "🔒",
    "certificados": "🎓",
}


# ===========================================================================
# AUTENTICAÇÃO GMAIL
# ===========================================================================

def _autenticar_gmail():
    if not os.path.exists(CREDENTIALS_PATH):
        raise FileNotFoundError(
            f"credentials.json não encontrado em: {CREDENTIALS_PATH}"
        )
    creds = None
    if os.path.exists(TOKEN_PATH):
        try:
            creds = Credentials.from_authorized_user_file(TOKEN_PATH, SCOPES_SEND)
        except Exception:
            creds = None

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except Exception:
                creds = None
        if not creds:
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_PATH, SCOPES_SEND)
            creds = flow.run_local_server(port=0)
        with open(TOKEN_PATH, "w") as f:
            f.write(creds.to_json())

    return build("gmail", "v1", credentials=creds)


# ===========================================================================
# COLETA DE DADOS DO SUPABASE
# ===========================================================================

def _criar_sb() -> Client | None:
    if not SUPABASE_URL or not SUPABASE_KEY:
        print("[WARN] Supabase não configurado — briefing sem dados do banco.")
        return None
    try:
        return create_client(SUPABASE_URL, SUPABASE_KEY)
    except Exception as e:
        print(f"[WARN] Supabase falhou: {e}")
        return None


def _janela_iso(dias: int) -> str:
    """Retorna timestamp ISO do início da janela de busca."""
    dt = datetime.datetime.now() - datetime.timedelta(days=dias)
    return dt.isoformat()


def coletar_vagas(sb: Client, dias: int) -> list[dict]:
    """Vagas novas com score > 0, ordenadas por score desc."""
    if not sb:
        return []
    try:
        res = (
            sb.table("vagas_extraidas")
            .select("cargo, fonte, tecnologias, links_candidatura, status_aplicacao, created_at")
            .gte("created_at", _janela_iso(dias))
            .order("created_at", desc=True)
            .limit(50)
            .execute()
        )
        return res.data or []
    except Exception as e:
        print(f"[WARN] coletar_vagas: {e}")
        return []


def coletar_despesas(sb: Client, dias: int) -> list[dict]:
    """Faturas e boletos da janela."""
    if not sb:
        return []
    try:
        res = (
            sb.table("despesas_domesticas")
            .select("estabelecimento, categoria, valor, data_despesa, created_at")
            .gte("created_at", _janela_iso(dias))
            .order("created_at", desc=True)
            .limit(30)
            .execute()
        )
        return res.data or []
    except Exception as e:
        print(f"[WARN] coletar_despesas: {e}")
        return []


def coletar_alertas(sb: Client, dias: int) -> list[dict]:
    """OTPs e alertas de acesso."""
    if not sb:
        return []
    try:
        res = (
            sb.table("log_acessos_otp")
            .select("plataforma, tipo_alerta, codigo_extraido, created_at")
            .gte("created_at", _janela_iso(dias))
            .order("created_at", desc=True)
            .limit(20)
            .execute()
        )
        return res.data or []
    except Exception as e:
        print(f"[WARN] coletar_alertas: {e}")
        return []


def coletar_certificados(sb: Client, dias: int) -> list[dict]:
    if not sb:
        return []
    try:
        res = (
            sb.table("certificados_portfolio")
            .select("plataforma, nome_curso, status_extracao, caminho_local, created_at")
            .gte("created_at", _janela_iso(dias))
            .order("created_at", desc=True)
            .limit(20)
            .execute()
        )
        return res.data or []
    except Exception as e:
        print(f"[WARN] coletar_certificados: {e}")
        return []


# ===========================================================================
# COLETA DE E-MAILS DIRETO DO GMAIL (para agendamentos e comunicados)
# Não dependem do Supabase — lê as labels diretamente
# ===========================================================================

def _listar_por_label(service, label_nome: str, dias: int) -> list[dict]:
    """Lista mensagens com uma label específica na janela de dias."""
    try:
        # Busca o ID da label pelo nome
        labels_res = service.users().labels().list(userId="me").execute()
        label_id = next(
            (lb["id"] for lb in labels_res.get("labels", [])
             if lb["name"] == label_nome),
            None,
        )
        if not label_id:
            return []

        res = service.users().messages().list(
            userId="me",
            q=f"newer_than:{dias}d",
            labelIds=[label_id],
            maxResults=20,
        ).execute()

        msgs = []
        for m in res.get("messages", []):
            detail = service.users().messages().get(
                userId="me", id=m["id"], format="metadata"
            ).execute()
            headers = detail.get("payload", {}).get("headers", [])
            assunto = next((h["value"] for h in headers if h["name"] == "Subject"), "")
            remetente = next((h["value"] for h in headers if h["name"] == "From"), "")
            snippet = detail.get("snippet", "")
            msgs.append({
                "assunto":   assunto,
                "remetente": remetente.split("<")[0].strip(),
                "snippet":   snippet[:120],
            })
        return msgs
    except Exception as e:
        print(f"[WARN] _listar_por_label({label_nome}): {e}")
        return []


# ===========================================================================
# MONTAGEM DO BRIEFING
# ===========================================================================

def _linha(char: str = "─", n: int = 52) -> str:
    return char * n


def _formatar_vagas(vagas: list[dict]) -> tuple[str, str]:
    """Retorna (bloco_texto, bloco_html)."""
    if not vagas:
        return "  Nenhuma vaga nova no período.\n", "<li>Nenhuma vaga nova no período.</li>"

    txt_linhas = []
    html_linhas = []

    # Agrupa por fonte
    por_fonte: dict[str, list] = {}
    for v in vagas:
        f = v.get("fonte", "outro")
        por_fonte.setdefault(f, []).append(v)

    for fonte, items in sorted(por_fonte.items()):
        txt_linhas.append(f"  [{fonte.upper()}] {len(items)} vaga(s)")
        html_linhas.append(f"<b>{fonte.upper()}</b> — {len(items)} vaga(s)<br>")
        for v in items[:5]:   # máx 5 por fonte no briefing
            cargo = v.get("cargo", "Cargo não identificado")[:55]
            links_raw = v.get("links_candidatura") or "[]"
            if isinstance(links_raw, str):
                try:
                    links = json.loads(links_raw)
                except Exception:
                    links = []
            else:
                links = links_raw or []
            link = links[0] if links else None
            status = v.get("status_aplicacao", "nova")

            txt_linhas.append(f"    · {cargo}  [{status}]")
            if link:
                txt_linhas.append(f"      {link[:70]}")

            html_cargo = f'<a href="{link}">{cargo}</a>' if link else cargo
            html_linhas.append(f"&nbsp;&nbsp;• {html_cargo} <i>[{status}]</i><br>")

    return "\n".join(txt_linhas) + "\n", "".join(html_linhas)


def _formatar_despesas(despesas: list[dict]) -> tuple[str, str]:
    if not despesas:
        return "  Nenhuma despesa registrada.\n", "<li>Nenhuma despesa registrada.</li>"
    txt = []
    html = []
    for d in despesas:
        est   = d.get("estabelecimento", "?")[:35]
        val   = d.get("valor") or "—"
        dt    = d.get("data_despesa") or "—"
        cat   = d.get("categoria", "")
        txt.append(f"  · {est:<35}  {val:<12}  venc. {dt}")
        html.append(f"<li><b>{est}</b> — {val} — venc. <b>{dt}</b> <i>({cat})</i></li>")
    return "\n".join(txt) + "\n", "".join(html)


def _formatar_lista_simples(items: list[dict], campos: list[str]) -> tuple[str, str]:
    if not items:
        return "  Nenhum item.\n", "<li>Nenhum item.</li>"
    txt, html = [], []
    for it in items:
        partes = [str(it.get(c, "")) for c in campos if it.get(c)]
        linha  = "  · " + "  |  ".join(partes[:3])
        txt.append(linha[:100])
        html.append("<li>" + "  |  ".join(partes[:3])[:100] + "</li>")
    return "\n".join(txt) + "\n", "".join(html)


def _formatar_comunicados(msgs: list[dict]) -> tuple[str, str]:
    if not msgs:
        return "  Nenhum comunicado.\n", "<li>Nenhum comunicado.</li>"
    txt, html = [], []
    for m in msgs:
        rem     = m.get("remetente", "?")[:25]
        assunto = m.get("assunto", "")[:60]
        snippet = m.get("snippet", "")[:80]
        txt.append(f"  · [{rem}] {assunto}")
        txt.append(f"    {snippet}")
        html.append(f"<li><b>{rem}</b> — {assunto}<br><i>{snippet}</i></li>")
    return "\n".join(txt) + "\n", "".join(html)


def montar_briefing(
    vagas:        list[dict],
    despesas:     list[dict],
    alertas:      list[dict],
    certificados: list[dict],
    agendamentos: list[dict],
    comunicados:  list[dict],
    dias:         int,
) -> tuple[str, str]:
    """
    Monta o briefing em dois formatos: texto puro e HTML.
    Retorna (texto, html).
    """
    agora = datetime.datetime.now()
    periodo = "hoje" if dias == 1 else f"últimas {dias * 24}h"

    # ── Cabeçalho ────────────────────────────────────────────────────────────
    txt_header = (
        f"\n{'═'*54}\n"
        f"  🤖 AUTOAPPLY AI — BRIEFING MATINAL\n"
        f"  {agora.strftime('%A, %d/%m/%Y às %H:%M')}  |  janela: {periodo}\n"
        f"{'═'*54}\n\n"
    )
    html_header = f"""
<div style="font-family:monospace;background:#0d0f14;color:#e8eaf0;padding:20px;border-radius:8px;max-width:680px">
<h2 style="color:#00e5a0;margin:0 0 4px">🤖 AutoApply AI — Briefing Matinal</h2>
<p style="color:#7a82a0;margin:0 0 16px">{agora.strftime('%A, %d/%m/%Y às %H:%M')} &nbsp;|&nbsp; janela: {periodo}</p>
<hr style="border-color:#1c2030">
"""

    # ── Sumário numérico ─────────────────────────────────────────────────────
    txt_sumario = (
        f"  RESUMO\n"
        f"  {'─'*40}\n"
        f"  💼 Vagas novas        : {len(vagas)}\n"
        f"  📅 Agendamentos       : {len(agendamentos)}\n"
        f"  💰 Financeiro         : {len(despesas)}\n"
        f"  📢 Comunicados        : {len(comunicados)}\n"
        f"  🔒 Alertas de acesso  : {len(alertas)}\n"
        f"  🎓 Certificados       : {len(certificados)}\n\n"
    )
    html_sumario = f"""
<table style="width:100%;border-collapse:collapse;margin-bottom:16px">
  <tr><td style="padding:4px 8px">💼 Vagas novas</td>
      <td style="color:#00e5a0;font-weight:bold;padding:4px 8px">{len(vagas)}</td></tr>
  <tr><td style="padding:4px 8px">📅 Agendamentos</td>
      <td style="color:#4d9fff;font-weight:bold;padding:4px 8px">{len(agendamentos)}</td></tr>
  <tr><td style="padding:4px 8px">💰 Financeiro</td>
      <td style="color:#f0c040;font-weight:bold;padding:4px 8px">{len(despesas)}</td></tr>
  <tr><td style="padding:4px 8px">📢 Comunicados</td>
      <td style="color:#a78bfa;font-weight:bold;padding:4px 8px">{len(comunicados)}</td></tr>
  <tr><td style="padding:4px 8px">🔒 Alertas</td>
      <td style="color:#ff5c6a;font-weight:bold;padding:4px 8px">{len(alertas)}</td></tr>
  <tr><td style="padding:4px 8px">🎓 Certificados</td>
      <td style="color:#4d9fff;font-weight:bold;padding:4px 8px">{len(certificados)}</td></tr>
</table>
<hr style="border-color:#1c2030">
"""

    def secao_txt(icone, titulo, conteudo):
        return f"{icone} {titulo}\n{'─'*54}\n{conteudo}\n"

    def secao_html(icone, titulo, conteudo_html, cor="#00e5a0"):
        return (
            f'<h3 style="color:{cor};margin:16px 0 6px">{icone} {titulo}</h3>'
            f'<ul style="margin:0;padding-left:16px;color:#e8eaf0;line-height:1.7">'
            f'{conteudo_html}</ul>'
            f'<hr style="border-color:#1c2030;margin:12px 0">'
        )

    # ── Seções ───────────────────────────────────────────────────────────────
    tv, hv = _formatar_vagas(vagas)
    td, hd = _formatar_despesas(despesas)
    ta, ha = _formatar_lista_simples(alertas,      ["plataforma", "tipo_alerta", "codigo_extraido"])
    tc, hc = _formatar_lista_simples(certificados, ["plataforma", "nome_curso",  "status_extracao"])
    tag, hag = _formatar_comunicados(agendamentos)
    tco, hco = _formatar_comunicados(comunicados)

    txt_body = (
        secao_txt("💼", "VAGAS",        tv)
        + secao_txt("📅", "AGENDAMENTOS", tag)
        + secao_txt("💰", "FINANCEIRO",   td)
        + secao_txt("📢", "COMUNICADOS",  tco)
        + secao_txt("🔒", "ALERTAS",      ta)
        + secao_txt("🎓", "CERTIFICADOS", tc)
    )

    html_body = (
        secao_html("💼", "Vagas",        hv,   "#00e5a0")
        + secao_html("📅", "Agendamentos", hag,  "#4d9fff")
        + secao_html("💰", "Financeiro",   hd,   "#f0c040")
        + secao_html("📢", "Comunicados",  hco,  "#a78bfa")
        + secao_html("🔒", "Alertas",      ha,   "#ff5c6a")
        + secao_html("🎓", "Certificados", hc,   "#4d9fff")
    )

    txt_footer = f"{'═'*54}\n  AutoApply AI · gerado em {agora.strftime('%H:%M:%S')}\n"
    html_footer = (
        f'<p style="color:#7a82a0;font-size:11px;margin-top:16px">'
        f'AutoApply AI &nbsp;·&nbsp; gerado em {agora.strftime("%H:%M:%S")}</p></div>'
    )

    texto = txt_header + txt_sumario + txt_body + txt_footer
    html  = html_header + html_sumario + html_body + html_footer

    return texto, html


# ===========================================================================
# ENVIO PELO GMAIL API
# ===========================================================================

def enviar_briefing(service, texto: str, html: str, destinatario: str = "me"):
    """Envia o briefing como e-mail para o próprio Gmail usando a API."""
    agora = datetime.datetime.now()
    assunto = f"[AutoApply AI] Briefing Matinal — {agora.strftime('%d/%m/%Y')}"

    # Descobre o endereço real do usuário ("me")
    try:
        perfil = service.users().getProfile(userId="me").execute()
        email_usuario = perfil.get("emailAddress", "me")
    except Exception:
        email_usuario = destinatario

    msg = MIMEMultipart("alternative")
    msg["Subject"] = assunto
    msg["From"]    = email_usuario
    msg["To"]      = email_usuario if destinatario == "me" else destinatario

    msg.attach(MIMEText(texto, "plain",  "utf-8"))
    msg.attach(MIMEText(html,  "html",   "utf-8"))

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    try:
        service.users().messages().send(
            userId="me",
            body={"raw": raw},
        ).execute()
        print(f"✅ Briefing enviado para: {msg['To']}")
        return True
    except HttpError as e:
        print(f"❌ Falha ao enviar briefing: {e}")
        return False


# ===========================================================================
# PONTO DE ENTRADA
# ===========================================================================

def main():
    parser = argparse.ArgumentParser(
        description="AutoApply AI — briefing matinal",
        formatter_class=argparse.RawTextHelpFormatter,
    )
    parser.add_argument(
        "--dias", type=int, default=1,
        help="Janela em dias (padrão: 1 = últimas 24h)",
    )
    parser.add_argument(
        "--preview", action="store_true",
        help="Apenas imprime no terminal, não envia o e-mail",
    )
    parser.add_argument(
        "--para", type=str, default="me",
        help="Endereço de destino (padrão: próprio Gmail)",
    )
    args = parser.parse_args()

    print(f"\n{'='*54}")
    print(f"  AutoApply AI — Briefing Matinal")
    print(f"  {datetime.datetime.now().strftime('%d/%m/%Y %H:%M:%S')}  |  janela: {args.dias} dia(s)")
    print(f"{'='*54}\n")

    # Supabase
    sb = _criar_sb()

    # Gmail
    try:
        service = _autenticar_gmail()
    except FileNotFoundError as e:
        print(f"[ERRO] {e}")
        sys.exit(1)

    print("Coletando dados do Supabase...")
    vagas        = coletar_vagas(sb, args.dias)
    despesas     = coletar_despesas(sb, args.dias)
    alertas      = coletar_alertas(sb, args.dias)
    certificados = coletar_certificados(sb, args.dias)

    print("Coletando e-mails das labels do Gmail...")
    agendamentos = _listar_por_label(service, "AutoApply/Geral/Agendamento", args.dias)
    comunicados  = _listar_por_label(service, "AutoApply/Geral/Comunicado",  args.dias)

    print("Montando briefing...")
    texto, html = montar_briefing(
        vagas, despesas, alertas, certificados,
        agendamentos, comunicados, args.dias,
    )

    if args.preview:
        print(texto)
        print("\n[PREVIEW] Briefing não enviado (--preview ativo)")
        return

    print("Enviando por e-mail...")
    enviar_briefing(service, texto, html, args.para)


if __name__ == "__main__":
    main()