"""Dependency detection and repair helpers for distributed Windows/macOS PCs.

This module intentionally uses only the Python standard library plus
app.config, so app.gui can run it before importing httpx/Pillow/edge_tts.
"""
from __future__ import annotations

import argparse
import importlib.metadata
import importlib.util
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable

from app.config import DATA_DIR, ROOT, config


LogFn = Callable[[str], None]
REPORT_FILE = DATA_DIR / "dependency_report.json"
DOWNLOAD_DIR = DATA_DIR / "downloads"
RUNTIME_FFMPEG_DIR = ROOT / "runtime" / "ffmpeg"
REQUIREMENTS_FILE = ROOT / "requirements.txt"
UPLOAD_VENDOR_SCRIPT = ROOT / "app" / "vendor" / "stage5_upload_browser.py"
UPLOAD_LEGACY_CANDIDATES = [
    Path(r"F:\Manao\drama_pipeline_app\app\stages\stage5_upload_browser.py"),
    Path(r"F:\Manao\drama_pipeline_release\app\stages\stage5_upload_browser.py"),
]


@dataclass
class PythonRequirement:
    dist: str
    import_name: str
    minimum: str
    scope: str

    @property
    def requirement(self) -> str:
        return f"{self.dist}>={self.minimum}" if self.minimum else self.dist


@dataclass
class DependencyStatus:
    name: str
    ok: bool
    installed: str = ""
    required: str = ""
    detail: str = ""
    path: str = ""


PYTHON_REQUIREMENTS = [
    PythonRequirement("httpx", "httpx", "0.27.0", "core"),
    PythonRequirement("selectolax", "selectolax", "0.3.21", "core"),
    PythonRequirement("beautifulsoup4", "bs4", "4.12.0", "core"),
    PythonRequirement("edge-tts", "edge_tts", "6.1.10", "core"),
    PythonRequirement("Pillow", "PIL", "10.0.0", "core"),
    PythonRequirement("numpy", "numpy", "1.24.0", "core"),
    PythonRequirement("playwright", "playwright", "1.40.0", "full"),
    PythonRequirement("pyperclip", "pyperclip", "1.8.2", "full"),
    PythonRequirement("pyautogui", "pyautogui", "0.9.54", "full"),
    PythonRequirement("pydantic", "pydantic", "2.0.0", "full"),
]


def ensure_dependencies(*, scope: str = "full", on_log: LogFn | None = None) -> dict[str, Any]:
    """Check dependencies and auto-repair the pieces enabled in config.

    scope="core" installs only the packages needed for the desktop GUI to
    import. scope="full" also checks FFmpeg, browser/upload prerequisites and
    optional browser upload packages.
    """
    scope = "core" if str(scope).lower() == "core" else "full"
    logs: list[str] = []

    def log(text: str) -> None:
        logs.append(text)
        if on_log:
            on_log(text)

    started = time.strftime("%Y-%m-%d %H:%M:%S")
    log(f"[依赖检测] scope={scope} started={started}")

    selected = _selected_requirements(scope)
    python_before = check_python_requirements(scope=scope)
    missing = [item for item in python_before if not item.ok]
    if missing:
        log("[依赖检测] 缺少/版本过低的 Python 包：" + ", ".join(item.name for item in missing))
        if bool(config.get("dependency_auto_install_python", True)):
            install_python_requirements(selected if scope == "core" else None, log)
            python_after = check_python_requirements(scope=scope)
        else:
            log("[依赖检测] 已关闭 Python 包自动安装。")
            python_after = python_before
    else:
        log("[依赖检测] Python 包正常。")
        python_after = python_before

    tkinter_status = check_tkinter_runtime()
    if tkinter_status.ok:
        log("[依赖检测] Tkinter GUI 运行时正常。")
    else:
        log("[依赖检测] Tkinter GUI 运行时异常：" + tkinter_status.detail)

    ffmpeg = None
    browser = None
    upload_script = None
    if scope == "full":
        ffmpeg = ensure_ffmpeg(log)
        browser = ensure_browser(log)
        upload_script = ensure_upload_script(log)

    report = {
        "started": started,
        "finished": time.strftime("%Y-%m-%d %H:%M:%S"),
        "scope": scope,
        "python": [asdict(item) for item in python_after],
        "tkinter": asdict(tkinter_status),
        "ffmpeg": asdict(ffmpeg) if ffmpeg else None,
        "browser": asdict(browser) if browser else None,
        "upload_script": asdict(upload_script) if upload_script else None,
        "ok": all(item.ok for item in python_after)
        and tkinter_status.ok
        and (ffmpeg is None or ffmpeg.ok)
        and (browser is None or browser.ok)
        and (upload_script is None or upload_script.ok),
        "logs": logs,
    }
    report["summary"] = summarize_report(report)
    _save_report(report)
    return report


def check_python_requirements(*, scope: str = "full") -> list[DependencyStatus]:
    return [_check_python_requirement(req) for req in _selected_requirements(scope)]


def check_tkinter_runtime() -> DependencyStatus:
    proc = _run(
        [
            sys.executable,
            "-c",
            "import tkinter; print('ok')",
        ],
        timeout=20,
    )
    if proc.returncode == 0:
        return DependencyStatus(
            name="Tkinter GUI runtime",
            ok=True,
            detail="ok",
            path=sys.executable,
            required="Python with working Tkinter",
        )
    detail = _tail(proc.stdout + "\n" + proc.stderr, 1200) or "Tkinter import failed"
    if "macOS 26" in detail and "have instead 16" in detail:
        detail += "；macOS 26/Tahoe 上请使用 Homebrew python@3.12 + python-tk@3.12 或 Python.org Python。"
    return DependencyStatus(
        name="Tkinter GUI runtime",
        ok=False,
        detail=detail,
        path=sys.executable,
        required="Python with working Tkinter",
    )


def install_python_requirements(requirements: list[PythonRequirement] | None, log: LogFn) -> None:
    if not _ensure_pip(log):
        raise RuntimeError("pip 不可用，无法自动安装 Python 包")

    cmd = [sys.executable, "-m", "pip", "install", "--disable-pip-version-check"]
    index_url = str(config.get("dependency_pip_index_url", "") or "").strip()
    if index_url:
        cmd.extend(["-i", index_url])
    extra_args = str(config.get("dependency_pip_extra_args", "") or "").strip()
    if extra_args:
        cmd.extend(shlex.split(extra_args))
    if requirements is None:
        cmd.extend(["-r", str(REQUIREMENTS_FILE)])
    else:
        cmd.extend(req.requirement for req in requirements)

    log("[依赖检测] 正在安装 Python 包，可能需要几分钟...")
    timeout = int(config.get("dependency_pip_timeout_seconds", 1800) or 1800)
    proc = _run(cmd, timeout=timeout)
    if proc.returncode != 0:
        tail = _tail(proc.stdout + "\n" + proc.stderr, 3000)
        log("[依赖检测] pip 安装失败：\n" + tail)
        raise RuntimeError("pip install failed")
    log("[依赖检测] Python 包安装完成。")


def ensure_ffmpeg(log: LogFn) -> DependencyStatus:
    status = check_ffmpeg()
    if status.ok:
        if sys.platform == "darwin":
            full_ffmpeg = _homebrew_ffmpeg_full_path("ffmpeg")
            if full_ffmpeg and _ffmpeg_has_filter(full_ffmpeg, "ass"):
                _write_ffmpeg_full_marker(full_ffmpeg)
        log(f"[依赖检测] FFmpeg 正常：{status.path}")
        return status
    log("[依赖检测] FFmpeg 缺失或不支持 ASS 硬字幕。")
    if not bool(config.get("dependency_auto_install_ffmpeg", True)):
        status.detail = "已关闭 FFmpeg 自动下载"
        return status
    if sys.platform == "darwin":
        return _ensure_macos_ffmpeg_full(log)
    if os.name != "nt":
        status.detail = "当前系统不支持自动安装 FFmpeg"
        return status
    try:
        _download_and_install_ffmpeg(log)
    except Exception as exc:
        status = check_ffmpeg()
        status.detail = f"FFmpeg 自动下载失败：{exc}"
        log("[依赖检测] " + status.detail)
        return status
    status = check_ffmpeg()
    if status.ok:
        log(f"[依赖检测] FFmpeg 已安装到：{status.path}")
    return status


def check_ffmpeg() -> DependencyStatus:
    ffmpeg = _resolve_executable("ffmpeg")
    ffprobe = _resolve_executable("ffprobe")
    ffmpeg_ok = bool(ffmpeg and _exe_works(ffmpeg, ["-version"]))
    ffprobe_ok = bool(ffprobe and _exe_works(ffprobe, ["-version"]))
    subtitle_ok = bool(ffmpeg_ok and (sys.platform != "darwin" or _ffmpeg_has_filter(ffmpeg, "ass")))
    ok = bool(ffmpeg_ok and subtitle_ok and (ffprobe_ok or os.name != "nt"))
    if ffmpeg_ok and not ffprobe_ok and os.name != "nt":
        detail = "ffprobe missing; duration detection will use ffmpeg fallback"
    elif ffmpeg_ok and sys.platform == "darwin" and not subtitle_ok:
        detail = "FFmpeg lacks libass/ass subtitle filter"
    elif ok:
        detail = "ok"
    else:
        detail = "missing"
    return DependencyStatus(
        name="FFmpeg / FFprobe",
        ok=ok,
        detail=detail,
        path=str(ffmpeg or ""),
        required="ffmpeg + ffprobe + libass" if sys.platform == "darwin" else "ffmpeg + ffprobe",
    )


def _ffmpeg_has_filter(path: Path | None, filter_name: str) -> bool:
    if not path:
        return False
    proc = _run([str(path), "-hide_banner", "-filters"], timeout=30)
    if proc.returncode != 0:
        return False
    pattern = re.compile(rf"^\s*[TSC\.]+\s+{re.escape(filter_name)}\s", re.MULTILINE)
    return bool(pattern.search(proc.stdout or ""))


def _ensure_macos_ffmpeg_full(log: LogFn) -> DependencyStatus:
    brew = shutil.which("brew")
    if not brew:
        status = check_ffmpeg()
        status.detail = "Homebrew 不可用，无法自动安装带 libass 的 ffmpeg-full"
        log("[依赖检测] " + status.detail)
        return status

    full_ffmpeg = _homebrew_ffmpeg_full_path("ffmpeg")
    if not full_ffmpeg or not _ffmpeg_has_filter(full_ffmpeg, "ass"):
        log("[依赖检测] 正在一次性安装 ffmpeg-full（包含 libass），首次可能需要几分钟...")
        env = dict(os.environ)
        env.update(
            {
                "HOMEBREW_NO_AUTO_UPDATE": "1",
                "HOMEBREW_NO_INSTALL_CLEANUP": "1",
                "HOMEBREW_NO_ENV_HINTS": "1",
                "HOMEBREW_NO_INTERACTIVE": "1",
            }
        )
        proc = _run([brew, "install", "ffmpeg-full"], timeout=3600, env=env)
        if proc.returncode != 0:
            status = check_ffmpeg()
            status.detail = "ffmpeg-full 自动安装失败：" + _tail(proc.stdout + "\n" + proc.stderr, 1800)
            log("[依赖检测] " + status.detail)
            return status
        full_ffmpeg = _homebrew_ffmpeg_full_path("ffmpeg")

    if full_ffmpeg and _ffmpeg_has_filter(full_ffmpeg, "ass"):
        _link_macos_ffmpeg_full(brew, full_ffmpeg, log)
        status = check_ffmpeg()
        if status.ok:
            _write_ffmpeg_full_marker(full_ffmpeg)
            log(f"[依赖检测] ffmpeg-full/libass 已就绪：{full_ffmpeg}")
        return status

    status = check_ffmpeg()
    status.detail = "ffmpeg-full 安装后仍未检测到 ass 字幕滤镜"
    log("[依赖检测] " + status.detail)
    return status


def _homebrew_ffmpeg_full_path(name: str) -> Path | None:
    for prefix in (Path("/opt/homebrew"), Path("/usr/local")):
        candidate = prefix / "opt" / "ffmpeg-full" / "bin" / name
        if candidate.exists() and os.access(candidate, os.X_OK):
            return candidate
    return None


def _link_macos_ffmpeg_full(brew: str, full_ffmpeg: Path, log: LogFn) -> None:
    current = shutil.which("ffmpeg")
    try:
        if current and Path(current).resolve() == full_ffmpeg.resolve():
            return
    except OSError:
        pass
    _run([brew, "unlink", "ffmpeg"], timeout=120)
    proc = _run([brew, "link", "--force", "--overwrite", "ffmpeg-full"], timeout=300)
    if proc.returncode == 0:
        log("[依赖检测] 系统 FFmpeg 已链接到 ffmpeg-full。")
    else:
        # The app still prefers the keg-only path, so a link failure is not fatal.
        log("[依赖检测] Homebrew 链接未完成，程序将直接使用 ffmpeg-full 路径。")


def _write_ffmpeg_full_marker(full_ffmpeg: Path) -> None:
    marker = DATA_DIR / "runtime" / "ffmpeg_full_ready.json"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(
        json.dumps(
            {
                "ready": True,
                "path": str(full_ffmpeg),
                "checked_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def ensure_browser(log: LogFn) -> DependencyStatus:
    status = check_browser()
    if status.ok:
        log(f"[依赖检测] 浏览器正常：{status.path}")
        return status
    log("[依赖检测] 未找到 Chrome/Chromium。YouTube 上传需要调试模式浏览器。")
    if not bool(config.get("dependency_auto_install_browser", False)):
        status.detail = "未开启浏览器自动安装；可在配置中开启，或手动安装 Chrome。"
        return status
    winget = shutil.which("winget")
    if not winget:
        status.detail = "未找到 winget，无法自动安装 Chrome。"
        return status
    cmd = [
        winget,
        "install",
        "--id",
        "Google.Chrome",
        "-e",
        "--silent",
        "--accept-package-agreements",
        "--accept-source-agreements",
    ]
    log("[依赖检测] 正在通过 winget 安装 Chrome...")
    proc = _run(cmd, timeout=1200)
    if proc.returncode != 0:
        status.detail = "winget 安装 Chrome 失败：" + _tail(proc.stdout + "\n" + proc.stderr, 1200)
        return status
    return check_browser()


def check_browser() -> DependencyStatus:
    chrome = _find_chrome()
    if chrome:
        return DependencyStatus(name="Chrome / Chromium", ok=True, path=str(chrome), detail="ok", required="Chrome or Chromium")
    edge = _find_edge()
    detail = "Chrome/Chromium missing"
    if edge:
        detail += f"; Edge exists at {edge}"
    return DependencyStatus(name="Chrome / Chromium", ok=False, path="", detail=detail, required="Chrome or Chromium")


def ensure_upload_script(log: LogFn) -> DependencyStatus:
    if UPLOAD_VENDOR_SCRIPT.exists():
        log(f"[依赖检测] 上传脚本正常：{UPLOAD_VENDOR_SCRIPT}")
        return DependencyStatus(name="YouTube upload script", ok=True, path=str(UPLOAD_VENDOR_SCRIPT), detail="ok")
    source = next((path for path in UPLOAD_LEGACY_CANDIDATES if path.exists()), None)
    if source:
        UPLOAD_VENDOR_SCRIPT.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, UPLOAD_VENDOR_SCRIPT)
        log(f"[依赖检测] 已复制上传脚本：{UPLOAD_VENDOR_SCRIPT}")
        return DependencyStatus(name="YouTube upload script", ok=True, path=str(UPLOAD_VENDOR_SCRIPT), detail="copied")
    return DependencyStatus(
        name="YouTube upload script",
        ok=False,
        detail="未找到本地上传脚本；请随软件一起分发 app/vendor/stage5_upload_browser.py",
    )


def summarize_report(report: dict[str, Any]) -> str:
    py_items = report.get("python") or []
    missing_py = [item["name"] for item in py_items if not item.get("ok")]
    parts = []
    parts.append("Python包正常" if not missing_py else "Python包缺失：" + ", ".join(missing_py))
    tkinter = report.get("tkinter")
    if tkinter:
        parts.append(f"Tkinter{'正常' if tkinter.get('ok') else '异常'}")
    for key, label in [("ffmpeg", "FFmpeg"), ("browser", "Chrome"), ("upload_script", "上传脚本")]:
        item = report.get(key)
        if item:
            parts.append(f"{label}{'正常' if item.get('ok') else '异常'}")
    return "；".join(parts)


def report_to_lines(report: dict[str, Any]) -> list[str]:
    lines = list(report.get("logs") or [])
    summary = str(report.get("summary") or "").strip()
    if summary:
        lines.append("[依赖检测] " + summary)
    return lines


def _selected_requirements(scope: str) -> list[PythonRequirement]:
    scope = "core" if str(scope).lower() == "core" else "full"
    if scope == "core":
        return [req for req in PYTHON_REQUIREMENTS if req.scope == "core"]
    return list(PYTHON_REQUIREMENTS)


def _check_python_requirement(req: PythonRequirement) -> DependencyStatus:
    installed = ""
    try:
        installed = importlib.metadata.version(req.dist)
    except importlib.metadata.PackageNotFoundError:
        installed = ""
    import_ok = importlib.util.find_spec(req.import_name) is not None
    version_ok = bool(installed) and _version_gte(installed, req.minimum)
    ok = bool(import_ok and version_ok)
    detail = "ok" if ok else ("not installed" if not installed else "version too old or import failed")
    return DependencyStatus(
        name=req.dist,
        ok=ok,
        installed=installed,
        required=req.requirement,
        detail=detail,
    )


def _version_gte(installed: str, minimum: str) -> bool:
    return _version_tuple(installed) >= _version_tuple(minimum)


def _version_tuple(value: str) -> tuple[int, int, int]:
    nums = [int(x) for x in re.findall(r"\d+", str(value or ""))[:3]]
    while len(nums) < 3:
        nums.append(0)
    return tuple(nums[:3])


def _ensure_pip(log: LogFn) -> bool:
    proc = _run([sys.executable, "-m", "pip", "--version"], timeout=30)
    if proc.returncode == 0:
        return True
    log("[依赖检测] pip 不可用，尝试 ensurepip...")
    proc = _run([sys.executable, "-m", "ensurepip", "--upgrade"], timeout=180)
    if proc.returncode != 0:
        log("[依赖检测] ensurepip 失败：" + _tail(proc.stdout + "\n" + proc.stderr, 1200))
        return False
    return _run([sys.executable, "-m", "pip", "--version"], timeout=30).returncode == 0


def _resolve_executable(name: str) -> Path | None:
    exe = f"{name}.exe" if os.name == "nt" else name
    env_value = os.environ.get(f"MEDIA_{name.upper()}_BIN")
    candidates = []
    if env_value:
        candidates.append(Path(env_value))
    if sys.platform == "darwin":
        full = _homebrew_ffmpeg_full_path(name)
        if full:
            candidates.append(full)
    found = shutil.which(name)
    if found:
        candidates.append(Path(found))
    if os.name != "nt":
        candidates.extend(
            [
                Path.home() / ".local" / "bin" / name,
                Path("/opt/homebrew/bin") / name,
                Path("/usr/local/bin") / name,
                Path("/usr/bin") / name,
            ]
        )
    candidates.extend(
        [
            ROOT / "runtime" / "ffmpeg" / exe,
            ROOT / "tools" / "ffmpeg" / exe,
            ROOT / "tools" / "ffmpeg" / "bin" / exe,
            ROOT / "vendor" / "ffmpeg" / exe,
            Path(r"F:\Manao\drama_pipeline_release\tools\ffmpeg") / exe,
        ]
    )
    for path in candidates:
        if path and path.exists() and (os.name == "nt" or os.access(path, os.X_OK)):
            return path
    return None


def _download_and_install_ffmpeg(log: LogFn) -> None:
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    urls = _ffmpeg_urls()
    errors: list[str] = []
    for url in urls:
        try:
            archive = DOWNLOAD_DIR / "ffmpeg-release.zip"
            _download_file(url, archive, log)
            _extract_ffmpeg_archive(archive, log)
            return
        except Exception as exc:
            errors.append(f"{url}: {exc}")
            log(f"[依赖检测] FFmpeg 下载源失败：{exc}")
    raise RuntimeError("; ".join(errors))


def _ffmpeg_urls() -> list[str]:
    configured = str(config.get("dependency_ffmpeg_url", "") or "").strip()
    urls = []
    if configured:
        urls.append(configured)
    urls.extend(
        [
            "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip",
            "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip",
        ]
    )
    deduped: list[str] = []
    for url in urls:
        if url and url not in deduped:
            deduped.append(url)
    return deduped


def _download_file(url: str, out_path: Path, log: LogFn) -> None:
    log(f"[依赖检测] 下载 FFmpeg：{url}")
    request = urllib.request.Request(url, headers={"User-Agent": "novel-video-pipeline/1.0"})
    with urllib.request.urlopen(request, timeout=60) as response:
        total = int(response.headers.get("Content-Length", "0") or "0")
        done = 0
        next_log = 32 * 1024 * 1024
        with out_path.open("wb") as handle:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                handle.write(chunk)
                done += len(chunk)
                if done >= next_log:
                    if total:
                        log(f"[依赖检测] FFmpeg 已下载 {done / 1024 / 1024:.0f}/{total / 1024 / 1024:.0f} MB")
                    else:
                        log(f"[依赖检测] FFmpeg 已下载 {done / 1024 / 1024:.0f} MB")
                    next_log += 32 * 1024 * 1024


def _extract_ffmpeg_archive(archive: Path, log: LogFn) -> None:
    with tempfile.TemporaryDirectory(prefix="novel_ffmpeg_") as temp:
        temp_dir = Path(temp)
        with zipfile.ZipFile(archive) as zf:
            zf.extractall(temp_dir)
        ffmpeg = next(temp_dir.rglob("ffmpeg.exe"), None)
        ffprobe = next(temp_dir.rglob("ffprobe.exe"), None)
        if not ffmpeg or not ffprobe:
            raise RuntimeError("压缩包里没有找到 ffmpeg.exe / ffprobe.exe")
        RUNTIME_FFMPEG_DIR.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ffmpeg, RUNTIME_FFMPEG_DIR / "ffmpeg.exe")
        shutil.copy2(ffprobe, RUNTIME_FFMPEG_DIR / "ffprobe.exe")
        ffplay = next(temp_dir.rglob("ffplay.exe"), None)
        if ffplay:
            shutil.copy2(ffplay, RUNTIME_FFMPEG_DIR / "ffplay.exe")
        log(f"[依赖检测] FFmpeg 已解压到：{RUNTIME_FFMPEG_DIR}")


def _exe_works(path: Path, args: list[str]) -> bool:
    return _run([str(path), *args], timeout=20).returncode == 0


def _find_chrome() -> Path | None:
    candidates = []
    found = (
        shutil.which("google-chrome")
        or shutil.which("chrome")
        or shutil.which("chromium")
        or shutil.which("chrome.exe")
    )
    if found:
        candidates.append(Path(found))
    if os.name == "nt":
        for env in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
            base = os.environ.get(env)
            if base:
                candidates.append(Path(base) / "Google" / "Chrome" / "Application" / "chrome.exe")
    elif sys.platform == "darwin":
        candidates.extend(
            [
                Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
                Path.home() / "Applications" / "Google Chrome.app" / "Contents" / "MacOS" / "Google Chrome",
                Path("/Applications/Chromium.app/Contents/MacOS/Chromium"),
                Path.home() / "Applications" / "Chromium.app" / "Contents" / "MacOS" / "Chromium",
            ]
        )
    found_path = next((path for path in candidates if path.exists()), None)
    return found_path or _find_playwright_chromium()


def _find_playwright_chromium() -> Path | None:
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as pw:
            path = Path(pw.chromium.executable_path)
        if path.exists():
            return path
    except Exception:
        return None
    return None


def _find_edge() -> Path | None:
    candidates = []
    found = shutil.which("msedge") or shutil.which("msedge.exe")
    if found:
        candidates.append(Path(found))
    if os.name == "nt":
        for env in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
            base = os.environ.get(env)
            if base:
                candidates.append(Path(base) / "Microsoft" / "Edge" / "Application" / "msedge.exe")
    elif sys.platform == "darwin":
        candidates.extend(
            [
                Path("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"),
                Path.home() / "Applications" / "Microsoft Edge.app" / "Contents" / "MacOS" / "Microsoft Edge",
            ]
        )
    return next((path for path in candidates if path.exists()), None)


def _run(args: list[str], *, timeout: int, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    kwargs: dict[str, Any] = {}
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        return subprocess.run(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="ignore",
            timeout=timeout,
            env=env,
            **kwargs,
        )
    except Exception as exc:
        return subprocess.CompletedProcess(args, 1, "", str(exc))


def _tail(text: str, limit: int) -> str:
    text = str(text or "").strip()
    return text[-limit:] if len(text) > limit else text


def _save_report(report: dict[str, Any]) -> None:
    try:
        REPORT_FILE.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        config.set("dependency_last_report", str(report.get("summary", "")))
        config.save()
    except Exception:
        pass


def main() -> int:
    parser = argparse.ArgumentParser(description="Check and repair novel_video_pipeline dependencies")
    parser.add_argument("--ensure", action="store_true", help="check full dependencies and install what is enabled")
    parser.add_argument("--ensure-core", action="store_true", help="check only GUI core Python packages")
    parser.add_argument("--ensure-ffmpeg-subtitles", action="store_true", help="ensure macOS ffmpeg-full/libass only")
    args = parser.parse_args()
    if args.ensure_ffmpeg_subtitles:
        status = ensure_ffmpeg(print)
        return 0 if status.ok else 1
    scope = "core" if args.ensure_core else "full"
    report = ensure_dependencies(scope=scope, on_log=print)
    print("[依赖检测] " + str(report.get("summary", "")))
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
