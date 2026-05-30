# Galaxy SDLC — Container App Job image
# All 18 agents share this image; AGENT_TYPE env var selects the handler at runtime.
# Base is imported to ACR from MCR (Docker Hub blocked by corporate proxy).

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

COPY core/        ./core/
COPY agents/      ./agents/
COPY governance/  ./governance/
COPY a2a/         ./a2a/
COPY scripts/     ./scripts/
COPY infra/       ./infra/

# Devcontainers base ships with non-root user `vscode` (UID 1000).
RUN chown -R vscode:vscode /app
USER vscode

# Entry point reads AGENT_TYPE env var to select which agent to run.
ENTRYPOINT ["python", "scripts/run_agent_job.py"]
