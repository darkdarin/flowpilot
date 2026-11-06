# FlowPilot runtime with the graph editor and the bundled plugins (Wyoming, VAD, audio, Soniox, LLM).
#
#   docker build -t flowpilot .
#   docker run -d -p 8765:8765 -v flowpilot-data:/data flowpilot
#
# The first start creates /data/config.json, /data/token and a starter graph (see datadir.py).
# The same image runs as the Home Assistant add-on (addon/).

# --- editor: Vue build into the server package --------------------------------------------------
FROM node:22-slim AS editor
WORKDIR /src/editor
COPY editor/package.json editor/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY editor/ ./
RUN mkdir -p ../packages/flowpilot-server/src/flowpilot_server && npm run build

# --- python: one venv with every workspace package ----------------------------------------------
FROM python:3.14-slim AS python
# pymicro-vad has no Python 3.14 wheels yet and builds from source
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*
COPY --from=ghcr.io/astral-sh/uv:0.13.7 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /src
COPY pyproject.toml uv.lock ./
COPY packages packages
COPY plugins plugins
COPY --from=editor /src/packages/flowpilot-server/src/flowpilot_server/editor \
    packages/flowpilot-server/src/flowpilot_server/editor
RUN uv sync --locked --no-dev --all-packages --no-editable

# --- runtime -----------------------------------------------------------------------------------
FROM python:3.14-slim
COPY --from=python /app/.venv /app/.venv
ENV PATH=/app/.venv/bin:$PATH PYTHONUNBUFFERED=1
VOLUME /data
EXPOSE 8765
ENTRYPOINT ["flowpilot-server", "--data", "/data"]
