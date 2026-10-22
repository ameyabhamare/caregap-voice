FROM python:3.12-slim
# Pinned. MLflow 3.x provides the prompt registry and tracing used in later phases.
RUN pip install --no-cache-dir "mlflow==3.16.1" "psycopg[binary]==3.3.6"
EXPOSE 5000
