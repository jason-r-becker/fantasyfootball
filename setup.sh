#!/usr/bin/env bash

set -euo pipefail

if [[ $# -gt 1 || ( $# -eq 1 && "$1" != "--jupyter" ) ]]; then
    echo "Usage: ./setup.sh [--jupyter]" >&2
    exit 2
fi

uv sync --extra dev

if [[ "${1:-}" == "--jupyter" ]]; then
    uv run python -m ipykernel install --user \
        --name fantasyfootball \
        --display-name "fantasyfootball"
fi
