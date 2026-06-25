#!/usr/bin/env bash

set -e

cd "$(dirname "$0")/.."

# Ensure a config dir and the integration symlink exist
mkdir -p config
bash tests/setup.sh 2>/dev/null || true

# Start Home Assistant with the workspace as config
hass -c config --debug
