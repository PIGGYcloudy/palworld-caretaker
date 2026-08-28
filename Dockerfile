# syntax=docker/dockerfile:1
# Debian provides the libc and 32-bit runtime required by the Linux Palworld
# server while keeping the image independent of a host Steam installation.
FROM debian:bookworm-slim

ARG STEAMCMD_URL=https://steamcdn-a.akamaihd.net/client/installer/steamcmd_linux.tar.gz

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH=/opt/venv/bin:/opt/steamcmd:/opt/palworld-caretaker/scripts:$PATH

RUN dpkg --add-architecture i386 \
    && apt-get update \
    && apt-get install --yes --no-install-recommends \
        ca-certificates curl gosu lib32gcc-s1 libstdc++6:i386 perl python3 \
        python3-pip python3-venv rsync tini \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 1000 steam \
    && useradd --uid 1000 --gid steam --home-dir /srv/palworld --create-home --shell /usr/sbin/nologin steam \
    && install -d -o steam -g steam /opt/steamcmd /srv/palworld /srv/palworld-backups /etc/palworld-caretaker /run/palworld-caretaker \
    && curl --fail --location --retry 3 "$STEAMCMD_URL" -o /tmp/steamcmd.tar.gz \
    && tar -xzf /tmp/steamcmd.tar.gz -C /opt/steamcmd \
    && rm /tmp/steamcmd.tar.gz \
    # The entrypoint is already PID 1's root child and uses gosu explicitly;
    # the image has no need to retain ambient set-id executables.
    && find / -xdev -type f -perm /6000 -exec chmod a-s {} + \
    && ln -s /opt/steamcmd/steamcmd.sh /usr/local/bin/steamcmd

WORKDIR /opt/palworld-caretaker
COPY requirements.txt pyproject.toml README.md ./
COPY src ./src
RUN python3 -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir . \
    && /opt/venv/bin/pip install --no-cache-dir -r requirements.txt

COPY scripts ./scripts
COPY docker/default-config ./docker/default-config
COPY docker/docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
COPY docker/docker-supervisor.py /usr/local/bin/docker-supervisor.py
RUN chmod 0755 /usr/local/bin/docker-entrypoint.sh /usr/local/bin/docker-supervisor.py scripts/*.sh scripts/palworld-*

VOLUME ["/srv/palworld", "/srv/palworld-backups", "/etc/palworld-caretaker"]
EXPOSE 8211/udp 25575/tcp 8765/tcp

ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/docker-entrypoint.sh"]
CMD ["run"]
