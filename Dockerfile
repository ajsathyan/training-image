FROM runpod/pytorch:1.0.7-cu1281-torch271-ubuntu2204

ENV DEBIAN_FRONTEND=noninteractive \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_NO_INPUT=1 \
    VIRTUAL_ENV=/opt/agora-venv \
    PATH=/opt/agora-venv/bin:$PATH

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        git \
        iproute2 \
        jq \
        procps \
        python3.11 \
        python3.11-dev \
        python3.11-venv \
        tmux \
    && rm -rf /var/lib/apt/lists/*

RUN python3.11 -c 'import sys; assert sys.version_info >= (3, 11), sys.version' \
    && python3.11 -m venv /opt/agora-venv \
    && /opt/agora-venv/bin/python -m pip install --upgrade \
        pip==25.3 \
        "setuptools<81.0" \
        wheel \
        hatchling \
        editables

RUN git clone --depth 1 https://github.com/PluralisResearch/agora /opt/agora-source \
    && cd /opt/agora-source \
    && /opt/agora-venv/bin/python -m pip install \
        filelock \
        fsspec \
        jinja2 \
        networkx \
        "numpy>=1.17,<2.4" \
        pillow \
        sympy \
        triton==3.3.0 \
        typing-extensions \
    && /opt/agora-venv/bin/python -m pip install \
        torch==2.7.0 \
        torchvision==0.22.0 \
        torchaudio==2.7.0 \
        --no-deps \
        --index-url https://download.pytorch.org/whl/cu128 \
    && /opt/agora-venv/bin/python -m pip install --no-deps nvidia-cusparselt-cu12==0.6.3 \
    && /opt/agora-venv/bin/python -m pip install \
        PyYAML \
        prometheus_client \
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
    && /opt/agora-venv/bin/python -m pip install --no-build-isolation --no-deps -e ./agora_server \
    && /opt/agora-venv/bin/python -m pip install --no-build-isolation --no-deps -e ./agora \
    && /opt/agora-venv/bin/python -c 'import torch, hivemind, agora, agora_server; assert torch.__version__.startswith("2.7."), torch.__version__; print(f"ready torch={torch.__version__}")'

WORKDIR /workspace
