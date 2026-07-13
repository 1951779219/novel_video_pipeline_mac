#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

mkdir -p data/runtime
exec > >(tee -a data/runtime/gui_launch.log) 2>&1

export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$PATH"
export NO_PROXY="localhost,127.0.0.1,::1"
export no_proxy="$NO_PROXY"
export PYTHONUNBUFFERED=1

pkill -f "python -m app.webui" 2>/dev/null || true

check_gui_python() {
  [ -x ".venv/bin/python" ] || return 1
  set +e
  .venv/bin/python - <<'PY' >/dev/null 2>&1
import tkinter  # noqa: F401
PY
  local status=$?
  set -e
  return "$status"
}

if [ ! -x ".venv/bin/python" ]; then
  scripts/setup_macos.sh
elif ! check_gui_python; then
  echo "[启动检查] 当前 .venv 的 Tkinter 不兼容，正在重建虚拟环境..."
  rm -rf .venv
  scripts/setup_macos.sh
fi

if ! check_gui_python; then
  cat <<'EOF'
[ERROR] GUI Python/Tkinter runtime is still not usable.

On macOS 26/Tahoe this usually means the system/Xcode Python Tk runtime is broken.
First try:
  Install_Mac_Dependencies.command

Or install a Python build with Tk support manually, then run this file again:
  brew install python@3.12 python-tk@3.12 ffmpeg-full

Or set a Python.org interpreter explicitly:
  export NOVEL_VIDEO_PYTHON=/Library/Frameworks/Python.framework/Versions/3.12/bin/python3
EOF
  exit 86
fi

if [ "$(uname -s)" = "Darwin" ]; then
  echo "[启动检查] 检查硬字幕组件 ffmpeg-full/libass..."
  .venv/bin/python -m app.dependency_manager --ensure-ffmpeg-subtitles || \
    echo "[启动警告] 硬字幕组件自动安装失败；请运行 Install_Mac_Dependencies.command。"
fi

echo "Starting Novel Video Pipeline GUI..."
echo "Project: $ROOT"
exec .venv/bin/python -m app.gui
