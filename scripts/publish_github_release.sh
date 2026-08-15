#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

RELEASE_REPO="${RELEASE_REPO:-1951779219/novel_video_pipeline_mac_release}"
PYTHON_BIN="${PYTHON_BIN:-python3}"

if ! command -v gh >/dev/null 2>&1; then
  echo "[ERROR] GitHub CLI not found. Install it first: brew install gh"
  exit 1
fi

if ! gh auth status >/dev/null 2>&1; then
  echo "[ERROR] GitHub CLI is not logged in. Run: gh auth login"
  exit 1
fi

VERSION="$("$PYTHON_BIN" - <<'PY'
from app.version import VERSION
print(VERSION)
PY
)"
TAG="v${VERSION}"
NOTES_FILE="${NOTES_FILE:-}"

BUILD_ARGS=(scripts/build_release_package.py --repo "$RELEASE_REPO")
if [ -n "$NOTES_FILE" ]; then
  BUILD_ARGS+=(--notes-file "$NOTES_FILE")
fi

"$PYTHON_BIN" "${BUILD_ARGS[@]}"

ZIP_PATH="$(ls -t dist/novel_video_pipeline_mac_"${VERSION//[^A-Za-z0-9._-]/_}"_*.zip | head -n 1)"
LATEST_JSON="dist/latest.json"

if gh release view "$TAG" --repo "$RELEASE_REPO" >/dev/null 2>&1; then
  echo "[release] ${TAG} exists, uploading assets with --clobber"
  gh release upload "$TAG" "$ZIP_PATH" "$LATEST_JSON" --repo "$RELEASE_REPO" --clobber
else
  echo "[release] creating ${TAG} in ${RELEASE_REPO}"
  if [ -n "$NOTES_FILE" ] && [ -f "$NOTES_FILE" ]; then
    gh release create "$TAG" "$ZIP_PATH" "$LATEST_JSON" --repo "$RELEASE_REPO" --title "Novel Video Pipeline ${VERSION}" --notes-file "$NOTES_FILE"
  else
    gh release create "$TAG" "$ZIP_PATH" "$LATEST_JSON" --repo "$RELEASE_REPO" --title "Novel Video Pipeline ${VERSION}" --notes "Novel Video Pipeline ${VERSION} macOS update."
  fi
fi

echo "[ok] Release assets uploaded:"
echo "  $ZIP_PATH"
echo "  $LATEST_JSON"
echo "Manifest URL:"
echo "  https://github.com/${RELEASE_REPO}/releases/latest/download/latest.json"
