# syntax=docker/dockerfile:1
#
# openflowbi/core - one image, many commands (docs/deployment-cf.md §2.1, §3).
# Wheels only: every third-party package installs from a prebuilt wheel, nothing
# is compiled, so the builder needs no gcc/headers and the runtime stage is just
# python:slim + a virtualenv.
#
# Build (behind Norton/corporate TLS interception, pass the CA bundle as a secret -
# never COPY it, or it lands in an image layer):
#   docker build --platform linux/amd64 --secret id=ca,src=<combined-ca-bundle.pem> -t openflowbi/core:dev .
# Build (no interception, e.g. CI):
#   docker build --platform linux/amd64 -t openflowbi/core:dev .

ARG PYTHON_IMAGE=python:3.12-slim-bookworm

# ---------------------------------------------------------------------------
FROM ${PYTHON_IMAGE} AS builder

# Docker Hub mirror of ghcr.io/astral-sh/uv (same image) - a stale ghcr.io login
# in Docker Desktop's credential store makes ghcr pulls fail with "denied".
COPY --from=docker.io/astral/uv:0.9.10 /uv /bin/uv

# No bytecode compilation: keeps the image smaller, at the cost of a slightly
# slower first import per container. Set UV_COMPILE_BYTECODE=1 to flip that.
ENV UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PYTHON=/usr/local/bin/python3 \
    UV_PROJECT_ENVIRONMENT=/app/.venv

WORKDIR /app

# 1. Third-party dependencies only. --no-build makes the build fail loudly if
#    any package would need compiling from an sdist (the wheels-only guarantee).
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=secret,id=ca,required=false \
    if [ -f /run/secrets/ca ]; then export SSL_CERT_FILE=/run/secrets/ca; fi; \
    uv sync --locked --no-dev --no-install-project --no-build

# 2. Our own package, non-editable so src/ is not needed at runtime. Builds the
#    project's own (pure-Python, hatchling) wheel, so no --no-build here.
#    README.md is read by hatchling for the package metadata.
COPY README.md ./
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=secret,id=ca,required=false \
    if [ -f /run/secrets/ca ]; then export SSL_CERT_FILE=/run/secrets/ca; fi; \
    uv sync --locked --no-dev --no-editable

# 3. Trim files nothing imports at runtime: pyarrow's C++ headers, Cython
#    sources and test suite, plus any stray bytecode caches.
RUN rm -rf /app/.venv/lib/python3.12/site-packages/pyarrow/include \
           /app/.venv/lib/python3.12/site-packages/pyarrow/src \
           /app/.venv/lib/python3.12/site-packages/pyarrow/tests \
 && find /app/.venv -type f \( -name '*.pyx' -o -name '*.pxd' -o -name '*.pxi' \) -delete \
 && find /app/.venv -type d -name '__pycache__' -prune -exec rm -rf {} +

# ---------------------------------------------------------------------------
FROM ${PYTHON_IMAGE}

RUN useradd --uid 1000 --create-home --shell /usr/sbin/nologin flowbi

WORKDIR /app

COPY --from=builder --chown=flowbi:flowbi /app/.venv /app/.venv
COPY --chown=flowbi:flowbi alembic.ini ./
COPY --chown=flowbi:flowbi migrations ./migrations

# out/ is where --destination filesystem writes Parquet; dlt keeps pipeline
# state under ~/.dlt. Both must exist in the image, owned by the non-root user:
# a named volume mounted on a path the image lacks is created root-owned, and
# dlt then fails with "Permission denied: '/home/flowbi/.dlt/pipelines'".
RUN mkdir -p /app/out /home/flowbi/.dlt \
 && chown flowbi:flowbi /app/out /home/flowbi/.dlt

ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

USER 1000

# No ENTRYPOINT - every run supplies its own command (docs/deployment-cf.md §2.1):
#   flowbi extract issues --destination postgres
#   alembic upgrade head
CMD ["flowbi", "--help"]
