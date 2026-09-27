FROM python:3.9.21-slim@sha256:40007fe18a72a2e7166be350d52dab86b9fe18f2de08e6a38e26422fb247e81e AS task-python
RUN python -m pip install --no-cache-dir \
    'pytest==8.4.2' 'httpbin==0.10.2' 'Pygments==2.21.0'

FROM tracefix/reverify-pytest10081:pilot@sha256:2cc93cb74dd96de3179e8320b5b99049fdef0545ddfcad97d388ccaf34fb646a
COPY --from=task-python /usr/local /opt/python39

ENV PYTHONPATH=/input/src
WORKDIR /input
