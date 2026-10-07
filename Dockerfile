# web-mcp: MCP server with web search (SearXNG) and page reading (curl_cffi -> Camoufox -> Wayback)
FROM python:3.12-slim-bookworm

COPY --from=ghcr.io/astral-sh/uv:0.12.1 /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Python dependencies first (cached unless the lock changes).
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --locked --no-dev --no-install-project

# Firefox runtime libraries (via Playwright's list), Xvfb for the optional
# virtual display, fonts and time zones.
RUN apt-get update \
 && playwright install-deps firefox \
 && apt-get install -y --no-install-recommends xvfb xauth tzdata fonts-liberation fonts-noto-core fonts-dejavu-core ca-certificates \
 && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --uid 10001 app \
 && mkdir -p /opt/venv/lib/python3.12/site-packages/fpgen/data \
 && chown -R app /opt/venv/lib/python3.12/site-packages/fpgen
USER app

# The Camoufox browser and its default addon (uBlock Origin), baked into the image.
RUN python -m camoufox fetch \
 && python -c "from camoufox.addons import DefaultAddons, maybe_download_addons; maybe_download_addons(list(DefaultAddons))" \
 && python -m camoufox list

USER root
COPY src ./src
RUN uv sync --locked --no-dev && chown -R app /app
USER app

ENV WEB_MCP_HOST=0.0.0.0 \
    WEB_MCP_PORT=8890 \
    SEARXNG_URL=http://searxng:8080
EXPOSE 8890

HEALTHCHECK --interval=60s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8890/health', timeout=4)" || exit 1

CMD ["web-mcp", "serve"]
