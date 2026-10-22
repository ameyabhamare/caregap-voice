-- Runs once on an empty data volume. The app database (POSTGRES_DB) already exists.
-- MLflow and HAPI FHIR each keep their store in their own database on the same server.
CREATE DATABASE mlflow;
CREATE DATABASE hapi;
