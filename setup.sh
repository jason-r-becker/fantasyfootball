#!/usr/bin/env bash

set -euo pipefail

if [[ $# -ne 0 ]]; then
    echo "Usage: ./setup.sh" >&2
    exit 2
fi

uv sync --extra dev
