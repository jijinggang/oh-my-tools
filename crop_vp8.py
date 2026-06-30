#!/usr/bin/env python3
"""CLI entry point: crop transparent edges from YUVA VP8 WebM videos."""
import sys
import os

# Ensure project root is in sys.path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from tools.crop_vp8 import main

if __name__ == "__main__":
    main()