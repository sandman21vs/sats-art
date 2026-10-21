FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATA_DIR=/data

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY motor ./motor
COPY servidor ./servidor
COPY static ./static

RUN useradd --create-home --uid 1000 app && mkdir -p /data && chown app /data
USER app
VOLUME /data
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/', timeout=4)"

# Um único processo (limitadores em memória). Conexões ociosas são baratas: o limite de
# concorrência é alto e o corpo lento é barrado por pessoa no app e bufferizado no Caddy. O IP real atrás do proxy é tratado pelo
# app (TRUST_PROXY/TRUSTED_PROXIES); o uvicorn não reescreve nada a partir de cabeçalhos.
CMD ["uvicorn", "servidor.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-proxy-headers", "--no-server-header", "--no-access-log", "--workers", "1", "--limit-concurrency", "1000"]
