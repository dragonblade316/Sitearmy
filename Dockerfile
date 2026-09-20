FROM docker.io/library/caddy:2 AS caddy

FROM docker.io/library/debian:bookworm-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        ca-certificates \
        git \
        hugo \
        jekyll \
        libffi-dev \
        libyaml-dev \
        ruby-bundler \
        ruby-dev \
        zlib1g-dev \
        python3 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=caddy /usr/bin/caddy /usr/bin/caddy
COPY sitearmy.py /opt/sitearmy/sitearmy.py
COPY github-credential-helper /opt/sitearmy/github-credential-helper
COPY builders /opt/sitearmy/builders

RUN chmod 700 /opt/sitearmy/github-credential-helper \
    && mkdir -p /etc/sitearmy /run/sitearmy /srv/sitearmy /data /config

ENV XDG_CONFIG_HOME=/config
ENV XDG_DATA_HOME=/data

VOLUME ["/data", "/config"]
EXPOSE 80 443 443/udp

ENTRYPOINT ["python3", "/opt/sitearmy/sitearmy.py"]
