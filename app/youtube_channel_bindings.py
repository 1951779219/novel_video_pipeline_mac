from __future__ import annotations

import json
from pathlib import Path


_PATH = Path(__file__).resolve().parent.parent / "data" / "youtube_channel_bindings.json"


def _key(chrome_profile: str, scheme_name: str = "") -> str:
    chrome = str(chrome_profile or "Default").strip() or "Default"
    scheme = str(scheme_name or "").strip()
    return f"{chrome}\n{scheme}"


def _load() -> dict:
    try:
        data = json.loads(_PATH.read_text(encoding="utf-8"))
    except Exception:
        data = {}
    return data if isinstance(data, dict) else {}


def get_binding(chrome_profile: str, scheme_name: str = "") -> dict:
    data = _load().get("bindings", {})
    if not isinstance(data, dict):
        return {}
    exact = data.get(_key(chrome_profile, scheme_name))
    if isinstance(exact, dict):
        return dict(exact)
    # A renamed scheme can still reuse the one unambiguous binding belonging
    # to its dedicated Chrome profile.
    prefix = f"{str(chrome_profile or 'Default').strip() or 'Default'}\n"
    matches = [value for key, value in data.items() if str(key).startswith(prefix) and isinstance(value, dict)]
    return dict(matches[0]) if len(matches) == 1 else {}


def save_binding(chrome_profile: str, scheme_name: str, channel_id: str, channel_name: str) -> None:
    channel_id = str(channel_id or "").strip()
    if not channel_id:
        raise ValueError("YouTube channel ID is empty")
    root = _load()
    bindings = root.get("bindings")
    if not isinstance(bindings, dict):
        bindings = {}
    bindings[_key(chrome_profile, scheme_name)] = {
        "chrome_profile": str(chrome_profile or "Default").strip() or "Default",
        "scheme_name": str(scheme_name or "").strip(),
        "channel_id": channel_id,
        "channel_name": str(channel_name or "").strip(),
    }
    root = {"version": 1, "bindings": bindings}
    _PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = _PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(root, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(_PATH)
