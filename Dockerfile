FROM docker.io/library/caddy:2 AS caddy

# Alpine edge is rolling; package updates may introduce incompatible builder versions.
FROM docker.io/library/alpine:edge

RUN apk add --no-cache \
        build-base \
        ca-certificates \
        git \
        hugo \
        jekyll \
        libffi-dev \
        nodejs \
        npm \
        python3 \
        ruby-bundler \
        ruby-dev \
        yaml-dev \
        zlib-dev

COPY --from=caddy /usr/bin/caddy /usr/bin/caddy
COPY sitearmy.py /opt/sitearmy/sitearmy.py
COPY github-credential-helper /opt/sitearmy/github-credential-helper
COPY builders /opt/sitearmy/builders

RUN chmod 700 /opt/sitearmy/github-credential-helper \
    && mkdir -p /etc/sitearmy /run/sitearmy /data/sitearmy/sites /data/config

ENV BUNDLE_PATH=/data/sitearmy/cache/bundle
ENV NPM_CONFIG_CACHE=/data/sitearmy/cache/npm
ENV XDG_CACHE_HOME=/data/sitearmy/cache/xdg
ENV XDG_CONFIG_HOME=/data/config
ENV XDG_DATA_HOME=/data

VOLUME ["/data"]
EXPOSE 80 443 443/udp

ENTRYPOINT ["python3", "/opt/sitearmy/sitearmy.py"]
