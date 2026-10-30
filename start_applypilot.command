#!/bin/zsh
set -eu

cd -- "${0:A:h}"
source .venv/bin/activate

echo "ApplyPilot - resuming scoring and preparing applications with Cursor"
echo "Completed scores are kept. No applications will be submitted."
python -u -m applypilot run score tailor cover pdf --validation strict
