# Galaxy Scanner — Container App Job image
# Base is imported to our ACR from MCR (Docker Hub blocked by corporate proxy).

FROM galaxyscannercrd63cdd.azurecr.io/devcontainers-python:3.13 AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Base image already has ca-certificates + python 3.13 + build tools.
# apt-get is skipped: corporate proxy blocks deb.debian.org.

COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

# Copy only the source files the scanner needs.
COPY nhi_identity.py  run_tracer.py  token_provider.py  trace_ledger.py  run_scanner.py  ./
COPY agents/            ./agents/
COPY governance/        ./governance/
COPY infra/             ./infra/

# Devcontainers base ships with non-root user `vscode` (UID 1000).
RUN chown -R vscode:vscode /app
USER vscode

# Default entrypoint: self-scan. Override --repo / --run-id / --module-id at job start.
ENTRYPOINT ["python", "run_scanner.py"]
CMD ["--repo", "/app", "--run-id", "run-azure-smoke-001", "--module-id", "self-scan"]
