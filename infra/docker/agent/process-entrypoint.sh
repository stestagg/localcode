#!/bin/sh
# Give a workflow its own clone, then run its trusted script. Unlike the agent
# entrypoint this does not publish edits: process output is the session unless
# a future definition opts into an explicit publication policy.
set -eu

: "${LOCALCODE_GITEA_URL:?}"
: "${LOCALCODE_USER:?}"
: "${LOCALCODE_TOKEN:?}"
: "${LOCALCODE_OWNER:?}"
: "${LOCALCODE_REPO:?}"
: "${LOCALCODE_METADATA_BRANCH:?}"
: "${LOCALCODE_PROCESS:?}"

if [ "$#" -ne 1 ]; then
    echo "process: expected one trusted script path" >&2
    exit 2
fi

# Processes that use OpenCode get the same project-owned configuration and
# credential handoff as one-shot agents. Keeping these files in the process
# container is what lets several OpenCode turns share one environment without
# mounting any part of the host checkout.
config_dir="${XDG_CONFIG_HOME:-$HOME/.config}/opencode"
data_dir="${XDG_DATA_HOME:-$HOME/.local/share}/opencode"

if [ -n "${LOCALCODE_OPENCODE_CONFIG:-}" ]; then
    mkdir -p "$config_dir"
    printf '%s' "$LOCALCODE_OPENCODE_CONFIG" > "$config_dir/opencode.json"
    echo "process: opencode config at $config_dir/opencode.json"
fi

if [ -n "${LOCALCODE_OPENCODE_AUTH:-}" ]; then
    mkdir -p "$data_dir"
    (umask 077 && printf '%s' "$LOCALCODE_OPENCODE_AUTH" > "$data_dir/auth.json")
    echo "process: opencode credentials at $data_dir/auth.json"
fi

unset LOCALCODE_OPENCODE_CONFIG LOCALCODE_OPENCODE_AUTH

scheme=${LOCALCODE_GITEA_URL%%://*}
hostpath=${LOCALCODE_GITEA_URL#*://}
origin="$scheme://$LOCALCODE_USER:$LOCALCODE_TOKEN@$hostpath/$LOCALCODE_OWNER/$LOCALCODE_REPO.git"

echo "process: cloning $LOCALCODE_OWNER/$LOCALCODE_REPO"
git clone --quiet --branch "$LOCALCODE_METADATA_BRANCH" "$origin" /work/repo
cd /work/repo

git config user.name "$LOCALCODE_USER"
git config user.email "$LOCALCODE_USER@localcode.local"

echo "process: running $LOCALCODE_PROCESS"
exec bun "$1"
