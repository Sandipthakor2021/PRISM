# Prism gateway image. The same image runs three containers (gateway, mock alpha, mock beta);
# docker-compose.yml chooses the command for each.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# dependencies first, so code changes don't invalidate the cached install layer
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

# run as a non-root user; /app/state holds the SQLite database (mounted as a volume by compose)
RUN useradd --create-home --uid 10001 prism \
    && mkdir -p /app/state \
    && chown -R prism:prism /app/state
USER prism

ENV PRISM_DB=/app/state/prism.sqlite3
EXPOSE 8080

HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=5 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8080/health', timeout=2).status == 200 else 1)"

CMD ["python", "-m", "uvicorn", "prism.app:app", "--host", "0.0.0.0", "--port", "8080"]
