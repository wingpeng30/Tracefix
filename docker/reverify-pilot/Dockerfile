FROM python:3.11-slim@sha256:e41613d42d4891e4930f79523f93f81bbc7632584ec65e36ab055f41a800b41e

RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

# Pilot environment only. The recorded image digest and pip freeze are part of its evidence.
RUN python -m pip install --no-cache-dir \
    'pydantic>=2.7,<3' \
    'pytest==7.1.2' \
    'setuptools-scm==6.4.2'

ENV PYTHONPATH=/input/src
WORKDIR /input
