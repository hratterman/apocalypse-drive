#!/usr/bin/env python3
"""
Build app icons for distribution.

Single source of truth lives in bin/make_icons.py (yellow ASCII-block A on
dark navy). This script is the entry point that CI calls during release builds.
Kept as a thin wrapper so the workflow YAML doesn't need to change every time
the icon design moves around.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "bin"))

import make_icons  # noqa: E402

if __name__ == "__main__":
    sys.exit(make_icons.main())
