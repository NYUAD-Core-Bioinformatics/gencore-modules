FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    GUNICORN_CMD_ARGS="--bind=0.0.0.0:8000 --workers=2 --threads=4 --timeout=60 --access-logfile=- --error-logfile=-"

WORKDIR /app

RUN addgroup --system --gid 10001 gencore \
    && adduser --system --uid 10001 --ingroup gencore --home /app gencore

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY --chown=gencore:gencore app.py tasks.py catalog.py conda_registry.py database.py hpc_client.py ./
COPY --chown=gencore:gencore catalog ./catalog
COPY --chown=gencore:gencore templates ./templates
COPY --chown=gencore:gencore static ./static

USER gencore

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3)"]

CMD ["gunicorn", "app:app"]
