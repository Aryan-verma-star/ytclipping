# YouTube Clipper — production image (Render free web service).
# Build context = repository root (contains backend/ and frontend/).
FROM python:3.12-slim

# ffmpeg: the trimming engine. fonts-dejavu-core: drawtext for the dev/test
# sample provider. Both from Debian repos — no paid services involved.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app/backend

COPY backend/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/ ./
COPY frontend/ /app/frontend/

# Ephemeral clip/tmp storage on the container filesystem.
# Metadata lives in the external database (Neon) and survives redeploys.
RUN mkdir -p /data
ENV PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/backend \
    CLIPPER_DATA_DIR=/data \
    CLIPPER_FRONTEND_DIR=/app/frontend \
    CLIPPER_ENVIRONMENT=production

EXPOSE 10000

# Render reaches Docker services on the port it detects; we bind $PORT
# (falling back to Render's documented default of 10000).
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-10000}"]
