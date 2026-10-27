FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONUTF8=1 \
    MUNIM_V2_HOST=0.0.0.0 \
    MUNIM_V2_PORT=8800 \
    HF_HOME=/app/.run/models

RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*

WORKDIR /app
# Dependencies first, from pyproject.toml alone: this layer is reused until the dependencies change, so a code
# change rebuilds in seconds and does not download half of PyPI again.
COPY pyproject.toml /app/
# wheels/ (infra/fetch_wheels.sh) is mounted only for this step, so it never ends up in the image
RUN --mount=type=bind,source=wheels,target=/wheels \
    mkdir -p munim && touch munim/__init__.py \
    && pip install --no-cache-dir --retries 8 --timeout 60 --find-links /wheels ".[voice]" \
    && pip uninstall -y ai-munim-agent && rm -rf munim
COPY munim /app/munim
RUN pip install --no-cache-dir --no-deps . && useradd --create-home --uid 1000 munim && mkdir -p /app/.run && chown munim /app/.run
USER munim

EXPOSE 8800
CMD ["python", "-m", "munim.core.server"]
