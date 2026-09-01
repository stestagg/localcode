# localcode

A bunch of agents all working together to build some software. See
[docs/project/vision.md](docs/project/vision.md).

## Shape

The controller runs on your machine. It spawns containers for the jobs that need
one, and nothing else runs in docker.

```
host                                 docker
────                                 ──────
localcode run <path>                 ┌─ localcode-hub-<id> ───────────┐
 ├ ws server  0.0.0.0:<ws>  ◀────────┤ caddy :80                      │
 │   runtime secret          /ws     │   /        → the web ui        │
 │   gitea automation/persona tokens │   /gitea/  → gitea :3000       │
 └ spawns ────────┐                  │ mounts <repo> rw at /repo      │
                  │                  └────────────────────────────────┘
                  │                  ┌─ localcode-agent-<id>-<run> ───┐
                  └─────────────────▶│ fresh clone from gitea         │
                                     │ work → commit → push → PR      │
                                     │ --rm, exits when done          │
                                     └────────────────────────────────┘
browser → http://localhost:8080/
```

The **hub** stays up for as long as `localcode run` does: caddy in front, gitea
behind it, and the web ui on top. It has no agent runtime in it.

An **agent** is one throwaway container per piece of work. It never sees your
checkout -- it clones from gitea, does the work, pushes a branch and opens a
pull request, and is gone the moment it finishes.

Agents reach the hub as `http://localcode/` on a per-project docker network; you
reach the same thing on `http://localhost:8080/`.

## Getting started

```sh
./localcode init <path>          # set an existing (or new) repo up
./localcode clone <url> [<path>] # or start from somewhere else
./localcode run [<path>]         # bring it up; Ctrl-C takes it down
```

`./localcode` is a small shell script that runs the CLI out of this checkout. Link it onto your PATH and it goes on working from anywhere, because
it resolves the checkout it lives in rather than the directory you are in:

```sh
ln -s "$PWD/localcode" ~/.local/bin/localcode
```

Which directory you are in still decides which *project* a command means, the
same as `git`.

`run` stays in the foreground and prints the gitea sign-in it generated. Flags:
`--port` selects the published HTTP port (8080 by default), `--rebuild` rebuilds
the images, `--dev-ui` serves the ui live (below), and `--no-browser` leaves
your browser alone.

`-C <path>` goes before the command and works the way `git -C` does: every
command then acts on the project there. It is the only way to point the `llm`
commands at a project, since those take no path of their own.

Everything is per-project: the containers, the network and the ports are keyed
by a hash of the repo path, so several projects run side by side.

## What `init` puts in your repo

```
<repo>/
├─ .git/
└─ .localcode/
   ├─ personas/       reusable perspectives and general attitudes
   ├─ roles/          reusable task-specific instructions
   ├─ stories/        stories organized by lifecycle stage
   ├─ tools/          project-specific workflow tools
   ├─ opencode.json   which model the agents use (yours to edit)
   └─ state/          gitignored: gitea's db, the master repo, secrets and keys
```

The project name comes from the repository directory. Its lowercased, URL-safe
form is used as the Gitea repository name under the fixed `localcode` owner.
The HTTP port is runtime-only and can be selected with `--port`.

Everything outside `state/` is committed on the dedicated `localcode` branch.
That branch is rebased onto `main` whenever localcode starts from a clean tree,
and its `.localcode/` snapshot wins over anything accidentally present on
`main`. Functional agent branches start directly from `main`; their commits and
pull requests explicitly exclude `.localcode/`.

You are signed into gitea automatically as the `human` administrator. Caddy is
the only thing that can reach it, so caddy is what says who you are: it stamps
`X-WEBAUTH-USER: human` on requests that arrive without credentials, and strips
the header from any request that brought its own. The controller's API calls
and host Git pushes authenticate separately as `localcode`; each agent's clone
and push authenticates as that agent. This keeps manual edits and reviews
attributed to `human`, while automated repository maintenance is attributed to
`localcode`. The `human` password that `run` prints is for signing in from
somewhere caddy is not in front of.

Each run combines any configured persona with any configured role. The persona
supplies the agent's perspective and is also its Gitea identity; the role
supplies the task-specific instructions. There is no allow-list connecting the
two catalogs.

Git smart HTTP starts without an `Authorization` header and sends Basic
credentials only after the server challenges it. Caddy recognizes Git and Git
LFS user agents and passes that first request through without the browser
identity, allowing Gitea to issue the challenge and attribute the retry to the
account embedded in the remote URL.

## The master repo

Gitea holds the master, as a bare repo inside `state/`. `run` creates it if it
is missing and pushes your checkout into it, leaving a `localcode` remote
behind:

```sh
git push localcode main   # publish what you have
git pull localcode main   # take what the agents merged
```

The push goes over http rather than straight at the bare repo on disk, because
gitea's receive hooks have to run inside the container where the gitea binary
they call actually lives.

## Choosing an LLM

Agents run [opencode](https://opencode.ai/) inside their container, so a project
says which model it wants in opencode's own config format: a `provider/model`
string, resolved against [models.dev](https://models.dev/)'s catalogue of some
two hundred providers. Adopting both means a provider localcode has never heard
of still works on the day it appears -- and localcode has heard of none of them,
by design. No provider is named anywhere in its source.

```sh
localcode llm configure                 # log in; it will ask who to
localcode llm configure-llamacpp        # or point it at a llama-server
localcode llm ask "say hello"           # prove it reaches something
```

All three act on the project you are standing in, or the one `-C` names.
[models.dev](https://models.dev/) is where to look up a model id; `opencode
models` inside an agent lists the same thing.

`configure` does not implement logging in. It runs `opencode providers login`
in the agents' image, with your terminal attached to it -- so API keys, OAuth,
device codes and whatever a provider invents next all work exactly as they do
in opencode, and go on working when opencode changes them. Naming a provider
(`configure some-provider`) skips its first question. It needs a terminal:
localcode has no way to store a credential of its own, because storing one
would mean understanding the file, and it deliberately does not. A machine
without a terminal gets the file the same way it would get any other secret --
copied in, or written by `opencode auth login` elsewhere.

Two files matter, and the split between them is the whole idea:

| path | what it is |
| --- | --- |
| `.localcode/opencode.json` | the model selection and any provider blocks. Committed on the `localcode` branch, so it travels with the project. |
| `.localcode/state/opencode-auth.json` | the credential, in opencode's own auth format. Gitignored, 0600, and never leaves this machine except into a container. |

**localcode does not write the config**, apart from the one block it can ask a
server for: see `configure-llamacpp` below. It is yours to edit -- opencode's own
format, `$schema` line and all, so an editor completes it and anything opencode
accepts works here. `configure` mounts both files into the container at the
paths opencode reads, so the login flow writes the credential in place; the
config goes in read-only, because a login has no business editing it.

localcode never parses either file. It finds them, forwards their bytes, and
creates the credential store empty when there is none so there is something to
mount. Everything that understands opencode's format -- comments and trailing
commas in the config, whatever shape a credential is this month -- runs in the
container, which is also the only place either file is read.

Nothing outside docker ever calls a model, and nothing outside docker chooses
one. `llm ask` starts one more throwaway container from the agents' image,
hands it those two files as environment variables and the prompt on stdin, and
streams back what comes out. The container reads which model to use out of the
config it was given, so the selection is made in one place -- the file -- and
there is no flag that can disagree with it.

Inside, it resolves that provider the way opencode does, because it is the same
resolution: the provider id is looked up in models.dev, which says which npm
package implements it, and that package is loaded and handed a base url and a
key. Nothing else about the provider is known in here -- which endpoint, which
wire protocol, how the answer is shaped are all the package's business. That is
why there is no table of providers in this repository to go stale, and why an
`ask` that works is evidence that an agent run will: the two agree by
construction rather than by coincidence. It runs on bun, next to opencode, in
`infra/docker/agent/ts/src/llm/` -- which has its own tests, run in the image
that already has a bun rather than one you have to install:

```sh
docker run --rm --entrypoint bun localcode-agent test /opt/localcode/src
```

A package the image was not built with is fetched from npm the first time a
provider needs it, which costs a second or two on that first question. An image
that has to run without a registry can be built with them already there:
`--build-arg PRELOAD="@ai-sdk/anthropic @ai-sdk/openai"`.

A key comes from an explicit `apiKey` in the config first, then the credential
store, then whichever environment variable models.dev says that provider
answers to -- so a key already exported for another tool is picked up without
being copied anywhere. An `apiKey` of `""` is an answer rather than a miss: see
below.

### A server on your own machine

Anything speaking the OpenAI shape, whether or not models.dev has heard of it.
Write the provider block yourself:

```json
{
  "$schema": "https://opencode.ai/config.json",
  "model": "my-server/a-model-it-serves",
  "provider": {
    "my-server": {
      "npm": "@ai-sdk/openai-compatible",
      "options": { "baseURL": "http://localhost:11434/v1", "apiKey": "" }
    }
  }
}
```

`localcode llm ask "say hello"` should then answer. Three things in there are
doing work:

`npm` -- which package implements this provider, for one models.dev cannot
resolve. Both opencode and `llm ask` read it, and it wins over the catalogue in
both, so it is the field to reach for when a provider needs pinning to
something specific. localcode will not guess one for you, because guessing
means knowing one provider from another and it deliberately knows none. A
provider models.dev does know needs no `npm` at either end; one it does not
needs either this or a `baseURL`, and saying neither is an error that names
both.

`"apiKey": ""` -- an empty key on purpose, meaning this server wants none.
Leaving the field out means the opposite: go and find a key, and complain when
there is none. (A placeholder is sent in place of the empty string, because the
SDK's client will not build without something; a server that wants no key does
not read it.)

`localhost` -- correct as written, and wrong inside a container, where it is
the container. So the config keeps the url you wrote and the copy handed to an
agent gets `host.docker.internal` in its place; `localhost`, `127.0.0.1`,
`0.0.0.0` and `[::1]` are all rewritten, and only when they are the host part
of a url. Agents are started with `--add-host host.docker.internal:host-gateway`
for this, so it resolves on linux as well as Docker Desktop.

Say `baseURL` explicitly even for a provider models.dev already knows. A
catalogue entry for something self-hosted carries a loopback address of its
own, and both opencode and the AI SDK read that catalogue from *inside* the
container, where such an address is nothing at all. An override in your config
is what gets rewritten; a default that never appears in the file cannot be.

Bind the server to more than loopback if you are on linux, or the rewritten
address will reach your machine and find nothing listening: `OLLAMA_HOST=0.0.0.0
ollama serve`.

### llama.cpp, without typing the block

llama-server will describe itself, so that is the one case where localcode
writes the config rather than asking you to:

```sh
localcode llm configure-llamacpp                       # http://localhost:8080
localcode llm configure-llamacpp http://localhost:9931/
```

It reads `/v1/models` for what the server is serving and `/props` for the
context it was started with, and writes `.localcode/opencode.json` -- the block
above, with the models filled in and the same deliberate `"apiKey": ""`. A url
ending in `/v1` is accepted too: it is the same server, and the one every other
tool asks for. A `llama serve` router has no single context to report, so no
`limit` is written and opencode's defaults apply.

The one thing the server cannot answer is which of its models you want, so it
lists them and asks -- by number, since the ids are often file names:

```
localcode: this server serves
  1. qwen3-coder-30b
  2. unsloth/gpt-oss-120b-GGUF
localcode: which one is the default [1]: 2
```

That writes the top-level key the rest of localcode reads:
`"model": "llama.cpp/unsloth/gpt-oss-120b-GGUF"`. It is qualified with the
provider because an unqualified id sends opencode looking through a catalogue
this server is not in. A server serving one model is shown but not asked about.

It replaces the file rather than merging with it. If you have hand-written
anything in there, this is not the command for you.

### How it reaches an agent

An agent container mounts nothing from the host, so both files travel to it as
environment variables and the entrypoint writes them to
`~/.config/opencode/opencode.json` and `~/.local/share/opencode/auth.json`
before the runner starts. `{env:NAME}` placeholders are left for opencode to
expand at the far end, with the variables they name forwarded from your shell;
`{file:...}` placeholders name paths only the host has, so those are expanded
before the config leaves.

That does put the key where `docker inspect` can see it, the same as the gitea
token already is. Bind-mounting the host's auth file instead would either be
read-only, breaking opencode's token refresh, or would let a container write
back into `.localcode/state/`.

## The websocket

The browser is a thin client: it fetches `/config.json` from caddy for the
socket path and the runtime secret, opens `/ws`, and sends binary MessagePack
maps such as `{"action": "<name>"}`. Actions are the functions registered with `@ws_handler`
in `server/commands.py` -- `status`, `agent.run`, `agent.stop`, `gitea.pulls` --
and nothing else is reachable.

Every websocket frame is MessagePack encoded, so byte strings remain native
binary values instead of being escaped or base64 encoded. The first frame has
to be `{"action": "auth", "secret": ...}` or the socket closes. That matters
because the controller binds every interface: caddy reaches
it from inside docker over the host gateway, so network position guarantees
nothing and the secret is the whole of the defence.

A handler gets a `WsCommand` and can stream progress back, either to the caller
or to every open tab:

```python
@ws_handler(name="agent.run")
async def agent_run(command: WsCommand) -> None:
    await agent.run(command.project, command.data["runner"], command.broadcast_stream())
```

`run_command` lives in `driver/process.py` and knows nothing about the web: it
takes any `async (event) -> None` stream. Events are `start`, `stdout`,
`stderr`, `exit` and `error`, each stamped with the command's id, so one socket
can carry several runs at once and the docker CLI can be streamed as-is.

### Story implementation process

The non-interactive `story` process takes a persona and a repository-relative
story path:

```json
{
  "action": "process.start",
  "process": "story",
  "persona": "antirez-dev",
  "story": ".localcode/stories/ready/07-add-password-reset.md"
}
```

The path must name an existing `NN-*.md` file under a known
`.localcode/stories/<stage>/` directory. `stories.list` includes this path as
the `path` field so callers do not have to derive it from a Gitea URL.

One process container and one clone own the whole run. OpenCode first composes
the selected persona with the required `story-pre-dev` role. That turn can
inspect the current code and edit only the selected story; the process rejects
other changes. The reviewed bytes are then carried, without a commit, into a
second OpenCode turn composed with `story-developer`. The functional branch is
created from `main`, while the local `.localcode` tree is excluded from Git so
the reviewed story remains readable but cannot enter the code diff.

The image includes the official Gitea MCP server. The process adds its
`pull_request_write` tool to the container's OpenCode config only after the
pre-development turn has ended. The developer turn is instructed to commit and
push the functional branch and use that MCP tool to open a pull request into
`main`. The process finishes successfully only after it independently verifies
that no metadata entered the commits, the pushed HEAD matches, and the pull
request is open.

In this first slice, pre-development story edits are process-local handoff
state. They are not silently committed to the metadata branch; deciding how a
later lifecycle stage records those edits and the PR id remains part of the
continuation of this process.

## Web ui

`web/` is a Vite + React app. It is built into the hub image, so a normal
`localcode run` serves it as static files from caddy.

```sh
./localcode run --dev-ui
```

runs the hub's dev variant instead: the same caddy and gitea, plus a vite dev
server supervised alongside them with `web/` bind-mounted in. Edit the react
source and the page reloads -- nothing restarts, and gitea never notices.
`node_modules` lives in a docker volume, so the install survives restarts and
stays out of your checkout.

## Images

Both are built from this checkout on first use, and rebuilt with `--rebuild`.

| image | what it is |
| --- | --- |
| `localcode-hub` | alpine, openrc, caddy, gitea, the built ui |
| `localcode-hub-dev` | the same plus node, serving the ui from vite |
| `localcode-agent` | git, node, python, and the `opencode` binary the agents run |

```sh
docker build -f infra/docker/hub/Dockerfile --target hub -t localcode-hub .
docker build -f infra/docker/agent/Dockerfile -t localcode-agent .
```

## Runners

An agent runs one *runner*: a script in `infra/docker/agent/runners/`, executed
in the fresh clone.

| runner | what it does |
| --- | --- |
| `hello` | appends a line to a file. Proves the loop end to end -- clone, change, branch, push, pull request -- and needs no LLM. |
| `opencode` | combines the selected persona and role instructions for `opencode run` and lets it edit the clone. Needs a provider configured. |

Either way the runner only makes changes. Committing them, branching from
`main`, pushing and opening the pull request is the entrypoint's job, which is
why a runner that fails takes the whole run down with it rather than producing
an empty pull request.
