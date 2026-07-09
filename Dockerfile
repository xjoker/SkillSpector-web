# syntax=docker/dockerfile:1.7

FROM python:3.12-slim-bookworm AS deps

WORKDIR /app
COPY pyproject.toml ./
RUN python -m venv .venv
RUN --mount=type=cache,target=/root/.cache/pip \
    python -c "import pathlib, tomllib; data = tomllib.loads(pathlib.Path('pyproject.toml').read_text()); reqs = data['build-system']['requires'] + data['project']['dependencies'] + data['project']['optional-dependencies']['mcp']; pathlib.Path('/tmp/requirements.txt').write_text('\n'.join(reqs) + '\n')" \
    && .venv/bin/pip install --retries 10 --timeout 120 --disable-pip-version-check -r /tmp/requirements.txt

FROM deps AS builder

COPY README.md VERSION ./
COPY src/ src/
RUN --mount=type=cache,target=/root/.cache/pip \
    .venv/bin/pip install --no-deps --no-build-isolation --disable-pip-version-check .

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
EXPOSE 8477

ENTRYPOINT ["skillspector-entrypoint"]
CMD ["web", "--port", "8477"]
