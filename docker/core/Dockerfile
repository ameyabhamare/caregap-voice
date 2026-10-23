# Core service image; also runs the Twilio mock. Build context is the repo root.
FROM python:3.12.15-slim-trixie

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    UV_PROJECT_ENVIRONMENT=/opt/venv UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_NO_CACHE=1
WORKDIR /app

# An optional extra CA (for builds behind a TLS-inspecting proxy) arrives as a build
# secret, so it never lands in an image layer. Empty by default.
RUN --mount=type=secret,id=extra_ca \
    if [ -s /run/secrets/extra_ca ]; then export PIP_CERT=/run/secrets/extra_ca; fi; \
    pip install --no-cache-dir "uv==0.11.32"

# Dependencies first (cached unless the lock changes). No dev or PySpark groups.
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=secret,id=extra_ca \
    if [ -s /run/secrets/extra_ca ]; then \
      cat /etc/ssl/certs/ca-certificates.crt /run/secrets/extra_ca > /tmp/ca.pem; \
      export SSL_CERT_FILE=/tmp/ca.pem; fi; \
    uv sync --frozen --no-dev --no-group pipeline --no-install-project && rm -f /tmp/ca.pem

COPY src ./src
COPY voice ./voice
# Customer configs are validated against the prompt files at startup.
COPY customers ./customers
COPY prompts ./prompts
# Installing the project itself fetches its build backend (hatchling).
RUN --mount=type=secret,id=extra_ca \
    if [ -s /run/secrets/extra_ca ]; then \
      cat /etc/ssl/certs/ca-certificates.crt /run/secrets/extra_ca > /tmp/ca.pem; \
      export SSL_CERT_FILE=/tmp/ca.pem; fi; \
    uv sync --frozen --no-dev --no-group pipeline --no-editable && rm -f /tmp/ca.pem

ENV PATH=/opt/venv/bin:$PATH
EXPOSE 8000
CMD ["uvicorn", "--factory", "caregap.api.app:create_app", "--host", "0.0.0.0", "--port", "8000"]
