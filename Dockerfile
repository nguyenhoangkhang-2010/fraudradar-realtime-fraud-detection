FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1

# LightGBM's wheel links against the OpenMP runtime, which the slim image does not ship.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install -e ".[model]"

EXPOSE 8000
CMD ["uvicorn", "fraudradar.api.app:app", "--host", "0.0.0.0", "--port", "8000"]
