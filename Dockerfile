FROM python:3.12-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.9 /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
# Dependencies first: this layer is rebuilt only when the lock file changes.
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project
COPY src src
RUN uv sync --locked --no-dev --no-editable

FROM python:3.12-slim
RUN useradd --system --uid 10001 app
WORKDIR /app
COPY --from=build /app/.venv .venv
COPY alembic.ini ./
COPY migrations migrations
COPY demo_supplier demo_supplier
ENV PATH=/app/.venv/bin:$PATH PYTHONUNBUFFERED=1
USER app
CMD ["uvicorn", "aggregator.api:app", "--host", "0.0.0.0", "--port", "8000"]
