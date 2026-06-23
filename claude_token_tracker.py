#!/usr/bin/env python3
"""Backwards-compat entry point. Real code lives in the `cct` package."""
from __future__ import annotations
import os
import sys


def main():
    # `claude-token-tracker` historically launched the GUI; preserve that.
    pkg_root = os.path.dirname(os.path.abspath(__file__))
    if pkg_root not in sys.path:
        sys.path.insert(0, pkg_root)
    from cct.gui import run
    run()


if __name__ == '__main__':
    main()
