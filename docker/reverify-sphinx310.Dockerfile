FROM python:3.10-slim@sha256:179cecec99c29e6f1e97caa424514599267f7dd836696dd673ea5587eefc65fa AS task-python

RUN python -m pip install --no-cache-dir \
    'pytest==7.1.2' \
    'Sphinx==5.0.0' \
    'docutils==0.18.1' \
    'Pygments==2.21.0' \
    'html5lib==1.1' \
    'alabaster==0.7.16' \
    'Babel==2.18.0' \
    'imagesize==2.0.1' \
    'Jinja2==3.1.6' \
    'requests==2.34.2' \
    'snowballstemmer==3.1.1' \
    'sphinxcontrib-applehelp==2.0.0' \
    'sphinxcontrib-devhelp==2.0.0' \
    'sphinxcontrib-htmlhelp==2.1.0' \
    'sphinxcontrib-jsmath==1.0.1' \
    'sphinxcontrib-qthelp==2.0.0' \
    'sphinxcontrib-serializinghtml==2.0.0'

FROM tracefix/reverify-pytest310:20260926-controller311

COPY --from=task-python /usr/local /opt/python310

ENV PYTHONPATH=/input/src
WORKDIR /input
