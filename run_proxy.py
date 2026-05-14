#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path


if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ag_proxy.server import main


if __name__ == "__main__":
    main()
