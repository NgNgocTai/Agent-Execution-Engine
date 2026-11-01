FROM python:3.12-slim@sha256:f77ac9e44ae96ef2c90b8053ea08c31f8be030f824196b0ae4db6d462c84e51f AS base

WORKDIR /app

COPY requirements.txt requirements.lock.txt ./
RUN pip install --no-cache-dir -r requirements.lock.txt

FROM base AS checks
# Ruff 0.9.10, matching requirements-dev.txt; no dev pip install in this image.
COPY --from=ghcr.io/astral-sh/ruff@sha256:6674a0a3dda66eba315417a187b8ca26eef438a8982683e2e04644b4323da16b /ruff /usr/local/bin/ruff
COPY . .
CMD ["python", "-m", "unittest", "discover", "-s", "tests"]

FROM base AS runtime
COPY . .
COPY --from=docker:28-cli@sha256:625d9431a9f54c5a2bc90f24f0e1c3d55b1349fd857dd85035f98c2c9acbdd4d /usr/local/bin/docker /usr/local/bin/docker
RUN mkdir -p data && chown 65532:65532 data
USER 65532:65532
ENV DATABASE_URL=sqlite+aiosqlite:////app/data/conversations.db DOCKER_CONFIG=/tmp/docker-config
EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
