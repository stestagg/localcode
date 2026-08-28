# localcode

See [docs/project/vision.md](docs/project/vision.md).

## Running

`./run` builds the image if it is missing, then starts the container with this
checkout mounted at `/workspace` and gitea on <http://localhost:8080/>:

```sh
./run                    # interactive shell, gitea and caddy up behind it
./run localcode hello    # run one command and exit
./run --build            # force an image rebuild first
```

`LOCALCODE_PORT`, `LOCALCODE_IMAGE` and `LOCALCODE_VOLUME` override the
defaults (`8080`, `localcode`, `localcode-data`). Gitea's state lives in the
named volume, so it survives across runs; `docker volume rm localcode-data`
starts over.

Outside the container:

```sh
uv run localcode hello     # no install, straight from a checkout
uv tool install --editable .   # or: put `localcode` on PATH permanently
```
