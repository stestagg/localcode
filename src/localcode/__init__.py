"""localcode: a bunch of agents all working together to build some software."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("localcode")
except PackageNotFoundError:  # running from a source tree that was never installed
    __version__ = "0+unknown"
