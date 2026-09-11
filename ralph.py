"""Thin entry point: `uv run ralph.py --spec specs/<name>.md [...]`."""

import sys
from pathlib import Path

# Running this file puts its directory first on sys.path, where `ralph.py` itself would
# shadow the `ralph` package. Put the package source first.
sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from ralph.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
