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
import subprocess
import sys
import time
import traceback
import threading
import unicodedata
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, as_completed, wait
from dataclasses import asdict
from pathlib import Path
from typing import Callable, Iterable

from PIL import Image, ImageDraw, ImageFont, ImageOps

from app.config import API_KEY_FIELDS, JOBS_DIR, ROOT, api_key_env_name, config
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
        "worker_starting", "starting", "scrape", "clean", "tts",
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
    "queued": "等待启动",
    "starting": "启动中",
    "scrape": "1/6 读取文本",
    "clean": "2/6 整理/洗稿切片",
    "tts": "3/6 配音",
    "images_prefetch": "4/6 预生成图片",
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
    last_error: Exception | None = None
    for _ in range(8):
        try:
            shutil.rmtree(path)
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


def start_worker(input_text: str, job_id: str | None = None, resume: bool = False, compose_only: bool = False) -> tuple[str, int]:
    """Start a detached worker process and return (job_id, pid)."""
    job_id = job_id or new_job_id("pipeline")
    job_dir = job_dir_for(job_id)
    if job_dir.exists() and not resume:
        raise FileExistsError(f"job directory already exists: {job_dir}")

    max_jobs = max(1, int(config.get("max_concurrent_jobs", 2)))
    running = count_running_workers()
    if running >= max_jobs:
        raise RuntimeError(
            f"已达到最大并行任务数限制 ({running}/{max_jobs})，请等待当前任务完成后再启动新任务。"
            "可在配置中调整 max_concurrent_jobs 提高上限。"
        )
    job_dir.mkdir(parents=True, exist_ok=True)

    # Freeze all settings for this worker.  A later GUI profile switch must not
    # change an already launched (or auto-started) job's AI prompts or routes.
    settings_snapshot = job_dir / "settings_snapshot.json"
    snapshot_settings = config.as_dict()
    for key in API_KEY_FIELDS:
        snapshot_settings.pop(key, None)
    settings_snapshot.write_text(
        json.dumps(snapshot_settings, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

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
    write_status(job_dir, job_id=job_id, input=input_text, stage="queued", progress=0.0, worker_pid=None)
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


def start_next_queued_job(*, exclude_job_id: str | None = None, on_log: LogFn = _noop) -> tuple[str, int] | None:
    """Start the next waiting job when worker capacity is available."""
    max_jobs = max(1, int(config.get("max_concurrent_jobs", 2)))
    if count_running_workers() >= max_jobs:
        return None
    for row in list_jobs(limit=500):
        job_id = str(row.get("job_id") or "")
        if not job_id or job_id == exclude_job_id:
            continue
        st = load_status(job_id)
        if st.get("worker_alive") or st.get("stage") not in {"queued", "stopped"}:
            continue
        input_text = str(st.get("input") or "")
        if not input_text:
            continue
        try:
            started = start_worker(input_text, job_id=job_id, resume=True)
            on_log(f"  auto-started waiting job {job_id} pid={started[1]}")
            return started
        except RuntimeError:
            return None
        except Exception as exc:
            append_log(job_dir_for(job_id), f"auto-start failed: {exc}")
            on_log(f"  WARN auto-start waiting job {job_id} failed: {exc}")
            return None
    return None


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
    if _looks_like_text_source(url_or_id, site):
        novel = _novel_from_text_source(url_or_id, max_chars=max_chars)
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
        if (
            stripped
            and _CHAPTER_HEADING_RE.match(stripped)
            and not bool(config.get("narrate_chapter_headings", False))
        ):
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


def _split_rewrite_paragraphs(text: str) -> list[str]:
    normalized = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    raw = re.split(r"\n\s*\n", normalized)
    paragraphs = []
    for para in raw:
        para = re.sub(r"[ \t]+\n", "\n", para).strip()
        if para:
            paragraphs.append(para)
    return paragraphs


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
            "model": config.get("tts_model", "tts-1"),
            "emotion": config.get("tts_emotion", ""),
            "base_url": config.tts_base_url,
            "extra": config.tts_extra or {},
            "api_key_hash": _text_hash(config.tts_api_key or "")[:16],
        }
    )


TTS_PRONUNCIATION_DICTIONARY = "tts_pronunciation_dictionary.txt"
IMPORTED_AUDIO_MANIFEST = "imported_audio.json"
IMPORTED_AUDIO_FILENAME = "imported_narration.mp3"


def parse_pronunciation_dictionary(text: str) -> list[tuple[str, str]]:
    """Parse `written=reading` lines and return longest terms first."""
    readings: dict[str, str] = {}
    first_seen: dict[str, int] = {}
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
        previous = readings.get(written)
        if previous is not None and previous != reading:
            raise ValueError(
                f"读音词典中的“{written}”有两个不同读音：{previous} / {reading}；"
                "请只保留本任务需要的一个读音"
            )
        if written not in first_seen:
            first_seen[written] = len(first_seen)
        readings[written] = reading
    if not readings:
        raise ValueError("读音词典中没有可用条目；格式应为：汉字词语=假名读音")
    return sorted(readings.items(), key=lambda item: (-len(item[0]), first_seen[item[0]]))


def inspect_pronunciation_dictionary(source_path: str | Path) -> dict:
    path = Path(source_path).expanduser()
    if not path.exists() or not path.is_file():
        raise FileNotFoundError(f"找不到读音词典：{path}")
    source = path.read_text(encoding="utf-8-sig")
    entries = parse_pronunciation_dictionary(source)
    normalized = source.lstrip("\ufeff")
    return {
        "path": str(path),
        "entries": len(entries),
        "text": source,
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


def attach_pronunciation_dictionary(job_id: str, source_path: str | Path) -> dict:
    """Copy a validated pronunciation dictionary into a job and invalidate its TTS."""
    if is_worker_running(job_id):
        raise RuntimeError(f"{job_id} 正在运行。请先停止任务，再附加读音词典。")
    info = inspect_pronunciation_dictionary(source_path)
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
        video="",
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
    for video_path in {job_dir / "final.mp4", video_output_path(job_dir)}:
        video_path.unlink(missing_ok=True)
        video_path.with_name(video_path.stem + ".tmp" + video_path.suffix).unlink(missing_ok=True)


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
        write_status(job_dir, stage="queued", progress=0.15, worker_pid=None, error="", video="", youtube_url="")
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
        write_status(job_dir, stage="queued", progress=0.15, worker_pid=None, error="", video="", youtube_url="")
        append_log(job_dir, f"TTS unfinished/bad segments reset {changed}; restart selected job to retry real audio")
    return {"job_id": job_id, "changed": len(changed), "segments": changed}


def _stage_tts_manifest_impl(
    segments: list[Segment],
    job_dir: Path,
    on_log: LogFn,
    on_prog: ProgFn,
) -> tuple[list[Path], list[float]]:
    dictionary_path = job_dir / TTS_PRONUNCIATION_DICTIONARY
    dictionary_entries: list[tuple[str, str]] = []
    dictionary_hash = ""
    narration_texts = [seg.text for seg in segments]
    replacement_counts = [0 for _seg in segments]
    if dictionary_path.exists() and config.tts_provider == "edge":
        dictionary_source = dictionary_path.read_text(encoding="utf-8-sig")
        dictionary_entries = parse_pronunciation_dictionary(dictionary_source)
        dictionary_hash = _text_hash(dictionary_source.lstrip("\ufeff"))
        applied = [_apply_pronunciation_dictionary(seg.text, dictionary_entries) for seg in segments]
        narration_texts = [item[0] for item in applied]
        replacement_counts = [item[1] for item in applied]

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
    elif dictionary_path.exists() and config.tts_provider != "edge":
        on_log("  pronunciation dictionary is attached but only applies when TTS provider=edge")

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
) -> list[ImagePlan]:
    on_log(f"[4/6] 图音配比: mode={config.pacing_mode}")
    plans = plan_images(
        segments,
        durations,
        mode=config.pacing_mode,
        seconds_per_image=config.pacing_seconds_per_image,
        sentences_per_image=config.pacing_sentences_per_image,
        fixed_count=config.pacing_fixed_count,
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

    fallback = normalize_story_context({
        "story_summary": str(novel.description or "").strip()[:600],
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
        return {
            "provider": provider,
            "base_url": base_url,
            "api_key": api_key,
            "model": model,
            "workflow_path": workflow_for(provider, str(config.image_workflow or "")),
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
        return {
            "provider": provider,
            "base_url": base_url,
            "api_key": api_key,
            "model": model,
            "workflow_path": workflow_for(provider, str(config.image_workflow or "")),
        }

    if role == "character_reference":
        provider = str(config.get("character_reference_provider", "same_as_image") or "").strip()
        if provider in {"", "same_as_image", "同配图", "同图片"}:
            return _image_route_settings("scene")
        provider, base_url, api_key, model = _apply_unified_image_api(
            provider,
            str(config.get("character_reference_base_url", "") or config.image_base_url or ""),
            str(config.get("character_reference_api_key", "") or config.image_api_key or ""),
            str(config.get("character_reference_model", "") or config.image_model or ""),
        )
        return {
            "provider": provider,
            "base_url": base_url,
            "api_key": api_key,
            "model": model,
            "workflow_path": workflow_for(provider, str(config.get("character_reference_workflow", "") or config.image_workflow or "")),
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
        return {
            "provider": provider,
            "base_url": base_url,
            "api_key": api_key,
            "model": model,
            "workflow_path": workflow_for(provider, str(config.get("scene_reference_workflow", "") or config.image_workflow or "")),
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
    char_dir = job_dir / "characters"
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

    _write_json(job_dir / "character_reference_manifest.json", manifest)
    _write_json(job_dir / "character_profiles.json", character_analysis)
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
            "fully clothed characters, clean composition, soft lighting, readable faces, only the requested title text, "
            "no extra captions, logos, UI, or watermarks"
        )
    else:
        guard = (
            "Family-friendly cinematic anime illustration, adult-looking characters, symbolic emotional drama, "
            "fully clothed characters, clean composition, soft lighting, readable faces, text-free artwork"
        )
    # Camera language from a story prompt often asks for dramatic close-ups.
    # Put this constraint at the front so it remains effective even if a long
    # prompt later has to be shortened for an image provider.
    framing_guard = (
        "Medium-wide 16:9 composition; every visible person has their complete head, face, forehead, and chin "
        "inside the frame, with generous headroom and safe margins on every edge; no cropped head, face, or body "
        "at the image boundary"
    )
    if not re.search(r"(?i)complete\s+(?:heads?|head)|generous\s+headroom|no\s+cropped\s+(?:heads?|faces?)", value):
        value = f"{framing_guard}. {value}"
    if "family-friendly" not in value.lower():
        value = f"{value}. {guard}"
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
            "schema": 1,
            "provider": str(route.get("provider") or "").strip().lower(),
            "base_url": str(route.get("base_url") or "").strip().rstrip("/"),
            "model": str(route.get("model") or "").strip(),
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
            negative_prompt=str(config.llm_negative_prompt or ""),
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
            for attempt in range(1, 3):
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
                            config.llm_negative_prompt,
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
                                    config.llm_negative_prompt,
                                    reference_paths,
                                    out,
                                    width=config.image_width,
                                    height=config.image_height,
                                )
                            else:
                                make_image_backend().generate(
                                    attempt_prompt,
                                    config.llm_negative_prompt,
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
                    on_log(f"  WARN 图{i} 生成失败 {attempt}/2: {exc}")
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
                raise RuntimeError(f"图{i} 生成失败，已停止合成，重试时会重新请求 API：{error_text}")
        else:
            on_log(f"  image {i + 1}/{len(plans)} reuse cached file: {out}")
        return i, out, prompt

    completed = 0
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="image") as pool:
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

    final_paths = [p for p in paths if p is not None]
    if len(final_paths) != len(plans):
        raise RuntimeError("Image stage did not produce all images")
    on_log(f"  OK {len(final_paths)} 张图")
    return final_paths


def _metadata_intro(novel: Novel, limit: int = 700) -> str:
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


def stage_metadata(novel: Novel, job_dir: Path, on_log: LogFn = _noop) -> dict:
    intro = _metadata_intro(novel)
    clean_title = _clean_display_title(novel.title)
    min_chars = max(6, int(config.get("short_title_min_chars", 20) or 20))
    max_chars = max(min_chars, int(config.get("short_title_max_chars", 30) or 30))
    short_title = ""
    if bool(config.get("short_title_enabled", True)):
        if _can_call_text_llm():
            try:
                route = _llm_route_settings()
                llm = LLMBackend(
                    provider=route["provider"],
                    base_url=route["base_url"],
                    api_key=route["api_key"],
                    model=route["model"],
                    system_prompt=str(config.get("short_title_prompt", "") or ""),
                    style_suffix="",
                )
                request = f"书名：{clean_title}\n简介：{intro[:900]}\n只输出短说明标题。"
                with external_api_slot(action="short title"):
                    short_title = _clean_short_title(llm.storyboard(request), min_chars, max_chars)
            except Exception as exc:
                on_log(f"  WARN short title LLM failed: {exc}; use intro fallback")
        if not short_title:
            short_title = _fallback_short_title(novel, intro, min_chars, max_chars)
    metadata = {
        "title": novel.title,
        "clean_title": clean_title,
        "short_title": short_title,
        "intro": intro,
        "author": novel.author,
        "tags": str(config.youtube_tags or ""),
    }
    _write_json(job_dir / "metadata.json", metadata)
    if short_title:
        on_log(f"  short_title: {short_title}")
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


def _build_cover_prompt(novel: Novel, segments: list[Segment], on_log: LogFn = _noop) -> str:
    title = _clean_display_title(novel.title)
    excerpt = _cover_excerpt(novel, segments)
    style = str(config.llm_image_style_suffix or "").strip()
    custom = str(config.get("cover_custom_prompt", "") or "").strip()
    template = str(config.get("cover_prompt_template", "") or "")
    context = {"title": title, "excerpt": excerpt, "style": style, "custom": custom}
    fallback = _format_template(
        template,
        context,
        fallback=(
            f'Create a cinematic YouTube cover illustration for a novel video. '
            f'Render the exact title text "{title}" as the main large readable cover title inside the image. '
            f'Story excerpt: {excerpt}. {custom} {style}'
        ),
    )

    if _can_call_text_llm():
        try:
            route = _llm_route_settings()
            llm = LLMBackend(
                provider=route["provider"],
                base_url=route["base_url"],
                api_key=route["api_key"],
                model=route["model"],
                system_prompt=str(config.get("cover_ai_analysis_prompt", "") or ""),
                style_suffix="",
            )
            request = (
                f"{fallback}\n\n"
                "Return one prompt only. The prompt must explicitly instruct the image model to render the "
                f'exact title text "{title}" inside the cover image as polished large readable typography. Do not request '
                "any post-production/editor overlay."
            )
            with external_api_slot(action="cover prompt"):
                prompt = llm.storyboard(request).strip()
            if title not in prompt:
                prompt = f'Title context: "{title}". {prompt}'
            return _policy_safe_image_prompt(
                f'{prompt}. Render the exact title text "{title}" as the main readable cover title in the image. '
                f'Do not add other text, logos, captions, UI, or watermarks.',
                fallback_text=excerpt,
                allow_title_text=True,
            )
        except Exception as exc:
            on_log(f"  WARN cover prompt LLM failed: {exc}; use template prompt")
    if title not in fallback:
        fallback = f'Render the exact title text "{title}" as the main readable cover title. {fallback}'
    return _policy_safe_image_prompt(fallback, fallback_text=excerpt, allow_title_text=True)


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


def _finalize_cover_image(raw_path: Path, out_path: Path, width: int, height: int) -> Path:
    img = Image.open(raw_path).convert("RGB")
    img = ImageOps.fit(img, (max(320, int(width)), max(180, int(height))), method=Image.LANCZOS, centering=(0.5, 0.5))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path, "JPEG", quality=93, optimize=True)
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
) -> Path | None:
    if not bool(config.get("cover_enabled", True)):
        return None

    title = _clean_display_title(novel.title)
    cover_dir = job_dir / "cover"
    cover_dir.mkdir(parents=True, exist_ok=True)
    raw_path = cover_dir / "cover_raw.png"
    out_path = cover_dir / "cover.jpg"
    manifest_path = cover_dir / "cover_manifest.json"
    excerpt = _cover_excerpt(novel, segments)
    cover_backend = _cover_backend_settings()
    provider = cover_backend["provider"]
    expected_hash = _stable_hash(
        {
            "schema": "cover_v2_prompt_generated_title",
            "title": title,
            "excerpt_hash": _text_hash(excerpt),
            "provider": provider,
            "base_url": cover_backend["base_url"],
            "model": cover_backend["model"],
            "style": config.llm_image_style_suffix,
            "prompt_template": config.get("cover_prompt_template", ""),
            "custom_prompt": config.get("cover_custom_prompt", ""),
            "analysis_prompt": config.get("cover_ai_analysis_prompt", ""),
            "width": int(config.get("cover_width", 1280) or 1280),
            "height": int(config.get("cover_height", 720) or 720),
            "title_mode": "prompt_generated",
            "api_key_hash": _text_hash(cover_backend["api_key"])[:16],
        }
    )
    manifest = _read_json(manifest_path, {})
    if (
        isinstance(manifest, dict)
        and manifest.get("input_hash") == expected_hash
        and out_path.exists()
        and out_path.stat().st_size > 1000
    ):
        on_log(f"  reuse cover: {out_path}")
        return out_path

    on_log(f"[cover] Generate cover from title/text: {title}")
    prompt = _build_cover_prompt(novel, segments, on_log=on_log)
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
    )
    raw_tmp = raw_path.with_suffix(".tmp.png")
    raw_tmp.unlink(missing_ok=True)
    try:
        if provider == "placeholder":
            _create_cover_placeholder(prompt, title, raw_tmp, width, height)
        else:
            with external_api_slot(action="cover image"):
                backend.generate(prompt, _cover_negative_prompt(), raw_tmp, width=width, height=height)
        raw_tmp.replace(raw_path)
    except Exception as exc:
        raw_tmp.unlink(missing_ok=True)
        on_log(f"  WARN cover image generation failed: {exc}; use generated video image fallback")
        if not _create_cover_from_video_image(job_dir, raw_path, width, height):
            on_log("  WARN no generated image available for cover fallback; use placeholder")
            _create_cover_placeholder(prompt, title, raw_path, width, height)

    _finalize_cover_image(raw_path, out_path, width, height)
    _write_json(
        manifest_path,
        {
            "schema_version": 1,
            "status": "ready",
            "input_hash": expected_hash,
            "title": title,
            "provider": provider,
            "prompt": prompt,
            "raw_path": str(raw_path),
            "path": str(out_path),
            "file": _file_signature(out_path),
            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        },
    )
    on_log(f"  OK cover: {out_path}")
    return out_path


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
            "composer_revision": 3,
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
                "safe_framing": bool(config.get("video_safe_framing", True)),
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
            safe_framing=bool(config.get("video_safe_framing", True)),
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


def stage_upload(video: Path, title: str, cover: Path | None, job_dir: Path, metadata: dict | None = None, on_log: LogFn = _noop) -> str:
    if not bool(config.upload_enabled):
        return ""
    if str(config.youtube_upload_method or "browser").lower() != "browser":
        on_log("  WARN only browser upload is wired in this build")
        return ""
    metadata = metadata or {}
    clean_title = str(metadata.get("clean_title") or _clean_display_title(title))
    context = {
        "title": title,
        "clean_title": clean_title,
        "short_title": metadata.get("short_title", ""),
        "intro": metadata.get("intro", ""),
        "author": metadata.get("author", ""),
        "tags": str(config.youtube_tags or metadata.get("tags", "")),
        "job_id": job_dir.name,
    }
    profiles = _selected_upload_profiles()
    if not profiles:
        return ""
    multi_upload = len(profiles) > 1
    on_log(f"[7/7] YouTube browser upload: {len(profiles)} profile(s)")
    lock_path = _acquire_upload_lock(job_dir, on_log)
    results: list[dict] = []
    try:
        _ensure_upload_dependencies(on_log)
        from app.upload import upload_to_youtube

        for index, profile in enumerate(profiles, start=1):
            title_template = str(profile.get("title_template") or config.youtube_title_template or "{clean_title}")
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

            upload_title = _limit_upload_title(
                _format_template(title_template, context, fallback=clean_title),
                int(config.get("youtube_title_max_chars", 100) or 100),
            ) or clean_title
            upload_description = _format_template(description_template, context, fallback="")
            profile_name = str(profile.get("name") or f"上传方案{index}").strip() or f"上传方案{index}"
            on_log(f"  [{index}/{len(profiles)}] profile={profile_name} chrome_profile={chrome_profile} flow={'精简' if flow == 'simple' else '完整'}")

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
                force_profile_launch=multi_upload,
                auto_restart=bool(config.browser_auto_restart),
                stall_timeout_min=int(config.browser_stall_timeout_min or 10),
                op_speed=str(config.browser_op_speed or "normal"),
                on_log=on_log,
                on_progress=_profile_progress,
            )
            if not video_id:
                raise RuntimeError(f"YouTube upload failed: {profile_name}")
            url = f"https://www.youtube.com/watch?v={video_id}"
            results.append(
                {
                    "profile": profile_name,
                    "chrome_profile": chrome_profile,
                    "video_id": video_id,
                    "url": url,
                    "title": upload_title,
                    "description": upload_description,
                    "visibility": visibility,
                    "flow": flow,
                }
            )
            _write_json(job_dir / "upload_result.json", {"uploads": results, **results[0]})
            on_log(f"  OK uploaded [{profile_name}]: {url}")
    finally:
        _release_upload_lock(lock_path, job_dir.name)
    if not results:
        raise RuntimeError("YouTube upload failed")
    _write_json(job_dir / "upload_result.json", {"uploads": results, **results[0]})
    if len(results) > 1:
        on_log(f"  OK uploaded {len(results)} copies")
    return str(results[0].get("url") or "")


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


def _selected_upload_profiles() -> list[dict]:
    profiles = [_normalize_upload_profile(profile, idx) for idx, profile in enumerate(_upload_profiles())]
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
    images = [job_dir / "images" / f"img_{i:05d}.png" for i in range(len(plans))]
    if len(plans) != len(images) or any(not path.exists() for path in images):
        raise RuntimeError("合成所需图片不完整；不会自动出图，请先检查图片缓存。")
    durations = _read_json(job_dir / "durations.json", [])
    if not isinstance(durations, list) or len(durations) != len(segments):
        raise RuntimeError("缺少或无效 durations.json，无法仅重试合成。")
    imported = _job_imported_audio(job_dir)
    audios = [imported[0]] if imported else [job_dir / "audio_full.mp3"]
    if any(not path.exists() for path in audios):
        raise RuntimeError("缺少合成所需音频，无法仅重试合成。")
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
        safe_framing=bool(config.get("video_safe_framing", True)),
    )
    on_log(f"  OK 视频完成: {out}")
    return out


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

    log(f"== Job {job_id} ==")
    progress("starting", 0.0, input=url_or_id, site=config.scraper_site, worker_pid=os.getpid(), started_at=time.strftime("%Y-%m-%d %H:%M:%S"))
    try:
        imported_audio_mode = (job_dir / IMPORTED_AUDIO_MANIFEST).exists()
        progress("scrape", 0.03)
        novel = timed(
            "1_scrape",
            stage_scrape,
            url_or_id,
            site=config.scraper_site,
            max_chars=0 if imported_audio_mode else config.scraper_max_chars,
            on_log=log,
        )
        _write_novel(job_dir / "novel.json", novel)
        metadata = timed("2_metadata", stage_metadata, novel, job_dir, on_log=log)
        progress("clean", 0.10, title=novel.title, short_title=metadata.get("short_title", ""))

        segments = timed(
            "3_clean",
            stage_clean,
            novel,
            on_log=log,
            job_dir=job_dir,
            preserve_source_text=imported_audio_mode,
        )
        _write_json(job_dir / "segments.json", [{"i": s.index, "text": s.text} for s in segments])
        story_context = timed("4_story_context", stage_story_context, novel, segments, job_dir, on_log=log)
        character_analysis = timed("4_character_analysis", stage_character_analysis, novel, segments, job_dir, on_log=log)
        character_analysis = timed("4b_character_references", stage_character_references, character_analysis, job_dir, on_log=log)
        progress("tts", 0.15, title=novel.title)

        overlap_images = bool(config.get("pipeline_overlap_tts_images", True))
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

            plans = timed("4_pacing_final", stage_pacing, segments, durs, on_log=log)
            _write_json(job_dir / "plans.json", [_image_plan_to_dict(p) for p in plans])
            progress("images", 0.50, title=novel.title)

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
        cover = timed("7_cover", stage_cover, novel, segments, job_dir, on_log=log)
        progress("compose", 0.90, title=novel.title, cover=str(cover or ""))

        video = timed("8_compose", stage_compose, audios, durs, segments, plans, images, job_dir, on_log=log)
        youtube_url = ""
        if bool(config.upload_enabled):
            progress("upload", 0.96, title=novel.title, short_title=metadata.get("short_title", ""), video=str(video), cover=str(cover or ""))
            youtube_url = timed("9_upload", stage_upload, video, novel.title, cover, job_dir, metadata=metadata, on_log=log)

        performance = save_performance()

        result = {
            "job_id": job_id,
            "job_dir": str(job_dir),
            "video": str(video),
            "cover": str(cover or ""),
            "title": novel.title,
            "short_title": metadata.get("short_title", ""),
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
        f"Story mood reference: {excerpt}. {config.llm_image_style_suffix}"
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
