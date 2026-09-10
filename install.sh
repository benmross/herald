#!/usr/bin/env bash
# Herald — install.
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
  for candidate in python3.14 python3.13 python3.12 python3.11 python3; do
    if python_ok "$candidate"; then echo "$candidate"; return 0; fi
  done
  return 1
}

install_macos_deps() {
  if ! have brew; then
    warn "Homebrew is not installed. It is how macOS gets developer tools."
    dim  "  https://brew.sh — one command, and then run this script again."
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
  local wanted=()
  have git || wanted+=(git)
  have tmux || wanted+=(tmux)
  pick_python >/dev/null || wanted+=(python3 python3-venv)
  # A venv needs python3-venv even when python3 is already present.
  if have python3 && ! python3 -c "import venv" >/dev/null 2>&1; then
    wanted+=(python3-venv)
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
  dim  "Herald runs it as a subprocess on your own subscription. It never uses"
  dim  "an API key, and there is nothing to pay per token."
  curl -fsSL https://claude.ai/install.sh | bash
  export PATH="$HOME/.local/bin:$PATH"
  have claude || warn "claude is installed but not on PATH yet — open a new terminal."
}

main() {
  echo
  bold "Herald"
  dim  "A personal agent that reads what you connect, keeps track of what it"
  dim  "concludes, and tells you each morning what actually matters."
  echo

  local os; os="$(detect_os)"
  [ "$os" = other ] && die "This installer handles macOS and Linux. On Windows, use WSL2."

  if [ "$os" = macos ]; then install_macos_deps; else install_linux_deps; fi

  local py; py="$(pick_python)" || die "No Python 3.$MIN_PY_MINOR or newer found."
  dim "Using $py ($("$py" --version 2>&1))"

  # A checkout: either we are in one, or we make one.
  if [ -f "$(dirname "$0")/bin/herald" ] 2>/dev/null; then
    DEST="$(cd "$(dirname "$0")" && pwd)"
    dim "Using this checkout: $DEST"
  elif [ -d "$DEST/.git" ]; then
    bold "Updating $DEST"
    git -C "$DEST" pull --ff-only || warn "could not fast-forward; leaving it alone"
  else
    have git || die "git is required."
    bold "Cloning into $DEST"
    git clone --depth 1 "$REPO" "$DEST"
  fi

  cd "$DEST"

  if [ ! -x venv/bin/python ]; then
    bold "Creating the virtualenv"
    "$py" -m venv venv
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
    *) warn "Add this to your shell profile so \`herald\` is on your PATH:"
       dim  "  export PATH=\"\$HOME/.local/bin:\$PATH\"" ;;
  esac

  echo
  bold "Installed."
  echo
  if ! [ -f "$HOME/.claude/.credentials.json" ]; then
    warn "One thing first: sign in to Claude Code."
    dim  "  run:  claude     then use  /login"
    echo
  fi
  dim "Then set Herald up. Either of these — they do the same thing:"
  echo
  echo "    herald setup --web      a page in your browser (easier)"
  echo "    herald setup            in this terminal"
  echo
}

main "$@"
