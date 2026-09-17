#!/usr/bin/env python3
"""dsh skill 入口：转调仓库 scripts/pit_financial.py（单一实现，勿在此堆逻辑）。"""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

from pit_financial import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
