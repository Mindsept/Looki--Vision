FROM node:24-slim AS frontend
WORKDIR /web/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend ./
RUN npm run build

FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 UV_LINK_MODE=copy
WORKDIR /app
RUN pip install --no-cache-dir uv==0.9.26
COPY pyproject.toml uv.lock ./
ARG INSTALL_VISION=false
RUN if [ "$INSTALL_VISION" = "true" ]; then apt-get update && apt-get install -y --no-install-recommends git ca-certificates libgl1 libglib2.0-0 libgomp1 && rm -rf /var/lib/apt/lists/*; fi
RUN --mount=type=cache,target=/root/.cache/uv if [ "$INSTALL_VISION" = "true" ]; then uv sync --frozen --no-dev --extra vision --no-install-project; else uv sync --frozen --no-dev --no-install-project; fi
COPY app ./app
COPY --from=frontend /web/app/static ./app/static
COPY alembic ./alembic
COPY alembic.ini ./alembic.ini
COPY fixtures ./fixtures
COPY scripts ./scripts
RUN groupadd --gid 10001 looki && useradd --uid 10001 --gid looki --create-home looki && mkdir -p /home/looki/.cache/huggingface && chown -R looki:looki /home/looki
ENV PATH="/app/.venv/bin:$PATH" HF_HOME=/home/looki/.cache/huggingface YOLO_CONFIG_DIR=/home/looki/.cache/huggingface/ultralytics YOLO_AUTOINSTALL=false
USER looki
EXPOSE 8000
CMD ["sh", "-c", "alembic upgrade head && exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1 --ws-max-size 8192"]
