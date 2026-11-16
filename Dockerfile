# syntax=docker/dockerfile:1
#
# One Tether image definition (ADR-0001), two variants selected with `--target`:
#
#   prod  the production image. It holds the core and the real workload only: no scenario
#         files, no demo workload, no scripted model provider, no bootstrap file and no dev
#         tooling. The operator mounts a reviewed bootstrap file and sets the environment
#         explicitly at run time; the image never chooses either.
#   dev   what the compose stack builds, and what a plain `docker build` produces (it is the last
#         stage on purpose). It adds the dev bootstrap file and the scripted-provider scenarios.
#
# Entrypoints, selected by the compose `command` or the orchestrator:
#   tether-api     the api process (port 8080)
#   tether-worker  the worker process
#   tether migrate the one-shot migrate service
#   tether         the CLI
# The dev token issuer and other dev tools are not part of either variant.
#
# `tests/unit/test_t32_production_image.py` checks this file against the excluded paths, and
# `tests/integration/test_production_image_built.py` inspects a built prod image.

# --- dependencies (shared by both variants; holds no application source) ---------------------
FROM python:3.12-slim AS deps
COPY --from=ghcr.io/astral-sh/uv:0.12.9 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_PYTHON_DOWNLOADS=never
WORKDIR /src
# Dependencies first, so source changes don't invalidate the dependency layer.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --locked --no-dev --no-install-project

# --- dev build: the whole source tree, including the demo workload and its scenarios ---------
FROM deps AS build-dev
COPY src ./src
COPY workloads ./workloads
RUN uv sync --locked --no-dev --no-editable

# --- prod build: the demo workload, every scenario directory and the scripted provider are
# deleted before the wheel is built, and the demo workload's entry point is dropped so no
# dangling registration remains. One RUN, so none of it survives in a layer.
FROM deps AS build-prod
COPY src ./src
COPY workloads ./workloads
RUN rm -rf workloads/incident_remediation workloads/change_control/scenarios src/tether/testing/scripted.py \
    && sed -i '/^incident_remediation = /d' pyproject.toml \
    && uv sync --locked --no-dev --no-editable

# --- runtime base ----------------------------------------------------------------------------
FROM python:3.12-slim AS runtime-base
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
RUN useradd --system --uid 10001 --no-create-home tether
USER tether
WORKDIR /tmp
EXPOSE 8080
CMD ["tether", "--help"]

# --- production image ------------------------------------------------------------------------
FROM runtime-base AS prod
COPY --from=build-prod /opt/venv /opt/venv

# --- development image (last stage: the default target) --------------------------------------
FROM runtime-base AS dev
COPY --from=build-dev /opt/venv /opt/venv
COPY config /app/config
COPY workloads/incident_remediation/scenarios /app/scenarios
