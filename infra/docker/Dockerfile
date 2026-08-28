FROM alpine:3.23

# gitea-openrc / caddy-openrc carry the init scripts; the bare packages don't.
RUN apk add --no-cache \
        bash \
        caddy \
        caddy-openrc \
        curl \
        git \
        gitea \
        gitea-openrc \
        openrc \
        pnpm \
        python3 \
        su-exec \
        uv

# --- OpenRC, made container-safe ---------------------------------------------
# rc_sys=docker makes OpenRC skip services keyworded -docker (networking,
# localmount, net-online, ...) instead of trying and failing to start them.
# rc_cgroup_mode=none stops it writing to the read-only /sys/fs/cgroup.
# rc_env_allow lets `docker run -e ...` reach the supervised services.
RUN sed -i \
        -e 's/^#\?rc_sys=.*/rc_sys="docker"/' \
        -e 's/^#\?rc_env_allow=.*/rc_env_allow="*"/' \
        -e 's/^#\?rc_logger=.*/rc_logger="NO"/' \
        -e 's/^#\?rc_cgroup_mode=.*/rc_cgroup_mode="none"/' \
        /etc/rc.conf \
    # hwdrivers and machine-id `need dev`, which only udev provides -- and udev
    # has no business in a container. Neither script is in a runlevel, but
    # depscan walks all of /etc/init.d, so they have to go to silence the
    # "needs non existent service 'dev'" errors.
    && rm -f /etc/init.d/hwdrivers /etc/init.d/machine-id \
    # No ttys here, and we never switch runlevels via init.
    && sed -i '/^tty[0-9]/d' /etc/inittab

# Service config: see conf.d/gitea and conf.d/caddy.
COPY conf.d/ /etc/conf.d/
RUN rc-update add gitea default \
    && rc-update add caddy default

# Template, copied into /data on first boot so it stays editable at runtime.
COPY Caddyfile /usr/share/localcode/Caddyfile

RUN addgroup -g 1000 localcode \
    && adduser -D -u 1000 -G localcode localcode

ENV HOME=/home/localcode
ENV SHELL=/bin/bash
ENV PATH="/home/localcode/bin:${PATH}"

# Where the localcode checkout is expected to be mounted (`docker run -v
# "$PWD:/workspace"`); the ~/bin/localcode shim below resolves against it.
ENV LOCALCODE_SRC=/workspace
# Alpine's python3 already matches requires-python, so never download another.
ENV UV_PYTHON_PREFERENCE=only-system
# Keep the venv out of the bind mount: /workspace/.venv would collide with the
# host's own, and these wheels are musl-linked.
ENV UV_PROJECT_ENVIRONMENT=/home/localcode/.venv
# Hardlinking from the uv cache into the venv can't cross the mount boundary.
ENV UV_LINK_MODE=copy

USER localcode
RUN mkdir -p /home/localcode/bin \
    && HOME=/home/localcode curl -fsSL https://opencode.ai/install | sh \
    && ln -s /home/localcode/.opencode/bin/opencode /home/localcode/bin/opencode

# Runs the CLI out of $LOCALCODE_SRC. Swap it for `uv tool install --editable
# "$LOCALCODE_SRC"` if you'd rather have a real entry point on PATH.
COPY --chmod=0755 localcode /home/localcode/bin/localcode

# OpenRC has to be PID 1's job, so the entrypoint runs as root; gitea and caddy
# drop to their own package users, and interactive work uses `-u localcode`.
USER root

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Mutable state all lives in /data; mount a volume over it to keep it.
WORKDIR /home/localcode
EXPOSE 80

ENTRYPOINT ["/entrypoint.sh"]
