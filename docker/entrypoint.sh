#!/usr/bin/env bash
# The container's entrypoint: install into the volume once, then supervise.
#
# The first start clones a release into /home/herald/herald and installs the
# chosen model CLI beside it, using the same install.sh everyone else runs.
# Every later start goes straight to the supervisor.
set -euo pipefail

DEST="$HOME/herald"

if [ ! -x "$DEST/venv/bin/python" ] || [ ! -x "$DEST/venv/bin/pip" ]; then
  echo "First start: installing Herald into the volume."
  HERALD_DIR="$DEST" bash < /opt/herald/install.sh
fi

SUPERVISOR="$DEST/bin/herald-supervisor"
if [ ! -x "$SUPERVISOR" ]; then
  echo
  echo "This release of Herald predates Docker support (it has no"
  echo "bin/herald-supervisor). Run \`docker exec -it herald herald update\`"
  echo "once a newer release exists, then \`docker restart herald\`."
  exec sleep infinity
fi

cat <<'NEXT'

Herald is installed. Choose your providers in setup:

    docker exec -it herald herald setup --web

Setup gives install and sign-in commands for Claude, Codex, or both.
Open a container terminal with docker exec -it herald bash to run them.

NEXT

exec "$DEST/venv/bin/python" "$SUPERVISOR"
