#!/usr/bin/env python3
"""Entry point for Railway deployment."""

import sys
from pathlib import Path

# Add the app directory to Python path
sys.path.insert(0, str(Path(__file__).parent))

from src.main import run

if __name__ == "__main__":
    run()
