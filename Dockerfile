FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN apt-get update && apt-get install -y --no-install-recommends poppler-utils util-linux age ca-certificates && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir "google-auth[requests]>=2.30,<3" "Pillow>=12,<14"
WORKDIR /app
COPY server.py drive_backup.py ai_api.py mcp_oauth.py discord_audit.py pictures.py ./
COPY migrations ./migrations
COPY static ./static
RUN addgroup --system app && adduser --system --ingroup app app \
    && mkdir -p /data && chown app:app /data
USER app
EXPOSE 8080
CMD ["python", "server.py"]
