#!/usr/bin/env python3
"""Apply a downloaded update zip while preserving local user data."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import stat
import sys
import time
import zipfile
from pathlib import Path


COPY_DIRS = ("app", "docs", "scripts", "prompts")
COPY_FILES = (
    "AGENTS.md",
    "README.md",
    "Open_GUI.command",
    "Install_Mac_Dependencies.command",
    "requirements.txt",
    "update.bat",
)
PROTECTED_DATA_ITEMS = (
    "settings.json",
    "profiles",
    "jobs",
    "projects",
    "runtime",
    "chrome_debug_profile",
    "chrome_debug_profiles",
    "updates",
)
API_KEY_FIELDS = (
    "ai_api_key",
    "llm_api_key",
    "tts_api_key",
    "image_api_key",
    "cover_api_key",
    "character_reference_api_key",
    "scene_reference_api_key",
    "pronunciation_dictionary_api_key",
)
DEFAULT_PROFILE_NAME = "配置1"


def log(handle, message: str) -> None:
    text = str(message)
    print(text, flush=True)
    handle.write(text + "\n")
    handle.flush()


def safe_extract(zip_path: Path, target: Path) -> None:
    root = target.resolve()
    with zipfile.ZipFile(zip_path, "r") as zf:
        for member in zf.infolist():
            dest = (target / member.filename).resolve()
            if root != dest and root not in dest.parents:
                raise RuntimeError(f"Unsafe path in update zip: {member.filename}")
        zf.extractall(target)


def find_payload_root(extract_dir: Path) -> Path:
    if (extract_dir / "app" / "gui.py").exists():
        return extract_dir
    children = [p for p in extract_dir.iterdir() if p.is_dir()]
    for child in children:
        if (child / "app" / "gui.py").exists():
            return child
    raise RuntimeError("Update package does not contain app/gui.py")


def copy_tree(src: Path, dst: Path) -> None:
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo", ".DS_Store"))


def copy_file(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)


def backup_user_config(target: Path, backup_dir: Path, handle) -> None:
    data_dir = target / "data"
    backup_dir.mkdir(parents=True, exist_ok=True)
    for name in ("settings.json", "profiles"):
        src = data_dir / name
        if not src.exists():
            continue
        dst = backup_dir / name
        if src.is_dir():
            shutil.copytree(src, dst, dirs_exist_ok=True)
        else:
            copy_file(src, dst)
        log(handle, f"Backed up data/{name} -> {dst}")


def _blank_api_keys(path: Path) -> bool:
    """Blank sensitive values in one JSON config file without exposing them."""
    if not path.exists():
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return False
    if not isinstance(data, dict):
        return False
    changed = False
    for key in API_KEY_FIELDS:
        if data.get(key):
            data[key] = ""
            changed = True
    if changed:
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return changed


def clear_default_profile_api_keys(target: Path, handle) -> None:
    """Ensure the default profile is always distributed as a blank-key template."""
    data_dir = target / "data"
    profile_path = data_dir / "profiles" / f"{DEFAULT_PROFILE_NAME}.json"
    if _blank_api_keys(profile_path):
        log(handle, "Cleared API keys from the default profile 配置1")

    settings_path = data_dir / "settings.json"
    try:
        settings = json.loads(settings_path.read_text(encoding="utf-8")) if settings_path.exists() else {}
    except Exception:
        settings = {}
    if isinstance(settings, dict) and str(settings.get("active_profile") or "").strip() == DEFAULT_PROFILE_NAME:
        if _blank_api_keys(settings_path):
            log(handle, "Cleared API keys from active default settings 配置1")


def copy_data_defaults(payload: Path, target: Path, handle) -> None:
    src = payload / "data" / "defaults"
    if not src.exists():
        return
    dst = target / "data" / "defaults"
    copy_tree(src, dst)
    log(handle, "Updated data/defaults templates")


def chmod_commands(target: Path) -> None:
    candidates = [
        target / "Open_GUI.command",
        target / "Install_Mac_Dependencies.command",
        target / "scripts" / "start_gui_macos.command",
        target / "scripts" / "start_chrome_debug_macos.command",
        target / "scripts" / "start_seedance_canvas_macos.command",
    ]
    for path in candidates:
        if path.exists():
            path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def write_update_marker(target: Path, version: str, backup_dir: Path) -> None:
    marker = target / "data" / "updates" / "last_update.json"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(
        json.dumps(
            {
                "version": version,
                "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "backup_dir": str(backup_dir),
                "protected": [f"data/{name}" for name in PROTECTED_DATA_ITEMS],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def apply_update(zip_path: Path, target: Path, version: str, wait: float) -> Path:
    if wait > 0:
        time.sleep(wait)
    if not (target / "app" / "gui.py").exists():
        raise RuntimeError(f"Target does not look like the app root: {target}")
    zip_path = zip_path.resolve()
    target = target.resolve()
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    updates_dir = target / "data" / "updates"
    extract_dir = updates_dir / f"apply_extract_{version}_{timestamp}"
    backup_dir = target / "data" / "backups" / f"pre_update_{version}_{timestamp}"
    log_path = updates_dir / f"apply_update_{version}_{timestamp}.log"
    updates_dir.mkdir(parents=True, exist_ok=True)

    with log_path.open("a", encoding="utf-8") as handle:
        log(handle, f"Applying update {version}")
        log(handle, f"Zip: {zip_path}")
        log(handle, f"Target: {target}")
        if extract_dir.exists():
            shutil.rmtree(extract_dir)
        extract_dir.mkdir(parents=True, exist_ok=True)
        safe_extract(zip_path, extract_dir)
        payload = find_payload_root(extract_dir)
        log(handle, f"Payload: {payload}")

        clear_default_profile_api_keys(target, handle)
        backup_user_config(target, backup_dir, handle)

        for dirname in COPY_DIRS:
            src = payload / dirname
            if not src.exists():
                continue
            copy_tree(src, target / dirname)
            log(handle, f"Updated {dirname}/")

        for filename in COPY_FILES:
            src = payload / filename
            if not src.exists():
                continue
            copy_file(src, target / filename)
            log(handle, f"Updated {filename}")

        copy_data_defaults(payload, target, handle)
        chmod_commands(target)
        write_update_marker(target, version, backup_dir)
        log(handle, "Update complete. User data was preserved.")
    return log_path


def main() -> int:
    parser = argparse.ArgumentParser(description="Apply Novel Video Pipeline update package.")
    parser.add_argument("--zip", required=True, help="Downloaded update zip path")
    parser.add_argument("--target", required=True, help="Existing app root to update")
    parser.add_argument("--version", default="", help="Update version")
    parser.add_argument("--wait", type=float, default=2.0, help="Seconds to wait before applying")
    args = parser.parse_args()
    try:
        log_path = apply_update(Path(args.zip), Path(args.target), args.version or "unknown", args.wait)
    except Exception as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1
    print(f"Update log: {log_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
