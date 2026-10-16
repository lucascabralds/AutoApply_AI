"""
notificador.py — AutoApply AI
==============================
Notificações push via bot do Telegram (item 3 do roadmap).

Usado para:
  - OTPs / alertas de login recentes → chegam no celular na hora
  - Vagas de alta prioridade (score ≥ 75)

Configuração (.env) — se não estiver preenchida, tudo vira no-op:
  TELEGRAM_BOT_TOKEN — token do bot criado no @BotFather
  TELEGRAM_CHAT_ID   — seu chat id (mande uma mensagem ao bot e abra
                       https://api.telegram.org/bot<TOKEN>/getUpdates)

Teste rápido:
    python notificador.py "mensagem de teste"
"""

import logging
import os
import sys

import requests
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "").strip()


def telegram_configurado() -> bool:
    return bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)


def escapar_html(texto: str) -> str:
    """Escapa o mínimo exigido pelo parse_mode=HTML do Telegram."""
    return (texto or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def enviar_telegram(texto_html: str, timeout: int = 10) -> bool:
    """
    Envia uma mensagem (HTML simples: <b>, <i>, <code>, <a>) para o chat
    configurado. Nunca levanta exceção — notificação não pode derrubar o
    pipeline. Retorna True se o Telegram aceitou a mensagem.
    """
    if not telegram_configurado():
        return False
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": texto_html[:4000],
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=timeout,
        )
        if resp.ok:
            return True
        logger.warning(f"Telegram recusou a mensagem ({resp.status_code}): {resp.text[:200]}")
    except requests.RequestException as e:
        logger.warning(f"Falha ao enviar para o Telegram: {e}")
    return False


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")
    if not telegram_configurado():
        print("Preencha TELEGRAM_BOT_TOKEN e TELEGRAM_CHAT_ID no .env.")
        sys.exit(1)
    texto = " ".join(sys.argv[1:]) or "✅ AutoApply AI conectado ao Telegram."
    print("Enviado!" if enviar_telegram(escapar_html(texto)) else "Falhou — veja o log acima.")
