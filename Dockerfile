FROM ghcr.io/astral-sh/uv:0.9.21 AS uv
FROM python:3.13-slim
COPY --from=uv /uv /usr/local/bin/uv
RUN useradd --create-home --uid 1000 bridge
USER bridge
WORKDIR /home/bridge/app
ENV PYTHONUNBUFFERED=1 UV_NO_CONFIG=1
COPY --chown=bridge:bridge pyproject.toml uv.lock ./
COPY --chown=bridge:bridge src ./src
COPY --chown=bridge:bridge bridge*.toml ./
RUN uv sync --frozen --no-dev --no-editable --no-cache
EXPOSE 7860
CMD [".venv/bin/python", "-m", "chat_bridge.space_app"]
