#!/usr/bin/env python3
"""源码树里直接跑：`python3 src/main.py`（等价于 `python3 -m gnome_poster.main`）。"""

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from gnome_poster.main import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
