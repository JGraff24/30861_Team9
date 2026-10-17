# Build dependencies come only from the checked-in uv.lock.
FROM ghcr.io/astral-sh/uv:0.13.1 AS uv
FROM python:3.12-slim AS build
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /service
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project
COPY app ./app
COPY tester ./tester
RUN uv sync --locked --no-dev --no-editable

FROM python:3.12-slim AS runtime
RUN groupadd --gid 10001 service && useradd --uid 10001 --gid 10001 --no-create-home service \
    && mkdir -p /service/state /service/tester-state && chown -R service:service /service
WORKDIR /service
COPY --from=build /service/.venv ./.venv
COPY --from=build /service/app ./app
COPY --from=build /service/tester ./tester
ENV PATH="/service/.venv/bin:$PATH" PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
USER 10001:10001
EXPOSE 8000
CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]

# Choose only when Ryan's actual measurement integration is enabled.
FROM build AS measurement-build
RUN uv sync --locked --no-dev --extra measurement --no-editable
FROM runtime AS measurement
COPY --from=measurement-build /service/.venv /service/.venv
