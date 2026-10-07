#!/usr/bin/env bash
# The container's entrypoint: install into the volume once, then supervise.
#
# The first start clones a release into /home/herald/herald and installs the
# Claude Code CLI beside it, using the same install.sh everyone else runs.
# Every later start finds both there and goes straight to the supervisor.
set -euo pipefail

DEST="$HOME/herald"

if [ ! -x "$DEST/venv/bin/python" ] || [ ! -x "$DEST/venv/bin/pip" ] \
     || ! command -v claude >/dev/null 2>&1; then
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

Herald is installed. Two commands, from the computer running Docker:

    docker exec -it herald claude auth login     sign in to your Claude account
    docker exec -it herald herald setup --web    then open the link it prints

NEXT

exec "$DEST/venv/bin/python" "$SUPERVISOR"
