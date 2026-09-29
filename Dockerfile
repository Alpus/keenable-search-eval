FROM python:3.12.13-slim-bookworm@sha256:4766d8b510c428e595d74b9cc5bbb2fae8e26316fffb4adc89908d79aacd58a2 AS base
COPY --from=ghcr.io/astral-sh/uv:0.11.2@sha256:c4f5de312ee66d46810635ffc5df34a1973ba753e7241ce3a08ef979ddd7bea5 /uv /uvx /bin/
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates && rm -rf /var/lib/apt/lists/*
ARG LOCAL_UID=1000
RUN useradd --create-home --uid ${LOCAL_UID} eval
WORKDIR /app
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1 MPLCONFIGDIR=/tmp/matplotlib

FROM base AS bundles
COPY assets /bundles
RUN cd /bundles && sha256sum -c SHA256SUMS && mkdir /benchmark /snapshot && tar -xzf benchmarks.tar.gz -C /benchmark && tar -xzf devdex-pilot.tar.gz -C /snapshot

FROM base AS replay
COPY --from=bundles /snapshot/runtime/ /app/
RUN uv sync --frozen --no-dev
COPY --from=bundles /snapshot/vendor/ /app/vendor/
COPY --from=bundles /snapshot/data/ /app/data/
COPY --from=bundles /snapshot/validation/ /app/validation/
COPY --from=bundles /snapshot/runs/ /recorded/
COPY --from=bundles /snapshot/expected.json /recorded/expected.json
COPY scripts/replay.py /replay.py
RUN mkdir /app/runs && chown -R eval:eval /app /recorded
USER eval
ENTRYPOINT ["python", "/replay.py"]

FROM base AS application
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev
COPY src ./src
COPY configs ./configs
RUN uv sync --frozen --no-dev && mkdir -p runs .gateway && chown -R eval:eval /app
USER eval
ENTRYPOINT ["search-eval"]

FROM application AS runner
USER root
COPY data ./data
COPY --from=bundles /benchmark/ /app/
RUN chown -R eval:eval /app
USER eval

FROM application AS gateway
ENTRYPOINT ["search-eval-mcp"]

FROM runner AS test
USER root
RUN uv sync --frozen
COPY tests ./tests
COPY assets ./assets
RUN chown -R eval:eval /app
USER eval
ENTRYPOINT ["sh", "-c", "ruff check src tests && pytest -q"]
