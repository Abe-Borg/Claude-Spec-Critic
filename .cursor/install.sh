#!/usr/bin/env bash
# Idempotent repository bootstrap for the Spec Critic Cloud Agent environment.
#
# Spec Critic is a Python 3.11+ CustomTkinter desktop app plus a hermetic test
# suite. This script installs the OS packages the GUI needs, creates a project
# virtualenv, and installs the pinned dependency set exactly as CI does
# (requirements-dev.txt -> editable package with --no-deps -> pip check).
set -euo pipefail

# Operate from the repository root regardless of where the script is invoked.
cd "$(dirname "$0")/.."

# --- System packages -------------------------------------------------------
# python3-tk : the Tk runtime for CustomTkinter / tkinterdnd2 (the GUI, and the
#              controller tests that require `tkinter` to be importable).
# xvfb       : a virtual X server so the desktop GUI can run headlessly.
# x11-utils / fonts-dejavu-core : basic X tooling and fonts for rendering.
# Node (the test-time JavaScript syntax check) already ships in the base image.
export DEBIAN_FRONTEND=noninteractive
sudo apt-get update -qq
sudo apt-get install -y -qq \
  python3-tk \
  python3-venv \
  xvfb \
  x11-utils \
  fonts-dejavu-core

# --- Python virtualenv -----------------------------------------------------
# Kept inside the repo at .venv (gitignored). PEP 668 marks the system Python
# as externally managed, so a venv is the supported way to install here.
if [ ! -x .venv/bin/python ]; then
  python3 -m venv .venv
fi

.venv/bin/python -m pip install --upgrade pip

# requirements-dev.txt pulls in requirements.txt (the runtime lock) and adds
# the pytest chain. Installing the package with --no-deps lets `pip check`
# validate pyproject's declared dependencies against the pinned set, exactly
# as the CI workflow does.
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/pip install -e . --no-deps
.venv/bin/python -m pip check

echo "Spec Critic environment ready. Use .venv/bin/python (e.g. .venv/bin/python -m pytest)."
