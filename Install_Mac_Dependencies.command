#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

mkdir -p data/runtime
exec > >(tee -a data/runtime/install_dependencies.log) 2>&1

export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$PATH"
export NO_PROXY="localhost,127.0.0.1,::1"
export no_proxy="$NO_PROXY"

pause_if_interactive() {
  if [ -t 0 ]; then
    printf '\nPress Enter to close this window...'
    read -r _ || true
  fi
}

load_homebrew_path() {
  for brew_path in /opt/homebrew/bin/brew /usr/local/bin/brew; do
    if [ -x "$brew_path" ]; then
      eval "$("$brew_path" shellenv)"
      return 0
    fi
  done
  command -v brew >/dev/null 2>&1
}

install_homebrew() {
  cat <<'EOF'
[install] Homebrew was not found.

This installer can run the official Homebrew installer now.
Homebrew's own script will explain what it will do and ask for confirmation.
It may ask for the Mac login password while installing Apple's command line tools.
EOF
  if [ -t 0 ]; then
    printf '\nInstall Homebrew now? [Y/n] '
    read -r answer || answer=""
    case "${answer:-Y}" in
      y|Y|yes|YES|"") ;;
      *)
        return 1
        ;;
    esac
  fi
  /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
  load_homebrew_path
}

load_homebrew_path || true

if ! command -v brew >/dev/null 2>&1; then
  if ! install_homebrew; then
    cat <<'EOF'
[ERROR] Homebrew is required for automatic Mac dependency installation.

Install it manually, then double-click this file again:
  /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"

After Homebrew finishes, run:
  Install_Mac_Dependencies.command
  Open_GUI.command
EOF
    pause_if_interactive
    exit 86
  fi
fi

echo "[install] Homebrew: $(command -v brew)"
echo "[install] Installing Python/Tk/FFmpeg Full (with libass subtitles)..."
brew update || true
if ! brew install python@3.12 python-tk@3.12 ffmpeg-full; then
  if ! brew install python@3.11 python-tk@3.11 ffmpeg-full; then
    brew install python python-tk ffmpeg-full
  fi
fi

echo "[install] Rebuilding local virtual environment..."
rm -rf .venv
scripts/setup_macos.sh

echo
echo "[install] Done. You can now double-click Open_GUI.command."
pause_if_interactive
