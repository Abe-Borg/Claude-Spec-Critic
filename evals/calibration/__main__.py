"""Convenience entry point so ``python -m evals.calibration`` works the
same as ``python -m evals.calibration.runner``.
"""
from __future__ import annotations

import sys

from .runner import main


if __name__ == "__main__":
    sys.exit(main())
