"""Single source of truth for putting this project's per-directory modules
(db.py at the root, broker/, news/, trading/) on sys.path as flat top-level
imports (`import trade_bot`, `import upstox_client`, ...) -- used by
conftest.py (pytest) and by any standalone script outside pytest (e.g.
analysis/backtest.py) that needs the same imports. Kept in one place so a
script reusing production logic (trade_bot._qualifies, etc.) can't drift
from how pytest itself resolves the same modules.
"""
import os
import sys

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def setup():
    for sub in ("", "broker", "news", "trading"):
        p = os.path.join(BASE_DIR, sub) if sub else BASE_DIR
        if p not in sys.path:
            sys.path.insert(0, p)
