FROM docker.io/vllm/vllm-openai@sha256:6ee133356d49ed24cee03dd0cda3d3980279a11c107ce3e752c89e76540f41c9
USER 0:0
COPY dev/model-serving/patch.py dev/model-serving/patch.lock.json /opt/klip/model-image/
RUN python3 /opt/klip/model-image/patch.py install \
    && python3 -m pip check \
    && mkdir -p /home/model /cache /tmp/model \
    && chown 10001:10001 /home/model /cache /tmp/model
ENV HOME=/home/model HF_HOME=/cache/huggingface XDG_CACHE_HOME=/cache \
    PYTHONDONTWRITEBYTECODE=1 \
    SSL_CERT_FILE=/usr/local/lib/python3.12/dist-packages/certifi/cacert.pem \
    REQUESTS_CA_BUNDLE=/usr/local/lib/python3.12/dist-packages/certifi/cacert.pem \
    CURL_CA_BUNDLE=/usr/local/lib/python3.12/dist-packages/certifi/cacert.pem
USER 10001:10001
# Preserve the upstream vLLM entrypoint; runtime writes use explicit mounts.
RUN python3 /opt/klip/model-image/patch.py verify
