FROM python:3.11.16-bookworm@sha256:00f0ecbf74ff8f915020d5a40c4bc6a83f46cd7b83f47db51c7e204f0d8a3ec2

WORKDIR /opt/tracefix
COPY pyproject.toml README.md ./
COPY src/ ./src/
COPY requirements/locks/reproduction-py311.txt ./requirements.lock
RUN python -m pip install --no-cache-dir --require-hashes -r requirements.lock \
    && python -m pip install --no-cache-dir --no-deps --no-build-isolation .

CMD ["sleep", "infinity"]
