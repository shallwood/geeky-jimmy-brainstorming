FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN apt-get update && apt-get install -y --no-install-recommends poppler-utils util-linux age ca-certificates && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY server.py drive_backup.py ./
COPY static ./static
RUN addgroup --system app && adduser --system --ingroup app app \
    && mkdir -p /data && chown app:app /data
USER app
EXPOSE 8080
CMD ["python", "server.py"]
