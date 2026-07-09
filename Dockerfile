# syntax=docker/dockerfile:1.7

FROM python:3.12-slim-bookworm AS builder

WORKDIR /app
COPY pyproject.toml README.md VERSION ./
COPY src/ src/
RUN python -m venv .venv
RUN --mount=type=cache,target=/root/.cache/pip \
    .venv/bin/pip install --retries 10 --timeout 120 '.[mcp]'

FROM python:3.12-slim-bookworm

RUN apt-get update \
    && apt-get install --no-install-recommends -y git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /app/.venv /app/.venv
COPY docker/entrypoint.sh /usr/local/bin/skillspector-entrypoint
RUN chmod +x /usr/local/bin/skillspector-entrypoint

ARG SKILLSPECTOR_GIT_COMMIT=unknown
ARG SKILLSPECTOR_SCHEMA_VERSION=none
ARG SKILLSPECTOR_RELEASE_VERSION=dev
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    SKILLSPECTOR_GIT_COMMIT="${SKILLSPECTOR_GIT_COMMIT}" \
    SKILLSPECTOR_SCHEMA_VERSION="${SKILLSPECTOR_SCHEMA_VERSION}" \
    SKILLSPECTOR_RELEASE_VERSION="${SKILLSPECTOR_RELEASE_VERSION}"
WORKDIR /scan
EXPOSE 8477 8765 8001

ENTRYPOINT ["skillspector-entrypoint"]
CMD ["--help"]
