# The hub: caddy in front, gitea behind it, and the web ui it serves.
#
# Built from the repo root so the ui source is in the context:
#   docker build -f infra/docker/hub/Dockerfile -t localcode-hub .
#
# No python, no uv, no agent runtime -- the controller runs on the host and
# agents get their own image.

# --- the web ui, built once at image time ------------------------------------
FROM node:22-alpine AS ui

WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY web/ ./
RUN npm run build

# --- the hub itself ----------------------------------------------------------
FROM alpine:3.23 AS hub

# gitea-openrc / caddy-openrc carry the init scripts; the bare packages don't.
# shadow is for the uid remap in the entrypoint.
RUN apk add --no-cache \
        caddy \
        caddy-openrc \
        curl \
        git \
        gitea \
        gitea-openrc \
        openrc \
        shadow

# OpenRC, made container-safe. rc_sys=docker makes it skip services keyworded
# -docker (networking, localmount, ...) instead of failing on them;
# rc_cgroup_mode=none stops it writing to the read-only /sys/fs/cgroup;
# rc_env_allow lets `docker run -e ...` reach the supervised services.
RUN sed -i \
        -e 's/^#\?rc_sys=.*/rc_sys="docker"/' \
        -e 's/^#\?rc_env_allow=.*/rc_env_allow="*"/' \
        -e 's/^#\?rc_logger=.*/rc_logger="NO"/' \
        -e 's/^#\?rc_cgroup_mode=.*/rc_cgroup_mode="none"/' \
        /etc/rc.conf \
    # hwdrivers and machine-id `need dev`, which only udev provides -- and udev
    # has no business in a container. Neither is in a runlevel, but depscan
    # walks all of /etc/init.d, so they have to go to silence its errors.
    && rm -f /etc/init.d/hwdrivers /etc/init.d/machine-id \
    # No ttys here, and we never switch runlevels via init.
    && sed -i '/^tty[0-9]/d' /etc/inittab

COPY infra/docker/hub/conf.d/ /etc/conf.d/
# The packaged caddy script pins command_user, but both services have to run as
# the one account the entrypoint remaps onto the host user -- otherwise the uid
# in the bind mount belongs to nobody the host knows.
RUN sed -i 's/^command_user=caddy:caddy$/command_user="${CADDY_USER:-caddy}"/' /etc/init.d/caddy \
    && rc-update add gitea default \
    && rc-update add caddy default

# Templates: the entrypoint picks one by LOCALCODE_UI_MODE and copies it into
# /data on every boot, so the routing always matches how the hub was started.
COPY infra/docker/hub/Caddyfile infra/docker/hub/Caddyfile.dev /usr/share/localcode/

# Gitea's custom directory. It lives in the image rather than under /data,
# because it is part of what the hub is, not part of a project's state.
COPY infra/docker/hub/gitea-custom/ /usr/share/localcode/gitea-custom/

COPY --from=ui /web/dist /srv/ui

COPY --chmod=0755 infra/docker/hub/entrypoint.sh /entrypoint.sh

EXPOSE 80
ENTRYPOINT ["/entrypoint.sh"]

# --- the same hub, serving the ui from a live vite dev server ----------------
FROM hub AS hub-dev

RUN apk add --no-cache nodejs npm

COPY --chmod=0755 infra/docker/hub/localcode-ui /usr/local/bin/localcode-ui
COPY --chmod=0755 infra/docker/hub/init.d/localcode-ui /etc/init.d/localcode-ui
RUN rc-update add localcode-ui default

ENV LOCALCODE_UI_MODE=dev
