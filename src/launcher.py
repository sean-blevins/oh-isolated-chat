#!/usr/bin/env python3
"""Runnable entry point for the oh-isolated-chat managed backend.

The built artifact lays this launcher out under ``bin/`` and the package under
``lib/``. The manifest runs this file directly, so its only job is to put
``lib/`` on ``sys.path`` and hand off to the package entry point.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

from isolated_chat_backend.__main__ import main  # noqa: E402

if __name__ == "__main__":
    main()
