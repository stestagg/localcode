# An agent: one throwaway container per piece of work.
#
# Built from the repo root:
#   docker build -f infra/docker/agent/Dockerfile -t localcode-agent .
#
# This is the only image that gets an agent runtime. It never sees the host
# checkout -- it clones from gitea, works, pushes a branch and opens a PR.
FROM docker.gitea.com/gitea-mcp-server:1.7.0 AS gitea-mcp

FROM alpine:3.23

# libstdc++ and unzip are for bun, below. python3 is not for anything in here:
# an agent has to build and test whatever the project is written in, and that is
# often python.
RUN apk add --no-cache \
        bash \
        curl \
        git \
        jq \
        libstdc++ \
        nodejs \
        npm \
        python3 \
        unzip

# The agent runtime. Its npm package ships musl builds, which is why this can
# be an alpine image at all.
ARG OPENCODE_VERSION=latest
RUN npm install -g "opencode-ai@${OPENCODE_VERSION}" \
    && opencode --version

# Official Gitea MCP server, narrowed further to pull-request tools by the
# story process configuration. The upstream image is scratch-based and keeps
# its static binary at this path.
COPY --from=gitea-mcp /app/gitea-mcp /usr/local/bin/gitea-mcp
RUN gitea-mcp --version

# bun, for localcode's own typescript in /opt/localcode -- model access for
# `localcode llm ask`, and the session client agent code reports through. Pinned
# and taken from the release archive directly: alpine has no package for it, and
# an agent runtime is not something to let float.
ARG BUN_VERSION=1.4.0
RUN set -eu; \
    case "$(uname -m)" in \
        x86_64)  arch=x64 ;; \
        aarch64) arch=aarch64 ;; \
        *) echo "no bun musl build for $(uname -m)" >&2; exit 1 ;; \
    esac; \
    curl -fsSL -o /tmp/bun.zip \
        "https://github.com/oven-sh/bun/releases/download/bun-v${BUN_VERSION}/bun-linux-${arch}-musl.zip"; \
    unzip -qj /tmp/bun.zip -d /tmp/bun; \
    install -m 0755 /tmp/bun/bun /usr/local/bin/bun; \
    rm -rf /tmp/bun /tmp/bun.zip; \
    bun --version

# The manifest before the source, so editing a script does not reinstall the
# world. The AI SDK, a JSONC parser and a MessagePack codec -- the last because
# an agent reports into a session over the controller's own websocket. No
# provider package, because which one a project needs is a runtime fact read out
# of its config -- see `ts/src/llm/provider.ts`, which fetches the named one if
# it is not already here.
WORKDIR /opt/localcode
COPY infra/docker/agent/ts/package.json infra/docker/agent/ts/bun.lock /opt/localcode/
# Keep the small type/test toolchain in the shared image as well. Built-in
# processes are image-owned TypeScript, so the image itself should be able to
# prove that the exact sources it ships still type-check and pass their tests.
RUN bun install --frozen-lockfile

# For an image that has to run without reaching npm: name the packages the
# providers you use are implemented by, and they are here already.
#   --build-arg PRELOAD="@ai-sdk/anthropic @ai-sdk/openai"
ARG PRELOAD=""
RUN [ -z "$PRELOAD" ] || bun add $PRELOAD

# models.dev, fetched rather than committed, so a provider id can be resolved to
# a package without this repository carrying a table of providers to go stale.
RUN curl -fsSL https://models.dev/api.json -o /opt/localcode/models.json \
    && jq -e 'length > 0' /opt/localcode/models.json > /dev/null

COPY infra/docker/agent/ts/tsconfig.json /opt/localcode/
COPY infra/docker/agent/ts/src/ /opt/localcode/src/
COPY --chmod=0755 infra/docker/agent/ts/scripts/ /opt/localcode/scripts/
COPY --chmod=0755 infra/docker/agent/ts/processes/ /opt/localcode/processes/

COPY --chmod=0755 infra/docker/agent/entrypoint.sh /entrypoint.sh
COPY --chmod=0755 infra/docker/agent/process-entrypoint.sh /process-entrypoint.sh
COPY --chmod=0755 infra/docker/agent/runners/ /runners/

# Root, deliberately. An agent has to build and test what it changed, which
# means installing whatever the project needs -- apk, npm, pip -- and a
# container that has to ask permission to do that is a container that fails at
# its job. The isolation is the container itself, not a user inside it: it
# clones from gitea rather than seeing your checkout, gets its own network, and
# is deleted the moment it exits. Passwordless sudo would come to the same
# thing while giving an agent one more way to get a command wrong.
#
# HOME is said explicitly because the entrypoint derives opencode's config and
# data directories from it, and those paths are agreed with `llm/container.py`.
ENV HOME=/root
WORKDIR /work
ENTRYPOINT ["/entrypoint.sh"]
