FROM python:3.13.11-slim-bookworm

COPY requirements/locks/mcp-serena-image-py313.txt /tmp/mcp-requirements.txt
RUN python -m pip install --no-cache-dir --require-hashes -r /tmp/mcp-requirements.txt \
    && pyright --version

# Serena's Python language server launcher normally invokes uvx. This wrapper
# admits only the pinned, already installed Pyright entrypoint at runtime.
RUN printf '%s\n' '#!/bin/sh' \
    'case "$*" in *pyright-langserver*) exec pyright-langserver --stdio ;; *) exit 2 ;; esac' \
    > /usr/local/bin/uvx \
    && chmod 755 /usr/local/bin/uvx

COPY docker/tracefix-serena-mcp /usr/local/bin/tracefix-serena-mcp
RUN chmod 755 /usr/local/bin/tracefix-serena-mcp

USER 10001:10001
WORKDIR /tmp
