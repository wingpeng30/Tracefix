FROM python:3.9.21-slim@sha256:40007fe18a72a2e7166be350d52dab86b9fe18f2de08e6a38e26422fb247e81e AS task-python

RUN python -m pip install --no-cache-dir \
    --timeout 180 --retries 8 \
    'pytest==4.0.2' 'attrs==18.2.0' 'httpbin==0.10.2' 'Pygments==2.21.0'

FROM tracefix/reverify-pytest310:20260926-controller311
COPY --from=task-python /usr/local /opt/python39
COPY requests-test-tls/service.py requests-test-tls/sitecustomize.py /opt/tracefix/request-test/

ENV TRACEFIX_TEST_CA_BUNDLE=/opt/tracefix/request-test/test-ca.pem
ENV PYTHONDONTWRITEBYTECODE=1
WORKDIR /input
