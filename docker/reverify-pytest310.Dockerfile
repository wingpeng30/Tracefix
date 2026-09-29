FROM python:3.10-slim@sha256:179cecec99c29e6f1e97caa424514599267f7dd836696dd673ea5587eefc65fa AS task-python

RUN python -m pip install --no-cache-dir \
    'pluggy==1.0.0' \
    'pytest==7.1.2' \
    'setuptools-scm==6.4.2'

FROM python:3.11-slim@sha256:e41613d42d4891e4930f79523f93f81bbc7632584ec65e36ab055f41a800b41e AS controller

RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

RUN python -m pip install --no-cache-dir 'pydantic==2.13.5'

COPY --from=task-python /usr/local /opt/python310

ENV PYTHONPATH=/input/src
WORKDIR /input
