#!/bin/sh
# Clone, run one runner, and turn whatever it changed into a pull request.
set -eu

: "${LOCALCODE_GITEA_URL:?}"
: "${LOCALCODE_USER:?}"
: "${LOCALCODE_TOKEN:?}"
: "${LOCALCODE_OWNER:?}"
: "${LOCALCODE_REPO:?}"
: "${LOCALCODE_BASE:?}"
: "${LOCALCODE_METADATA_BRANCH:?}"
: "${LOCALCODE_BRANCH:?}"
: "${LOCALCODE_RUNNER:?}"
: "${LOCALCODE_ROLE:?}"

# opencode reads its configuration and its credentials from fixed paths. Nothing
# is mounted into this container, so the controller hands both over as
# environment variables and they become files here, once, before any runner
# starts. Absent when the project has not configured a provider.
config_dir="${XDG_CONFIG_HOME:-$HOME/.config}/opencode"
data_dir="${XDG_DATA_HOME:-$HOME/.local/share}/opencode"

if [ -n "${LOCALCODE_OPENCODE_CONFIG:-}" ]; then
    mkdir -p "$config_dir"
    printf '%s' "$LOCALCODE_OPENCODE_CONFIG" > "$config_dir/opencode.json"
    echo "agent: opencode config at $config_dir/opencode.json"
fi

if [ -n "${LOCALCODE_OPENCODE_AUTH:-}" ]; then
    mkdir -p "$data_dir"
    # 0600 from the moment it exists, the same as it is on the host.
    (umask 077 && printf '%s' "$LOCALCODE_OPENCODE_AUTH" > "$data_dir/auth.json")
    echo "agent: opencode credentials at $data_dir/auth.json"
fi

# They are files now. Nothing a runner spawns needs to inherit the credential.
unset LOCALCODE_OPENCODE_CONFIG LOCALCODE_OPENCODE_AUTH

runner="/runners/$LOCALCODE_RUNNER"
if [ ! -x "$runner" ]; then
    echo "agent: no runner named '$LOCALCODE_RUNNER'" >&2
    exit 2
fi

api="$LOCALCODE_GITEA_URL/api/v1"
scheme=${LOCALCODE_GITEA_URL%%://*}
hostpath=${LOCALCODE_GITEA_URL#*://}
# Credentials go in the url rather than a config file, so they live only as long
# as the container does.
origin="$scheme://$LOCALCODE_USER:$LOCALCODE_TOKEN@$hostpath/$LOCALCODE_OWNER/$LOCALCODE_REPO.git"

echo "agent: cloning $LOCALCODE_OWNER/$LOCALCODE_REPO"
git clone --quiet --branch "$LOCALCODE_METADATA_BRANCH" "$origin" /work/repo
cd /work/repo

# The email matches the gitea account, which is how gitea attributes the
# commits to the named persona rather than to nobody.
git config user.name "$LOCALCODE_USER"
git config user.email "$LOCALCODE_USER@localcode.local"
echo "agent: running $LOCALCODE_RUNNER"
"$runner"

if [ -z "$(git status --porcelain)" ]; then
    echo "agent: $LOCALCODE_RUNNER changed nothing, no pull request"
    exit 0
fi

# Metadata has its own permanent branch. Commit and publish only that directory;
# source edits remain unstaged in the working tree.
if [ -n "$(git status --porcelain -- .localcode)" ]; then
    git add -A -- .localcode
    git commit --quiet -m "$LOCALCODE_USER/$LOCALCODE_ROLE/$LOCALCODE_RUNNER metadata: ${LOCALCODE_RUN_ID:-run}"
    git push --quiet origin "HEAD:$LOCALCODE_METADATA_BRANCH"
    echo "agent: updated $LOCALCODE_METADATA_BRANCH"
fi

# Move the remaining functional edits onto a branch created directly from
# main. Switching removes the clean, tracked .localcode tree, guaranteeing it
# is absent from both the commit and the PR diff.
git switch --quiet "$LOCALCODE_BASE"
git switch --quiet -c "$LOCALCODE_BRANCH"

if [ -z "$(git status --porcelain -- . ':!.localcode')" ]; then
    echo "agent: $LOCALCODE_RUNNER changed metadata only, no pull request"
    exit 0
fi

git add -A -- . ':!.localcode'
if [ -n "$(git diff --cached --name-only -- .localcode)" ]; then
    echo "agent: refusing to include .localcode in a functional commit" >&2
    exit 3
fi
git commit --quiet -m "$LOCALCODE_USER/$LOCALCODE_ROLE/$LOCALCODE_RUNNER: ${LOCALCODE_RUN_ID:-run}"
git push --quiet origin "$LOCALCODE_BRANCH"
echo "agent: pushed $LOCALCODE_BRANCH"

body=$(jq -n \
    --arg head "$LOCALCODE_BRANCH" \
    --arg base "$LOCALCODE_BASE" \
    --arg title "$LOCALCODE_USER/$LOCALCODE_ROLE/$LOCALCODE_RUNNER: ${LOCALCODE_RUN_ID:-run}" \
    '{head: $head, base: $base, title: $title}')

url=$(curl -fsS -X POST "$api/repos/$LOCALCODE_OWNER/$LOCALCODE_REPO/pulls" \
    -H "Authorization: token $LOCALCODE_TOKEN" \
    -H "Content-Type: application/json" \
    -d "$body" | jq -r '.html_url')

echo "agent: opened $url"
