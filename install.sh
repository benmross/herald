#!/usr/bin/env bash
# Herald: install.
#
#   curl -fsSL https://raw.githubusercontent.com/benmross/herald/main/install.sh | bash
#
# or, in a checkout you already have:
#
#   ./install.sh
#
# It installs what is missing (git, Python, the Claude Code CLI), creates the
# virtualenv, and hands over to the setup wizard. It asks before it uses sudo,
# and it says what each command is for, because "curl into bash" deserves at
# least that much.
#
# Everything it does is idempotent: running it twice is not a problem, and
# running it after a failure picks up where it stopped.

set -euo pipefail

REPO="${HERALD_REPO:-https://github.com/benmross/herald.git}"
DEST="${HERALD_DIR:-$HOME/herald}"
MIN_PY_MINOR=11

bold() { printf '\033[1m%s\033[0m\n' "$*"; }
dim()  { printf '\033[2m%s\033[0m\n' "$*"; }
warn() { printf '\033[33m%s\033[0m\n' "$*"; }
die()  { printf '\033[31m%s\033[0m\n' "$*" >&2; exit 1; }

ask() {
  # ask "question" -> 0 for yes. Defaults to yes; non-interactive answers yes,
  # because a piped installer that stops to ask has nobody to answer it.
  local prompt="$1"
  if [ ! -t 0 ]; then return 0; fi
  printf '\033[1m%s\033[0m [Y/n] ' "$prompt"
  read -r reply </dev/tty || return 0
  case "$reply" in [nN]*) return 1 ;; *) return 0 ;; esac
}

run_sudo() {
  # Every privileged command is printed before it runs. Nothing is hidden
  # behind a spinner.
  dim "  sudo $*"
  sudo "$@"
}

detect_os() {
  case "$(uname -s)" in
    Darwin) echo macos ;;
    Linux)  echo linux ;;
    *)      echo other ;;
  esac
}

have() { command -v "$1" >/dev/null 2>&1; }

python_ok() {
  local py="$1"
  have "$py" || return 1
  "$py" - <<'EOF' >/dev/null 2>&1
import sys
raise SystemExit(0 if sys.version_info >= (3, 11) else 1)
EOF
}

pick_python() {
  # The distro's own python3 first: it is the one whose -venv package is in
  # the default repositories. A newer interpreter from a third-party
  # repository is used only when the system one is too old.
  for candidate in python3 python3.14 python3.13 python3.12 python3.11; do
    if python_ok "$candidate"; then echo "$candidate"; return 0; fi
  done
  return 1
}

venv_ok() {
  # Can *this* interpreter make a virtualenv with pip in it? On Debian and
  # Ubuntu the venv module imports fine but ensurepip is missing until
  # pythonX.Y-venv is installed, and `python -m venv` then fails halfway,
  # leaving a venv/ with a python and no pip. Checked on the chosen
  # interpreter, because the first install that hit this had checked python3
  # and then used python3.14.
  "$1" -c "import venv, ensurepip" >/dev/null 2>&1
}

venv_package() {
  # python3.14-venv, python3.12-venv: the apt package name for this interpreter.
  "$1" -c 'import sys; print(f"python{sys.version_info[0]}.{sys.version_info[1]}-venv")'
}

install_macos_deps() {
  if ! have brew; then
    warn "Homebrew is not installed. It is how a Mac gets the tools Herald needs."
    dim  "  See https://brew.sh for the one command, then run this script again."
    if ask "Install Homebrew now?"; then
      /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
      eval "$(/opt/homebrew/bin/brew shellenv 2>/dev/null || true)"
    else
      die "Herald needs git and Python; Homebrew is the simplest way to get them."
    fi
  fi
  local wanted=()
  have git || wanted+=(git)
  have tmux || wanted+=(tmux)
  python_ok python3 || wanted+=(python@3.13)
  if [ ${#wanted[@]} -gt 0 ]; then
    bold "Installing: ${wanted[*]}"
    brew install "${wanted[@]}"
  fi
}

install_linux_deps() {
  local wanted=() py
  have git || wanted+=(git)
  have tmux || wanted+=(tmux)
  if py="$(pick_python)"; then
    # The venv package for the interpreter that will actually be used.
    venv_ok "$py" || wanted+=("$(venv_package "$py")")
  else
    wanted+=(python3 python3-venv)
  fi
  [ ${#wanted[@]} -eq 0 ] && return 0

  if have apt-get; then
    bold "Installing: ${wanted[*]}"
    dim "This needs your password once."
    run_sudo apt-get update -qq
    run_sudo apt-get install -y "${wanted[@]}"
  elif have dnf; then
    run_sudo dnf install -y "${wanted[@]}"
  elif have pacman; then
    run_sudo pacman -S --noconfirm "${wanted[@]}"
  else
    die "Install these with your package manager, then run this again: ${wanted[*]}"
  fi
}

install_claude() {
  have claude && return 0
  bold "Installing the Claude Code CLI"
  dim  "Herald runs it for you, on your own Claude subscription. There is"
  dim  "nothing extra to pay."
  curl -fsSL https://claude.ai/install.sh | bash
  export PATH="$HOME/.local/bin:$PATH"
  have claude || warn "Claude Code is installed but this terminal cannot see it yet. Open a new terminal window and run this again."
}

main() {
  echo
  bold "Herald"
  dim  "A personal agent that reads what you connect, keeps track of what it"
  dim  "concludes, and tells you each morning what actually matters."
  echo

  local os; os="$(detect_os)"
  [ "$os" = other ] && die "This installer works on macOS and Linux. On Windows, install WSL first and run it there."

  if [ "$os" = macos ]; then install_macos_deps; else install_linux_deps; fi

  local py latest; py="$(pick_python)" || die "No Python 3.$MIN_PY_MINOR or newer found."
  dim "Using $py ($("$py" --version 2>&1))"
  venv_ok "$py" || die "$py cannot create a virtualenv. Install $(venv_package "$py") and run this again."

  # A checkout: either we are in one, or we make one.
  if [ -f "$(dirname "$0")/bin/herald" ] 2>/dev/null; then
    DEST="$(cd "$(dirname "$0")" && pwd)"
    dim "Using this checkout: $DEST"
  elif [ -d "$DEST/.git" ]; then
    bold "Updating $DEST"
    # Only ever forwards. `herald update` is the real path once installed;
    # this is for a checkout whose setup never finished.
    git -C "$DEST" fetch --tags --quiet origin || warn "could not reach $REPO; leaving it alone"
    latest="$(git -C "$DEST" tag --list 'v[0-9]*' | sort -V | tail -1)"
    if [ -n "$latest" ] && git -C "$DEST" merge --ff-only --quiet "$latest" 2>/dev/null; then
      dim "Fast-forwarded to $latest"
    fi
  else
    have git || die "git is needed and could not be installed. Install it, then run this again."
    bold "Cloning into $DEST"
    git clone --quiet "$REPO" "$DEST"
    # Start at the newest release, not at whatever main is this minute. main
    # is where the maintainer works; a tag is where a change is meant for
    # other people, and `herald update` moves between tags from here.
    latest="$(git -C "$DEST" tag --list 'v[0-9]*' | sort -V | tail -1)"
    if [ -n "$latest" ]; then
      git -C "$DEST" checkout --quiet -B main "$latest"
      dim "At release $latest"
    fi
  fi

  cd "$DEST"

  # A venv without pip is the leftover of a venv module that had no ensurepip
  # -- what the check above now catches first. Rebuilt rather than trusted,
  # because trusting it is how the second run failed on "./venv/bin/pip: No
  # such file".
  if [ ! -x venv/bin/python ] || [ ! -x venv/bin/pip ]; then
    bold "Creating the virtualenv"
    "$py" -m venv --clear venv
  fi
  bold "Installing Python dependencies"
  ./venv/bin/pip install --quiet --upgrade pip
  ./venv/bin/pip install --quiet -r requirements.txt

  install_claude

  # `herald` on PATH, without needing a package manager or a sudo write.
  mkdir -p "$HOME/.local/bin"
  ln -sf "$DEST/bin/herald" "$HOME/.local/bin/herald"
  case ":$PATH:" in
    *":$HOME/.local/bin:"*) ;;
    *) warn "So that typing \`herald\` works in every new terminal window, add this"
       warn "line to the end of your shell profile (~/.zshrc or ~/.bashrc), then"
       warn "open a new window:"
       dim  "  export PATH=\"\$HOME/.local/bin:\$PATH\"" ;;
  esac

  echo
  bold "Installed."
  echo

  # Signed in? Asked of the CLI, which is right on macOS too, where the login
  # lives in the Keychain and no credentials file exists. If not, that is the
  # only next step, and this script ends on it rather than listing setup
  # commands underneath as if the sign-in were optional.
  if ! claude auth status 2>/dev/null | grep -q '"loggedIn": *true'; then
    warn "Herald needs Claude Code to be signed in to your Claude account."
    if [ -c /dev/tty ] && ( : </dev/tty ) 2>/dev/null; then
      if ask "Sign in now? (it opens a link)"; then
        claude auth login --claudeai </dev/tty >/dev/tty 2>&1 || true
      fi
    fi
    if ! claude auth status 2>/dev/null | grep -q '"loggedIn": *true'; then
      echo
      bold "Not signed in yet. When you are ready, run these two commands:"
      echo
      echo "    claude auth login       sign in to your Claude account"
      echo "    herald setup --web      then set Herald up"
      echo
      exit 0
    fi
    echo
    bold "Signed in."
    echo
  fi

  dim "Now set Herald up. Either of these does the same thing:"
  echo
  echo "    herald setup --web      a page in your browser (easier)"
  echo "    herald setup            in this terminal"
  echo
}

main "$@"
