# AgentCrew - production image for the Streamlit UI.
#
#   docker build -t agentcrew .
#   docker run --rm -p 8080:8080 agentcrew
#
# Two stages. The builder has everything needed to *produce* the app (pip, the
# database build script); the runtime stage gets only the finished results.
# Nothing a stage leaves behind reaches the final image unless it is copied
# across explicitly.

# Pinned to an exact Python patch and Debian release so a rebuild next month
# gets the same interpreter; plain "3.12-slim" moves silently. Declared before
# the first FROM so both stages share one value: the venv's "python" is only a
# symlink to /usr/local/bin/python (see /opt/venv/pyvenv.cfg), so stage 2 must
# have the same interpreter at the same path or the copied venv breaks.
ARG PYTHON_IMAGE=python:3.12.14-slim-trixie


# =============================================================================
# Stage 1: builder
# =============================================================================

# "slim" rather than the full image: every dependency here ships a prebuilt
# manylinux wheel for Python 3.12, so no compiler or -dev headers are needed.
FROM ${PYTHON_IMAGE} AS builder

# No pip download cache (it would only bloat this stage's layers) and no
# "new pip available" check (an extra network call with nothing to act on).
ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# A virtualenv gives the dependencies one self-contained directory, which is
# what makes the multi-stage copy clean: stage 2 takes /opt/venv and nothing
# else from pip. Same path in both stages, because a venv hardcodes its path.
RUN python -m venv /opt/venv

# Putting the venv first on PATH means "pip" and "python" below are the venv's,
# with no need to "activate" (activation does not persist across RUN lines).
ENV PATH="/opt/venv/bin:${PATH}"

# All later relative paths resolve from here.
WORKDIR /app

# requirements.txt alone, BEFORE any source code. Docker caches each layer and
# reuses it while its inputs are unchanged, so editing app code skips the slow
# pip install below; only a change to requirements.txt re-runs it.
COPY requirements.txt .

# Exactly the pins in requirements.txt, including the Gemini SDK the demo runs
# on. Keeping every runtime pin in that one file means the image, CI and a
# local install cannot drift apart.
RUN pip install -r requirements.txt

# The database script imports agentcrew.config and agentcrew.db, so it needs
# the package. Copied after the install so code edits keep pip's cache hit.
COPY agentcrew/ agentcrew/
COPY scripts/build_database.py scripts/build_database.py

# Build the demo database here, not on your laptop: data/*.db is gitignored, so
# a build from a clean clone (or Cloud Build) would never see a local copy. The
# script is seeded, so the data matches the local database exactly. chmod 0444
# makes the file read-only for everyone, and COPY --from keeps that mode.
RUN python scripts/build_database.py \
 && chmod 0444 data/northstar.db


# =============================================================================
# Stage 2: runtime
# =============================================================================

# Fresh copy of the same base: no pip cache, no build script, no stage-1
# history. This is the only stage that ships.
FROM ${PYTHON_IMAGE} AS runtime

# Image metadata, readable with "docker inspect" and shown by registries.
LABEL org.opencontainers.image.title="AgentCrew" \
      org.opencontainers.image.description="Text-to-SQL analytics agent (LangGraph + Streamlit)" \
      org.opencontainers.image.source="https://github.com/mohdnayif799/agentic-text-to-sql" \
      org.opencontainers.image.licenses="MIT"

# PYTHONDONTWRITEBYTECODE: the app user cannot write into /app, so skip trying.
# PYTHONUNBUFFERED: logs reach "docker logs" / Cloud Logging immediately
#   instead of sitting in a buffer (and being lost if the container dies).
# PATH: use the venv copied from the builder.
# STREAMLIT_SERVER_HEADLESS: never try to open a browser, never prompt for an
#   email on first run - a prompt would block startup with no one to answer.
# STREAMLIT_BROWSER_GATHER_USAGE_STATS: no telemetry from a server deployment.
# STREAMLIT_SERVER_FILE_WATCHER_TYPE: the code never changes inside an image,
#   so watching it for hot-reload only costs CPU.
# PORT: default 8080. Cloud Run injects its own PORT, which overrides this.
# AGENTCREW_PROVIDER / AGENTCREW_MODEL: the provider and model the UI starts
#   on. Flash-lite because its free daily quota is far larger than Flash's and
#   one agent run makes about 6 model calls. Not secrets, so safe to bake in;
#   the API key itself is only ever supplied at runtime.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:${PATH}" \
    STREAMLIT_SERVER_HEADLESS=true \
    STREAMLIT_BROWSER_GATHER_USAGE_STATS=false \
    STREAMLIT_SERVER_FILE_WATCHER_TYPE=none \
    PORT=8080 \
    AGENTCREW_PROVIDER=gemini \
    AGENTCREW_MODEL=gemini-3.5-flash-lite

# A dedicated unprivileged user. If an attacker ever gets code execution in the
# app, they land as a user who cannot modify the code, the venv, or the
# database - all of which stay owned by root. A fixed, high UID/GID (10001)
# cannot collide with an account that already exists in the base image, and
# stays stable if the image is rebuilt. A home directory is created because
# Streamlit looks for per-user config under ~/.streamlit. The login shell is
# disabled because no one should ever log in as this account.
# data/traces is the one path the app writes to (a JSONL trace per run), so it
# is the only directory handed to the app user.
RUN groupadd --gid 10001 app \
 && useradd --uid 10001 --gid app --create-home --home-dir /home/app \
        --shell /usr/sbin/nologin app \
 && mkdir -p /app/data/traces \
 && chown app:app /app/data/traces

WORKDIR /app

# Layers ordered from least to most frequently changed: dependencies, then the
# database, then the source. A code edit does re-run stage 1's ~1 s database
# build, but the output is byte-identical and BuildKit caches COPY --from by
# file *content*, so these two layers stay cached and only the source COPYs
# below re-run (measured: ~7 s rebuild after a code change).
# All of it is owned by root (COPY's default), so the app user can read it
# but not change it.
COPY --from=builder /opt/venv /opt/venv
COPY --from=builder /app/data/northstar.db data/northstar.db

# Only what runs: the package, the entry point and the Streamlit theme. Tests,
# scripts, eval data and docs stay out, and .dockerignore keeps secrets out of
# the context itself. The theme has to be copied explicitly: without it the
# app still starts, just silently unstyled.
COPY agentcrew/ agentcrew/
COPY .streamlit/ .streamlit/
COPY app.py .

# Drop root for everything that follows, including the running app. Numeric
# IDs let Kubernetes-style runtimes check "runAsNonRoot" without reading
# /etc/passwd.
USER 10001:10001

# Documents the default port. It does not publish anything: "docker run -p"
# does that locally, and Cloud Run routes to $PORT on its own.
EXPOSE 8080

# Lets "docker ps" show healthy/unhealthy. Streamlit serves /_stcore/health.
# Python's urllib is used because slim images ship neither curl nor wget.
# Shell form here on purpose, so ${PORT} is expanded when the check runs.
# (Cloud Run ignores HEALTHCHECK and uses its own startup probe.)
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request as u; u.urlopen('http://127.0.0.1:${PORT}/_stcore/health', timeout=4)" || exit 1

# Why not the plain exec form, CMD ["streamlit", ..., "--server.port=${PORT}"]?
#   No shell runs it, so "${PORT}" would reach Streamlit as literal text.
# Why not the shell form, CMD streamlit run ... ?
#   /bin/sh becomes PID 1 and does not forward SIGTERM, so "docker stop" and
#   Cloud Run scale-down would wait out the grace period and then SIGKILL.
# So: exec form that starts a shell explicitly (the shell expands ${PORT}),
# then "exec" replaces the shell with Streamlit. Streamlit becomes PID 1 and
# receives SIGTERM directly. 0.0.0.0 = listen on every interface, not just
# loopback: traffic from "docker run -p" or Cloud Run arrives on the
# container's network interface, never on 127.0.0.1. Stated explicitly rather
# than left to Streamlit's default.
CMD ["sh", "-c", "exec streamlit run app.py --server.address=0.0.0.0 --server.port=${PORT}"]
