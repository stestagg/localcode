"""Which LLM this project uses, where its credentials are, and how to call it.

Two files, both opencode's own: `.localcode/opencode.json` says which model, and
`.localcode/state/opencode-auth.json` holds the credentials. Neither is written
here and neither is parsed here -- they are located, and their bytes are handed
to a container, which is where everything that understands them runs.

Nothing in this package calls a model, and nothing in it names a provider.
"""

from __future__ import annotations

from .config import ConfigError

__all__ = ["ConfigError"]
