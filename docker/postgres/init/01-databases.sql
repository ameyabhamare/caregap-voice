-- Runs once on an empty data volume. The app database (POSTGRES_DB) already exists.
-- MLflow keeps its tracking store in its own database on the same server.
CREATE DATABASE mlflow;
