"""
calendario.py — AutoApply AI
=============================
Leva para o Google Calendar o que tem data nos e-mails:

  - vencimentos (boletos, faturas, IPVA, licenciamento, multas) → evento de
    dia inteiro na data de vencimento, com lembrete LEMBRETE_DIAS_ANTES antes
  - agendamentos (consulta, barbearia, reserva) → evento no horário marcado
    (1h de duração), com lembrete 1h antes; sem horário vira dia inteiro

Configuração (.env):
  GOOGLE_CALENDAR_ENABLED=true   liga o recurso (o navegador abre 1x para
                                 autorizar o escopo do Calendar)
  CALENDAR_ID=primary            agenda de destino
  LEMBRETE_DIAS_ANTES=2          antecedência do lembrete de vencimentos
"""

import datetime
import os
import re

from dotenv import load_dotenv

load_dotenv()

CALENDAR_ID          = os.getenv("CALENDAR_ID", "primary")
LEMBRETE_DIAS_ANTES  = int(os.getenv("LEMBRETE_DIAS_ANTES", "2"))
DURACAO_PADRAO_MIN   = 60
TIMEZONE             = os.getenv("TZ") or "America/Sao_Paulo"

_MESES = {
    "janeiro": 1, "fevereiro": 2, "março": 3, "marco": 3, "abril": 4, "maio": 5, "junho": 6,
    "julho": 7, "agosto": 8, "setembro": 9, "outubro": 10, "novembro": 11, "dezembro": 12,
    "jan": 1, "fev": 2, "mar": 3, "abr": 4, "mai": 5, "jun": 6, "jul": 7, "ago": 8,
    "set": 9, "out": 10, "nov": 11, "dez": 12,
}


def _ano_provavel(dia: int, mes: int, hoje: datetime.date) -> int:
    """Sem ano no texto: escolhe o ano que deixa a data mais perto de hoje (dez → jan vira o ano)."""
    candidatos = []
    for ano in (hoje.year - 1, hoje.year, hoje.year + 1):
        try:
            candidatos.append(datetime.date(ano, mes, dia))
        except ValueError:
            continue
    if not candidatos:
        return hoje.year
    return min(candidatos, key=lambda d: abs((d - hoje).days)).year


def parsear_data_hora(texto: str | None, hoje: datetime.date | None = None
                      ) -> tuple[datetime.date | None, datetime.time | None]:
    """
    Extrai (data, hora) de textos como:
      '2026-06-29', '29/06/2026', '29/06 às 14h', '28/06 14:30',
      '29 de junho de 2026', 'sábado, 5 de julho às 9h30'
    Retorna (None, None) quando não reconhece uma data.
    """
    if not texto:
        return None, None
    hoje = hoje or datetime.date.today()
    t = str(texto).lower()

    data, fim = None, 0
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", t)
    if m:
        a, mo, d = (int(x) for x in m.groups())
        data, fim = _data_segura(a, mo, d), m.end()
    if not data:
        m = re.search(r"\b(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\b", t)
        if m:
            d, mo = int(m.group(1)), int(m.group(2))
            a = int(m.group(3)) if m.group(3) else _ano_provavel(d, mo, hoje)
            data, fim = _data_segura(a + 2000 if a < 100 else a, mo, d), m.end()
    if not data:
        m = re.search(r"\b(\d{1,2})\s+de\s+([a-zç]+)(?:\s+de\s+(\d{4}))?", t)
        if m and m.group(2) in _MESES:
            d, mo = int(m.group(1)), _MESES[m.group(2)]
            a = int(m.group(3)) if m.group(3) else _ano_provavel(d, mo, hoje)
            data, fim = _data_segura(a, mo, d), m.end()
    if not data:
        return None, None
    return data, _hora_depois_da_data(t[fim:])


def _hora_depois_da_data(resto: str) -> datetime.time | None:
    """
    Hora só é aceita em formatos inequívocos depois da data: "14:30",
    "T14:30", "às 9h30", "das 14h" ou "14h" logo em seguida à data. Assim
    "duração 2h" não vira 02:00. Meia-noite (00:00) é tratada como "sem
    horário" — é como a IA escreve uma data pura em ISO.
    """
    padroes = [
        r"^t?\s*(\d{1,2}):(\d{2})",                        # T14:30 / " 14:30"
        r"^[\s,–-]*(\d{1,2})h(\d{2})?\b",                    # "28/10 14h"
        r"\b(?:às|as|das|a partir das)\s+(\d{1,2})(?:h(\d{2})?\b|:(\d{2}))",
        r"\b(\d{1,2}):(\d{2})\b",
    ]
    for padrao in padroes:
        m = re.search(padrao, resto)
        if not m:
            continue
        grupos = [g for g in m.groups() if g is not None]
        h = int(grupos[0])
        mi = int(grupos[1]) if len(grupos) > 1 else 0
        if 0 <= h <= 23 and 0 <= mi <= 59 and (h, mi) != (0, 0):
            return datetime.time(h, mi)
        return None
    return None


def _data_segura(a: int, mo: int, d: int) -> datetime.date | None:
    try:
        return datetime.date(a, mo, d)
    except ValueError:
        return None


def montar_evento(titulo: str, data: datetime.date, hora: datetime.time | None = None,
                  descricao: str = "", lembrete_dias: int | None = None) -> dict:
    """Corpo do evento no formato da Calendar API v3."""
    evento = {"summary": titulo[:250], "description": descricao[:4000]}
    if hora:
        inicio = datetime.datetime.combine(data, hora)
        fim = inicio + datetime.timedelta(minutes=DURACAO_PADRAO_MIN)
        evento["start"] = {"dateTime": inicio.isoformat(), "timeZone": TIMEZONE}
        evento["end"] = {"dateTime": fim.isoformat(), "timeZone": TIMEZONE}
        minutos = 60
    else:
        evento["start"] = {"date": data.isoformat()}
        evento["end"] = {"date": (data + datetime.timedelta(days=1)).isoformat()}
        # Em evento de dia inteiro o lembrete conta a partir da meia-noite do dia;
        # "N dias antes às 9h" = N*24h - 9h
        dias = LEMBRETE_DIAS_ANTES if lembrete_dias is None else lembrete_dias
        minutos = max(0, dias * 24 * 60 - 9 * 60)
    evento["reminders"] = {"useDefault": False, "overrides": [{"method": "popup", "minutes": minutos}]}
    return evento


# Só o que é A PAGAR vira "Vence:" — recibo, PIX enviado ou pedágio já
# cobrado não (a não ser que a IA diga que o e-mail pede ação).
_SUBCATEGORIAS_A_PAGAR = {
    "boleto_fatura", "cobranca",
    "veiculo_ipva", "veiculo_licenciamento", "veiculo_multa", "veiculo_seguro", "veiculo_cnh",
}


def evento_do_registro(registro: dict, hoje: datetime.date | None = None) -> dict | None:
    """
    Converte um e-mail geral já classificado em evento, ou None se não houver
    data aproveitável. Só agenda o que ainda não passou.
    """
    categoria = registro.get("categoria")
    if categoria not in ("agendamento", "financeiro", "veiculo"):
        return None
    if (categoria != "agendamento" and registro.get("subcategoria") not in _SUBCATEGORIAS_A_PAGAR
            and not registro.get("requer_acao")):
        return None
    hoje = hoje or datetime.date.today()
    data, hora = parsear_data_hora(registro.get("data_evento"), hoje)
    if not data or data < hoje:
        return None

    nome = registro.get("remetente_nome") or "E-mail"
    linhas = [registro.get("resumo") or registro.get("assunto") or ""]
    if registro.get("link_principal"):
        linhas.append(registro["link_principal"])
    linhas.append("Criado pelo AutoApply AI a partir de um e-mail.")
    descricao = "\n\n".join(l for l in linhas if l)

    if categoria == "agendamento":
        return montar_evento(f"📅 {nome}", data, hora, descricao)

    valor = registro.get("valor_formatado")
    titulo = f"{'🚗' if categoria == 'veiculo' else '💰'} Vence: {nome}" + (f" — {valor}" if valor else "")
    return montar_evento(titulo, data, None, descricao)


def criar_evento(service, evento: dict) -> str | None:
    criado = service.events().insert(calendarId=CALENDAR_ID, body=evento).execute()
    return criado.get("id")
