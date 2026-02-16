"""PyInstaller entry point for MEP Spec Review."""
import sys
import os
from pathlib import Path

if getattr(sys, 'frozen', False):
    base_path = sys._MEIPASS
    sys.path.insert(0, base_path)
else:
    base_path = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, base_path)

from src.gui import main

if __name__ == "__main__":
    main()