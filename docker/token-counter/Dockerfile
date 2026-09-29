FROM python:3.11-slim@sha256:e41613d42d4891e4930f79523f93f81bbc7632584ec65e36ab055f41a800b41e

COPY deepseek_recipe-0.1.1-cp310-abi3-manylinux_2_28_x86_64.whl /tmp/deepseek_recipe-0.1.1-cp310-abi3-manylinux_2_28_x86_64.whl
RUN echo '7818802935becc76ac9b81af6b4a1e5390ec65355cecefd1fd4790f51845c1c5  /tmp/deepseek_recipe-0.1.1-cp310-abi3-manylinux_2_28_x86_64.whl' | sha256sum -c - \
    && python -m pip install --no-cache-dir 'pydantic==2.13.5' /tmp/deepseek_recipe-0.1.1-cp310-abi3-manylinux_2_28_x86_64.whl \
    && rm /tmp/deepseek_recipe-0.1.1-cp310-abi3-manylinux_2_28_x86_64.whl

ENV PYTHONPATH=/input/src
WORKDIR /input
