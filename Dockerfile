FROM ghcr.io/pluralisresearch/agora-test:latest

ENV DEBIAN_FRONTEND=noninteractive \
    VIRTUAL_ENV=/opt/agora-venv \
    PATH=/opt/agora-venv/bin:$PATH

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        cron \
        iproute2 \
        jq \
        openssh-server \
        procps \
        tmux \
    && ln -s /opt/conda /opt/agora-venv \
    && rm -rf /var/lib/apt/lists/* \
    && /opt/agora-venv/bin/python -c 'import sys, torch; assert sys.version_info[:2] == (3, 13), sys.version; assert torch.__version__.startswith("2.11."), torch.__version__'

COPY start.sh /start.sh
COPY machine-sentinel /opt/agora-machine-sentinel

RUN chmod 755 /start.sh \
    && chmod 755 /opt/agora-machine-sentinel/start-machine-sentinel.sh \
    && find /opt/agora-machine-sentinel/actions -type f -name '*.sh' -exec chmod 755 {} + \
    && find /opt/agora-machine-sentinel -type f -name '*.py' -exec chmod 644 {} + \
    && mkdir -p /run/sshd /root/.ssh \
    && chmod 700 /root/.ssh \
    && rm -f /etc/ssh/ssh_host_*_key /etc/ssh/ssh_host_*_key.pub

WORKDIR /workspace

EXPOSE 22 49200

CMD ["/start.sh"]
