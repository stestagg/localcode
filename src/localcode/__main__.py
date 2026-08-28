"""Allow `python -m localcode` alongside the `localcode` script."""

import sys

from .cli import main

sys.exit(main())
