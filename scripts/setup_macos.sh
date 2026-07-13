#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$PATH"
export NO_PROXY="localhost,127.0.0.1,::1"
export no_proxy="$NO_PROXY"

python_has_working_tk() {
  local candidate="$1"
  [ -x "$candidate" ] || return 1
  set +e
  "$candidate" - <<'PY' >/dev/null 2>&1
import sys
if sys.version_info < (3, 10):
    raise SystemExit(2)
import tkinter  # noqa: F401
PY
  local status=$?
  set -e
  return "$status"
}

select_python() {
  local seen=":"
  local candidates=()
  if [ -n "${NOVEL_VIDEO_PYTHON:-}" ]; then
    candidates+=("$NOVEL_VIDEO_PYTHON")
  fi
  candidates+=(
    "/opt/homebrew/bin/python3.12"
    "/opt/homebrew/bin/python3.11"
    "/opt/homebrew/opt/python@3.12/bin/python3.12"
    "/opt/homebrew/opt/python@3.11/bin/python3.11"
    "/usr/local/bin/python3.12"
    "/usr/local/bin/python3.11"
    "/usr/local/opt/python@3.12/bin/python3.12"
    "/usr/local/opt/python@3.11/bin/python3.11"
    "/Library/Frameworks/Python.framework/Versions/3.12/bin/python3"
    "/Library/Frameworks/Python.framework/Versions/3.11/bin/python3"
  )
  for name in python3.12 python3.11 python3; do
    if command -v "$name" >/dev/null 2>&1; then
      candidates+=("$(command -v "$name")")
    fi
  done
  for candidate in "${candidates[@]}"; do
    [ -n "$candidate" ] || continue
    case "$seen" in
      *":$candidate:"*) continue ;;
    esac
    seen="${seen}${candidate}:"
    [ -e "$candidate" ] || continue
    if python_has_working_tk "$candidate"; then
      printf '%s\n' "$candidate"
      return 0
    fi
    echo "[setup] skip Python without working Tkinter: $candidate" >&2
  done
  return 1
}

install_homebrew_runtime() {
  local brew_bin
  brew_bin="$(command -v brew || true)"
  if [ -z "$brew_bin" ]; then
    return 1
  fi
  echo "[setup] Homebrew found: $brew_bin"
  echo "[setup] installing Python/Tk/FFmpeg Full with libass..."
  "$brew_bin" update || true
  if "$brew_bin" install python@3.12 python-tk@3.12 ffmpeg-full; then
    return 0
  fi
  if "$brew_bin" install python@3.11 python-tk@3.11 ffmpeg-full; then
    return 0
  fi
  "$brew_bin" install python python-tk ffmpeg-full
}

if ! PYTHON_BIN="$(select_python)"; then
  if install_homebrew_runtime; then
    hash -r
    if PYTHON_BIN="$(select_python)"; then
      echo "[setup] Python/Tk runtime repaired by Homebrew."
    else
      echo "[setup] Homebrew install finished, but no working Tkinter Python was found." >&2
    fi
  fi
fi

if [ -z "${PYTHON_BIN:-}" ]; then
  cat <<'EOF'
[ERROR] No Python with a working Tkinter GUI runtime was found.

On macOS 26/Tahoe, Apple/Xcode system Python may crash with:
  macOS 26 (...) or later required, have instead 16 (...) !

If Homebrew is installed, run:
  brew install python@3.12 python-tk@3.12 ffmpeg-full

If Homebrew is not installed, install it first:
  /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"

If you use Python.org instead, set it explicitly before launching:
  export NOVEL_VIDEO_PYTHON=/Library/Frameworks/Python.framework/Versions/3.12/bin/python3
EOF
  exit 86
fi

echo "[setup] using Python: $PYTHON_BIN"

"$PYTHON_BIN" -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip setuptools wheel
python -m pip install -r requirements.txt
python - <<'PY'
import tkinter  # noqa: F401
print("Tkinter runtime OK")
PY

if [ ! -x "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" ] && \
   [ ! -x "$HOME/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" ]; then
  python -m playwright install chromium
fi

if ! command -v ffmpeg >/dev/null 2>&1; then
  echo "FFmpeg was not found. Install it with: brew install ffmpeg-full"
  exit 2
fi

if ! command -v ffprobe >/dev/null 2>&1; then
  echo "ffprobe was not found. The app can use ffmpeg fallback for duration checks, but brew install ffmpeg-full is still recommended."
fi

python -m app.dependency_manager --ensure
