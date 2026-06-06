FROM python:3.12-slim

WORKDIR /app

RUN addgroup --system exporter && adduser --system --ingroup exporter exporter

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY google_tasks_export.py .

RUN mkdir -p /app/secrets && chown -R exporter:exporter /app

USER exporter

ENV GOOGLE_OAUTH_CREDENTIALS=/app/secrets/credentials.json \
    TOKEN_PATH=/app/secrets/token.json \
    EXPORT_FOLDER_NAME="Google Tasks Exports"

ENTRYPOINT ["python", "-u", "google_tasks_export.py"]
