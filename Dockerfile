# syntax=docker/dockerfile:1
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY requirements.txt ./
RUN --mount=type=secret,id=proxy_ca \
    if [ -s /run/secrets/proxy_ca ]; then \
        PIP_CERT=/run/secrets/proxy_ca python -m pip install --no-cache-dir -r requirements.txt; \
    else \
        python -m pip install --no-cache-dir -r requirements.txt; \
    fi \
    && useradd --create-home --uid 1000 paperbot \
    && mkdir -p /data /backups \
    && chown -R paperbot:paperbot /app /data /backups

COPY --chown=paperbot:paperbot *.py ./

USER paperbot

VOLUME ["/data", "/backups"]

CMD ["python", "futures_paper.py"]
