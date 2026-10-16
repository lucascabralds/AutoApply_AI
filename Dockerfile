FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    AUTOAPPLY_HEADLESS=true \
    TZ=America/Sao_Paulo

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Usuário sem privilégios; /app/dados guarda token, cache e budget entre reinícios
RUN useradd --create-home autoapply && mkdir -p /app/dados && chown -R autoapply /app
USER autoapply

ENV GMAIL_CREDENTIALS_PATH=/app/dados/credentials.json \
    GMAIL_TOKEN_PATH=/app/dados/token.json \
    AUTOAPPLY_DADOS_DIR=/app/dados

CMD ["python", "agendador.py"]
