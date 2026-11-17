FROM python:3.14-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY --chown=1000:1000 audit_console.py quotas.py management.py usage.py model_discovery.py index.html app.js console.css ./
USER 1000:1000
EXPOSE 8787
CMD ["python", "audit_console.py"]
