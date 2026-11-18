FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /srv/backend
COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY backend/ /srv/backend/
COPY db/ /srv/db/
COPY features.yaml /srv/features.yaml
ENV MIGRATIONS_DIR=/srv/db/migrations FEATURES_MANIFEST=/srv/features.yaml PYTHONPATH=/srv/backend
RUN useradd --system --uid 10001 app && chown -R app /srv
USER app
EXPOSE 8000
# migrate (+ seed and first P&L run on an empty DB), then serve
CMD ["sh", "-c", "python -m app.migrate && exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --proxy-headers"]
