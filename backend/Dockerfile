FROM python:3.12-slim AS base

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

FROM base AS checks
# Ruff 0.9.10, matching requirements-dev.txt; no dev pip install in this image.
COPY --from=ghcr.io/astral-sh/ruff@sha256:6674a0a3dda66eba315417a187b8ca26eef438a8982683e2e04644b4323da16b /ruff /usr/local/bin/ruff
COPY . .
CMD ["python", "-m", "unittest", "discover", "-s", "tests"]

FROM base AS runtime
COPY . .
RUN mkdir -p data
EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
