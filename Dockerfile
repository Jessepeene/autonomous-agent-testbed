FROM docker:cli AS docker-cli

FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    # Shared, world-readable browser location. Playwright's default is
    # $HOME/.cache/ms-playwright, which would install under /root during the build
    # and then be unreadable to the uid-10001 runtime user -- the browser would be
    # present in the image and still fail to launch.
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

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
# `[browser]` pulls in Playwright; `playwright install --with-deps chromium` adds
# Chromium plus its ~15 apt-level shared-object dependencies. Needed because the
# product reviewer (agents/roles.py::AUDITOR) has to actually *use* a live page --
# submit its forms, not just read its HTML. Run as root, before USER factory.
RUN pip install --no-cache-dir ".[browser]" \
    && playwright install --with-deps chromium \
    && chmod -R a+rX /ms-playwright \
    && rm -rf /var/lib/apt/lists/*

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
