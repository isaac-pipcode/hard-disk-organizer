FROM python:3.12-slim

# Ferramentas livres de linha de comando usadas pela varredura:
#   mediainfo, exiftool, rclone  (via apt)  |  siegfried/sf (binario oficial)
RUN apt-get update && apt-get install -y --no-install-recommends \
        mediainfo libimage-exiftool-perl rclone curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Siegfried (identificacao de formato PRONOM). Ajuste a versao conforme releases.
ARG SF_VERSION=1.11.1
RUN curl -sSL -o /tmp/sf.tar.gz \
      "https://github.com/richardlehane/siegfried/releases/download/v${SF_VERSION}/siegfried_${SF_VERSION}-1_amd64.deb" \
      || true \
    && (dpkg -i /tmp/sf.tar.gz 2>/dev/null || echo "AVISO: instalar 'sf' manualmente se este passo falhar") \
    && (sf -update || true)

WORKDIR /app
COPY scanner/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY scanner/ /app/

# Painel de operacao local na porta 8080 (o profissional interno usa no navegador)
EXPOSE 8080
CMD ["uvicorn", "panel:app", "--host", "0.0.0.0", "--port", "8080"]
