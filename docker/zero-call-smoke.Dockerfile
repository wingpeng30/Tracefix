FROM python:3.11-slim@sha256:e41613d42d4891e4930f79523f93f81bbc7632584ec65e36ab055f41a800b41e

RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/tracefix
COPY pyproject.toml README.md ./
COPY src/ ./src/
COPY scripts/reproduce_zero_call.py ./scripts/reproduce_zero_call.py
RUN python -m pip install --no-cache-dir '.[dev]'

ENTRYPOINT ["python", "scripts/reproduce_zero_call.py"]
