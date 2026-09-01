#!/bin/sh
# PID 1 for the hub: point /data at the project's state directory, make gitea
# and caddy write as the host user, choose the routing, then hand off to OpenRC.
#
# app.ini is written by the controller on the host before this container starts,
# so nothing here generates configuration for gitea.
set -eu

: "${LOCALCODE_UID:?LOCALCODE_UID is required}"
: "${LOCALCODE_GID:?LOCALCODE_GID is required}"

# The whole repo is mounted at /repo; its state directory is where every
# service keeps what it writes. The symlink is what lets conf.d and the
# Caddyfile talk about /data instead of a path six levels deep.
ln -sfn /repo/.localcode/state /data

# Both services run as the user that owns the checkout, so the bare master
# gitea creates is one the controller can push into directly, and nothing in
# .localcode/state/ ends up owned by a uid the host has never heard of.
#
# The gid may already be taken by one of alpine's own groups -- only the number
# matters for the files in the mount, so an existing one is as good as ours.
if ! awk -F: -v gid="$LOCALCODE_GID" '$3 == gid { found = 1 } END { exit !found }' /etc/group; then
    groupadd -o -g "$LOCALCODE_GID" localcode
fi

# -o so an id already in use on the host side is not an obstacle. caddy runs
# as this account too, via CADDY_USER in conf.d/caddy.
usermod -o -u "$LOCALCODE_UID" -g "$LOCALCODE_GID" gitea

mkdir -p \
    /data/gitea/data /data/gitea/repos /data/gitea/log \
    /data/gitea/custom /data/gitea/tmp /data/gitea/home \
    /data/caddy/data /data/caddy/config /data/caddy/log

chown "$LOCALCODE_UID:$LOCALCODE_GID" \
    /data/gitea /data/gitea/* /data/caddy /data/caddy/*
chmod 700 /data/gitea/home

# Where gitea finds the templates that put a way back to the project pages at
# the top of its own. rc.conf sets rc_env_allow="*", which is what carries this
# through openrc to the service.
export GITEA_CUSTOM=/usr/share/localcode/gitea-custom

case "${LOCALCODE_UI_MODE:-static}" in
    dev) cp /usr/share/localcode/Caddyfile.dev /data/caddy/Caddyfile ;;
    *)   cp /usr/share/localcode/Caddyfile /data/caddy/Caddyfile ;;
esac

# /run survives `docker stop` + `docker start`, so OpenRC can find state from a
# previous boot -- an rc.stopping left by the shutdown trap makes it refuse to
# start. Treat /run/openrc as throwaway, including the softlevel that tells
# OpenRC the system is booted.
rm -rf /run/openrc
mkdir -p /run/openrc
touch /run/openrc/softlevel
openrc default

echo "hub: caddy on :80, gitea behind /gitea/"

trap 'rc-service caddy stop; rc-service gitea stop; exit 0' INT TERM
while :; do
    sleep 86400 &
    wait $! || true
done
