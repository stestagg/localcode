#!/bin/sh
# PID 1 for the localcode container: lay out /data, seed config on first boot,
# hand the runtime to OpenRC, then run CMD (or idle if there isn't one).
set -eu

GITEA_DIR=/data/gitea
CADDY_DIR=/data/caddy
GITEA_CONF="$GITEA_DIR/conf/app.ini"
CADDYFILE="$CADDY_DIR/Caddyfile"

# Address Caddy listens on. ":80" serves plain HTTP; set a hostname instead and
# Caddy will provision a certificate for it.
SITE_ADDRESS="${SITE_ADDRESS:-:80}"
# Must match what a browser types, or gitea builds broken links and redirects.
GITEA_ROOT_URL="${GITEA_ROOT_URL:-http://localhost/}"
GITEA_ADMIN_USERNAME="${GITEA_ADMIN_USERNAME:-localcode}"
GITEA_ADMIN_EMAIL="${GITEA_ADMIN_EMAIL:-localcode@localhost}"

# app.ini is the marker: gitea rewrites it at runtime, so it only ever gets
# written once, and after that /data is the source of truth.
first_boot=false
[ -f "$GITEA_CONF" ] || first_boot=true

mkdir -p \
    "$GITEA_DIR/conf" "$GITEA_DIR/data" "$GITEA_DIR/repos" "$GITEA_DIR/log" \
    "$GITEA_DIR/custom" "$GITEA_DIR/tmp" "$GITEA_DIR/home" "$GITEA_DIR/ssh" \
    "$CADDY_DIR/data" "$CADDY_DIR/config" "$CADDY_DIR/log"

if [ "$first_boot" = true ]; then
    cat > "$GITEA_CONF" <<INI
APP_NAME  = localcode
RUN_USER  = gitea
RUN_MODE  = prod
WORK_PATH = $GITEA_DIR

[server]
PROTOCOL         = http
; Only Caddy talks to gitea; it is not reachable from outside the container.
HTTP_ADDR        = 127.0.0.1
HTTP_PORT        = 3000
ROOT_URL         = $GITEA_ROOT_URL
APP_DATA_PATH    = $GITEA_DIR/data
DISABLE_SSH      = true
; Unused while SSH is off, but gitea still creates it -- keep it out of /var.
SSH_ROOT_PATH    = $GITEA_DIR/ssh
LFS_START_SERVER = true
LFS_JWT_SECRET   = $(gitea generate secret LFS_JWT_SECRET)

[database]
DB_TYPE             = sqlite3
PATH                = $GITEA_DIR/data/gitea.db
SQLITE_JOURNAL_MODE = WAL

[repository]
ROOT           = $GITEA_DIR/repos
DEFAULT_BRANCH = main

[repository.upload]
TEMP_PATH = $GITEA_DIR/tmp/uploads

[git]
HOME_PATH = $GITEA_DIR/home

[log]
MODE      = file
ROOT_PATH = $GITEA_DIR/log
LEVEL     = info

[security]
INSTALL_LOCK   = true
SECRET_KEY     = $(gitea generate secret SECRET_KEY)
INTERNAL_TOKEN = $(gitea generate secret INTERNAL_TOKEN)

[service]
DISABLE_REGISTRATION = true
REQUIRE_SIGNIN_VIEW  = false

[session]
PROVIDER        = file
PROVIDER_CONFIG = $GITEA_DIR/data/sessions

[oauth2]
JWT_SECRET = $(gitea generate secret JWT_SECRET)

[cron.update_checker]
ENABLED = false
INI
fi

if [ ! -f "$CADDYFILE" ]; then
    cp /usr/share/localcode/Caddyfile "$CADDYFILE"
fi

if [ "$first_boot" = true ]; then
    chown -R gitea:www-data "$GITEA_DIR"
    chown -R caddy:caddy "$CADDY_DIR"
else
    # Cheap pass for directories this boot just created; a recursive chown over
    # a populated repo tree would be slow for no gain.
    chown gitea:www-data "$GITEA_DIR" "$GITEA_DIR"/*
    chown caddy:caddy "$CADDY_DIR" "$CADDY_DIR"/*
fi
chmod 700 "$GITEA_DIR/home"

# Create the first admin before the web service comes up, so the instance is
# never briefly reachable with no account on it.
if [ "$first_boot" = true ]; then
    admin_password="${GITEA_ADMIN_PASSWORD:-$(head -c 18 /dev/urandom | base64)}"

    # `admin user create` will not build the schema itself, so seed the empty
    # SQLite file first.
    su-exec gitea:www-data env GITEA_WORK_DIR="$GITEA_DIR" \
        gitea migrate --config "$GITEA_CONF"

    su-exec gitea:www-data env GITEA_WORK_DIR="$GITEA_DIR" \
        gitea admin user create \
            --admin \
            --username "$GITEA_ADMIN_USERNAME" \
            --password "$admin_password" \
            --email "$GITEA_ADMIN_EMAIL" \
            --must-change-password=false \
            --config "$GITEA_CONF"

    if [ -z "${GITEA_ADMIN_PASSWORD:-}" ]; then
        printf '%s\n' "$admin_password" > "$GITEA_DIR/conf/admin-password"
        chown gitea:www-data "$GITEA_DIR/conf/admin-password"
        chmod 600 "$GITEA_DIR/conf/admin-password"
        echo "gitea admin '$GITEA_ADMIN_USERNAME' password: $admin_password"
        echo "(also saved to $GITEA_DIR/conf/admin-password)"
    fi
fi

# /run survives `docker stop` + `docker start`, so OpenRC can find state from
# the previous boot -- an rc.stopping left by the shutdown trap will make it
# refuse to start. Treat /run/openrc as throwaway and rebuild it, including the
# softlevel that tells OpenRC the system is booted.
rm -rf /run/openrc
mkdir -p /run/openrc
touch /run/openrc/softlevel
openrc default

echo "gitea is up behind caddy on $SITE_ADDRESS ($GITEA_ROOT_URL)"

# Services are root's job; anything interactive is the localcode user's, which
# is where opencode and the dev tooling live.
as_localcode() {
    exec su-exec localcode env HOME=/home/localcode USER=localcode "$@"
}

if [ "$#" -gt 0 ]; then
    as_localcode "$@"
elif [ -t 0 ]; then
    # `docker run -it`: drop to a prompt with the services running behind it.
    # Not a login shell -- /etc/profile hard-resets PATH and would drop ~/bin.
    as_localcode /bin/bash
fi

# `docker run -d`: no tty and no command, so stay alive as PID 1 to keep the
# supervised services up, and stop them cleanly when docker signals us.
trap 'rc-service caddy stop; rc-service gitea stop; exit 0' INT TERM
while :; do
    sleep 86400 &
    wait $! || true
done
