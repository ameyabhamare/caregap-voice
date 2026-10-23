FROM python:3.12.15-slim-trixie
# Pinned. MLflow 3.x provides the prompt registry and tracing used in later phases.
# Optional extra CA as a build secret (see docker/core/Dockerfile).
RUN --mount=type=secret,id=extra_ca \
    if [ -s /run/secrets/extra_ca ]; then export PIP_CERT=/run/secrets/extra_ca; fi; \
    pip install --no-cache-dir "mlflow==3.16.1" "psycopg[binary]==3.3.6"
EXPOSE 5000
