# Build with an independently reviewed immutable Python image reference.
ARG PYTHON_IMAGE
FROM ${PYTHON_IMAGE}
USER 65532:65532
WORKDIR /skill
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
# Security/resource/network flags are enforced by sandbox_executor at runtime.
