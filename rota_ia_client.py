"""
rota_ia_client.py — AutoApply AI
==================================
Cliente HTTP central para o gateway local Rota.IA
(https://github.com/lucascabralds/Rota-ia).

Antes o projeto chamava o SDK do Gemini diretamente. Agora TODA chamada de
IA do pipeline passa pelo endpoint único do Rota.IA, que fala o formato
OpenAI-compatible (`/v1/chat/completions`) e decide, por trás, qual provedor
cadastrado (OpenAI, Gemini, Claude, Groq, DeepSeek, etc.) atende — trocando
sozinho de provedor se algum falhar.

Configuração (.env):
  ROTA_IA_URL      — endpoint base do gateway (padrão: http://localhost:8787/v1)
  ROTA_IA_API_KEY  — a INTERNAL_API_KEY gerada pelo Rota.IA na primeira execução
                      (aparece no terminal ao rodar `npm run start`, e também
                      na aba "Documentação da API" do painel)
  ROTA_IA_MODEL    — "auto" (padrão) usa a fila de fallback cadastrada no painel,
                      ou force um provedor específico com o id/name dele
"""

import json
import os
import re
import time
import logging

import requests
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuração do gateway
# ---------------------------------------------------------------------------
ROTA_IA_URL     = (os.getenv("ROTA_IA_URL") or "http://localhost:8787/v1").rstrip("/")
ROTA_IA_API_KEY = os.getenv("ROTA_IA_API_KEY")
ROTA_IA_MODEL   = os.getenv("ROTA_IA_MODEL", "auto")

CHAT_COMPLETIONS_ENDPOINT = f"{ROTA_IA_URL}/chat/completions"

# ---------------------------------------------------------------------------
# Circuit breaker: se o gateway não responde (Rota.IA desligado, porta
# errada...), cada e-mail esperava todas as tentativas e ainda gastava o
# budget diário. Quando uma chamada esgota as tentativas sem conseguir
# CONECTAR, as próximas falham na hora durante ROTA_IA_PAUSA_SEG segundos —
# e o pipeline usa a heurística local nesse intervalo. Timeout (provedor
# lento, mas no ar) não pausa: um digest grande não pode calar os alertas.
#
# A pausa fica gravada em disco: o agendador abre um processo novo a cada
# minuto, e uma pausa só em memória seria esquecida a cada execução.
# ---------------------------------------------------------------------------
PAUSA_SEG    = int(os.getenv("ROTA_IA_PAUSA_SEG", "300"))
_DADOS_DIR   = os.getenv("AUTOAPPLY_DADOS_DIR") or os.path.dirname(os.path.abspath(__file__))
_PAUSA_PATH  = os.path.join(_DADOS_DIR, "rota_ia_pausa.json")

_cache_pausa: tuple[float, float] = (-1.0, 0.0)   # (mtime do arquivo, pausado_ate)


class GatewayIndisponivel(RuntimeError):
    """O Rota.IA não respondeu nas últimas chamadas; nova tentativa só após a pausa."""


def _pausado_ate() -> float:
    """Lê a pausa do disco só quando o arquivo muda (é consultada várias vezes por e-mail)."""
    global _cache_pausa
    try:
        mtime = os.path.getmtime(_PAUSA_PATH)
    except OSError:
        return 0.0
    if mtime != _cache_pausa[0]:
        try:
            with open(_PAUSA_PATH, encoding="utf-8") as f:
                _cache_pausa = (mtime, float(json.load(f).get("pausado_ate", 0)))
        except (OSError, ValueError, AttributeError):
            _cache_pausa = (mtime, 0.0)
    return _cache_pausa[1]


def _gravar_pausa(ate: float) -> None:
    try:
        if ate:
            with open(_PAUSA_PATH, "w", encoding="utf-8") as f:
                json.dump({"pausado_ate": ate}, f)
        elif os.path.exists(_PAUSA_PATH):
            os.remove(_PAUSA_PATH)
    except OSError:
        pass


def gateway_disponivel() -> bool:
    """False enquanto o circuit breaker estiver aberto (gateway fora do ar)."""
    return time.time() >= _pausado_ate()


def _registrar_falha_de_conexao() -> None:
    _gravar_pausa(time.time() + PAUSA_SEG)
    logger.warning(
        f"Rota.IA não responde em {ROTA_IA_URL} — pausando chamadas de IA por "
        f"{PAUSA_SEG}s (o pipeline segue com heurísticas locais)."
    )


def _registrar_sucesso() -> None:
    if _pausado_ate():
        _gravar_pausa(0)


def _extrair_retry_delay(texto: str) -> float:
    """Lê o tempo de espera sugerido em erros 429/503, quando disponível."""
    m = re.search(r"'retryDelay':\s*'([0-9.]+)(ms|s)'", texto)
    if m:
        v, u = float(m.group(1)), m.group(2)
        return (v / 1000.0) if u == "ms" else v
    m2 = re.search(r"retry in ([0-9.]+)s", texto)
    if m2:
        return float(m2.group(1))
    return 15.0


def chamar_ia(prompt: str, max_tentativas: int = 3, temperature: float = 0.1,
              timeout: int = 60) -> str:
    """
    Envia o prompt para o gateway Rota.IA (POST {ROTA_IA_URL}/chat/completions)
    e retorna o texto de resposta do modelo escolhido pela fila de fallback.

    Faz retry automático em 429 (rate limit) e 503 (provedor indisponível).
    Levanta a última exceção se todas as tentativas falharem.
    """
    if not gateway_disponivel():
        raise GatewayIndisponivel(f"Rota.IA indisponível em {ROTA_IA_URL} (pausa ativa)")

    if not ROTA_IA_API_KEY:
        raise ValueError(
            "ROTA_IA_API_KEY não encontrada no .env — inicie o gateway Rota.IA "
            "(cd rota-ia && npm run start) e copie a INTERNAL_API_KEY exibida "
            "no terminal (ou na aba 'Documentação da API' do painel)."
        )

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {ROTA_IA_API_KEY}",
    }
    payload = {
        "model": ROTA_IA_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": temperature,
    }

    ultima_exc = None
    for tentativa in range(1, max_tentativas + 1):
        try:
            resp = requests.post(
                CHAT_COMPLETIONS_ENDPOINT,
                headers=headers,
                json=payload,
                timeout=timeout,
            )

            if resp.status_code in (429, 503):
                if tentativa == max_tentativas:
                    resp.raise_for_status()
                espera = _extrair_retry_delay(resp.text)
                logger.warning(
                    f"Rota.IA {resp.status_code} — aguardando {espera:.1f}s "
                    f"antes da tentativa {tentativa + 1}/{max_tentativas}..."
                )
                time.sleep(espera + 1)
                continue

            resp.raise_for_status()
            # Lê o conteúdo ANTES de registrar sucesso: um 200 com {"error": ...}
            # (sem "choices") é falha, não resposta da IA.
            conteudo = resp.json()["choices"][0]["message"]["content"]
            _registrar_sucesso()
            return conteudo

        except (requests.ConnectionError, requests.Timeout) as exc:
            ultima_exc = exc
            if tentativa == max_tentativas:
                # ReadTimeout = conectou mas o provedor demorou; ConnectTimeout
                # (subclasse de ConnectionError) = gateway inalcançável → pausa
                if not isinstance(exc, requests.ReadTimeout):
                    _registrar_falha_de_conexao()
                break
            logger.warning(
                f"Rota.IA — sem conexão (tentativa {tentativa}/{max_tentativas}): {exc}"
            )
            time.sleep(2)

        except requests.RequestException as exc:
            ultima_exc = exc
            if tentativa == max_tentativas:
                break
            logger.warning(
                f"Rota.IA — falha de conexão (tentativa {tentativa}/{max_tentativas}): {exc}"
            )
            time.sleep(2)

    raise ultima_exc


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")
    print(f"Endpoint configurado: {CHAT_COMPLETIONS_ENDPOINT}")
    print(f"Modelo: {ROTA_IA_MODEL}")
    resposta = chamar_ia("Responda apenas com o JSON: {\"ok\": true}")
    print("Resposta:", resposta)
