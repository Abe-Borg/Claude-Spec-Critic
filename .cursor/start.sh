#!/usr/bin/env bash
# Per-boot startup for the Spec Critic environment.
#
# The hermetic test suite needs no display, but the app itself is a desktop
# GUI. Provide a headless X server on display :99 so the GUI can be launched on
# demand with:  DISPLAY=:99 .venv/bin/python main.py
#
# Idempotent: reuse an already-running Xvfb instead of starting a duplicate.
set -euo pipefail

if ! pgrep -x Xvfb >/dev/null 2>&1; then
  Xvfb :99 -screen 0 1280x900x24 >/tmp/xvfb.log 2>&1 &
fi

echo "Xvfb is available on DISPLAY=:99 for the desktop GUI."
