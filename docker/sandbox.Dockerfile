# Pre-baked sandbox image for the Development Engine.
#
# Every generated app (per `scaffold/generator.py`'s SYSTEM_PROMPT) is a FastAPI
# backend with Stripe Checkout billing, token-based auth with password hashing, and a
# pytest suite. This image pre-installs exactly that stack so a typical build needs no
# runtime `pip install` at all — the difference between "the sandbox usually needs no
# network to run a build" and "every single build pip-installs from scratch, at the
# mercy of whatever the package registry is doing that second."
#
# Deliberately Python-only: the codegen contract explicitly specifies a plain
# HTML/CSS/JS frontend with "no build step", so there is nothing here for Node to do.
# Add it only if that contract changes.
#
# A `requirements.txt` package not in this list is still installed at container run
# time — network is explicitly enabled for that one step (see
# `development/graph.py::_verify_and_ship`) — this image narrows how often that path
# is needed, it does not remove it, since a generated app is free to depend on
# anything.
#
# Build and publish once, then point SANDBOX_IMAGE at the tag you pushed:
#   docker build -f docker/sandbox.Dockerfile -t your-registry/factory-sandbox:latest .
#   docker push your-registry/factory-sandbox:latest
#   # in .env: SANDBOX_IMAGE=your-registry/factory-sandbox:latest
#
# This image is never granted any privilege of its own — every hardening flag
# (--network none, --cap-drop ALL, --read-only, --user 10001:10001, --pids-limit,
# memory/cpu caps) is applied by `sandbox/docker_runner.py` at `docker run` time,
# regardless of which image is named here.

FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

RUN pip install --no-cache-dir \
    fastapi \
    "uvicorn[standard]" \
    pydantic \
    pydantic-settings \
    sqlalchemy \
    stripe \
    httpx \
    "passlib[bcrypt]" \
    "python-jose[cryptography]" \
    python-multipart \
    pytest \
    pytest-asyncio

# Matches the sandbox's fixed `--user 10001:10001` (see docker_runner.py). Not
# strictly required to run as that UID, but some tooling calls getpwuid() and warns
# or misbehaves without a matching passwd entry — cheap to add, avoids the surprise.
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin sandbox

WORKDIR /workspace
