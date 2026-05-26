#!/usr/bin/env bash
# Development environment activator for Linux.
# Run: source activate_dev.sh
#
# What this does:
#   1. Activates the project venv
#   2. Adds stubs/ to PYTHONPATH so MetaTrader5 stub resolves on Linux
#   3. Confirms interpreter path and installed packages
#
# On Windows production (MT5 terminal running):
#   - Use venv\Scripts\activate instead
#   - Do NOT add stubs/ to PYTHONPATH — real MetaTrader5 package takes precedence

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

source "${PROJECT_DIR}/venv/bin/activate"

export PYTHONPATH="${PROJECT_DIR}/stubs:${PYTHONPATH}"

echo "Venv : $(which python)"
echo "Stubs: ${PROJECT_DIR}/stubs (MetaTrader5 stub active)"
echo ""
echo "Run bot : python main.py"
echo "Run test: python -c \"import MetaTrader5 as mt5; print(mt5.initialize())\""
