"""Durable novel-project storage and deterministic import detection."""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import threading
import time
import unicodedata
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable

from app.config import DATA_DIR, JOBS_DIR


PROJECTS_DIR = DATA_DIR / "projects"
PROJECT_SCHEMA_VERSION = 2
PROJECT_FEATURE_REVISION = "项目添加2"
PROJECT_FILE = "project.json"
CHARACTER_PROFILES_FILE = "character_profiles.json"
CHARACTER_RELATIONSHIPS_FILE = "character_relationships.json"
NAME_REGISTRY_FILE = "name_registry.json"
VISUAL_BIBLE_FILE = "visual_bible.json"
CHARACTER_REFERENCE_MANIFEST_FILE = "character_reference_manifest.json"
PROJECTS_DIR.mkdir(parents=True, exist_ok=True)

_WRITE_LOCK = threading.RLock()
_PART_SUFFIX_RE = re.compile(
    r"(?:[\s_.\-—－]*"
    r"(?:第\s*)?0*(\d{1,4})\s*(?:集|話|话|章|回|部|篇|期)?"
    r"|[\s_.\-—－]*(上|中|下|前|后|後|终|終)(?:篇|部|集)?"
    r")$",
    re.I,
)
_PART_LABELS = {
    "上": 1,
    "前": 1,
    "中": 2,
    "下": 2,
    "后": 2,
    "後": 2,
    "终": 3,
    "終": 3,
}
DEFAULT_SERIES_VIDEO_SETTINGS = {
    "shared_novel_title": "",
    "shared_novel_title_locked": True,
    "ai_episode_title_enabled": True,
    "ai_cover_copy_enabled": True,
    "episode_start": 1,
    "episode_label_style": "第{episode}集",
    "upload_title_template": "{series_title}｜{episode_label}｜{ai_title}",
    "cover_label_template": "{series_title}【{episode_label}】",
}


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _read_json(path: Path, default):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value
    except (OSError, ValueError, TypeError):
        return default


def _write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with _WRITE_LOCK:
        temporary = path.with_name(
            f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
        )
        temporary.write_text(
            json.dumps(value, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(temporary, path)


@contextmanager
def _project_file_lock(project_id: str):
    """Serialize project merges across GUI and worker processes on macOS."""
    directory = project_dir(project_id)
    directory.mkdir(parents=True, exist_ok=True)
    lock_path = directory / ".project.lock"
    handle = lock_path.open("a+", encoding="utf-8")
    try:
        try:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        except (ImportError, OSError):
            pass
        yield
    finally:
        try:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except (ImportError, OSError):
            pass
        handle.close()


def normalize_project_name(value: str) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).strip()
    text = re.sub(r"\.[^.]+$", "", text)
    match = _PART_SUFFIX_RE.search(text)
    if match:
        text = text[: match.start()]
    text = re.sub(r"[\s_.\-—－]+$", "", text).strip()
    return text


def normalize_series_video_settings(value: dict | None, *, project_name: str = "") -> dict:
    raw = value if isinstance(value, dict) else {}
    settings = dict(DEFAULT_SERIES_VIDEO_SETTINGS)
    settings.update({
        key: raw[key]
        for key in DEFAULT_SERIES_VIDEO_SETTINGS
        if key in raw
    })
    settings["shared_novel_title"] = re.sub(
        r"\s+",
        " ",
        str(settings.get("shared_novel_title") or project_name or ""),
    ).strip()
    settings["shared_novel_title_locked"] = bool(settings.get("shared_novel_title_locked", True))
    settings["ai_episode_title_enabled"] = bool(settings.get("ai_episode_title_enabled", True))
    settings["ai_cover_copy_enabled"] = bool(settings.get("ai_cover_copy_enabled", True))
    try:
        settings["episode_start"] = max(1, int(settings.get("episode_start") or 1))
    except (TypeError, ValueError):
        settings["episode_start"] = 1
    for key, fallback in (
        ("episode_label_style", DEFAULT_SERIES_VIDEO_SETTINGS["episode_label_style"]),
        ("upload_title_template", DEFAULT_SERIES_VIDEO_SETTINGS["upload_title_template"]),
        ("cover_label_template", DEFAULT_SERIES_VIDEO_SETTINGS["cover_label_template"]),
    ):
        settings[key] = str(settings.get(key) or fallback).strip()
    return settings


def project_match_key(value: str) -> str:
    normalized = normalize_project_name(value)
    return re.sub(r"[\W_]+", "", normalized, flags=re.UNICODE).casefold()


def _safe_artifact_name(value: str, fallback: str = "character") -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", str(value or "")).strip("._-")
    return (cleaned or fallback)[:80]


def infer_episode(value: str | Path) -> tuple[str, int]:
    stem = Path(value).stem if isinstance(value, Path) or Path(str(value)).suffix else str(value)
    normalized = unicodedata.normalize("NFKC", stem).strip()
    match = _PART_SUFFIX_RE.search(normalized)
    if not match:
        return normalized, 0
    episode = int(match.group(1) or 0)
    if not episode and match.group(2):
        episode = _PART_LABELS.get(match.group(2), 0)
    base = normalized[: match.start()].rstrip(" _-.—－")
    return base or normalized, episode


def project_dir(project_id: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_-]+", "", str(project_id or ""))
    if not safe or safe != str(project_id):
        raise ValueError("无效的项目 ID")
    root = PROJECTS_DIR.resolve()
    result = (PROJECTS_DIR / safe).resolve()
    if root not in result.parents:
        raise ValueError("项目路径超出允许目录")
    return result


def load_project(project_id: str) -> dict:
    value = _read_json(project_dir(project_id) / PROJECT_FILE, {})
    if not isinstance(value, dict):
        return {}
    legacy_without_series_settings = "series_video_settings" not in value
    value["series_video_settings"] = normalize_series_video_settings(
        value.get("series_video_settings"),
        project_name=str(value.get("name") or ""),
    )
    if legacy_without_series_settings:
        # Projects created by “项目添加1” used the old AI short-name flow.
        # Preserve that behavior until the operator explicitly saves settings.
        value["series_video_settings"]["shared_novel_title_locked"] = False
    return value


def list_projects() -> list[dict]:
    rows = []
    if not PROJECTS_DIR.exists():
        return rows
    for child in PROJECTS_DIR.iterdir():
        if not child.is_dir():
            continue
        project = _read_json(child / PROJECT_FILE, {})
        if isinstance(project, dict) and project.get("project_id"):
            legacy_without_series_settings = "series_video_settings" not in project
            project["series_video_settings"] = normalize_series_video_settings(
                project.get("series_video_settings"),
                project_name=str(project.get("name") or ""),
            )
            if legacy_without_series_settings:
                project["series_video_settings"]["shared_novel_title_locked"] = False
            rows.append(project)
    rows.sort(
        key=lambda item: (
            str(item.get("updated_at") or ""),
            str(item.get("name") or ""),
        ),
        reverse=True,
    )
    return rows


def _unique_project_id(name: str) -> str:
    digest = hashlib.sha256(
        f"{normalize_project_name(name)}\0{time.time_ns()}\0{secrets.token_hex(8)}".encode("utf-8")
    ).hexdigest()[:12]
    return f"project_{digest}"


def create_project(
    name: str,
    *,
    aliases: Iterable[str] = (),
    source_directories: Iterable[str | Path] = (),
    series_video_settings: dict | None = None,
) -> dict:
    clean_name = normalize_project_name(name) or "未命名项目"
    project_id = _unique_project_id(clean_name)
    now = _now()
    payload = {
        "schema_version": PROJECT_SCHEMA_VERSION,
        "feature_revision": PROJECT_FEATURE_REVISION,
        "project_id": project_id,
        "name": clean_name,
        "aliases": sorted({
            str(value).strip()
            for value in aliases
            if str(value).strip() and project_match_key(value) != project_match_key(clean_name)
        }),
        "source_directories": sorted({
            str(Path(value).expanduser().resolve(strict=False))
            for value in source_directories
            if str(value).strip()
        }),
        "jobs": [],
        "series_video_settings": normalize_series_video_settings(
            series_video_settings,
            project_name=clean_name,
        ),
        "created_at": now,
        "updated_at": now,
    }
    directory = project_dir(project_id)
    directory.mkdir(parents=True, exist_ok=False)
    (directory / "characters").mkdir()
    (directory / "history").mkdir()
    _write_json(directory / PROJECT_FILE, payload)
    _write_json(directory / CHARACTER_PROFILES_FILE, {"enabled": False, "characters": []})
    _write_json(directory / CHARACTER_RELATIONSHIPS_FILE, {"relationships": []})
    _write_json(directory / NAME_REGISTRY_FILE, {"names": []})
    _write_json(directory / VISUAL_BIBLE_FILE, {})
    _write_json(directory / CHARACTER_REFERENCE_MANIFEST_FILE, [])
    return payload


def save_project(project: dict) -> dict:
    project_id = str(project.get("project_id") or "")
    if not project_id:
        raise ValueError("项目缺少 project_id")
    current = load_project(project_id)
    merged = {**current, **project}
    merged["schema_version"] = PROJECT_SCHEMA_VERSION
    merged["feature_revision"] = PROJECT_FEATURE_REVISION
    merged["project_id"] = project_id
    merged["series_video_settings"] = normalize_series_video_settings(
        merged.get("series_video_settings"),
        project_name=str(merged.get("name") or ""),
    )
    merged["updated_at"] = _now()
    _write_json(project_dir(project_id) / PROJECT_FILE, merged)
    return merged


def rename_project(project_id: str, name: str) -> dict:
    project = load_project(project_id)
    if not project:
        raise FileNotFoundError(f"项目不存在：{project_id}")
    clean_name = normalize_project_name(name)
    if not clean_name:
        raise ValueError("项目名称不能为空")
    old_name = str(project.get("name") or "").strip()
    aliases = [str(value) for value in project.get("aliases") or [] if str(value)]
    if old_name and project_match_key(old_name) != project_match_key(clean_name) and old_name not in aliases:
        aliases.append(old_name)
    project["name"] = clean_name
    project["aliases"] = aliases
    return save_project(project)


def update_series_video_settings(project_id: str, updates: dict) -> dict:
    project = load_project(project_id)
    if not project:
        raise FileNotFoundError(f"项目不存在：{project_id}")
    current = dict(project.get("series_video_settings") or {})
    current.update(updates if isinstance(updates, dict) else {})
    project["series_video_settings"] = normalize_series_video_settings(
        current,
        project_name=str(project.get("name") or ""),
    )
    return save_project(project)


def archive_project(project_id: str) -> Path:
    """Move a project to a recoverable trash directory."""
    source = project_dir(project_id)
    if not source.exists():
        raise FileNotFoundError(f"项目不存在：{project_id}")
    trash = PROJECTS_DIR / ".trash"
    trash.mkdir(parents=True, exist_ok=True)
    destination = trash / f"{project_id}_{time.strftime('%Y%m%d_%H%M%S')}"
    sequence = 2
    while destination.exists():
        destination = trash / f"{project_id}_{time.strftime('%Y%m%d_%H%M%S')}_{sequence}"
        sequence += 1
    shutil.move(str(source), str(destination))
    return destination


def add_job(project_id: str, job_id: str, *, source_path: str = "") -> dict:
    with _project_file_lock(project_id):
        project = load_project(project_id)
        if not project:
            raise FileNotFoundError(f"项目不存在：{project_id}")
        jobs = [str(value) for value in project.get("jobs") or [] if str(value)]
        if job_id not in jobs:
            jobs.append(job_id)
        project["jobs"] = jobs
        if source_path:
            directory = str(Path(source_path).expanduser().parent.resolve(strict=False))
            directories = [str(value) for value in project.get("source_directories") or [] if str(value)]
            if directory not in directories:
                directories.append(directory)
            project["source_directories"] = sorted(directories)
        return save_project(project)


def remove_job(project_id: str, job_id: str) -> dict:
    with _project_file_lock(project_id):
        project = load_project(project_id)
        if not project:
            return {}
        project["jobs"] = [
            str(value) for value in project.get("jobs") or [] if str(value) != job_id
        ]
        return save_project(project)


def find_matching_projects(name: str, source_directory: str | Path = "") -> list[dict]:
    key = project_match_key(name)
    directory = (
        str(Path(source_directory).expanduser().resolve(strict=False))
        if str(source_directory).strip()
        else ""
    )
    scored: list[tuple[int, dict]] = []
    for project in list_projects():
        settings = project.get("series_video_settings") or {}
        names = [str(project.get("name") or ""), *[
            str(value) for value in project.get("aliases") or []
        ], str(settings.get("shared_novel_title") or "")]
        name_match = bool(key) and any(project_match_key(value) == key for value in names)
        directory_match = bool(directory) and directory in {
            str(value) for value in project.get("source_directories") or []
        }
        score = int(name_match) * 2 + int(directory_match)
        if score:
            scored.append((score, project))
    scored.sort(
        key=lambda item: (
            item[0],
            str(item[1].get("updated_at") or ""),
        ),
        reverse=True,
    )
    return [project for _score, project in scored]


def detect_import_groups(paths: Iterable[str | Path]) -> list[dict]:
    """Group likely series and attach the best existing-project suggestion."""
    rows = []
    for raw in paths:
        path = Path(raw).expanduser()
        base, episode = infer_episode(path)
        rows.append({
            "path": path,
            "base": base,
            "episode": episode,
            "key": project_match_key(base),
            "directory": str(path.parent.resolve(strict=False)),
        })
    grouped: dict[tuple[str, str], list[dict]] = {}
    for row in rows:
        grouped.setdefault((row["directory"], row["key"]), []).append(row)

    results = []
    for (_directory, _key), members in grouped.items():
        members.sort(key=lambda item: (item["episode"] or 10**9, item["path"].name))
        first = members[0]
        matches = find_matching_projects(first["base"], first["directory"])
        has_numbered_series = len(members) >= 2 and sum(bool(item["episode"]) for item in members) >= 2
        if not has_numbered_series and not matches:
            continue
        confidence = "high" if has_numbered_series or (
            matches and first["directory"] in set(matches[0].get("source_directories") or [])
        ) else "medium"
        results.append({
            "name": normalize_project_name(first["base"]) or first["path"].parent.name,
            "paths": [item["path"] for item in members],
            "episodes": {str(item["path"]): item["episode"] for item in members},
            "existing_project": matches[0] if matches else None,
            "confidence": confidence,
            "reason": "同目录分集文件名一致" if has_numbered_series else "名称与已有项目一致",
        })
    return results


def _merge_character_profiles_unlocked(project_id: str, payload: dict | None) -> dict | None:
    """Merge new analysis without overwriting confirmed project decisions."""
    if not isinstance(payload, dict):
        return payload
    path = project_dir(project_id) / CHARACTER_PROFILES_FILE
    shared = _read_json(path, {})
    if not isinstance(shared, dict):
        shared = {}
    merged = dict(shared or payload)
    existing = [item for item in merged.get("characters") or [] if isinstance(item, dict)]
    identities: dict[str, dict] = {}
    for item in existing:
        names = [item.get("name"), item.get("trigger"), *(item.get("aliases") or [])]
        for name in names:
            key = project_match_key(str(name or ""))
            if key:
                identities[key] = item
    for incoming in payload.get("characters") or []:
        if not isinstance(incoming, dict):
            continue
        names = [incoming.get("name"), incoming.get("trigger"), *(incoming.get("aliases") or [])]
        target = next(
            (identities.get(project_match_key(str(name or ""))) for name in names if name),
            None,
        )
        if target is None:
            target = dict(incoming)
            target.setdefault("record_status", "auto")
            existing.append(target)
        elif str(target.get("record_status") or "auto") != "confirmed":
            old_aliases = [str(value) for value in target.get("aliases") or [] if str(value)]
            for alias in incoming.get("aliases") or []:
                alias = str(alias).strip()
                if alias and alias not in old_aliases:
                    old_aliases.append(alias)
            target["aliases"] = old_aliases
            stable_fields = {
                "gender",
                "age_group",
                "role_in_story",
                "personality",
                "visual_profile_zh",
                "visual_prompt_en",
                "reference_prompt_en",
                "lock_rules_zh",
            }
            for key, value in incoming.items():
                if key not in target or target.get(key) in ("", None, [], {}):
                    target[key] = value
                elif (
                    key in stable_fields
                    and value not in ("", None, [], {})
                    and target.get(key) != value
                ):
                    conflicts = target.get("conflicts")
                    if not isinstance(conflicts, dict):
                        conflicts = {}
                    alternatives = [
                        str(item)
                        for item in conflicts.get(key) or []
                        if str(item)
                    ]
                    if str(value) not in alternatives:
                        alternatives.append(str(value))
                    conflicts[key] = alternatives
                    target["conflicts"] = conflicts
                    target["record_status"] = "conflicted"
        for name in names:
            key = project_match_key(str(name or ""))
            if key:
                identities[key] = target
    merged["characters"] = existing
    merged["enabled"] = bool(shared.get("enabled", payload.get("enabled", False)))
    for key in (
        "visual_theme",
        "plot_summary",
        "story_conflict",
        "protagonists",
        "supporting_characters",
    ):
        if not merged.get(key) and payload.get(key):
            merged[key] = payload[key]
    merged["project_id"] = project_id
    merged["updated_at"] = _now()
    _write_json(path, merged)
    names = []
    for character in existing:
        name = str(character.get("name") or "").strip()
        if not name:
            continue
        names.append({
            "name": name,
            "aliases": [
                str(value).strip()
                for value in character.get("aliases") or []
                if str(value).strip()
            ],
            "trigger": str(character.get("trigger") or "").strip(),
            "importance": str(character.get("importance") or "").strip(),
            "record_status": str(character.get("record_status") or "auto"),
        })
    _write_json(
        project_dir(project_id) / NAME_REGISTRY_FILE,
        {"project_id": project_id, "names": names, "updated_at": _now()},
    )
    incoming_relationships = (
        payload.get("relationships")
        or payload.get("character_relationships")
        or []
    )
    relationship_path = project_dir(project_id) / CHARACTER_RELATIONSHIPS_FILE
    relationship_data = _read_json(relationship_path, {})
    old_relationships = (
        relationship_data.get("relationships")
        if isinstance(relationship_data, dict)
        else []
    )
    combined_relationships = []
    relationship_index: dict[tuple[str, str], dict] = {}
    for relationship in [*(old_relationships or []), *(incoming_relationships or [])]:
        if not isinstance(relationship, dict):
            continue
        source = str(relationship.get("from") or relationship.get("source") or "").strip()
        target = str(relationship.get("to") or relationship.get("target") or "").strip()
        relation = str(relationship.get("relation") or relationship.get("type") or "").strip()
        if not source or not target or not relation:
            continue
        pair = (project_match_key(source), project_match_key(target))
        current = relationship_index.get(pair)
        normalized_relationship = {
            "from": source,
            "to": target,
            "relation": relation,
            "record_status": "confirmed"
            if str(relationship.get("record_status") or "").lower() == "confirmed"
            else "auto",
        }
        if current is None:
            combined_relationships.append(normalized_relationship)
            relationship_index[pair] = normalized_relationship
        elif current.get("relation") != relation:
            conflicts = [
                str(value) for value in current.get("conflicts") or [] if str(value)
            ]
            if relation not in conflicts:
                conflicts.append(relation)
            current["conflicts"] = conflicts
            if str(current.get("record_status") or "") != "confirmed":
                current["record_status"] = "conflicted"
    _write_json(
        relationship_path,
        {
            "project_id": project_id,
            "relationships": combined_relationships,
            "updated_at": _now(),
        },
    )
    visual_theme = merged.get("visual_theme")
    if isinstance(visual_theme, dict) and visual_theme:
        visual_path = project_dir(project_id) / VISUAL_BIBLE_FILE
        visual_bible = _read_json(visual_path, {})
        if not isinstance(visual_bible, dict):
            visual_bible = {}
        for key, value in visual_theme.items():
            if key not in visual_bible or visual_bible.get(key) in ("", None, [], {}):
                visual_bible[key] = value
        visual_bible["project_id"] = project_id
        visual_bible["updated_at"] = _now()
        _write_json(visual_path, visual_bible)
    return merged


def merge_character_profiles(project_id: str, payload: dict | None) -> dict | None:
    with _project_file_lock(project_id):
        return _merge_character_profiles_unlocked(project_id, payload)


def load_character_profiles(project_id: str) -> dict:
    value = _read_json(project_dir(project_id) / CHARACTER_PROFILES_FILE, {})
    return value if isinstance(value, dict) else {}


def set_character_record_status(project_id: str, identity: str, status: str) -> dict:
    normalized = "confirmed" if str(status).lower() == "confirmed" else "auto"
    with _project_file_lock(project_id):
        payload = load_character_profiles(project_id)
        target_key = project_match_key(identity)
        found = False
        for character in payload.get("characters") or []:
            if not isinstance(character, dict):
                continue
            keys = {
                project_match_key(str(value or ""))
                for value in (
                    character.get("name"),
                    character.get("trigger"),
                    *(character.get("aliases") or []),
                )
                if value
            }
            if target_key in keys:
                character["record_status"] = normalized
                found = True
                break
        if not found:
            raise KeyError(f"项目中没有找到人物：{identity}")
        payload["updated_at"] = _now()
        _write_json(project_dir(project_id) / CHARACTER_PROFILES_FILE, payload)
        return payload


def character_reference_manifest_path(project_id: str) -> Path:
    return project_dir(project_id) / CHARACTER_REFERENCE_MANIFEST_FILE


@contextmanager
def character_reference_lock(project_id: str, identity: str):
    safe_identity = hashlib.sha256(str(identity or "character").encode("utf-8")).hexdigest()[:16]
    directory = project_dir(project_id)
    directory.mkdir(parents=True, exist_ok=True)
    lock_path = directory / f".character_{safe_identity}.lock"
    handle = lock_path.open("a+", encoding="utf-8")
    try:
        try:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        except (ImportError, OSError):
            pass
        yield
    finally:
        try:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except (ImportError, OSError):
            pass
        handle.close()


def write_character_reference_manifest(project_id: str, value: list[dict]) -> None:
    with _project_file_lock(project_id):
        path = character_reference_manifest_path(project_id)
        existing = _read_json(path, [])
        combined = [item for item in existing if isinstance(item, dict)] if isinstance(existing, list) else []
        index = {
            project_match_key(str(item.get("trigger") or item.get("name") or "")): position
            for position, item in enumerate(combined)
            if project_match_key(str(item.get("trigger") or item.get("name") or ""))
        }
        for item in value:
            if not isinstance(item, dict):
                continue
            key = project_match_key(str(item.get("trigger") or item.get("name") or ""))
            if key and key in index:
                combined[index[key]] = item
            else:
                if key:
                    index[key] = len(combined)
                combined.append(item)
        _write_json(path, combined)


def migrate_legacy_series(
    *,
    write_job_status,
) -> list[dict]:
    """Create projects for old series groups; safe to call repeatedly."""
    groups: dict[str, list[tuple[Path, dict]]] = {}
    if not JOBS_DIR.exists():
        return []
    for job_dir in JOBS_DIR.iterdir():
        if not job_dir.is_dir():
            continue
        status = _read_json(job_dir / "status.json", {})
        if not isinstance(status, dict) or status.get("project_id"):
            continue
        key = str(status.get("series_group_key") or "").strip()
        if key and int(status.get("series_episode") or 0) > 0:
            groups.setdefault(key, []).append((job_dir, status))

    created = []
    legacy_profiles = _read_json(JOBS_DIR / ".series_character_profiles.json", {})
    for group_key, members in groups.items():
        if len(members) < 2:
            continue
        first_status = members[0][1]
        title = str(first_status.get("series_title") or first_status.get("title") or group_key)
        source_directories = {
            str(status.get("source_directory") or "")
            for _job_dir, status in members
            if str(status.get("source_directory") or "")
        }
        project = create_project(
            title,
            source_directories=source_directories,
            series_video_settings={
                "shared_novel_title": title,
                "shared_novel_title_locked": False,
            },
        )
        for job_dir, status in members:
            add_job(
                project["project_id"],
                job_dir.name,
                source_path=str(status.get("source_path") or ""),
            )
            write_job_status(
                job_dir,
                project_id=project["project_id"],
                project_name=project["name"],
                project_episode=int(status.get("series_episode") or 0),
                project_mode="shared",
            )
        old_profile = legacy_profiles.get(group_key) if isinstance(legacy_profiles, dict) else None
        if isinstance(old_profile, dict):
            merge_character_profiles(project["project_id"], old_profile)
        shared_profile = load_character_profiles(project["project_id"])
        candidate_profiles = [shared_profile]
        for job_dir, _status in members:
            candidate = _read_json(job_dir / CHARACTER_PROFILES_FILE, {})
            if isinstance(candidate, dict):
                candidate_profiles.append(candidate)
        reference_sources: dict[str, Path] = {}
        for profile in candidate_profiles:
            for character in profile.get("characters") or []:
                if not isinstance(character, dict):
                    continue
                source = Path(str(character.get("reference_image") or "")).expanduser()
                if not source.is_file():
                    continue
                for identity in (
                    character.get("trigger"),
                    character.get("name"),
                    *(character.get("aliases") or []),
                ):
                    key = project_match_key(str(identity or ""))
                    if key and key not in reference_sources:
                        reference_sources[key] = source
        migrated_manifest = []
        changed = False
        for index, character in enumerate(shared_profile.get("characters") or []):
            if not isinstance(character, dict):
                continue
            identity_values = [
                character.get("trigger"),
                character.get("name"),
                *(character.get("aliases") or []),
            ]
            source = next(
                (
                    reference_sources.get(project_match_key(str(identity or "")))
                    for identity in identity_values
                    if reference_sources.get(project_match_key(str(identity or "")))
                ),
                None,
            )
            if source is None:
                continue
            trigger = str(character.get("trigger") or character.get("name") or f"char_{index + 1}")
            destination = (
                project_dir(project["project_id"])
                / "characters"
                / f"{_safe_artifact_name(trigger, f'char_{index + 1}')}{source.suffix.lower() or '.png'}"
            )
            if not destination.exists():
                shutil.copy2(source, destination)
            character["reference_image"] = str(destination)
            migrated_manifest.append({
                "name": str(character.get("name") or trigger),
                "trigger": trigger,
                "path": str(destination),
                "prompt": str(character.get("reference_prompt") or ""),
            })
            changed = True
        if changed:
            _write_json(
                project_dir(project["project_id"]) / CHARACTER_PROFILES_FILE,
                shared_profile,
            )
            write_character_reference_manifest(project["project_id"], migrated_manifest)
        created.append(load_project(project["project_id"]))
    return created
