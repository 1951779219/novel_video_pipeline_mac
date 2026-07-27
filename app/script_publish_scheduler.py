"""Durable, GUI-driven queue for unattended YouTube uploads."""
from __future__ import annotations

import json
import os
import re
import threading
from datetime import datetime, timedelta
from pathlib import Path

from app.config import DATA_DIR


QUEUE_FILE = DATA_DIR / "script_publish_queue.json"
_LOCK = threading.RLock()


def _read() -> dict:
    with _LOCK:
        try:
            payload = json.loads(QUEUE_FILE.read_text(encoding="utf-8"))
        except Exception:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        items = payload.get("items")
        payload["items"] = items if isinstance(items, list) else []
        payload.setdefault("version", 1)
        return payload


def _write(payload: dict) -> None:
    with _LOCK:
        QUEUE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = QUEUE_FILE.with_name(f".{QUEUE_FILE.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, QUEUE_FILE)


def load_items(profile_name: str = "") -> list[dict]:
    items = [dict(item) for item in _read()["items"] if isinstance(item, dict)]
    if profile_name:
        items = [item for item in items if str(item.get("profile") or "") == profile_name]
    return sorted(items, key=lambda item: (str(item.get("scheduled_at") or ""), str(item.get("job_id") or "")))


_CN_DIGITS = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "兩": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}


def _episode_number(text: str) -> int:
    text = str(text or "").strip()
    if text.isdigit():
        return int(text)
    if text in _CN_DIGITS:
        return _CN_DIGITS[text]
    if text == "十":
        return 10
    if "十" in text:
        left, right = text.split("十", 1)
        tens = _CN_DIGITS.get(left, 1) if left else 1
        ones = _CN_DIGITS.get(right, 0) if right else 0
        return tens * 10 + ones
    return 0


def infer_series(status: dict, job_id: str) -> tuple[str, int, str]:
    source = str(status.get("source_path") or status.get("input") or "").strip()
    raw = Path(source).stem if source and Path(source).suffix else str(status.get("source_title") or status.get("title") or job_id)
    known_series = str(status.get("series_title") or "").strip()
    known_episode = int(status.get("series_episode") or 0)
    if known_series and known_episode:
        return known_series, known_episode, raw
    normalized = raw.strip()
    patterns = (
        r"第\s*([0-9]{1,4}|[零〇一二两兩三四五六七八九十]{1,5})\s*(?:话|話|期|集|章|部)",
        r"(?i)(?:^|[\s_.\-—－])(?:EP?|PART)\s*0*([0-9]{1,4})(?=$|[\s_.\-—－])",
        r"(?:^|[\s_.\-—－])([上中下])(?:篇|集|部)?(?=$|[\s_.\-—－])",
    )
    for pattern in patterns:
        match = re.search(pattern, normalized)
        if not match:
            continue
        marker = match.group(1)
        episode = {"上": 1, "中": 2, "下": 3}.get(marker, _episode_number(marker))
        if episode < 1:
            continue
        series = (normalized[:match.start()] + " " + normalized[match.end():]).strip()
        series = re.sub(r"[\s_.\-—－]+", " ", series).strip(" _.-—－") or normalized
        return series, episode, raw
    # No explicit episode marker means an independent one-shot story.
    return normalized or job_id, 0, raw


def _ordered_candidates(job_statuses: list[tuple[str, dict]]) -> list[tuple[str, dict, str, int, str]]:
    groups: dict[str, list[tuple[str, dict, str, int, str]]] = {}
    group_order: dict[str, tuple[str, str]] = {}

    def imported_order(job_id: str) -> tuple[str, str]:
        match = re.search(r"_(\d{8}_\d{6})(?:_\d+)?$", job_id)
        return ((match.group(1) if match else "99999999_999999"), job_id)
    for job_id, status in job_statuses:
        if str(status.get("youtube_url") or "").strip():
            continue
        series, episode, display = infer_series(status, job_id)
        key = series.casefold() if episode else f"__single__{job_id}"
        row = (job_id, status, series, episode, display)
        groups.setdefault(key, []).append(row)
        order = imported_order(job_id)
        group_order[key] = min(group_order.get(key, order), order)
    ordered: list[tuple[str, dict, str, int, str]] = []
    for key in sorted(groups, key=lambda value: group_order[value]):
        rows = groups[key]
        rows.sort(key=lambda row: (row[3] if row[3] else 10**9, imported_order(row[0])))
        ordered.extend(rows)
    return ordered


def rebuild_queue(
    profile_name: str,
    job_statuses: list[tuple[str, dict]],
    first_at: datetime,
    interval_hours: int = 24,
) -> list[dict]:
    payload = _read()
    others = [
        dict(item) for item in payload["items"]
        if isinstance(item, dict) and str(item.get("profile") or "") != profile_name
    ]
    old = {
        str(item.get("job_id") or ""): dict(item)
        for item in payload["items"]
        if isinstance(item, dict) and str(item.get("profile") or "") == profile_name
    }
    step = timedelta(hours=max(1, int(interval_hours or 24)))
    rows = []
    slot = first_at.replace(second=0, microsecond=0)
    for position, (job_id, status, series, episode, display) in enumerate(_ordered_candidates(job_statuses), start=1):
        previous = old.get(job_id, {})
        state = str(previous.get("state") or "pending")
        if state not in {"published", "uploading"}:
            state = "pending"
        rows.append(
            {
                "job_id": job_id,
                "profile": profile_name,
                "series": series,
                "episode": episode,
                "display_title": str(status.get("title") or display or job_id),
                "scheduled_at": slot.strftime("%Y-%m-%dT%H:%M"),
                "position": position,
                "state": state,
                "last_error": str(previous.get("last_error") or "") if state != "pending" else "",
                "youtube_url": str(previous.get("youtube_url") or ""),
            }
        )
        slot += step
    payload["items"] = others + rows
    payload["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    _write(payload)
    return rows


def ensure_jobs(
    profile_name: str,
    job_statuses: list[tuple[str, dict]],
    first_at: datetime,
    interval_hours: int = 24,
) -> list[dict]:
    """Append newly imported jobs without moving already assigned slots."""
    payload = _read()
    profile_items = [
        item for item in payload["items"]
        if isinstance(item, dict) and str(item.get("profile") or "") == profile_name
    ]
    known = {str(item.get("job_id") or "") for item in profile_items}
    missing = [row for row in _ordered_candidates(job_statuses) if row[0] not in known]
    if not missing:
        return load_items(profile_name)
    step = timedelta(hours=max(1, int(interval_hours or 24)))
    assigned = []
    for item in profile_items:
        try:
            assigned.append(datetime.strptime(str(item.get("scheduled_at") or ""), "%Y-%m-%dT%H:%M"))
        except ValueError:
            pass
    slot = (max(assigned) + step) if assigned else first_at.replace(second=0, microsecond=0)
    position = len(profile_items)
    for job_id, status, series, episode, display in missing:
        position += 1
        profile_items.append(
            {
                "job_id": job_id,
                "profile": profile_name,
                "series": series,
                "episode": episode,
                "display_title": str(status.get("title") or display or job_id),
                "scheduled_at": slot.strftime("%Y-%m-%dT%H:%M"),
                "position": position,
                "state": "pending",
                "last_error": "",
                "youtube_url": "",
            }
        )
        slot += step
    payload["items"] = [
        item for item in payload["items"]
        if not isinstance(item, dict) or str(item.get("profile") or "") != profile_name
    ] + profile_items
    payload["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    _write(payload)
    return load_items(profile_name)


def set_manual_schedule(
    profile_name: str,
    assignments: list[tuple[str, datetime]],
    statuses: dict[str, dict],
) -> list[dict]:
    """Insert or replace exact per-job times chosen in the task context menu."""
    payload = _read()
    assigned_ids = {str(job_id) for job_id, _scheduled in assignments}
    retained = [
        dict(item) for item in payload["items"]
        if isinstance(item, dict) and str(item.get("job_id") or "") not in assigned_ids
    ]
    rows = []
    for position, (job_id, scheduled) in enumerate(assignments, start=1):
        status = statuses.get(job_id, {})
        series, episode, display = infer_series(status, job_id)
        rows.append(
            {
                "job_id": job_id,
                "profile": profile_name,
                "series": series,
                "episode": episode,
                "display_title": str(status.get("title") or display or job_id),
                "scheduled_at": scheduled.replace(second=0, microsecond=0).strftime("%Y-%m-%dT%H:%M"),
                "position": position,
                "state": "pending",
                "last_error": "",
                "youtube_url": "",
                "manual": True,
            }
        )
    payload["items"] = retained + rows
    payload["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    _write(payload)
    return load_items(profile_name)


def remove_jobs(job_ids: list[str] | set[str]) -> None:
    targets = {str(job_id) for job_id in job_ids}
    payload = _read()
    payload["items"] = [
        item for item in payload["items"]
        if not isinstance(item, dict) or str(item.get("job_id") or "") not in targets
    ]
    _write(payload)


def due_item(profile_name: str, now: datetime | None = None) -> dict | None:
    now = now or datetime.now()
    for item in load_items(profile_name):
        if str(item.get("state") or "pending") not in {"pending", "retry"}:
            continue
        try:
            scheduled = datetime.strptime(str(item.get("scheduled_at") or ""), "%Y-%m-%dT%H:%M")
        except ValueError:
            continue
        if scheduled <= now:
            return item
    return None


def update_item(profile_name: str, job_id: str, **updates) -> None:
    payload = _read()
    for item in payload["items"]:
        if not isinstance(item, dict):
            continue
        if str(item.get("profile") or "") == profile_name and str(item.get("job_id") or "") == job_id:
            item.update(updates)
            item["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            break
    _write(payload)


def postpone_pending(profile_name: str, from_at: str, hours: int) -> None:
    payload = _read()
    step = timedelta(hours=max(1, int(hours or 24)))
    try:
        threshold = datetime.strptime(from_at, "%Y-%m-%dT%H:%M")
    except ValueError:
        threshold = datetime.min
    for item in payload["items"]:
        if not isinstance(item, dict) or str(item.get("profile") or "") != profile_name:
            continue
        if str(item.get("state") or "pending") not in {"pending", "retry"}:
            continue
        try:
            scheduled = datetime.strptime(str(item.get("scheduled_at") or ""), "%Y-%m-%dT%H:%M")
        except ValueError:
            continue
        if scheduled >= threshold:
            item["scheduled_at"] = (scheduled + step).strftime("%Y-%m-%dT%H:%M")
    _write(payload)


def move_item(profile_name: str, job_id: str, delta: int) -> bool:
    payload = _read()
    items = [
        item for item in payload["items"]
        if isinstance(item, dict) and str(item.get("profile") or "") == profile_name
    ]
    items.sort(key=lambda item: (str(item.get("scheduled_at") or ""), str(item.get("job_id") or "")))
    index = next((i for i, item in enumerate(items) if str(item.get("job_id") or "") == job_id), -1)
    target = index + int(delta)
    if index < 0 or target < 0 or target >= len(items):
        return False
    items[index]["scheduled_at"], items[target]["scheduled_at"] = items[target].get("scheduled_at", ""), items[index].get("scheduled_at", "")
    _write(payload)
    return True
