# syntax=docker/dockerfile:1
#
# One Tether image (ADR-0001). Entrypoints, selected by the compose `command`:
#   tether-api     the api process (port 8080)
#   tether-worker  the worker process
#   tether migrate the one-shot migrate service
#   tether         the CLI
# Workloads, the dev token issuer and other dev tools are not part of this image.

FROM python:3.12-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.12.9 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_PYTHON_DOWNLOADS=never
WORKDIR /src
# Dependencies first, so source changes don't invalidate the dependency layer.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --locked --no-dev --no-install-project
COPY src ./src
RUN uv sync --locked --no-dev --no-editable

FROM python:3.12-slim AS runtime
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
COPY --from=build /opt/venv /opt/venv
RUN useradd --system --uid 10001 --no-create-home tether
USER tether
WORKDIR /tmp
EXPOSE 8080
CMD ["tether", "--help"]
