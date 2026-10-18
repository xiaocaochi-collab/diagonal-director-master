#!/bin/sh
cd "$(dirname "$0")" || exit 1
python3 -B scripts/asset_server.py start --open
