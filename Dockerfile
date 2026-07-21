FROM pytorch/pytorch:2.7.0-cuda12.8-cudnn9-runtime@sha256:7db0e1bf4b1ac274ea09cf6358ab516f8a5c7d3d0e02311bed445f7e236a5d80

ENV DEBIAN_FRONTEND=noninteractive \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_NO_INPUT=1 \
    VIRTUAL_ENV=/opt/agora-venv \
    PATH=/opt/agora-venv/bin:$PATH

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        ca-certificates \
        cron \
        curl \
        git \
        iproute2 \
        jq \
        openssh-server \
        procps \
        tmux \
    && rm -rf /var/lib/apt/lists/*

RUN python -c 'import sys, torch; assert sys.version_info[:2] == (3, 11), sys.version; assert torch.__version__.startswith("2.7."), torch.__version__' \
    && ln -s /opt/conda /opt/agora-venv \
    && python -m pip install --upgrade \
        pip==25.3 \
        "setuptools<81.0" \
        wheel \
        hatchling \
        editables

RUN /opt/agora-venv/bin/python -m pip install \
        PyYAML \
        prometheus_client \
        "numpy>=1.17,<2.4" \
        scipy \
        prefetch_generator \
        msgpack \
        sortedcontainers \
        uvloop \
        grpcio-tools==1.80.0 \
        protobuf \
        configargparse \
        py-multihash \
        cryptography \
        pydantic \
        packaging \
        varint \
        base58 \
        netaddr \
        idna \
        py-cid \
        requests \
        speedtest-cli \
        psutil \
        boto3 \
        omegaconf \
        tenacity \
        pySmartDL \
    && /opt/agora-venv/bin/python -m pip install --no-build-isolation --no-deps \
        "hivemind @ git+https://github.com/learning-at-home/hivemind.git@4d5c41495be082490ea44cce4e9dd58f9926bb4e" \
    && /opt/agora-venv/bin/python -c 'import torch, hivemind; assert torch.__version__.startswith("2.7."), torch.__version__; print(f"ready torch={torch.__version__}")'

COPY start.sh /start.sh

RUN chmod 755 /start.sh \
    && mkdir -p /run/sshd /root/.ssh \
    && chmod 700 /root/.ssh \
    && rm -f /etc/ssh/ssh_host_*_key /etc/ssh/ssh_host_*_key.pub

WORKDIR /workspace

EXPOSE 22 49200

CMD ["/start.sh"]
