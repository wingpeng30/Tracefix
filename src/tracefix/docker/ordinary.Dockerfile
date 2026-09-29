FROM python:3.11.16-bookworm@sha256:00f0ecbf74ff8f915020d5a40c4bc6a83f46cd7b83f47db51c7e204f0d8a3ec2

COPY ordinary-base.lock /opt/tracefix-build/ordinary-base.lock
COPY project-requirements.lock /opt/tracefix-build/project-requirements.lock
RUN python -m pip install --no-cache-dir --require-hashes \
      -r /opt/tracefix-build/ordinary-base.lock \
    && python -m pip install --no-cache-dir --require-hashes \
      -r /opt/tracefix-build/project-requirements.lock \
    && python -c "import pydantic,pytest; print(pytest.__version__)" \
    && mkdir -p /input /work/agent /work/evidence /opt/tracefix/src \
    && chown -R 10001:10001 /input /work /opt/tracefix

USER 10001:10001
WORKDIR /work/agent
CMD ["sleep", "infinity"]
