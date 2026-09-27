FROM tracefix/reverify-pytest10081:pilot@sha256:2cc93cb74dd96de3179e8320b5b99049fdef0545ddfcad97d388ccaf34fb646a

RUN python -m pip install --no-cache-dir \
    'pytest==9.1.1' 'Sphinx==5.0.0' 'docutils==0.18.1' \
    'Pygments==2.21.0' 'html5lib==1.1'

ENV PYTHONPATH=/input/src
WORKDIR /input
