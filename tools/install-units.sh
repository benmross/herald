#!/usr/bin/env bash
# Install (or refresh) Herald's background jobs.
#
# Kept as a script because muscle memory and old documentation point at it. The
# work happens in setup/services.py, which knows about both platforms, renders
# the unit templates with this checkout's real paths, and covers extensions'
# units as well as Herald's own.
set -euo pipefail
HERE="$(cd "$(dirname "$0")/.." && pwd)"
exec "$HERE/bin/herald" services install "$@"
