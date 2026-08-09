FROM docker:cli AS docker-cli

FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# docker-cli only: the factory drives the host daemon to launch sibling sandboxes.
# Copied from the official docker:cli image rather than installed via apt: Debian
# trixie's `docker.io` package does not ship /usr/bin/docker at all (only
# dockerd/docker-proxy/docker-init) -- `dpkg -l` reports it installed, but the CLI
# binary is simply absent, so `docker_available()` silently reports false forever.
# The static binary from Docker's own image has no distro-repo dependency.
COPY --from=docker-cli /usr/local/bin/docker /usr/local/bin/docker

RUN apt-get update \
    && apt-get install -y --no-install-recommends git curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

COPY alembic.ini ./
COPY alembic ./alembic
COPY blacklist.txt ./blacklist.txt

RUN useradd --create-home --uid 10001 factory \
    && mkdir -p /app/workspaces \
    && chown -R factory:factory /app
USER factory

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://localhost:8000/health || exit 1

CMD ["python", "-m", "factory.main"]
