FROM mwader/static-ffmpeg:9.0.2-1@sha256:f5d100184884eb3e82b0eb14bb5ebf98da99826ac824989720297fb9ef21c822 AS media-runtime

FROM python:3.12-slim-bookworm
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates fontconfig fonts-dejavu-core libfribidi0 libgomp1 libharfbuzz0b \
    && rm -rf /var/lib/apt/lists/*
COPY --from=media-runtime /ffmpeg /ffprobe /usr/local/bin/
WORKDIR /app
COPY requirements.txt .
RUN python -m pip install --no-cache-dir --only-binary=:all: -r requirements.txt
COPY factory/ factory/
COPY docs/ docs/
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt
CMD ["python", "-m", "factory", "--data", "/data", "--listen", "0.0.0.0", "--port", "8765"]
