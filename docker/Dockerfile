# Herald in a container.
#
# The image is the runtime only: an OS, Python, git, tmux. The program, its
# virtualenv, the Claude Code CLI, the logins and everything Herald knows about
# its person all live in one volume mounted at /home/herald. That is what lets
# a containerised install update by fast-forward like any other, and what makes
# "back it up" and "delete it" each mean one volume.
#
# docs/docker.md is the guide. Build with the repository root as the context:
#
#   docker compose -f docker/compose.yml up -d --build
FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ca-certificates curl git gh less openssh-client \
      python3 python3-venv sqlite3 tmux tzdata \
 && rm -rf /var/lib/apt/lists/*

# One unprivileged user and no sudo. The base image's own uid-1000 user is
# replaced so that the volume's files have an owner with the right name.
RUN (userdel -r ubuntu 2>/dev/null || true) \
 && useradd --create-home --uid 1000 --shell /bin/bash herald

COPY --chmod=0644 install.sh /opt/herald/install.sh
COPY --chmod=0755 docker/entrypoint.sh /opt/herald/entrypoint.sh

USER herald
WORKDIR /home/herald
ENV HOME=/home/herald \
    HERALD_CONTAINER=1 \
    PATH=/home/herald/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
    LANG=C.UTF-8 \
    TERM=xterm-256color

VOLUME /home/herald
# The setup page, and the port Google redirects to at the end of its sign-in.
EXPOSE 8799 8765

HEALTHCHECK --interval=60s --timeout=10s --start-period=10m --retries=3 \
  CMD /home/herald/herald/venv/bin/python /home/herald/herald/bin/herald-supervisor --healthy

ENTRYPOINT ["/opt/herald/entrypoint.sh"]
