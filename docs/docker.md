# Herald in Docker

A second way to install Herald. It suits two kinds of people: anyone who wants
a wall between an agent that runs shell commands and the rest of their
computer, and anyone on Windows who wants Herald to keep running after every
window is closed.

[`install.md`](install.md) is the ordinary install, straight onto a Mac or a
Linux machine. Read "What you need first" there before starting here: the
Claude or ChatGPT subscription and the Google account requirements are the same.

## What the container changes, and what it does not

Herald's sessions run commands. On an ordinary install they run as you, with
your whole home folder in reach, and what stops a bad command is Herald's own
guard. In the container they run as an unprivileged user with no sudo and no
way to gain privileges, and the only files they can see are in one Docker
volume. A command that goes wrong cannot touch your documents, your SSH keys
or your browser profile, because none of those exist inside the container.

It does not make your accounts any safer. The container holds the Google
token and the Telegram token and has the network, because Herald cannot read
your mail without them. Anything a session could do to your Google account
on an ordinary install, it can do from the container.

Three things do not work in it, because each one reaches the host: reading
iMessage on a Mac, `herald root`, and SSH to your other machines (unless you
put a key in the volume yourself).

## Install

You need Docker: [Docker Desktop](https://www.docker.com/products/docker-desktop/)
on Windows or a Mac, Docker Engine on Linux. On Windows, run the commands
below in PowerShell.

```bash
git clone https://github.com/benmross/herald herald-docker
cd herald-docker
docker compose -f docker/compose.yml up -d --build
```

That folder is only the recipe for the image. The first start downloads the
newest release of Herald into the volume, which takes a
few minutes. Watch it with:

```bash
docker logs -f herald
```

When it prints "Herald is installed", press `Ctrl-C` and start setup:

```bash
docker exec -it herald herald setup --web
```

Open the link in your normal browser and follow [the wizard](install.md#step-3-the-wizard).
Choose Claude only, Codex only, or both. Setup prints the install and login
commands for your choices. Run each in the container with
`docker exec -it herald bash` to open its terminal first, then return to setup
and check again. No model CLI is installed automatically without your choice.
The Google sign-in opens in the same browser. Neither needs port
forwarding: compose publishes both ports to this computer and to nothing
else.

Every `herald` command in the other guides works the same with
`docker exec -it herald` in front of it:

```bash
docker exec -it herald herald status
docker exec -it herald herald attach
docker exec -it herald herald update
```

## Keeping it running

Compose sets `restart: unless-stopped`, so the container comes back when
Docker does.

On Windows and on a Mac, switch on "Start Docker Desktop when you sign in to
your computer" in Docker Desktop's settings. With that, Herald is running
whenever you are logged in, with no window open. It still stops while the
computer is asleep: a laptop asleep at 06:30 sends that digest late or not at
all. Run `herald doctor` after downtime if the selected provider needs a new login.

On a Linux server, Docker starts at boot and nothing more is needed.

## Where everything is

One volume, `herald-home`, mounted at `/home/herald`. It holds the program
(`~/herald`), your selected model CLIs and their logins, the Google credentials, and
`~/.herald`, which is everything Herald knows about you. The image holds an
operating system and nothing else.

Back it up by backing up that volume. Delete everything with:

```bash
docker compose -f docker/compose.yml down --volumes
```

`herald update` updates the program inside the volume, exactly as on any
other install. Rebuild the image only when a release note says the image
itself changed:

```bash
git pull && docker compose -f docker/compose.yml up -d --build
```

## How it differs underneath

A container has no systemd, so `bin/herald-supervisor` is the container's
main process and does the units' work: it keeps the Telegram bridge and the
optional Remote Control surface running, collects every thirty minutes, and runs the cycles at their
times of day in your configured timezone. It starts nothing until the
wizard's "Start it running" step. `herald services status` shows what it is
running, and `docker logs herald` shows when each job started and how it
exited.

There is no watchdog in the container. A job that exits is started again,
but one that is alive and stuck goes unnoticed where the systemd watchdog
would have caught it, and `docker restart herald` is what clears it.

Background jobs that an extension ships as systemd units are not run in the
container.

The setup page and the Google sign-in listen on every interface inside the
container, because a published port cannot reach a listener on the
container's own loopback. What keeps them off your network is the
`127.0.0.1:` in front of both port lines in `docker/compose.yml`. Leave it
there.
