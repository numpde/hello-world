# Only pinned, separately acquired images enter this offline build.
ARG PYTHON_IMAGE
ARG UV_IMAGE
FROM ${UV_IMAGE} AS uv-bin
FROM ${PYTHON_IMAGE}
COPY --from=uv-bin /uv /uvx /usr/local/bin/
ENV UV_NO_MANAGED_PYTHON=1 PYTHONDONTWRITEBYTECODE=1 UV_NO_PROGRESS=1
USER 65532:65532
