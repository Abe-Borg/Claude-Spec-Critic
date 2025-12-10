#!/usr/bin/env python
"""Entry point for PyInstaller executable."""
import sys
import os

# Add the src directory to the path
if getattr(sys, 'frozen', False):
    # Running as compiled executable
    base_path = sys._MEIPASS
else:
    # Running as script
    base_path = os.path.dirname(os.path.abspath(__file__))

sys.path.insert(0, base_path)

# Now import and run using absolute imports
from src.cli import main

if __name__ == '__main__':
    main()
