"""Pipeline orchestration.

Stages write durable artifacts into data/jobs/{job_id}.  Re-running the same
job can reuse existing audio/images, which matters for very long videos.
"""
from __future__ import annotations

import json
import hashlib
import html
import os
import re
import shlex
import shutil
import signal
import secrets
import subprocess
import sys
import time
import traceback
import threading
import unicodedata
from contextlib import nullcontext
from datetime import datetime, timedelta
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, as_completed, wait
from dataclasses import asdict
from pathlib import Path
from typing import Callable, Iterable

from PIL import Image, ImageDraw, ImageFont, ImageOps

from app.config import API_KEY_FIELDS, JOBS_DIR, ROOT, api_key_env_name, config
from app import project_manager as projects
from app.concurrency import external_api_slot, ffmpeg_slot, pid_alive
from app.scrapers.base import BookSearchResult, Novel, NovelChapter
from app.scrapers.kakuyomu import KakuyomuScraper
from app.scrapers.legado import LegadoStaticScraper
from app.scrapers.qingtian import QingtianAggregateScraper, parse_host_list
from app.scrapers.source_catalog import SourceCatalogScraper
from app.scrapers.syosetu import SyosetuScraper
from app.stages.stage2_clean import Segment, split_segments
from app.stages.stage6_compose import build_ass, build_srt, build_video, concat_audios
from app.stages.stage_pacing import ImagePlan, plan_images
from app.backends.image import ImageBackend
from app.backends.llm import LLMBackend
from app.backends.tts import TTSBackend, _audio_duration
from app.character_analysis import (
    DEFAULT_CHARACTER_ANALYSIS_PROMPT,
    analyze_characters,
    can_call_analysis_llm,
    character_context_for_text,
    character_reference_prompt,
    visual_theme_context,
)
from app.storyboard_highlights import (
    HIGHLIGHT_SYSTEM_PROMPT,
    STORY_CONTEXT_SYSTEM_PROMPT,
    fallback_highlight,
    highlight_request,
    normalize_highlight,
    normalize_story_context,
    parse_json_object,
    sampled_story_input,
)
from app.utils.ffmpeg import ffmpeg_path
from app.utils.secrets import clean_api_key, redact_secret_text


LogFn = Callable[[str], None]
ProgFn = Callable[[float], None]
IMAGE_FAILURE_DECISION_FILE = "image_failure_decision.json"
IMAGE_SELECTION_FILE = "image_selection.json"
TTS_REDO_REUSE_IMAGES_FILE = "tts_redo_reuse_images.json"
SETTINGS_SNAPSHOT_FILE = "settings_snapshot.json"
ACCELERATION_PREFETCH_REPORT = "acceleration_prefetch.json"
SOURCE_INPUT_SNAPSHOT_DIR = "_source_input"


class ImageGenerationFailed(RuntimeError):
    """A scene image could not be generated after its normal retries."""

    def __init__(self, index: int, error: str):
        super().__init__(error)
        self.index = index
        self.error = error


class ImageGenerationSkipped(RuntimeError):
    """The operator chose to leave an image-failed job for later handling."""


def _parallel_limit(config_key: str, default: int, total: int, hard_cap: int = 16) -> int:
    try:
        raw = int(config.get(config_key, default) or default)
    except Exception:
        raw = default
    return max(1, min(int(total or 1), hard_cap, raw))


def _bounded_timeout(value, default: float, minimum: float = 30.0, maximum: float = 1800.0) -> float:
    try:
        seconds = float(value if value not in (None, "") else default)
    except Exception:
        seconds = float(default)
    return max(minimum, min(maximum, seconds))


def _optional_timeout(value, default: float, minimum: float = 30.0, maximum: float = 7200.0) -> float:
    try:
        seconds = float(value if value not in (None, "") else default)
    except Exception:
        seconds = float(default)
    if seconds <= 0:
        return 0.0
    return max(minimum, min(maximum, seconds))


def _unified_ai_enabled() -> bool:
    return bool(config.get("ai_api_enabled", True)) and bool(
        str(config.get("ai_api_base_url", "") or "").strip() or str(config.get("ai_api_key", "") or "").strip()
    )


def _llm_route_settings() -> dict:
    provider = str(config.llm_provider or "openai").strip().lower()
    base_url = str(config.llm_base_url or "").strip()
    api_key = str(config.llm_api_key or "").strip()
    model = str(config.llm_model or "").strip()
    if _unified_ai_enabled() and provider in {"openai", "custom", "deepseek", "gemini"}:
        base_url = str(config.get("ai_api_base_url", "") or base_url).strip()
        api_key = str(config.get("ai_api_key", "") or api_key).strip()
        model = str(config.get("ai_api_text_model", "") or model).strip()
    return {"provider": provider, "base_url": base_url, "api_key": api_key, "model": model}


def _can_call_text_llm() -> bool:
    route = _llm_route_settings()
    return bool(route["api_key"]) or route["provider"] in {"ollama", "custom"}


def _pronunciation_dictionary_route_settings() -> dict:
    """Use the existing text route unless the operator enables dictionary overrides."""
    route = _llm_route_settings()
    if not bool(config.get("pronunciation_dictionary_dedicated_api_enabled", False)):
        return route
    return {
        "provider": route["provider"],
        "base_url": str(config.get("pronunciation_dictionary_base_url", "") or route["base_url"]).strip(),
        "api_key": str(config.get("pronunciation_dictionary_api_key", "") or route["api_key"]).strip(),
        "model": str(config.get("pronunciation_dictionary_model", "") or route["model"]).strip(),
    }


def _can_call_pronunciation_dictionary_llm() -> bool:
    route = _pronunciation_dictionary_route_settings()
    return bool(route["api_key"]) or route["provider"] in {"ollama", "custom"}


def _apply_unified_image_api(provider: str, base_url: str, api_key: str, model: str) -> tuple[str, str, str, str]:
    provider = str(provider or "").strip().lower()
    if _unified_ai_enabled():
        # The unified image API is the master route while enabled.  Force every
        # image role through its OpenAI-compatible endpoint instead of allowing
        # a stale per-role provider (for example Replicate) to bypass it.
        provider = "openai"
        base_url = str(config.get("ai_api_base_url", "") or base_url or "").strip()
        api_key = str(config.get("ai_api_key", "") or api_key or "").strip()
        model = str(config.get("ai_api_image_model", "") or model or "").strip()
    return provider, base_url, api_key, model


def _noop(*_args, **_kwargs):
    pass


_JOB_ID_LOCK = threading.Lock()
_STATUS_WRITE_LOCK = threading.RLock()
_JOB_ID_LAST_STAMP = ""
_JOB_ID_SEQUENCE = 0
_INVALID_FILENAME_CHARS_RE = re.compile(r'[\x00-\x1f\x7f/\\:<>"|?*]+')
_WINDOWS_RESERVED_NAMES = {
    "con", "prn", "aux", "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}


def safe_job_name(value: str, fallback: str = "article") -> str:
    """Return a readable, cross-platform-safe project/output basename."""
    name = unicodedata.normalize("NFKC", str(value or "")).strip()
    name = _INVALID_FILENAME_CHARS_RE.sub("_", name)
    name = re.sub(r"\s+", " ", name).strip(" .")
    if not name or name in {".", ".."}:
        name = fallback
    if name.casefold() in _WINDOWS_RESERVED_NAMES:
        name = f"_{name}"
    # Leave room for duplicate suffixes and output extensions on filesystems
    # whose filename limit is measured in UTF-8 bytes.
    if len(name.encode("utf-8")) > 180:
        name = name.encode("utf-8")[:180].decode("utf-8", errors="ignore").rstrip(" .")
    return name or fallback


def new_named_job_id(name: str, fallback: str = "article") -> str:
    """Use the imported filename as the job directory, without overwriting."""
    base = safe_job_name(name, fallback=fallback)
    with _JOB_ID_LOCK:
        candidate = base
        sequence = 1
        while job_dir_for(candidate).exists():
            sequence += 1
            suffix = f"_{sequence}"
            max_base_bytes = 180 - len(suffix.encode("utf-8"))
            trimmed = base.encode("utf-8")[:max_base_bytes].decode("utf-8", errors="ignore").rstrip(" .")
            candidate = f"{trimmed or fallback}{suffix}"
        return candidate


def new_job_id(prefix: str = "job") -> str:
    global _JOB_ID_LAST_STAMP, _JOB_ID_SEQUENCE

    with _JOB_ID_LOCK:
        stamp = time.strftime("%Y%m%d_%H%M%S")
        if stamp == _JOB_ID_LAST_STAMP:
            _JOB_ID_SEQUENCE += 1
        else:
            _JOB_ID_LAST_STAMP = stamp
            _JOB_ID_SEQUENCE = 0

        suffix = "" if _JOB_ID_SEQUENCE == 0 else f"_{_JOB_ID_SEQUENCE:03d}"
        candidate = f"{prefix}_{stamp}{suffix}"
        while job_dir_for(candidate).exists():
            _JOB_ID_SEQUENCE += 1
            candidate = f"{prefix}_{stamp}_{_JOB_ID_SEQUENCE:03d}"
        return candidate


def job_dir_for(job_id: str) -> Path:
    return JOBS_DIR / job_id


def video_output_path(job_dir: Path) -> Path:
    """Choose a readable final video name while retaining old-job behavior."""
    output_basename = ""
    status_path = job_dir / "status.json"
    if status_path.exists():
        try:
            status = json.loads(status_path.read_text(encoding="utf-8"))
            output_basename = str(status.get("output_basename") or "").strip()
        except Exception:
            pass
    if not output_basename:
        # Jobs created before readable names were introduced always used this.
        return job_dir / "final.mp4"
    return job_dir / f"{safe_job_name(output_basename)}.mp4"


def export_completed_video(
    video_path: Path,
    cover_path: Path | None,
    title: str,
    short_title: str = "",
    source_path: str = "",
    on_log: LogFn = _noop,
) -> Path | None:
    """Export one completed task as a self-contained delivery folder.

    The working copy deliberately stays in the job folder, because it is used
    for resume and optional YouTube upload.  The delivery folder is an
    additional stable hand-off package for workflows where publishing is off.
    """
    source = Path(str(source_path or "")).expanduser()
    if bool(config.get("output_to_input_directory", False)) and source.is_file():
        try:
            destination_dir = source.parent.resolve()
            base = safe_job_name(source.stem, fallback="article")
            destination_video = destination_dir / f"{base}.mp4"
            temporary_video = destination_video.with_name(
                f".{destination_video.stem}.{os.getpid()}.new{destination_video.suffix}"
            )
            temporary_video.unlink(missing_ok=True)
            try:
                shutil.copy2(video_path, temporary_video)
                if temporary_video.stat().st_size < 1000:
                    raise RuntimeError("copied video is unexpectedly small")
                os.replace(temporary_video, destination_video)
            finally:
                temporary_video.unlink(missing_ok=True)
            if cover_path is not None and cover_path.exists():
                shutil.copy2(cover_path, destination_dir / f"{base}_cover{cover_path.suffix.lower() or '.jpg'}")
            title_lines = [f"系列标题：{title.strip() or video_path.stem}"]
            if short_title.strip():
                title_lines.append(f"本集标题：{short_title.strip()}")
            (destination_dir / f"{base}_标题.txt").write_text("\n".join(title_lines) + "\n", encoding="utf-8")
            on_log(f"  OK 成品已输出回原文件夹: {destination_dir}")
            return destination_dir
        except Exception as exc:
            on_log(f"  ⚠️ 输出回原文件夹失败（任务内成品仍保留）: {exc}")
            return None

    configured = str(config.get("output_directory", "") or "").strip()
    if not configured:
        return None
    try:
        destination_dir = Path(configured).expanduser().resolve()
        destination_dir.mkdir(parents=True, exist_ok=True)
        destination = destination_dir / safe_job_name(video_path.stem, fallback="article")
        sequence = 2
        while destination.exists():
            destination = destination_dir / f"{safe_job_name(video_path.stem, fallback='article')}_{sequence}"
            sequence += 1
        temporary = destination.with_name(f".{destination.name}.copying")
        if temporary.exists():
            shutil.rmtree(temporary)
        temporary.mkdir()
        shutil.copy2(video_path, temporary / video_path.name)
        if cover_path is not None and cover_path.exists():
            shutil.copy2(cover_path, temporary / cover_path.name)
        title_lines = [f"原始标题：{title.strip() or video_path.stem}"]
        if short_title.strip():
            title_lines.append(f"生成短标题：{short_title.strip()}")
        (temporary / "标题.txt").write_text("\n".join(title_lines) + "\n", encoding="utf-8")
        temporary.replace(destination)
        on_log(f"  OK 成品文件夹已导出: {destination}")
        return destination
    except Exception as exc:
        on_log(f"  ⚠️ 成品文件夹导出失败（任务内成品仍保留）: {exc}")
        return None


def append_log(job_dir: Path, message: str):
    job_dir.mkdir(parents=True, exist_ok=True)
    with (job_dir / "log.txt").open("a", encoding="utf-8") as f:
        f.write(redact_secret_text(message).rstrip() + "\n")


def tail_log(job_id: str, lines: int = 240) -> str:
    path = job_dir_for(job_id) / "log.txt"
    if not path.exists():
        return ""
    data = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    return "\n".join(data[-lines:])


def write_status(job_dir: Path, **updates):
    # TTS and image generation report progress from multiple threads.  Serialize
    # their read/modify/write cycle and replace atomically so one reporter cannot
    # erase fields (notably worker_pid) written by another reporter.
    with _STATUS_WRITE_LOCK:
        job_dir.mkdir(parents=True, exist_ok=True)
        path = job_dir / "status.json"
        current = {}
        if path.exists():
            try:
                current = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                current = {}
        current.update({key: redact_secret_text(value) if isinstance(value, str) else value for key, value in updates.items()})
        current["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        temp_path = job_dir / f".status.{os.getpid()}.{threading.get_ident()}.tmp"
        temp_path.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temp_path, path)


def _worker_pid_path(job_id: str) -> Path:
    return job_dir_for(job_id) / "worker.pid"


def record_worker_pid(job_id: str, pid: int) -> None:
    path = _worker_pid_path(job_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(int(pid)), encoding="ascii")


def clear_worker_pid(job_id: str, pid: int | None = None) -> None:
    path = _worker_pid_path(job_id)
    if pid is not None:
        try:
            if int(path.read_text(encoding="ascii").strip()) != int(pid):
                return
        except (FileNotFoundError, OSError, ValueError):
            return
    path.unlink(missing_ok=True)


def worker_pid(job_id: str) -> int:
    try:
        pid = int(_worker_pid_path(job_id).read_text(encoding="ascii").strip())
        if pid_alive(pid):
            return pid
    except (FileNotFoundError, OSError, ValueError):
        pass
    status = load_status(job_id, include_worker=False)
    try:
        pid = int(status.get("worker_pid") or 0)
        if pid_alive(pid):
            return pid
    except (TypeError, ValueError):
        pass
    # Compatibility recovery for workers launched by an older build before the
    # durable PID file existed.  This is intentionally an exact argv match.
    legacy_active_stages = {
        "worker_starting", "starting", "scrape", "clean", "api_preprocessing", "tts",
        "images_prefetch", "pacing", "images", "cover", "compose",
        "upload", "stopping",
    }
    if os.name != "nt" and status.get("stage") in legacy_active_stages:
        try:
            output = subprocess.run(
                ["ps", "-axo", "pid=,command="],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="ignore",
                timeout=3,
                check=False,
            ).stdout
            for line in output.splitlines():
                raw_pid, _, command = line.strip().partition(" ")
                try:
                    argv = shlex.split(command)
                    index = argv.index("--job-id")
                except (ValueError, IndexError):
                    continue
                is_pipeline_worker = any(
                    argv[i] == "-m" and argv[i + 1] == "app.worker"
                    for i in range(len(argv) - 1)
                )
                if is_pipeline_worker and index + 1 < len(argv) and argv[index + 1] == job_id:
                    pid = int(raw_pid)
                    if pid_alive(pid):
                        record_worker_pid(job_id, pid)
                        return pid
        except (OSError, subprocess.SubprocessError, ValueError):
            pass
    return 0


def load_status(job_id: str, *, include_worker: bool = True) -> dict:
    path = job_dir_for(job_id) / "status.json"
    if not path.exists():
        return {"job_id": job_id, "stage": "missing", "progress": 0}
    try:
        status = json.loads(path.read_text(encoding="utf-8"))
        if include_worker:
            durable_pid = worker_pid(job_id)
            if durable_pid:
                status["worker_pid"] = durable_pid
            status["worker_alive"] = bool(durable_pid)
        status["stage_display"] = stage_display(status)
        return status
    except Exception as exc:
        return {"job_id": job_id, "stage": "broken", "progress": 0, "error": str(exc)}


_STAGE_LABELS = {
    "queued": "已加入队列",
    "paused": "已暂停",
    "starting": "启动中",
    "scrape": "1/6 读取文本",
    "clean": "2/6 整理/洗稿切片",
    "api_preprocessing": "加速预处理",
    "preprocessed": "加速预处理完成",
    "tts": "3/6 配音",
    "images_prefetch": "4/6 预生成图片",
    "image_decision": "等待图片处理选择",
    "pacing": "4/6 规划画面",
    "images": "4/6 生成图片",
    "cover": "5/6 生成封面",
    "compose": "5/6 合成视频",
    "upload": "6/6 上传",
    "completed": "完成",
    "failed": "失败",
    "stopping": "停止中",
    "stopped": "已停止",
    "stop_failed": "停止失败",
    "missing": "目录缺失",
    "broken": "状态损坏",
}


def stage_display(status: dict) -> str:
    stage = str(status.get("stage") or "")
    label = _STAGE_LABELS.get(stage, stage or "未知")
    if stage == "tts" and str(status.get("audio_mode") or "") == "imported":
        label = "3/6 读取导入音频"
    try:
        progress = max(0.0, min(1.0, float(status.get("progress") or 0)))
    except Exception:
        progress = 0.0
    if stage == "upload" and status.get("upload_progress") is not None:
        try:
            upload_progress = max(0.0, min(1.0, float(status.get("upload_progress") or 0)))
            return f"{label} {upload_progress * 100:.0f}%"
        except Exception:
            pass
    return f"{label} {progress * 100:.0f}%"


def list_jobs(limit: int = 50) -> list[dict]:
    rows: list[dict] = []
    if not JOBS_DIR.exists():
        return rows
    for d in [p for p in JOBS_DIR.iterdir() if p.is_dir()]:
        st = load_status(d.name)
        rows.append(
            {
                "job_id": d.name,
                "stage": st.get("stage", ""),
                "stage_display": stage_display(st),
                "progress": st.get("progress", 0),
                "title": st.get("title", ""),
                "video": st.get("video", ""),
                "worker_pid": st.get("worker_pid"),
                "worker_alive": st.get("worker_alive", False),
                "updated_at": st.get("updated_at", ""),
            }
        )
    # Project names are now human-readable instead of timestamp-prefixed, so
    # alphabetical directory order no longer represents recency.
    rows.sort(key=lambda row: (str(row.get("updated_at") or ""), row["job_id"]), reverse=True)
    return rows[:limit]


def is_worker_running(job_id: str) -> bool:
    return bool(worker_pid(job_id))


def count_running_workers() -> int:
    """Count how many worker processes are currently alive across all jobs."""
    if not JOBS_DIR.exists():
        return 0
    count = 0
    for d in JOBS_DIR.iterdir():
        if not d.is_dir():
            continue
        st = load_status(d.name)
        if st.get("worker_alive"):
            count += 1
    return count


def job_acceleration_enabled(job_id: str) -> bool:
    return bool(load_status(job_id, include_worker=False).get("acceleration_enabled", False))


def set_jobs_acceleration(job_ids: Iterable[str], enabled: bool) -> int:
    value = bool(enabled)
    changed = 0
    for raw_job_id in job_ids:
        job_id = str(raw_job_id)
        job_dir = _safe_job_path(job_id)
        status = load_status(job_id)
        if not job_dir.exists():
            continue
        if not value and status.get("worker_alive") and bool(status.get("acceleration_preprocess")):
            stop_job(job_id)
            status = load_status(job_id)
        stage = str(status.get("stage") or "")
        if not value and stage == "preprocessed":
            stage = "queued"
        write_status(
            job_dir,
            stage=stage,
            acceleration_enabled=value,
            acceleration_preprocess=False if not value else bool(status.get("acceleration_preprocess")),
            worker_pid=None if not value and bool(status.get("acceleration_preprocess")) else status.get("worker_pid"),
            error="",
        )
        append_log(job_dir, f"task acceleration {'enabled' if value else 'disabled'}")
        changed += 1
    return changed


def _acceleration_remote_image_allowed() -> bool:
    provider = str(_image_route_settings("scene").get("provider") or "").strip().lower()
    return provider not in {"", "placeholder", "sdwebui", "comfyui"}


def start_acceleration_preprocess_next(
    *,
    source_job_id: str | None = None,
    on_log: LogFn = _noop,
    require_active_stage: bool = True,
) -> tuple[str, int] | None:
    """Use one look-ahead worker for API stages without entering TTS/FFmpeg."""
    rows = list_jobs(limit=500)
    for row in rows:
        job_id = str(row.get("job_id") or "")
        if job_id == source_job_id:
            continue
        st = load_status(job_id)
        if st.get("worker_alive") and bool(st.get("acceleration_preprocess")):
            return None
        if st.get("stage") == "preprocessed" and bool(st.get("acceleration_enabled")):
            return None
    if require_active_stage and source_job_id is None:
        active_stages = {"tts", "images_prefetch", "pacing", "images", "cover", "compose"}
        if not any(
            bool(row.get("worker_alive")) and str(row.get("stage") or "") in active_stages
            for row in rows
        ):
            return None
    for row in rows:
        job_id = str(row.get("job_id") or "")
        if not job_id or job_id == source_job_id:
            continue
        st = load_status(job_id)
        if st.get("worker_alive") or st.get("stage") != "queued":
            continue
        if not bool(st.get("acceleration_enabled", False)):
            continue
        input_text = str(st.get("input") or "")
        if not input_text:
            continue
        try:
            started = start_worker(
                input_text,
                job_id=job_id,
                resume=True,
                preprocess_only=True,
                bypass_capacity=True,
            )
        except Exception as exc:
            safe_error = str(redact_secret_text(exc))
            write_status(
                job_dir_for(job_id),
                stage="failed",
                worker_pid=None,
                acceleration_preprocess=False,
                error=safe_error,
            )
            append_log(job_dir_for(job_id), f"accelerator source preflight blocked: {safe_error}")
            on_log(f"  加速模式跳过无效任务 {job_id}：{safe_error}")
            continue
        if started[1] > 0:
            append_log(job_dir_for(job_id), f"accelerator look-ahead started by {source_job_id or 'GUI'}")
            on_log(f"  加速模式：提前预处理下一任务 {job_id} pid={started[1]}")
            return started
    return None


def _safe_job_path(job_id: str) -> Path:
    root = JOBS_DIR.resolve()
    path = job_dir_for(job_id).resolve()
    if path == root or root not in path.parents:
        raise ValueError(f"unsafe job path: {path}")
    return path


def _terminate_process_tree(pid: int, *, timeout_seconds: float = 8.0) -> bool:
    if not pid_alive(pid):
        return True
    if os.name == "nt":
        proc = subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="ignore",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            if not pid_alive(pid):
                return True
            time.sleep(0.2)
        return proc.returncode == 0 and not pid_alive(pid)

    try:
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        return True
    except Exception:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            return True
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if not pid_alive(pid):
            return True
        time.sleep(0.2)
    try:
        os.killpg(pid, signal.SIGKILL)
    except Exception:
        try:
            os.kill(pid, signal.SIGKILL)
        except Exception:
            pass
    return not pid_alive(pid)


def stop_job(job_id: str) -> bool:
    path = _safe_job_path(job_id)
    st = load_status(job_id)
    pid = worker_pid(job_id) or st.get("worker_pid")
    try:
        pid_int = int(pid or 0)
    except Exception:
        pid_int = 0

    if not path.exists():
        return False
    if not pid_int or not pid_alive(pid_int):
        clear_worker_pid(job_id)
        write_status(
            path,
            stage="stopped" if st.get("stage") not in {"completed", "failed"} else st.get("stage"),
            worker_pid=None,
            stopped_at=time.strftime("%Y-%m-%d %H:%M:%S"),
        )
        append_log(path, "stop requested: worker was not running")
        return False

    append_log(path, f"stop requested: terminating worker pid={pid_int}")
    write_status(path, stage="stopping", worker_pid=pid_int, stopped_at=time.strftime("%Y-%m-%d %H:%M:%S"))
    ok = _terminate_process_tree(pid_int)
    if not ok:
        write_status(path, stage="stop_failed", worker_pid=pid_int, error=f"无法停止 worker pid={pid_int}")
        append_log(path, f"stop failed: worker pid={pid_int} is still alive")
        raise RuntimeError(f"{job_id} 停止失败，worker pid={pid_int} 仍在运行")
    write_status(path, stage="stopped", worker_pid=None, stopped_at=time.strftime("%Y-%m-%d %H:%M:%S"))
    clear_worker_pid(job_id, pid_int)
    append_log(path, f"worker pid={pid_int} stopped by user")
    return True


def delete_job(job_id: str, *, stop_running: bool = False) -> bool:
    if is_worker_running(job_id):
        if not stop_running:
            raise RuntimeError(f"{job_id} 仍在运行，不能删除")
        stop_job(job_id)
    path = _safe_job_path(job_id)
    if not path.exists():
        return False
    project_id = str(load_status(job_id, include_worker=False).get("project_id") or "")
    last_error: Exception | None = None
    for _ in range(8):
        try:
            shutil.rmtree(path)
            if project_id:
                projects.remove_job(project_id, job_id)
            return True
        except FileNotFoundError:
            return False
        except Exception as exc:
            last_error = exc
            time.sleep(0.25)
    if last_error:
        raise last_error
    return True


def clear_job_media_cache(job_id: str) -> None:
    """Clear generated media so rerun will call image/cover/compose stages again."""
    if is_worker_running(job_id):
        raise RuntimeError(f"{job_id} 仍在运行，不能清理缓存")
    path = _safe_job_path(job_id)
    if not path.exists():
        return
    for name in ("images", "cover", "_clips"):
        target = path / name
        if target.exists():
            shutil.rmtree(target)
    for name in (
        "prompts.json",
        "compose_manifest.json",
        "audio_full.mp3",
        "subtitle.ass",
        "subtitle.srt",
        "result.json",
        "upload_result.json",
    ):
        (path / name).unlink(missing_ok=True)
    for video_path in {path / "final.mp4", video_output_path(path)}:
        video_path.unlink(missing_ok=True)
        video_path.with_name(video_path.stem + ".tmp" + video_path.suffix).unlink(missing_ok=True)
    write_status(path, stage="queued", progress=0.0, video="", youtube_url="", cover="")
    append_log(path, "media cache cleared: images/cover/final video will regenerate")


def reset_full_job(job_id: str) -> dict:
    """Reset every generated stage while preserving operator-owned inputs.

    The settings snapshot is intentionally removed so the next start freezes
    the configuration currently visible in the GUI. Imported narration and a
    manually attached pronunciation dictionary are user inputs, not caches.
    """
    if is_worker_running(job_id):
        raise RuntimeError(f"{job_id} 正在运行。请先停止任务，再执行全部重试。")
    path = _safe_job_path(job_id)
    if not path.exists():
        raise FileNotFoundError(f"任务不存在：{job_id}")
    status = load_status(job_id, include_worker=False)
    _resolve_job_input_source(
        path,
        str(status.get("input") or status.get("source_path") or ""),
        on_log=lambda message: append_log(path, message),
    )

    imported_audio = (path / IMPORTED_AUDIO_MANIFEST).exists()
    for name in ("images", "_prefetch_images", "cover", "characters", "_clips"):
        target = path / name
        if target.exists():
            shutil.rmtree(target)

    audio_dir = path / "audio"
    if audio_dir.exists():
        if imported_audio:
            for child in audio_dir.iterdir():
                if child.name != IMPORTED_AUDIO_FILENAME:
                    if child.is_dir():
                        shutil.rmtree(child)
                    else:
                        child.unlink(missing_ok=True)
        else:
            shutil.rmtree(audio_dir)

    generated_files = (
        SETTINGS_SNAPSHOT_FILE,
        IMAGE_FAILURE_DECISION_FILE,
        IMAGE_SELECTION_FILE,
        TTS_REDO_REUSE_IMAGES_FILE,
        "novel.json", "metadata.json", "marketing_candidates.json",
        "タイトル・あらすじ候補.txt", "タイトル・あらすじ・タグ候補.txt",
        "upload_title_selection.json", "performance.json", "result.json", "upload_result.json",
        ACCELERATION_PREFETCH_REPORT,
        "text_rewritten.txt", "text_rewrite_report.json", "text_tts_ready.txt",
        "segments.json", "durations.json", "plans.json", "plans_prefetch.json", "prompts.json",
        "story_visual_context.json", "character_profiles.json", "character_reference_manifest.json",
        "tts_auto_pronunciation_dictionary.txt", "tts_auto_pronunciation_report.json",
        "audio_full.mp3", "imported_audio_timing.json", "compose_manifest.json",
        "subtitle.ass", "subtitle.srt",
    )
    for name in generated_files:
        (path / name).unlink(missing_ok=True)
    # Preserve the previous successful video until the replacement compose has
    # passed validation. Only stale temporary output is safe to remove now.
    for video_path in {path / "final.mp4", video_output_path(path)}:
        video_path.with_name(video_path.stem + ".tmp" + video_path.suffix).unlink(missing_ok=True)

    write_status(
        path,
        job_id=job_id,
        stage="queued",
        progress=0.0,
        worker_pid=None,
        error="",
        cover="",
        youtube_url="",
        image_failure=None,
    )
    append_log(path, "full retry reset: generated stages cleared; previous final video preserved until replacement succeeds")
    return {"job_id": job_id, "imported_audio_preserved": imported_audio}


def start_worker(
    input_text: str,
    job_id: str | None = None,
    resume: bool = False,
    compose_only: bool = False,
    marketing_cover_only: bool = False,
    preprocess_only: bool = False,
    bypass_capacity: bool = False,
) -> tuple[str, int]:
    """Start a detached worker, or durably queue it when capacity is full.

    A queued result returns ``(job_id, 0)``. The requested launch mode is kept
    in status.json so ``start_next_queued_job`` can dispatch it unchanged.
    """
    job_id = job_id or new_job_id("pipeline")
    job_dir = job_dir_for(job_id)
    if job_dir.exists() and not resume:
        raise FileExistsError(f"job directory already exists: {job_dir}")

    job_dir.mkdir(parents=True, exist_ok=True)
    input_text = _resolve_job_input_source(
        job_dir,
        input_text,
        on_log=lambda message: append_log(job_dir, message),
    )

    # Freeze settings before either launching or queueing. A later GUI profile
    # switch must not change the queued job's prompts, routes, or TTS settings.
    settings_snapshot = job_dir / SETTINGS_SNAPSHOT_FILE
    if not settings_snapshot.exists():
        _write_settings_snapshot(job_dir, config.as_dict())

    max_jobs = max(1, int(config.get("max_concurrent_jobs", 2)))
    running = count_running_workers()
    if running >= max_jobs and not bypass_capacity:
        write_status(
            job_dir,
            job_id=job_id,
            input=input_text,
            stage="queued",
            worker_pid=None,
            error="",
            queued_compose_only=bool(compose_only),
            queued_marketing_cover_only=bool(marketing_cover_only),
            queued_preprocess_only=bool(preprocess_only),
        )
        append_log(job_dir, f"worker queued automatically: capacity {running}/{max_jobs}")
        return job_id, 0

    command = [
        sys.executable,
        "-B",
        "-m",
        "app.worker",
        "--job-id",
        job_id,
        "--input",
        input_text,
    ]
    if resume:
        command.append("--resume")
    if compose_only:
        command.append("--compose-only")
    if marketing_cover_only:
        command.append("--marketing-cover-only")
    if preprocess_only:
        command.append("--preprocess-only")

    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"
    env["NOVEL_VIDEO_CONFIG_FILE"] = str(settings_snapshot)
    for key in API_KEY_FIELDS:
        env[api_key_env_name(key)] = clean_api_key(config.get(key, ""))
    env.setdefault("NO_PROXY", "localhost,127.0.0.1,::1")
    env.setdefault("no_proxy", "localhost,127.0.0.1,::1")

    kwargs = {}
    if os.name == "nt":
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        if bool(config.worker_detached):
            flags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        kwargs["creationflags"] = flags
    elif bool(config.worker_detached):
        kwargs["start_new_session"] = True

    worker_log = job_dir / "worker.log"
    append_log(job_dir, f"configuration snapshot: {settings_snapshot.name} (profile={config.get('active_profile', '')})")
    append_log(job_dir, f"dispatch worker: {' '.join(command)}")
    write_status(
        job_dir,
        job_id=job_id,
        input=input_text,
        stage="queued",
        progress=0.0,
        worker_pid=None,
        queued_compose_only=False,
        queued_marketing_cover_only=False,
        queued_preprocess_only=False,
        acceleration_preprocess=bool(preprocess_only),
    )
    with worker_log.open("a", encoding="utf-8", buffering=1) as log_handle:
        process = subprocess.Popen(
            command,
            cwd=str(ROOT),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            close_fds=False,
            **kwargs,
        )
    record_worker_pid(job_id, process.pid)
    write_status(job_dir, job_id=job_id, input=input_text, stage="queued", progress=0.0, worker_pid=process.pid)
    append_log(job_dir, f"worker pid={process.pid}")
    return job_id, int(process.pid)


def _write_settings_snapshot(job_dir: Path, settings: dict) -> Path:
    """Store non-secret settings once, so a queued job is profile-pinned."""
    snapshot_settings = dict(settings)
    for key in API_KEY_FIELDS:
        snapshot_settings.pop(key, None)
    path = job_dir / SETTINGS_SNAPSHOT_FILE
    temp_path = job_dir / f".{SETTINGS_SNAPSHOT_FILE}.{os.getpid()}.tmp"
    temp_path.write_text(json.dumps(snapshot_settings, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp_path, path)
    return path


def apply_profile_to_jobs(job_ids: Iterable[str], profile_name: str) -> tuple[str, int]:
    """Pin a saved configuration profile to queued tasks without switching the UI."""
    cleaned, settings = config.profile_settings(profile_name)
    targets = [str(job_id) for job_id in job_ids]
    for job_id in targets:
        status = load_status(job_id)
        if status.get("worker_alive"):
            raise RuntimeError(f"任务正在运行，不能套用方案：{job_id}")
        if not job_dir_for(job_id).exists():
            raise FileNotFoundError(f"任务不存在：{job_id}")
    for job_id in targets:
        job_dir = job_dir_for(job_id)
        if not job_dir.exists():
            raise FileNotFoundError(f"任务不存在：{job_id}")
        _write_settings_snapshot(job_dir, settings)
        write_status(job_dir, assigned_profile=cleaned)
        append_log(job_dir, f"assigned configuration profile: {cleaned}")
    return cleaned, len(targets)


def start_next_queued_job(*, exclude_job_id: str | None = None, on_log: LogFn = _noop) -> tuple[str, int] | None:
    """Start the next waiting job when worker capacity is available."""
    max_jobs = max(1, int(config.get("max_concurrent_jobs", 2)))
    if count_running_workers() >= max_jobs:
        return None
    rows = list_jobs(limit=500)
    rows.sort(key=lambda row: 0 if str(row.get("stage") or "") == "preprocessed" else 1)
    for row in rows:
        job_id = str(row.get("job_id") or "")
        if not job_id or job_id == exclude_job_id:
            continue
        st = load_status(job_id)
        if st.get("worker_alive") or st.get("stage") not in {"queued", "stopped", "preprocessed"}:
            continue
        input_text = str(st.get("input") or "")
        if not input_text:
            continue
        try:
            started = start_worker(
                input_text,
                job_id=job_id,
                resume=True,
                compose_only=bool(st.get("queued_compose_only", False)),
                marketing_cover_only=bool(st.get("queued_marketing_cover_only", False)),
                preprocess_only=bool(st.get("queued_preprocess_only", False)),
            )
            if started[1] <= 0:
                return None
            on_log(f"  auto-started waiting job {job_id} pid={started[1]}")
            return started
        except RuntimeError as exc:
            safe_error = str(redact_secret_text(exc))
            write_status(job_dir_for(job_id), stage="failed", worker_pid=None, error=safe_error)
            append_log(job_dir_for(job_id), f"auto-start preflight blocked: {safe_error}")
            on_log(f"  WARN waiting job {job_id} blocked before start: {safe_error}")
            continue
        except Exception as exc:
            safe_error = str(redact_secret_text(exc))
            write_status(job_dir_for(job_id), stage="failed", worker_pid=None, error=safe_error)
            append_log(job_dir_for(job_id), f"auto-start failed: {safe_error}")
            on_log(f"  WARN auto-start waiting job {job_id} failed: {safe_error}")
            continue
    return None


def queue_all_pending_jobs(*, on_log: LogFn = _noop) -> tuple[list[str], list[tuple[str, int]]]:
    """Queue every restartable pending job and fill the available worker slots.

    This is deliberately separate from ``start_worker``: a batch request must
    remain useful when its size is greater than ``max_concurrent_jobs``.  Jobs
    left in ``queued`` are picked up by each finishing worker.
    """
    queued: list[str] = []
    for row in list_jobs(limit=500):
        job_id = str(row.get("job_id") or "")
        if not job_id or row.get("worker_alive"):
            continue
        status = load_status(job_id)
        if status.get("stage") not in {"queued", "failed"}:
            continue
        if not str(status.get("input") or "").strip():
            continue
        # A failed job is a resumable job in a batch.  Convert it to the same
        # durable queue state as a newly created job so a later worker can
        # start it after capacity becomes available.
        if status.get("stage") != "queued":
            write_status(job_dir_for(job_id), stage="queued", worker_pid=None, error="")
            append_log(job_dir_for(job_id), "queued by 'start all pending'")
        queued.append(job_id)

    started: list[tuple[str, int]] = []
    max_jobs = max(1, int(config.get("max_concurrent_jobs", 2)))
    while count_running_workers() < max_jobs:
        next_job = start_next_queued_job(on_log=on_log)
        if next_job is None:
            break
        started.append(next_job)
    return queued, started


def _build_scraper(site: str):
    if site in {"source_catalog", "catalog", "novel_catalog"}:
        return SourceCatalogScraper()
    if site == "kakuyomu":
        return KakuyomuScraper()
    if site == "syosetu":
        return SyosetuScraper()
    if site in {"qingtian", "fanqie", "aggregate"}:
        return QingtianAggregateScraper(
            base_url=config.source_base_url,
            source=config.source_platform,
            media=config.source_media,
            hosts=parse_host_list(config.source_hosts),
            delay=float(config.source_delay),
        )
    if site in {"legado", "legado_static"}:
        return LegadoStaticScraper(config.legado_source_ref)
    raise ValueError(f"暂不支持 site={site}")


def search_books(
    keyword: str,
    site: str | None = None,
    source: str | None = None,
    media: str | None = None,
    limit: int = 20,
    enrich_latest: bool = False,
) -> list[BookSearchResult]:
    site = site or config.scraper_site
    sc = _build_scraper(site)
    try:
        if isinstance(sc, SourceCatalogScraper):
            return sc.search(keyword, limit=limit, source=source, media=media, enrich_latest=enrich_latest)
        if isinstance(sc, QingtianAggregateScraper):
            return sc.search(keyword, limit=limit, source=source, media=media, enrich_latest=enrich_latest)
        if isinstance(sc, LegadoStaticScraper):
            return sc.search(keyword, limit=limit)
        raise ValueError(f"{site} 暂不支持关键词搜索，请直接输入 URL/ID")
    finally:
        sc.close()


def _probable_local_path(value: str) -> bool:
    """Recognize a local path even after its file has been moved."""
    text = str(value or "").strip().strip('"')
    if not text or "\n" in text:
        return False
    lowered = text.lower()
    if re.match(r"^[a-z][a-z0-9+.-]*://", lowered) or lowered.startswith("data:;base64,"):
        return False
    if lowered.startswith(("http:", "https:", "qingtian:", "legado:", "syosetu:")):
        return False
    if re.match(r"^[a-zA-Z]:[\\/]", text):
        return True
    if text.startswith(("/", "~/", "./", "../")):
        return True
    if "/" in text or "\\" in text:
        return True
    return Path(text).suffix.lower() in {".txt", ".text", ".md", ".markdown"}


def _source_file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _source_snapshot_path(job_dir: Path, source_name: str) -> Path:
    safe_name = safe_job_name(Path(source_name).name, fallback="source.txt")
    if not Path(safe_name).suffix:
        safe_name += ".txt"
    return job_dir / SOURCE_INPUT_SNAPSHOT_DIR / safe_name


def _snapshot_local_source(job_dir: Path, source: Path) -> Path:
    """Keep a durable copy so moving the operator's folder cannot corrupt retry."""
    source = source.expanduser().resolve()
    snapshot = _source_snapshot_path(job_dir, source.name)
    snapshot.parent.mkdir(parents=True, exist_ok=True)
    if source != snapshot.resolve(strict=False):
        temporary = snapshot.with_name(f".{snapshot.name}.{os.getpid()}.tmp")
        temporary.unlink(missing_ok=True)
        try:
            shutil.copy2(source, temporary)
            os.replace(temporary, snapshot)
        finally:
            temporary.unlink(missing_ok=True)
    write_status(
        job_dir,
        source_kind="local_text",
        source_path=str(source),
        source_directory=str(source.parent),
        source_name=source.name,
        source_sha256=_source_file_hash(source),
        source_snapshot_path=str(snapshot),
        source_safety_blocked=False,
        source_safety_reason="",
    )
    return snapshot


def _find_relocated_local_source(status: dict, original: Path, *, exclude_root: Path | None = None) -> Path | None:
    """Find a moved source only when its exact filename has one safe match."""
    name = str(status.get("source_name") or original.name or "").strip()
    if not name:
        return None
    roots: list[Path] = []
    configured_root = str(config.get("task_category_root", "") or "").strip()
    if configured_root:
        roots.append(Path(configured_root).expanduser())
    parents = list(original.expanduser().parents)
    roots.extend(parents[1:4])

    candidates: list[Path] = []
    seen_roots: set[str] = set()
    seen_candidates: set[str] = set()
    for root in roots:
        try:
            resolved_root = root.resolve()
        except OSError:
            continue
        if resolved_root == Path(resolved_root.anchor):
            continue
        root_key = str(resolved_root)
        if root_key in seen_roots or not resolved_root.is_dir():
            continue
        seen_roots.add(root_key)
        try:
            for match in resolved_root.rglob(name):
                if not match.is_file():
                    continue
                if exclude_root is not None:
                    try:
                        match.resolve().relative_to(exclude_root.resolve())
                        continue
                    except (OSError, ValueError):
                        pass
                match_key = str(match.resolve())
                if match_key not in seen_candidates:
                    seen_candidates.add(match_key)
                    candidates.append(match)
                if len(candidates) > 20:
                    break
        except OSError:
            continue
        if len(candidates) > 20:
            break

    expected_hash = str(status.get("source_sha256") or "").strip()
    if expected_hash:
        verified: list[Path] = []
        for candidate in candidates:
            try:
                if _source_file_hash(candidate) == expected_hash:
                    verified.append(candidate)
            except OSError:
                continue
        candidates = verified
    return candidates[0] if len(candidates) == 1 else None


def _resolve_job_input_source(job_dir: Path, input_text: str, on_log: LogFn = _noop) -> str:
    """Resolve a task input before any scraper, paid API, or TTS work."""
    status = _read_json(job_dir / "status.json", {})
    source_text = str(status.get("source_path") or input_text or "").strip()
    expected_local = bool(
        str(status.get("source_kind") or "").strip() == "local_text"
        or str(status.get("source_path") or "").strip()
        or _probable_local_path(source_text)
    )
    if not expected_local:
        return str(input_text or "")

    original = Path(source_text).expanduser()
    if original.is_file():
        resolved = original.resolve()
        _snapshot_local_source(job_dir, resolved)
        write_status(job_dir, input=str(resolved))
        return str(resolved)

    relocated = _find_relocated_local_source(status, original, exclude_root=job_dir)
    if relocated is not None:
        resolved = relocated.resolve()
        _snapshot_local_source(job_dir, resolved)
        write_status(job_dir, input=str(resolved), source_relocated_from=str(original))
        on_log(f"  本地原文已移动，已按唯一匹配安全重连：{resolved}")
        return str(resolved)

    snapshot_text = str(status.get("source_snapshot_path") or "").strip()
    snapshot = Path(snapshot_text).expanduser() if snapshot_text else _source_snapshot_path(job_dir, original.name)
    if snapshot.is_file():
        expected_hash = str(status.get("source_sha256") or "").strip()
        if expected_hash and _source_file_hash(snapshot) != expected_hash:
            raise RuntimeError("任务的本地原文安全副本校验失败，已停止，未调用任何 API 或 TTS。")
        on_log(f"  原文件已移动或删除，改用任务内保存的原文安全副本：{snapshot}")
        write_status(job_dir, source_kind="local_text", source_snapshot_path=str(snapshot))
        return str(snapshot)

    raise FileNotFoundError(
        "本地原文文件不存在，已在抓取、洗稿、图片 API 和配音之前停止。"
        f"\n原路径：{original}\n请重新导入或把原文移回可访问位置；程序不会把本地路径交给网络抓取器。"
    )


def _reject_obvious_non_story_payload(text: str) -> None:
    """Block known access-limit and promotion payloads before they consume APIs."""
    value = "".join(
        char
        for char in unicodedata.normalize("NFKC", str(text or ""))
        if unicodedata.category(char) != "Cf"
    ).lower()
    markers = (
        "您当前未登录，今日已访问",
        "晴天提醒您",
        "开通永久svip",
        "qd书源",
        "会员限时折扣中",
        "qingtian618_novel",
    )
    if sum(value.count(marker) for marker in markers) >= 3:
        raise RuntimeError(
            "正文安全检查失败：内容中重复出现登录限制、会员广告或书源推广，"
            "疑似抓取了提示页而不是小说正文。已停止，未进入洗稿、图片 API 或 TTS。"
        )


def _validate_job_novel_source(job_dir: Path, novel: Novel, segments: list[Segment] | None = None) -> None:
    """Ensure a saved/resumed novel still belongs to the task's input type."""
    status = _read_json(job_dir / "status.json", {})
    expected_local = bool(
        str(status.get("source_kind") or "") == "local_text"
        or str(status.get("source_path") or "").strip()
    )
    if expected_local and str(novel.site or "").lower() != "text":
        raise RuntimeError(
            "正文来源不一致：这个任务是本地 TXT，但缓存正文来自网络抓取。"
            "已阻止继续调用 API 和配音；请重新从本地原文开始重试。"
        )
    _reject_obvious_non_story_payload(novel.full_text)
    if segments is not None:
        _reject_obvious_non_story_payload("\n".join(segment.text for segment in segments))


def _looks_like_text_source(value: str, site: str) -> bool:
    text = str(value or "")
    if site in {"text", "local_text", "manual"}:
        return True
    stripped = text.strip()
    lowered = stripped.lower()
    if re.match(r"^[a-z][a-z0-9+.-]*://", lowered) or lowered.startswith("data:;base64,"):
        return False
    if lowered.startswith(("http:", "https:", "qingtian:", "legado:", "syosetu:")):
        return False
    if stripped.startswith("{") and ("book_id" in stripped or "bookid" in stripped):
        return False
    if "\n" in text or len(text) > 500:
        return True
    try:
        return Path(text.strip().strip('"')).exists()
    except OSError:
        return False


def _infer_scraper_site(value: str, site: str) -> str:
    text = str(value or "").strip().lower()
    if text.startswith("qingtian://"):
        return "qingtian"
    if text.startswith(("http://ncode.syosetu.com/", "https://ncode.syosetu.com/")):
        return "syosetu"
    if text.startswith(("http://kakuyomu.jp/works/", "https://kakuyomu.jp/works/")):
        return "kakuyomu"
    if re.match(r"^n[0-9a-z]+$", text):
        return "syosetu"
    return site


def _novel_from_text_source(value: str, max_chars: int = 0) -> Novel:
    raw = str(value or "")
    source = raw.strip().strip('"')
    path: Path | None = None
    try:
        candidate = Path(source)
        if candidate.exists() and candidate.is_file():
            path = candidate
    except OSError:
        path = None
    if path is not None:
        text = path.read_text(encoding="utf-8", errors="ignore")
        title = path.stem
        novel_id = str(path)
    else:
        text = raw
        first_line = next((line.strip() for line in text.splitlines() if line.strip()), "")
        title = re.sub(r"[<>:\"/\\|?*\x00-\x1f]", "_", first_line[:60]).strip(" .") or "manual_text"
        novel_id = "manual"
    if max_chars and max_chars > 0:
        text = text[: int(max_chars)]
    text = text.strip()
    return Novel(
        site="text",
        novel_id=novel_id,
        title=title,
        author="",
        description="local text input",
        chapters=[NovelChapter(index=1, title=title, text=text)] if text else [],
    )


def stage_scrape(
    url_or_id: str,
    site: str = "syosetu",
    max_chars: int = 0,
    on_log: LogFn = _noop,
) -> Novel:
    def warn_if_limited(novel: Novel) -> None:
        try:
            limit = int(max_chars or 0)
        except Exception:
            limit = 0
        if limit > 0 and len(novel.full_text) >= limit:
            on_log(f"  WARN text reached scraper_max_chars={limit}; set it to 0 to process the full text")

    site = _infer_scraper_site(url_or_id, site)
    if _probable_local_path(url_or_id):
        candidate = Path(str(url_or_id or "").strip().strip('"')).expanduser()
        if not candidate.is_file():
            raise FileNotFoundError(
                f"本地原文文件不存在：{candidate}。已停止，不会把本地路径交给 {site} 网络抓取器。"
            )
    if _looks_like_text_source(url_or_id, site):
        novel = _novel_from_text_source(url_or_id, max_chars=max_chars)
        _reject_obvious_non_story_payload(novel.full_text)
        on_log(f"[1/6] Local text: {novel.title}")
        on_log(f"  OK title={novel.title} chars={len(novel.full_text)}")
        warn_if_limited(novel)
        if not novel.full_text.strip():
            raise RuntimeError("local text is empty")
        return novel
    on_log(f"[1/6] 抓取 {site}: {url_or_id}")
    sc = _build_scraper(site)
    try:
        if isinstance(sc, QingtianAggregateScraper):
            novel = sc.fetch(url_or_id, max_chars=max_chars, chapter_limit=int(config.scraper_chapter_limit))
        else:
            novel = sc.fetch(url_or_id, max_chars=max_chars)
    finally:
        sc.close()
    on_log(f"  OK 标题: {novel.title}  章节数: {len(novel.chapters)}  字数: {len(novel.full_text)}")
    if not novel.chapters or not novel.full_text.strip():
        raise RuntimeError("抓取成功但没有正文内容")
    _reject_obvious_non_story_payload(novel.full_text)
    return novel


def stage_clean(
    novel: Novel,
    on_log: LogFn = _noop,
    job_dir: Path | None = None,
    preserve_source_text: bool = False,
) -> list[Segment]:
    on_log("[2/6] 整理/洗稿 + 切片")
    if preserve_source_text:
        # An imported narration was generated from this exact script.  Removing
        # headings or rewriting the prose would make subtitles and images refer
        # to words that are not present in the supplied audio.
        text = str(novel.full_text or "").strip()
        on_log("  导入音频模式：保留原文，跳过标题清理和 AI 洗稿")
    else:
        text = _strip_story_scaffold(novel.full_text, novel, on_log=on_log, job_dir=job_dir)
        text = _rewrite_story_text(text, on_log=on_log, job_dir=job_dir)
        if bool(config.get("tts_clean_rewritten_text", True)):
            text = _clean_rewritten_narration_text(text, on_log=on_log, job_dir=job_dir)
        _generate_auto_pronunciation_dictionary(text, on_log=on_log, job_dir=job_dir)
    segs = split_segments(text)
    on_log(f"  OK 切成 {len(segs)} 段")
    if not segs:
        raise RuntimeError("正文切片结果为空")
    return segs


_CHAPTER_HEADING_RE = re.compile(
    r"^\s*(?:"
    r"第[\d零〇一二三四五六七八九十百千万两]+[章节章回话話卷集部篇].{0,40}|"
    r"chapter\s*\d+.{0,40}|"
    r"(?:序章|楔子|引子|尾声|番外).{0,30}"
    r")\s*$",
    re.I,
)
_INTRO_HEADING_RE = re.compile(r"^\s*(?:简介|内容简介|小说简介|作品简介|书籍简介|文案|作者|作者名|书名|小说名)\s*[:：]", re.I)


def _strip_story_scaffold(text: str, novel: Novel, on_log: LogFn = _noop, job_dir: Path | None = None) -> str:
    source = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    if not source.strip():
        return source
    title_values = {
        re.sub(r"\s+", "", str(value or ""))
        for value in (novel.title, _clean_display_title(novel.title))
        if str(value or "").strip()
    }
    author_value = re.sub(r"\s+", "", str(novel.author or ""))
    description_value = re.sub(r"\s+", "", str(novel.description or ""))
    cleaned: list[str] = []
    removed_leading = 0
    removed_chapters = 0
    leading_window_open = True
    for raw_line in source.split("\n"):
        stripped = raw_line.strip()
        compact = re.sub(r"\s+", "", stripped)
        if leading_window_open and len(cleaned) < 24:
            remove_leading = False
            if not stripped:
                remove_leading = removed_leading > 0
            elif compact in title_values:
                remove_leading = True
            elif author_value and (compact == author_value or compact in {f"作者:{author_value}", f"作者：{author_value}"}):
                remove_leading = True
            elif description_value and compact and compact in description_value and len(compact) <= 120:
                remove_leading = True
            elif _INTRO_HEADING_RE.search(stripped):
                remove_leading = True
            if remove_leading:
                removed_leading += 1
                continue
            if stripped:
                leading_window_open = False
        if stripped and _CHAPTER_HEADING_RE.match(stripped):
            removed_chapters += 1
            continue
        cleaned.append(raw_line)
    result = "\n".join(cleaned).strip()
    if not result:
        return source
    if removed_leading or removed_chapters:
        if job_dir is not None:
            _write_json(
                job_dir / "story_scaffold_clean_report.json",
                {
                    "removed_leading_lines": removed_leading,
                    "removed_chapter_headings": removed_chapters,
                    "chars_before": len(source),
                    "chars_after": len(result),
                },
            )
        on_log(f"  story scaffold clean: removed {removed_leading} leading lines and {removed_chapters} chapter headings")
    return result


def _rewrite_story_text(text: str, on_log: LogFn = _noop, job_dir: Path | None = None) -> str:
    source = str(text or "")
    if not bool(config.get("ai_rewrite_enabled", False)):
        return source
    if not _can_call_text_llm():
        on_log("  AI 洗稿已开启，但未配置 LLM API Key；已保留原文")
        return source
    paragraphs = _split_rewrite_paragraphs(source)
    if not paragraphs:
        return source

    started_chars = len(source)
    try:
        rewritten = _ai_rewrite_paragraphs(paragraphs, on_log)
    except Exception as exc:
        on_log(f"  WARN AI 洗稿失败，已保留原文: {exc}")
        return source
    if not rewritten.strip():
        on_log("  WARN AI 洗稿结果为空，已保留原文")
        return source
    report = {
        "enabled": True,
        "mode": "rewrite",
        "paragraphs": len(paragraphs),
        "chars_before": started_chars,
        "chars_after": len(rewritten),
        "batch_chars": int(config.get("ai_rewrite_batch_chars", 3500) or 3500),
        "prompt_hash": _text_hash(str(config.get("ai_rewrite_prompt", "") or "")),
    }
    if job_dir is not None:
        _write_json(job_dir / "text_rewrite_report.json", report)
        (job_dir / "text_rewritten.txt").write_text(rewritten, encoding="utf-8")
    on_log(f"  AI 洗稿改写完成：段落 {len(paragraphs)}，字数 {started_chars} -> {len(rewritten)}")
    return rewritten


_NARRATION_URL_RE = re.compile(r"(?:https?://|www\.)[^\s<>]+", re.I)
_NARRATION_DECORATION_RE = re.compile(r"[《》〈〉「」『』“”‘’\"'（）()［］\[\]【】〔〕{}｛｝]")
_NARRATION_EMOJI_RE = re.compile(r"[\U0001F000-\U0001FAFF\U00002700-\U000027BF\U0000FE0F]")


def _clean_rewritten_narration_text(text: str, on_log: LogFn = _noop, job_dir: Path | None = None) -> str:
    """Apply provider-specific cleanup without flattening VOICEVOX punctuation."""
    source = str(text or "")
    provider = str(config.tts_provider or "").strip().lower()
    if provider == "voicevox":
        value = _clean_voicevox_narration_text(source)
    else:
        value = _clean_edge_style_narration_text(source)
    value = re.sub(r"[ \t]+\n", "\n", value)
    value = re.sub(r"\n{3,}", "\n\n", value).strip()
    if job_dir is not None:
        (job_dir / "text_tts_ready.txt").write_text(value, encoding="utf-8")
    removed = len(source) - len(value)
    if value != source:
        on_log(f"  朗读净化完成（{provider or 'default'}）：净减少 {max(0, removed)} 个技术/装饰字符")
    return value


def _remove_format_control_characters(text: str) -> str:
    return "".join(char for char in str(text or "") if unicodedata.category(char) != "Cf")


def _clean_voicevox_narration_text(text: str) -> str:
    """Preserve authored Japanese punctuation; remove only proven TTS hazards."""
    value = _remove_format_control_characters(text)
    value = _NARRATION_URL_RE.sub("", value)
    value = _NARRATION_EMOJI_RE.sub("", value)
    # VOICEVOX understands authored Japanese punctuation well. Quotes,
    # ellipses, dashes, commas, periods, question marks, exclamation marks,
    # wave marks, and original line endings all remain untouched.
    value = re.sub(r"(?m)^[ \t]*[-*＊•●◆◇▪▫]+[ \t]+", "", value)
    return value


def _clean_edge_style_narration_text(text: str) -> str:
    """Normalize punctuation more aggressively for Edge-style narrators."""
    value = _remove_format_control_characters(text)
    value = _NARRATION_URL_RE.sub("", value)
    value = _NARRATION_EMOJI_RE.sub("", value)
    value = re.sub(r"[#$＃＊*•●◆◇▪▫]+", "", value)
    value = _NARRATION_DECORATION_RE.sub("", value)
    value = re.sub(r"(?:\.{2,}|…+)", "。", value)
    value = re.sub(r"(?:[—―－〜～]+|-{2,})", "，", value)
    value = re.sub(r"[，,]{2,}", "，", value)
    value = re.sub(r"[。]{2,}", "。", value)
    return value


_JP_RISK_TOKEN_RE = re.compile(r"[\u3400-\u9fff々〆ヶァ-ヴー]{2,}")
_JP_KANJI_RE = re.compile(r"[\u3400-\u9fff々〆ヶ]")
_JP_KATAKANA_RE = re.compile(r"[ァ-ヴー]")
_JP_READING_RE = re.compile(r"^[ぁ-ゖァ-ヺー・]+$")


def _high_risk_japanese_terms(text: str, limit: int) -> list[str]:
    """Return conservative, deterministic candidates; ordinary short words stay out."""
    terms: list[str] = []
    seen: set[str] = set()
    for match in _JP_RISK_TOKEN_RE.finditer(str(text or "")):
        term = match.group(0)
        kanji = len(_JP_KANJI_RE.findall(term))
        katakana = len(_JP_KATAKANA_RE.findall(term))
        if not (kanji >= 3 or (kanji >= 1 and katakana >= 2)):
            continue
        if term not in seen:
            seen.add(term)
            terms.append(term)
        if len(terms) >= limit:
            break
    return terms


def _generate_auto_pronunciation_dictionary(text: str, on_log: LogFn = _noop, job_dir: Path | None = None) -> None:
    """Optionally ask the text LLM for readings of a small, pre-filtered term list."""
    if job_dir is None:
        return
    auto_path = job_dir / TTS_AUTO_PRONUNCIATION_DICTIONARY
    report_path = job_dir / "tts_auto_pronunciation_report.json"
    auto_path.unlink(missing_ok=True)
    report_path.unlink(missing_ok=True)
    if not bool(config.get("ai_rewrite_enabled", False)):
        return
    if not bool(config.get("tts_auto_pronunciation_enabled", False)):
        return
    if config.tts_provider != "edge":
        on_log("  自动读音词典仅适用于 Edge TTS，已跳过")
        return
    if not _can_call_pronunciation_dictionary_llm():
        on_log("  自动读音词典已开启，但未配置文本 API；已跳过")
        return
    try:
        max_terms = max(1, min(100, int(config.get("tts_auto_pronunciation_max_terms", 40) or 40)))
    except Exception:
        max_terms = 40
    candidates = _high_risk_japanese_terms(text, max_terms)
    if not candidates:
        on_log("  自动读音词典：未发现高风险日语词")
        return
    system = str(config.get("pronunciation_dictionary_prompt", "") or "").strip()
    if not system:
        system = (
            "你是日语 TTS 生词词典助手。只为用户给出的候选词提供准确的日语假名读音。"
            "只输出严格的“原词=假名读音”，每行一条；不得输出解释、标题或 Markdown，"
            "不得添加候选列表以外的词，不确定时直接省略。"
            "读音只能使用平假名、片假名、中点或长音符。"
        )
    route = _pronunciation_dictionary_route_settings()
    llm = LLMBackend(
        provider=route["provider"], base_url=route["base_url"], api_key=route["api_key"], model=route["model"],
        system_prompt=system, temperature=0.0, max_tokens=max(200, min(1200, len(candidates) * 24)), timeout=90.0,
    )
    try:
        with external_api_slot(action="auto pronunciation"):
            reply = llm.complete(system, "候选词：\n" + "\n".join(candidates), temperature=0.0)
    except Exception as exc:
        on_log(f"  WARN 自动读音词典生成失败，已保留 Edge 原文朗读: {exc}")
        return
    candidate_set = set(candidates)
    entries: list[tuple[str, str]] = []
    seen: set[str] = set()
    for line in _strip_llm_fences(reply).splitlines():
        if "=" not in line:
            continue
        written, reading = (part.strip() for part in line.split("=", 1))
        if written in candidate_set and written not in seen and _JP_READING_RE.fullmatch(reading or ""):
            seen.add(written)
            entries.append((written, reading))
    _write_json(
        report_path,
        {
            "candidates": candidates,
            "entries": [{"written": w, "reading": r} for w, r in entries],
            "model": route["model"],
            "dedicated_api": bool(config.get("pronunciation_dictionary_dedicated_api_enabled", False)),
        },
    )
    if not entries:
        on_log(f"  自动读音词典：API 未返回可用条目（候选 {len(candidates)} 个）")
        return
    auto_path.write_text("\n".join(f"{written}={reading}" for written, reading in entries) + "\n", encoding="utf-8")
    on_log(f"  自动读音词典：候选 {len(candidates)} 个，采用 {len(entries)} 条")


def _split_rewrite_paragraphs(text: str) -> list[str]:
    normalized = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    raw = re.split(r"\n\s*\n", normalized)
    paragraphs = []
    for para in raw:
        para = re.sub(r"[ \t]+\n", "\n", para).strip()
        if para:
            paragraphs.append(para)
    return paragraphs


SERIES_BIBLE_FILENAME = ".novel_video_series_bible.json"


def series_animation_enabled_for_job(job_dir: Path | None) -> bool:
    if not bool(config.get("series_animation_enabled", True)):
        return False
    if job_dir is None:
        return True
    status = _read_json(job_dir / "status.json", {})
    return str(status.get("series_animation_mode") or "auto").strip().lower() != "single"


def _apply_series_title_to_novel(novel: Novel, job_dir: Path, on_log: LogFn) -> None:
    """Make cover/upload titles visibly related while preserving source files."""
    status = _read_json(job_dir / "status.json", {})
    source_path = Path(str(status.get("source_path") or status.get("input") or "")).expanduser()
    project_id = str(status.get("project_id") or "").strip()
    project = projects.load_project(project_id) if project_id else {}
    series_settings = project.get("series_video_settings") if isinstance(project, dict) else {}
    bible = _read_json(source_path.parent / SERIES_BIBLE_FILENAME, {}) if source_path.is_file() else {}
    series_title = str(
        (series_settings.get("shared_novel_title") if isinstance(series_settings, dict) else "")
        or project.get("name")
        or (bible.get("series_title") if isinstance(bible, dict) else "")
        or status.get("series_title")
        or (source_path.parent.name if source_path.is_file() else "")
    ).strip()
    if not series_title:
        return
    episode = int(status.get("project_episode") or status.get("series_episode") or 0)
    if not episode and source_path.is_file():
        _base, episode = projects.infer_episode(source_path)
    original_title = novel.title
    novel.title = f"{series_title}｜第{episode}集" if episode else series_title
    write_status(job_dir, source_title=original_title, series_title=series_title, series_episode=episode)
    on_log(f"  系列标题：{novel.title}")


def register_imported_series_job(job_id: str, source_value: str | Path) -> dict:
    """Detect split-series membership immediately when a local TXT is imported."""
    source = Path(str(source_value)).expanduser()
    if not source.is_file():
        return {}
    match = re.search(r"(?:[_-]|第)0*(\d{1,3})$", source.stem)
    if not match:
        return {}
    episode = int(match.group(1))
    base_stem = source.stem[: match.start()].rstrip(" _-")
    siblings: list[tuple[int, Path]] = []
    for candidate in source.parent.glob(f"*{source.suffix}"):
        sibling_match = re.search(r"(?:[_-]|第)0*(\d{1,3})$", candidate.stem)
        if not sibling_match:
            continue
        sibling_base = candidate.stem[: sibling_match.start()].rstrip(" _-")
        if unicodedata.normalize("NFKC", sibling_base) != unicodedata.normalize("NFKC", base_stem):
            continue
        siblings.append((int(sibling_match.group(1)), candidate))
    if len(siblings) < 2:
        return {}
    total = max(value for value, _path in siblings)
    series_title = source.parent.name.strip() or base_stem
    payload = {
        "series_title": series_title,
        "series_episode": episode,
        "series_total": total,
        "series_group_key": _stable_hash({
            "directory": str(source.parent.resolve()),
            "base": unicodedata.normalize("NFKC", base_stem),
        })[:20],
    }
    write_status(job_dir_for(job_id), **payload)
    append_log(
        job_dir_for(job_id),
        f"series detected at import: {series_title} episode={episode}/{total}",
    )
    return payload


def create_novel_project(
    name: str,
    *,
    aliases: Iterable[str] = (),
    source_directories: Iterable[str | Path] = (),
    series_video_settings: dict | None = None,
) -> dict:
    return projects.create_project(
        name,
        aliases=aliases,
        source_directories=source_directories,
        series_video_settings=series_video_settings,
    )


def list_novel_projects() -> list[dict]:
    return projects.list_projects()


def load_novel_project(project_id: str) -> dict:
    return projects.load_project(project_id)


def update_novel_project_series_settings(project_id: str, updates: dict) -> dict:
    project = projects.update_series_video_settings(project_id, updates)
    settings = project.get("series_video_settings") or {}
    shared_title = str(settings.get("shared_novel_title") or project.get("name") or "")
    for job_id in project.get("jobs") or []:
        job_dir = job_dir_for(str(job_id))
        if job_dir.is_dir():
            write_status(
                job_dir,
                project_name=str(project.get("name") or ""),
                series_title=shared_title,
                shared_novel_title=shared_title,
                shared_novel_title_locked=bool(settings.get("shared_novel_title_locked", True)),
            )
    return project


def series_video_settings_for_job(job_dir: Path) -> dict:
    status = _read_json(job_dir / "status.json", {})
    project_id = str(status.get("project_id") or "") if isinstance(status, dict) else ""
    project = projects.load_project(project_id) if project_id else {}
    return dict(project.get("series_video_settings") or {}) if project else {}


def load_project_character_profiles(project_id: str) -> dict:
    return projects.load_character_profiles(project_id)


def set_project_character_record_status(project_id: str, identity: str, status: str) -> dict:
    return projects.set_character_record_status(project_id, identity, status)


def novel_project_dir(project_id: str) -> Path:
    return projects.project_dir(project_id)


def rename_novel_project(project_id: str, name: str) -> dict:
    project = projects.rename_project(project_id, name)
    settings = project.get("series_video_settings") or {}
    shared_title = str(settings.get("shared_novel_title") or project["name"])
    for job_id in project.get("jobs") or []:
        job_dir = job_dir_for(str(job_id))
        if job_dir.is_dir():
            write_status(
                job_dir,
                project_name=project["name"],
                series_title=shared_title,
                shared_novel_title=shared_title,
            )
    return project


def archive_novel_project(project_id: str) -> Path:
    project = projects.load_project(project_id)
    if not project:
        raise FileNotFoundError(f"项目不存在：{project_id}")
    for job_id in project.get("jobs") or []:
        job_dir = job_dir_for(str(job_id))
        if not job_dir.is_dir():
            continue
        status = _read_json(job_dir / "status.json", {})
        if str(status.get("project_id") or "") != project_id:
            continue
        write_status(
            job_dir,
            project_id="",
            project_name="",
            project_episode=0,
            project_mode="single",
            series_title="",
            series_episode=0,
            series_total=0,
            series_group_key="",
            series_animation_mode="single",
        )
        append_log(job_dir, f"novel project archived: {project.get('name') or project_id}")
    return projects.archive_project(project_id)


def detect_import_project_groups(paths: Iterable[str | Path]) -> list[dict]:
    return projects.detect_import_groups(paths)


def infer_project_episode(value: str | Path) -> int:
    return projects.infer_episode(value)[1]


def assign_job_to_project(
    job_id: str,
    project_id: str,
    *,
    episode: int = 0,
    source_path: str = "",
) -> dict:
    project = projects.add_job(project_id, job_id, source_path=source_path)
    series_settings = project.get("series_video_settings") or {}
    shared_title = str(
        series_settings.get("shared_novel_title")
        or project.get("name")
        or ""
    )
    updates = {
        "project_id": project_id,
        "project_name": str(project.get("name") or ""),
        "project_episode": max(0, int(episode or 0)),
        "project_mode": "shared",
        # Keep existing series presentation and scheduling behavior working.
        "series_title": shared_title,
        "series_episode": max(0, int(episode or 0)),
        "series_group_key": project_id,
        "series_animation_mode": "series",
        "shared_novel_title": shared_title,
        "shared_novel_title_locked": bool(
            series_settings.get("shared_novel_title_locked", True)
        ),
    }
    write_status(job_dir_for(job_id), **updates)
    append_log(
        job_dir_for(job_id),
        f"assigned to novel project: {project.get('name') or project_id}"
        + (f" episode={episode}" if episode else ""),
    )
    return project


def remove_job_from_project(job_id: str) -> None:
    status = load_status(job_id, include_worker=False)
    project_id = str(status.get("project_id") or "")
    if project_id:
        projects.remove_job(project_id, job_id)
    write_status(
        job_dir_for(job_id),
        project_id="",
        project_name="",
        project_episode=0,
        project_mode="single",
        series_title="",
        series_episode=0,
        series_total=0,
        series_group_key="",
        series_animation_mode="single",
    )
    append_log(job_dir_for(job_id), "removed from novel project")


def migrate_legacy_series_projects() -> list[dict]:
    return projects.migrate_legacy_series(write_job_status=write_status)


def series_start_choice_info(job_id: str, selected_job_ids: Iterable[str] = ()) -> dict:
    """Describe a partial-series launch that needs an operator choice."""
    job_dir = job_dir_for(job_id)
    group, expected = _series_group_job_dirs(job_dir)
    if not bool(config.get("series_animation_enabled", True)) or expected < 2 or len(group) < 2:
        return {}
    selected = {str(value) for value in selected_job_ids}
    group_ids = [path.name for path in group]
    if selected and all(value in selected for value in group_ids):
        return {}
    status = _read_json(job_dir / "status.json", {})
    if str(status.get("series_animation_mode") or "").strip().lower() in {"series", "single"}:
        return {}
    return {
        "series_title": str(status.get("series_title") or job_dir.name),
        "episode": int(status.get("series_episode") or 0),
        "total": expected,
        "group_job_ids": group_ids,
    }


def set_job_series_animation_mode(job_id: str, mode: str) -> None:
    normalized = "series" if str(mode).lower() == "series" else "single"
    job_dir = job_dir_for(job_id)
    write_status(job_dir, series_animation_mode=normalized)
    if normalized == "single":
        metadata_path = job_dir / "metadata.json"
        metadata = _read_json(metadata_path, {})
        if isinstance(metadata, dict) and metadata_path.exists():
            for key in (
                "series_short_title", "series_part_label", "series_upload_prefix",
                "series_cover_label", "series_display_title",
            ):
                metadata.pop(key, None)
            _write_json(metadata_path, metadata)
    append_log(job_dir, f"series animation launch mode: {normalized}")


def _ai_rewrite_paragraphs(paragraphs: list[str], on_log: LogFn) -> str:
    batch_chars = max(800, int(config.get("ai_rewrite_batch_chars", 3500) or 3500))
    max_tokens = max(1200, min(8192, int(batch_chars * 1.6)))
    route = _llm_route_settings()
    llm = LLMBackend(
        provider=route["provider"],
        base_url=route["base_url"],
        api_key=route["api_key"],
        model=route["model"],
        system_prompt=str(config.get("ai_rewrite_prompt", "") or ""),
        style_suffix="",
        temperature=0.45,
        max_tokens=max_tokens,
        timeout=180.0,
    )
    batches: list[list[str]] = []
    batch: list[str] = []
    size = 0
    for para in paragraphs:
        projected = size + len(para) + 2
        if batch and projected > batch_chars:
            batches.append(batch)
            batch = []
            size = 0
        batch.append(para)
        size += len(para) + 2
    if batch:
        batches.append(batch)

    rewritten_parts: list[str] = []
    for index, current in enumerate(batches, start=1):
        request = (
            "请洗稿改写以下小说正文。只输出改写后的正文，不要解释，不要标题，不要编号。\n"
            "段落之间用一个空行分隔。\n\n"
            + "\n\n".join(current)
        )
        with external_api_slot(action="ai rewrite"):
            reply = llm.storyboard(request)
        cleaned_reply = _strip_llm_fences(reply)
        if cleaned_reply:
            rewritten_parts.append(cleaned_reply)
        on_log(f"  AI 洗稿批次 {index}/{len(batches)} 完成")
    return "\n\n".join(part.strip() for part in rewritten_parts if part.strip()).strip()


def _strip_llm_fences(text: str) -> str:
    value = str(text or "").strip()
    if value.startswith("```"):
        value = re.sub(r"^```[a-zA-Z0-9_-]*\s*", "", value)
        value = re.sub(r"\s*```$", "", value)
    for prefix in ("改写后的正文：", "洗稿后的正文：", "正文："):
        if value.startswith(prefix):
            value = value[len(prefix):].strip()
    return value


def _is_transient_json_response_error(error: str) -> bool:
    """Whether a text-model failure is worth pausing for before retrying.

    A bad title bundle can be corrected immediately by tightening the prompt,
    while a missing JSON object normally means an upstream model/relay briefly
    returned an unrelated response.  Pausing only for the latter keeps an
    unattended queue moving without delaying deterministic validation errors.
    """
    text = str(error or "").lower()
    return "llm did not return a json object" in text


def _stable_hash(value) -> str:
    data = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def _text_hash(text: str) -> str:
    return hashlib.sha256(str(text or "").encode("utf-8")).hexdigest()


def _file_signature(path: Path) -> dict:
    try:
        st = path.stat()
    except OSError:
        return {}
    return {"size": int(st.st_size), "mtime": round(float(st.st_mtime), 3)}


def _read_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _tts_config_hash() -> str:
    return _stable_hash(
        {
            "provider": config.tts_provider,
            "voice": config.tts_voice,
            "rate": config.tts_rate,
            "volume": config.get("tts_volume", 1.0),
            "voicevox_speed_scale": config.get("voicevox_speed_scale", 0.90),
            "voicevox_intonation_scale": config.get("voicevox_intonation_scale", 0.85),
            "voicevox_pause_scale": config.get("voicevox_pause_scale", 1.25),
            "model": config.get("tts_model", "tts-1"),
            "emotion": config.get("tts_emotion", ""),
            "base_url": config.tts_base_url,
            "extra": config.tts_extra or {},
            "api_key_hash": _text_hash(config.tts_api_key or "")[:16],
        }
    )


TTS_PRONUNCIATION_DICTIONARY = "tts_pronunciation_dictionary.txt"
TTS_AUTO_PRONUNCIATION_DICTIONARY = "tts_auto_pronunciation_dictionary.txt"
TTS_MANUAL_PRONUNCIATION_PROVIDERS = {"edge", "voicevox"}
IMPORTED_AUDIO_MANIFEST = "imported_audio.json"
IMPORTED_AUDIO_FILENAME = "imported_narration.mp3"


class PronunciationDictionaryConflictError(ValueError):
    """Raised when one written form has multiple candidate readings."""

    def __init__(self, conflicts: list[dict]):
        self.conflicts = conflicts
        summary = "；".join(
            f"“{item['written']}”：{' / '.join(item['readings'])}" for item in conflicts
        )
        super().__init__(f"读音词典中有重复词语需要选择读音：{summary}")


def parse_pronunciation_dictionary(
    text: str,
    conflict_choices: dict[str, str] | None = None,
) -> list[tuple[str, str]]:
    """Parse `written=reading` lines and return longest terms first."""
    candidates: dict[str, list[str]] = {}
    first_seen: dict[str, int] = {}
    line_numbers: dict[str, list[int]] = {}
    for line_number, raw_line in enumerate(str(text or "").lstrip("\ufeff").splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#") or line.startswith("//"):
            continue
        separator = "=" if "=" in line else ("＝" if "＝" in line else "")
        if not separator:
            raise ValueError(f"读音词典第 {line_number} 行缺少 = 或 ＝：{line[:50]}")
        written, reading = (part.strip() for part in line.split(separator, 1))
        if not written or not reading:
            raise ValueError(f"读音词典第 {line_number} 行的词语或读音为空")
        if written not in first_seen:
            first_seen[written] = len(first_seen)
        values = candidates.setdefault(written, [])
        if reading not in values:
            values.append(reading)
        line_numbers.setdefault(written, []).append(line_number)
    if not candidates:
        raise ValueError("读音词典中没有可用条目；格式应为：汉字词语=假名读音")
    conflicts = [
        {"written": written, "readings": values, "lines": line_numbers[written]}
        for written, values in candidates.items()
        if len(values) > 1
    ]
    choices = conflict_choices or {}
    unresolved = [
        item for item in conflicts
        if choices.get(item["written"]) not in item["readings"]
    ]
    if unresolved:
        raise PronunciationDictionaryConflictError(unresolved)
    readings = {
        written: choices.get(written, values[0])
        for written, values in candidates.items()
    }
    return sorted(readings.items(), key=lambda item: (-len(item[0]), first_seen[item[0]]))


def inspect_pronunciation_dictionary(
    source_path: str | Path,
    conflict_choices: dict[str, str] | None = None,
) -> dict:
    path = Path(source_path).expanduser()
    if not path.exists() or not path.is_file():
        raise FileNotFoundError(f"找不到读音词典：{path}")
    source = path.read_text(encoding="utf-8-sig")
    entries = parse_pronunciation_dictionary(source, conflict_choices=conflict_choices)
    normalized = source.lstrip("\ufeff")
    if conflict_choices:
        normalized = "".join(f"{written}={reading}\n" for written, reading in entries)
    return {
        "path": str(path),
        "entries": len(entries),
        "text": normalized if conflict_choices else source,
        "hash": _text_hash(normalized),
    }


def _apply_pronunciation_dictionary(text: str, entries: list[tuple[str, str]]) -> tuple[str, int]:
    if not entries or not text:
        return str(text or ""), 0
    readings = dict(entries)
    pattern = re.compile("|".join(re.escape(written) for written, _reading in entries))
    replacements = 0

    def replace(match: re.Match) -> str:
        nonlocal replacements
        replacements += 1
        return readings[match.group(0)]

    return pattern.sub(replace, str(text)), replacements


def _prepare_tts_pronunciation(
    segments: list[Segment],
    job_dir: Path,
    provider: str,
) -> tuple[list[str], list[int], list[tuple[str, str]], str]:
    """Build temporary narration text without changing subtitle source segments."""
    provider = str(provider or "").strip().lower()
    dictionary_path = job_dir / TTS_PRONUNCIATION_DICTIONARY
    auto_dictionary_path = job_dir / TTS_AUTO_PRONUNCIATION_DICTIONARY
    narration_texts = [seg.text for seg in segments]
    replacement_counts = [0 for _seg in segments]

    # Uploaded dictionaries are safe text substitutions for both Edge and
    # VOICEVOX. Auto-generated readings remain Edge-only by design.
    manual_source = ""
    if dictionary_path.exists() and provider in TTS_MANUAL_PRONUNCIATION_PROVIDERS:
        manual_source = dictionary_path.read_text(encoding="utf-8-sig")
    generated_source = ""
    if auto_dictionary_path.exists() and provider == "edge":
        generated_source = auto_dictionary_path.read_text(encoding="utf-8-sig")
    if not manual_source.strip() and not generated_source.strip():
        return narration_texts, replacement_counts, [], ""

    generated_entries = parse_pronunciation_dictionary(generated_source) if generated_source.strip() else []
    manual_entries = parse_pronunciation_dictionary(manual_source) if manual_source.strip() else []
    merged_readings = {written: reading for written, reading in generated_entries}
    merged_readings.update({written: reading for written, reading in manual_entries})
    dictionary_entries = sorted(merged_readings.items(), key=lambda item: -len(item[0]))
    dictionary_hash = _text_hash(
        generated_source.lstrip("\ufeff") + "\n--manual--\n" + manual_source.lstrip("\ufeff")
    )
    applied = [_apply_pronunciation_dictionary(seg.text, dictionary_entries) for seg in segments]
    narration_texts = [item[0] for item in applied]
    replacement_counts = [item[1] for item in applied]
    return narration_texts, replacement_counts, dictionary_entries, dictionary_hash


def attach_pronunciation_dictionary(
    job_id: str,
    source_path: str | Path,
    conflict_choices: dict[str, str] | None = None,
) -> dict:
    """Copy a validated pronunciation dictionary into a job and invalidate its TTS."""
    if is_worker_running(job_id):
        raise RuntimeError(f"{job_id} 正在运行。请先停止任务，再附加读音词典。")
    info = inspect_pronunciation_dictionary(source_path, conflict_choices=conflict_choices)
    job_dir = _safe_job_path(job_id)
    job_dir.mkdir(parents=True, exist_ok=True)
    target = job_dir / TTS_PRONUNCIATION_DICTIONARY
    target.write_text(str(info["text"]).lstrip("\ufeff"), encoding="utf-8")
    reset_result = {"changed": 0, "segments": []}
    if (job_dir / "segments.json").exists():
        segments = _load_job_segments(job_dir)
        reset_result = reset_tts_segments(job_id, indices=range(len(segments)))
    write_status(
        job_dir,
        pronunciation_dictionary=TTS_PRONUNCIATION_DICTIONARY,
        pronunciation_dictionary_entries=int(info["entries"]),
        pronunciation_dictionary_hash=str(info["hash"]),
    )
    append_log(
        job_dir,
        f"pronunciation dictionary attached: {info['entries']} entries; "
        f"TTS segments reset={reset_result.get('changed', 0)}",
    )
    return {
        "job_id": job_id,
        "entries": int(info["entries"]),
        "hash": str(info["hash"]),
        "path": str(target),
        "reset_segments": int(reset_result.get("changed", 0)),
    }


def inspect_imported_audio(source_path: str | Path) -> dict:
    """Validate an MP3 narration before it is copied into a job."""
    path = Path(source_path).expanduser()
    if not path.exists() or not path.is_file():
        raise FileNotFoundError(f"找不到 MP3 音频：{path}")
    if path.suffix.lower() != ".mp3":
        raise ValueError("导入音频目前只支持 MP3 文件。")
    duration = float(_audio_duration(path) or 0.0)
    if duration <= 0.05:
        raise ValueError(f"无法读取 MP3 时长，或音频为空：{path.name}")
    signature = _file_signature(path)
    if int(signature.get("size") or 0) < 512:
        raise ValueError(f"MP3 文件过小或已损坏：{path.name}")
    return {
        "path": str(path.resolve()),
        "name": path.name,
        "duration": duration,
        "size": int(signature.get("size") or 0),
    }


def attach_imported_audio(job_id: str, source_path: str | Path) -> dict:
    """Copy a complete narration MP3 into a job and mark TTS as bypassed."""
    if is_worker_running(job_id):
        raise RuntimeError(f"{job_id} 正在运行。请先停止任务，再导入 MP3。")
    info = inspect_imported_audio(source_path)
    job_dir = _safe_job_path(job_id)
    job_dir.mkdir(parents=True, exist_ok=True)
    audio_dir = job_dir / "audio"
    audio_dir.mkdir(parents=True, exist_ok=True)
    target = audio_dir / IMPORTED_AUDIO_FILENAME
    source = Path(str(info["path"]))
    if source.resolve() != target.resolve():
        temp = target.with_suffix(".tmp.mp3")
        temp.unlink(missing_ok=True)
        shutil.copy2(source, temp)
        temp.replace(target)

    copied_duration = float(_audio_duration(target) or 0.0)
    if copied_duration <= 0.05:
        target.unlink(missing_ok=True)
        raise ValueError("复制后的 MP3 无法读取，导入已取消。")
    signature = _file_signature(target)
    manifest = {
        "mode": "imported_mp3",
        "path": str(Path("audio") / IMPORTED_AUDIO_FILENAME),
        "original_name": str(info["name"]),
        "duration": round(copied_duration, 6),
        "file_size": int(signature.get("size") or 0),
        "file_mtime": float(signature.get("mtime") or 0),
        "timing_mode": "text_weight_estimate",
        "attached_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    _write_json(job_dir / IMPORTED_AUDIO_MANIFEST, manifest)
    _clear_downstream_after_tts_change(job_dir)
    write_status(
        job_dir,
        stage="queued",
        progress=0.0,
        audio_mode="imported",
        imported_audio=str(Path("audio") / IMPORTED_AUDIO_FILENAME),
        imported_audio_name=str(info["name"]),
        imported_audio_duration=round(copied_duration, 3),
        worker_pid=None,
        error="",
        youtube_url="",
    )
    append_log(
        job_dir,
        f"imported narration attached: {info['name']} ({copied_duration:.1f}s); TTS will be skipped",
    )
    return {
        "job_id": job_id,
        "path": str(target),
        "name": str(info["name"]),
        "duration": copied_duration,
    }


def _job_imported_audio(job_dir: Path) -> tuple[Path, dict] | None:
    manifest_path = job_dir / IMPORTED_AUDIO_MANIFEST
    if not manifest_path.exists():
        return None
    manifest = _read_json(manifest_path, {})
    if not isinstance(manifest, dict) or str(manifest.get("mode") or "") != "imported_mp3":
        raise RuntimeError("导入音频记录已损坏，请重新创建“正文 + MP3”任务。")
    relative = Path(str(manifest.get("path") or Path("audio") / IMPORTED_AUDIO_FILENAME))
    audio_path = relative if relative.is_absolute() else job_dir / relative
    try:
        resolved = audio_path.resolve()
        root = job_dir.resolve()
    except OSError as exc:
        raise RuntimeError(f"无法读取导入的 MP3：{exc}") from exc
    if resolved == root or root not in resolved.parents:
        raise RuntimeError("导入音频路径不安全，请重新创建任务。")
    if not resolved.exists():
        raise FileNotFoundError("任务内的导入 MP3 已丢失，请重新创建“正文 + MP3”任务。")
    return resolved, manifest


def _load_tts_manifest(path: Path) -> dict:
    data = _read_json(path, {})
    if not isinstance(data, dict):
        data = {}
    if not isinstance(data.get("segments"), list):
        data["segments"] = []
    data["schema_version"] = 1
    return data


_TTS_CACHE_READY_STATUSES = {"ready"}
_TTS_RESET_STATUSES = {
    "failed",
    "waiting_slot",
    "running",
    "invalid_waveform",
    "fallback_silence",
    "fallback_stalled",
    "fallback_manual",
}


def _set_tts_manifest_entry(manifest_path: Path, manifest: dict, entry: dict) -> None:
    segments = manifest.setdefault("segments", [])
    index = int(entry.get("index", -1))
    for pos, current in enumerate(segments):
        if int(current.get("index", -2)) == index:
            segments[pos] = entry
            break
    else:
        segments.append(entry)
    segments.sort(key=lambda item: int(item.get("index", 0)))
    manifest["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    _write_json(manifest_path, manifest)


def _tts_manifest_entry(manifest: dict, index: int) -> dict | None:
    for entry in manifest.get("segments") or []:
        try:
            if int(entry.get("index", -1)) == int(index):
                return entry
        except Exception:
            continue
    return None


def _tts_cache_matches(entry: dict | None, expected: dict, out: Path) -> bool:
    if not entry or not out.exists():
        return False
    if str(entry.get("status")) not in _TTS_CACHE_READY_STATUSES:
        return False
    for key in ("text_hash", "config_hash", "provider", "voice", "rate"):
        if str(entry.get(key, "")) != str(expected.get(key, "")):
            return False
    if str(entry.get("volume", "1.0")) != str(expected.get("volume", "1.0")):
        return False
    sig = _file_signature(out)
    if not sig:
        return False
    if int(entry.get("file_size") or -1) != sig["size"]:
        return False
    try:
        if abs(float(entry.get("file_mtime") or 0) - float(sig["mtime"])) > 1.0:
            return False
    except Exception:
        return False
    return float(entry.get("duration") or 0) > 0.05


def _tts_expected_for_segment(
    seg: Segment,
    config_hash: str | None = None,
    narration_text: str | None = None,
) -> dict:
    return {
        "text_hash": _text_hash(seg.text if narration_text is None else narration_text),
        "config_hash": config_hash or _tts_config_hash(),
        "provider": config.tts_provider,
        "voice": config.tts_voice,
        "rate": config.tts_rate,
        "volume": config.get("tts_volume", 1.0),
    }


def _ready_tts_entry(
    index: int,
    seg: Segment,
    out: Path,
    duration: float,
    expected: dict,
    status: str = "ready",
    waveform: dict | None = None,
) -> dict:
    sig = _file_signature(out)
    entry = {
        "index": index,
        "status": status,
        "path": str(out),
        "duration": round(float(duration), 3),
        "text_hash": expected["text_hash"],
        "config_hash": expected["config_hash"],
        "provider": expected["provider"],
        "voice": expected["voice"],
        "rate": expected["rate"],
        "volume": expected.get("volume", 1.0),
        "chars": len(seg.text),
        "file_size": sig.get("size", 0),
        "file_mtime": sig.get("mtime", 0),
    }
    if waveform:
        entry["waveform_ok"] = True
        entry["waveform"] = waveform
    return entry


def _tts_entries_by_index(manifest: dict) -> dict[int, dict]:
    entries: dict[int, dict] = {}
    for entry in manifest.get("segments") or []:
        if not isinstance(entry, dict):
            continue
        try:
            entries[int(entry.get("index", -1))] = entry
        except Exception:
            continue
    return entries


def _tts_backend_payload(segment_timeout_seconds: float, text: str, out_path: Path) -> dict:
    return {
        "provider": config.tts_provider,
        "voice": config.tts_voice,
        "rate": config.tts_rate,
        "api_key": config.tts_api_key,
        "base_url": config.tts_base_url,
        "extra": {
            **(config.tts_extra or {}),
            "emotion": config.tts_emotion,
            "model": config.get("tts_model", "tts-1"),
            "timeout_seconds": segment_timeout_seconds,
            "voicevox_speed_scale": config.get("voicevox_speed_scale", 0.90),
            "voicevox_intonation_scale": config.get("voicevox_intonation_scale", 0.85),
            "voicevox_pause_scale": config.get("voicevox_pause_scale", 1.25),
        },
        "volume": config.get("tts_volume", 1.0),
        "text": text,
        "out_path": str(out_path),
    }


def _synth_tts_subprocess(payload: dict, timeout_seconds: float) -> float:
    out_path = Path(str(payload["out_path"]))
    token = f"{os.getpid()}_{threading.get_ident()}_{time.time_ns()}"
    payload_path = out_path.with_name(f"{out_path.stem}.{token}.payload.json")
    result_path = out_path.with_name(f"{out_path.stem}.{token}.result.json")
    _write_json(payload_path, payload)
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    kwargs = {}
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    else:
        kwargs["start_new_session"] = True
    proc = subprocess.Popen(
        [sys.executable, "-B", "-m", "app.tts_worker", str(payload_path), str(result_path)],
        cwd=str(ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="ignore",
        env=env,
        **kwargs,
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout_seconds)
    except subprocess.TimeoutExpired as exc:
        if os.name == "nt":
            proc.kill()
        else:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except Exception:
                proc.kill()
        stdout, stderr = proc.communicate()
        result_path.unlink(missing_ok=True)
        raise TimeoutError(f"TTS subprocess timed out after {timeout_seconds:.0f}s") from exc
    finally:
        payload_path.unlink(missing_ok=True)

    result = _read_json(result_path, {})
    result_path.unlink(missing_ok=True)
    if proc.returncode != 0 or not result.get("ok"):
        detail = str(result.get("error") or stderr or stdout or f"exit {proc.returncode}").strip()
        raise RuntimeError(detail[:1200])
    return float(result.get("duration") or 0.0)


def _synth_tts_segment(tts: TTSBackend, text: str, out_path: Path, segment_timeout_seconds: float) -> float:
    if bool(config.get("tts_subprocess_isolation", True)):
        payload = _tts_backend_payload(segment_timeout_seconds, text, out_path)
        return _synth_tts_subprocess(payload, segment_timeout_seconds + 15.0)
    return tts.synth(text, out_path)


def _audio_waveform_stats(path: Path, duration: float) -> dict:
    timeout = max(30.0, min(180.0, float(duration or 0) + 30.0))
    try:
        with ffmpeg_slot(action="tts waveform check"):
            proc = subprocess.run(
                [
                    ffmpeg_path(),
                    "-hide_banner",
                    "-nostdin",
                    "-i",
                    str(path),
                    "-af",
                    "silencedetect=noise=-50dB:d=0.30,astats=metadata=1:reset=0",
                    "-f",
                    "null",
                    "-",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="ignore",
                timeout=timeout,
            )
    except Exception as exc:
        raise RuntimeError(f"waveform probe failed: {exc}") from exc
    if proc.returncode != 0:
        detail = (proc.stderr or "").strip().splitlines()[-1:] or [f"ffmpeg exit {proc.returncode}"]
        raise RuntimeError(f"waveform probe failed: {detail[0]}")

    stderr = proc.stderr or ""
    rms_values: list[float] = []
    peak_values: list[float] = []
    for value in re.findall(r"RMS level dB:\s*([-+]?inf|[-+]?\d+(?:\.\d+)?)", stderr, flags=re.I):
        if value.lower().endswith("inf"):
            continue
        try:
            rms_values.append(float(value))
        except Exception:
            pass
    for value in re.findall(r"Peak level dB:\s*([-+]?inf|[-+]?\d+(?:\.\d+)?)", stderr, flags=re.I):
        if value.lower().endswith("inf"):
            continue
        try:
            peak_values.append(float(value))
        except Exception:
            pass
    silence_total = 0.0
    for value in re.findall(r"silence_duration:\s*([0-9]+(?:\.[0-9]+)?)", stderr):
        try:
            silence_total += float(value)
        except Exception:
            pass
    silence_ratio = min(1.0, max(0.0, silence_total / max(0.001, float(duration or 0))))
    return {
        "rms_db": round(max(rms_values), 3) if rms_values else None,
        "peak_db": round(max(peak_values), 3) if peak_values else None,
        "silence_ratio": round(silence_ratio, 4),
        "silence_seconds": round(silence_total, 3),
    }


def _validate_tts_audio(path: Path) -> tuple[float, dict]:
    duration = _audio_duration(path)
    if duration <= 0.05:
        raise RuntimeError("audio duration is zero")
    sig = _file_signature(path)
    if int(sig.get("size") or 0) < 512:
        raise RuntimeError("audio file is too small")
    if not bool(config.get("tts_waveform_validation", True)):
        return duration, {}

    stats = _audio_waveform_stats(path, duration)
    rms_db = stats.get("rms_db")
    peak_db = stats.get("peak_db")
    silence_ratio = float(stats.get("silence_ratio") or 0.0)
    try:
        min_rms_db = float(config.get("tts_waveform_min_rms_db", -55.0))
    except Exception:
        min_rms_db = -55.0
    try:
        max_silence_ratio = float(config.get("tts_waveform_max_silence_ratio", 0.92))
    except Exception:
        max_silence_ratio = 0.92
    max_silence_ratio = max(0.5, min(0.99, max_silence_ratio))

    if rms_db is None or peak_db is None:
        raise RuntimeError("waveform has no measurable signal")
    if float(rms_db) < min_rms_db:
        raise RuntimeError(f"waveform too quiet: rms={float(rms_db):.1f}dB < {min_rms_db:.1f}dB")
    if silence_ratio > max_silence_ratio:
        raise RuntimeError(f"waveform mostly silent: {silence_ratio:.0%} > {max_silence_ratio:.0%}")
    return duration, stats


def _load_job_segments(job_dir: Path) -> list[Segment]:
    rows = _read_json(job_dir / "segments.json", [])
    if not isinstance(rows, list) or not rows:
        raise RuntimeError("还没有 segments.json，请先让任务跑过整理/切片阶段。")
    segments: list[Segment] = []
    for pos, row in enumerate(rows):
        if isinstance(row, dict):
            raw_index = row.get("i", row.get("index", pos))
            text = str(row.get("text") or "")
        else:
            raw_index = pos
            text = str(row or "")
        if not text.strip():
            continue
        try:
            index = int(raw_index)
        except Exception:
            index = pos
        segments.append(Segment(index=index, text=text))
    if not segments:
        raise RuntimeError("segments.json 中没有可用文本段。")
    return segments


def _safe_unlink_under(root: Path, value) -> None:
    if not value:
        return
    try:
        candidate = Path(str(value))
        if not candidate.is_absolute():
            candidate = root / candidate
        resolved = candidate.resolve()
        root_resolved = root.resolve()
        if resolved == root_resolved or root_resolved not in resolved.parents:
            return
        resolved.unlink(missing_ok=True)
    except OSError:
        pass


def _clear_downstream_after_tts_change(job_dir: Path) -> None:
    # TTS duration changes invalidate video clips and pacing, but not the generated
    # story images or their prefetch cache. Keeping images avoids paid regeneration.
    for name in ("_clips",):
        target = job_dir / name
        if target.exists():
            shutil.rmtree(target)
    for name in (
        "durations.json",
        "imported_audio_timing.json",
        "plans.json",
        "plans_prefetch.json",
        "compose_manifest.json",
        "audio_full.mp3",
        "subtitle.ass",
        "subtitle.srt",
        "result.json",
        "upload_result.json",
    ):
        (job_dir / name).unlink(missing_ok=True)
    # A retry invalidates the compose manifest, not the last known-good MP4.
    # The composer writes a new temporary MP4 and replaces the old one only
    # after duration validation succeeds.
    for video_path in {job_dir / "final.mp4", video_output_path(job_dir)}:
        video_path.with_name(video_path.stem + ".tmp" + video_path.suffix).unlink(missing_ok=True)


def reset_from_clean_reuse_images(job_id: str) -> dict:
    """Redo cleanup and narration while keeping all generated scene images."""
    if is_worker_running(job_id):
        raise RuntimeError(f"{job_id} 正在运行。请先停止任务，再重新洗稿和配音。")
    job_dir = _safe_job_path(job_id)
    status = load_status(job_id, include_worker=False)
    _resolve_job_input_source(
        job_dir,
        str(status.get("input") or status.get("source_path") or ""),
        on_log=lambda message: append_log(job_dir, message),
    )
    images = _valid_scene_images(job_dir)
    if not images:
        raise RuntimeError("没有可复用的图片，无法执行“生图除外”重试。")
    _write_json(job_dir / IMAGE_SELECTION_FILE, {
        "mode": "cycle",
        "force_even_pacing": True,
        "available_images": len(images),
        "chosen_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    })
    audio_dir = job_dir / "audio"
    if audio_dir.exists():
        shutil.rmtree(audio_dir)
    for name in (
        ACCELERATION_PREFETCH_REPORT,
        "text_rewritten.txt", "text_rewrite_report.json", "text_tts_ready.txt",
        "segments.json", "tts_auto_pronunciation_report.json",
    ):
        (job_dir / name).unlink(missing_ok=True)
    _clear_downstream_after_tts_change(job_dir)
    write_status(job_dir, stage="queued", progress=0.10, worker_pid=None, error="", youtube_url="")
    append_log(job_dir, f"reset from clean with {len(images)} cached images; image API disabled")
    return {"job_id": job_id, "images": len(images)}


def _refresh_tts_manifest_header(manifest: dict, total: int) -> None:
    manifest.update(
        {
            "provider": config.tts_provider,
            "voice": config.tts_voice,
            "rate": config.tts_rate,
            "volume": config.get("tts_volume", 1.0),
            "config_hash": _tts_config_hash(),
            "total": total,
        }
    )


def reset_tts_segments(job_id: str, indices: Iterable[int] | None = None) -> dict:
    """Remove selected bad TTS segment cache so the next resume resynthesizes it."""
    if is_worker_running(job_id):
        raise RuntimeError(f"{job_id} 正在运行。请先停止任务，再重试 TTS 段。")
    job_dir = _safe_job_path(job_id)
    segments = _load_job_segments(job_dir)
    selected = {int(i) for i in indices} if indices is not None else None
    audio_dir = job_dir / "audio"
    manifest_path = audio_dir / "tts_manifest.json"
    manifest = _load_tts_manifest(manifest_path)
    entries = _tts_entries_by_index(manifest)
    changed: list[int] = []
    keep_entries = []
    remove_indexes: set[int] = set()

    for i, _seg in enumerate(segments):
        entry = entries.get(i)
        status = str(entry.get("status") or "") if entry else ""
        should_reset = i in selected if selected is not None else status in _TTS_RESET_STATUSES
        if not should_reset:
            continue
        remove_indexes.add(i)
        changed.append(i)
        out = audio_dir / f"seg_{i:05d}.mp3"
        out.unlink(missing_ok=True)
        out.with_suffix(".tmp.mp3").unlink(missing_ok=True)
        if entry:
            _safe_unlink_under(job_dir, entry.get("path"))

    for entry in manifest.get("segments") or []:
        try:
            index = int(entry.get("index", -1))
        except Exception:
            index = -1
        if index not in remove_indexes:
            keep_entries.append(entry)
    manifest["segments"] = keep_entries
    _refresh_tts_manifest_header(manifest, len(segments))
    audio_dir.mkdir(parents=True, exist_ok=True)
    _write_json(manifest_path, manifest)

    if changed:
        _clear_downstream_after_tts_change(job_dir)
        write_status(job_dir, stage="queued", progress=0.15, worker_pid=None, error="", youtube_url="")
        append_log(job_dir, f"TTS reset segments {changed}; restart selected job to synthesize them again")
    return {"job_id": job_id, "changed": len(changed), "segments": changed}


def reset_tts_unfinished_segments(job_id: str) -> dict:
    """Remove unfinished/failed/invalid TTS segments so the next resume retries real audio."""
    if is_worker_running(job_id):
        raise RuntimeError(f"{job_id} 正在运行。请先停止任务，再执行 TTS 重试。")
    job_dir = _safe_job_path(job_id)
    segments = _load_job_segments(job_dir)
    audio_dir = job_dir / "audio"
    audio_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = audio_dir / "tts_manifest.json"
    manifest = _load_tts_manifest(manifest_path)
    _refresh_tts_manifest_header(manifest, len(segments))
    entries = _tts_entries_by_index(manifest)
    changed: list[int] = []
    keep_entries = []
    remove_indexes: set[int] = set()

    for i, _seg in enumerate(segments):
        out = audio_dir / f"seg_{i:05d}.mp3"
        entry = entries.get(i)
        status = str(entry.get("status") or "missing") if entry else "missing"
        if entry and status not in _TTS_RESET_STATUSES:
            try:
                _validate_tts_audio(out)
                continue
            except Exception:
                status = "invalid_waveform"
        if not entry and not out.exists():
            continue
        remove_indexes.add(i)
        changed.append(i)
        out.unlink(missing_ok=True)
        out.with_suffix(".tmp.mp3").unlink(missing_ok=True)
        if entry:
            _safe_unlink_under(job_dir, entry.get("path"))

    for entry in manifest.get("segments") or []:
        try:
            index = int(entry.get("index", -1))
        except Exception:
            index = -1
        if index not in remove_indexes:
            keep_entries.append(entry)
    manifest["segments"] = keep_entries
    _write_json(manifest_path, manifest)

    if changed:
        _clear_downstream_after_tts_change(job_dir)
        write_status(job_dir, stage="queued", progress=0.15, worker_pid=None, error="", youtube_url="")
        append_log(job_dir, f"TTS unfinished/bad segments reset {changed}; restart selected job to retry real audio")
    return {"job_id": job_id, "changed": len(changed), "segments": changed}


def redo_all_tts_reuse_images(job_id: str) -> dict:
    """Regenerate every TTS segment, then recompose strictly with existing images.

    This durable mode is used by the desktop UI's full TTS redo action.  It
    deliberately prevents both scene-image and cover generation when the job
    resumes, while allowing image durations to be recalculated for the new
    narration.
    """
    if is_worker_running(job_id):
        raise RuntimeError(f"{job_id} 正在运行。请先停止任务，再重做 TTS。")
    job_dir = _safe_job_path(job_id)
    images = _valid_scene_images(job_dir)
    if not images:
        raise RuntimeError("没有可复用的图片，无法执行“仅重做 TTS”。")
    segments = _load_job_segments(job_dir)
    if not segments:
        raise RuntimeError("没有可用的文本分段，无法重做 TTS。")

    # Capture the image-only decision before plans.json is cleared by the TTS
    # reset.  The resumed run creates a new timing plan with this same count.
    set_image_fallback_selection(job_id, "cycle")
    result = reset_tts_segments(job_id, indices=range(len(segments)))
    _write_json(job_dir / TTS_REDO_REUSE_IMAGES_FILE, {
        "mode": "reuse_existing_images_only",
        "images": len(images),
        "requested_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    })
    append_log(job_dir, "full TTS redo requested: existing scene images and cover are locked; image APIs are disabled")
    return {**result, "images_reused": len(images)}


def _stage_tts_manifest_impl(
    segments: list[Segment],
    job_dir: Path,
    on_log: LogFn,
    on_prog: ProgFn,
) -> tuple[list[Path], list[float]]:
    dictionary_path = job_dir / TTS_PRONUNCIATION_DICTIONARY
    auto_dictionary_path = job_dir / TTS_AUTO_PRONUNCIATION_DICTIONARY
    narration_texts, replacement_counts, dictionary_entries, dictionary_hash = _prepare_tts_pronunciation(
        segments,
        job_dir,
        config.tts_provider,
    )

    workers = _parallel_limit("max_parallel_tts", 2, len(segments), hard_cap=12)
    segment_timeout_seconds = _bounded_timeout(config.get("tts_segment_timeout_seconds", 180), 180.0)
    stall_retry_seconds = _optional_timeout(config.get("tts_stall_fallback_seconds", 240), 240.0, minimum=5.0)
    retry_until_success = bool(config.get("tts_retry_until_success", True))
    on_log(
        f"[3/6] TTS provider={config.tts_provider} voice={config.tts_voice} "
        f"volume={config.get('tts_volume', 1.0)} parallel={workers} "
        f"timeout={segment_timeout_seconds:.0f}s stall_retry_log={stall_retry_seconds:.0f}s "
        f"retry_until_success={'on' if retry_until_success else 'off'} waveform_check={'on' if config.get('tts_waveform_validation', True) else 'off'}"
    )
    if dictionary_entries:
        changed_segments = sum(1 for count in replacement_counts if count)
        total_replacements = sum(replacement_counts)
        on_log(
            f"  pronunciation dictionary: {len(dictionary_entries)} entries, "
            f"{total_replacements} replacements in {changed_segments}/{len(segments)} segments"
        )
    elif dictionary_path.exists() and config.tts_provider not in TTS_MANUAL_PRONUNCIATION_PROVIDERS:
        on_log("  uploaded pronunciation dictionary only applies when TTS provider=edge or voicevox")
    elif auto_dictionary_path.exists() and config.tts_provider != "edge":
        on_log("  auto pronunciation dictionary only applies when TTS provider=edge")

    def make_tts() -> TTSBackend:
        return TTSBackend(
            provider=config.tts_provider,
            voice=config.tts_voice,
            rate=config.tts_rate,
            api_key=config.tts_api_key,
            base_url=config.tts_base_url,
            extra={
                **(config.tts_extra or {}),
                "emotion": config.tts_emotion,
                "model": config.get("tts_model", "tts-1"),
                "timeout_seconds": segment_timeout_seconds,
                "voicevox_speed_scale": config.get("voicevox_speed_scale", 0.90),
                "voicevox_intonation_scale": config.get("voicevox_intonation_scale", 0.85),
                "voicevox_pause_scale": config.get("voicevox_pause_scale", 1.25),
            },
            volume=config.get("tts_volume", 1.0),
        )

    audio_dir = job_dir / "audio"
    audio_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = audio_dir / "tts_manifest.json"
    manifest = _load_tts_manifest(manifest_path)
    manifest_lock = threading.Lock()
    _refresh_tts_manifest_header(manifest, len(segments))
    if dictionary_hash:
        manifest["config_hash"] = _stable_hash(
            {"tts_config_hash": manifest["config_hash"], "pronunciation_dictionary_hash": dictionary_hash}
        )
        manifest["pronunciation_dictionary"] = TTS_PRONUNCIATION_DICTIONARY
        manifest["pronunciation_dictionary_entries"] = len(dictionary_entries)
        manifest["pronunciation_dictionary_hash"] = dictionary_hash
    else:
        for key in (
            "pronunciation_dictionary",
            "pronunciation_dictionary_entries",
            "pronunciation_dictionary_hash",
        ):
            manifest.pop(key, None)
    manifest["parallel"] = workers
    _write_json(manifest_path, manifest)

    preview_path = audio_dir / "tts_pronunciation_preview.json"
    if dictionary_entries:
        _write_json(
            preview_path,
            {
                "dictionary": TTS_PRONUNCIATION_DICTIONARY,
                "entries": len(dictionary_entries),
                "total_replacements": sum(replacement_counts),
                "changed_segments": [
                    {
                        "index": i,
                        "replacements": replacement_counts[i],
                        "original": seg.text,
                        "tts_text": narration_texts[i],
                    }
                    for i, seg in enumerate(segments)
                    if replacement_counts[i]
                ],
            },
        )
    else:
        preview_path.unlink(missing_ok=True)

    audios: list[Path | None] = [None] * len(segments)
    durations: list[float] = [0.0] * len(segments)
    try:
        retries = max(1, int(config.get("tts_retries", 3) or 3))
    except Exception:
        retries = 3
    try:
        heartbeat_seconds = float(config.get("tts_heartbeat_seconds", 30) or 30)
    except Exception:
        heartbeat_seconds = 30.0
    heartbeat_seconds = max(10.0, min(300.0, heartbeat_seconds))
    running_lock = threading.Lock()
    running_segments: dict[int, dict[str, float | int]] = {}
    stall_warned: set[int] = set()

    def set_manifest(entry: dict) -> None:
        with manifest_lock:
            _set_tts_manifest_entry(manifest_path, manifest, entry)

    def mark_running(index: int, attempt: int) -> None:
        with running_lock:
            running_segments[index] = {"attempt": attempt, "started": time.monotonic()}

    def clear_running(index: int) -> None:
        with running_lock:
            running_segments.pop(index, None)

    def running_summary() -> str:
        with running_lock:
            items = sorted(running_segments.items())
        if not items:
            return "waiting for an API slot or finishing cached audio"
        parts = []
        now = time.monotonic()
        for index, info in items[:4]:
            elapsed = max(0.0, now - float(info.get("started") or now))
            parts.append(f"seg{index} attempt {int(info.get('attempt') or 1)} {elapsed:.0f}s")
        extra = "" if len(items) <= 4 else f", +{len(items) - 4} more"
        return ", ".join(parts) + extra

    def process_segment(i: int, seg: Segment) -> tuple[int, Path, float]:
        out = audio_dir / f"seg_{i:05d}.mp3"
        narration_text = narration_texts[i]
        expected = _tts_expected_for_segment(
            seg,
            str(manifest["config_hash"]),
            narration_text=narration_text,
        )
        with manifest_lock:
            entry = _tts_manifest_entry(manifest, i)
        d = 0.0
        waveform: dict = {}

        # A segment containing only punctuation (for example "！") has
        # nothing for TTS to pronounce.  Voice engines commonly return a
        # near-silent file for it, which then used to trigger an unbounded
        # retry loop.  Preserve the segment timing with a tiny silent file
        # and mark it as intentionally skipped.
        if not any(ch.isalnum() for ch in narration_text):
            tmp = out.with_suffix(".tmp.mp3")
            tmp.unlink(missing_ok=True)
            proc = subprocess.run(
                [
                    ffmpeg_path(), "-y", "-f", "lavfi", "-i",
                    "anullsrc=r=24000:cl=mono", "-t", "0.10",
                    "-c:a", "libmp3lame", "-q:a", "9", str(tmp),
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="ignore",
                timeout=30,
            )
            if proc.returncode != 0:
                raise RuntimeError("failed to create punctuation silence")
            tmp.replace(out)
            d = _audio_duration(out)
            set_manifest({
                **expected,
                "index": i,
                "status": "skipped_punctuation",
                "path": str(out),
                "duration": d,
                "waveform": {"intentional_silence": True},
                "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            })
            on_log(f"  TTS seg{i} contains only punctuation; inserted {d:.2f}s silence")
            return i, out, d
        if _tts_cache_matches(entry, expected, out):
            try:
                d, waveform = _validate_tts_audio(out)
                set_manifest(_ready_tts_entry(i, seg, out, d, expected, waveform=waveform))
            except Exception as exc:
                out.unlink(missing_ok=True)
                set_manifest(
                    {
                        **expected,
                        "index": i,
                        "status": "invalid_waveform",
                        "path": str(out),
                        "error": str(exc),
                        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                    }
                )
                on_log(f"  WARN seg{i} cached TTS invalid, retrying: {exc}")
                d = 0.0
        elif out.exists() and entry is not None:
            out.unlink(missing_ok=True)
            on_log(f"  TTS seg{i} text/settings changed; regenerating cached audio")
        elif out.exists():
            try:
                d, waveform = _validate_tts_audio(out)
                set_manifest(_ready_tts_entry(i, seg, out, d, expected, waveform=waveform))
            except Exception as exc:
                out.unlink(missing_ok=True)
                set_manifest(
                    {
                        **expected,
                        "index": i,
                        "status": "invalid_waveform",
                        "path": str(out),
                        "error": str(exc),
                        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                    }
                )
                on_log(f"  WARN seg{i} existing TTS invalid, retrying: {exc}")
                d = 0.0

        if d <= 0.05:
            tts = make_tts()
            attempt = 1
            while True:
                tmp = out.with_suffix(".tmp.mp3")
                tmp.unlink(missing_ok=True)
                try:
                    set_manifest(
                        {
                            **expected,
                            "index": i,
                            "status": "waiting_slot",
                            "path": str(out),
                            "attempt": attempt,
                            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                        },
                    )
                    try:
                        with external_api_slot(
                            action=f"tts {i + 1}/{len(segments)}",
                            timeout_seconds=segment_timeout_seconds,
                        ):
                            set_manifest(
                                {
                                    **expected,
                                    "index": i,
                                    "status": "running",
                                    "path": str(out),
                                    "attempt": attempt,
                                    "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                                },
                            )
                            mark_running(i, attempt)
                            d = _synth_tts_segment(tts, narration_text, tmp, segment_timeout_seconds)
                        d, waveform = _validate_tts_audio(tmp)
                        tmp.replace(out)
                        set_manifest(_ready_tts_entry(i, seg, out, d, expected, waveform=waveform))
                        break
                    finally:
                        clear_running(i)
                except Exception as exc:
                    tmp.unlink(missing_ok=True)
                    set_manifest(
                        {
                            **expected,
                            "index": i,
                            "status": "invalid_waveform" if "waveform" in str(exc).lower() or "silent" in str(exc).lower() or "quiet" in str(exc).lower() else "failed",
                            "path": str(out),
                            "attempt": attempt,
                            "error": str(exc),
                            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                        },
                    )
                    limit_label = "until success" if retry_until_success else f"{attempt}/{retries}"
                    on_log(f"  WARN seg{i} TTS failed {limit_label}: {exc}")
                    if not retry_until_success and attempt >= retries:
                        raise RuntimeError(f"seg{i} TTS failed after {retries} attempts") from exc
                    attempt += 1
                    delay = min(60.0, max(2.0, (attempt - 1) * 2.0))
                    on_log(f"    TTS seg{i} retrying real audio after {delay:.0f}s (attempt {attempt})...")
                    time.sleep(delay)

        return i, out, d

    completed = 0
    completed_indexes: set[int] = set()

    def record_completed(i: int, out: Path, d: float) -> None:
        nonlocal completed
        if i in completed_indexes:
            return
        audios[i] = out
        durations[i] = d
        completed_indexes.add(i)
        completed += 1
        _write_json(job_dir / "durations.json", durations)
        on_log(f"  TTS progress {completed}/{len(segments)}: seg{i} {d:.1f}s, total {sum(durations):.1f}s")
        on_prog(completed / max(1, len(segments)))

    pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="tts")
    future_segments = {pool.submit(process_segment, i, seg): (i, seg) for i, seg in enumerate(segments)}
    pending = set(future_segments)
    last_heartbeat = 0.0

    def stalled_futures() -> list[tuple[object, int, Segment, float]]:
        if stall_retry_seconds <= 0:
            return []
        now = time.monotonic()
        with running_lock:
            snapshot = dict(running_segments)
        stalled = []
        for future in list(pending):
            i, seg = future_segments[future]
            info = snapshot.get(i)
            if not info:
                continue
            elapsed = max(0.0, now - float(info.get("started") or now))
            if elapsed >= stall_retry_seconds:
                stalled.append((future, i, seg, elapsed))
        return stalled

    try:
        while pending:
            monitor_timeout = heartbeat_seconds
            if stall_retry_seconds > 0:
                monitor_timeout = min(heartbeat_seconds, max(1.0, stall_retry_seconds / 4.0))
            done, pending = wait(pending, timeout=monitor_timeout, return_when=FIRST_COMPLETED)
            if not done:
                stalled = stalled_futures()
                for _future, i, _seg, elapsed in stalled:
                    if i not in stall_warned:
                        stall_warned.add(i)
                        on_log(
                            f"  WARN seg{i} still running {elapsed:.0f}s; subprocess timeout will kill and retry, "
                            "no silent placeholder will be generated"
                        )
                now = time.monotonic()
                if now - last_heartbeat >= heartbeat_seconds:
                    last_heartbeat = now
                    on_log(f"  TTS still running: {running_summary()} ({completed}/{len(segments)} done)")
                on_prog(completed / max(1, len(segments)))
                continue
            for future in done:
                i, out, d = future.result()
                if i not in completed_indexes:
                    record_completed(i, out, d)
    finally:
        pool.shutdown(wait=False, cancel_futures=True)

    final_audios = [p for p in audios if p is not None]
    if len(final_audios) != len(segments):
        raise RuntimeError("TTS did not produce all audio segments")
    on_log(f"  OK {len(final_audios)} audio segments, total {sum(durations):.1f}s")
    return final_audios, durations


def stage_tts(
    segments: list[Segment],
    job_dir: Path,
    on_log: LogFn = _noop,
    on_prog: ProgFn = _noop,
) -> tuple[list[Path], list[float]]:
    imported = _job_imported_audio(job_dir)
    if imported is not None:
        audio_path, manifest = imported
        total_duration = float(_audio_duration(audio_path) or 0.0)
        if total_duration <= 0.05:
            raise RuntimeError("导入的 MP3 无法读取或时长为 0，请重新导入。")
        if not segments:
            raise RuntimeError("没有可用的文本切片，无法为导入音频生成时间轴。")
        weights = [max(0.001, _estimated_speech_duration(seg.text)) for seg in segments]
        weight_total = sum(weights)
        durations = [total_duration * weight / weight_total for weight in weights]
        durations[-1] += total_duration - sum(durations)
        _write_json(job_dir / "durations.json", durations)

        cursor = 0.0
        timing_rows = []
        for seg, duration in zip(segments, durations):
            timing_rows.append(
                {
                    "index": int(seg.index),
                    "start": round(cursor, 6),
                    "end": round(cursor + duration, 6),
                    "duration": round(duration, 6),
                    "text": seg.text,
                }
            )
            cursor += duration
        _write_json(
            job_dir / "imported_audio_timing.json",
            {
                "mode": "text_weight_estimate",
                "audio": str(audio_path),
                "audio_duration": round(total_duration, 6),
                "original_name": str(manifest.get("original_name") or audio_path.name),
                "note": "No word timestamps were supplied; segment timing is estimated from text length.",
                "segments": timing_rows,
            },
        )
        on_log(
            f"[3/6] 使用导入 MP3，跳过 TTS：{manifest.get('original_name') or audio_path.name} "
            f"({total_duration:.1f}s, {len(segments)} 个文本段)"
        )
        on_log("  字幕和画面时间轴按各段文本长度估算，总时长与 MP3 严格一致")
        on_prog(1.0)
        return [audio_path], durations
    return _stage_tts_manifest_impl(segments, job_dir, on_log, on_prog)


def stage_pacing(
    segments: list[Segment],
    durations: list[float],
    on_log: LogFn = _noop,
    fixed_count_override: int | None = None,
) -> list[ImagePlan]:
    mode = "fixed_count" if fixed_count_override else config.pacing_mode
    fixed_count = fixed_count_override or config.pacing_fixed_count
    on_log(f"[4/6] 图音配比: mode={mode}")
    plans = plan_images(
        segments,
        durations,
        mode=mode,
        seconds_per_image=config.pacing_seconds_per_image,
        sentences_per_image=config.pacing_sentences_per_image,
        fixed_count=fixed_count,
    )
    avg = sum(p.duration for p in plans) / max(1, len(plans))
    on_log(f"  OK 需要 {len(plans)} 张图，平均 {avg:.1f}s/张")
    return plans


def stage_character_analysis(
    novel: Novel,
    segments: list[Segment],
    job_dir: Path,
    on_log: LogFn = _noop,
) -> dict | None:
    if not bool(config.get("character_analysis_enabled", True)):
        return None
    out = job_dir / "character_profiles.json"
    if out.exists():
        try:
            cached = json.loads(out.read_text(encoding="utf-8"))
            if isinstance(cached, dict) and cached.get("enabled"):
                on_log(f"  character analysis reuse cached profiles: {out.name}")
                return cached
        except Exception:
            pass

    llm_route = _llm_route_settings()
    if not can_call_analysis_llm(llm_route["provider"], llm_route["api_key"]):
        payload = {"enabled": False, "reason": "LLM API key is empty and provider is not ollama/custom"}
        _write_json(out, payload)
        on_log("  WARN character analysis skipped: no usable LLM credentials")
        return payload

    on_log("[4/6] character analysis: extracting protagonists, supporting roles, and visual locks")
    llm = LLMBackend(
        provider=llm_route["provider"],
        base_url=llm_route["base_url"],
        api_key=llm_route["api_key"],
        model=llm_route["model"],
        system_prompt=config.llm_storyboard_prompt,
        style_suffix="",
    )
    try:
        payload = analyze_characters(
            novel,
            segments,
            llm,
            max_chars=int(config.get("character_analysis_max_chars", 12000) or 12000),
            max_tokens=int(config.get("character_analysis_max_tokens", 1800) or 1800),
            system_prompt=str(config.get("character_analysis_prompt", "") or DEFAULT_CHARACTER_ANALYSIS_PROMPT),
        )
    except Exception as exc:
        payload = {"enabled": False, "reason": f"character analysis failed: {exc}"}
        _write_json(out, payload)
        on_log(f"  WARN character analysis failed: {exc}")
        return payload

    _write_json(out, payload)
    characters = payload.get("characters") if isinstance(payload, dict) else []
    on_log(f"  OK character profiles: {len(characters) if isinstance(characters, list) else 0} characters -> {out.name}")
    return payload


def share_series_character_analysis(job_dir: Path, payload: dict | None, on_log: LogFn = _noop) -> dict | None:
    """Merge stable character locks across every episode in an enabled series."""
    if not series_animation_enabled_for_job(job_dir) or not isinstance(payload, dict):
        return payload
    status = _read_json(job_dir / "status.json", {})
    project_id = str(status.get("project_id") or "").strip()
    if project_id:
        merged = projects.merge_character_profiles(project_id, payload)
        if isinstance(merged, dict):
            _write_json(job_dir / "character_profiles.json", merged)
            characters = merged.get("characters")
            on_log(
                f"  project shared character profiles: "
                f"{len(characters) if isinstance(characters, list) else 0}"
            )
        return merged
    group_key = str(status.get("series_group_key") or status.get("series_title") or "").strip()
    try:
        episode = int(status.get("series_episode") or 0)
    except (TypeError, ValueError):
        episode = 0
    if not group_key or episode < 1:
        return payload
    registry_path = JOBS_DIR / ".series_character_profiles.json"
    registry = _read_json(registry_path, {})
    if not isinstance(registry, dict):
        registry = {}
    shared = registry.get(group_key) if isinstance(registry.get(group_key), dict) else {}
    merged = dict(shared or payload)
    existing_characters = [
        item for item in merged.get("characters") or [] if isinstance(item, dict)
    ]
    known = {
        str(item.get("name") or item.get("trigger") or "").strip()
        for item in existing_characters
    }
    for item in payload.get("characters") or []:
        if not isinstance(item, dict):
            continue
        identity = str(item.get("name") or item.get("trigger") or "").strip()
        if identity and identity not in known:
            existing_characters.append(item)
            known.add(identity)
    merged["characters"] = existing_characters
    merged["enabled"] = bool(shared.get("enabled", payload.get("enabled", False)))
    for key in ("visual_theme", "plot_summary", "story_conflict", "protagonists", "supporting_characters"):
        if not merged.get(key) and payload.get(key):
            merged[key] = payload[key]
    registry[group_key] = merged
    _write_json(registry_path, registry)
    _write_json(job_dir / "character_profiles.json", merged)
    on_log(f"  series shared character profiles: {len(existing_characters)}")
    return merged


def stage_story_context(
    novel: Novel,
    segments: list[Segment],
    job_dir: Path,
    on_log: LogFn = _noop,
) -> dict:
    """Build one cached global background guide for highlight selection.

    The guide is deliberately not an image prompt: local numbered narration remains
    the only source allowed to introduce visible people, actions, and objects.
    """
    if not bool(config.get("storyboard_highlight_enabled", True)):
        return {}
    max_chars = int(config.get("storyboard_highlight_context_max_chars", 18000) or 18000)
    source = sampled_story_input(novel, segments, max_chars)
    route = _llm_route_settings()
    input_hash = _stable_hash({
        "schema": "video_image_solution_1_story_context_v1",
        "source": source,
        "provider": route["provider"],
        "model": route["model"],
        "prompt": STORY_CONTEXT_SYSTEM_PROMPT,
    })
    out = job_dir / "story_visual_context.json"
    cached = _read_json(out, {})
    if isinstance(cached, dict) and cached.get("input_hash") == input_hash:
        on_log("  reuse story visual context for highlight selection")
        return normalize_story_context(cached.get("context"))

    fallback_summary = str(novel.description or "").strip()
    if fallback_summary.lower() == "local text input":
        fallback_summary = ""
    fallback_summary = fallback_summary[:600]
    fallback = normalize_story_context({
        "story_summary": fallback_summary,
        "title_brief": fallback_summary,
        "era_and_world": "Follow the local narration exactly.",
    })
    if not _can_call_text_llm():
        _write_json(out, {"schema": "video_image_solution_1", "input_hash": input_hash, "context": fallback, "fallback_used": True})
        on_log("  WARN story context analysis skipped: no usable text LLM")
        return fallback

    llm = LLMBackend(
        provider=route["provider"], base_url=route["base_url"], api_key=route["api_key"],
        model=route["model"], system_prompt=STORY_CONTEXT_SYSTEM_PROMPT,
        style_suffix="", temperature=0.15, max_tokens=1200,
    )
    try:
        with external_api_slot(action="story visual context"):
            raw = llm.complete(STORY_CONTEXT_SYSTEM_PROMPT, source, max_tokens=1200, temperature=0.15)
        context = normalize_story_context(parse_json_object(raw))
        _write_json(out, {"schema": "video_image_solution_1", "input_hash": input_hash, "context": context, "fallback_used": False})
        on_log("  OK story visual context ready for highlight selection")
        return context
    except Exception as exc:
        _write_json(out, {"schema": "video_image_solution_1", "input_hash": input_hash, "context": fallback, "fallback_used": True, "error": str(redact_secret_text(exc))})
        on_log(f"  WARN story context analysis failed: {redact_secret_text(exc)}; using local narration only")
        return fallback


def _image_route_settings(role: str) -> dict:
    role = str(role or "scene")

    def request_size() -> tuple[int, int]:
        if not _unified_ai_enabled() or role == "character_reference":
            return 0, 0
        return (
            int(config.get("ai_api_image_width", 1792) or 1792),
            int(config.get("ai_api_image_height", 1008) or 1008),
        )

    def workflow_for(provider: str, value: str) -> str:
        return str(value or "") if str(provider or "").strip().lower() == "comfyui" else ""

    if role == "scene":
        provider = str(config.image_provider or "placeholder")
        provider, base_url, api_key, model = _apply_unified_image_api(
            provider,
            str(config.image_base_url or ""),
            str(config.image_api_key or ""),
            str(config.image_model or ""),
        )
        request_width, request_height = request_size()
        return {
            "provider": provider,
            "base_url": base_url,
            "api_key": api_key,
            "model": model,
            "workflow_path": workflow_for(provider, str(config.image_workflow or "")),
            "request_width": request_width,
            "request_height": request_height,
        }

    if role == "cover":
        provider = str(config.get("cover_provider", "same_as_image") or "").strip()
        if provider in {"", "same_as_image", "同配图", "同图片"}:
            return _image_route_settings("scene")
        provider, base_url, api_key, model = _apply_unified_image_api(
            provider,
            str(config.get("cover_base_url", "") or config.image_base_url or ""),
            str(config.get("cover_api_key", "") or config.image_api_key or ""),
            str(config.get("cover_model", "") or config.image_model or ""),
        )
        request_width, request_height = request_size()
        return {
            "provider": provider,
            "base_url": base_url,
            "api_key": api_key,
            "model": model,
            "workflow_path": workflow_for(provider, str(config.image_workflow or "")),
            "request_width": request_width,
            "request_height": request_height,
        }

    if role == "character_reference":
        provider = str(config.get("character_reference_provider", "same_as_image") or "").strip()
        if provider in {"", "same_as_image", "同配图", "同图片"}:
            route = _image_route_settings("scene")
            # Character sheets retain their configured portrait/square target;
            # the unified landscape request size is only for scenes and covers.
            route["request_width"] = 0
            route["request_height"] = 0
            return route
        provider, base_url, api_key, model = _apply_unified_image_api(
            provider,
            str(config.get("character_reference_base_url", "") or config.image_base_url or ""),
            str(config.get("character_reference_api_key", "") or config.image_api_key or ""),
            str(config.get("character_reference_model", "") or config.image_model or ""),
        )
        request_width, request_height = request_size()
        return {
            "provider": provider,
            "base_url": base_url,
            "api_key": api_key,
            "model": model,
            "workflow_path": workflow_for(provider, str(config.get("character_reference_workflow", "") or config.image_workflow or "")),
            "request_width": request_width,
            "request_height": request_height,
        }

    if role == "scene_reference":
        provider = str(config.get("scene_reference_provider", "same_as_image") or "").strip()
        if provider in {"", "same_as_image", "同配图", "同图片"}:
            return _image_route_settings("scene")
        provider, base_url, api_key, model = _apply_unified_image_api(
            provider,
            str(config.get("scene_reference_base_url", "") or config.image_base_url or ""),
            str(config.get("scene_reference_api_key", "") or config.image_api_key or ""),
            str(config.get("scene_reference_model", "") or config.image_model or ""),
        )
        request_width, request_height = request_size()
        return {
            "provider": provider,
            "base_url": base_url,
            "api_key": api_key,
            "model": model,
            "workflow_path": workflow_for(provider, str(config.get("scene_reference_workflow", "") or config.image_workflow or "")),
            "request_width": request_width,
            "request_height": request_height,
        }

    return _image_route_settings("scene")


def _make_image_backend(route: dict, *, provider: str | None = None) -> ImageBackend:
    return ImageBackend(
        provider=provider or route["provider"],
        base_url=route.get("base_url", ""),
        api_key=route.get("api_key", ""),
        model=route.get("model", ""),
        steps=config.image_steps,
        cfg=config.image_cfg,
        workflow_path=route.get("workflow_path", ""),
        timeout_seconds=config.get("image_api_timeout_seconds", 300),
        request_width=route.get("request_width", 0),
        request_height=route.get("request_height", 0),
    )


def _safe_artifact_name(value: str, fallback: str = "artifact") -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", str(value or "")).strip("._-")
    return (cleaned or fallback)[:80]


def _reference_images_for_text(analysis: dict | None, text: str, *, max_count: int = 2) -> list[Path]:
    if not analysis or not analysis.get("enabled"):
        return []
    characters = analysis.get("characters")
    if not isinstance(characters, list):
        return []
    haystack = str(text or "")
    protagonist_names = set(str(x).strip() for x in (analysis.get("protagonists") or []) if str(x).strip())
    matched: list[dict] = []
    for item in characters:
        if not isinstance(item, dict):
            continue
        names = [str(item.get("name") or "").strip()]
        aliases = item.get("aliases")
        if isinstance(aliases, list):
            names.extend(str(x).strip() for x in aliases)
        if any(name and name in haystack for name in names):
            matched.append(item)
    if (
        bool(config.get("character_analysis_always_include_protagonists", True))
        and not bool(config.get("storyboard_highlight_enabled", True))
    ):
        for item in characters:
            if not isinstance(item, dict):
                continue
            if str(item.get("name") or "").strip() in protagonist_names and item not in matched:
                matched.append(item)
            if len(matched) >= max_count:
                break

    paths: list[Path] = []
    for item in matched[: max(1, int(max_count or 1))]:
        ref = str(item.get("reference_image") or "").strip()
        if ref:
            path = Path(ref)
            if path.exists():
                paths.append(path)
    return paths


def stage_character_references(
    character_analysis: dict | None,
    job_dir: Path,
    on_log: LogFn = _noop,
) -> dict | None:
    if not character_analysis or not character_analysis.get("enabled"):
        return character_analysis
    if not bool(config.get("character_reference_enabled", False)):
        return character_analysis

    characters = character_analysis.get("characters")
    if not isinstance(characters, list) or not characters:
        return character_analysis

    route = _image_route_settings("character_reference")
    provider = route["provider"]
    width = int(config.get("character_reference_width", 768) or 768)
    height = int(config.get("character_reference_height", 1024) or 1024)
    max_count = max(1, int(config.get("character_reference_max_count", 6) or 6))
    timeout_seconds = _bounded_timeout(config.get("image_api_timeout_seconds", 300), 300.0)
    status = _read_json(job_dir / "status.json", {})
    project_id = str(status.get("project_id") or "").strip()
    project = projects.load_project(project_id) if project_id else {}
    char_dir = (
        projects.project_dir(project_id) / "characters"
        if project
        else job_dir / "characters"
    )
    char_dir.mkdir(parents=True, exist_ok=True)

    backend = _make_image_backend(route)
    manifest: list[dict] = []
    selected = [
        c for c in characters
        if isinstance(c, dict) and str(c.get("importance") or "") in {"protagonist", "supporting"}
    ][:max_count]
    if not selected:
        selected = [c for c in characters if isinstance(c, dict)][:max_count]

    on_log(f"[4/6] character references: provider={provider} count={len(selected)} size={width}x{height}")
    for i, character in enumerate(selected):
        name = str(character.get("name") or f"character_{i + 1}").strip()
        trigger = str(character.get("trigger") or f"char_{i + 1:02d}").strip()
        out = char_dir / f"{_safe_artifact_name(trigger or name, f'char_{i + 1:02d}')}.png"
        prompt = character_reference_prompt(character, character_analysis)
        suffix = str(config.get("character_reference_prompt_suffix", "") or "").strip()
        if suffix:
            prompt = f"{prompt}. {suffix}"
        prompt = _policy_safe_image_prompt(prompt, fallback_text=name)
        character["reference_image"] = str(out)
        character["reference_prompt"] = prompt
        manifest.append({"name": name, "trigger": trigger, "path": str(out), "prompt": prompt})
        lock_context = (
            projects.character_reference_lock(project_id, trigger or name)
            if project
            else nullcontext()
        )
        with lock_context:
            if out.exists() and out.stat().st_size > 100:
                on_log(f"  character reference reuse {name}: {out.name}")
                continue
            try:
                if provider == "placeholder":
                    backend.generate(prompt, config.llm_negative_prompt, out, width=width, height=height)
                else:
                    with external_api_slot(action=f"character reference {name}", timeout_seconds=timeout_seconds):
                        backend.generate(prompt, config.llm_negative_prompt, out, width=width, height=height)
                on_log(f"  character reference {i + 1}/{len(selected)} OK: {name} -> {out.name}")
            except Exception as exc:
                on_log(f"  WARN character reference failed for {name}: {exc}")

    if project:
        projects.write_character_reference_manifest(project_id, manifest)
        on_log(f"  project character references saved: {project.get('name') or project_id}")
    _write_json(job_dir / "character_reference_manifest.json", manifest)
    _write_json(job_dir / "character_profiles.json", character_analysis)
    if project:
        projects.merge_character_profiles(project_id, character_analysis)
    return character_analysis


_POLICY_REPLACEMENTS = [
    (r"\b(severed limbs?|dismember(?:ed|ment)?|guts?|corpse|corpses|dead bod(?:y|ies)|rivers? of blood)\b", "dramatic aftermath implied by scattered shadows"),
    (r"\b(bloody|blood-soaked|blood splatter|gore|graphic violence|massacre)\b", "tense dramatic atmosphere"),
    (r"\b(kill(?:ing)?|murder(?:ing)?|slaughter(?:ing)?|stab(?:bing|bed)?|tortur(?:e|ing)|behead(?:ing|ed)?)\b", "confronting in a tense scene"),
    (r"\b(nude|naked|sexual|erotic|seductive|intimate bed scene|rape|assault)\b", "emotional conflict"),
    (r"\b(teenage boy|teenage girl|schoolboy|schoolgirl|minor child|underage)\b", "young adult"),
    (r"\b(sword through|pinned by a sword|knife at|gun at|weapon close-up)\b", "symbolic confrontation"),
    (r"\b(victim|body|bodies)\b", "character"),
]


def _policy_safe_image_prompt(prompt: str, *, fallback_text: str = "", allow_title_text: bool = False) -> str:
    value = " ".join(str(prompt or fallback_text or "").split()).strip()
    if not value:
        value = "Cinematic anime illustration of a tense emotional novel scene, expressive faces, soft lighting"
    for pattern, replacement in _POLICY_REPLACEMENTS:
        value = re.sub(pattern, replacement, value, flags=re.I)
    value = re.sub(r"(?i)\b(blood|gore|corpse|nude|naked|sexual|erotic|severed)\b", "", value)
    value = re.sub(r"\s{2,}", " ", value).strip(" ,.;")
    if allow_title_text:
        guard = (
            "Family-friendly cinematic cover illustration, adult-looking characters, symbolic emotional drama, "
            "fully clothed characters, clean composition, subject-appropriate high-contrast lighting, readable faces, only the requested Japanese poster text, "
            "no extra captions, logos, UI, or watermarks"
        )
    else:
        guard = (
            "Family-friendly cinematic anime illustration, adult-looking characters, symbolic emotional drama, "
            "fully clothed characters, clean composition, soft lighting, readable faces, text-free artwork"
        )
    if "family-friendly" not in value.lower():
        value = f"{value}. {guard}"
    if not allow_title_text:
        # Keep this at the end of every non-cover request so a profile's
        # no-readable-text instruction survives prompt length limiting.
        text_free_guard = (
            " Strict final requirement: generate text-free artwork only. Do not render any readable or pseudo-readable "
            "writing, letters, words, Japanese or Chinese characters, numbers, signage, document pages, speech bubbles, "
            "captions, typography, UI, logos, watermarks, symbols resembling glyphs, or text-like magical effects; use "
            "blank surfaces and abstract light instead."
        )
        # Make prompt hardening idempotent.  In particular, strip an existing
        # protected suffix before calculating the head/tail budget, otherwise
        # a second pass mistakes the suffix for the scene-specific tail.
        text_free_guard_core = text_free_guard.strip().rstrip(".")
        value = re.sub(
            rf"(?:{re.escape(text_free_guard_core)}[.\s]*)+",
            "",
            value,
            flags=re.I,
        ).strip(" ,.;")
        max_prompt_chars = 1400
        # Preserve both the global visual lock at the beginning and the selected
        # scene/action at the end.
        budget = max_prompt_chars - len(text_free_guard)
        if len(value) > budget:
            # Keep enough of the beginning to retain the profile's historical
            # setting (for example, Chinese late-Han costumes) before keeping
            # the final scene-specific instruction below.
            # Reserve a real scene budget.  The previous calculation usually
            # left only ~120 characters at 1400 total, so a later pass could
            # shave the beginning off even a short concrete action anchor.
            desired_tail = min(400, max(280, budget // 3))
            head = max(500, budget - desired_tail - 24)
            tail = max(240, budget - head - 24)
            value = f"{value[:head].rstrip(' ,.;')}. Scene details: {value[-tail:].lstrip(' ,.;')}"
        value = value.rstrip(" ,.;") + text_free_guard
    # Scene prompts stay compact for SD-style backends.  Covers need room for
    # the event, two reacting characters, palette, composition and four text
    # hierarchy instructions; clipping them to 1400 characters previously
    # produced visibly truncated prompts such as "(masterpiece:1.".
    if allow_title_text:
        if len(value) <= 6000:
            return value
        # Preserve the final typography checks instead of slicing them off
        # when a detailed poster method produces a long prompt.
        return f"{value[:5200].rstrip(' ,.;')}. Final constraints: {value[-760:].lstrip(' ,.;')}"[:6000]
    return value[:1400]


def _is_image_policy_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return any(
        item in text
        for item in (
            "content policy",
            "policy",
            "safety",
            "防护",
            "违反",
            "裸露",
            "色情",
            "情色",
            "暴力",
            "no b64_json/url",
            "can't generate",
            "cannot generate",
        )
    )


def _image_cache_metadata_path(image_path: Path) -> Path:
    return image_path.with_suffix(image_path.suffix + ".cache.json")


def _image_cache_key(
    *,
    route: dict,
    prompt: str,
    negative_prompt: str,
    width: int,
    height: int,
    reference_paths: list[Path],
) -> str:
    """Fingerprint everything that affects a generated scene image.

    API keys are intentionally excluded: rotating credentials should not throw
    away a previously successful image, while provider/endpoint/model changes
    must never reuse an image produced by a different backend.
    """
    return _stable_hash(
        {
            # Request dimensions are part of the fingerprint below, so a real
            # API-size change invalidates only the affected generated images.
            "schema": 5,
            "provider": str(route.get("provider") or "").strip().lower(),
            "base_url": str(route.get("base_url") or "").strip().rstrip("/"),
            "model": str(route.get("model") or "").strip(),
            "request_width": int(route.get("request_width") or 0),
            "request_height": int(route.get("request_height") or 0),
            "prompt_hash": _text_hash(prompt),
            "negative_prompt_hash": _text_hash(negative_prompt),
            "width": int(width),
            "height": int(height),
            "references": [
                {"path": str(path), "signature": _file_signature(path)}
                for path in reference_paths
            ],
        }
    )


def _valid_scene_images(job_dir: Path) -> list[tuple[int, Path]]:
    images: list[tuple[int, Path]] = []
    for path in sorted((job_dir / "images").glob("img_*.png")):
        try:
            index = int(path.stem.removeprefix("img_"))
            if path.stat().st_size >= 100:
                images.append((index, path))
        except (OSError, ValueError):
            continue
    return images


def image_fallback_info(job_id: str) -> dict:
    """Report whether a partial image cache can finish a video without AI calls."""
    job_dir = _safe_job_path(job_id)
    rows = _read_json(job_dir / "plans.json", [])
    total = len(rows) if isinstance(rows, list) else 0
    available = _valid_scene_images(job_dir)
    existing_indexes = {index for index, _ in available}
    return {
        "available_images": len(available),
        "total_images": total,
        "needs_fallback": bool(total and available and any(i not in existing_indexes for i in range(total))),
    }


def tts_completion_info(job_id: str) -> dict:
    """Check whether compose-only has all narration audio it needs."""
    job_dir = _safe_job_path(job_id)
    segments = _load_job_segments(job_dir)
    durations = _read_json(job_dir / "durations.json", [])
    complete = bool(
        segments
        and isinstance(durations, list)
        and len(durations) == len(segments)
        and all(isinstance(value, (int, float)) and float(value) > 0.05 for value in durations)
    )
    imported = _job_imported_audio(job_dir)
    if imported is not None:
        complete = complete and imported[0].exists() and _audio_duration(imported[0]) > 0.05
    elif complete:
        complete = all((job_dir / "audio" / f"seg_{i:05d}.mp3").exists() for i in range(len(segments)))
    return {"complete": complete, "segments": len(segments)}


def _image_fallback_mode(job_dir: Path) -> str:
    selection = _read_json(job_dir / IMAGE_SELECTION_FILE, {})
    mode = str(selection.get("mode") or "") if isinstance(selection, dict) else ""
    return mode if mode in {"cycle", "hold_last"} else ""


def set_image_fallback_selection(job_id: str, mode: str) -> dict:
    """Persist a no-API image reuse plan for normal and compose-only runs."""
    if mode not in {"cycle", "hold_last"}:
        raise ValueError(f"unsupported image fallback mode: {mode}")
    job_dir = _safe_job_path(job_id)
    info = image_fallback_info(job_id)
    total = int(info["total_images"])
    available = _valid_scene_images(job_dir)
    if not total or not available:
        raise RuntimeError("没有足够的分镜或已有图片，无法复用图片继续合成。")
    by_index = dict(available)
    reusable = [path for _, path in available]
    last_path = available[-1][1]
    result = [
        by_index[i] if i in by_index else (last_path if mode == "hold_last" else reusable[i % len(reusable)])
        for i in range(total)
    ]
    selection = {
        "mode": mode,
        "available_images": len(available),
        "total_images": total,
        "paths": [str(path.relative_to(job_dir)) for path in result],
        "chosen_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    _write_json(job_dir / IMAGE_SELECTION_FILE, selection)
    request_path = job_dir / IMAGE_FAILURE_DECISION_FILE
    request = _read_json(request_path, {})
    if isinstance(request, dict) and request.get("status") == "awaiting_choice":
        request["decision"] = mode
        request["decided_at"] = selection["chosen_at"]
        _write_json(request_path, request)
    return selection


def _wait_for_image_failure_choice(job_dir: Path, plans: list[ImagePlan], failure: ImageGenerationFailed, on_log: LogFn) -> list[Path]:
    """Let the GUI choose how to reuse saved images without another API call."""
    available = _valid_scene_images(job_dir)
    if not available:
        raise RuntimeError(f"图{failure.index + 1}生成失败，且没有可复用的已生成图片：{failure.error}")
    request_path = job_dir / IMAGE_FAILURE_DECISION_FILE
    request = {
        "status": "awaiting_choice",
        "failed_image_index": failure.index,
        "available_images": len(available),
        "total_images": len(plans),
        "error": failure.error,
        "requested_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    _write_json(request_path, request)
    write_status(
        job_dir,
        stage="image_decision",
        image_failure=request,
        error="",
    )
    on_log(
        f"  image generation paused after failure; {len(available)}/{len(plans)} usable images saved. "
        "Waiting for a choice in the desktop app."
    )
    while True:
        time.sleep(0.5)
        decision = _read_json(request_path, {})
        mode = str(decision.get("decision") or "").strip().lower() if isinstance(decision, dict) else ""
        if mode == "skip":
            request["status"] = "skipped"
            _write_json(request_path, request)
            raise ImageGenerationSkipped("图片生成失败后由用户跳过；任务保留，稍后可手动处理。")
        if mode not in {"cycle", "hold_last"}:
            continue
        selection = set_image_fallback_selection(job_dir.name, mode)
        request["status"] = "chosen"
        request["decision"] = mode
        _write_json(request_path, request)
        on_log(f"  image fallback selected: {mode}; continuing composition without more image API calls")
        return [job_dir / str(path) for path in selection["paths"]]


def stage_storyboard_and_image(
    plans: list[ImagePlan],
    job_dir: Path,
    character_analysis: dict | None = None,
    story_context: dict | None = None,
    on_log: LogFn = _noop,
    on_prog: ProgFn = _noop,
) -> list[Path]:
    workers = _parallel_limit("max_parallel_images", 2, len(plans), hard_cap=8)
    image_timeout_seconds = _bounded_timeout(config.get("image_api_timeout_seconds", 300), 300.0)
    scene_route = _image_route_settings("scene")
    scene_reference_route = _image_route_settings("scene_reference")
    llm_route = _llm_route_settings()
    on_log(
        f"[5/6] 分镜+出图: llm={llm_route['provider']} image={scene_route['provider']} "
        f"parallel={workers} timeout={image_timeout_seconds:.0f}s"
    )

    def make_llm() -> LLMBackend:
        return LLMBackend(
            provider=llm_route["provider"],
            base_url=llm_route["base_url"],
            api_key=llm_route["api_key"],
            model=llm_route["model"],
            system_prompt=config.llm_storyboard_prompt,
            style_suffix=config.llm_image_style_suffix,
        )

    def make_image_backend(provider: str | None = None) -> ImageBackend:
        return _make_image_backend(scene_route, provider=provider)

    def make_scene_reference_backend() -> ImageBackend:
        return _make_image_backend(scene_reference_route)

    img_dir = job_dir / "images"
    img_dir.mkdir(parents=True, exist_ok=True)
    prompts_log: list[dict | None] = [None] * len(plans)
    paths: list[Path | None] = [None] * len(plans)
    write_lock = threading.Lock()
    scene_negative_prompt = str(config.llm_negative_prompt or "").strip(" ,")

    def process_image(i: int, plan: ImagePlan) -> tuple[int, Path, str]:
        out = img_dir / f"img_{i:05d}.png"
        base_prompt, highlight = _prompt_for_plan(make_llm(), plan, story_context or {}, on_log, i)
        grounding_text = " ".join([
            str(highlight.get("highlight_text") or ""),
            " ".join(str(x) for x in (highlight.get("people") or [])),
            str(highlight.get("location") or ""),
            str(highlight.get("action") or ""),
        ])
        plan.highlight_segment_indexes = list(highlight.get("segment_indexes") or [])
        plan.highlight_text = str(highlight.get("highlight_text") or "")
        plan.highlight_people = list(highlight.get("people") or [])
        plan.highlight_location = str(highlight.get("location") or "")
        plan.highlight_action = str(highlight.get("action") or "")
        theme_context = visual_theme_context(character_analysis) if bool(config.get("scene_inject_visual_theme", True)) else ""
        character_context = character_context_for_text(
            character_analysis,
            grounding_text or plan.text,
            max_characters=int(config.get("character_analysis_max_characters_per_prompt", 4) or 4),
            # In highlight mode, a protagonist absent from the selected moment must not be invented.
            always_include_protagonists=(
                bool(config.get("character_analysis_always_include_protagonists", True))
                and not bool(config.get("storyboard_highlight_enabled", True))
            ),
        ) if bool(config.get("scene_inject_character_triggers", True)) else ""
        if theme_context and theme_context not in character_context:
            base_prompt = f"{base_prompt}. {theme_context}"
        if character_context:
            base_prompt = f"{base_prompt}. {character_context}"
        prompt = _policy_safe_image_prompt(base_prompt, fallback_text=plan.text)
        reference_paths = _reference_images_for_text(
            character_analysis,
            grounding_text or plan.text,
            max_count=int(config.get("scene_reference_max_images", 2) or 2),
        ) if bool(config.get("scene_reference_enabled", False)) else []
        prompts_log[i] = {
            "index": i,
            "text": plan.text,
            "solution": "视频生图解决方案1",
            "story_context": story_context or {},
            "highlight_segment_indexes": plan.highlight_segment_indexes,
            "highlight_text": plan.highlight_text,
            "highlight_people": plan.highlight_people,
            "highlight_location": plan.highlight_location,
            "highlight_action": plan.highlight_action,
            "excluded_people": highlight.get("excluded_people") or [],
            "highlight_fallback_used": bool(highlight.get("fallback_used")),
            "prompt": prompt,
            "theme_context": theme_context,
            "character_context": character_context,
            "reference_images": [str(p) for p in reference_paths],
        }
        cache_path = _image_cache_metadata_path(out)
        generation_route = (
            scene_reference_route
            if reference_paths and scene_route["provider"] != "placeholder"
            else scene_route
        )
        cache_key = _image_cache_key(
            route=generation_route,
            prompt=prompt,
            negative_prompt=scene_negative_prompt,
            width=config.image_width,
            height=config.image_height,
            reference_paths=reference_paths,
        )
        cache_meta = _read_json(cache_path, {})
        expected_status = "placeholder" if generation_route["provider"] == "placeholder" else "success"
        reusable = (
            out.exists()
            and out.stat().st_size >= 100
            and isinstance(cache_meta, dict)
            and cache_meta.get("status") == expected_status
            and cache_meta.get("cache_key") == cache_key
        )
        if not reusable:
            if out.exists():
                reason = "missing/invalid metadata" if not cache_meta else "route, prompt, or source changed"
                on_log(f"  image {i + 1}/{len(plans)} invalidate cached file: {reason}")
            out.unlink(missing_ok=True)
            cache_path.unlink(missing_ok=True)
            last_exc: Exception | None = None
            try:
                image_attempts = int(config.get("image_retry_attempts", config.get("pipeline_failure_retry_limit", 5)) or 5)
            except (TypeError, ValueError):
                image_attempts = 5
            image_attempts = max(1, min(10, image_attempts))
            for attempt in range(1, image_attempts + 1):
                try:
                    # A backend may create a partial output before raising.  It
                    # must not survive into the next attempt or a resumed job.
                    out.unlink(missing_ok=True)
                    on_log(
                        f"  image {i + 1}/{len(plans)} call provider={scene_route['provider']} "
                        f"base={scene_route.get('base_url') or '-'} model={scene_route.get('model') or '-'} "
                        f"size={config.image_width}x{config.image_height} attempt={attempt}"
                    )
                    attempt_prompt = prompt
                    if attempt > 1 and last_exc is not None and _is_image_policy_error(last_exc):
                        attempt_prompt = _policy_safe_image_prompt(
                            "A calm symbolic scene showing the same story mood through facial expressions, "
                            "distance, lighting, and environment; no direct harm or explicit content.",
                            fallback_text=plan.text,
                        )
                        if isinstance(prompts_log[i], dict):
                            prompts_log[i]["prompt"] = attempt_prompt
                            prompts_log[i]["safe_retry"] = True
                        on_log(f"  image {i + 1}/{len(plans)} retry with safer prompt")
                    if scene_route["provider"] == "placeholder":
                        make_image_backend("placeholder").generate(
                            attempt_prompt,
                            scene_negative_prompt,
                            out,
                            width=config.image_width,
                            height=config.image_height,
                        )
                    else:
                        with external_api_slot(
                            action=f"image {i + 1}/{len(plans)}",
                            timeout_seconds=image_timeout_seconds,
                        ):
                            if reference_paths:
                                make_scene_reference_backend().generate_with_references(
                                    attempt_prompt,
                                    scene_negative_prompt,
                                    reference_paths,
                                    out,
                                    width=config.image_width,
                                    height=config.image_height,
                                )
                            else:
                                make_image_backend().generate(
                                    attempt_prompt,
                                    scene_negative_prompt,
                                    out,
                                    width=config.image_width,
                                    height=config.image_height,
                                )
                    if not out.exists() or out.stat().st_size < 100:
                        raise RuntimeError("image backend returned no valid output file")
                    _write_json(
                        cache_path,
                        {
                            "schema": 1,
                            "status": expected_status,
                            "cache_key": cache_key,
                            "provider": generation_route["provider"],
                            "base_url": generation_route.get("base_url", ""),
                            "model": generation_route.get("model", ""),
                            "file": _file_signature(out),
                        },
                    )
                    break
                except Exception as exc:
                    last_exc = exc
                    out.unlink(missing_ok=True)
                    on_log(f"  WARN 图{i} 生成失败 {attempt}/{image_attempts}: {exc}")
                    time.sleep(attempt)
            if not out.exists() or out.stat().st_size < 100:
                error_text = redact_secret_text(last_exc or "unknown image generation failure", limit=800)
                _write_json(
                    cache_path,
                    {
                        "schema": 1,
                        "status": "failed",
                        "cache_key": cache_key,
                        "provider": generation_route["provider"],
                        "base_url": generation_route.get("base_url", ""),
                        "model": generation_route.get("model", ""),
                        "error": error_text,
                    },
                )
                # Never silently turn an API failure into a completed color-
                # screen video.  Leave a retryable failure marker and stop.
                raise ImageGenerationFailed(i, error_text)
        else:
            on_log(f"  image {i + 1}/{len(plans)} reuse cached file: {out}")
        return i, out, prompt

    completed = 0
    pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="image")
    try:
        future_labels = {pool.submit(process_image, i, plan): i for i, plan in enumerate(plans)}
        pending = set(future_labels)
        while pending:
            done, pending = wait(pending, timeout=30.0, return_when=FIRST_COMPLETED)
            if not done:
                waiting = ", ".join(f"image {future_labels[f] + 1}" for f in list(pending)[:4])
                more = "" if len(pending) <= 4 else f", +{len(pending) - 4} more"
                on_log(f"  image still running: {waiting}{more} ({completed}/{len(plans)} done)")
                on_prog(completed / max(1, len(plans)))
                continue
            for future in done:
                i, out, prompt = future.result()
                paths[i] = out
                completed += 1
                with write_lock:
                    _write_json(job_dir / "prompts.json", [p for p in prompts_log if p is not None])
                on_log(f"  image progress {completed}/{len(plans)}: {prompt[:80]}...")
                on_prog(completed / max(1, len(plans)))
    except ImageGenerationFailed as failure:
        # Do not wait for queued work after a failure: the GUI can decide to
        # finish with files already on disk, avoiding another full restart.
        for future in future_labels:
            future.cancel()
        pool.shutdown(wait=False, cancel_futures=True)
        if job_dir.name == "_prefetch_images":
            raise RuntimeError(f"图{failure.index + 1} 生成失败：{failure.error}") from failure
        if bool(config.get("pipeline_skip_after_failures", True)):
            raise RuntimeError(
                f"图{failure.index + 1} 连续生成失败达到上限；已跳过当前任务并继续队列：{failure.error}"
            ) from failure
        return _wait_for_image_failure_choice(job_dir, plans, failure, on_log)
    except Exception:
        for future in future_labels:
            future.cancel()
        pool.shutdown(wait=False, cancel_futures=True)
        raise
    else:
        pool.shutdown(wait=True)

    final_paths = [p for p in paths if p is not None]
    if len(final_paths) != len(plans):
        raise RuntimeError("Image stage did not produce all images")
    on_log(f"  OK {len(final_paths)} 张图")
    return final_paths


def _metadata_intro(novel: Novel, story_context: dict | None = None, limit: int = 700) -> str:
    """Return a compact, grounded title brief without re-sending the full novel."""
    if isinstance(story_context, dict):
        brief = re.sub(r"\s+", " ", str(story_context.get("title_brief") or "")).strip()
        if brief:
            return brief[:limit]
        summary = re.sub(r"\s+", " ", str(story_context.get("story_summary") or "")).strip()
        if summary:
            return summary[:limit]
    desc = re.sub(r"\s+", " ", str(novel.description or "")).strip()
    if desc and desc.lower() != "local text input":
        return desc[:limit]
    text = str(novel.full_text or "")
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if lines and lines[0].strip() == str(novel.title or "").strip():
        lines = lines[1:]
    joined = " ".join(lines)
    joined = re.sub(r"\s+", " ", joined).strip()
    return joined[:limit]


_AUTHOR_SUFFIX_RE = re.compile(r"\s*[\(（【\[][^()（）【】\[\]]{1,24}[\)）】\]]\s*$")


def _clean_display_title(title: str) -> str:
    """Remove trailing author/source hints such as `Title(author)` from public titles."""
    value = re.sub(r"\s+", " ", str(title or "")).strip()
    previous = None
    while value and value != previous:
        previous = value
        value = _AUTHOR_SUFFIX_RE.sub("", value).strip()
    return value or str(title or "").strip() or "Untitled"


def _clean_short_title(text: str, min_chars: int, max_chars: int) -> str:
    value = html.unescape(str(text or ""))
    value = re.sub(r"[《》“”\"'`#【】\[\]（）()]", "", value)
    value = re.sub(r"(?i)^(短说明标题|标题|short title)[:：]\s*", "", value.strip())
    value = re.sub(r"\s+", "", value)
    value = re.sub(r"[，。！？；、,.!?;：:]+$", "", value)
    if len(value) > max_chars:
        value = value[:max_chars]
        value = re.sub(r"[，。！？；、,.!?;：:]+$", "", value)
    return value[:max_chars]


def _fallback_short_title(novel: Novel, intro: str, min_chars: int, max_chars: int) -> str:
    text = re.sub(r"\s+", "", intro or novel.full_text or novel.title or "")
    text = re.sub(r"^(简介|内容简介|小说简介|作品简介|书籍简介)[:：]?", "", text)
    text = re.sub(r"[《》“”\"'`#【】\[\]（）()]", "", text)
    title = re.sub(r"\s+", "", _clean_display_title(novel.title))
    if title and text.startswith(title):
        text = text[len(title):]
    parts = re.split(r"[。！？!?；;]", text)
    value = ""
    for part in parts:
        part = re.sub(r"[，、,:：]+", "", part.strip())
        if not part:
            continue
        value += part
        if len(value) >= min_chars:
            break
    if not value:
        value = title or "精彩小说推文剧情解说"
    if len(value) < min_chars and title and title not in value:
        value = f"{title}{value}"
    return _clean_short_title(value, min_chars, max_chars) or (title[:max_chars] if title else "精彩小说推文剧情解说")


def _candidate_title(value: str) -> str:
    """Clean model wrappers without deleting useful Japanese ad punctuation."""
    text = html.unescape(str(value or ""))
    text = re.sub(r"^\s*(?:[①②③]|[1-3][.、:：)])\s*", "", text)
    text = re.sub(r"(?i)^\s*(?:動画タイトル|タイトル|title)\s*[:：]\s*", "", text)
    return re.sub(r"\s+", " ", text).strip().strip('"')


def _complete_title_within_limit(value: str, max_chars: int) -> str:
    """Keep only a semantically complete title clause; never cut mid-clause."""
    text = _candidate_title(value).strip()
    if len(text) <= max_chars:
        return text
    prefix = text[:max_chars]
    marks = "。！？!?；;、，,：:！!？?"
    positions = [prefix.rfind(mark) for mark in marks]
    cut = max(positions, default=-1)
    if cut >= 0:
        return prefix[: cut + 1].rstrip("、，,：:")
    return ""


def _strip_series_part_prefix(value: str) -> str:
    """Remove an older series marker before the current one is applied."""
    text = _candidate_title(value)
    patterns = (
        r"^[^｜]{1,32}｜(?:上篇|中篇|下篇|第\d+話|前編|後編|第\d+編)｜",
        r"^.{1,32}【(?:上篇|中篇|下篇|第\d+話|前編|後編|第\d+編)】",
    )
    changed = True
    while changed:
        changed = False
        for pattern in patterns:
            if re.match(pattern, text):
                text = re.sub(pattern, "", text, count=1).strip()
                changed = True
    return text


_MARKETING_SOURCE_WRAPPERS = ("标题：", "タイトル：", "简介：", "概要：", "local text input", "[开头]", "[中段]", "[结尾]")

# A formatting/API outage must never turn a historical episode into an
# isekai-romance upload.  These are deliberately narrow signals: they only
# activate when the source is clearly Three Kingdoms material.
_THREE_KINGDOMS_MARKERS = (
    "三国", "三國", "三国志", "三國志", "曹操", "劉備", "刘备", "呂布", "吕布",
    "関羽", "关羽", "張飛", "张飞", "孫策", "孙策", "董卓", "袁紹", "袁绍",
)
_THREE_KINGDOMS_WRONG_TAGS = {
    "#異世界", "#恋愛", "#ざまぁ", "#貴族令嬢", "#ファンタジー", "#漫画風",
}


def _is_three_kingdoms_material(*values) -> bool:
    text = " ".join(str(value or "") for value in values)
    return sum(marker in text for marker in _THREE_KINGDOMS_MARKERS) >= 2 or "三国" in text or "三國" in text


def _fallback_marketing_tags(novel: Novel, story_material: str) -> list[str]:
    """Return conservative tags derived from the actual source category."""
    if _is_three_kingdoms_material(novel.title, novel.full_text, story_material):
        return _three_kingdoms_fallback_tags()
    # Do not guess genre, romance, gender, or setting when the LLM response is
    # unavailable.  Broad format tags are less promotional, but remain true.
    return ["#小説", "#朗読", "#物語", "#オーディオブック", "#ストーリー", "#小説紹介", "#日本語", "#動画", "#文学", "#長編"]


def _three_kingdoms_fallback_tags() -> list[str]:
    """Stable, source-true tags for a Three Kingdoms recovery path."""
    return [
        "#三国志", "#三国志演義", "#中国史", "#中国古代史", "#歴史",
        "#歴史物語", "#歴史小説", "#群雄割拠", "#武将", "#軍師",
        "#戦記", "#朗読", "#物語",
    ]


def _safe_generated_tags_for_upload(tags, *source_values) -> list[str]:
    """Repair legacy/bad fallback tags before an upload can use them."""
    normalized = _candidate_tags(tags)
    if _is_three_kingdoms_material(*source_values):
        has_wrong_tag = any(tag in _THREE_KINGDOMS_WRONG_TAGS for tag in normalized)
        has_anchor = any("三国" in tag or "三國" in tag for tag in normalized)
        if has_wrong_tag or not has_anchor:
            return _three_kingdoms_fallback_tags()
    return normalized


def _contains_marketing_source_wrapper(value: str) -> bool:
    text = str(value or "").lower()
    return any(marker.lower() in text for marker in _MARKETING_SOURCE_WRAPPERS)


def _candidate_synopsis(value: str) -> str:
    text = html.unescape(str(value or ""))
    text = re.sub(r"^\s*(?:[①②]|[1-2][.、:：)])\s*", "", text)
    text = re.sub(r"(?i)^\s*(?:あらすじ|概要|synopsis)\s*[:：]\s*", "", text)
    return re.sub(r"\s+", " ", text).strip().strip('"')


def _candidate_tags(value) -> list[str]:
    rows = value if isinstance(value, list) else re.findall(r"#[^#\s【】]+", str(value or ""))
    tags: list[str] = []
    for raw in rows:
        tag = re.sub(r"\s+", "", str(raw or "").strip())
        tag_name = tag.lstrip("#＃").strip("【】[]")
        if not tag or re.sub(r"[・·]", "", tag_name) == "朗読小説":
            continue
        if not tag.startswith("#"):
            tag = "#" + tag.lstrip("＃")
        if tag.lstrip("#＃") == "赤陽の勧めるノベル":
            continue
        if tag not in tags:
            tags.append(tag)
    return tags[:15]


def _parse_marketing_candidates(raw) -> dict:
    data = raw if isinstance(raw, dict) else parse_json_object(str(raw or ""))
    titles_raw = data.get("titles") or data.get("video_titles") or data.get("動画タイトル") or []
    synopses_raw = data.get("synopses") or data.get("summaries") or data.get("あらすじ") or []
    titles = [_candidate_title(x) for x in titles_raw] if isinstance(titles_raw, list) else []
    synopses = [_candidate_synopsis(x) for x in synopses_raw] if isinstance(synopses_raw, list) else []
    # Internal source labels are never titles.  Dropping them here forces a
    # corrective LLM retry instead of letting task metadata reach upload.
    titles = [x for x in titles if x and not _contains_marketing_source_wrapper(x)]
    synopses = [x for x in synopses if x]
    tags = _candidate_tags(data.get("tags") or data.get("hashtags") or data.get("タグ") or [])
    series_short_title = re.sub(r"\s+", " ", str(
        data.get("series_short_title") or data.get("series_title") or data.get("系列短名") or ""
    )).strip().strip("【】[]『』「」")
    series_short_title = re.sub(r"[｜|].*$", "", series_short_title).strip()
    return {
        "titles": titles[:3],
        "synopses": synopses[:2],
        "tags": tags,
        "tag_line": "".join(tags),
        "series_short_title": series_short_title[:18],
    }


def _marketing_validation_error(bundle: dict, min_chars: int, max_chars: int) -> str:
    titles = bundle.get("titles") if isinstance(bundle.get("titles"), list) else []
    synopses = bundle.get("synopses") if isinstance(bundle.get("synopses"), list) else []
    problems = []
    if len(titles) != 3:
        problems.append(f"titles={len(titles)} (required 3)")
    if len(set(titles)) != len(titles):
        problems.append("titles must be distinct")
    bad_title_lengths = [len(x) for x in titles if len(x) < min_chars or len(x) > max_chars]
    if bad_title_lengths:
        problems.append(f"title lengths outside {min_chars}-{max_chars}: {bad_title_lengths}")
    if any(_contains_marketing_source_wrapper(title) for title in titles):
        problems.append("titles contain source-wrapper text")
    if len(synopses) != 2:
        problems.append(f"synopses={len(synopses)} (required 2)")
    bad_synopsis_lengths = [len(x) for x in synopses if len(x) < 80 or len(x) > 160]
    if bad_synopsis_lengths:
        problems.append(f"synopsis lengths outside 80-160: {bad_synopsis_lengths}")
    tags = bundle.get("tags") if isinstance(bundle.get("tags"), list) else []
    if not 10 <= len(tags) <= 15:
        problems.append(f"tags={len(tags)} (required 10-15)")
    return "; ".join(problems)


def _marketing_topic_error(bundle: dict, *source_values) -> str:
    """Reject category-crossed tags while there is still a chance to retry."""
    if not _is_three_kingdoms_material(*source_values):
        return ""
    tags = _candidate_tags(bundle.get("tags") if isinstance(bundle, dict) else [])
    if any(tag in _THREE_KINGDOMS_WRONG_TAGS for tag in tags):
        return "Three Kingdoms source has unrelated isekai/romance tags"
    if not any("三国" in tag or "三國" in tag for tag in tags):
        return "Three Kingdoms source lacks a Three Kingdoms tag"
    return ""


def _fallback_marketing_candidates(novel: Novel, story_material: str, max_chars: int) -> dict:
    """Produce upload-safe local metadata when a text gateway cannot return JSON.

    This is deliberately source-derived rather than inventive: it keeps a job
    moving without silently turning an API formatting outage into a pipeline
    failure.  The generated candidates remain editable in the GUI afterwards.
    """
    clean_title = _clean_display_title(novel.title)
    compact = re.sub(r"\s+", " ", str(novel.full_text or story_material or "")).strip()
    compact = re.sub(r"(?:标题|简介)\s*[:：]\s*", "", compact)
    if compact.startswith(clean_title):
        compact = compact[len(clean_title):].strip()
    sentences = [x.strip() for x in re.split(r"(?<=[。！？!?])", compact) if x.strip()]
    seeds = sentences[:3] or [clean_title]
    titles = []
    for index in range(3):
        seed = seeds[index % len(seeds)]
        value = _candidate_title(seed) or clean_title
        value = re.sub(r"[\r\n]+", " ", value).strip()
        # Marketing titles have a configured minimum length.  Extend with
        # neighbouring source sentences, then a neutral source-safe suffix.
        next_seed = seeds[(index + 1) % len(seeds)]
        while len(value) < 40 and next_seed:
            value = f"{value}――{_candidate_title(next_seed)}"
            next_seed = ""
        if len(value) < 40:
            value = f"{value}――明かされる物語の真相"
        value = _complete_title_within_limit(value, max_chars)
        while len(value) < 40:
            candidate = _complete_title_within_limit(value + "――逆転の結末へ。", max_chars)
            if not candidate or candidate == value:
                break
            value = candidate
        if value in titles:
            alternative = _complete_title_within_limit(value + f"――転機{index + 1}。", max_chars)
            if alternative:
                value = alternative
        titles.append(value)

    def synopsis_from(rows: list[str]) -> str:
        value = _candidate_synopsis("".join(rows)) or compact or clean_title
        if len(value) < 80:
            value = (value + "。" + compact)[:160]
        return value[:160].rstrip()

    synopsis = synopsis_from(sentences[:4])
    synopsis2 = synopsis_from(sentences[-4:])
    tags = _fallback_marketing_tags(novel, story_material)
    return {"titles": titles, "synopses": [synopsis, synopsis2], "tags": tags, "tag_line": "".join(tags)}


def _write_marketing_candidates_text(path: Path, bundle: dict) -> None:
    titles = list(bundle.get("titles") or [])[:3]
    synopses = list(bundle.get("synopses") or [])[:2]
    rows = ["【動画タイトル】", ""]
    rows.extend(f"{label}{value}" for label, value in zip(("①", "②", "③"), titles))
    rows.extend(["", "【あらすじ】", ""])
    rows.extend(f"{label}{value}" for label, value in zip(("①", "②"), synopses))
    tags = _candidate_tags(bundle.get("tags") or bundle.get("tag_line") or [])
    if tags:
        rows.extend(["", "【タグ候補】", "", "".join(tags)])
    rows.append("")
    path.write_text("\n".join(rows), encoding="utf-8")


def _metadata_has_marketing_candidates(metadata: dict | None) -> bool:
    data = metadata if isinstance(metadata, dict) else {}
    bundle = {
        "titles": [_candidate_title(x) for x in data.get("titles", [])] if isinstance(data.get("titles"), list) else [],
        "synopses": [_candidate_synopsis(x) for x in data.get("synopses", [])] if isinstance(data.get("synopses"), list) else [],
        "tags": _candidate_tags(data.get("generated_tags") or data.get("generated_tag_line") or data.get("tags") or ""),
    }
    min_chars = max(20, int(config.get("marketing_title_min_chars", 40) or 40))
    max_chars = max(min_chars, int(config.get("marketing_title_max_chars", 70) or 70))
    return not _marketing_validation_error(bundle, min_chars, max_chars)


def _metadata_has_valid_marketing_titles(metadata: dict | None) -> bool:
    """Check title safety before a manual upload can reach YouTube."""
    data = metadata if isinstance(metadata, dict) else {}
    titles = [_candidate_title(x) for x in data.get("titles", [])] if isinstance(data.get("titles"), list) else []
    min_chars = max(20, int(config.get("marketing_title_min_chars", 40) or 40))
    max_chars = max(min_chars, int(config.get("marketing_title_max_chars", 70) or 70))
    return (
        len(titles) == 3
        and len(set(titles)) == 3
        and all(min_chars <= len(title) <= max_chars and not _contains_marketing_source_wrapper(title) for title in titles)
    )


def _series_marketing_label(job_dir: Path, short_name: str = "") -> str:
    """Return a compact, stable prefix for a split novel series.

    The source title can be far too long for YouTube.  Keep a recognizable
    opening of it, but make the part marker deterministic so every candidate
    title and its cover visibly belong to the same series.
    """
    status = _read_json(job_dir / "status.json", {})
    if not isinstance(status, dict):
        return ""
    try:
        episode = int(status.get("series_episode") or 0)
    except (TypeError, ValueError):
        episode = 0
    series_title = str(status.get("series_title") or "").strip()
    if not series_title or episode < 1:
        return ""
    siblings = []
    for candidate in JOBS_DIR.iterdir():
        if not candidate.is_dir():
            continue
        sibling = _read_json(candidate / "status.json", {})
        if not isinstance(sibling, dict):
            continue
        if str(sibling.get("series_title") or "").strip() != series_title:
            continue
        try:
            value = int(sibling.get("series_episode") or 0)
        except (TypeError, ValueError):
            value = 0
        if value > 0:
            siblings.append(value)
    total = max(siblings, default=episode)
    part = "上篇" if total == 2 and episode == 1 else "下篇" if total == 2 and episode == 2 else f"第{episode}話"
    source_path = Path(str(status.get("source_path") or status.get("input") or "")).expanduser()
    bible = _read_json(source_path.parent / SERIES_BIBLE_FILENAME, {}) if source_path.is_file() else {}
    saved_short = str(bible.get("marketing_short_title") or "").strip() if isinstance(bible, dict) else ""
    compact = str(short_name or saved_short).strip()
    return f"{compact}｜{part}" if compact else ""


def _persist_series_marketing_short_title(job_dir: Path, short_name: str) -> str:
    """Persist one AI-renamed short title for every part in the source folder."""
    clean = re.sub(r"\s+", " ", str(short_name or "")).strip().strip("【】[]『』「」")[:18]
    status = _read_json(job_dir / "status.json", {})
    source_path = Path(str(status.get("source_path") or status.get("input") or "")).expanduser() if isinstance(status, dict) else Path()
    if not source_path.is_file():
        return clean
    bible_path = source_path.parent / SERIES_BIBLE_FILENAME
    bible = _read_json(bible_path, {})
    if not isinstance(bible, dict):
        bible = {}
    existing = str(bible.get("marketing_short_title") or "").strip()
    if existing:
        return existing
    if not clean:
        return ""
    bible["marketing_short_title"] = clean
    bible["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    _write_json(bible_path, bible)
    return clean


def _apply_series_marketing_label(bundle: dict, label: str, max_chars: int) -> dict:
    if not label:
        return bundle
    titles = []
    for raw in list(bundle.get("titles") or [])[:3]:
        title = _candidate_title(raw)
        title = re.sub(r"^(?:【[^】]+】|[^｜]{1,24}｜(?:上篇|下篇|第\d+話)｜)\s*", "", title)
        available = max(1, max_chars - len(label))
        title = title[:available].rstrip("、。！？!?｜ ")
        titles.append(f"{label}｜{title}")
    bundle = dict(bundle)
    bundle["titles"] = titles
    return bundle


def _fit_marketing_title_lengths(bundle: dict, max_chars: int) -> dict:
    """Normalize model wrappers without silently cutting a sentence in half.

    Length enforcement belongs to validation/retry.  Slicing here used to
    turn an otherwise complete Japanese title into fragments such as ``今さ``.
    """
    bundle = dict(bundle)
    bundle["titles"] = [
        _candidate_title(value)
        for value in list(bundle.get("titles") or [])[:3]
    ]
    return bundle


SERIES_MARKETING_TITLES_FILE = ".series_marketing_titles.json"


def _series_presentation_info(job_dir: Path) -> tuple[str, int, int, Path | None]:
    status = _read_json(job_dir / "status.json", {})
    if not isinstance(status, dict):
        return "", 0, 0, None
    series_title = str(status.get("series_title") or "").strip()
    try:
        episode = int(status.get("series_episode") or 0)
    except (TypeError, ValueError):
        episode = 0
    if not series_title or episode < 1:
        return "", 0, 0, None
    episodes = []
    for candidate in JOBS_DIR.iterdir():
        if not candidate.is_dir():
            continue
        sibling = _read_json(candidate / "status.json", {})
        if str(sibling.get("series_title") or "").strip() != series_title:
            continue
        try:
            value = int(sibling.get("series_episode") or 0)
        except (TypeError, ValueError):
            value = 0
        if value:
            episodes.append(value)
    return series_title, episode, max(episodes, default=episode), JOBS_DIR / SERIES_MARKETING_TITLES_FILE


def _series_part_label(episode: int, total: int) -> str:
    if total == 2:
        return "上篇" if episode == 1 else "下篇"
    if total == 3:
        return ("上篇", "中篇", "下篇")[max(0, min(2, episode - 1))]
    return f"第{episode}話"


def _series_short_name_from_existing_titles(
    job_dir: Path,
    titles: list[str],
    on_log: LogFn,
    *,
    _lock_held: bool = False,
) -> str:
    """Let the first-started episode initialize one durable shared novel name."""
    series_title, _episode, _total, bible_path = _series_presentation_info(job_dir)
    if not series_title or bible_path is None:
        return ""
    settings = series_video_settings_for_job(job_dir)
    manual_title = str(settings.get("shared_novel_title") or "").strip()
    if manual_title and bool(settings.get("shared_novel_title_locked", True)):
        on_log(f"  series shared title locked by user: {manual_title}")
        return manual_title
    registry = _read_json(bible_path, {})
    if not isinstance(registry, dict):
        registry = {}
    saved = str(registry.get(series_title) or "").strip()
    if saved:
        return saved
    if not _lock_held:
        lock_path = JOBS_DIR / f".series_name_{_text_hash(series_title)[:16]}.lock"
        lock_fd = None
        deadline = time.monotonic() + 300
        while lock_fd is None:
            try:
                lock_fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                os.write(lock_fd, str(os.getpid()).encode("ascii"))
            except FileExistsError:
                try:
                    if time.time() - lock_path.stat().st_mtime > 1800:
                        lock_path.unlink(missing_ok=True)
                        continue
                except OSError:
                    pass
                if time.monotonic() >= deadline:
                    raise TimeoutError("等待同系列小说名初始化超时")
                time.sleep(0.5)
        try:
            return _series_short_name_from_existing_titles(
                job_dir, titles, on_log, _lock_held=True
            )
        finally:
            if lock_fd is not None:
                os.close(lock_fd)
            lock_path.unlink(missing_ok=True)
    llm = None
    if _can_call_text_llm():
        route = _llm_route_settings()
        llm = LLMBackend(
            provider=route["provider"], base_url=route["base_url"], api_key=route["api_key"], model=route["model"],
            system_prompt="Return JSON only.", temperature=0.25, max_tokens=180, timeout=90.0,
        )
    series_titles = list(dict.fromkeys(str(value).strip() for value in titles if str(value).strip()))[:3]
    request = (
        "以下は同じ小説シリーズで最初に制作開始された1本の動画タイトルです。この1本の内容を根拠に、"
        "今後のシリーズ全話で共有できる、CTRを意識した"
        "短い日本語の小説名を3案作り、その中から最も内容が伝わりクリックされやすい1案を選んでください。"
        "ライトノベルまたは漫画広告の作品名らしく、主人公属性、関係性、溺愛、逆転、能力、危機など、その作品に"
        "根拠がある強い要素を1〜2個だけ入れてください。元ファイル名や元シリーズ名の単純な短縮・言い換えは禁止です。"
        "各案は6〜18文字程度、上篇・中篇・下篇・前編・後編・話数・タグ・括弧・各動画固有の出来事を含めないでください。"
        "3案は切り口を変え、selectedにはcandidates内の文字列を完全一致で入れてください。"
        "JSONのみ: {\"candidates\":[\"候補1\",\"候補2\",\"候補3\"],\"selected\":\"最良候補\"}\n\n"
        f"元シリーズ名（重複回避の確認用、出力しない）：{series_title}\n"
        f"最初に制作された動画タイトル：{json.dumps(series_titles, ensure_ascii=False)}"
    )
    try:
        if llm is None:
            raise RuntimeError("文字模型不可用")
        with external_api_slot(action="series short title"):
            data = parse_json_object(llm.complete(llm.system_prompt, request, temperature=0.25))
        candidates = [
            re.sub(r"\s+", " ", str(value or "")).strip().strip("【】[]『』「」")
            for value in data.get("candidates") or []
        ]
        candidates = [
            value for value in candidates
            if 6 <= len(value) <= 18
            and not re.search(r"(?:上篇|中篇|下篇|前編|後編|第\d+話|[#｜|])", value)
            and unicodedata.normalize("NFKC", value) != unicodedata.normalize("NFKC", series_title)
        ]
        selected = re.sub(r"\s+", " ", str(data.get("selected") or "")).strip().strip("【】[]『』「」")
        short_name = selected if selected in candidates else (candidates[0] if candidates else "")
        if not short_name:
            raise ValueError("invalid short name")
    except Exception as exc:
        # Legacy repairs must remain usable even when a compatibility gateway
        # emits prose rather than the requested JSON.  Preserve series
        # readability with a stable source-derived fallback instead of failing
        # every cover repair.
        short_name = unicodedata.normalize("NFKC", series_title)
        short_name = re.sub(r"[～~].*$", "", short_name).strip(" ｜・、。-–—")
        short_name = re.sub(r"[【\[].*$", "", short_name).strip()
        short_name = short_name[:18].rstrip(" ｜・、。-–—") or "物語シリーズ"
        on_log(f"  WARN series short-title AI failed; use stable fallback: {redact_secret_text(exc)}")
    registry[series_title] = short_name
    _write_json(bible_path, registry)
    on_log(f"  series short title: {short_name}")
    return short_name


def apply_series_presentation(metadata: dict, job_dir: Path, on_log: LogFn = _noop) -> dict:
    """Add a stable series badge after ordinary title generation, without altering it."""
    data = dict(metadata or {})
    if not series_animation_enabled_for_job(job_dir):
        for key in (
            "series_short_title", "series_part_label", "series_upload_prefix",
            "series_cover_label", "series_display_title",
        ):
            data.pop(key, None)
        return data
    titles = [_strip_series_part_prefix(str(value)) for value in data.get("titles") or [] if str(value).strip()]
    if not titles:
        titles = [str(data.get("short_title") or data.get("clean_title") or data.get("title") or "").strip()]
    titles = [value for value in titles if value]
    series_title, episode, total, _bible_path = _series_presentation_info(job_dir)
    if not series_title or not titles:
        return data
    settings = series_video_settings_for_job(job_dir)
    short_name = _series_short_name_from_existing_titles(job_dir, titles, on_log)
    label_template = str(settings.get("episode_label_style") or "").strip()
    if label_template:
        try:
            part = label_template.format(episode=episode, total=total)
        except (KeyError, ValueError):
            part = _series_part_label(episode, total)
    else:
        part = _series_part_label(episode, total)
    upload_template = str(
        settings.get("upload_title_template")
        or "{series_title}｜{episode_label}｜{ai_title}"
    )
    cover_template = str(
        settings.get("cover_label_template")
        or "{series_title}【{episode_label}】"
    )
    base_title = _strip_series_part_prefix(str(data.get("short_title") or titles[0]))
    template_context = {
        "series_title": short_name,
        "episode": episode,
        "total": total,
        "episode_label": part,
        "ai_title": base_title,
    }
    upload_prefix = _format_template(
        upload_template,
        {**template_context, "ai_title": ""},
        fallback=f"{short_name}｜{part}｜",
    )
    upload_prefix = re.sub(r"\s+", " ", upload_prefix).strip()
    upload_display = _format_template(
        upload_template,
        template_context,
        fallback=f"{short_name}｜{part}｜{base_title}",
    )
    cover_label = _format_template(
        cover_template,
        template_context,
        fallback=f"{short_name}【{part}】",
    )
    data["series_short_title"] = short_name
    data["series_part_label"] = part
    data["series_upload_prefix"] = upload_prefix
    data["series_upload_include_ai_title"] = "{ai_title}" in upload_template
    data["series_cover_label"] = cover_label
    data["series_display_title"] = re.sub(r"\s+", " ", upload_display).strip(" ｜")
    data["ai_cover_copy_enabled"] = bool(settings.get("ai_cover_copy_enabled", True))
    data["manual_cover_title"] = cover_label
    return data


def stage_metadata(
    novel: Novel,
    job_dir: Path,
    story_context: dict | None = None,
    segments: list[Segment] | None = None,
    on_log: LogFn = _noop,
) -> dict:
    intro = _metadata_intro(novel, story_context)
    clean_title = _clean_display_title(novel.title)
    series_settings = series_video_settings_for_job(job_dir)
    ai_episode_title_enabled = bool(
        series_settings.get("ai_episode_title_enabled", True)
    )
    min_chars = max(20, int(config.get("marketing_title_min_chars", 40) or 40))
    max_chars = max(min_chars, int(config.get("marketing_title_max_chars", 70) or 70))
    story_material = sampled_story_input(
        novel,
        list(segments or []),
        int(config.get("storyboard_highlight_context_max_chars", 18000) or 18000),
    )
    route = _llm_route_settings()
    prompt = str(config.get("marketing_candidates_prompt", "") or "").strip()
    # Keep the editable prompt and the numeric settings on one source of
    # truth.  Older saved prompts commonly contain the original 40-70 rule.
    prompt = re.sub(
        r"各\s*40[〜～-]70\s*文字程度",
        f"各{min_chars}〜{max_chars}文字程度",
        prompt,
    )
    prompt += (
        f"\n\n【実行時の最優先文字数ルール】動画タイトル3案は各{min_chars}〜{max_chars}文字。"
        "この範囲を超えるタイトルは返さず、文の途中で切らず、句読点または意味の完結する位置で"
        "内容を組み直してください。プログラム側でタイトルを途中切断することはありません。"
    )
    input_hash = _stable_hash({
        "schema": "marketing_candidates_v4_no_fallback_source_text",
        "source": story_material,
        "story_context": story_context or {},
        "provider": route["provider"],
        "model": route["model"],
        "prompt": prompt,
        "min_chars": min_chars,
        "max_chars": max_chars,
        "ai_episode_title_enabled": ai_episode_title_enabled,
    })
    candidates_path = job_dir / "marketing_candidates.json"
    cached = _read_json(candidates_path, {})
    bundle = {}
    if isinstance(cached, dict) and cached.get("input_hash") == input_hash:
        try:
            candidate = _parse_marketing_candidates(cached)
            candidate = _fit_marketing_title_lengths(candidate, max_chars)
            if not (_marketing_validation_error(candidate, min_chars, max_chars) or _marketing_topic_error(candidate, novel.title, story_material)):
                bundle = candidate
                on_log("  reuse 3 title / 2 synopsis marketing candidates")
        except Exception:
            bundle = {}
    raw_output = ""
    validation_error = ""
    generation_attempts = 0
    last_candidate: dict = {}
    try:
        max_generation_attempts = int(config.get("marketing_candidates_retry_attempts", 5) or 5)
    except (TypeError, ValueError):
        max_generation_attempts = 5
    max_generation_attempts = max(1, min(10, max_generation_attempts))
    retry_delay_seconds = _optional_timeout(
        config.get("marketing_candidates_retry_delay_seconds", 60), 60.0, minimum=5.0, maximum=900.0,
    )
    if bool(config.get("short_title_enabled", True)) and ai_episode_title_enabled:
        if not bundle and _can_call_text_llm():
            try:
                llm = LLMBackend(
                    provider=route["provider"],
                    base_url=route["base_url"],
                    api_key=route["api_key"],
                    model=route["model"],
                    system_prompt=prompt,
                    style_suffix="",
                    temperature=0.7,
                    max_tokens=int(config.get("marketing_candidates_max_tokens", 1600) or 1600),
                    timeout=180.0,
                )
                request = (
                    "以下は作品のタイトルと、冒頭・中盤・終盤から抽出した事実資料です。"
                    "この資料全体を根拠に、指定JSONだけを返してください。\n\n"
                    f"作品タイトル（事実確認用。出力しない）：{clean_title}\n"
                    f"既存の全体簡報：{json.dumps(story_context or {}, ensure_ascii=False)}\n\n"
                    + f"冒頭・中盤・終盤の資料：\n{story_material}"
                )
                for attempt in range(max_generation_attempts):
                    generation_attempts = attempt + 1
                    retry_note = ""
                    if attempt:
                        if _is_transient_json_response_error(validation_error) and retry_delay_seconds > 0:
                            on_log(
                                "  WARN 标题接口未返回 JSON，"
                                f"等待 {retry_delay_seconds:.0f}s 后自动重试 "
                                f"({attempt + 1}/{max_generation_attempts})..."
                            )
                            time.sleep(retry_delay_seconds)
                        retry_note = (
                            f"\n\n前回の出力は形式検査に失敗しました：{validation_error}。"
                            "内容を作り直し、3タイトル・2あらすじ・10〜15個の内容一致タグを含む正しいJSONだけを一行で返してください。"
                            "入力内の「标题」「简介」「local text input」「[开头]」等の内部ラベルをタイトルへ絶対に写さないでください。"
                            "【朗読・小説】と#赤陽の勧めるノベルは使用しないでください。"
                        )
                    try:
                        with external_api_slot(action="marketing candidates"):
                            raw_output = llm.complete(
                                prompt,
                                request + retry_note,
                                max_tokens=int(config.get("marketing_candidates_max_tokens", 1600) or 1600),
                                temperature=0.7,
                            ).strip()
                        candidate = _parse_marketing_candidates(raw_output)
                        candidate = _fit_marketing_title_lengths(candidate, max_chars)
                        last_candidate = candidate
                        validation_error = (
                            _marketing_validation_error(candidate, min_chars, max_chars)
                            or _marketing_topic_error(candidate, novel.title, story_material)
                        )
                    except Exception as exc:
                        candidate = {}
                        validation_error = str(exc)
                    if not validation_error:
                        bundle = candidate
                        break
            except Exception as exc:
                on_log(f"  WARN marketing candidate LLM failed: {redact_secret_text(exc)}; use grounded fallback")
    if not bundle:
        message = validation_error or "text LLM unavailable or did not return a valid bundle"
        bundle = _fallback_marketing_candidates(novel, story_material, max_chars)
        fallback_error = (
            _marketing_validation_error(bundle, min_chars, max_chars)
            or _marketing_topic_error(bundle, novel.title, story_material)
        )
        if fallback_error:
            raise RuntimeError(f"本地标题兜底生成失败：{fallback_error}")
        if not ai_episode_title_enabled:
            on_log("  series setting disabled AI episode titles; using local source-derived titles")
        else:
            on_log(
                "  WARN 标题接口格式异常；已使用本地保底标题继续任务："
                + redact_secret_text(message)
            )

    bundle_record = {
        "schema_version": 2,
        "input_hash": input_hash,
        "titles": bundle["titles"],
        "synopses": bundle["synopses"],
        "tags": bundle["tags"],
        "tag_line": bundle["tag_line"],
        "generation_attempts": generation_attempts,
        "validation_warning": validation_error,
        "raw_model_output": raw_output,
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    _write_json(candidates_path, bundle_record)
    _write_marketing_candidates_text(job_dir / "タイトル・あらすじ候補.txt", bundle_record)
    short_title = str(bundle["titles"][0] or "")
    intro = str(bundle["synopses"][0] or intro)
    metadata = {
        "title": novel.title,
        "clean_title": clean_title,
        "short_title": short_title,
        "intro": intro,
        "titles": bundle["titles"],
        "synopses": bundle["synopses"],
        "generated_tags": bundle["tags"],
        "generated_tag_line": bundle["tag_line"],
        "story_brief": _metadata_intro(novel, story_context),
        "author": novel.author,
        "tags": str(config.youtube_tags or ""),
    }
    _write_json(job_dir / "metadata.json", metadata)
    on_log(f"  marketing candidates: {len(bundle['titles'])} titles / {len(bundle['synopses'])} synopses")
    for index, value in enumerate(bundle["titles"], start=1):
        on_log(f"  title {index}: {value}")
    return metadata


class _SafeFormatDict(dict):
    def __missing__(self, key):
        return "{" + str(key) + "}"


def _format_template(template: str, context: dict, fallback: str = "") -> str:
    text = str(template or "")
    if not text:
        return fallback
    try:
        return text.format_map(_SafeFormatDict({k: "" if v is None else v for k, v in context.items()}))
    except Exception:
        return text or fallback


def _limit_upload_title(text: str, max_chars: int = 100) -> str:
    title = re.sub(r"\s+", " ", str(text or "")).strip()
    limit = max(0, int(max_chars or 0))
    if not limit or len(title) <= limit:
        return title
    for suffix in (" #小说 #推文", "#小说 #推文"):
        if title.endswith(suffix) and limit > len(suffix) + 8:
            body = title[: limit - len(suffix)].rstrip(" ，。！？、,.!?;；:")
            return (body + suffix).strip()
    return title[:limit].rstrip(" ，。！？、,.!?;；:")


def _remove_disallowed_upload_tag(text: str) -> str:
    """Remove the retired channel tag from new and legacy upload text."""
    value = re.sub(r"[#＃]?赤陽の勧めるノベル", "", str(text or ""))
    return re.sub(r"[ \t]{2,}", " ", value).strip()


def _append_generated_tags_to_upload_title(text: str, generated_tags, max_chars: int) -> str:
    """Fill unused title space with relevant generated tags not already in the template."""
    limit = max(1, int(max_chars or 100))
    title = _limit_upload_title(_remove_disallowed_upload_tag(text), limit)
    existing_tags = set(_candidate_tags(title))
    for tag in _candidate_tags(generated_tags):
        if tag in existing_tags:
            continue
        candidate = f"{title} {tag}".strip()
        if len(candidate) > limit:
            continue
        title = candidate
        existing_tags.add(tag)
    return title


def _persistent_upload_candidate(
    job_dir: Path,
    profile: dict,
    metadata: dict,
) -> dict | None:
    titles_raw = metadata.get("titles") if isinstance(metadata.get("titles"), list) else []
    titles = [_strip_series_part_prefix(x) for x in titles_raw if _strip_series_part_prefix(x)][:3]
    if not titles:
        return None
    candidate_hash = _stable_hash({
        "schema": "upload_candidate_v2_template_driven",
        "titles": titles,
    })
    path = job_dir / "upload_title_selection.json"
    record = _read_json(path, {})
    if not isinstance(record, dict) or record.get("candidate_hash") != candidate_hash:
        record = {"schema_version": 2, "candidate_hash": candidate_hash, "selections": {}}
    selections = record.get("selections") if isinstance(record.get("selections"), dict) else {}
    profile_key = "|".join(
        (
            str(profile.get("name") or "default").strip(),
            str(profile.get("chrome_profile") or "Default").strip(),
        )
    )
    selected = selections.get(profile_key) if isinstance(selections.get(profile_key), dict) else {}
    chosen = str(selected.get("candidate_title") or "")
    if chosen not in titles:
        chosen = secrets.choice(titles)
        selected = {
            "profile_name": str(profile.get("name") or "default"),
            "chrome_profile": str(profile.get("chrome_profile") or "Default"),
            "candidate_index": titles.index(chosen) + 1,
            "candidate_title": chosen,
            "selected_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        selections[profile_key] = selected
        record["selections"] = selections
        record["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        _write_json(path, record)
    return selected


def _upload_lock_path() -> Path:
    return JOBS_DIR.parent / "upload.lock"


def _read_upload_lock(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _acquire_upload_lock(job_dir: Path, on_log: LogFn = _noop) -> Path:
    path = _upload_lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "job_id": job_dir.name,
        "pid": os.getpid(),
        "created_at": time.time(),
        "created_at_text": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    last_log = 0.0
    while True:
        try:
            fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False)
            on_log(f"  upload queue acquired by {job_dir.name}")
            return path
        except FileExistsError:
            info = _read_upload_lock(path)
            pid = int(info.get("pid") or 0)
            age = time.time() - float(info.get("created_at") or 0)
            if (pid and not pid_alive(pid)) or age > 12 * 3600:
                try:
                    path.unlink()
                    on_log("  WARN removed stale upload queue lock")
                    continue
                except FileNotFoundError:
                    continue
                except Exception:
                    pass
            now = time.monotonic()
            if now - last_log >= 60:
                owner = info.get("job_id") or "another job"
                on_log(f"  upload queue busy: waiting for {owner} to finish")
                last_log = now
            time.sleep(5)


def _release_upload_lock(path: Path, job_id: str) -> None:
    info = _read_upload_lock(path)
    if info.get("job_id") not in {"", None, job_id}:
        return
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def _ensure_upload_dependencies(on_log: LogFn = _noop) -> None:
    if not bool(config.get("upload_dependency_check_before_upload", True)):
        return
    from app.dependency_manager import ensure_dependencies

    on_log("  checking upload dependencies before YouTube upload")
    report = ensure_dependencies(scope="full", on_log=lambda line: on_log(f"  {line}"))
    if not bool(report.get("ok")):
        summary = str(report.get("summary") or "upload dependencies are not ready")
        raise RuntimeError(f"YouTube upload dependencies are not ready: {summary}")


def _cover_provider() -> str:
    provider = str(config.get("cover_provider", "same_as_image") or "").strip()
    if provider in {"", "same_as_image", "同配图", "同图片"}:
        return str(config.image_provider or "placeholder")
    return provider


def _cover_backend_settings() -> dict:
    return _image_route_settings("cover")


def _cover_excerpt(novel: Novel, segments: list[Segment], limit: int = 900) -> str:
    text = novel.full_text or "\n".join(seg.text for seg in segments[:8])
    text = re.sub(r"\s+", " ", text).strip()
    return text[: max(80, int(limit))]


def _cover_marketing_context(
    novel: Novel,
    segments: list[Segment],
    metadata: dict | None,
) -> tuple[str, str]:
    """Return the generated marketing title and synopsis for a cover.

    Covers must use the metadata stage's audience-facing title, never the job
    directory or source-work title.  Metadata is created before the cover
    stage and reuses the story-context sampling, so this adds no API call.
    """
    data = metadata if isinstance(metadata, dict) else {}
    title = _clean_short_title(str(data.get("short_title") or ""), 1, 80)
    source_title = _clean_short_title(_clean_display_title(novel.title), 1, 80)
    if title and title == source_title:
        # A provider occasionally echoes the source title.  It is a useful
        # fact for title generation, but must never become the cover hook.
        title = ""
    synopsis = re.sub(r"\s+", " ", str(data.get("intro") or "")).strip()
    if not synopsis:
        synopsis = _cover_excerpt(novel, segments)
    if not title:
        # Degraded/offline fallback: derive display text from story material,
        # deliberately never from novel.title (which is also the task name).
        title = _clean_short_title(synopsis, 1, 30) or "物語の真相"
    return title, synopsis[:900]


def _cover_marketing_bundle(
    novel: Novel,
    segments: list[Segment],
    metadata: dict | None,
) -> dict:
    data = metadata if isinstance(metadata, dict) else {}
    if data.get("ai_cover_copy_enabled") is False:
        manual_title = str(
            data.get("manual_cover_title")
            or data.get("series_cover_label")
            or data.get("series_short_title")
            or ""
        ).strip()
        synopsis = _candidate_synopsis(str(data.get("intro") or ""))
        if not synopsis:
            synopsis = _cover_excerpt(novel, segments)
        return {
            "titles": [manual_title or "物語シリーズ"],
            "synopses": [synopsis],
            "tags": [],
            "tag_line": "",
        }
    titles_raw = data.get("titles") if isinstance(data.get("titles"), list) else []
    synopses_raw = data.get("synopses") if isinstance(data.get("synopses"), list) else []
    titles = [_candidate_title(x) for x in titles_raw if _candidate_title(x)][:3]
    synopses = [_candidate_synopsis(x) for x in synopses_raw if _candidate_synopsis(x)][:2]
    if not titles or not synopses:
        legacy_title, legacy_synopsis = _cover_marketing_context(novel, segments, metadata)
        if not titles:
            titles = [legacy_title]
        if not synopses:
            synopses = [legacy_synopsis]
    return {"titles": titles, "synopses": synopses, "tags": [], "tag_line": ""}


def _cover_prompt_aspect_only(value: str) -> str:
    """Remove legacy fixed pixel dimensions from cover prompt text."""
    text = str(value or "")
    text = re.sub(r"(?i)\b1280\s*[x×✖]\s*720\b", "16:9", text)
    return text


def _normalize_cover_prompt(prompt: str) -> str:
    """Normalize whitespace and remove legacy fixed pixel dimensions."""
    return " ".join(_cover_prompt_aspect_only(prompt).split()).strip()


def _full_cover_poster_method_prompt() -> str:
    """Use only the complete poster method currently editable in the GUI."""
    configured = str(config.get("cover_poster_method_prompt", "") or "").strip()
    return _cover_prompt_aspect_only(configured)


def _cover_planner_quality_error(prompt: str) -> str:
    """Reject incomplete art-direction output without spending a second call."""
    value = re.sub(r"\s+", " ", str(prompt or "")).strip()
    problems: list[str] = []
    if len(value) < 180:
        problems.append("too short")
    if not re.search(r"[\u3040-\u30ff\u3400-\u9fff]", value):
        problems.append("no exact Japanese poster copy")
    lowered = value.lower()
    if not any(term in lowered for term in ("headline", "main title", "typography")):
        problems.append("no headline layout")
    if not any(term in lowered for term in ("hook", "emphasis", "subtitle", "supporting line")):
        problems.append("no text hierarchy")
    if not any(term in lowered for term in ("action", "confront", "reveal", "react", "turns", "raises", "hands", "stops", "standing", "holding", "facing")):
        problems.append("no concrete action/reaction")
    return "; ".join(problems)


def _build_cover_prompt(
    novel: Novel,
    segments: list[Segment],
    on_log: LogFn = _noop,
    metadata: dict | None = None,
) -> str:
    bundle = _cover_marketing_bundle(novel, segments, metadata)
    title = str(bundle["titles"][0])
    excerpt = str(bundle["synopses"][0])
    series_badge = (
        str((metadata or {}).get("series_cover_label") or "").strip()
        if bool(config.get("series_animation_enabled", True))
        else ""
    )
    style = str(config.llm_image_style_suffix or "").strip()
    custom = str(config.get("cover_custom_prompt", "") or "").strip()
    template = _cover_prompt_aspect_only(str(config.get("cover_prompt_template", "") or ""))
    title_list = "\n".join(f"{index}. {value}" for index, value in enumerate(bundle["titles"], start=1))
    synopsis_list = "\n".join(f"{index}. {value}" for index, value in enumerate(bundle["synopses"], start=1))
    context = {
        "title": title,
        "excerpt": excerpt,
        "titles": title_list,
        "synopses": synopsis_list,
        "tags": bundle["tag_line"],
        "style": style,
        "custom": custom,
        "series_badge": series_badge,
    }
    fallback = _format_template(
        template,
        context,
        fallback=(
            f'Create a cinematic YouTube cover illustration for a novel video. '
            f'Render the exact title text "{title}" as the main large readable cover title inside the image. '
            + (f'Render the exact recurring series identifier "{series_badge}" as a prominent primary text element near the upper edge, clearly separate from the episode hook. ' if series_badge else "")
            + f'Story excerpt: {excerpt}. {custom} {style}'
        ),
    )

    if _can_call_text_llm():
        try:
            route = _llm_route_settings()
            analysis_prompt = _cover_prompt_aspect_only(str(config.get("cover_ai_analysis_prompt", "") or "")).strip()
            poster_method = _full_cover_poster_method_prompt()
            system_prompt = "\n\n".join(x for x in (analysis_prompt, poster_method) if x)
            llm = LLMBackend(
                provider=route["provider"],
                base_url=route["base_url"],
                api_key=route["api_key"],
                model=route["model"],
                system_prompt=system_prompt,
                style_suffix="",
                temperature=0.65,
                max_tokens=int(config.get("cover_prompt_max_tokens", 1400) or 1400),
            )
            request = (
                "Choose one supported climax. Return one compact prompt only; never use the source title as poster copy.\n"
                + (f'Prominent primary series identifier, exact text: "{series_badge}". Keep it clearly separate from the episode headline.\n' if series_badge else "")
                + f"Candidates:{json.dumps(bundle, ensure_ascii=False, separators=(',', ':'))}\n"
                f"Base:{custom} {style}\n"
                "No logo, UI, watermark, or post-production overlay."
            )
            with external_api_slot(action="cover prompt"):
                prompt = llm.complete(
                    system_prompt,
                    request,
                    max_tokens=int(config.get("cover_prompt_max_tokens", 1400) or 1400),
                    temperature=0.65,
                ).strip()
            quality_error = _cover_planner_quality_error(prompt)
            if quality_error:
                raise RuntimeError(f"incomplete cover planner output: {quality_error}")
            return _policy_safe_image_prompt(
                _normalize_cover_prompt(
                    f"{prompt}. Only the specified Japanese title, hook and subtitle; no other text or watermark."
                ),
                fallback_text=excerpt,
                allow_title_text=True,
            )
        except Exception as exc:
            on_log(f"  WARN cover prompt LLM failed: {exc}; use template prompt")
    return _policy_safe_image_prompt(
        _normalize_cover_prompt(fallback),
        fallback_text=excerpt,
        allow_title_text=True,
    )


def _cover_error_kind(exc: Exception | str) -> str:
    text = str(redact_secret_text(exc)).lower()
    policy_markers = (
        "content policy", "safety system", "safety filter", "moderation", "responsible ai",
        "content_filter", "content blocked", "prompt blocked", "unsafe prompt", "sensitive content",
        "policy violation", "violates", "违规", "内容审核", "安全策略", "敏感内容", "审核拒绝",
    )
    if any(marker in text for marker in policy_markers):
        return "policy"
    transient_markers = (
        "timeout", "timed out", "rate limit", "too many requests", "temporarily unavailable",
        "service unavailable", "connection reset", "connection refused", "connection aborted",
        "remote protocol", "server disconnected", "bad gateway", "gateway timeout",
        "http 429", "http 500", "http 502", "http 503", "http 504", "超时", "限流", "暂时不可用",
    )
    if any(marker in text for marker in transient_markers):
        return "transient"
    return "other"


def _rewrite_cover_prompt_for_policy(
    novel: Novel,
    segments: list[Segment],
    metadata: dict | None,
    previous_prompt: str,
    error_text: str,
    on_log: LogFn = _noop,
) -> str:
    if not _can_call_text_llm():
        return ""
    bundle = _cover_marketing_bundle(novel, segments, metadata)
    route = _llm_route_settings()
    analysis_prompt = _cover_prompt_aspect_only(str(config.get("cover_ai_analysis_prompt", "") or "")).strip()
    poster_method = _full_cover_poster_method_prompt()
    system_prompt = "\n\n".join(x for x in (analysis_prompt, poster_method) if x)
    llm = LLMBackend(
        provider=route["provider"],
        base_url=route["base_url"],
        api_key=route["api_key"],
        model=route["model"],
        system_prompt=system_prompt,
        style_suffix="",
        temperature=0.45,
        max_tokens=int(config.get("cover_prompt_max_tokens", 1400) or 1400),
    )
    request = (
        "The image provider rejected the previous prompt for content-policy reasons. Rewrite it as exactly ONE complete English "
        "image-generation prompt. Keep the same factual story event, premium manga-ad style, subject-specific palette, Japanese "
        "headline hierarchy and horizontal 16:9 composition, but replace any risky physical action, injury, threat, sexualized detail, "
        "minor depiction or graphic wording with a fully clothed adult-looking, non-contact, symbolic emotional confrontation. "
        "Do not return analysis or alternatives.\n\n"
        f"Marketing candidates:\n{json.dumps(bundle, ensure_ascii=False, separators=(',', ':'))}\n\n"
        f"Provider rejection summary:\n{redact_secret_text(error_text)[:700]}\n\n"
        f"Previous prompt:\n{previous_prompt[:5000]}"
    )
    try:
        with external_api_slot(action="cover policy rewrite"):
            rewritten = llm.complete(
                system_prompt,
                request,
                max_tokens=int(config.get("cover_prompt_max_tokens", 1400) or 1400),
                temperature=0.45,
            ).strip()
        return _policy_safe_image_prompt(
            _normalize_cover_prompt(
                f"{rewritten}. Only the specified Japanese title, hook and subtitle; no other text or watermark."
            ),
            fallback_text=" ".join(bundle["synopses"]),
            allow_title_text=True,
        )
    except Exception as exc:
        on_log(f"  WARN cover policy rewrite failed: {redact_secret_text(exc)}")
        return ""


def _cover_negative_prompt() -> str:
    negative = str(config.llm_negative_prompt or "")
    filtered = []
    for part in negative.split(","):
        item = part.strip()
        lower = item.lower()
        if lower in {"text", "texts", "word", "words", "caption", "captions"}:
            continue
        if "title text" in lower or "readable text" in lower:
            continue
        filtered.append(item)
    return ", ".join(item for item in filtered if item)


def _hex_rgb(value: str, fallback: tuple[int, int, int]) -> tuple[int, int, int]:
    text = str(value or "").strip().lstrip("#")
    if len(text) == 3:
        text = "".join(ch * 2 for ch in text)
    if len(text) != 6:
        return fallback
    try:
        return int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16)
    except ValueError:
        return fallback


def _load_title_font(font_name: str, size: int) -> ImageFont.ImageFont:
    windir = Path(os.environ.get("WINDIR", r"C:\Windows"))
    fonts_dir = windir / "Fonts"
    candidates: list[str | Path] = []
    name = str(font_name or "").strip()
    if name:
        candidates.append(name)
    if "yahei" in name.lower() or "雅黑" in name:
        candidates.extend(["msyhbd.ttc", "msyh.ttc"])
    candidates.extend(
        [
            fonts_dir / "msyhbd.ttc",
            fonts_dir / "msyh.ttc",
            fonts_dir / "simhei.ttf",
            fonts_dir / "arialbd.ttf",
            fonts_dir / "arial.ttf",
            "msyhbd.ttc",
            "msyh.ttc",
            "simhei.ttf",
            "arialbd.ttf",
            "arial.ttf",
        ]
    )
    for candidate in candidates:
        try:
            return ImageFont.truetype(str(candidate), size)
        except Exception:
            continue
    return ImageFont.load_default()


def _text_width(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont) -> int:
    box = draw.textbbox((0, 0), text, font=font)
    return int(box[2] - box[0])


def _line_height(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont) -> int:
    box = draw.textbbox((0, 0), text or "Ag", font=font)
    return int(box[3] - box[1])


def _wrap_pixels(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont, max_width: int) -> list[str]:
    lines: list[str] = []
    for paragraph in [part.strip() for part in str(text or "").splitlines() if part.strip()] or [str(text or "").strip()]:
        current = ""
        for ch in paragraph:
            test = current + ch
            if not current or _text_width(draw, test, font) <= max_width:
                current = test
            else:
                lines.append(current)
                current = ch
        if current:
            lines.append(current)
    return lines or [str(text or "").strip()]


def _ellipsize(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont, max_width: int) -> str:
    suffix = "..."
    text = str(text or "")
    while text and _text_width(draw, text + suffix, font) > max_width:
        text = text[:-1]
    return (text + suffix) if text else suffix


def _fit_title(
    draw: ImageDraw.ImageDraw,
    title: str,
    font_name: str,
    start_size: int,
    max_width: int,
    max_height: int,
) -> tuple[ImageFont.ImageFont, list[str], int]:
    min_size = 26
    for size in range(max(min_size, int(start_size)), min_size - 1, -2):
        font = _load_title_font(font_name, size)
        lines = _wrap_pixels(draw, title, font, max_width)
        spacing = max(6, int(size * 0.16))
        line_h = _line_height(draw, title, font)
        total_h = len(lines) * line_h + max(0, len(lines) - 1) * spacing
        if len(lines) <= 2 and total_h <= max_height and all(_text_width(draw, line, font) <= max_width for line in lines):
            return font, lines, spacing

    font = _load_title_font(font_name, min_size)
    lines = _wrap_pixels(draw, title, font, max_width)
    if len(lines) > 2:
        merged_tail = "".join(lines[1:])
        lines = [lines[0], _ellipsize(draw, merged_tail, font, max_width)]
    return font, lines[:2], max(5, int(min_size * 0.16))


def _render_cover_title(raw_path: Path, out_path: Path, title: str) -> Path:
    width = max(320, int(config.get("cover_width", 1280) or 1280))
    height = max(180, int(config.get("cover_height", 720) or 720))
    ratio = float(config.get("cover_title_area_ratio", 0.24) or 0.24)
    band_h = max(110, min(height // 2, int(height * ratio)))
    img = Image.open(raw_path).convert("RGB")
    img = ImageOps.fit(img, (width, height), method=Image.LANCZOS, centering=(0.5, 0.45)).convert("RGBA")

    fg = _hex_rgb(str(config.get("cover_title_color", "#FFFFFF")), (255, 255, 255))

    draw = ImageDraw.Draw(img)
    title = (title or "Untitled").strip()
    max_width = int(width * 0.88)
    max_height = max(60, band_h - 34)
    font, lines, spacing = _fit_title(
        draw,
        title,
        str(config.get("cover_title_font", "Microsoft YaHei") or "Microsoft YaHei"),
        int(config.get("cover_title_size", 72) or 72),
        max_width,
        max_height,
    )
    line_h = _line_height(draw, title, font)
    total_h = len(lines) * line_h + max(0, len(lines) - 1) * spacing
    y = height - band_h + (band_h - total_h) / 2
    shadow = max(2, int(getattr(font, "size", 40) * 0.04))
    stroke = max(2, int(getattr(font, "size", 40) * 0.055))
    for line in lines:
        line_w = _text_width(draw, line, font)
        x = (width - line_w) / 2
        draw.text((x + shadow, y + shadow), line, fill=(0, 0, 0, 165), font=font)
        draw.text((x, y), line, fill=(*fg, 255), font=font, stroke_width=stroke, stroke_fill=(0, 0, 0, 205))
        y += line_h + spacing

    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.convert("RGB").save(out_path, "JPEG", quality=93, optimize=True)
    return out_path


def render_series_cover_badge(cover_path: Path, label: str) -> Path:
    """Add a durable, readable series/part marker without regenerating art."""
    label = str(label or "").strip()
    if not label or not cover_path.exists():
        return cover_path
    img = Image.open(cover_path).convert("RGBA")
    width, height = img.size
    size = max(22, min(56, int(width / 30)))
    font = _load_title_font(str(config.get("cover_title_font", "Microsoft YaHei") or "Microsoft YaHei"), size)
    draw = ImageDraw.Draw(img)
    pad_x, pad_y = max(16, width // 55), max(10, height // 70)
    text_w = _text_width(draw, label, font)
    text_h = _line_height(draw, label, font)
    box_w = min(width - pad_x * 2, text_w + pad_x * 2)
    box_h = text_h + pad_y * 2
    x, y = pad_x, pad_y
    draw.rounded_rectangle((x, y, x + box_w, y + box_h), radius=max(8, size // 4), fill=(20, 15, 30, 220), outline=(244, 210, 110, 255), width=max(1, size // 18))
    draw.text((x + pad_x, y + pad_y - 2), label, font=font, fill=(255, 244, 205, 255), stroke_width=max(1, size // 24), stroke_fill=(0, 0, 0, 220))
    img.convert("RGB").save(cover_path, "JPEG", quality=93, optimize=True)
    return cover_path


def _finalize_cover_image(raw_path: Path, out_path: Path, width: int, height: int) -> Path:
    img = Image.open(raw_path).convert("RGB")
    img = ImageOps.fit(img, (max(320, int(width)), max(180, int(height))), method=Image.LANCZOS, centering=(0.5, 0.5))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path, "JPEG", quality=93, optimize=True)
    return out_path


def _fit_provider_cover_to_16_9(source_path: Path, out_path: Path, width: int, height: int) -> Path:
    """Normalize a provider response to the configured exact 16:9 output size."""
    target = (max(320, int(width)), max(180, int(height)))
    with Image.open(source_path) as opened:
        source = ImageOps.exif_transpose(opened).convert("RGB")
    result = ImageOps.fit(source, target, method=Image.Resampling.LANCZOS, centering=(0.5, 0.5))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    result.save(out_path, "PNG")
    return out_path


def _create_cover_placeholder(prompt: str, title: str, out_path: Path, width: int, height: int) -> Path:
    seed = int(_text_hash(f"{title}\n{prompt}")[:12], 16)
    palettes = [
        ((18, 24, 34), (94, 132, 148), (222, 189, 96)),
        ((28, 24, 40), (116, 86, 139), (218, 174, 126)),
        ((18, 36, 35), (76, 126, 103), (204, 210, 173)),
        ((38, 28, 26), (138, 76, 61), (231, 173, 91)),
    ]
    base, mid, accent = palettes[seed % len(palettes)]
    img = Image.new("RGB", (width, height), base)
    px = img.load()
    for y in range(height):
        t = y / max(1, height - 1)
        mix = t * 0.78
        row = tuple(int(base[i] * (1 - mix) + mid[i] * mix) for i in range(3))
        for x in range(width):
            px[x, y] = row

    overlay = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    d = ImageDraw.Draw(overlay)
    shift = seed % max(1, width // 3)
    d.polygon(
        [
            (width * 0.08 + shift // 3, height * 0.20),
            (width * 0.74, height * 0.06),
            (width * 0.92, height * 0.62),
            (width * 0.20, height * 0.78),
        ],
        fill=(*accent, 44),
    )
    d.polygon(
        [
            (-width * 0.08, height * 0.62),
            (width * 0.42, height * 0.32),
            (width * 0.70, height * 0.86),
            (width * 0.06, height * 1.04),
        ],
        fill=(255, 255, 255, 20),
    )
    d.rectangle((0, int(height * 0.70), width, height), fill=(0, 0, 0, 46))
    img = Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path, "PNG")
    return out_path


def stage_cover(
    novel: Novel,
    segments: list[Segment],
    job_dir: Path,
    on_log: LogFn = _noop,
    metadata: dict | None = None,
    force: bool = False,
) -> Path | None:
    if not force and not bool(config.get("cover_enabled", True)):
        return None
    if not series_animation_enabled_for_job(job_dir) and isinstance(metadata, dict):
        metadata = dict(metadata)
        for key in (
            "series_short_title", "series_part_label", "series_upload_prefix",
            "series_cover_label", "series_display_title",
        ):
            metadata.pop(key, None)

    marketing_bundle = _cover_marketing_bundle(novel, segments, metadata)
    title = str(marketing_bundle["titles"][0])
    excerpt = str(marketing_bundle["synopses"][0])
    cover_dir = job_dir / "cover"
    cover_dir.mkdir(parents=True, exist_ok=True)
    provider_raw_path = cover_dir / "cover_provider_raw.png"
    raw_path = cover_dir / "cover_raw.png"
    out_path = cover_dir / "cover.jpg"
    manifest_path = cover_dir / "cover_manifest.json"
    attempts_path = cover_dir / "cover_prompt_attempts.json"
    if not force and _image_fallback_mode(job_dir):
        if out_path.exists() and out_path.stat().st_size > 1000:
            on_log(f"  reuse cover without image API: {out_path}")
            return out_path
        on_log("  skip cover generation: image-reuse retry does not call image API")
        return None
    cover_backend = _cover_backend_settings()
    provider = cover_backend["provider"]
    expected_hash = _stable_hash(
        {
            "schema": "cover_v13_configurable_api_request_size",
            "marketing_bundle": marketing_bundle,
            "series_cover_label": str((metadata or {}).get("series_cover_label") or ""),
            "provider": provider,
            "base_url": cover_backend["base_url"],
            "model": cover_backend["model"],
            "request_width": int(cover_backend.get("request_width") or 0),
            "request_height": int(cover_backend.get("request_height") or 0),
            "style": config.llm_image_style_suffix,
            "prompt_template": config.get("cover_prompt_template", ""),
            "custom_prompt": config.get("cover_custom_prompt", ""),
            "analysis_prompt": config.get("cover_ai_analysis_prompt", ""),
            "poster_method_prompt": _full_cover_poster_method_prompt(),
            "width": int(config.get("cover_width", 1280) or 1280),
            "height": int(config.get("cover_height", 720) or 720),
            "title_mode": "three_candidates_single_cover_prompt",
            "api_key_hash": _text_hash(cover_backend["api_key"])[:16],
        }
    )
    manifest = _read_json(manifest_path, {})
    if (
        isinstance(manifest, dict)
        and manifest.get("status") == "ready"
        and manifest.get("input_hash") == expected_hash
        and out_path.exists()
        and out_path.stat().st_size > 1000
    ):
        on_log(f"  reuse cover: {out_path}")
        return out_path

    on_log(f"[cover] Generate from generated title and story synopsis: {title}")
    prompt = _build_cover_prompt(novel, segments, metadata=metadata, on_log=on_log)
    (cover_dir / "cover_prompt.txt").write_text(prompt, encoding="utf-8")
    width = int(config.get("cover_width", 1280) or 1280)
    height = int(config.get("cover_height", 720) or 720)
    backend = ImageBackend(
        provider=provider,
        base_url=cover_backend["base_url"],
        api_key=cover_backend["api_key"],
        model=cover_backend["model"],
        steps=config.image_steps,
        cfg=config.image_cfg,
        workflow_path=cover_backend.get("workflow_path", ""),
        timeout_seconds=config.get("image_api_timeout_seconds", 300),
        preserve_source=True,
        request_width=cover_backend.get("request_width", 0),
        request_height=cover_backend.get("request_height", 0),
    )
    provider_tmp = cover_dir / "cover_provider_raw.tmp.png"
    provider_tmp.unlink(missing_ok=True)
    provider_raw_path.unlink(missing_ok=True)
    prompt_versions = max(1, min(3, int(config.get("cover_policy_prompt_versions", 3) or 3)))
    transient_retries = max(1, min(5, int(config.get("cover_transient_retries", 3) or 3)))
    attempt_log = {
        "schema_version": 1,
        "input_hash": expected_hash,
        "marketing_bundle": marketing_bundle,
        "prompt_versions": [],
        "status": "running",
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    generated = False
    final_error = ""
    current_prompt = prompt
    for version_index in range(prompt_versions):
        version_record = {
            "version": version_index + 1,
            "prompt": current_prompt,
            "image_attempts": [],
        }
        attempt_log["prompt_versions"].append(version_record)
        (cover_dir / "cover_prompt.txt").write_text(current_prompt, encoding="utf-8")
        policy_rejected = False
        for image_attempt in range(transient_retries):
            provider_tmp.unlink(missing_ok=True)
            try:
                if provider == "placeholder":
                    _create_cover_placeholder(current_prompt, title, provider_tmp, width, height)
                else:
                    with external_api_slot(action="cover image"):
                        backend.generate(
                            current_prompt,
                            _cover_negative_prompt(),
                            provider_tmp,
                            width=width,
                            height=height,
                        )
                provider_tmp.replace(provider_raw_path)
                _fit_provider_cover_to_16_9(provider_raw_path, raw_path, width, height)
                with Image.open(provider_raw_path) as provider_image:
                    provider_size = list(provider_image.size)
                version_record["image_attempts"].append({
                    "attempt": image_attempt + 1,
                    "status": "success",
                    "provider_size": provider_size,
                    "adapted_size": [width, height],
                    "adaptation": "exact_16_9_output_fit",
                    "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                })
                generated = True
                prompt = current_prompt
                break
            except Exception as exc:
                provider_tmp.unlink(missing_ok=True)
                final_error = str(redact_secret_text(exc))
                kind = _cover_error_kind(exc)
                version_record["image_attempts"].append({
                    "attempt": image_attempt + 1,
                    "status": "failed",
                    "error_kind": kind,
                    "error": final_error[:1000],
                    "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                })
                attempt_log["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
                _write_json(attempts_path, attempt_log)
                if kind == "policy":
                    policy_rejected = True
                    on_log(f"  WARN cover prompt rejected by policy (version {version_index + 1}); rewrite only this prompt")
                    break
                if kind == "transient" and image_attempt + 1 < transient_retries:
                    on_log(f"  WARN temporary cover image error; retry same prompt ({image_attempt + 2}/{transient_retries})")
                    time.sleep(min(4, 2 * (image_attempt + 1)))
                    continue
                break
        if generated:
            break
        if policy_rejected and version_index + 1 < prompt_versions:
            rewritten = _rewrite_cover_prompt_for_policy(
                novel,
                segments,
                metadata,
                current_prompt,
                final_error,
                on_log=on_log,
            )
            if rewritten:
                current_prompt = rewritten
                continue
        break

    attempt_log["status"] = "success" if generated else "fallback"
    attempt_log["final_prompt_version"] = len(attempt_log["prompt_versions"])
    attempt_log["final_error"] = "" if generated else final_error[:1000]
    attempt_log["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    _write_json(attempts_path, attempt_log)
    if not generated:
        prompt = current_prompt
        on_log(f"  WARN cover image generation failed: {final_error}; use generated video image fallback")
        if not _create_cover_from_video_image(job_dir, raw_path, width, height):
            on_log("  WARN no generated image available for cover fallback; use placeholder")
            _create_cover_placeholder(prompt, title, raw_path, width, height)

    _finalize_cover_image(raw_path, out_path, width, height)
    _write_json(
        manifest_path,
        {
            "schema_version": 1,
            "status": "ready" if generated else "fallback",
            "input_hash": expected_hash,
            "title": title,
            "title_candidates": marketing_bundle["titles"],
            "series_cover_label": str((metadata or {}).get("series_cover_label") or ""),
            "synopsis_candidates": marketing_bundle["synopses"],
            "title_source": "metadata.titles[0]",
            "synopsis_source": "metadata.synopses[0]",
            "provider": provider,
            "prompt": prompt,
            "prompt_attempts_path": str(attempts_path),
            "provider_raw_path": str(provider_raw_path) if provider_raw_path.exists() else "",
            "raw_path": str(raw_path),
            "path": str(out_path),
            "file": _file_signature(out_path),
            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        },
    )
    on_log(f"  OK cover: {out_path}")
    return out_path


def _series_group_job_dirs(job_dir: Path) -> tuple[list[Path], int]:
    status = _read_json(job_dir / "status.json", {})
    group_key = str(status.get("series_group_key") or "").strip()
    series_title = str(status.get("series_title") or "").strip()
    try:
        expected = int(status.get("series_total") or 0)
    except (TypeError, ValueError):
        expected = 0
    entries: list[tuple[int, Path]] = []
    for candidate in JOBS_DIR.iterdir():
        if not candidate.is_dir():
            continue
        sibling = _read_json(candidate / "status.json", {})
        same_group = (
            bool(group_key)
            and str(sibling.get("series_group_key") or "").strip() == group_key
        ) or (
            not group_key
            and bool(series_title)
            and str(sibling.get("series_title") or "").strip() == series_title
        )
        if not same_group:
            continue
        try:
            episode = int(sibling.get("series_episode") or 0)
        except (TypeError, ValueError):
            episode = 0
        if episode > 0:
            entries.append((episode, candidate))
            expected = max(expected, episode)
    entries.sort(key=lambda item: (item[0], item[1].name))
    return [path for _episode, path in entries], expected


def regenerate_job_cover(
    job_id: str, on_log: LogFn = _noop, *, allow_running: bool = False, ensure_marketing: bool = True
) -> Path:
    """Regenerate only one job's cover from its saved text artifacts.

    The current GUI configuration is used intentionally. Existing narration,
    scene images, subtitles and video are left untouched. The previous cover
    is restored if an unexpected error interrupts regeneration.
    """
    if is_worker_running(job_id) and not allow_running:
        raise RuntimeError(f"{job_id} 正在运行，请先停止任务后再重新生成封面。")
    job_dir = _safe_job_path(job_id)
    if not job_dir.exists():
        raise FileNotFoundError(f"任务不存在：{job_id}")

    novel = _read_saved_novel(job_dir / "novel.json")
    segments = _read_saved_segments(job_dir / "segments.json")
    if novel is None or segments is None:
        raise RuntimeError("任务还没有完成文本整理，暂时不能单独重新生成封面。")
    metadata = _read_json(job_dir / "metadata.json", {})
    if not isinstance(metadata, dict):
        metadata = {}
    if ensure_marketing and not _metadata_has_marketing_candidates(metadata):
        saved_context = _read_json(job_dir / "story_visual_context.json", {})
        story_context = saved_context.get("context") if isinstance(saved_context, dict) else {}
        if not isinstance(story_context, dict):
            story_context = {}
        on_log("manual cover regeneration: old/missing marketing metadata; generate 3 titles, 2 synopses and tags first")
        metadata = stage_metadata(
            novel,
            job_dir,
            story_context,
            segments=segments,
            on_log=on_log,
        )

    previous_status = load_status(job_id, include_worker=False)
    previous_stage = str(previous_status.get("stage") or "queued")
    previous_progress = previous_status.get("progress", 0.0)
    previous_error = str(previous_status.get("error") or "")
    cover_dir = job_dir / "cover"
    backup_dir = job_dir / f".cover_before_regenerate_{secrets.token_hex(6)}"
    if cover_dir.exists():
        cover_dir.replace(backup_dir)

    write_status(job_dir, stage="cover", progress=0.89, error="", manual_cover_error="")
    on_log("manual cover regeneration started: keep audio, scene images and video")
    try:
        cover = stage_cover(
            novel,
            segments,
            job_dir,
            metadata=metadata,
            on_log=on_log,
            force=True,
        )
        if cover is None or not cover.exists():
            raise RuntimeError("封面生成没有产生可用文件。")
    except Exception as exc:
        if cover_dir.exists():
            shutil.rmtree(cover_dir)
        if backup_dir.exists():
            backup_dir.replace(cover_dir)
        safe_error = str(redact_secret_text(exc))
        write_status(
            job_dir,
            stage=previous_stage,
            progress=previous_progress,
            error=previous_error,
            manual_cover_error=safe_error,
        )
        on_log(f"manual cover regeneration failed: {safe_error}")
        raise

    if backup_dir.exists():
        shutil.rmtree(backup_dir)

    result_path = job_dir / "result.json"
    result = _read_json(result_path, {})
    if result_path.exists() and isinstance(result, dict):
        result["cover"] = str(cover)
        _write_json(result_path, result)

    # Keep an already-exported delivery package in sync without re-exporting
    # or touching its finished video.
    try:
        source_path = str(previous_status.get("source_path") or previous_status.get("input") or "")
        source = Path(source_path).expanduser()
        exported_directory = str(result.get("exported_directory") or "") if isinstance(result, dict) else ""
        exported = Path(exported_directory).expanduser() if exported_directory else None
        source_export = bool(
            source.is_file()
            and exported is not None
            and exported.is_dir()
            and exported.resolve() == source.parent.resolve()
        )
        if source.is_file() and (bool(config.get("output_to_input_directory", False)) or source_export):
            base = safe_job_name(source.stem, fallback="article")
            shutil.copy2(cover, source.parent.resolve() / f"{base}_cover{cover.suffix.lower() or '.jpg'}")
        if exported is not None and exported.is_dir() and not source_export:
            shutil.copy2(cover, exported / cover.name)
    except Exception as exc:
        on_log(f"  WARN regenerated cover could not be synced to the delivery folder: {redact_secret_text(exc)}")

    write_status(
        job_dir,
        stage=previous_stage,
        progress=previous_progress,
        cover=str(cover),
        error=previous_error,
        manual_cover_error="",
    )
    on_log(f"manual cover regeneration completed: {cover}")
    return cover


def regenerate_job_marketing_and_cover(
    job_id: str, on_log: LogFn = _noop, *, allow_running: bool = False
) -> Path:
    """Rebuild only publishing metadata and cover, never video-related media."""
    if is_worker_running(job_id) and not allow_running:
        raise RuntimeError(f"{job_id} 正在运行，请先停止任务后再重新生成。")
    job_dir = _safe_job_path(job_id)
    novel = _read_saved_novel(job_dir / "novel.json")
    segments = _read_saved_segments(job_dir / "segments.json")
    if novel is None or segments is None:
        raise RuntimeError("任务缺少已保存的正文或分段，无法只重做标题和封面。")
    saved_context = _read_json(job_dir / "story_visual_context.json", {})
    story_context = saved_context.get("context") if isinstance(saved_context, dict) else {}
    if not isinstance(story_context, dict):
        story_context = {}
    on_log("legacy marketing rebuild: regenerate titles/synopses, then cover; keep video/audio/images/subtitles")
    metadata = stage_metadata(novel, job_dir, story_context, segments=segments, on_log=on_log)
    metadata = apply_series_presentation(metadata, job_dir, on_log=on_log)
    _write_json(job_dir / "metadata.json", metadata)
    return regenerate_job_cover(job_id, on_log=on_log, allow_running=True)


def queue_marketing_cover_rebuild(job_ids: Iterable[str], on_log: LogFn = _noop) -> tuple[list[str], list[tuple[str, int]]]:
    """Durably queue metadata-and-cover-only repairs using current settings."""
    queued: list[str] = []
    for raw_job_id in job_ids:
        job_id = str(raw_job_id)
        if is_worker_running(job_id):
            continue
        job_dir = _safe_job_path(job_id)
        if not job_dir.exists():
            continue
        status = load_status(job_id, include_worker=False)
        input_text = str(status.get("input") or status.get("source_path") or "")
        if not input_text:
            continue
        _write_settings_snapshot(job_dir, config.as_dict())
        clear_worker_pid(job_id)
        write_status(
            job_dir, job_id=job_id, input=input_text, stage="queued", progress=0.80,
            worker_pid=None, error="", queued_compose_only=False,
            queued_marketing_cover_only=True, queued_at=time.strftime("%Y-%m-%d %H:%M:%S"),
        )
        append_log(job_dir, "queued: legacy marketing title/synopsis + cover rebuild (video preserved)")
        queued.append(job_id)
    started: list[tuple[str, int]] = []
    max_jobs = max(1, int(config.get("max_concurrent_jobs", 2)))
    while count_running_workers() < max_jobs:
        next_job = start_next_queued_job(on_log=on_log)
        if next_job is None:
            break
        started.append(next_job)
    return queued, started


def _create_cover_from_video_image(job_dir: Path, out_path: Path, width: int, height: int) -> bool:
    image_dir = job_dir / "images"
    candidates = sorted(image_dir.glob("img_*.png")) + sorted(image_dir.glob("*.jpg")) + sorted(image_dir.glob("*.jpeg"))
    for src in candidates:
        try:
            with Image.open(src) as img:
                fitted = ImageOps.fit(img.convert("RGB"), (max(1, int(width)), max(1, int(height))), method=Image.LANCZOS)
                fitted.save(out_path, "PNG")
                return True
        except Exception:
            continue
    return False


def _load_compose_manifest(path: Path) -> dict:
    data = _read_json(path, {})
    if not isinstance(data, dict):
        data = {}
    if not isinstance(data.get("steps"), dict):
        data["steps"] = {}
    data["schema_version"] = 1
    return data


def _compose_step_ready(manifest: dict, step: str, input_hash: str, out_path: Path) -> bool:
    data = (manifest.get("steps") or {}).get(step) or {}
    if str(data.get("input_hash") or "") != input_hash:
        return False
    if not out_path.exists():
        return False
    sig = _file_signature(out_path)
    if int(data.get("file_size") or -1) != sig.get("size"):
        return False
    try:
        if abs(float(data.get("file_mtime") or 0) - float(sig.get("mtime") or 0)) > 1.0:
            return False
    except Exception:
        return False
    return _audio_duration(out_path) > 0.05


def _record_compose_step(manifest_path: Path, manifest: dict, step: str, input_hash: str, out_path: Path) -> None:
    sig = _file_signature(out_path)
    steps = manifest.setdefault("steps", {})
    steps[step] = {
        "status": "ready",
        "input_hash": input_hash,
        "path": str(out_path),
        "duration": round(_audio_duration(out_path), 3),
        "file_size": sig.get("size", 0),
        "file_mtime": sig.get("mtime", 0),
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    manifest["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    _write_json(manifest_path, manifest)


def _stage_compose_manifest_impl(
    audios: list[Path],
    durations: list[float],
    segments: list[Segment],
    plans: list[ImagePlan],
    images: list[Path],
    job_dir: Path,
    on_log: LogFn,
) -> Path:
    on_log("[6/6] Video compose")
    manifest_path = job_dir / "compose_manifest.json"
    manifest = _load_compose_manifest(manifest_path)

    audio_full = job_dir / "audio_full.mp3"
    audio_hash = _stable_hash(
        {
            "kind": "audio_concat_v1",
            "audios": [{"path": str(p), **_file_signature(p)} for p in audios],
        }
    )
    if _compose_step_ready(manifest, "audio_concat", audio_hash, audio_full):
        on_log("  reuse audio_full.mp3 from compose manifest")
    else:
        concat_audios(audios, audio_full, on_log)
        _record_compose_step(manifest_path, manifest, "audio_concat", audio_hash, audio_full)

    subs = []
    t = 0.0
    for seg, dur in zip(segments, durations):
        end = t + max(0.05, dur)
        subs.append((t, end, seg.text))
        t = end

    ass_path = job_dir / "subtitle.ass"
    build_ass(
        subs,
        ass_path,
        font=config.video_subtitle_font,
        size=config.video_subtitle_size,
        video_w=config.video_width,
        video_h=config.video_height,
        position=config.get("video_subtitle_position", "下边"),
        primary_color=config.get("video_subtitle_color", "#FFFFFF"),
        outline_color=config.get("video_subtitle_outline_color", "#000000"),
        back_color=config.get("video_subtitle_back_color", "#000000"),
        outline=float(config.get("video_subtitle_outline", 4.0) or 4.0),
        shadow=float(config.get("video_subtitle_shadow", 1.0) or 1.0),
        margin_v=int(config.get("video_subtitle_margin_v", 60) or 60),
        margin_lr=int(config.get("video_subtitle_margin_lr", 56) or 56),
        spacing=float(config.get("video_subtitle_spacing", 0.0) or 0.0),
        bold=bool(config.get("video_subtitle_bold", False)),
        italic=bool(config.get("video_subtitle_italic", False)),
        chars_per_line=int(config.get("video_subtitle_chars_per_line", 24) or 24),
        max_lines=int(config.get("video_subtitle_max_lines", 2) or 2),
    )
    # Always keep an SRT fallback. FFmpeg builds without libass cannot burn ASS
    # subtitles, but can still embed SRT as a selectable mov_text track.
    build_srt(
        subs,
        job_dir / "subtitle.srt",
        chars_per_line=int(config.get("video_subtitle_chars_per_line", 24) or 24),
        max_lines=int(config.get("video_subtitle_max_lines", 2) or 2),
    )

    bgm = Path(config.video_bgm_path) if config.video_bgm_path else None
    image_durations = [p.duration for p in plans]
    out = video_output_path(job_dir)
    final_hash = _stable_hash(
        {
            "kind": "final_video_v2",
            # Bump when composition semantics change so an old cached render
            # (for example, one rendered with motion suppressed) is not reused.
            "composer_revision": 4,
            "audio": {"path": str(audio_full), **_file_signature(audio_full)},
            "images": [{"path": str(p), **_file_signature(p)} for p in images],
            "image_durations": [round(float(v), 3) for v in image_durations],
            "segments": [_text_hash(s.text) for s in segments],
            "video": {
                "width": config.video_width,
                "height": config.video_height,
                "fps": config.video_fps,
                "encoder": config.get("video_encoder", "libx264"),
                "encoder_preset": config.get("video_encoder_preset", "veryfast"),
                "encoder_quality": config.get("video_encoder_quality", 20),
                "ken_burns": bool(config.ken_burns),
                "long_mode": bool(config.video_long_mode),
                "motion": str(config.video_motion or "none"),
                "motion_curve": str(config.get("video_motion_curve", "ease") or "ease"),
                "motion_cycle_seconds": float(config.get("video_motion_cycle_seconds", 18.0) or 18.0),
                "transition": str(config.video_transition or "none"),
                "transition_duration": float(config.video_transition_duration or 0.4),
                "subtitle": True,
                "subtitle_font": config.video_subtitle_font,
                "subtitle_size": config.video_subtitle_size,
                "subtitle_style": {
                    "position": config.get("video_subtitle_position", "下边"),
                    "color": config.get("video_subtitle_color", "#FFFFFF"),
                    "outline_color": config.get("video_subtitle_outline_color", "#000000"),
                    "back_color": config.get("video_subtitle_back_color", "#000000"),
                    "outline": config.get("video_subtitle_outline", 4.0),
                    "shadow": config.get("video_subtitle_shadow", 1.0),
                    "margin_v": config.get("video_subtitle_margin_v", 60),
                    "margin_lr": config.get("video_subtitle_margin_lr", 56),
                    "spacing": config.get("video_subtitle_spacing", 0.0),
                    "bold": config.get("video_subtitle_bold", False),
                    "italic": config.get("video_subtitle_italic", False),
                    "chars_per_line": config.get("video_subtitle_chars_per_line", 24),
                    "max_lines": config.get("video_subtitle_max_lines", 2),
                },
                "bgm": str(bgm) if bgm else "",
                "bgm_sig": _file_signature(bgm) if bgm and bgm.exists() else {},
                "bgm_volume": config.video_bgm_volume,
            },
        }
    )
    if _compose_step_ready(manifest, "final_video", final_hash, out):
        on_log(f"  reuse final video: {out}")
        return out

    tmp = out.with_name(out.stem + ".tmp" + out.suffix)
    tmp.unlink(missing_ok=True)
    try:
        build_video(
            images=images,
            image_durations=image_durations,
            audio_path=audio_full,
            out_path=tmp,
            on_log=on_log,
            width=config.video_width,
            height=config.video_height,
            fps=config.video_fps,
            ken_burns=bool(config.ken_burns),
            subtitle_ass=ass_path,
            bgm_path=bgm,
            bgm_volume=config.video_bgm_volume,
            long_mode=config.video_long_mode,
            cleanup_temp=config.video_cleanup_temp,
            motion=str(config.video_motion or "none"),
            motion_curve=str(config.get("video_motion_curve", "ease") or "ease"),
            motion_cycle_seconds=float(config.get("video_motion_cycle_seconds", 18.0) or 18.0),
            transition=config.video_transition,
            transition_duration=float(config.video_transition_duration or 0.4),
            video_encoder=str(config.get("video_encoder", "libx264") or "libx264"),
            video_encoder_preset=str(config.get("video_encoder_preset", "veryfast") or "veryfast"),
            video_encoder_quality=int(config.get("video_encoder_quality", 20) or 20),
        )
        video_duration = _audio_duration(tmp)
        audio_duration = _audio_duration(audio_full)
        if video_duration <= 0.05:
            raise RuntimeError("final video duration is zero")
        if audio_duration > 60 and video_duration < audio_duration * 0.75:
            raise RuntimeError(f"final video too short: video={video_duration:.1f}s audio={audio_duration:.1f}s")
        if audio_duration > 60 and video_duration < audio_duration * 0.95:
            on_log(f"  WARN final video shorter than audio: video={video_duration:.1f}s audio={audio_duration:.1f}s")
        tmp.replace(out)
        _record_compose_step(manifest_path, manifest, "final_video", final_hash, out)
    finally:
        tmp.unlink(missing_ok=True)
    on_log(f"  OK video completed: {out}")
    return out


def stage_upload(
    video: Path,
    title: str,
    cover: Path | None,
    job_dir: Path,
    metadata: dict | None = None,
    on_log: LogFn = _noop,
    *,
    force: bool = False,
    profile_name_override: str = "",
    schedule_enabled_override: bool | None = None,
    scheduled_at_override: str = "",
    schedule_timezone_override: str = "",
    browser_upload_job=None,
) -> str:
    if not force and not bool(config.upload_enabled):
        return ""
    profiles = _selected_upload_profiles(profile_name_override)
    if not profiles:
        return ""
    active_mode = str(profiles[0].get("publish_mode") or config.get("youtube_publish_mode", "immediate") or "immediate")
    if active_mode not in {"immediate", "youtube", "script"}:
        active_mode = "immediate"
    schedule_requested = active_mode == "youtube"
    if active_mode in {"youtube", "script"} and not force:
        if active_mode == "script":
            on_log("  脚本内定时已开启：成片保留在本地，等待 GUI 发布队列到点自动上传。")
        else:
            on_log("  油管内定时目前处于单视频测试阶段：已跳过生成后的自动上传，请在 GUI 选中成片后测试。")
        return ""
    if str(config.youtube_upload_method or "browser").lower() != "browser":
        on_log("  WARN only browser upload is wired in this build")
        return ""
    metadata = metadata or {}
    if metadata.get("titles") and not _metadata_has_valid_marketing_titles(metadata):
        raise RuntimeError(
            "标题候选无效，已阻止上传：请先重新生成标题/概梗，不能把“标题：”“简介：”或“local text input”等内部任务文字发布到 YouTube。"
        )
    clean_title = str(metadata.get("clean_title") or _clean_display_title(title))
    context = {
        "title": title,
        "clean_title": clean_title,
        "short_title": metadata.get("short_title", ""),
        "candidate_title": metadata.get("short_title", "") or clean_title,
        "intro": metadata.get("intro", ""),
        "author": metadata.get("author", ""),
        "tags": _remove_disallowed_upload_tag(str(config.youtube_tags or "")),
        "job_id": job_dir.name,
        "series_title": "",
        "series_episode": "",
        "series_total": "",
        "series_label": "",
    }
    schedule_enabled = bool(force and schedule_requested)
    if schedule_enabled_override is not None:
        schedule_enabled = bool(schedule_enabled_override)
    scheduled_at = ""
    schedule_timezone = str(
        schedule_timezone_override or config.get("youtube_schedule_timezone", "Asia/Tokyo") or "Asia/Tokyo"
    ).strip()
    if schedule_enabled:
        if scheduled_at_override:
            try:
                scheduled_local = datetime.strptime(str(scheduled_at_override), "%Y-%m-%dT%H:%M")
            except ValueError as exc:
                raise ValueError("指定的油管定时时间格式不正确") from exc
        else:
            schedule_date = str(config.get("youtube_schedule_date", "") or "").strip()
            schedule_time = str(config.get("youtube_schedule_time", "") or "").strip()
            try:
                scheduled_local = datetime.strptime(f"{schedule_date} {schedule_time}", "%Y-%m-%d %H:%M")
            except ValueError as exc:
                raise ValueError("定时发布日期或时间格式不正确，应为 YYYY-MM-DD 和 HH:MM") from exc
        if scheduled_local <= datetime.now() + timedelta(minutes=5):
            raise ValueError("定时发布时间必须至少晚于当前时间 5 分钟")
        scheduled_at = scheduled_local.strftime("%Y-%m-%dT%H:%M")
        on_log(f"  scheduled publish test: {scheduled_at} timezone={schedule_timezone}")
    on_log(f"[7/7] YouTube browser upload: {len(profiles)} profile(s)")
    lock_path = _acquire_upload_lock(job_dir, on_log)
    results: list[dict] = []
    try:
        _ensure_upload_dependencies(on_log)
        from app.upload import upload_to_youtube

        for index, profile in enumerate(profiles, start=1):
            title_template = str(profile.get("title_template") or config.youtube_title_template or "{candidate_title}")
            description_template = str(profile.get("description") or config.youtube_description or "")
            flow = _normalize_upload_flow(profile.get("flow") or config.browser_flow or "simple")
            visibility = str(profile.get("visibility") or config.youtube_visibility or "PRIVATE").strip().upper()
            upload_policy = str(profile.get("upload_policy") or config.browser_upload_policy or "BTRA")
            ad_interval = _to_int(profile.get("ad_interval") or config.browser_ad_interval, 60)
            ad_start = _to_int(profile.get("ad_start") or config.browser_ad_start, 0)
            chrome_profile = str(profile.get("chrome_profile") or config.get("browser_chrome_profile", "Default") or "Default").strip() or "Default"
            ad_suitability_template = str(
                profile.get("ad_suitability_template") or config.get("browser_ad_suitability_template", "") or ""
            )
            title_limit = int(config.get("youtube_title_max_chars", 100) or 100)
            selected_candidate = _persistent_upload_candidate(job_dir, profile, metadata)
            profile_context = dict(context)
            if selected_candidate:
                chosen_title = str(selected_candidate.get("candidate_title") or "").strip()
                profile_context["candidate_title"] = chosen_title
                profile_context["short_title"] = chosen_title
            series_title = str(profile.get("series_title") or "").strip()
            series_episode = str(profile.get("series_episode") or "").strip()
            series_total = str(profile.get("series_total") or "").strip()
            series_format = str(profile.get("series_format") or "第{episode}话").strip()
            if series_title and series_episode:
                try:
                    episode_label = series_format.format(episode=series_episode, total=series_total)
                except (KeyError, ValueError):
                    episode_label = series_format
                series_prefix = f"{series_title}｜{episode_label}" if episode_label else series_title
                profile_context["series_title"] = series_title
                profile_context["series_episode"] = series_episode
                profile_context["series_total"] = series_total
                profile_context["series_label"] = episode_label
                selected = _strip_series_part_prefix(str(profile_context.get("candidate_title") or clean_title))
                profile_context["candidate_title"] = f"{series_prefix}｜{selected}"
                profile_context["short_title"] = profile_context["candidate_title"]
            elif series_animation_enabled_for_job(job_dir) and str(metadata.get("series_upload_prefix") or "").strip():
                # Automatic series presentation is derived from the completed
                # normal titles.  Keep the series/part prefix even when the
                # channel has no manually configured series profile.
                auto_prefix = str(metadata.get("series_upload_prefix") or "").strip()
                profile_context["series_title"] = str(metadata.get("series_short_title") or "")
                profile_context["series_label"] = str(metadata.get("series_part_label") or "")
                selected = _strip_series_part_prefix(str(profile_context.get("candidate_title") or clean_title))
                if metadata.get("series_upload_include_ai_title") is False:
                    profile_context["candidate_title"] = str(
                        metadata.get("series_display_title") or auto_prefix
                    ).strip()
                else:
                    profile_context["candidate_title"] = selected if selected.startswith(auto_prefix) else f"{auto_prefix}{selected}"
                profile_context["short_title"] = profile_context["candidate_title"]
            fallback_title = str(profile_context.get("candidate_title") or clean_title)
            upload_title = _limit_upload_title(
                _format_template(title_template, profile_context, fallback=fallback_title),
                title_limit,
            ) or fallback_title
            if series_title and series_episode:
                # 无论频道模板使用哪个标题变量，系列前缀都必须稳定保留。
                prefix = f"{series_title}｜{episode_label}｜" if episode_label else f"{series_title}｜"
                if not upload_title.startswith(prefix):
                    upload_title = _limit_upload_title(prefix + upload_title, title_limit) or prefix.rstrip("｜")
            elif series_animation_enabled_for_job(job_dir) and str(metadata.get("series_upload_prefix") or "").strip():
                prefix = str(metadata.get("series_upload_prefix") or "").strip()
                if not upload_title.startswith(prefix):
                    upload_title = _limit_upload_title(prefix + upload_title, title_limit) or prefix.rstrip("｜")
            upload_title = _append_generated_tags_to_upload_title(
                upload_title,
                _safe_generated_tags_for_upload(
                    metadata.get("generated_tags") or metadata.get("generated_tag_line") or [],
                    title,
                    job_dir.name,
                    metadata.get("clean_title"),
                    metadata.get("short_title"),
                    metadata.get("intro"),
                    metadata.get("story_brief"),
                ),
                title_limit,
            )
            upload_description = _remove_disallowed_upload_tag(
                _format_template(description_template, profile_context, fallback="")
            )
            profile_name = str(profile.get("name") or f"上传方案{index}").strip() or f"上传方案{index}"
            on_log(f"  [{index}/{len(profiles)}] profile={profile_name} chrome_profile={chrome_profile} flow={'精简' if flow == 'simple' else '完整'}")
            if selected_candidate:
                on_log(
                    f"    random title candidate {selected_candidate.get('candidate_index')}: "
                    f"{selected_candidate.get('candidate_title')}"
                )

            def _profile_progress(progress, *, current=index, total=len(profiles)):
                try:
                    value = float(progress or 0)
                except Exception:
                    value = 0.0
                if total > 1:
                    value = ((current - 1) * 100.0 + max(0.0, min(100.0, value))) / total
                write_status(job_dir, stage="upload", upload_progress=value)

            video_id = upload_to_youtube(
                video,
                upload_title,
                description=upload_description,
                visibility=visibility,
                cover_path=cover if cover and cover.exists() else None,
                flow=flow,
                upload_policy=upload_policy,
                ad_interval=ad_interval,
                ad_start=ad_start,
                chrome_profile=chrome_profile,
                ad_suitability_template=ad_suitability_template,
                browser_profiles=str(config.get("browser_profiles", "[]") or "[]"),
                browser_active_profile=profile_name,
                upload_all_profiles=False,
                # Port 9222 may still belong to another channel's Chrome
                # profile. Always restart with the selected profile before
                # uploading, including single-channel uploads.
                force_profile_launch=True,
                auto_restart=bool(config.browser_auto_restart),
                stall_timeout_min=int(config.browser_stall_timeout_min or 10),
                op_speed=str(config.browser_op_speed or "normal"),
                schedule_enabled=schedule_enabled,
                scheduled_at=scheduled_at,
                schedule_timezone=schedule_timezone,
                job=browser_upload_job,
                on_log=on_log,
                on_progress=_profile_progress,
            )
            if not video_id:
                raise RuntimeError(f"YouTube upload failed: {profile_name}")
            if video_id == "SCHEDULE_UNCONFIRMED":
                review_result = {
                    "profile": profile_name,
                    "chrome_profile": chrome_profile,
                    "video_id": "",
                    "url": "",
                    "title": upload_title,
                    "description": upload_description,
                    "visibility": "SCHEDULED",
                    "flow": flow,
                    "publish_mode": "scheduled",
                    "scheduled_at": scheduled_at,
                    "schedule_timezone": schedule_timezone,
                    "schedule_status": "needs_review",
                }
                _write_json(job_dir / "upload_result.json", {"uploads": results + [review_result], **review_result})
                raise RuntimeError("已经点击 YouTube 预定按钮，但未确认到成功弹窗；为避免重复上传，已停止重试，请到 Studio 检查。")
            url = f"https://www.youtube.com/watch?v={video_id}" if re.fullmatch(r"[A-Za-z0-9_-]{11}", str(video_id)) else ""
            results.append(
                {
                    "profile": profile_name,
                    "chrome_profile": chrome_profile,
                    "video_id": video_id,
                    "url": url,
                    "title": upload_title,
                    "description": upload_description,
                    "visibility": "SCHEDULED" if schedule_enabled else visibility,
                    "flow": flow,
                    "publish_mode": "scheduled" if schedule_enabled else "immediate",
                    "scheduled_at": scheduled_at if schedule_enabled else "",
                    "schedule_timezone": schedule_timezone if schedule_enabled else "",
                    "schedule_status": "scheduled" if schedule_enabled else "published",
                }
            )
            _write_json(job_dir / "upload_result.json", {"uploads": results, **results[0]})
            if schedule_enabled:
                on_log(f"  OK scheduled [{profile_name}] {scheduled_at}: {url or video_id}")
            else:
                on_log(f"  OK uploaded [{profile_name}]: {url}")
    finally:
        _release_upload_lock(lock_path, job_dir.name)
    if not results:
        raise RuntimeError("YouTube upload failed")
    _write_json(job_dir / "upload_result.json", {"uploads": results, **results[0]})
    if len(results) > 1:
        on_log(f"  OK uploaded {len(results)} copies")
    return str(results[0].get("url") or "")


def upload_completed_job(
    job_id: str,
    on_log: LogFn = _noop,
    *,
    profile_name_override: str = "",
    schedule_enabled_override: bool | None = None,
    scheduled_at_override: str = "",
    schedule_timezone_override: str = "",
    browser_upload_job=None,
) -> str:
    """Upload an existing finished video without restarting its production stages."""
    job_dir = job_dir_for(job_id)
    status = load_status(job_id)
    if status.get("worker_alive"):
        raise RuntimeError("任务仍在制作中，请完成后再单独上传。")
    video = Path(str(status.get("video") or video_output_path(job_dir)))
    if not video.exists() or video.stat().st_size < 100:
        raise FileNotFoundError("没有找到已完成的视频，无法上传。")
    metadata = _read_json(job_dir / "metadata.json", {})
    if not isinstance(metadata, dict):
        metadata = {}
    cover_value = str(status.get("cover") or "")
    cover = Path(cover_value) if cover_value else None
    if cover is not None and not cover.exists():
        cover = None
    title = str(metadata.get("title") or status.get("title") or job_id)
    write_status(job_dir, stage="upload", progress=1.0, upload_progress=0.0, error="")
    append_log(job_dir, "manual upload started for completed video")
    try:
        url = stage_upload(
            video,
            title,
            cover,
            job_dir,
            metadata=metadata,
            on_log=on_log,
            force=True,
            profile_name_override=profile_name_override,
            schedule_enabled_override=schedule_enabled_override,
            scheduled_at_override=scheduled_at_override,
            schedule_timezone_override=schedule_timezone_override,
            browser_upload_job=browser_upload_job,
        )
    except Exception as exc:
        write_status(job_dir, stage="completed", progress=1.0, upload_error=str(exc))
        append_log(job_dir, f"manual upload failed: {exc}")
        raise
    write_status(job_dir, stage="completed", progress=1.0, youtube_url=url, upload_error="")
    append_log(job_dir, f"manual upload completed: {url}")
    return url


def _upload_profiles() -> list[dict]:
    try:
        raw = json.loads(str(config.get("browser_profiles", "[]") or "[]"))
    except Exception:
        raw = []
    profiles = [p for p in raw if isinstance(p, dict)]
    if profiles:
        return profiles
    return [
        {
            "name": "无创收精简流程",
            "flow": "simple",
            "upload_policy": "BTRA",
            "ad_interval": 60,
            "ad_start": 0,
            "visibility": str(config.youtube_visibility or "PUBLIC"),
            "chrome_profile": str(config.get("browser_chrome_profile", "Default") or "Default"),
            "title_template": "",
            "description": "",
            "ad_suitability_template": str(config.get("browser_ad_suitability_template", "") or ""),
        },
        {
            "name": "完整创收流程",
            "flow": "full",
            "upload_policy": str(config.browser_upload_policy or "BTRA"),
            "ad_interval": int(config.browser_ad_interval or 60),
            "ad_start": int(config.browser_ad_start or 0),
            "visibility": str(config.youtube_visibility or "PUBLIC"),
            "chrome_profile": str(config.get("browser_chrome_profile", "Default") or "Default"),
            "title_template": "",
            "description": "",
            "ad_suitability_template": str(config.get("browser_ad_suitability_template", "") or ""),
        },
    ]


def _to_int(value, default: int = 0) -> int:
    try:
        return int(float(value))
    except Exception:
        return default


def _bool_upload_profile_value(value, default: bool = True) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    text = str(value).strip().lower()
    if text in {"0", "false", "no", "off", "disabled", "关闭"}:
        return False
    if text in {"1", "true", "yes", "on", "enabled", "开启"}:
        return True
    return default


def _normalize_upload_profile(profile: dict, index: int = 0) -> dict:
    if not isinstance(profile, dict):
        profile = {}
    name = str(profile.get("name") or f"上传方案{index + 1}").strip() or f"上传方案{index + 1}"
    return {
        **profile,
        "name": name,
        "enabled": _bool_upload_profile_value(profile.get("enabled"), True),
        "flow": _normalize_upload_flow(profile.get("flow") or config.browser_flow or "simple"),
        "upload_policy": str(profile.get("upload_policy") or config.browser_upload_policy or "BTRA").strip() or "BTRA",
        "ad_interval": _to_int(profile.get("ad_interval") or config.browser_ad_interval, 60),
        "ad_start": _to_int(profile.get("ad_start") or config.browser_ad_start, 0),
        "visibility": str(profile.get("visibility") or config.youtube_visibility or "PRIVATE").strip().upper() or "PRIVATE",
        "chrome_profile": str(profile.get("chrome_profile") or config.get("browser_chrome_profile", "Default") or "Default").strip() or "Default",
        "title_template": str(profile.get("title_template") or ""),
        "description": str(profile.get("description") or ""),
        "ad_suitability_template": str(profile.get("ad_suitability_template") or ""),
    }


def _active_upload_profile() -> dict:
    profiles = [_normalize_upload_profile(profile, idx) for idx, profile in enumerate(_upload_profiles())]
    active = str(config.get("browser_active_profile", "") or "").strip()
    for profile in profiles:
        if str(profile.get("name", "")).strip() == active:
            return dict(profile)
    return dict(profiles[0]) if profiles else {}


def _selected_upload_profiles(profile_name_override: str = "") -> list[dict]:
    profiles = [_normalize_upload_profile(profile, idx) for idx, profile in enumerate(_upload_profiles())]
    if profile_name_override:
        selected = [dict(profile) for profile in profiles if str(profile.get("name") or "") == profile_name_override]
        if not selected:
            raise ValueError(f"找不到脚本定时队列指定的频道方案：{profile_name_override}")
        return selected
    if bool(config.get("browser_upload_all_profiles", False)):
        enabled = [dict(profile) for profile in profiles if profile.get("enabled", True)]
        return enabled or [_active_upload_profile()]
    return [_active_upload_profile()]


def _normalize_upload_flow(value: str) -> str:
    text = str(value or "").strip().lower()
    if "full" in text or "完整" in text or "创收" in text:
        return "full"
    return "simple"


def stage_compose(
    audios: list[Path],
    durations: list[float],
    segments: list[Segment],
    plans: list[ImagePlan],
    images: list[Path],
    job_dir: Path,
    on_log: LogFn = _noop,
) -> Path:
    return _stage_compose_manifest_impl(audios, durations, segments, plans, images, job_dir, on_log)


def resume_compose_only(job_id: str, on_log: LogFn = _noop) -> Path:
    """Build only the final video from durable job artifacts; never calls AI services."""
    job_dir = _safe_job_path(job_id)
    segments = _load_job_segments(job_dir)
    rows = _read_json(job_dir / "plans.json", [])
    if not isinstance(rows, list) or not rows:
        raise RuntimeError("缺少 plans.json，无法仅重试合成。")
    plans: list[ImagePlan] = []
    for pos, row in enumerate(rows):
        if not isinstance(row, dict):
            continue
        plan_segments = [
            Segment(index=int(item.get("index", 0)), text=str(item.get("text") or ""))
            for item in row.get("segments", []) if isinstance(item, dict)
        ]
        plans.append(ImagePlan(
            image_index=int(row.get("image_index", pos)),
            segments=plan_segments,
            duration=float(row.get("duration") or 0.0),
            highlight_segment_indexes=[int(i) for i in row.get("highlight_segment_indexes", [])],
            highlight_text=str(row.get("highlight_text") or ""),
            highlight_people=[str(i) for i in row.get("highlight_people", [])],
            highlight_location=str(row.get("highlight_location") or ""),
            highlight_action=str(row.get("highlight_action") or ""),
        ))
    plans.sort(key=lambda p: p.image_index)
    selection = _read_json(job_dir / IMAGE_SELECTION_FILE, {})
    selected_paths = selection.get("paths") if isinstance(selection, dict) else None
    if isinstance(selected_paths, list) and len(selected_paths) == len(plans):
        images = [job_dir / str(path) for path in selected_paths]
    else:
        images = [job_dir / "images" / f"img_{i:05d}.png" for i in range(len(plans))]
    if len(plans) != len(images) or any(not path.exists() for path in images):
        raise RuntimeError("合成所需图片不完整；不会自动出图，请先检查图片缓存。")
    durations = _read_json(job_dir / "durations.json", [])
    if not isinstance(durations, list) or len(durations) != len(segments):
        raise RuntimeError("缺少或无效 durations.json，无法仅重试合成。")
    imported = _job_imported_audio(job_dir)
    audios = [imported[0]] if imported else [job_dir / "audio" / f"seg_{i:05d}.mp3" for i in range(len(segments))]
    if any(not path.exists() for path in audios):
        raise RuntimeError("配音尚未完成；请先选择补完 TTS 后再合成。")
    on_log("== compose-only retry: reuse existing images and audio; no image generation ==")
    return stage_compose(audios, [float(value) for value in durations], segments, plans, images, job_dir, on_log)
    audio_full = job_dir / "audio_full.mp3"
    if not audio_full.exists() or _audio_duration(audio_full) <= 0.05:
        concat_audios(audios, audio_full, on_log)

    subs = []
    t = 0.0
    for seg, dur in zip(segments, durations):
        end = t + max(0.05, dur)
        subs.append((t, end, seg.text))
        t = end

    ass_path = job_dir / "subtitle.ass"
    build_ass(
        subs,
        ass_path,
        font=config.video_subtitle_font,
        size=config.video_subtitle_size,
        video_w=config.video_width,
        video_h=config.video_height,
        position=config.get("video_subtitle_position", "下边"),
        primary_color=config.get("video_subtitle_color", "#FFFFFF"),
        outline_color=config.get("video_subtitle_outline_color", "#000000"),
        back_color=config.get("video_subtitle_back_color", "#000000"),
        outline=float(config.get("video_subtitle_outline", 4.0) or 4.0),
        shadow=float(config.get("video_subtitle_shadow", 1.0) or 1.0),
        margin_v=int(config.get("video_subtitle_margin_v", 60) or 60),
        margin_lr=int(config.get("video_subtitle_margin_lr", 56) or 56),
        spacing=float(config.get("video_subtitle_spacing", 0.0) or 0.0),
        bold=bool(config.get("video_subtitle_bold", False)),
        italic=bool(config.get("video_subtitle_italic", False)),
        chars_per_line=int(config.get("video_subtitle_chars_per_line", 24) or 24),
        max_lines=int(config.get("video_subtitle_max_lines", 2) or 2),
    )
    # Always keep an SRT fallback for FFmpeg builds without libass.
    build_srt(
        subs,
        job_dir / "subtitle.srt",
        chars_per_line=int(config.get("video_subtitle_chars_per_line", 24) or 24),
        max_lines=int(config.get("video_subtitle_max_lines", 2) or 2),
    )

    bgm = Path(config.video_bgm_path) if config.video_bgm_path else None
    out = video_output_path(job_dir)
    build_video(
        images=images,
        image_durations=[p.duration for p in plans],
        audio_path=audio_full,
        out_path=out,
        on_log=on_log,
        width=config.video_width,
        height=config.video_height,
        fps=config.video_fps,
        ken_burns=bool(config.ken_burns),
        subtitle_ass=ass_path,
        bgm_path=bgm,
        bgm_volume=config.video_bgm_volume,
        long_mode=config.video_long_mode,
        cleanup_temp=config.video_cleanup_temp,
        motion=str(config.video_motion or "none"),
        motion_curve=str(config.get("video_motion_curve", "ease") or "ease"),
        motion_cycle_seconds=float(config.get("video_motion_cycle_seconds", 18.0) or 18.0),
        transition=config.video_transition,
        transition_duration=float(config.video_transition_duration or 0.4),
        video_encoder=str(config.get("video_encoder", "libx264") or "libx264"),
        video_encoder_preset=str(config.get("video_encoder_preset", "veryfast") or "veryfast"),
        video_encoder_quality=int(config.get("video_encoder_quality", 20) or 20),
    )
    on_log(f"  OK 视频完成: {out}")
    return out


def run_acceleration_preprocess(
    url_or_id: str,
    *,
    job_id: str,
    on_log: LogFn = _noop,
) -> dict:
    """Prepare one queued job through remote API stages, then pause before TTS."""
    job_dir = job_dir_for(job_id)
    job_dir.mkdir(parents=True, exist_ok=True)

    def log(message: str) -> None:
        safe = redact_secret_text(message)
        append_log(job_dir, safe)
        on_log(safe)

    url_or_id = _resolve_job_input_source(job_dir, url_or_id, on_log=log)
    write_status(
        job_dir,
        job_id=job_id,
        input=url_or_id,
        stage="api_preprocessing",
        progress=0.04,
        worker_pid=os.getpid(),
        acceleration_preprocess=True,
        error="",
    )
    log("== 加速模式：开始 API 预处理；不会启动 TTS 或 FFmpeg ==")
    imported_audio_mode = (job_dir / IMPORTED_AUDIO_MANIFEST).exists()
    novel = _read_saved_novel(job_dir / "novel.json")
    segments = _read_saved_segments(job_dir / "segments.json")
    if novel is None or segments is None:
        novel = stage_scrape(
            url_or_id,
            site=config.scraper_site,
            max_chars=0 if imported_audio_mode else config.scraper_max_chars,
            on_log=log,
        )
        _validate_job_novel_source(job_dir, novel)
        _write_novel(job_dir / "novel.json", novel)
        segments = stage_clean(
            novel,
            on_log=log,
            job_dir=job_dir,
            preserve_source_text=imported_audio_mode,
        )
        _apply_series_title_to_novel(novel, job_dir, log)
        _write_novel(job_dir / "novel.json", novel)
        _write_json(job_dir / "segments.json", [{"i": s.index, "text": s.text} for s in segments])
    else:
        log(f"  加速模式：复用正文和 {len(segments)} 个已洗稿分段")
    _validate_job_novel_source(job_dir, novel, segments)

    write_status(job_dir, stage="api_preprocessing", progress=0.07, title=novel.title)
    story_context = stage_story_context(novel, segments, job_dir, on_log=log)
    metadata = stage_metadata(novel, job_dir, story_context, segments=segments, on_log=log)
    if series_animation_enabled_for_job(job_dir):
        metadata = apply_series_presentation(metadata, job_dir, on_log=log)
        _write_json(job_dir / "metadata.json", metadata)
    character_analysis = stage_character_analysis(novel, segments, job_dir, on_log=log)
    character_analysis = share_series_character_analysis(job_dir, character_analysis, on_log=log)
    character_analysis = stage_character_references(character_analysis, job_dir, on_log=log)

    prefetched_images: list[Path] = []
    cover: Path | None = None
    fallback_mode = _image_fallback_mode(job_dir)
    if fallback_mode:
        log("  加速模式：任务要求复用图片，跳过全部图片 API")
    elif _acceleration_remote_image_allowed():
        estimated_durations = [_estimated_speech_duration(segment.text) for segment in segments]
        plans = stage_pacing(segments, estimated_durations, on_log=log)
        _write_json(job_dir / "plans_prefetch.json", [_image_plan_to_dict(plan) for plan in plans])
        prefetch_dir = job_dir / "_prefetch_images"
        prefetched_images = stage_storyboard_and_image(
            plans,
            prefetch_dir,
            character_analysis,
            story_context,
            on_log=log,
        )
        cover = stage_cover(novel, segments, job_dir, metadata=metadata, on_log=log)
    else:
        log("  加速模式：检测到本地生图后端，仅完成文本 API 预处理")

    report = {
        "job_id": job_id,
        "completed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "segments": len(segments),
        "prefetched_images": len(prefetched_images),
        "cover": str(cover or ""),
        "tts_started": False,
        "ffmpeg_started": False,
    }
    _write_json(job_dir / ACCELERATION_PREFETCH_REPORT, report)
    write_status(
        job_dir,
        stage="preprocessed",
        progress=0.14,
        worker_pid=None,
        acceleration_preprocess=False,
        acceleration_preprocessed=True,
        error="",
    )
    log(
        f"== 加速预处理完成：洗稿/文本分析完成，预生成图片 {len(prefetched_images)} 张；"
        "已暂停等待 TTS =="
    )
    return report


def run_full(
    url_or_id: str,
    on_log: LogFn = _noop,
    on_prog: ProgFn = _noop,
    job_id: str | None = None,
    resume: bool = False,
) -> dict:
    job_id = job_id or new_job_id()
    job_dir = job_dir_for(job_id)
    if job_dir.exists() and not resume:
        raise FileExistsError(f"任务目录已存在: {job_dir}")
    job_dir.mkdir(parents=True, exist_ok=True)

    def log(message: str):
        safe_message = redact_secret_text(message)
        append_log(job_dir, safe_message)
        on_log(safe_message)

    def progress(stage: str, value: float, **extra):
        write_status(job_dir, job_id=job_id, stage=stage, progress=max(0.0, min(1.0, float(value))), **extra)
        on_prog(value)

    job_started = time.monotonic()
    timings: dict[str, float] = {}
    timings_lock = threading.Lock()

    def timed(label: str, fn, *args, **kwargs):
        started = time.monotonic()
        try:
            return fn(*args, **kwargs)
        finally:
            elapsed = time.monotonic() - started
            with timings_lock:
                timings[label] = timings.get(label, 0.0) + elapsed
            log(f"  timing {label}: {elapsed:.1f}s")

    def save_performance() -> dict:
        with timings_lock:
            ordered = dict(sorted(timings.items(), key=lambda item: item[1], reverse=True))
        payload = {
            "job_id": job_id,
            "wall_seconds": round(time.monotonic() - job_started, 3),
            "summed_stage_seconds": round(sum(ordered.values()), 3),
            "stages": {key: round(value, 3) for key, value in ordered.items()},
            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        _write_json(job_dir / "performance.json", payload)
        if ordered:
            summary = " | ".join(f"{name} {seconds:.1f}s" for name, seconds in list(ordered.items())[:6])
            log(f"  performance summary: {summary}")
        return payload

    url_or_id = _resolve_job_input_source(job_dir, url_or_id, on_log=log)
    log(f"== Job {job_id} ==")
    progress("starting", 0.0, input=url_or_id, site=config.scraper_site, worker_pid=os.getpid(), started_at=time.strftime("%Y-%m-%d %H:%M:%S"))
    try:
        imported_audio_mode = (job_dir / IMPORTED_AUDIO_MANIFEST).exists()
        novel = _read_saved_novel(job_dir / "novel.json") if resume else None
        segments = _read_saved_segments(job_dir / "segments.json") if resume else None
        if novel is not None and segments is not None:
            log(f"resume: reuse saved source and {len(segments)} cleaned segments")
            progress("tts", 0.15, title=novel.title)
        else:
            progress("scrape", 0.03)
            novel = timed(
                "1_scrape",
                stage_scrape,
                url_or_id,
                site=config.scraper_site,
                max_chars=0 if imported_audio_mode else config.scraper_max_chars,
                on_log=log,
            )
            _validate_job_novel_source(job_dir, novel)
            _write_novel(job_dir / "novel.json", novel)
            progress("clean", 0.10, title=novel.title)

            segments = timed(
                "2_clean",
                stage_clean,
                novel,
                on_log=log,
                job_dir=job_dir,
                preserve_source_text=imported_audio_mode,
            )
            _apply_series_title_to_novel(novel, job_dir, log)
            _write_json(job_dir / "segments.json", [{"i": s.index, "text": s.text} for s in segments])
        _validate_job_novel_source(job_dir, novel, segments)
        story_context = timed("3_story_context", stage_story_context, novel, segments, job_dir, on_log=log)
        metadata = timed("4_metadata", stage_metadata, novel, job_dir, story_context, segments=segments, on_log=log)
        if series_animation_enabled_for_job(job_dir):
            metadata = apply_series_presentation(metadata, job_dir, on_log=log)
            _write_json(job_dir / "metadata.json", metadata)
        character_analysis = timed("5_character_analysis", stage_character_analysis, novel, segments, job_dir, on_log=log)
        character_analysis = share_series_character_analysis(job_dir, character_analysis, on_log=log)
        character_analysis = timed("5b_character_references", stage_character_references, character_analysis, job_dir, on_log=log)
        progress("tts", 0.15, title=novel.title)
        start_acceleration_preprocess_next(
            source_job_id=job_id,
            on_log=log,
            require_active_stage=False,
        )

        # A user-selected fallback means this resumed job must spend effort on
        # missing TTS only; it must never start another image API request.
        tts_redo_reuse_images = (job_dir / TTS_REDO_REUSE_IMAGES_FILE).exists()
        image_fallback_mode = _image_fallback_mode(job_dir)
        even_image_count = len(_valid_scene_images(job_dir)) if image_fallback_mode else None
        # A resumed task already has durable audio and image artifacts.  Do not
        # launch a second speculative image pass: it can duplicate API calls
        # and obscures the useful guarantee that resume retries only missing or
        # failed images.
        overlap_images = (
            bool(config.get("pipeline_overlap_tts_images", True))
            and not image_fallback_mode
            and not resume
        )
        if resume and not image_fallback_mode:
            log("resume: keep completed artifacts; retry only incomplete or failed downstream steps")
        if overlap_images:
            log("  overlap enabled: prefetch storyboard/images while TTS is running")
            estimated_durs = [_estimated_speech_duration(s.text) for s in segments]
            prefetch_plans = timed("4_pacing_prefetch", stage_pacing, segments, estimated_durs, on_log=log)
            _write_json(job_dir / "plans_prefetch.json", [_image_plan_to_dict(p) for p in prefetch_plans])
            prefetch_dir = job_dir / "_prefetch_images"
            executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="pipeline-overlap")
            tts_future = executor.submit(
                timed,
                "5_tts",
                stage_tts,
                segments,
                job_dir,
                log,
                lambda p: progress("tts", 0.15 + p * 0.30, title=novel.title),
            )
            image_future = executor.submit(
                timed,
                "6_images_prefetch",
                stage_storyboard_and_image,
                prefetch_plans,
                prefetch_dir,
                character_analysis,
                story_context,
                log,
                lambda p: progress("images_prefetch", 0.18 + p * 0.28, title=novel.title),
            )
            try:
                audios, durs = tts_future.result()
                progress("pacing", 0.45, title=novel.title)
                plans = timed("4_pacing_final", stage_pacing, segments, durs, on_log=log)
                _write_json(job_dir / "plans.json", [_image_plan_to_dict(p) for p in plans])
                if _plan_signature(plans) == _plan_signature(prefetch_plans):
                    progress("images", 0.50, title=novel.title)
                    try:
                        prefetch_images = image_future.result()
                        images = _copy_prefetched_images(prefetch_images, prefetch_dir, job_dir)
                        log("  reuse prefetched storyboard/images")
                    except Exception as exc:
                        log(f"  WARN prefetched images failed: {exc}; regenerating images after TTS")
                        images = timed(
                            "6_images_final",
                            stage_storyboard_and_image,
                            plans,
                            job_dir,
                            character_analysis,
                            story_context,
                            on_log=log,
                            on_prog=lambda p: progress("images", 0.50 + p * 0.38, title=novel.title),
                        )
                else:
                    log("  prefetch plan differs from real TTS pacing; regenerating images with final pacing")
                    try:
                        image_future.result()
                    except Exception as exc:
                        log(f"  WARN discarded prefetch image task failed: {exc}")
                    images = timed(
                        "6_images_final",
                        stage_storyboard_and_image,
                        plans,
                        job_dir,
                        character_analysis,
                        story_context,
                        on_log=log,
                        on_prog=lambda p: progress("images", 0.50 + p * 0.38, title=novel.title),
                    )
            finally:
                executor.shutdown(wait=True, cancel_futures=True)
        else:
            audios, durs = timed(
                "5_tts",
                stage_tts,
                segments,
                job_dir,
                on_log=log,
                on_prog=lambda p: progress("tts", 0.15 + p * 0.30, title=novel.title),
            )
            progress("pacing", 0.45, title=novel.title)

            plans = timed(
                "4_pacing_final", stage_pacing, segments, durs, on_log=log,
                fixed_count_override=even_image_count,
            )
            _write_json(job_dir / "plans.json", [_image_plan_to_dict(p) for p in plans])
            progress("images", 0.50, title=novel.title)

            if image_fallback_mode:
                selection = set_image_fallback_selection(job_id, image_fallback_mode)
                images = [job_dir / str(path) for path in selection["paths"]]
                log(f"  reuse selected partial images ({image_fallback_mode}); skip image API")
                progress("images", 0.88, title=novel.title)
            else:
                images = timed(
                    "6_images_final",
                    stage_storyboard_and_image,
                    plans,
                    job_dir,
                    character_analysis,
                    story_context,
                    on_log=log,
                    on_prog=lambda p: progress("images", 0.50 + p * 0.38, title=novel.title),
                )
        _finalize_highlight_timeline(plans, segments, durs, job_dir, on_log=log)
        _write_json(job_dir / "plans.json", [_image_plan_to_dict(p) for p in plans])
        progress("cover", 0.89, title=novel.title)
        if tts_redo_reuse_images:
            saved_cover = job_dir / "cover" / "cover.jpg"
            cover = saved_cover if saved_cover.exists() else None
            log("  full TTS redo: reuse existing scene images and cover; skip all image API calls")
        else:
            cover = timed("7_cover", stage_cover, novel, segments, job_dir, metadata=metadata, on_log=log)
        progress("compose", 0.90, title=novel.title, cover=str(cover or ""))

        start_acceleration_preprocess_next(
            source_job_id=job_id,
            on_log=log,
            require_active_stage=False,
        )
        video = timed("8_compose", stage_compose, audios, durs, segments, plans, images, job_dir, on_log=log)
        exported_directory = export_completed_video(
            video,
            cover,
            novel.title,
            str(metadata.get("short_title", "") or ""),
            source_path=str(_read_json(job_dir / "status.json", {}).get("source_path") or _read_json(job_dir / "status.json", {}).get("input") or ""),
            on_log=log,
        )
        if tts_redo_reuse_images:
            (job_dir / TTS_REDO_REUSE_IMAGES_FILE).unlink(missing_ok=True)
        youtube_url = ""
        if bool(config.upload_enabled):
            progress("upload", 0.96, title=novel.title, short_title=metadata.get("short_title", ""), video=str(video), cover=str(cover or ""))
            youtube_url = timed("9_upload", stage_upload, video, novel.title, cover, job_dir, metadata=metadata, on_log=log)

        performance = save_performance()

        result = {
            "job_id": job_id,
            "job_dir": str(job_dir),
            "video": str(video),
            "exported_directory": str(exported_directory or ""),
            "cover": str(cover or ""),
            "title": novel.title,
            "short_title": metadata.get("short_title", ""),
            "title_candidates": metadata.get("titles", []),
            "synopsis_candidates": metadata.get("synopses", []),
            "generated_tag_line": metadata.get("generated_tag_line", ""),
            "youtube_url": youtube_url,
            "performance": performance,
        }
        _write_json(job_dir / "result.json", result)
        progress(
            "completed",
            1.0,
            title=novel.title,
            short_title=metadata.get("short_title", ""),
            video=str(video),
            cover=str(cover or ""),
            youtube_url=youtube_url,
            worker_pid=None,
            finished_at=time.strftime("%Y-%m-%d %H:%M:%S"),
        )
        start_next_queued_job(exclude_job_id=job_id, on_log=log)
        return result
    except ImageGenerationSkipped as exc:
        safe_exc = redact_secret_text(exc)
        log(f"任务跳过: {safe_exc}")
        progress("failed", load_status(job_id).get("progress", 0), error=safe_exc, worker_pid=None, finished_at=time.strftime("%Y-%m-%d %H:%M:%S"))
        start_next_queued_job(exclude_job_id=job_id, on_log=log)
        return {"job_id": job_id, "skipped": True, "error": safe_exc}
    except Exception as exc:
        safe_exc = redact_secret_text(exc)
        log(f"ERROR 失败: {safe_exc}\n{redact_secret_text(traceback.format_exc())}")
        progress("failed", load_status(job_id).get("progress", 0), error=safe_exc, worker_pid=None, finished_at=time.strftime("%Y-%m-%d %H:%M:%S"))
        raise


def _prompt_for_plan(
    llm: LLMBackend,
    plan: ImagePlan,
    story_context: dict,
    on_log: LogFn,
    index: int,
) -> tuple[str, dict]:
    if bool(config.get("storyboard_highlight_enabled", True)):
        max_segments = int(config.get("storyboard_highlight_max_segments", 3) or 3)
        highlight = fallback_highlight(plan.segments)
        if _can_call_text_llm():
            request = highlight_request(plan.segments, story_context, max_segments)
            try:
                with external_api_slot(action=f"highlight storyboard {index}"):
                    raw = llm.complete(HIGHLIGHT_SYSTEM_PROMPT, request, max_tokens=900, temperature=0.15)
                highlight = normalize_highlight(parse_json_object(raw), plan.segments, max_segments=max_segments)
            except Exception as exc:
                on_log(f"  WARN highlight storyboard {index} failed: {exc}; using grounded local fallback")
        selected_text = str(highlight.get("highlight_text") or plan.text)
        generated = str(highlight.get("image_prompt_en") or "").strip()
        if generated:
            excluded = ", ".join(str(x) for x in (highlight.get("excluded_people") or []) if str(x).strip())
            exclusion = f" Do not show these people: {excluded}." if excluded else ""
            grounding = (
                "Depict only this selected narration event; do not import other events or people from the broader story. "
                f"Selected source moment: {selected_text[:700]}."
                + exclusion
            )
            prompt = _with_image_prefix(f"{generated}. {grounding} {config.llm_image_style_suffix}")
            return _policy_safe_image_prompt(prompt, fallback_text=selected_text), highlight
        return _fallback_storyboard_prompt(selected_text), highlight

    prefix = str(config.get("llm_image_prompt_prefix", "") or "").strip()
    context = {
        "text": plan.text,
        "excerpt": re.sub(r"\s+", " ", str(plan.text or "")).strip()[:500],
        "index": index + 1,
        "prefix": prefix,
        "style": str(config.llm_image_style_suffix or "").strip(),
        "theme_context": "",
        "character_context": "",
    }
    request = _format_template(
        str(config.get("llm_storyboard_user_template", "") or ""),
        context,
        fallback=plan.text,
    )
    if _can_call_text_llm():
        try:
            with external_api_slot(action=f"storyboard {index}"):
                prompt = _policy_safe_image_prompt(_with_image_prefix(llm.storyboard(request)), fallback_text=plan.text)
                return prompt, fallback_highlight(plan.segments)
        except Exception as exc:
            on_log(f"  WARN storyboard {index} LLM failed: {exc}; using safe template fallback")
    return _fallback_storyboard_prompt(plan.text), fallback_highlight(plan.segments)


def _with_image_prefix(prompt: str) -> str:
    prefix = re.sub(r"\s+", " ", str(config.get("llm_image_prompt_prefix", "") or "").strip())
    value = re.sub(r"\s+", " ", str(prompt or "").strip())
    if prefix and value and not value.lower().startswith(prefix[:80].lower()):
        return f"{prefix} {value}"
    return value or prefix


def _fallback_storyboard_prompt(text: str) -> str:
    excerpt = re.sub(r"\s+", " ", str(text or "")).strip()
    excerpt = re.sub(r"[\"'`<>#]", " ", excerpt)[:120]
    prefix = str(config.get("llm_image_prompt_prefix", "") or "").strip()
    prompt = (
        f"{prefix} "
        "Depict mainland Chinese late Han dynasty people and places only: Chinese facial features, Han robes and armor, "
        "Chinese palace, village, battlefield, banners, and weapons; never Japanese people, kimono, samurai, shrine, or Japan setting. "
        f"{config.llm_image_style_suffix}. "
        # Keep the concrete narration at the end: the safe prompt length cap
        # intentionally preserves the tail as well as the global style lock.
        f"Scene-specific Three Kingdoms event to depict: {excerpt}."
    )
    return _policy_safe_image_prompt(prompt, fallback_text="")


def _plan_signature(plans: list[ImagePlan]) -> list[list[int]]:
    return [[int(seg.index) for seg in plan.segments] for plan in plans]


def _copy_prefetched_images(paths: list[Path], prefetch_dir: Path, job_dir: Path) -> list[Path]:
    img_dir = job_dir / "images"
    img_dir.mkdir(parents=True, exist_ok=True)
    final_paths: list[Path] = []
    for i, src in enumerate(paths):
        suffix = src.suffix if src.suffix else ".png"
        dst = img_dir / f"img_{i:05d}{suffix}"
        if src.resolve() != dst.resolve():
            shutil.copy2(src, dst)
        src_cache = _image_cache_metadata_path(src)
        dst_cache = _image_cache_metadata_path(dst)
        if src_cache.exists() and src.resolve() != dst.resolve():
            shutil.copy2(src_cache, dst_cache)
        final_paths.append(dst)
    prompts = prefetch_dir / "prompts.json"
    if prompts.exists():
        shutil.copy2(prompts, job_dir / "prompts.json")
    try:
        shutil.rmtree(prefetch_dir)
    except OSError:
        pass
    return final_paths


def _finalize_highlight_timeline(
    plans: list[ImagePlan],
    segments: list[Segment],
    durations: list[float],
    job_dir: Path,
    on_log: LogFn = _noop,
) -> None:
    """Center unchanged image count around selected narration highlights."""
    if not plans or not bool(config.get("storyboard_highlight_enabled", True)):
        return
    rows = _read_json(job_dir / "prompts.json", [])
    by_image = {
        int(row.get("index")): row
        for row in rows
        if isinstance(row, dict) and str(row.get("index", "")).lstrip("-").isdigit()
    } if isinstance(rows, list) else {}
    timing: dict[int, tuple[float, float]] = {}
    cursor = 0.0
    for seg, duration in zip(segments, durations):
        end = cursor + max(0.05, float(duration))
        timing[int(seg.index)] = (cursor, end)
        cursor = end
    total = cursor
    if total <= 0.05:
        return

    anchors: list[float] = []
    for i, plan in enumerate(plans):
        row = by_image.get(i, {})
        indexes = []
        for raw in row.get("highlight_segment_indexes") or []:
            try:
                idx = int(raw)
            except Exception:
                continue
            if idx in timing:
                indexes.append(idx)
        if indexes:
            plan.highlight_segment_indexes = indexes
            plan.highlight_text = str(row.get("highlight_text") or "")
            plan.highlight_people = [str(x) for x in (row.get("highlight_people") or [])]
            plan.highlight_location = str(row.get("highlight_location") or "")
            plan.highlight_action = str(row.get("highlight_action") or "")
            anchors.append((timing[indexes[0]][0] + timing[indexes[-1]][1]) / 2.0)
        else:
            plan_indexes = [int(s.index) for s in plan.segments if int(s.index) in timing]
            if plan_indexes:
                anchors.append((timing[plan_indexes[0]][0] + timing[plan_indexes[-1]][1]) / 2.0)
            else:
                anchors.append(total * (i + 0.5) / len(plans))

    if not bool(config.get("storyboard_highlight_align_timeline", True)) or len(plans) == 1:
        return
    boundaries = [0.0]
    for left, right in zip(anchors, anchors[1:]):
        boundaries.append(max(boundaries[-1] + 0.05, min(total, (left + right) / 2.0)))
    boundaries.append(total)
    if len(boundaries) != len(plans) + 1 or boundaries[-2] >= total:
        on_log("  WARN highlight timeline alignment skipped: invalid highlight ordering")
        return
    for i, plan in enumerate(plans):
        plan.duration = max(0.05, boundaries[i + 1] - boundaries[i])
    # Correct tiny floating-point drift without changing image count.
    plans[-1].duration += total - sum(p.duration for p in plans)
    on_log(f"  highlight timeline aligned: {len(plans)} images unchanged, total={total:.1f}s")


def _estimated_speech_duration(text: str) -> float:
    compact = "".join(text.split())
    return max(1.2, min(45.0, len(compact) / 4.2))


def _write_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _write_novel(path: Path, novel: Novel):
    _write_json(
        path,
        {
            "site": novel.site,
            "id": novel.novel_id,
            "title": novel.title,
            "author": novel.author,
            "description": novel.description,
            "chapters": [{"index": c.index, "title": c.title, "text": c.text} for c in novel.chapters],
        },
    )


def _read_saved_novel(path: Path) -> Novel | None:
    """Read a completed scrape artifact for a genuine resume without re-scraping."""
    payload = _read_json(path, {})
    if not isinstance(payload, dict):
        return None
    chapters = payload.get("chapters")
    if not isinstance(chapters, list):
        return None
    restored = []
    for index, chapter in enumerate(chapters, start=1):
        if not isinstance(chapter, dict):
            return None
        text = str(chapter.get("text") or "").strip()
        if not text:
            return None
        restored.append(NovelChapter(
            index=int(chapter.get("index") or index),
            title=str(chapter.get("title") or ""),
            text=text,
        ))
    if not restored:
        return None
    return Novel(
        site=str(payload.get("site") or "text"),
        novel_id=str(payload.get("id") or ""),
        title=str(payload.get("title") or "untitled"),
        author=str(payload.get("author") or ""),
        description=str(payload.get("description") or ""),
        chapters=restored,
    )


def _read_saved_segments(path: Path) -> list[Segment] | None:
    """Read saved clean/split output, returning None when it is incomplete."""
    rows = _read_json(path, [])
    if not isinstance(rows, list) or not rows:
        return None
    segments = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            return None
        text = str(row.get("text") or "").strip()
        if not text:
            return None
        segments.append(Segment(index=int(row.get("i", row.get("index", index)) or index), text=text))
    return segments


def _image_plan_to_dict(plan: ImagePlan) -> dict:
    return {
        "image_index": plan.image_index,
        "duration": plan.duration,
        "solution": "视频生图解决方案1" if bool(config.get("storyboard_highlight_enabled", True)) else "legacy",
        "highlight_segment_indexes": list(plan.highlight_segment_indexes),
        "highlight_text": plan.highlight_text,
        "highlight_people": list(plan.highlight_people),
        "highlight_location": plan.highlight_location,
        "highlight_action": plan.highlight_action,
        "segments": [{"index": s.index, "text": s.text} for s in plan.segments],
    }
