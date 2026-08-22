"""
Stage 5 (浏览器上传): 用 Playwright 通过 CDP 连接已打开的 Chrome
前提：Chrome 必须以 --remote-debugging-port=9222 启动（见软件目录的快捷方式）
流程：
  1. 连接已有 Chrome（复用登录态，无需重新登录）
  2. 打开上传页面 → 填标题/封面
  精简流程（flow=simple）：直接跳公开范围页 → 自动发布
  完整流程（flow=full）：设置权利管理 → 创收/广告位 → 分级 → 发布
"""
from __future__ import annotations

import re
import json
import os
import shutil
import sys
import time
import socket
import subprocess
import threading
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

try:
    from app.youtube_ad_suitability import normalize_ad_suitability_template
except Exception:
    try:
        from ..youtube_ad_suitability import normalize_ad_suitability_template
    except Exception:
        def normalize_ad_suitability_template(template=None):
            if isinstance(template, dict):
                questions = template.get("questions")
                return {
                    "default": int(template.get("default", 1) or 1),
                    "questions": questions if isinstance(questions, dict) else {},
                }
            if isinstance(template, str) and template.strip():
                try:
                    data = json.loads(template)
                    if isinstance(data, dict):
                        questions = data.get("questions")
                        return {
                            "default": int(data.get("default", 1) or 1),
                            "questions": questions if isinstance(questions, dict) else {},
                        }
                except Exception:
                    pass
            return {"default": 1, "questions": {}}

_DEBUG_PORT = 9222
_STALL = "STALL"   # 标志：上传卡住，需要外层重启浏览器
_CHANNEL_GUARD_FAILED = "CHANNEL_GUARD_FAILED"


class _UploadPolicyConfirmError(RuntimeError):
    """Upload policy did not stick; refresh Studio and restart the upload flow."""


def _activate_chrome_window():
    """将 Chrome 主窗口激活到前台，确保原生文件对话框能正常弹出。
    用 keybd_event 模拟按键解除 Windows 对 SetForegroundWindow 的限制。"""
    if sys.platform == "darwin":
        for app_name in ("Google Chrome", "Google Chrome for Testing", "Chromium"):
            try:
                subprocess.run(
                    ["osascript", "-e", f'tell application "{app_name}" to activate'],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=3,
                    check=True,
                )
                return True
            except Exception:
                continue
        return False
    if os.name != "nt":
        return False
    import ctypes
    import ctypes.wintypes

    user32 = ctypes.windll.user32
    WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM)
    chrome_hwnds = []

    def _enum(hwnd, _):
        if user32.IsWindowVisible(hwnd):
            cls = ctypes.create_unicode_buffer(64)
            user32.GetClassNameW(hwnd, cls, 64)
            if cls.value == "Chrome_WidgetWin_1":
                title_buf = ctypes.create_unicode_buffer(256)
                user32.GetWindowTextW(hwnd, title_buf, 256)
                if title_buf.value:
                    chrome_hwnds.append(hwnd)
        return True

    user32.EnumWindows(WNDENUMPROC(_enum), 0)
    if not chrome_hwnds:
        return False

    hwnd = chrome_hwnds[0]
    try:
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        # 模拟一次 Alt 按键释放，解除 Windows 对非前台进程调用
        # SetForegroundWindow 的限制（否则只会闪烁任务栏图标）
        VK_MENU = 0x12
        KEYEVENTF_EXTENDEDKEY = 0x0001
        KEYEVENTF_KEYUP = 0x0002
        user32.keybd_event(VK_MENU, 0, KEYEVENTF_EXTENDEDKEY, 0)
        user32.keybd_event(VK_MENU, 0, KEYEVENTF_EXTENDEDKEY | KEYEVENTF_KEYUP, 0)
        user32.SetForegroundWindow(hwnd)
        return True
    except Exception:
        return False


# ─────────────────────────────────────────────────────────────
# 操作速度延迟配置
# ─────────────────────────────────────────────────────────────

class _Delays:
    """根据速度档位统一管理各步骤等待时间（毫秒）"""
    def __init__(self, speed: str):
        if speed == "very_slow":
            self.click_after   = 1500
            self.step_between  = 2000
            self.type_delay    = 30
            self.page_load     = 3000
        elif speed == "slow":
            self.click_after   = 1000
            self.step_between  = 1500
            self.type_delay    = 20
            self.page_load     = 2000
        else:  # normal
            self.click_after   = 800
            self.step_between  = 1000
            self.type_delay    = 15
            self.page_load     = 1000


# ─────────────────────────────────────────────────────────────
# Chrome 重启工具
# ─────────────────────────────────────────────────────────────

def _find_chrome_exe() -> Optional[str]:
    candidates = []
    found = (
        shutil.which("google-chrome")
        or shutil.which("chrome")
        or shutil.which("chromium")
        or shutil.which("chrome.exe")
    )
    if found:
        candidates.append(found)
    if os.name == "nt":
        candidates.extend(
            [
                os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
                r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            ]
        )
    elif sys.platform == "darwin":
        candidates.extend(
            [
                "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                str(Path.home() / "Applications" / "Google Chrome.app" / "Contents" / "MacOS" / "Google Chrome"),
                "/Applications/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing",
                str(Path.home() / "Applications" / "Google Chrome for Testing.app" / "Contents" / "MacOS" / "Google Chrome for Testing"),
                "/Applications/Chromium.app/Contents/MacOS/Chromium",
                str(Path.home() / "Applications" / "Chromium.app" / "Contents" / "MacOS" / "Chromium"),
            ]
        )
    for c in candidates:
        if Path(c).exists():
            return c
    playwright_chromium = _find_playwright_chromium_exe()
    if playwright_chromium:
        return playwright_chromium
    return None


def _find_playwright_chromium_exe() -> Optional[str]:
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as pw:
            path = Path(pw.chromium.executable_path)
        if path.exists():
            return str(path)
    except Exception:
        return None
    return None


def _safe_chrome_profile_name(name: str) -> str:
    raw = str(name or "").strip() or "Default"
    safe = re.sub(r'[\\/:*?"<>|]+', "_", raw).strip(" ._")
    return (safe or "Default")[:64]


def _chrome_debug_profile_dir(config, profile_name: str = "") -> Path:
    name = _safe_chrome_profile_name(profile_name or getattr(config, "browser_chrome_profile", "Default"))
    data_dir = Path(__file__).parent.parent.parent / "data"
    if name.lower() == "default":
        return data_dir / "chrome_debug_profile"
    return data_dir / "chrome_debug_profiles" / name


def _kill_chrome(profile_dir: Optional[Path] = None):
    if os.name != "nt":
        try:
            subprocess.run(
                ["pkill", "-f", f"remote-debugging-port={_DEBUG_PORT}"],
                capture_output=True,
                timeout=10,
            )
            if profile_dir is not None:
                subprocess.run(
                    ["pkill", "-f", str(profile_dir)],
                    capture_output=True,
                    timeout=10,
                )
        except Exception:
            pass
        return

    matched = False
    profile_text = str(profile_dir).replace("\\", "\\\\") if profile_dir else ""
    ps = (
        "$items = Get-CimInstance Win32_Process -Filter \"Name='chrome.exe'\" | "
        "Where-Object { "
        "$_.CommandLine -like '*--remote-debugging-port=9222*' "
        "-or $_.CommandLine -like '*chrome_debug_profile*' "
        "-or $_.CommandLine -like '*chrome_debug_profiles*'"
    )
    if profile_text:
        ps += f" -or $_.CommandLine -like '*{profile_text}*'"
    ps += (
        " }; "
        "$items | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }; "
        "if ($items) { 'killed' }"
    )
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            capture_output=True, text=True, timeout=10,
            creationflags=0x08000000,
        )
        matched = "killed" in (r.stdout or "")
    except Exception:
        pass
    if matched:
        return
    if profile_dir is not None:
        return
    try:
        subprocess.run(["taskkill", "/F", "/IM", "chrome.exe"],
                       capture_output=True, timeout=10, creationflags=0x08000000)
    except Exception:
        pass


def _stop_debug_chrome_and_wait(profile_dir: Optional[Path], on_log: Callable) -> bool:
    """Stop the port-9222 browser before changing YouTube accounts.

    A fixed remote-debugging port can only belong to one Chrome instance.  If
    the prior instance has not exited yet, launching the requested profile may
    silently leave the old account serving port 9222.  In that situation it is
    safer to stop than to upload to the wrong channel.
    """
    _kill_chrome(profile_dir)
    for _ in range(20):
        if not _is_port_open(_DEBUG_PORT):
            return True
        time.sleep(0.25)
    on_log("❌ 上一个 Chrome 未能退出；为防止上传到错误频道，本次上传已停止。")
    return False


def _launch_chrome(config, on_log: Callable, profile_name: str = "") -> bool:
    """启动调试模式 Chrome，等端口就绪后再等页面基本加载完"""
    chrome = _find_chrome_exe()
    if not chrome:
        on_log("❌ 未找到 Chrome 可执行文件，无法自动重启")
        return False
    profile_dir = _chrome_debug_profile_dir(config, profile_name)
    profile = str(profile_dir)
    profile_label = _safe_chrome_profile_name(profile_name or getattr(config, "browser_chrome_profile", "Default"))
    if _is_port_open(_DEBUG_PORT):
        on_log(f"❌ 调试端口仍被其他 Chrome 占用，未切换到账号资料「{profile_label}」。")
        return False
    _clean_chrome_debug_profile_cache(profile_dir, on_log)
    cmd = [
        chrome,
        f"--remote-debugging-port={_DEBUG_PORT}",
        "--remote-allow-origins=*",
        f"--user-data-dir={profile}",
        "--disable-features=PreloadMediaEngagementData,MediaRouter",
        "--no-first-run",
        "--no-default-browser-check",
        "https://studio.youtube.com",
    ]
    try:
        subprocess.Popen(cmd)
        on_log(f"  Chrome 已启动（账号资料: {profile_label}），等待调试端口就绪...")
        for _ in range(30):
            time.sleep(0.5)
            if _is_port_open(_DEBUG_PORT):
                on_log(f"  ✓ 调试端口 {_DEBUG_PORT} 就绪，等待 YouTube Studio 加载...")
                time.sleep(3)
                return True
        on_log(f"  ⚠️ 等待超时，端口 {_DEBUG_PORT} 仍未开放")
        return False
    except Exception as e:
        on_log(f"  ❌ 启动 Chrome 失败: {e}")
        return False


def _use_local_playwright_upload() -> bool:
    """Whether to replace the signed-in debug Chrome with a new local one.

    Keep this disabled.  Reusing the exact debug Chrome preserves the channel
    login selected in the GUI.  Large local files are injected with CDP by
    :func:`_set_local_file_via_cdp`, avoiding Playwright's 50 MB transfer cap
    without changing browser sessions.
    """
    return False


def read_current_studio_channel(timeout_seconds: int = 300) -> dict:
    """Read the channel currently opened in the debug Chrome.

    This is used by the GUI's "open and bind" action.  The immutable Studio
    channel ID is the actual binding; the display name is saved for the
    operator to recognize it in the scheduling dialog.
    """
    from playwright.sync_api import sync_playwright

    deadline = time.time() + max(5, int(timeout_seconds or 300))
    with sync_playwright() as pw:
        browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{_DEBUG_PORT}")
        ctx = browser.contexts[0] if browser.contexts else browser.new_context()
        page = next((p for p in reversed(ctx.pages) if "studio.youtube.com" in p.url), None)
        if page is None:
            page = ctx.new_page()
            page.goto("https://studio.youtube.com", timeout=60_000)
        while time.time() < deadline:
            match = re.search(r"studio\.youtube\.com/channel/([^/?#]+)", page.url)
            if match:
                channel_id = match.group(1).strip()
                channel_name = ""
                for selector in ("ytcp-navigation-drawer #entity-name", "#entity-name"):
                    try:
                        locator = page.locator(selector).first
                        if locator.is_visible(timeout=2_000):
                            channel_name = " ".join(locator.inner_text(timeout=2_000).split())
                            if channel_name:
                                break
                    except Exception:
                        continue
                if channel_name:
                    return {"channel_id": channel_id, "channel_name": channel_name, "url": page.url}
            page.wait_for_timeout(1_000)
        raise RuntimeError("未能读取当前 YouTube Studio 频道；请确认页面已登录并进入频道信息中心")


def _clean_chrome_debug_profile_cache(profile_dir: Path, on_log: Callable | None = None):
    """
    Clear stale YouTube Studio frontend caches in the dedicated debug profile
    while preserving Google login cookies.
    """
    import shutil

    if not profile_dir.exists():
        return
    try:
        resolved = profile_dir.resolve()
        data_dir = (Path(__file__).parent.parent.parent / "data").resolve()
        legacy = (data_dir / "chrome_debug_profile").resolve()
        profiles_root = (data_dir / "chrome_debug_profiles").resolve()
        if resolved != legacy and not resolved.is_relative_to(profiles_root):
            return
    except Exception:
        return

    rel_targets = [
        "Default/Cache",
        "Default/Code Cache",
        "Default/GPUCache",
        "Default/DawnGraphiteCache",
        "Default/DawnWebGPUCache",
        "Default/Service Worker",
        "Default/Session Storage",
        "Default/blob_storage",
        "ShaderCache",
        "GrShaderCache",
        "GraphiteDawnCache",
        "GPUPersistentCache",
    ]
    removed = 0
    for rel in rel_targets:
        target = profile_dir / rel
        if not target.exists():
            continue
        try:
            if target.is_dir():
                shutil.rmtree(target, ignore_errors=True)
            else:
                target.unlink(missing_ok=True)
            removed += 1
        except Exception:
            pass
    if removed and on_log:
        on_log(f"  已清理调试 Chrome 前端缓存 {removed} 项（保留登录状态）")


def _reset_youtube_studio_runtime_cache(ctx, page, on_log: Callable | None = None):
    """Reset Studio app cache for already-open debug Chrome sessions."""
    try:
        session = ctx.new_cdp_session(page)
    except Exception:
        return
    try:
        session.send("Network.clearBrowserCache")
    except Exception:
        pass
    cleared = 0
    for origin in (
        "https://studio.youtube.com",
        "https://www.youtube.com",
        "https://youtube.com",
    ):
        try:
            session.send("Storage.clearDataForOrigin", {
                "origin": origin,
                "storageTypes": "cache_storage,service_workers,shader_cache",
            })
            cleared += 1
        except Exception:
            pass
    if cleared and on_log:
        on_log("  已刷新 YouTube Studio 前端缓存（保留账号登录）")


def _restart_chrome(config, on_log: Callable, profile_name: str = "") -> bool:
    on_log("⚙ 重启调试浏览器...")
    if not _stop_debug_chrome_and_wait(_chrome_debug_profile_dir(config, profile_name), on_log):
        return False
    return _launch_chrome(config, on_log, profile_name)


# ─────────────────────────────────────────────────────────────
# 上传方案解析
# ─────────────────────────────────────────────────────────────

def _bool_profile_value(value, default: bool = True) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    text = str(value).strip().lower()
    if text in {"0", "false", "no", "off", "disabled"}:
        return False
    if text in {"1", "true", "yes", "on", "enabled"}:
        return True
    return default


def _legacy_profile(config) -> dict:
    return {
        "name": getattr(config, "browser_active_profile", "") or "Default",
        "enabled": True,
        "chrome_profile": getattr(config, "browser_chrome_profile", "Default") or "Default",
        "flow": "full",
        "upload_policy": getattr(config, "browser_upload_policy", "BTRA"),
        "ad_interval": int(getattr(config, "browser_ad_interval", 60)),
        "ad_start": int(getattr(config, "browser_ad_start", 0)),
        "visibility": str(getattr(config, "browser_visibility", "PUBLIC")).strip().upper() or "PUBLIC",
        "title_template": "",
        "description": getattr(config, "youtube_description", ""),
        "cover_prompt": getattr(config, "cover_prompt", ""),
        "ad_suitability_template": getattr(config, "browser_ad_suitability_template", ""),
        "schedule_enabled": bool(getattr(config, "youtube_schedule_enabled", False)),
        "scheduled_at": str(getattr(config, "youtube_scheduled_at", "") or ""),
        "schedule_timezone": str(getattr(config, "youtube_schedule_timezone", "Asia/Tokyo") or "Asia/Tokyo"),
    }


def _normalize_profile(config, profile: dict, idx: int = 0) -> dict:
    base = _legacy_profile(config)
    if not isinstance(profile, dict):
        profile = {}
    merged = {**base, **profile}
    merged["name"] = str(merged.get("name") or f"方案{idx + 1}").strip() or f"方案{idx + 1}"
    merged["enabled"] = _bool_profile_value(merged.get("enabled"), True)
    merged["chrome_profile"] = str(
        merged.get("chrome_profile") or getattr(config, "browser_chrome_profile", "Default") or "Default"
    ).strip() or "Default"
    try:
        from app.youtube_channel_bindings import get_binding
        saved_binding = get_binding(merged["chrome_profile"], merged["name"])
    except Exception:
        saved_binding = {}
    merged["youtube_channel_id"] = str(
        merged.get("youtube_channel_id") or saved_binding.get("channel_id") or ""
    ).strip()
    merged["youtube_channel_name"] = str(
        merged.get("youtube_channel_name") or saved_binding.get("channel_name") or merged["name"]
    ).strip()
    merged["flow"] = str(merged.get("flow") or "simple").strip() or "simple"
    merged["upload_policy"] = str(merged.get("upload_policy") or "BTRA").strip() or "BTRA"
    try:
        merged["ad_interval"] = int(merged.get("ad_interval", 60))
    except Exception:
        merged["ad_interval"] = 60
    try:
        merged["ad_start"] = int(merged.get("ad_start", 0))
    except Exception:
        merged["ad_start"] = 0
    merged["visibility"] = str(merged.get("visibility") or "PUBLIC").strip().upper() or "PUBLIC"
    merged["title_template"] = str(merged.get("title_template") or "")
    merged["description"] = str(merged.get("description") or "")
    merged["cover_prompt"] = str(merged.get("cover_prompt") or "")
    merged["schedule_enabled"] = _bool_profile_value(merged.get("schedule_enabled"), False)
    merged["scheduled_at"] = str(merged.get("scheduled_at") or "")
    merged["schedule_timezone"] = str(merged.get("schedule_timezone") or "Asia/Tokyo")
    return merged


def _load_upload_profiles(config) -> list[dict]:
    profiles_raw = getattr(config, "browser_profiles", "[]") or "[]"
    try:
        profiles = json.loads(profiles_raw)
    except Exception:
        profiles = []

    if not profiles:
        return [_legacy_profile(config)]
    return [_normalize_profile(config, p, idx) for idx, p in enumerate(profiles)]


def _get_active_profile(config) -> dict:
    """解析激活的上传方案，找不到时 fallback 到旧字段"""
    profiles = _load_upload_profiles(config)
    active_name = getattr(config, "browser_active_profile", "") or ""
    for p in profiles:
        if p.get("name") == active_name:
            return p
    return profiles[0]


def _get_upload_profiles(config) -> list[dict]:
    """返回本次浏览器上传要执行的方案列表。默认只上传激活方案。"""
    profiles = _load_upload_profiles(config)
    if bool(getattr(config, "browser_upload_all_profiles", False)):
        enabled = [p for p in profiles if p.get("enabled", True)]
        return enabled or [_get_active_profile(config)]
    return [_get_active_profile(config)]


# ─────────────────────────────────────────────────────────────
# 公开入口
# ─────────────────────────────────────────────────────────────

def upload_via_browser(job, config, video_path: Path, title: str,
                       cover_path: Optional[Path],
                       on_log: Callable, on_progress: Callable,
                       profile: Optional[dict] = None,
                       force_profile_launch: bool = False) -> Optional[str]:
    """
    连接已打开的 Chrome（需带 --remote-debugging-port=9222），执行上传流程。
    根据激活方案的 flow 字段选择精简或完整流程。
    支持 Chrome 崩溃自动重启和上传卡住自动重启。
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        on_log("❌ playwright 未安装，请运行: pip install playwright && playwright install chrome")
        return None

    profile = _normalize_profile(config, profile, 0) if profile is not None else _get_active_profile(config)
    flow          = profile.get("flow", "simple")
    ad_interval   = int(profile.get("ad_interval", 60))
    ad_start      = int(profile.get("ad_start", 0))
    upload_policy = str(profile.get("upload_policy", "BTRA")).strip() or "BTRA"
    visibility    = str(profile.get("visibility", "PUBLIC")).strip().upper() or "PUBLIC"
    schedule_enabled = bool(profile.get("schedule_enabled", False))
    scheduled_at = str(profile.get("scheduled_at") or "")
    schedule_timezone = str(profile.get("schedule_timezone") or "Asia/Tokyo")
    desc_override = profile.get("description", "")
    chrome_profile = str(profile.get("chrome_profile") or getattr(config, "browser_chrome_profile", "Default") or "Default").strip()
    ad_suitability_template = normalize_ad_suitability_template(
        profile.get("ad_suitability_template") or getattr(config, "browser_ad_suitability_template", "")
    )

    auto_restart      = getattr(config, "browser_auto_restart", True)
    stall_timeout_min = int(getattr(config, "browser_stall_timeout_min", 10))
    op_speed          = getattr(config, "browser_op_speed", "normal")
    delays            = _Delays(op_speed)

    profile_name = profile.get("name", "")
    expected_channel_id = str(profile.get("youtube_channel_id") or "").strip()
    expected_channel_name = str(profile.get("youtube_channel_name") or profile_name or "").strip()
    on_log(f"上传方案: 「{profile_name}」  账号资料: {chrome_profile}  流程: {'精简' if flow == 'simple' else '完整'}")

    _MAX_BROWSER_RESTARTS = 3
    for _browser_attempt in range(1, _MAX_BROWSER_RESTARTS + 1):
        if job.is_cancelled():
            on_log("上传已取消")
            return None

        if _use_local_playwright_upload():
            # The profile cannot be opened by Chrome and Playwright at the
            # same time.  Stop a stale debug Chrome before opening the local
            # persistent context below; cookies and the signed-in account are
            # preserved in the profile directory.
            _kill_chrome(_chrome_debug_profile_dir(config, chrome_profile))
            time.sleep(1)
            on_log("使用本机上传浏览器（支持大于 50MB 的视频文件）...")
        elif force_profile_launch and _browser_attempt == 1:
            on_log(f"切换到账号资料「{chrome_profile}」...")
            if not _stop_debug_chrome_and_wait(_chrome_debug_profile_dir(config, chrome_profile), on_log):
                return None
            if not _launch_chrome(config, on_log, chrome_profile):
                on_log(f"❌ 无法启动账号资料「{chrome_profile}」")
                return None

        # Windows continues to use the established debug-Chrome workflow.
        # macOS/Linux use a local persistent Playwright context instead so
        # selecting a local video is not limited to 50 MB.
        if not _use_local_playwright_upload() and not _is_port_open(_DEBUG_PORT):
            if auto_restart and _browser_attempt == 1:
                on_log(f"⚠️ 未检测到 Chrome 调试端口 {_DEBUG_PORT}，尝试自动启动...")
                if not _launch_chrome(config, on_log, chrome_profile):
                    on_log("   macOS 可运行 scripts/start_chrome_debug_macos.command；Windows 可双击「Chrome调试模式启动.bat」。")
                    on_log("   登录 YouTube Studio 后重试。")
                    return None
            elif auto_restart and _browser_attempt > 1:
                on_log(f"⚠️ Chrome 端口消失（第 {_browser_attempt} 次），重启...")
                if not _restart_chrome(config, on_log, chrome_profile):
                    on_log("❌ 无法重启 Chrome，放弃")
                    return None
            else:
                on_log(f"❌ 未检测到 Chrome 调试端口 {_DEBUG_PORT}")
                on_log("   请用调试模式启动 Chrome（macOS 运行 scripts/start_chrome_debug_macos.command；Windows 双击「Chrome调试模式启动.bat」），")
                on_log("   打开 YouTube Studio 并登录后，再点击上传。")
                return None

        if job.is_cancelled():
            on_log("上传已取消")
            return None

        if not _use_local_playwright_upload():
            on_log(f"检测到 Chrome 调试端口，正在连接...")
        on_progress(22)

        result = _run_upload_session(
            job, config, video_path, title, cover_path,
            flow, ad_interval, ad_start, upload_policy, visibility,
            desc_override, delays, stall_timeout_min, ad_suitability_template,
            on_log, on_progress, chrome_profile, expected_channel_name, expected_channel_id,
            schedule_enabled, scheduled_at, schedule_timezone,
        )

        if job.is_cancelled():
            on_log("上传已取消")
            return None

        if result == _CHANNEL_GUARD_FAILED:
            # A channel mismatch/read failure is a safety decision, not a
            # transient upload or browser failure.  Retrying cannot make it
            # safer and previously caused three unnecessary Chrome restarts.
            return None
        if result == _STALL:
            on_log(f"⚠️ 上传卡住超过 {stall_timeout_min} 分钟（第 {_browser_attempt} 次），重启 Chrome...")
            if auto_restart:
                if _use_local_playwright_upload():
                    on_log("  将重新创建本机上传会话...")
                    time.sleep(3)
                else:
                    if not _restart_chrome(config, on_log, chrome_profile):
                        on_log("❌ 无法重启 Chrome，放弃")
                        return None
                    on_log("  等待 YouTube Studio 加载完成...")
                    time.sleep(10)
                continue
            else:
                on_log("❌ 上传超时，已禁用自动重启")
                return None
        elif result == "CHROME_CRASH":
            on_log(f"⚠️ Chrome 连接断开（第 {_browser_attempt} 次），重启...")
            if auto_restart:
                if _use_local_playwright_upload():
                    on_log("  将重新创建本机上传会话...")
                    time.sleep(3)
                else:
                    if not _restart_chrome(config, on_log, chrome_profile):
                        on_log("❌ 无法重启 Chrome，放弃")
                        return None
                    time.sleep(10)
                continue
            else:
                on_log("❌ Chrome 崩溃，已禁用自动重启")
                return None
        elif result:
            return result
        else:
            if _browser_attempt < _MAX_BROWSER_RESTARTS:
                on_log(f"  上传失败，等待重试（第 {_browser_attempt} 次）...")
                time.sleep(5)
            continue

    on_log(f"❌ 重启 {_MAX_BROWSER_RESTARTS} 次后仍失败")
    return None


def _run_upload_session(job, config, video_path, title, cover_path,
                        flow, ad_interval, ad_start, upload_policy, visibility,
                        desc_override, delays, stall_timeout_min, ad_suitability_template,
                        on_log, on_progress, chrome_profile, expected_channel_name, expected_channel_id,
                        schedule_enabled=False, scheduled_at="", schedule_timezone="Asia/Tokyo"):
    """
    单次 Playwright 会话：连接 Chrome，尝试上传，返回 video_id / None / _STALL / 'CHROME_CRASH'
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return None

    with sync_playwright() as pw:
        browser = None
        if _use_local_playwright_upload():
            # We are already inside sync_playwright().  Do not call
            # _find_chrome_exe() here: its fallback opens a second synchronous
            # Playwright manager, which is rejected while this one is active.
            chrome_path = Path(pw.chromium.executable_path)
            if not chrome_path.exists():
                on_log("❌ 未找到 Chrome 可执行文件")
                return "CHROME_CRASH"
            chrome = str(chrome_path)
            profile_dir = _chrome_debug_profile_dir(config, chrome_profile)
            try:
                on_log("启动本机 Chrome 上传会话...")
                ctx = pw.chromium.launch_persistent_context(
                    str(profile_dir), executable_path=chrome, headless=False,
                    args=["--no-first-run", "--no-default-browser-check"],
                    # The debug Chrome used for channel login stores Google
                    # cookies with the real macOS keychain.  Playwright's
                    # defaults switch to a mock/basic password store, making
                    # those same cookies unreadable and showing a fresh login
                    # page.  Keep the system keychain for this persistent
                    # account profile.
                    ignore_default_args=[
                        "--password-store=basic",
                        "--use-mock-keychain",
                    ],
                )
            except Exception as e:
                on_log(f"❌ 启动本机 Chrome 失败: {e}")
                return "CHROME_CRASH"
        else:
            on_log(f"连接到 Chrome (127.0.0.1:{_DEBUG_PORT})...")
            try:
                browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{_DEBUG_PORT}")
            except Exception as e:
                on_log(f"❌ 连接 Chrome 失败: {e}")
                return "CHROME_CRASH"
            ctx = browser.contexts[0] if browser.contexts else browser.new_context()

        upload_page = None
        try:
            # Keep the signed-in Studio tab and the Chrome process intact.
            # Each item gets its own temporary upload tab, which is closed
            # only after that item finishes (or fails).  The next item can
            # immediately open another tab in this already-running browser.
            upload_page = ctx.new_page()
            page = upload_page
            on_log("已打开本条视频的上传标签页（Chrome 和登录状态会继续保留）")
            _reset_youtube_studio_runtime_cache(ctx, page, on_log)

            _MAX_UPLOAD_RETRY = 3
            for _attempt in range(1, _MAX_UPLOAD_RETRY + 1):
                if job.is_cancelled():
                    on_log("上传已取消")
                    return None

                if _attempt > 1:
                    on_log(f"⚠ 上传流程第 {_attempt} 次重试，导航回 Studio 主页重新开始...")
                    _close_stale_file_dialogs()
                    try:
                        channel_m = re.search(r"studio\.youtube\.com/channel/([^/?]+)", page.url)
                        if channel_m:
                            page.goto(f"https://studio.youtube.com/channel/{channel_m.group(1)}", timeout=60_000)
                        else:
                            page.goto("https://studio.youtube.com", timeout=60_000)
                        page.wait_for_load_state("domcontentloaded", timeout=30_000)
                    except Exception as nav_e:
                        on_log(f"  导航失败: {nav_e}，继续尝试...")

                ban_check = getattr(job, "mode", "full") == "ban_check"
                try:
                    video_id = _do_upload(
                        page, job, config, video_path, title, cover_path,
                        ad_interval, ad_start, upload_policy, visibility, on_log, on_progress,
                        ban_check=ban_check, flow=flow, desc_override=desc_override,
                        delays=delays, stall_timeout_min=stall_timeout_min,
                        ad_suitability_template=ad_suitability_template,
                        expected_channel_name=expected_channel_name,
                        expected_channel_id=expected_channel_id,
                        schedule_enabled=schedule_enabled,
                        scheduled_at=scheduled_at,
                        schedule_timezone=schedule_timezone,
                    )
                except _UploadPolicyConfirmError as e:
                    on_log(f"  ⚠️ {e}，刷新浏览器后重新走上传流程...")
                    try:
                        page.reload(timeout=60_000, wait_until="domcontentloaded")
                        page.wait_for_timeout(3_000)
                    except Exception as refresh_e:
                        on_log(f"  刷新失败: {refresh_e}，下一轮将重新导航 Studio 首页")
                    video_id = None
                except Exception as e:
                    err = str(e)
                    if "Target page, context or browser has been closed" in err or \
                       "Connection closed" in err or "WebSocket" in err or \
                       "TargetClosedError" in err:
                        on_log(f"  ⚠️ Chrome 连接断开: {err[:80]}")
                        return "CHROME_CRASH"
                    on_log(f"  上传异常: {e}")
                    video_id = None

                if video_id == _STALL:
                    return _STALL
                if video_id == _CHANNEL_GUARD_FAILED:
                    return _CHANNEL_GUARD_FAILED
                if video_id:
                    return video_id
                if job.is_cancelled():
                    on_log("上传已取消")
                    return None
                if _attempt < _MAX_UPLOAD_RETRY:
                    on_log(f"  上传失败，稍后重试...")
                    time.sleep(3)

            on_log("❌ 上传重试 3 次均失败")
            return None
        finally:
            if upload_page is not None:
                try:
                    if not upload_page.is_closed():
                        upload_page.close()
                        on_log("本条上传标签页已关闭；Chrome 保持打开，准备下一条上传")
                except Exception:
                    pass
            # Never call browser.close() for a browser attached over CDP: it
            # sends Browser.close and kills the user's signed-in debug Chrome.
            # Leaving the sync_playwright block disconnects the client cleanly.
            if _use_local_playwright_upload():
                try:
                    ctx.close()
                except Exception:
                    pass


# ─────────────────────────────────────────────────────────────
# 上传主流程
# ─────────────────────────────────────────────────────────────

def _verify_studio_channel(page, expected_channel_name: str, expected_channel_id: str, on_log: Callable) -> bool:
    """Refuse uploads unless Studio matches the scheme's bound channel.

    A Chrome profile can contain several Google/brand channels.  The upload
    scheme name is the user's explicit channel binding, not merely a display
    label, so relying on the tab that Studio happened to restore is unsafe.
    """
    expected = " ".join(str(expected_channel_name or "").split())
    expected_id = str(expected_channel_id or "").strip()
    current_match = re.search(r"studio\.youtube\.com/channel/([^/?#]+)", page.url)
    actual_id = current_match.group(1).strip() if current_match else ""
    if expected_id:
        if actual_id != expected_id:
            on_log(
                f"❌ YouTube Studio 当前频道 ID 是「{actual_id or '无法读取'}」，"
                f"但频道方案绑定的是「{expected_id}」。为防止传错频道，本次上传已停止。"
            )
            return False
        on_log(f"✓ 已确认绑定的 YouTube 频道 ID：{expected_id}")
        return True
    if not expected:
        on_log("❌ 频道方案没有频道名称；为防止上传到错误频道，本次上传已停止。")
        return False
    selectors = (
        "ytcp-navigation-drawer #entity-name",
        "#entity-name",
        "ytcp-channel-switcher #channel-name",
        "ytcp-channel-switcher [id='channel-name']",
        "#channel-name",
        "ytcp-account-item #channel-title",
    )
    actual = ""
    for selector in selectors:
        try:
            locator = page.locator(selector).first
            if locator.is_visible(timeout=2_000):
                actual = " ".join(locator.inner_text(timeout=2_000).split())
                if actual:
                    break
        except Exception:
            continue
    if not actual:
        on_log(
            f"❌ 无法读取 YouTube Studio 当前频道，预期频道「{expected}」。"
            "为防止上传到错误频道，本次上传已停止；请打开该频道 Chrome 资料并确认已进入正确频道。"
        )
        return False
    if actual.casefold() != expected.casefold():
        on_log(
            f"❌ YouTube Studio 当前频道是「{actual}」，但所选频道方案要求「{expected}」。"
            "为防止传错频道，本次上传已停止。"
        )
        return False
    on_log(f"✓ 已确认目标 YouTube 频道：{actual}")
    return True


def _do_upload(page, job, config, video_path: Path, title: str,
               cover_path: Optional[Path],
               ad_interval: int, ad_start: int, upload_policy: str,
               visibility: str,
               on_log: Callable, on_progress: Callable,
               ban_check: bool = False,
               flow: str = "simple",
               desc_override: str | None = None,
               delays: "_Delays" = None,
               stall_timeout_min: int = 10,
               ad_suitability_template: dict | None = None,
               expected_channel_name: str = "",
               expected_channel_id: str = "",
               schedule_enabled: bool = False,
               scheduled_at: str = "",
               schedule_timezone: str = "Asia/Tokyo") -> Optional[str]:
    from playwright.sync_api import TimeoutError as PWTimeout

    if delays is None:
        delays = _Delays("normal")

    TIMEOUT = 60_000
    UPLOAD_TIMEOUT = 1_800_000  # 30 分钟
    LOGIN_WAIT = 300_000        # 5 分钟等待登录

    # An explicitly blank description is valid and must not fall back to a
    # bundled/default template during upload.
    desc = getattr(config, "youtube_description", "") if desc_override is None else desc_override
    upload_state = {
        "complete": False,
        "last_pct": -1,
        "last_activity": time.time(),
        "app_pct": 22,
    }

    def step_progress(pct: int):
        _emit_upload_progress(upload_state, pct, on_progress)

    # ── 步骤1：确认 Studio 已就绪 ─────────────────────────
    on_log("打开 YouTube Studio...")
    if expected_channel_id:
        clean_url = f"https://studio.youtube.com/channel/{expected_channel_id}"
        current_match = re.search(r"studio\.youtube\.com/channel/([^/?#]+)", page.url)
        if not current_match or current_match.group(1) != expected_channel_id:
            page.goto(clean_url, timeout=TIMEOUT)
    elif not re.search(r"studio\.youtube\.com/channel/[^/]+/?$", page.url):
        channel_m = re.search(r"studio\.youtube\.com/channel/([^/?]+)", page.url)
        if channel_m:
            clean_url = f"https://studio.youtube.com/channel/{channel_m.group(1)}"
        else:
            clean_url = "https://studio.youtube.com"
        page.goto(clean_url, timeout=TIMEOUT)
        try:
            page.wait_for_url(re.compile(r"studio\.youtube\.com/channel/"), timeout=LOGIN_WAIT)
        except PWTimeout:
            on_log(f"❌ 未能进入 Studio 主页，当前: {page.url}")
            return None
        cur = page.url
        if "accounts.google.com" in cur or "youtube.com/signin" in cur:
            on_log("检测到登录页面，请在浏览器中完成登录（最多 5 分钟）...")
            try:
                page.wait_for_url(re.compile(r"studio\.youtube\.com/channel/"), timeout=LOGIN_WAIT)
            except PWTimeout:
                on_log("❌ 登录超时")
                return None

    ctx = page.context
    for p in ctx.pages:
        channel_match = re.search(r"studio\.youtube\.com/channel/([^/?#]+)", p.url)
        if channel_match and p != page and (
            not expected_channel_id or channel_match.group(1) == expected_channel_id
        ):
            on_log("  切换到 Studio 活跃 tab")
            page = p
            break

    page.wait_for_load_state("domcontentloaded", timeout=TIMEOUT)
    on_log(f"✓ Studio 已就绪: {page.url}")
    if not _verify_studio_channel(page, expected_channel_name, expected_channel_id, on_log):
        return _CHANNEL_GUARD_FAILED

    # ── 步骤2：点击"创建"→"上传视频" ─────────────────────
    if job.is_cancelled():
        on_log("上传已取消")
        return None
    on_log("点击创建按钮...")
    try:
        page.click('button[aria-label="创建"]', timeout=TIMEOUT)
    except PWTimeout:
        try:
            page.click('button[aria-label="Create"]', timeout=10_000)
        except PWTimeout:
            on_log("❌ 未找到创建按钮")
            return None
    page.wait_for_timeout(delays.click_after)

    try:
        page.click('tp-yt-paper-item:has-text("上传视频")', timeout=8_000)
    except PWTimeout:
        try:
            page.click('tp-yt-paper-item:has-text("Upload videos")', timeout=8_000)
        except PWTimeout:
            on_log("❌ 未找到上传视频菜单项")
            return None
    page.wait_for_timeout(delays.step_between)
    step_progress(25)

    # ── 步骤3：选择文件 ────────────────────────────────────
    if job.is_cancelled():
        on_log("上传已取消")
        return None
    on_log(f"选择视频文件: {video_path.name}")
    if not _select_file_via_dialog(page, video_path, on_log, TIMEOUT):
        on_log("❌ 文件对话框未识别，触发整体重试...")
        return None
    step_progress(28)

    # ── 步骤4：填写标题 ────────────────────────────────────
    if job.is_cancelled():
        on_log("上传已取消")
        return None
    if not ban_check:
        on_log("填写标题...")
        title_inner = page.locator(
            'ytcp-video-metadata-editor #title-textarea #textbox, '
            'ytcp-video-metadata-editor ytcp-social-suggestions-textbox #textbox'
        ).first
        title_inner.wait_for(timeout=10_000)
        title_inner.click()
        page.evaluate(
            "el => { el.textContent = ''; el.dispatchEvent(new Event('input', {bubbles:true})); }",
            title_inner.element_handle()
        )
        title_inner.type(title, delay=delays.type_delay)
        page.wait_for_timeout(300)

        if desc:
            desc_box = page.locator(
                'ytcp-video-metadata-editor #description-container #textbox, '
                'ytcp-video-metadata-editor #description-textarea #textbox'
            ).first
            try:
                desc_box.wait_for(timeout=5_000)
                desc_box.click()
                page.evaluate(
                    "el => { el.textContent = ''; el.dispatchEvent(new Event('input', {bubbles:true})); }",
                    desc_box.element_handle()
                )
                desc_box.type(desc, delay=5)
            except PWTimeout:
                pass

    step_progress(32)

    # ── 步骤5：上传封面 ────────────────────────────────────
    if job.is_cancelled():
        on_log("上传已取消")
        return None
    if not ban_check and cover_path and cover_path.exists():
        on_log("上传封面...")
        _upload_cover(page, cover_path, on_log)

    step_progress(35)

    # ── 步骤6-10：根据 flow 和 ban_check 决定中间步骤 ─────
    if job.is_cancelled():
        on_log("上传已取消")
        return None
    if ban_check:
        on_log("[查禁播] 跳过标题/封面/版权/广告/分级，直接跳到公开范围...")
        step_progress(50)
        _next_clicks = 5
    elif flow == "simple":
        on_log("精简流程：跳过创收/广告位/分级，直接跳到公开范围...")
        step_progress(50)
        _next_clicks = 5
    else:
        on_log("继续 → 权利管理...")
        _click_next(page, TIMEOUT)
        page.wait_for_timeout(delays.step_between)
        step_progress(38)

        if job.is_cancelled():
            on_log("上传已取消")
            return None
        on_log(f"设置上传政策（{upload_policy}）...")
        _set_upload_policy(page, upload_policy, on_log, TIMEOUT)
        step_progress(45)

        if job.is_cancelled():
            on_log("上传已取消")
            return None
        on_log("继续 → 创收页...")
        _click_next(page, TIMEOUT)
        page.wait_for_timeout(delays.step_between + 200)
        step_progress(50)

        if job.is_cancelled():
            on_log("上传已取消")
            return None
        on_log("插入中贴片广告位...")
        _insert_ad_breaks(page, ad_interval, ad_start, on_log, TIMEOUT, job=job)
        step_progress(60)

        if job.is_cancelled():
            on_log("上传已取消")
            return None
        on_log("提交广告适合性分级...")
        on_log(f"  当前 URL: {page.url}")
        page.wait_for_timeout(delays.step_between + 500)
        _submit_ad_suitability(page, on_log, TIMEOUT, ad_suitability_template)
        page.wait_for_timeout(delays.click_after)
        _next_clicks = 3

    # ── 步骤11：跳到公开范围页 ────────────────────────────
    if job.is_cancelled():
        on_log("上传已取消")
        return None
    on_log("继续 → 公开范围...")
    for _ in range(_next_clicks):
        if _is_upload_visibility_page(page):
            break
        try:
            _click_next(page, 8_000)
            page.wait_for_timeout(delays.click_after)
            if _is_upload_visibility_page(page):
                break
        except Exception:
            break

    step_progress(70)

    # 公开范围页已经出现时先选好目标范围，不等上传 100%。
    # 定时发布严格按 Studio 当前页面流程执行：填写日期/时间后立即
    # 点击预定，处理“知道了”，并以“已安排好视频发布时间”为成功依据。
    if schedule_enabled:
        if not _select_upload_schedule(page, scheduled_at, schedule_timezone, on_log):
            return None
        video_id = _schedule_upload_immediately(
            page, job, scheduled_at, on_log, on_progress, UPLOAD_TIMEOUT,
            upload_state=upload_state,
        )
    else:
        _select_upload_visibility(page, visibility, on_log)
        # ── 步骤12：等待上传完成并发布 ────────────────────
        if job.is_cancelled():
            on_log("上传已取消")
            return None
        on_log("等待视频上传完成并发布...")
        video_id = _wait_for_upload_and_publish(
            page, job, visibility, on_log, on_progress, UPLOAD_TIMEOUT,
            stall_timeout_min=stall_timeout_min,
            upload_state=upload_state,
            schedule_enabled=False,
        )

    if video_id is None:
        on_log("❌ 上传失败或被取消")
        return None

    if video_id == _STALL:
        return _STALL

    done_label = "预定" if schedule_enabled else ("发布" if visibility == "PUBLIC" else "保存")
    on_log(f"✓ 视频已{done_label}，video_id: {video_id}")
    step_progress(100)

    return video_id


# ─────────────────────────────────────────────────────────────
# 封面上传
# ─────────────────────────────────────────────────────────────

def _upload_cover(page, cover_path: Path, on_log: Callable):
    from playwright.sync_api import TimeoutError as PWTimeout
    try:
        # 等封面上传区域可见，找其中 visible 的按钮
        page.wait_for_selector('ytcp-thumbnail-uploader', timeout=10_000)
        with page.expect_file_chooser(timeout=10_000) as fc:
            # 用 JS 点击第一个可见的 select-button，避免 Playwright 误选隐藏元素
            page.evaluate('''() => {
                const btns = document.querySelectorAll("ytcp-thumbnail-editor button#select-button");
                for (const b of btns) {
                    if (b.offsetParent !== null) { b.click(); return; }
                }
                // fallback: 点第一个
                if (btns.length) btns[0].click();
            }''')
        fc.value.set_files(str(cover_path))
        page.wait_for_timeout(2000)
        on_log("  ✓ 封面已上传")
    except Exception as e:
        on_log(f"  ⚠️ 封面上传失败（继续）: {e}")


# ─────────────────────────────────────────────────────────────
# 权利管理：选择 BTRA 上传政策
# ─────────────────────────────────────────────────────────────

def _set_upload_policy(page, policy_name: str, on_log: Callable, timeout: int):
    from playwright.sync_api import TimeoutError as PWTimeout

    # 等待权利管理页面出现（特征：ytcp-form-select#metadata-asset-select）
    try:
        page.wait_for_selector('ytcp-form-select#metadata-asset-select', timeout=timeout)
    except PWTimeout:
        on_log("  ⚠️ 未找到权利管理页，跳过政策设置")
        return

    last_state = ""
    for attempt in range(2):
        try:
            # 点开"上传政策"下拉（专属 trigger id: policy-select-trigger）
            _click_upload_policy_dropdown(page, timeout=10_000)
            page.wait_for_timeout(800)

            # 等待选项列表渲染。调试 Chrome 的旧缓存有时会让 Studio 先画出空白面板。
            page.wait_for_function("""policyName => {
                const wanted = String(policyName || '').trim().toLowerCase();
                function visible(el) {
                    if (!el) return false;
                    const style = window.getComputedStyle(el);
                    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
                    const rect = el.getBoundingClientRect();
                    return rect.width > 0 && rect.height > 0;
                }
                function textOf(el) {
                    return (el && el.textContent || '').trim().replace(/\s+/g, ' ');
                }
                function policyRootFor(el) {
                    let node = el;
                    while (node && node !== document.body) {
                        const tag = (node.tagName || '').toLowerCase();
                        const id = node.id || '';
                        const cls = String(node.className || '');
                        const text = textOf(node);
                        if (
                            tag === 'ytcms-policy-select-menu' ||
                            id === 'policy-content-container' ||
                            cls.includes('policy-list') ||
                            (tag === 'ytcp-list' && text.includes('BTRA') && text.includes('Track in all countries'))
                        ) {
                            return node;
                        }
                        node = node.parentElement;
                    }
                    return null;
                }
                return Array.from(document.querySelectorAll('tp-yt-paper-item, [role="option"]'))
                    .some(el => textOf(el).toLowerCase() === wanted && visible(el) && policyRootFor(el));
            }""", arg=policy_name, timeout=20_000)
            _click_upload_policy_option(page, policy_name, timeout=8_000)
            page.wait_for_timeout(400)
            # 点击"应用"按钮确认
            _click_apply_upload_policy(page, timeout=5_000)
            page.wait_for_timeout(700)

            if _wait_for_upload_policy_selection(page, policy_name, timeout=6_000):
                suffix = "" if attempt == 0 else "（重试后确认）"
                on_log(f"  ✓ 已选择上传政策: {policy_name}{suffix}")
                return

            last_state = _current_upload_policy_text(page) or _describe_upload_policy_state(page)
            if attempt == 0:
                on_log(f"  ⚠️ 上传政策未确认到「{policy_name}」（当前: {last_state or '未知'}），重新选择一次...")
                continue
        except PWTimeout:
            if _upload_policy_panel_looks_empty(page):
                msg = "上传政策列表为空：调试 Chrome 的 YouTube Studio 前端缓存可能异常，请关闭调试 Chrome 后重试"
                on_log(f"  ❌ {msg}")
                raise _UploadPolicyConfirmError(msg)
            last_state = _describe_upload_policy_state(page)
            if attempt == 0:
                on_log(f"  ⚠️ 上传政策选择未完成（当前列表: {last_state or '未知'}），重新选择一次...")
                continue
            if last_state:
                msg = f"未找到政策「{policy_name}」，当前列表: {last_state}"
            else:
                msg = f"未找到政策「{policy_name}」"
            on_log(f"  ❌ {msg}，停止进入创收页")
            raise _UploadPolicyConfirmError(msg)

    last_state = last_state or _current_upload_policy_text(page) or "未知"
    msg = f"上传政策未确认到「{policy_name}」，当前为「{last_state}」，停止进入创收页"
    on_log(f"  ❌ {msg}")
    raise _UploadPolicyConfirmError(msg)


def _current_upload_policy_text(page) -> str:
    try:
        value = page.evaluate("""() => {
            const norm = s => (s || '').trim().replace(/\s+/g, ' ');
            const roots = [
                document.querySelector('ytcms-policy-select'),
                document.querySelector('ytcp-text-dropdown-trigger#policy-select-trigger'),
                document.querySelector('#policy-select-trigger'),
            ].filter(Boolean);
            for (const root of roots) {
                const direct = root.querySelector('.dropdown-trigger-text, span.dropdown-trigger-text');
                const text = norm(direct && direct.textContent);
                if (text) return text;
                const fallback = norm(root.textContent);
                if (fallback) return fallback;
            }
            const candidates = Array.from(document.querySelectorAll('ytcp-text-dropdown-trigger, ytcp-dropdown-trigger'));
            for (const trigger of candidates) {
                const text = norm(trigger.querySelector('.dropdown-trigger-text')?.textContent || trigger.textContent);
                if (
                    text === 'Track in all countries' ||
                    text.includes('BTRA') ||
                    text.includes('Monetize in all countries') ||
                    text.includes('Block in all countries')
                ) {
                    return text;
                }
            }
            return '';
        }""")
        return str(value or "").strip()
    except Exception:
        return ""


def _wait_for_upload_policy_selection(page, policy_name: str, timeout: int = 6_000) -> bool:
    try:
        page.wait_for_function("""policyName => {
            const wanted = String(policyName || '').trim().toLowerCase();
            const norm = s => (s || '').trim().replace(/\s+/g, ' ');
            const matches = text => {
                const value = norm(text).toLowerCase();
                return value === wanted || value.includes(wanted);
            };
            const roots = [
                document.querySelector('ytcms-policy-select'),
                document.querySelector('ytcp-text-dropdown-trigger#policy-select-trigger'),
                document.querySelector('#policy-select-trigger'),
            ].filter(Boolean);
            for (const root of roots) {
                const direct = root.querySelector('.dropdown-trigger-text, span.dropdown-trigger-text');
                if (matches(direct && direct.textContent)) return true;
                if (matches(root.textContent)) return true;
            }
            return Array.from(document.querySelectorAll('ytcp-text-dropdown-trigger, ytcp-dropdown-trigger'))
                .some(trigger => matches(trigger.querySelector('.dropdown-trigger-text')?.textContent || trigger.textContent));
        }""", arg=policy_name, timeout=timeout)
        return True
    except Exception:
        return False


def _upload_policy_panel_looks_empty(page) -> bool:
    try:
        return bool(page.evaluate("""() => {
            const panel = document.querySelector('#policy-content-container, ytcp-list, tp-yt-paper-listbox');
            if (!panel) return false;
            const items = Array.from(document.querySelectorAll(
                '#policy-content-container tp-yt-paper-item, #items tp-yt-paper-item, ytcp-list tp-yt-paper-item, tp-yt-paper-item[role="option"], tp-yt-paper-item'
            )).filter(el => {
                const style = window.getComputedStyle(el);
                const rect = el.getBoundingClientRect();
                return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
            });
            return items.length === 0;
        }"""))
    except Exception:
        return False


def _describe_upload_policy_state(page) -> str:
    try:
        state = page.evaluate("""() => {
            const norm = s => (s || '').trim().replace(/\s+/g, ' ');
            function textOf(el) { return norm(el && el.textContent || ''); }
            function policyRootFor(el) {
                let node = el;
                while (node && node !== document.body) {
                    const tag = (node.tagName || '').toLowerCase();
                    const id = node.id || '';
                    const cls = String(node.className || '');
                    const text = textOf(node);
                    if (
                        tag === 'ytcms-policy-select-menu' ||
                        id === 'policy-content-container' ||
                        cls.includes('policy-list') ||
                        (tag === 'ytcp-list' && text.includes('BTRA') && text.includes('Track in all countries'))
                    ) {
                        return node;
                    }
                    node = node.parentElement;
                }
                return null;
            }
            const btra = Array.from(document.querySelectorAll('tp-yt-paper-item, [role="option"]'))
                .find(el => textOf(el) === 'BTRA' && policyRootFor(el));
            const root = btra && policyRootFor(btra);
            const rowSource = root ? root.querySelectorAll('tp-yt-paper-item, [role="option"]') : [];
            const rows = Array.from(rowSource).map(el => ({
                id: el.id || '',
                text: norm(el.textContent),
                selected: el.hasAttribute('selected'),
                ariaDisabled: el.getAttribute('aria-disabled')
            })).filter(row => row.text);
            const current = norm(document.querySelector('ytcms-policy-select')?.textContent || '');
            return {current, rows};
        }""")
        if not isinstance(state, dict):
            return ""
        current = str(state.get("current") or "").strip()
        rows = state.get("rows") or []
        parts = []
        for row in rows[:8]:
            if not isinstance(row, dict):
                continue
            item = f"{row.get('id') or '?'}={row.get('text') or ''}"
            if row.get("selected"):
                item += "[selected]"
            parts.append(item)
        summary = "; ".join(parts)
        if current:
            return f"current={current}; {summary}"
        return summary
    except Exception:
        return ""


def _click_upload_policy_dropdown(page, timeout: int = 10_000):
    """Open the upload-policy dropdown across YouTube Studio DOM variants."""
    from playwright.sync_api import TimeoutError as PWTimeout

    selectors = [
        'ytcms-policy-select ytcp-dropdown-trigger[role="button"]',
        'ytcms-policy-select ytcp-text-dropdown-trigger',
        'ytcms-policy-select',
        'ytcp-text-dropdown-trigger#policy-select-trigger',
    ]
    last_exc = None
    selector_timeout = min(timeout, 3_000)
    for selector in selectors:
        try:
            page.click(selector, timeout=selector_timeout)
            return
        except PWTimeout as exc:
            last_exc = exc

    try:
        clicked = page.evaluate("""() => {
            function visible(el) {
                if (!el) return false;
                const style = window.getComputedStyle(el);
                if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
                const rect = el.getBoundingClientRect();
                return rect.width > 0 && rect.height > 0;
            }
            function fireClick(el) {
                el.scrollIntoView({block: 'center', inline: 'center'});
                for (const type of ['pointerdown', 'mousedown', 'mouseup', 'click']) {
                    el.dispatchEvent(new MouseEvent(type, {bubbles: true, composed: true, view: window}));
                }
            }
            function innerButton(host) {
                if (!host) return null;
                const roots = [host, host.shadowRoot].filter(Boolean);
                for (const root of roots) {
                    const btn = root.querySelector('[role="button"], button, .container, .left-container, .dropdown-trigger-text');
                    if (btn && visible(btn)) return btn;
                }
                return visible(host) ? host : null;
            }

            const exact = document.querySelector(
                'ytcms-policy-select ytcp-dropdown-trigger, ' +
                'ytcms-policy-select ytcp-text-dropdown-trigger, ' +
                'ytcms-policy-select, ' +
                'ytcp-text-dropdown-trigger#policy-select-trigger, ' +
                '#policy-select-trigger'
            );
            const exactBtn = innerButton(exact);
            if (exactBtn) {
                fireClick(exactBtn);
                return true;
            }

            const policyHints = ['上传政策', 'Upload policy', 'BTRA', 'Track in all countries', 'Monetize in all countries', 'Block in all countries'];
            const triggers = Array.from(document.querySelectorAll('ytcms-policy-select, ytcp-text-dropdown-trigger, tp-yt-paper-dropdown-menu'));
            for (const trigger of triggers) {
                const text = (trigger.textContent || '').trim();
                if (trigger.matches('ytcms-policy-select') || policyHints.some(hint => text.includes(hint))) {
                    const btn = innerButton(trigger);
                    if (btn) {
                        fireClick(btn);
                        return true;
                    }
                }
            }
            return false;
        }""")
        if clicked:
            return
        raise last_exc or PWTimeout("Upload policy dropdown not found")
    except PWTimeout:
        raise


def _click_upload_policy_option(page, policy_name: str, timeout: int = 8_000):
    """Select the named upload policy from either the old or current policy list."""
    from playwright.sync_api import TimeoutError as PWTimeout

    def selection_matches() -> bool:
        try:
            return bool(page.evaluate("""policyName => {
                const wanted = String(policyName || '').trim().toLowerCase();
                const norm = s => (s || '').trim().replace(/\s+/g, ' ').toLowerCase();
                function textOf(el) { return (el && el.textContent || '').trim().replace(/\s+/g, ' '); }
                function policyRootFor(el) {
                    let node = el;
                    while (node && node !== document.body) {
                        const tag = (node.tagName || '').toLowerCase();
                        const id = node.id || '';
                        const cls = String(node.className || '');
                        const text = textOf(node);
                        if (
                            tag === 'ytcms-policy-select-menu' ||
                            id === 'policy-content-container' ||
                            cls.includes('policy-list') ||
                            (tag === 'ytcp-list' && text.includes('BTRA') && text.includes('Track in all countries'))
                        ) {
                            return node;
                        }
                        node = node.parentElement;
                    }
                    return null;
                }
                const btra = Array.from(document.querySelectorAll('tp-yt-paper-item, [role="option"]'))
                    .find(el => textOf(el).toLowerCase() === wanted && policyRootFor(el));
                const root = btra && policyRootFor(btra);
                const selected = root && Array.from(root.querySelectorAll('tp-yt-paper-item[selected], [role="option"][selected]'))
                    .find(el => norm(el.textContent) === wanted || norm(el.textContent).includes(wanted));
                if (selected) return true;
                const current = norm(document.querySelector('ytcms-policy-select')?.textContent || '');
                return current === wanted || current.includes(wanted);
            }""", policy_name))
        except Exception:
            return False

    try:
        selected = page.evaluate("""policyName => {
            const wanted = String(policyName || '').trim().toLowerCase();
            function visible(el) {
                if (!el) return false;
                const style = window.getComputedStyle(el);
                if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
                const rect = el.getBoundingClientRect();
                return rect.width > 0 && rect.height > 0;
            }
            function textOf(el) {
                return (el && el.textContent || '').trim().replace(/\s+/g, ' ');
            }
            function policyRootFor(el) {
                let node = el;
                while (node && node !== document.body) {
                    const tag = (node.tagName || '').toLowerCase();
                    const id = node.id || '';
                    const cls = String(node.className || '');
                    const text = textOf(node);
                    if (
                        tag === 'ytcms-policy-select-menu' ||
                        id === 'policy-content-container' ||
                        cls.includes('policy-list') ||
                        (tag === 'ytcp-list' && text.includes('BTRA') && text.includes('Track in all countries'))
                    ) {
                        return node;
                    }
                    node = node.parentElement;
                }
                return null;
            }
            function selectedText() {
                const btra = Array.from(document.querySelectorAll('tp-yt-paper-item, [role="option"]'))
                    .find(el => textOf(el).toLowerCase() === wanted && visible(el) && policyRootFor(el));
                const root = btra && policyRootFor(btra);
                const selected = root && Array.from(root.querySelectorAll('tp-yt-paper-item[selected], [role="option"][selected]')).find(visible);
                return textOf(selected);
            }
            function fireClick(el) {
                el.scrollIntoView({block: 'center', inline: 'center'});
                el.focus && el.focus();
                const rect = el.getBoundingClientRect();
                const x = rect.left + rect.width / 2;
                const y = rect.top + rect.height / 2;
                if (window.PointerEvent) {
                    for (const type of ['pointerover', 'pointermove', 'pointerdown', 'pointerup']) {
                        el.dispatchEvent(new PointerEvent(type, {
                            bubbles: true, composed: true, view: window,
                            clientX: x, clientY: y, pointerId: 1, pointerType: 'mouse', isPrimary: true
                        }));
                    }
                }
                for (const type of ['mouseover', 'mousemove', 'mousedown', 'mouseup', 'click']) {
                    el.dispatchEvent(new MouseEvent(type, {bubbles: true, composed: true, view: window, clientX: x, clientY: y}));
                }
                el.click && el.click();
                el.dispatchEvent(new CustomEvent('tap', {bubbles: true, composed: true}));
            }
            const btra = Array.from(document.querySelectorAll('tp-yt-paper-item, [role="option"]'))
                .find(el => textOf(el).toLowerCase() === wanted && visible(el) && policyRootFor(el));
            const root = btra && policyRootFor(btra);
            const itemSource = root ? root.querySelectorAll('tp-yt-paper-item, [role="option"]') : [];
            const items = Array.from(itemSource).filter(visible).map((el, index) => {
                const text = textOf(el);
                const rect = el.getBoundingClientRect();
                return {
                    el,
                    id: el.id || '',
                    text,
                    exact: text.toLowerCase() === wanted,
                    contains: text.toLowerCase().includes(wanted),
                    index,
                    x: rect.left + rect.width / 2,
                    y: rect.top + rect.height / 2
                };
            }).filter(item => item.exact || item.contains);
            items.sort((a, b) => Number(b.exact) - Number(a.exact));
            const target = items[0];
            if (!target) return {selected: selectedText(), found: false};
            fireClick(target.el);
            return {
                found: true,
                selected: selectedText(),
                id: target.id,
                targetText: target.text,
                x: target.x,
                y: target.y
            };
        }""", policy_name)
        if isinstance(selected, dict) and str(selected.get("selected", "")).strip().lower() == policy_name.strip().lower():
            return
        if isinstance(selected, dict) and selected.get("id"):
            escaped_id = str(selected["id"]).replace("\\", "\\\\").replace('"', '\\"')
            direct_selectors = [
                f'ytcms-policy-select-menu tp-yt-paper-item#{escaped_id}',
                f'#policy-content-container tp-yt-paper-item#{escaped_id}',
                f'ytcp-list tp-yt-paper-item#{escaped_id}',
            ]
            for selector in direct_selectors:
                try:
                    page.click(selector, timeout=1_000, force=True)
                    page.wait_for_timeout(250)
                    if selection_matches():
                        return
                except Exception:
                    pass
        if isinstance(selected, dict) and selected.get("x") is not None and selected.get("y") is not None:
            x = float(selected["x"])
            y = float(selected["y"])
            page.mouse.move(x, y)
            page.mouse.down()
            page.wait_for_timeout(80)
            page.mouse.up()
            page.wait_for_timeout(250)
            if selection_matches():
                return
        forced = page.evaluate("""policyName => {
            const wanted = String(policyName || '').trim().toLowerCase();
            function visible(el) {
                if (!el) return false;
                const style = window.getComputedStyle(el);
                const rect = el.getBoundingClientRect();
                return style.display !== 'none' && style.visibility !== 'hidden' && style.opacity !== '0' && rect.width > 0 && rect.height > 0;
            }
            function textOf(el) {
                return (el && el.textContent || '').trim().replace(/\s+/g, ' ');
            }
            function policyRootFor(el) {
                let node = el;
                while (node && node !== document.body) {
                    const tag = (node.tagName || '').toLowerCase();
                    const id = node.id || '';
                    const cls = String(node.className || '');
                    const text = textOf(node);
                    if (
                        tag === 'ytcms-policy-select-menu' ||
                        id === 'policy-content-container' ||
                        cls.includes('policy-list') ||
                        (tag === 'ytcp-list' && text.includes('BTRA') && text.includes('Track in all countries'))
                    ) {
                        return node;
                    }
                    node = node.parentElement;
                }
                return null;
            }
            function fireClick(el) {
                el.scrollIntoView({block: 'center', inline: 'center'});
                el.focus && el.focus();
                const rect = el.getBoundingClientRect();
                const x = rect.left + rect.width / 2;
                const y = rect.top + rect.height / 2;
                if (window.PointerEvent) {
                    for (const type of ['pointerover', 'pointermove', 'pointerdown', 'pointerup']) {
                        el.dispatchEvent(new PointerEvent(type, {
                            bubbles: true, composed: true, view: window,
                            clientX: x, clientY: y, pointerId: 1, pointerType: 'mouse', isPrimary: true
                        }));
                    }
                }
                for (const type of ['mouseover', 'mousemove', 'mousedown', 'mouseup', 'click']) {
                    el.dispatchEvent(new MouseEvent(type, {bubbles: true, composed: true, view: window, clientX: x, clientY: y}));
                }
                el.click && el.click();
                el.dispatchEvent(new CustomEvent('tap', {bubbles: true, composed: true}));
                el.dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', code: 'Enter', bubbles: true, composed: true}));
                el.dispatchEvent(new KeyboardEvent('keyup', {key: 'Enter', code: 'Enter', bubbles: true, composed: true}));
            }
            const btra = Array.from(document.querySelectorAll('tp-yt-paper-item, [role="option"]'))
                .find(el => textOf(el).toLowerCase() === wanted && visible(el) && policyRootFor(el));
            const root = btra && policyRootFor(btra);
            const item = root && Array.from(root.querySelectorAll('tp-yt-paper-item, [role="option"]')).filter(visible).find(el => textOf(el).toLowerCase() === wanted);
            if (!item) return false;
            fireClick(item);
            const selected = Array.from(root.querySelectorAll('tp-yt-paper-item[selected], [role="option"][selected]')).filter(visible).find(el => textOf(el).toLowerCase() === wanted);
            return !!selected;
        }""", policy_name)
        if forced or selection_matches():
            return
    except Exception:
        pass

    escaped = policy_name.replace('"', '\\"')
    selectors = [
        f'ytcms-policy-select-menu tp-yt-paper-item:has-text("{escaped}")',
        f'ytcms-policy-select-menu [role="option"]:has-text("{escaped}")',
        f'#policy-content-container tp-yt-paper-item:has-text("{escaped}")',
        f'ytcp-list tp-yt-paper-item:has-text("{escaped}")',
    ]
    last_exc = None
    selector_timeout = min(timeout, 3_000)
    for selector in selectors:
        try:
            page.click(selector, timeout=selector_timeout)
            return
        except PWTimeout as exc:
            last_exc = exc
    try:
        clicked = page.evaluate("""policyName => {
            const wanted = String(policyName || '').trim().toLowerCase();
            function visible(el) {
                if (!el) return false;
                const style = window.getComputedStyle(el);
                if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
                const rect = el.getBoundingClientRect();
                return rect.width > 0 && rect.height > 0;
            }
            function fireClick(el) {
                el.scrollIntoView({block: 'center', inline: 'center'});
                el.focus && el.focus();
                const rect = el.getBoundingClientRect();
                const x = rect.left + rect.width / 2;
                const y = rect.top + rect.height / 2;
                if (window.PointerEvent) {
                    for (const type of ['pointerover', 'pointermove', 'pointerdown', 'pointerup']) {
                        el.dispatchEvent(new PointerEvent(type, {
                            bubbles: true, composed: true, view: window,
                            clientX: x, clientY: y, pointerId: 1, pointerType: 'mouse', isPrimary: true
                        }));
                    }
                }
                for (const type of ['mouseover', 'mousemove', 'mousedown', 'mouseup', 'click']) {
                    el.dispatchEvent(new MouseEvent(type, {bubbles: true, composed: true, view: window, clientX: x, clientY: y}));
                }
                el.click && el.click();
                el.dispatchEvent(new CustomEvent('tap', {bubbles: true, composed: true}));
            }
            function textOf(el) {
                return (el && el.textContent || '').trim().replace(/\s+/g, ' ');
            }
            function policyRootFor(el) {
                let node = el;
                while (node && node !== document.body) {
                    const tag = (node.tagName || '').toLowerCase();
                    const id = node.id || '';
                    const cls = String(node.className || '');
                    const text = textOf(node);
                    if (
                        tag === 'ytcms-policy-select-menu' ||
                        id === 'policy-content-container' ||
                        cls.includes('policy-list') ||
                        (tag === 'ytcp-list' && text.includes('BTRA') && text.includes('Track in all countries'))
                    ) {
                        return node;
                    }
                    node = node.parentElement;
                }
                return null;
            }
            const btra = Array.from(document.querySelectorAll('tp-yt-paper-item, [role="option"]'))
                .find(el => textOf(el).toLowerCase() === wanted && visible(el) && policyRootFor(el));
            const root = btra && policyRootFor(btra);
            const items = root ? Array.from(root.querySelectorAll('tp-yt-paper-item, [role="option"]')).filter(visible) : [];
            for (const item of items) {
                const text = textOf(item);
                if (text.toLowerCase() === wanted || text.toLowerCase().includes(wanted)) {
                    fireClick(item);
                    return true;
                }
            }
            return false;
        }""", policy_name)
        if clicked:
            return
        raise last_exc or PWTimeout(f"Policy option not found: {policy_name}")
    except PWTimeout:
        raise


def _click_apply_upload_policy(page, timeout: int = 5_000):
    from playwright.sync_api import TimeoutError as PWTimeout

    try:
        page.click('ytcms-policy-select-menu ytcp-button#apply-policy', timeout=timeout)
        return
    except PWTimeout:
        pass

    try:
        page.click('ytcp-button#apply-policy', timeout=timeout)
        return
    except PWTimeout:
        clicked = page.evaluate("""() => {
            function visible(el) {
                if (!el) return false;
                const style = window.getComputedStyle(el);
                if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
                const rect = el.getBoundingClientRect();
                return rect.width > 0 && rect.height > 0;
            }
            function fireClick(el) {
                el.scrollIntoView({block: 'center', inline: 'center'});
                for (const type of ['pointerdown', 'mousedown', 'mouseup', 'click']) {
                    el.dispatchEvent(new MouseEvent(type, {bubbles: true, composed: true, view: window}));
                }
            }
            const candidates = Array.from(document.querySelectorAll('ytcp-button#apply-policy, ytcp-button'));
            for (const btn of candidates) {
                const text = (btn.textContent || '').trim();
                if (btn.id === 'apply-policy' || text === '应用' || text === 'Apply') {
                    const inner = (btn.shadowRoot && (btn.shadowRoot.querySelector('button') || btn.shadowRoot.querySelector('[role="button"]'))) ||
                        btn.querySelector('button, [role="button"]') ||
                        btn;
                    if (visible(inner)) {
                        fireClick(inner);
                        return true;
                    }
                }
            }
            return false;
        }""")
        if clicked:
            return
        raise


# ─────────────────────────────────────────────────────────────
# 创收：插入中贴片广告位
# ─────────────────────────────────────────────────────────────

def _insert_ad_breaks(page, interval: int, start: int, on_log: Callable, timeout: int, job=None):
    from playwright.sync_api import TimeoutError as PWTimeout

    # 等待创收页出现
    try:
        page.wait_for_selector('ytcp-uploads-monetization', timeout=timeout)
    except PWTimeout:
        on_log("  ⚠️ 未找到创收页，跳过广告位设置")
        return

    # 点击"管理广告位"按钮进入广告位编辑页面
    try:
        page.click('ytcp-button#place-manually-button', timeout=10_000)
        page.wait_for_timeout(1000)
        on_log(f"  进入广告位页面，开始自动插入（间隔={interval}s，起始={start}s）...")
    except PWTimeout:
        on_log("  ⚠️ 未找到管理广告位按钮，跳过广告位插入")
        return

    # ── 读取视频真实时长 ────────────────────────────────────
    # 广告位编辑页包含隐藏的 <video> 元素，duration 属性直接给出精确秒数
    # 最多等 8 秒让视频元数据加载完
    duration = None
    for _ in range(16):
        duration = page.evaluate("""() => {
            const v = document.querySelector('video');
            if (!v) return null;
            const d = v.duration;
            return (d && isFinite(d) && d > 10) ? Math.floor(d) : null;
        }""")
        if duration:
            break
        page.wait_for_timeout(500)

    if duration:
        on_log(f"  视频时长: {duration}s ({duration//3600}:{(duration%3600)//60:02d}:{duration%60:02d})")
        # 最后一个广告位至少距结尾 60 秒
        stop_at = duration - 60
    else:
        on_log("  ⚠️ 无法读取视频时长，将靠连续卡住检测停止")
        stop_at = None

    # ── 注入广告位插入脚本 ──────────────────────────────────
    # 注意：JS 模板里含 % 运算符，不能用 % 格式化，用字符串拼接传参
    _stop_at_js = str(stop_at) if stop_at is not None else 'null'
    ad_script = (
        "(function(intervalSeconds, startTimeInput, stopAt) {\n"
        "    let currentStartTime = startTimeInput;\n"
        "    let sameTimeCount = 0;\n"
        "    let lastDetectedTime = null;\n"
        "    let insertCount = 0;\n"
        "\n"
        "    function formatTime(s) {\n"
        "        const h = Math.floor(s / 3600);\n"
        "        const m = Math.floor((s % 3600) / 60);\n"
        "        const ss = s % 60;\n"
        "        return h + ':' + String(m).padStart(2,'0') + ':' + String(ss).padStart(2,'0') + ':00';\n"
        "    }\n"
        "\n"
        "    function findTimeInput() {\n"
        "        const sels = [\n"
        "            'input[type=\"text\"][class*=\"ytcp-media-timestamp-input\"]',\n"
        "            'input.ytcp-media-timestamp-input',\n"
        "            'input[maxlength=\"10\"][type=\"text\"]',\n"
        "        ];\n"
        "        for (const sel of sels) {\n"
        "            const inputs = document.querySelectorAll(sel);\n"
        "            for (let i = inputs.length - 1; i >= 0; i--) {\n"
        "                if (inputs[i].offsetWidth > 0) return inputs[i];\n"
        "            }\n"
        "        }\n"
        "        return null;\n"
        "    }\n"
        "\n"
        "    function getTimeFromInput(input) {\n"
        "        try {\n"
        "            const parts = (input.value || '').trim().split(':');\n"
        "            if (parts.length !== 4) return null;\n"
        "            return +parts[0]*3600 + +parts[1]*60 + +parts[2];\n"
        "        } catch(e) { return null; }\n"
        "    }\n"
        "\n"
        "    function setInputValue(input, value) {\n"
        "        input.focus(); input.select();\n"
        "        document.execCommand('insertText', false, value);\n"
        "        ['input','change'].forEach(t => input.dispatchEvent(new Event(t, {bubbles:true})));\n"
        "    }\n"
        "\n"
        "    function clickInsertButton() {\n"
        "        for (const btn of document.querySelectorAll('ytcp-button#place-manually-button, button')) {\n"
        "            const t = btn.textContent || btn.getAttribute('aria-label') || '';\n"
        "            if (t.includes('\\u63d2\\u5165\\u5e7f\\u544a\\u4f4d') || t.includes('Place ad break')) {\n"
        "                btn.click(); return true;\n"
        "            }\n"
        "        }\n"
        "        return false;\n"
        "    }\n"
        "\n"
        "    function processInsert() {\n"
        "        if (!document.body.innerText.includes('\\u4e2d\\u8d34\\u7247\\u5e7f\\u544a\\u4f4d')) {\n"
        "            window.__adHelperDone = true; return;\n"
        "        }\n"
        "        if (stopAt !== null && currentStartTime > stopAt) {\n"
        "            window.__adHelperDone = true; return;\n"
        "        }\n"
        "        if (sameTimeCount >= 5) { window.__adHelperDone = true; return; }\n"
        "\n"
        "        const input = findTimeInput();\n"
        "        if (!input) { setTimeout(processInsert, 200); return; }\n"
        "\n"
        "        const cur = getTimeFromInput(input);\n"
        "        if (cur !== null) {\n"
        "            if (lastDetectedTime === cur) sameTimeCount++;\n"
        "            else { sameTimeCount = 0; lastDetectedTime = cur; }\n"
        "            if (cur === currentStartTime) {\n"
        "                currentStartTime += intervalSeconds;\n"
        "                setTimeout(processInsert, 50);\n"
        "                return;\n"
        "            }\n"
        "        }\n"
        "\n"
        "        setInputValue(input, formatTime(currentStartTime));\n"
        "        setTimeout(() => {\n"
        "            if (getTimeFromInput(input) === currentStartTime) {\n"
        "                clickInsertButton();\n"
        "                insertCount++;\n"
        "                currentStartTime += intervalSeconds;\n"
        "                setTimeout(processInsert, 80);\n"
        "            } else {\n"
        "                setTimeout(processInsert, 150);\n"
        "            }\n"
        "        }, 40);\n"
        "    }\n"
        "\n"
        "    window.__adHelperDone = false;\n"
        "    window.__adInsertCount = 0;\n"
        "    setTimeout(processInsert, 300);\n"
        "    const _ticker = setInterval(() => {\n"
        "        window.__adInsertCount = insertCount;\n"
        "        if (window.__adHelperDone) clearInterval(_ticker);\n"
        "    }, 200);\n"
        f"}})({interval}, {start}, {_stop_at_js});\n"
    )

    page.evaluate(ad_script)
    on_log("  广告位脚本已注入，等待插入完成...")

    deadline = time.time() + 300
    last_count = 0
    while time.time() < deadline:
        if job is not None and job.is_cancelled():
            on_log("上传已取消（广告插入阶段）")
            return
        if page.evaluate("() => !!window.__adHelperDone"):
            break
        count = page.evaluate("() => window.__adInsertCount || 0")
        if count != last_count:
            on_log(f"  已插入 {count} 个广告位...")
            last_count = count
        page.wait_for_timeout(1500)

    final_count = page.evaluate("() => window.__adInsertCount || 0")
    on_log(f"  ✓ 广告位插入完成，共插入 {final_count} 个，返回创收页...")

    # 点广告位页的"继续"（#save-button）返回创收界面
    try:
        save_btn = page.locator('ytcp-button#save-button')
        save_btn.wait_for(state="visible", timeout=10_000)
        save_btn.click(timeout=10_000)
        page.wait_for_timeout(1500)
    except PWTimeout:
        on_log("  ⚠️ 未找到广告位页继续按钮，请手动点击")
        return

    # 等创收界面的"继续"（#next-button）变为 enabled 后点击
    on_log("  继续 → 是否适合投放广告...")
    try:
        next_btn = page.locator('ytcp-button#next-button')
        next_btn.wait_for(state="visible", timeout=10_000)
        for _ in range(20):
            if next_btn.is_enabled():
                break
            page.wait_for_timeout(300)
        next_btn.click(timeout=10_000)
        page.wait_for_timeout(1500)
        on_log(f"  点击继续后 URL: {page.url}")
    except PWTimeout:
        on_log("  ⚠️ 未找到创收页继续按钮，请手动点击")


def _submit_ad_suitability(page, on_log: Callable, timeout: int, template: dict | None = None):
    """
    是否适合投放广告页面：全程用 JS evaluate 操作（元素在深层 Shadow DOM 内）
    1. 轮询等待分级页面出现
    2. 按模板匹配问题分类，展开并点击指定 radio
    3. 选项序号 0 表示保持默认/跳过
    4. 点 #submit-questionnaire-button 提交
    """
    JS_HELPERS = """
        function findInShadow(root, sel) {
            const el = root.querySelector(sel);
            if (el) return el;
            for (const c of root.querySelectorAll('*')) {
                if (c.shadowRoot) { const f = findInShadow(c.shadowRoot, sel); if (f) return f; }
            }
            return null;
        }
        function findAllInShadow(root, sel, out) {
            for (const el of root.querySelectorAll(sel)) out.push(el);
            for (const c of root.querySelectorAll('*')) {
                if (c.shadowRoot) findAllInShadow(c.shadowRoot, sel, out);
            }
        }
    """

    # 轮询等待分级页面（JS 穿透 Shadow DOM）
    on_log("  等待分级页面...")
    deadline = time.time() + 30
    found = False
    while time.time() < deadline:
        found = page.evaluate(f"() => {{ {JS_HELPERS} return !!findInShadow(document, 'ytpp-self-certification-question-answer-details'); }}")
        if found:
            break
        page.wait_for_timeout(500)

    if not found:
        on_log(f"  ⚠️ 未找到是否适合投放广告页面，跳过")
        return

    on_log("  分级页面已就绪")
    page.wait_for_timeout(500)

    # 展开第一个问题卡片
    # 结构（全 light DOM，无 shadowRoot）：
    #   ytpp-self-certification-questionnaire .questions
    #     > ytpp-self-certification-question
    #       > ytcp-expansion-panel #container > ytcp-ve > div#button-area > button  ← 展开按钮
    #       > ytcp-expansion-panel #container > div#content-area
    #         > tp-yt-paper-radio-group > tp-yt-paper-radio-button  ← radio 选项
    template = normalize_ad_suitability_template(template)
    choices = page.evaluate(r"""async (template) => {
        const sleep = (ms) => new Promise(resolve => setTimeout(resolve, ms));
        const normalize = (text) => (text || '').replace(/\s+/g, ' ').trim();
        const visible = (el) => {
            if (!el || !el.getBoundingClientRect) return false;
            const rect = el.getBoundingClientRect();
            const style = getComputedStyle(el);
            return rect.width > 0 && rect.height > 0 && style.display !== 'none' && style.visibility !== 'hidden';
        };
        const questions = Array.from(document.querySelectorAll(
            'ytpp-self-certification-questionnaire .questions ytpp-self-certification-question'
        ));
        const results = [];
        for (let i = 0; i < questions.length; i++) {
            const q = questions[i];
            const qText = normalize(q.innerText || q.textContent || '');
            let choice = Number(template.default || 1);
            let matched = 'default';
            for (const [key, rawChoice] of Object.entries(template.questions || {})) {
                if (key && qText.includes(key)) {
                    choice = Number(rawChoice);
                    matched = key;
                    break;
                }
            }
            if (!choice || choice < 1) {
                results.push({index: i + 1, matched, choice, status: 'skipped', text: qText.slice(0, 36)});
                continue;
            }
            let radios = Array.from(q.querySelectorAll('tp-yt-paper-radio-button'));
            if (!radios.some(visible)) {
                const btn = q.querySelector('ytcp-expansion-panel #button-area button');
                if (btn) {
                    btn.click();
                    await sleep(700);
                }
                radios = Array.from(q.querySelectorAll('tp-yt-paper-radio-button'));
            }
            const visibleRadios = radios.filter(visible);
            const usable = visibleRadios.length ? visibleRadios : radios;
            const radio = usable[choice - 1];
            if (!radio) {
                results.push({index: i + 1, matched, choice, status: 'missing-radio', text: qText.slice(0, 36)});
                continue;
            }
            radio.click();
            await sleep(180);
            results.push({
                index: i + 1,
                matched,
                choice,
                status: 'clicked',
                text: qText.slice(0, 36),
                radio: normalize(radio.innerText || radio.textContent || radio.getAttribute('name') || '').slice(0, 36)
            });
        }
        return results;
    }""", template)
    selected_count = sum(1 for item in choices if item.get("status") == "clicked")
    skipped_count = sum(1 for item in choices if item.get("status") == "skipped")
    on_log(f"  广告分级模板已应用：选择 {selected_count} 项，跳过 {skipped_count} 项")
    for item in choices[:12]:
        on_log(f"    Q{item.get('index')} {item.get('matched')}={item.get('choice')} -> {item.get('status')}")
    page.wait_for_timeout(600)

    # 点提交按钮
    submitted = page.evaluate(f"""() => {{
        {JS_HELPERS}
        const btn = findInShadow(document, 'ytcp-button#submit-questionnaire-button');
        if (!btn) return false;
        const inner = btn.shadowRoot ? btn.shadowRoot.querySelector('button') : btn.querySelector('button');
        if (inner) {{ inner.click(); return 'inner'; }}
        btn.click(); return 'outer';
    }}""")
    on_log(f"  提交结果: {submitted}")
    if submitted:
        page.wait_for_timeout(500)
        on_log("  ✓ 已提交分级结果")
    else:
        on_log("  ⚠️ 点击提交分级结果失败，请手动操作")



# ─────────────────────────────────────────────────────────────
# 等待上传完成并发布
# ─────────────────────────────────────────────────────────────

def _read_upload_progress(page) -> Optional[dict]:
    """Read the visible bottom-left YouTube progress label."""
    try:
        return page.evaluate("""() => {
            function visible(el) {
                if (!el || !el.getBoundingClientRect) return false;
                const r = el.getBoundingClientRect();
                const s = getComputedStyle(el);
                return r.width > 0 && r.height > 0 && s.display !== 'none' && s.visibility !== 'hidden';
            }
            function collectDeep(root, selector, out = []) {
                if (!root || !root.querySelectorAll) return out;
                for (const el of root.querySelectorAll(selector)) out.push(el);
                for (const el of root.querySelectorAll('*')) {
                    if (el.shadowRoot) collectDeep(el.shadowRoot, selector, out);
                }
                return out;
            }
            function collectText(root) {
                let parts = [];
                const text = root.innerText || root.textContent || '';
                if (text) parts.push(text);
                for (const el of root.querySelectorAll('*')) {
                    const t = el.innerText || el.textContent || '';
                    if (t) parts.push(t);
                    if (el.shadowRoot) {
                        const st = collectText(el.shadowRoot);
                        if (st) parts.push(st);
                    }
                }
                return parts.join('\\n');
            }
            const comp = document.querySelector('ytcp-video-upload-progress');
            if (!comp) return null;
            const roots = [comp, comp.shadowRoot].filter(Boolean);
            const progressLabels = roots.flatMap(root => collectDeep(root, 'span.progress-label, .progress-label'))
                .filter(visible)
                .map(el => (el.innerText || el.textContent || '').trim())
                .filter(Boolean);
            const text = progressLabels.length ? progressLabels[progressLabels.length - 1] : roots.map(collectText).join('\\n');
            const m = text.match(/(\\d+)\\s*%/);
            const pct = m ? parseInt(m[1]) : null;
            const lower = text.toLowerCase();
            let phase = 'unknown';
            if (text.includes('上传完毕') || lower.includes('upload complete') || lower.includes('upload finished')) {
                phase = 'complete';
            } else if (text.includes('检查') || text.includes('检测') || lower.includes('check')) {
                phase = 'checking';
            } else if (text.includes('处理') || lower.includes('processing')) {
                phase = 'processing';
            } else if (text.includes('上传') || lower.includes('upload')) {
                phase = 'uploading';
            }
            return { pct, phase, text };
        }""")
    except Exception:
        return None


def _emit_upload_progress(state: dict, pct: int, on_progress: Callable) -> None:
    """Emit app progress without ever moving the UI backwards."""
    try:
        pct = int(pct)
        current = int(state.get("app_pct", 0))
    except Exception:
        return
    if pct <= current:
        return
    state["app_pct"] = pct
    on_progress(pct)


def _click_precheck_publish_dialog(page) -> Optional[dict]:
    """Click Publish in YouTube's still-checking dialog and report whether it closed."""
    from playwright.sync_api import TimeoutError as PWTimeout

    try:
        exact_btn = page.locator('ytcp-prechecks-warning-dialog ytcp-button#secondary-action-button').first
        if exact_btn.is_visible(timeout=800):
            exact_btn.click(timeout=3_000, force=True)
            page.wait_for_timeout(800)
            try:
                still_visible = page.locator('ytcp-prechecks-warning-dialog').first.is_visible(timeout=800)
            except PWTimeout:
                still_visible = False
            if not still_visible:
                return {
                    "visible": False,
                    "clicked": True,
                    "dismissed": True,
                    "method": "playwright-secondary-action",
                    "buttonId": "secondary-action-button",
                }
    except Exception:
        pass

    try:
        return page.evaluate("""async () => {
            function allDeep(root, selector, out = []) {
                if (!root) return out;
                if (root.querySelectorAll) {
                    for (const el of root.querySelectorAll(selector)) out.push(el);
                    for (const el of root.querySelectorAll('*')) {
                        if (el.shadowRoot) allDeep(el.shadowRoot, selector, out);
                    }
                }
                return out;
            }
            function textOf(node) {
                const parts = [];
                function visit(n) {
                    if (!n) return;
                    if (n.nodeType === Node.TEXT_NODE) {
                        const t = (n.textContent || '').trim();
                        if (t) parts.push(t);
                        return;
                    }
                    if (
                        n.nodeType !== Node.ELEMENT_NODE &&
                        n.nodeType !== Node.DOCUMENT_FRAGMENT_NODE &&
                        n.nodeType !== Node.DOCUMENT_NODE
                    ) return;
                    if (n.shadowRoot) visit(n.shadowRoot);
                    for (const child of n.childNodes || []) visit(child);
                }
                visit(node);
                return parts.join(' ').replace(/\\s+/g, ' ').trim();
            }
            function visible(el) {
                if (!el || !el.getBoundingClientRect) return false;
                const r = el.getBoundingClientRect();
                const s = getComputedStyle(el);
                return r.width > 0 && r.height > 0 && s.display !== 'none' && s.visibility !== 'hidden';
            }
            function clickTarget(el) {
                return (
                    (el.shadowRoot && (el.shadowRoot.querySelector('button') || el.shadowRoot.querySelector('[role="button"]'))) ||
                    el.querySelector('button, [role="button"]') ||
                    el
                );
            }
            function fireClick(el) {
                if (!el) return false;
                try { el.scrollIntoView({ block: 'center', inline: 'center' }); } catch (_) {}
                try { el.removeAttribute('disabled'); } catch (_) {}
                try { el.removeAttribute('aria-disabled'); } catch (_) {}
                try { if ('disabled' in el) el.disabled = false; } catch (_) {}
                for (const type of ['pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click']) {
                    try {
                        el.dispatchEvent(new MouseEvent(type, {
                            bubbles: true,
                            cancelable: true,
                            composed: true,
                            view: window
                        }));
                    } catch (_) {}
                }
                try { el.click(); } catch (_) {}
                return true;
            }
            function clickElementCenter(el) {
                if (!el || !el.getBoundingClientRect) return false;
                try { el.scrollIntoView({ block: 'center', inline: 'center' }); } catch (_) {}
                const r = el.getBoundingClientRect();
                const x = Math.max(1, Math.min(window.innerWidth - 1, r.left + r.width / 2));
                const y = Math.max(1, Math.min(window.innerHeight - 1, r.top + r.height / 2));
                const top = document.elementFromPoint(x, y) || el;
                fireClick(top);
                return true;
            }

            const dialogs = allDeep(document, 'ytcp-prechecks-warning-dialog')
                .filter(visible);
            const dialog = dialogs.find(d => {
                const t = textOf(d).toLowerCase();
                return t.includes('仍在检查') ||
                    t.includes('检查你的视频') ||
                    t.includes('still checking') ||
                    t.includes('checking your video') ||
                    t.includes('checks are still');
            });
            if (!dialog) return { visible: false, clicked: false, reason: 'not_found' };

            const dialogText = textOf(dialog);
            const exactButton =
                dialog.querySelector('ytcp-button#secondary-action-button') ||
                dialog.querySelector('#secondary-action-button');
            const allButtons = allDeep(dialog, 'ytcp-button, tp-yt-paper-button, button, [role="button"]')
                .filter(visible)
                .map(el => ({ el, text: textOf(el), id: el.id || el.getAttribute('id') || '' }));
            const candidates = (exactButton ? [{ el: exactButton, text: textOf(exactButton), id: exactButton.id || exactButton.getAttribute('id') || 'secondary-action-button' }] : [])
                .concat(allButtons)
                .filter(item => {
                    const t = (item.text || '').toLowerCase();
                    const isPublish = item.text === '发布' || t === 'publish' || t.includes('发布') || t.includes('publish');
                    const isChangeVisibility = t.includes('更改') || t.includes('change') || t.includes('visibility') || t.includes('公开范围');
                    return isPublish && !isChangeVisibility;
                })
                .sort((a, b) => {
                    const aSecondary = (a.id || '').includes('secondary-action') ? 0 : 1;
                    const bSecondary = (b.id || '').includes('secondary-action') ? 0 : 1;
                    if (aSecondary !== bSecondary) return aSecondary - bSecondary;
                    return (a.text || '').length - (b.text || '').length;
                });
            if (!candidates.length) {
                return {
                    visible: true,
                    clicked: false,
                    dismissed: false,
                    reason: 'button_not_found',
                    text: dialogText.slice(0, 200),
                    buttons: allButtons.map(item => ({ id: item.id, text: item.text })).slice(0, 8)
                };
            }

            const target = clickTarget(candidates[0].el);
            const disabled =
                candidates[0].el.hasAttribute('disabled') ||
                candidates[0].el.getAttribute('aria-disabled') === 'true' ||
                target.disabled === true ||
                target.getAttribute('aria-disabled') === 'true';
            fireClick(candidates[0].el);
            if (target !== candidates[0].el) fireClick(target);
            clickElementCenter(target);
            clickElementCenter(candidates[0].el);
            await new Promise(resolve => setTimeout(resolve, 800));
            const stillVisible = document.contains(dialog) && visible(dialog);
            return {
                visible: stillVisible,
                clicked: true,
                dismissed: !stillVisible,
                disabledBefore: disabled,
                buttonText: candidates[0].text,
                buttonId: candidates[0].id
            };
        }""")
    except Exception:
        return None


def _click_schedule_checking_notice(page) -> Optional[dict]:
    """Acknowledge the occasional scheduled-publish checks notice.

    YouTube can show “我们仍在检查你的内容” after Schedule is clicked.  The
    reservation does not continue to the success/share dialog until the
    operator clicks “知道了” (Got it).
    """
    try:
        return page.evaluate("""async () => {
            function visible(el) {
                if (!el || !el.getBoundingClientRect) return false;
                const r = el.getBoundingClientRect();
                const s = getComputedStyle(el);
                return r.width > 0 && r.height > 0 && s.display !== 'none' && s.visibility !== 'hidden';
            }
            function clean(text) {
                return (text || '').replace(/\s+/g, ' ').trim();
            }
            const hasNotice = text =>
                text.includes('我们仍在检查你的内容') ||
                text.includes('我們仍在檢查你的內容') ||
                /we(?:'|’)re still checking your content/i.test(text) ||
                /we are still checking your content/i.test(text);
            // Only act on a visible modal that contains the notice.  Looking
            // at document.body can match stale/background Studio text and
            // wrongly click an unrelated OK button when no notice is shown.
            const noticeDialog = Array.from(document.querySelectorAll(
                'ytcp-dialog, tp-yt-paper-dialog, [role="dialog"]'
            )).filter(visible).find(el => hasNotice(clean(el.innerText || el.textContent || '')));
            if (!noticeDialog) return { visible: false, clicked: false, dismissed: true };

            // The label varies by Studio language/build.  In the macOS
            // Chromium build this dialog is currently rendered as "OK".
            const labels = ['知道了', '知道瞭', '确定', 'OK', 'Got it', 'Understood'];
            const candidates = Array.from(noticeDialog.querySelectorAll(
                'button, ytcp-button, tp-yt-paper-button, [role="button"]'
            ));
            const button = candidates.find(el => {
                if (!visible(el)) return false;
                const text = clean(el.innerText || el.textContent || '');
                const aria = clean(el.getAttribute('aria-label') || '');
                return labels.some(label => text === label || aria === label);
            });
            if (!button) {
                return { visible: true, clicked: false, dismissed: false, reason: 'button_not_found' };
            }

            const inner = button.matches('button, [role="button"]')
                ? button
                : (button.querySelector('button, [role="button"]') || button);
            try { inner.scrollIntoView({ block: 'center', inline: 'center' }); } catch (_) {}
            for (const type of ['pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click']) {
                try {
                    const EventClass = type.startsWith('pointer') && window.PointerEvent ? PointerEvent : MouseEvent;
                    inner.dispatchEvent(new EventClass(type, {
                        bubbles: true, cancelable: true, composed: true, view: window,
                        pointerId: 1, pointerType: 'mouse', isPrimary: true, button: 0
                    }));
                } catch (_) {}
            }
            try { inner.click(); } catch (_) {}
            if (inner !== button) {
                try { button.click(); } catch (_) {}
            }
            await new Promise(resolve => setTimeout(resolve, 700));
            const dismissed = !visible(noticeDialog) || !hasNotice(clean(noticeDialog.innerText || noticeDialog.textContent || ''));
            return { visible: !dismissed, clicked: true, dismissed, method: 'schedule-got-it' };
        }""")
    except Exception:
        return None


def _schedule_upload_immediately(page, job, scheduled_at: str,
                                 on_log: Callable, on_progress: Callable,
                                 timeout: int, upload_state: dict = None) -> Optional[str]:
    """Complete Studio's scheduled-publish flow after a short upload settle period.

    Expected UI sequence:
      Schedule -> optional "still checking" / Got it notice ->
      "Video publish time scheduled" dialog -> Close.
    Once Studio accepts the Schedule action, the reservation is committed.
    Later notices/dialogs are cleanup only and must never cause a retry of the
    same video, which would create a duplicate upload.
    """
    upload_state = upload_state or {}
    deadline = time.time() + timeout / 1000

    if job.is_cancelled():
        on_log("上传已取消")
        return None

    on_log("日期和时间已填写，点击预定后会等待上传稳定再继续...")
    page.wait_for_timeout(700)

    def response_state() -> str:
        try:
            return str(page.evaluate("""() => {
                function visible(el) {
                    if (!el || !el.getBoundingClientRect) return false;
                    const r = el.getBoundingClientRect();
                    const s = getComputedStyle(el);
                    return r.width > 0 && r.height > 0 &&
                        s.display !== 'none' && s.visibility !== 'hidden';
                }
                const text = (document.body.innerText || document.body.textContent || '')
                    .replace(/\\s+/g, ' ').trim();
                if (
                    text.includes('已安排好视频发布时间') ||
                    text.includes('已安排好影片發布時間') ||
                    /video publish time (?:has been )?scheduled/i.test(text)
                ) return 'scheduled';
                if (
                    text.includes('我们仍在检查你的内容') ||
                    text.includes('我們仍在檢查你的內容') ||
                    /we(?:'|’)re still checking your content/i.test(text) ||
                    /we are still checking your content/i.test(text)
                ) return 'checking-notice';
                return '';
            }""") or "")
        except Exception:
            return ""

    click_accepted = False
    click_deadline = min(deadline, time.time() + 30)
    attempt = 0
    while time.time() < click_deadline and not click_accepted:
        if job.is_cancelled():
            on_log("上传已取消")
            return None

        # Studio exposes the unique final action as #done-button.  Resolve
        # its native button first; text/aria matching is only a fallback.
        schedule_button = page.locator(
            'ytcp-button#done-button button, #done-button button'
        ).first
        try:
            if not (
                schedule_button.is_visible(timeout=500) and
                schedule_button.is_enabled(timeout=500)
            ):
                schedule_button = None
        except Exception:
            schedule_button = None
        if schedule_button is None:
            schedule_button = _find_enabled_upload_action_button(
                page, schedule_enabled=True, visibility="PUBLIC"
            )
        if schedule_button is None:
            page.wait_for_timeout(300)
            continue

        attempt += 1
        method = "按钮点击"
        clicked = False
        try:
            if attempt % 3 == 1:
                method = "#done-button 强制目标点击"
                schedule_button.click(timeout=5_000, force=True)
                clicked = True
            elif attempt % 3 == 2:
                method = "#done-button DOM 点击"
                clicked = bool(page.evaluate("""() => {
                    const host = document.querySelector('ytcp-button#done-button');
                    if (!host) return false;
                    const button = host.querySelector('button');
                    if (!button) return false;
                    button.click();
                    return true;
                }"""))
            else:
                method = "#done-button 键盘确认"
                schedule_button.focus()
                page.keyboard.press("Enter")
                clicked = True
        except Exception as exc:
            on_log(f"  第 {attempt} 次{method}未完成: {exc}")

        if not clicked:
            page.wait_for_timeout(300)
            continue

        state_deadline = min(click_deadline, time.time() + 3)
        state = ""
        while time.time() < state_deadline:
            state = response_state()
            if state:
                break
            page.wait_for_timeout(200)
        if state:
            click_accepted = True
            on_log(f"  ✓ 第 {attempt} 次{method}已被页面接受（{state}）")
            break

        on_log(f"  ⚠️ 第 {attempt} 次{method}后页面无变化，重新获取预定按钮再试")

    if not click_accepted:
        on_log("❌ 已找到“预定”按钮，但连续点击后 YouTube Studio 页面仍无响应")
        return None

    _emit_upload_progress(upload_state, 88, on_progress)
    # The Schedule action is often accepted while the file is still uploading
    # (for example, Studio may show 61% with seconds remaining).  Keep this
    # page alive for 30 seconds before dismissing its dialogs or moving to the
    # next item, so Studio can finish committing the upload.  The total wait
    # is capped at 35 seconds, preserving batch throughput if processing is
    # slow or the UI does not expose a reliable percentage.
    settle_until = min(deadline, time.time() + 30)
    cleanup_deadline = min(deadline, time.time() + 35)
    on_log("预约已提交，等待约 30 秒让 YouTube 上传并保存，再关闭页面进入下一个视频...")

    notice_logged = False
    settle_logged = False
    while time.time() < cleanup_deadline:
        if job.is_cancelled():
            on_log("上传已取消")
            return None

        notice = _click_schedule_checking_notice(page)
        if notice and notice.get("clicked"):
            on_log("  ✓ 检测到“我们仍在检查你的内容”，已点击“知道了”")
            try:
                page.wait_for_timeout(700)
            except Exception as exc:
                # Studio occasionally tears down the upload tab immediately
                # after acknowledging this notice.  At this point its Schedule
                # action has already been accepted, so retrying would upload
                # the same video again and block the rest of a batch.
                if "closed" in str(exc).lower():
                    on_log("  ✓ 已提交预约并点击“知道了”；Studio 自动关闭上传页，继续下一个视频")
                    _emit_upload_progress(upload_state, 100, on_progress)
                    return "scheduled"
                raise
            continue
        if notice and notice.get("visible"):
            if not notice_logged:
                on_log("等待“知道了”按钮可点击...")
                notice_logged = True
            page.wait_for_timeout(500)
            continue

        try:
            processing_info = page.evaluate("""() => {
            const visible = el => {
                if (!el || !el.getBoundingClientRect) return false;
                const r = el.getBoundingClientRect();
                const s = getComputedStyle(el);
                return r.width > 0 && r.height > 0 && s.display !== 'none' && s.visibility !== 'hidden';
            };
            const dialog = Array.from(document.querySelectorAll(
                'ytcp-uploads-still-processing-dialog, ytcp-dialog, tp-yt-paper-dialog, [role="dialog"]'
            )).filter(visible).find(el => /正在处理视频|正在處理影片|processing video/i.test(
                (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim()
            ));
            if (!dialog) return null;
            const text = (dialog.innerText || dialog.textContent || '').replace(/\\s+/g, ' ').trim();
            const link = dialog.querySelector(
                'a[href*="youtu.be"], a[href*="watch?v="], a[href*="shorts/"]'
            );
            return {
                text,
                href: link ? link.href : '',
                // This is the explicit file-transfer completion state shown
                // by Studio ("上传完毕 … 即将开始处理").  Processing may
                // continue server-side, but the browser can safely move on.
                uploadComplete: /上传完毕|上傳完畢|upload complete|upload finished/i.test(text),
            };
            }""")
        except Exception as exc:
            on_log(f"  ⚠️ 预约已提交后无法继续读取 Studio 弹窗：{exc}；不重传该视频")
            _emit_upload_progress(upload_state, 100, on_progress)
            return "scheduled"
        if processing_info:
            upload_complete = bool(processing_info.get("uploadComplete", False))
            if time.time() < settle_until and not upload_complete:
                if not settle_logged:
                    on_log("检测到“正在处理视频”，继续等待上传稳定，不关闭此页面")
                    settle_logged = True
                page.wait_for_timeout(1_000)
                continue
            if upload_complete:
                on_log("检测到“上传完毕，即将开始处理”，文件已上传完成，立即关闭页面并继续下一个视频")
            else:
                on_log("检测到“正在处理视频”弹窗，预约信息已被 YouTube 接受")
            href = str(processing_info.get("href") or "")
            video_id = None
            match = re.search(
                r"youtu\.be/([A-Za-z0-9_-]{11})|shorts/([A-Za-z0-9_-]{11})|v=([A-Za-z0-9_-]{11})",
                href,
            )
            if match:
                video_id = match.group(1) or match.group(2) or match.group(3)
            closed = _close_still_processing_dialog(page)
            if not (closed and closed.get("dismissed")):
                on_log("⚠️ 已确认预约信息，但“正在处理视频”弹窗未能关闭")
                page.wait_for_timeout(500)
                continue
            on_log("  ✓ 已点击真实“关闭”按钮并关闭处理弹窗")
            _emit_upload_progress(upload_state, 100, on_progress)
            return video_id or "scheduled"

        try:
            confirmation = page.evaluate("""() => {
            function visible(el) {
                if (!el || !el.getBoundingClientRect) return false;
                const r = el.getBoundingClientRect();
                const s = getComputedStyle(el);
                return r.width > 0 && r.height > 0 &&
                    s.display !== 'none' && s.visibility !== 'hidden';
            }
            function clean(text) {
                return (text || '').replace(/\\s+/g, ' ').trim();
            }
            const dialogs = Array.from(document.querySelectorAll(
                'ytcp-video-share-dialog, ytcp-dialog, tp-yt-paper-dialog'
            )).filter(visible);
            const dialog = dialogs.find(el => {
                const text = clean(el.innerText || el.textContent || '');
                return text.includes('已安排好视频发布时间') ||
                    text.includes('已安排好影片發布時間') ||
                    /video publish time (?:has been )?scheduled/i.test(text) ||
                    /video scheduled/i.test(text);
            });
            if (!dialog) return null;
            const text = clean(dialog.innerText || dialog.textContent || '');
            const link = dialog.querySelector(
                'a[href*="youtu.be"], a[href*="youtube.com/shorts/"], a[href*="watch?v="]'
            );
            const input = dialog.querySelector('input[type="text"], .text-input');
            return {
                text,
                href: link ? link.href : (input ? input.value : ''),
            };
            }""")
        except Exception as exc:
            on_log(f"  ⚠️ 预约已提交后确认页不可用：{exc}；不重传该视频")
            _emit_upload_progress(upload_state, 100, on_progress)
            return "scheduled"
        if not confirmation:
            if time.time() < settle_until and not settle_logged:
                on_log("等待 YouTube 完成上传/保存状态...")
                settle_logged = True
            page.wait_for_timeout(500)
            continue

        if time.time() < settle_until:
            if not settle_logged:
                on_log("已出现预约确认，仍等待上传稳定后再关闭页面")
                settle_logged = True
            page.wait_for_timeout(500)
            continue

        on_log("  ✓ 检测到“已安排好视频发布时间”，定时成功")
        href = str(confirmation.get("href") or "")
        video_id = None
        match = re.search(
            r"youtu\.be/([A-Za-z0-9_-]{11})|shorts/([A-Za-z0-9_-]{11})|v=([A-Za-z0-9_-]{11})",
            href,
        )
        if match:
            video_id = match.group(1) or match.group(2) or match.group(3)
            on_log(f"  视频链接：{href}")

        closed = _close_scheduled_confirmation_dialog(page)
        if not (closed and closed.get("dismissed")):
            on_log(f"⚠️ 已确认定时成功，但确认弹窗未能关闭：{closed}")
        else:
            on_log(f"  ✓ 已点击关闭（{closed.get('method')}）")

        _emit_upload_progress(upload_state, 100, on_progress)
        return video_id or "scheduled"

    target_label = str(scheduled_at or "").replace("T", " ")
    on_log(f"  ✓ 已点击预定（目标 {target_label}）；已等待上传稳定，收尾弹窗未完成，自动继续下一个视频且不重传")
    _emit_upload_progress(upload_state, 100, on_progress)
    return "scheduled"


def _close_scheduled_confirmation_dialog(page) -> Optional[dict]:
    """Dismiss the final scheduled-publish confirmation dialog.

    Studio has used both a text Close button and icon/Done buttons for this
    dialog.  Clicking a matching element alone is insufficient: only report
    success once the actual confirmation dialog has disappeared.
    """
    try:
        result = page.evaluate("""async () => {
            function visible(el) {
                if (!el || !el.getBoundingClientRect) return false;
                const r = el.getBoundingClientRect();
                const s = getComputedStyle(el);
                return r.width > 0 && r.height > 0 &&
                    s.display !== 'none' && s.visibility !== 'hidden';
            }
            function confirmationDialog() {
                return Array.from(document.querySelectorAll(
                    'ytcp-video-share-dialog, ytcp-dialog, tp-yt-paper-dialog'
                )).filter(visible).find(el => {
                    const text = (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
                    return text.includes('已安排好视频发布时间') ||
                        text.includes('已安排好影片發布時間') ||
                        /video publish time (?:has been )?scheduled/i.test(text) ||
                        /video scheduled/i.test(text);
                }) || null;
            }
            const dialog = confirmationDialog();
            if (!dialog) return { clicked: false, dismissed: true, method: 'already-gone' };

            const candidates = Array.from(dialog.querySelectorAll(
                '#close-button button, ytcp-button#close-button, [id*="close"] button, ' +
                'button, [role="button"], ytcp-button, tp-yt-paper-button'
            )).filter(visible);
            const label = el => [el.innerText, el.textContent, el.getAttribute('aria-label'), el.getAttribute('title')]
                .filter(Boolean).join(' ').replace(/\\s+/g, ' ').trim().toLowerCase();
            const close = candidates.find(el => /关闭|關閉|close|完成|done|finish/.test(label(el)));
            if (!close) return { clicked: false, dismissed: false, reason: 'close_control_not_found' };

            const inner = close.matches('button, [role="button"]')
                ? close
                : (close.querySelector('button, [role="button"]') || close.shadowRoot?.querySelector('button, [role="button"]') || close);
            try { inner.scrollIntoView({ block: 'center', inline: 'center' }); } catch (_) {}
            try { inner.click(); } catch (_) {}
            if (inner !== close) { try { close.click(); } catch (_) {} }
            await new Promise(resolve => setTimeout(resolve, 900));
            return {
                clicked: true,
                dismissed: !confirmationDialog(),
                method: 'confirmation-close-control',
                label: label(close),
            };
        }""")
        if result and result.get("dismissed"):
            return result
    except Exception:
        result = None

    # Some Studio revisions only wire the dialog's Escape handler.
    try:
        page.keyboard.press("Escape")
        page.wait_for_timeout(700)
        still_visible = page.evaluate("""() => Array.from(document.querySelectorAll(
            'ytcp-video-share-dialog, ytcp-dialog, tp-yt-paper-dialog'
        )).some(el => {
            const r = el.getBoundingClientRect();
            const text = (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
            return r.width > 0 && r.height > 0 && (
                text.includes('已安排好视频发布时间') || text.includes('已安排好影片發布時間') ||
                /video publish time (?:has been )?scheduled/i.test(text) || /video scheduled/i.test(text)
            );
        })""")
        if not still_visible:
            return {"clicked": True, "dismissed": True, "method": "escape"}
    except Exception:
        pass
    return result if result is not None else {"clicked": False, "dismissed": False}


def _close_still_processing_dialog(page) -> Optional[dict]:
    """Close YouTube's still-processing upload dialog with the exact close button."""
    from playwright.sync_api import TimeoutError as PWTimeout

    dialog_selector = (
        ':is(ytcp-uploads-still-processing-dialog, ytcp-dialog, '
        'tp-yt-paper-dialog, [role="dialog"])'
    )
    native_selector = f'{dialog_selector} ytcp-button#close-button button'
    host_selector = f'{dialog_selector} ytcp-button#close-button'
    shape_selector = f'{dialog_selector} ytcp-button#close-button ytcp-button-shape'

    def _still_visible() -> bool:
        try:
            return bool(page.evaluate("""() => {
                const visible = el => {
                    if (!el || !el.getBoundingClientRect) return false;
                    const r = el.getBoundingClientRect();
                    const s = getComputedStyle(el);
                    return r.width > 0 && r.height > 0 && s.display !== 'none' && s.visibility !== 'hidden';
                };
                return Array.from(document.querySelectorAll(
                    'ytcp-uploads-still-processing-dialog, ytcp-dialog, tp-yt-paper-dialog, [role="dialog"]'
                )).some(el => visible(el) && /正在处理视频|正在處理影片|processing video/i.test(
                    (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim()
                ));
            }"""))
        except PWTimeout:
            return False
        except Exception:
            return False

    try:
        close_btn = page.locator(native_selector).first
        if close_btn.is_visible(timeout=800):
            close_btn.click(timeout=1_500, force=True)
            page.wait_for_timeout(400)
            if not _still_visible():
                return {
                    "clicked": True,
                    "dismissed": True,
                    "method": "native-close-button",
                }
    except Exception:
        pass

    # Current Studio uses the component structure shown below.  The native
    # button is inside ytcp-button-shape's shadow root, so neither the native
    # selector nor clicking the outer ytcp-button is reliable.
    try:
        close_shape = page.locator(shape_selector).first
        if close_shape.is_visible(timeout=800):
            close_shape.click(timeout=1_500, force=True)
            page.wait_for_timeout(400)
            if not _still_visible():
                return {
                    "clicked": True,
                    "dismissed": True,
                    "method": "close-button-shape",
                }
    except Exception:
        pass

    try:
        close_host = page.locator(host_selector).first
        if close_host.is_visible(timeout=800):
            close_host.click(timeout=1_500, force=True)
            page.wait_for_timeout(400)
            if not _still_visible():
                return {
                    "clicked": True,
                    "dismissed": True,
                    "method": "close-button-host",
                }
    except Exception:
        pass

    try:
        box = page.evaluate("""() => {
            const dialog = document.querySelector('ytcp-uploads-still-processing-dialog');
            if (!dialog) return null;
            const btn = dialog.querySelector('ytcp-button#close-button') || dialog.querySelector('#close-button');
            if (!btn) return null;
            const target =
                btn.querySelector('ytcp-button-shape') ||
                btn.querySelector('ytcp-button-shape button') ||
                btn.querySelector('button[aria-label="关闭"], button[aria-label="Close"]') ||
                btn.querySelector('button, [role="button"]') ||
                btn;
            const r = target.getBoundingClientRect();
            if (!r.width || !r.height) return null;
            return {
                x: Math.max(1, Math.min(window.innerWidth - 1, r.left + r.width / 2)),
                y: Math.max(1, Math.min(window.innerHeight - 1, r.top + r.height / 2)),
                text: target.innerText || target.textContent || btn.innerText || btn.textContent || '',
                tag: target.tagName
            };
        }""")
        if box:
            page.mouse.move(box["x"], box["y"])
            page.wait_for_timeout(100)
            page.mouse.down()
            page.wait_for_timeout(80)
            page.mouse.up()
            page.wait_for_timeout(800)
            if not _still_visible():
                return {"clicked": True, "dismissed": True, "method": "mouse-center", "box": box}
    except Exception:
        pass

    try:
        focused = page.evaluate("""() => {
            const dialog = document.querySelector('ytcp-uploads-still-processing-dialog');
            if (!dialog) return false;
            const btn = dialog.querySelector('ytcp-button#close-button') || dialog.querySelector('#close-button');
            if (!btn) return false;
            const target =
                btn.querySelector('ytcp-button-shape') ||
                btn.querySelector('ytcp-button-shape button') ||
                btn.querySelector('button[aria-label="关闭"], button[aria-label="Close"]') ||
                btn.querySelector('button, [role="button"]') ||
                btn;
            try { target.focus(); } catch (_) {}
            return true;
        }""")
        if focused:
            page.keyboard.press("Enter")
            page.wait_for_timeout(500)
            if not _still_visible():
                return {"clicked": True, "dismissed": True, "method": "focus-enter"}
            page.keyboard.press("Space")
            page.wait_for_timeout(800)
            if not _still_visible():
                return {"clicked": True, "dismissed": True, "method": "focus-space"}
    except Exception:
        pass

    try:
        return page.evaluate("""async () => {
            function visible(el) {
                if (!el || !el.getBoundingClientRect) return false;
                const r = el.getBoundingClientRect();
                const s = getComputedStyle(el);
                return r.width > 0 && r.height > 0 && s.display !== 'none' && s.visibility !== 'hidden';
            }
            function fireClick(el) {
                if (!el) return false;
                try { el.scrollIntoView({ block: 'center', inline: 'center' }); } catch (_) {}
                for (const type of ['pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click']) {
                    try {
                        const EventClass = type.startsWith('pointer') && window.PointerEvent ? PointerEvent : MouseEvent;
                        el.dispatchEvent(new EventClass(type, {
                            bubbles: true,
                            cancelable: true,
                            composed: true,
                            view: window,
                            pointerId: 1,
                            pointerType: 'mouse',
                            isPrimary: true,
                            button: 0,
                            buttons: type.endsWith('down') ? 1 : 0
                        }));
                    } catch (_) {}
                }
                try { el.click(); } catch (_) {}
                return true;
            }
            function clickCenter(el) {
                if (!el || !el.getBoundingClientRect) return false;
                const r = el.getBoundingClientRect();
                const x = Math.max(1, Math.min(window.innerWidth - 1, r.left + r.width / 2));
                const y = Math.max(1, Math.min(window.innerHeight - 1, r.top + r.height / 2));
                const top = document.elementFromPoint(x, y) || el;
                fireClick(top);
                return true;
            }

            const dialog = document.querySelector('ytcp-uploads-still-processing-dialog');
            if (!dialog || !visible(dialog)) {
                return { clicked: false, dismissed: true, reason: 'not_visible' };
            }
            const btn = dialog.querySelector('ytcp-button#close-button') || dialog.querySelector('#close-button');
            if (!btn) {
                return { clicked: false, dismissed: false, reason: 'close_button_not_found' };
            }
            const inner =
                (btn.shadowRoot && (btn.shadowRoot.querySelector('button') || btn.shadowRoot.querySelector('[role="button"]'))) ||
                (btn.querySelector('ytcp-button-shape') && btn.querySelector('ytcp-button-shape').shadowRoot &&
                    (btn.querySelector('ytcp-button-shape').shadowRoot.querySelector('button') ||
                     btn.querySelector('ytcp-button-shape').shadowRoot.querySelector('[role="button"]'))) ||
                btn.querySelector('ytcp-button-shape') ||
                btn.querySelector('button, [role="button"]') ||
                btn;
            fireClick(btn);
            if (inner !== btn) fireClick(inner);
            clickCenter(inner);
            clickCenter(btn);
            await new Promise(resolve => setTimeout(resolve, 800));
            return {
                clicked: true,
                dismissed: !(document.contains(dialog) && visible(dialog)),
                method: 'js-close-button'
            };
        }""")
    except Exception:
        return None


def _schedule_date_labels(value: datetime) -> list[str]:
    month_en = value.strftime("%b")
    month_en_full = value.strftime("%B")
    return [
        f"{value.year}年{value.month}月{value.day}日",
        f"{value.year}/{value.month}/{value.day}",
        f"{value.month}/{value.day}/{value.year}",
        f"{month_en} {value.day}, {value.year}",
        f"{month_en_full} {value.day}, {value.year}",
        f"{value.day} {month_en} {value.year}",
    ]


def _schedule_date_text_matches(text: str, value: datetime) -> bool:
    compact = re.sub(r"\s+", "", str(text or "")).lower()
    for label in _schedule_date_labels(value):
        if re.sub(r"\s+", "", label).lower() in compact:
            return True
    numbers = [int(part) for part in re.findall(r"\d+", compact)]
    return numbers[:3] == [value.year, value.month, value.day]


def _select_upload_schedule(page, scheduled_at: str, timezone_name: str,
                            on_log: Callable) -> bool:
    """Expand Studio's Schedule card and set its date/time controls.

    The date trigger opens a calendar whose top field is a real
    ``tp-yt-paper-input``.  Fill that field exactly like the operator does:
    select all, paste the desired date, then confirm.  Stop before Schedule if
    Studio does not echo the exact target date back into the trigger.
    """
    try:
        target = datetime.strptime(str(scheduled_at or ""), "%Y-%m-%dT%H:%M")
    except ValueError:
        on_log("  ❌ 定时发布时间格式不正确")
        return False

    on_log(f"设置定时发布：{target:%Y-%m-%d %H:%M}（{timezone_name}）...")
    schedule_root = page.locator('ytcp-video-visibility-select #second-container').first
    try:
        schedule_root.wait_for(state="visible", timeout=10_000)
        title = schedule_root.locator('#visibility-title').first
        if title.is_visible(timeout=2_000):
            title.click()
        else:
            schedule_root.click()
        page.wait_for_timeout(500)
    except Exception as exc:
        on_log(f"  ❌ 未找到“安排时间”卡片: {exc}")
        return False

    date_trigger = schedule_root.locator('ytcp-dropdown-trigger').first
    try:
        date_trigger.wait_for(state="visible", timeout=8_000)
        current_date_text = date_trigger.inner_text(timeout=3_000).strip()
    except Exception as exc:
        on_log(f"  ❌ 未找到发布日期按钮: {exc}")
        return False

    on_log(f"  当前日期为“{current_date_text}”，打开日期输入面板...")
    target_date_formats = [
        f"{target.year}年{target.month}月{target.day}日",
        f"{target.year}/{target.month:02d}/{target.day:02d}",
    ]
    date_set = False
    last_date_text = current_date_text
    for date_text in target_date_formats:
        try:
            date_trigger.click(timeout=5_000)
            page.wait_for_timeout(400)

            # 图2：日历弹窗顶部的真实输入框。时间框仍在下方可见，
            # 因此排除值/占位符中带冒号的输入框。
            date_input = None
            focused = page.locator('input:focus').first
            try:
                focused_value = focused.input_value(timeout=800)
                if ":" not in focused_value:
                    date_input = focused
            except Exception:
                pass
            if date_input is None:
                inputs = page.locator('tp-yt-paper-input input:visible, input:visible')
                for index in range(min(inputs.count(), 30)):
                    candidate = inputs.nth(index)
                    try:
                        value = candidate.input_value(timeout=800)
                    except Exception:
                        continue
                    placeholder = str(candidate.get_attribute("placeholder") or "")
                    if ":" in value or ":" in placeholder:
                        continue
                    if re.search(r"\d{4}.*\d{1,2}.*\d{1,2}", value) or "年" in value or "/" in value:
                        date_input = candidate
                        break
            if date_input is None:
                raise RuntimeError("calendar date input not found")

            date_input.click()
            date_input.press("Meta+A" if sys.platform == "darwin" else "Control+A")
            page.keyboard.insert_text(date_text)
            date_input.press("Enter")
            page.wait_for_timeout(600)

            last_date_text = date_trigger.inner_text(timeout=3_000).strip()
            if _schedule_date_text_matches(last_date_text, target):
                on_log(f"  ✓ 已按指定格式填写发布日期：{last_date_text}")
                date_set = True
                break
            on_log(f"  日期格式“{date_text}”未被页面接受，尝试备用格式...")
        except Exception as exc:
            on_log(f"  日期格式“{date_text}”填写失败: {exc}")
            try:
                page.keyboard.press("Escape")
            except Exception:
                pass

    if not date_set:
        on_log(f"  ❌ 日期输入后校验失败，页面显示：{last_date_text}；已在点击预定前停止")
        return False

    time_input = None
    try:
        inputs = schedule_root.locator('tp-yt-paper-input input:visible, input:visible')
        for index in range(min(inputs.count(), 20)):
            candidate = inputs.nth(index)
            value = candidate.input_value(timeout=1_000)
            placeholder = str(candidate.get_attribute("placeholder") or "")
            aria = str(candidate.get_attribute("aria-label") or "")
            if ":" in value or ":" in placeholder or "时间" in aria or "time" in aria.lower():
                time_input = candidate
                break
        if time_input is None:
            raise RuntimeError("time input not found")
        time_input.click()
        time_input.press("Meta+A" if sys.platform == "darwin" else "Control+A")
        page.keyboard.insert_text(target.strftime("%H:%M"))
        time_input.press("Tab")
        page.wait_for_timeout(400)
        actual_time = time_input.input_value(timeout=2_000).strip()
        if actual_time[:5] != target.strftime("%H:%M"):
            on_log(f"  ❌ 时间输入后校验失败，页面显示：{actual_time}")
            return False
        on_log(f"  ✓ 已设置发布时间：{actual_time}")
    except Exception as exc:
        on_log(f"  ❌ 设置发布时间失败，已停止预定: {exc}")
        return False

    on_log(f"  ✓ 定时信息填写完成；时区使用 YouTube Studio 当前页面设置（目标 {timezone_name}）")
    return True


def _select_upload_visibility(page, visibility: str, on_log: Callable) -> bool:
    from playwright.sync_api import TimeoutError as PWTimeout

    visibility = (visibility or "PUBLIC").strip().upper()
    label = {"PUBLIC": "公开", "UNLISTED": "不公开", "PRIVATE": "私享"}.get(visibility, visibility)
    on_log(f"设置公开范围：{label}...")
    try:
        page.wait_for_selector(
            f'tp-yt-paper-radio-button[name="{visibility}"], '
            f'ytcp-video-visibility-select tp-yt-paper-radio-button[name="{visibility}"]',
            timeout=10_000
        )
        select_result = page.evaluate("""(name) => {
            const radios = Array.from(document.querySelectorAll(
                'ytcp-video-visibility-select tp-yt-paper-radio-button, tp-yt-paper-radio-button'
            ));
            const radio = radios.find(r => (r.getAttribute('name') || '').toUpperCase() === name);
            if (!radio) return 'not-found';
            const target =
                radio.querySelector('#radioContainer') ||
                radio.querySelector('#offRadio') ||
                radio.querySelector('#onRadio') ||
                (radio.shadowRoot && (
                    radio.shadowRoot.querySelector('#radioContainer') ||
                    radio.shadowRoot.querySelector('#offRadio') ||
                    radio.shadowRoot.querySelector('#onRadio')
                )) ||
                radio;
            target.click();
            radio.click();
            const checked =
                radio.getAttribute('aria-checked') === 'true' ||
                radio.hasAttribute('checked') ||
                radio.classList.contains('iron-selected');
            return checked ? 'selected' : 'clicked';
        }""", visibility)
        page.wait_for_timeout(300)
        on_log(f"  ✓ 已选择{label}（{select_result}）")
        return True
    except PWTimeout:
        on_log(f"  ⚠️ 未找到「{label}」选项，请手动选择")
        return False


def _find_enabled_upload_action_button(page, schedule_enabled: bool,
                                       visibility: str = "PUBLIC"):
    """Return Studio's currently visible and enabled publish action button.

    Newer Studio builds render the real action as a native ``button`` inside
    ``ytcp-button-shape``.  Older builds exposed ``ytcp-button#done-button``.
    Check the native button first so an already-enabled Schedule button is not
    left waiting on an obsolete host selector.
    """
    if schedule_enabled:
        selectors = (
            'ytcp-button#done-button button',
            'ytcp-button#done-button',
            '#done-button button',
            'button[aria-label="预定"]',
            'button[aria-label="Schedule"]',
            'ytcp-button-shape button:has-text("预定")',
            'ytcp-button-shape button:has-text("Schedule")',
        )
    elif (visibility or "").strip().upper() == "PUBLIC":
        selectors = (
            'button[aria-label="发布"]',
            'button[aria-label="Publish"]',
            'ytcp-button-shape button:has-text("发布")',
            'ytcp-button-shape button:has-text("Publish")',
            '#done-button button',
            'ytcp-button#done-button',
        )
    else:
        selectors = (
            'button[aria-label="保存"]',
            'button[aria-label="Save"]',
            'ytcp-button-shape button:has-text("保存")',
            'ytcp-button-shape button:has-text("Save")',
            '#done-button button',
            'ytcp-button#done-button',
        )

    for selector in selectors:
        try:
            candidates = page.locator(selector)
            for index in range(min(candidates.count(), 8)):
                candidate = candidates.nth(index)
                if candidate.is_visible(timeout=500) and candidate.is_enabled(timeout=500):
                    return candidate
        except Exception:
            continue
    return None


def _click_upload_action_button(page, button, label: str) -> bool:
    """Click the upload dialog's final action despite transient UI overlays."""
    try:
        button.scroll_into_view_if_needed(timeout=2_000)
    except Exception:
        pass

    try:
        button.click(timeout=5_000)
        return True
    except Exception:
        pass

    # Studio frequently swaps the button host while checks are starting.  A
    # real mouse click at the current native button's centre survives that
    # host replacement better than retrying the stale locator.
    try:
        box = button.bounding_box()
        if box and box.get("width", 0) > 0 and box.get("height", 0) > 0:
            page.mouse.click(
                box["x"] + box["width"] / 2,
                box["y"] + box["height"] / 2,
            )
            page.wait_for_timeout(500)
            return True
    except Exception:
        pass

    # Last fallback: reacquire the visible native action by its accessible
    # label, then dispatch the click in the page.
    try:
        clicked = page.evaluate("""(wanted) => {
            function visible(el) {
                if (!el || !el.getBoundingClientRect) return false;
                const r = el.getBoundingClientRect();
                return r.width > 0 && r.height > 0 &&
                    getComputedStyle(el).visibility !== 'hidden';
            }
            const labels = wanted === '预定'
                ? ['预定', 'Schedule']
                : (wanted === '发布' ? ['发布', 'Publish'] : ['保存', 'Save']);
            const buttons = Array.from(document.querySelectorAll(
                'ytcp-button-shape button, ytcp-button button, button'
            ));
            const target = buttons.find(btn => {
                if (!visible(btn)) return false;
                const text = (btn.innerText || btn.textContent || '').trim();
                const aria = (btn.getAttribute('aria-label') || '').trim();
                return labels.includes(text) || labels.includes(aria);
            });
            if (!target) return false;
            target.click();
            return true;
        }""", label)
        if clicked:
            page.wait_for_timeout(500)
            return True
    except Exception:
        pass
    return False


def _wait_for_upload_and_publish(page, job, visibility: str, on_log: Callable,
                                  on_progress: Callable,
                                  upload_timeout: int,
                                  stall_timeout_min: int = 10,
                                  upload_state: dict = None,
                                  schedule_enabled: bool = False) -> Optional[str]:
    from playwright.sync_api import TimeoutError as PWTimeout

    upload_state = upload_state or {}
    upload_state.setdefault("app_pct", 60)
    start = time.time()
    on_log("等待公开范围页左下角上传状态...")

    last_pct = int(upload_state.get("app_pct", 60) or 60)
    last_log_pct = -1
    last_activity = float(upload_state.get("last_activity") or time.time())   # 最近一次有变化的时间
    stall_sec = stall_timeout_min * 60 if stall_timeout_min > 0 else 0
    upload_complete = bool(upload_state.get("complete"))
    if upload_complete:
        on_log("前置监控已确认文件上传完成，立即进入公开范围设置")

    while True:
        if job.is_cancelled():
            on_log("上传已取消")
            return None

        if (time.time() - start) > upload_timeout / 1000:
            on_log("❌ 上传超时")
            return None

        # YouTube allows Schedule while upload/check processing continues.
        # Once date/time are valid and the real native button is enabled, click
        # it immediately instead of waiting for the progress text to reach 100%.
        if schedule_enabled:
            publish_btn = _find_enabled_upload_action_button(
                page, schedule_enabled=True, visibility=visibility
            )
            if publish_btn is not None:
                last_activity = time.time()
                on_log("检测到预定按钮，立即尝试点击（不等待后台检查完成）")
                break

        # 卡住检测：stall_timeout_min 分钟无任何进度变化
        if not upload_complete and stall_sec > 0 and (time.time() - last_activity) > stall_sec:
            on_log(f"⚠️ 上传已 {stall_timeout_min} 分钟无进度，判定为卡住")
            return _STALL

        if upload_complete:
            break

        if not upload_complete:
            info = _read_upload_progress(page)
            phase = info.get("phase") if info else None
            pct_result = info.get("pct") if info else None

            if phase in ("checking", "processing", "complete"):
                upload_complete = True
                upload_state["complete"] = True
                last_activity = time.time()
                on_log("检测到 YouTube 已进入上传完毕/检查/处理阶段，立即进入公开范围设置")
                break
            elif pct_result is not None and phase in ("uploading", "unknown"):
                mapped = min(82, 60 + int(pct_result * 0.22))
                if pct_result != last_log_pct:
                    on_log(f"上传进度: {pct_result}%")
                    last_log_pct = pct_result
                    last_activity = time.time()
                _emit_upload_progress(upload_state, mapped, on_progress)
                last_pct = max(last_pct, int(upload_state.get("app_pct", mapped)))
                if pct_result >= 100:
                    upload_complete = True
                    upload_state["complete"] = True
                    last_activity = time.time()
                    on_log("上传进度已到 100%，立即进入公开范围设置...")
                    break
                else:
                    page.wait_for_timeout(2000)
                    continue

        processing_result = page.evaluate("""() => {
            const t = document.body.innerText || '';
            const isProcessing = t.includes('正在进行高清处理') || t.includes('Processing') || t.includes('处理中');
            const isChecking   = t.includes('正在检查') || t.includes('仍在检查') || t.includes('Checking');
            return isProcessing || isChecking ? 'processing' : null;
        }""")
        if processing_result:
            last_activity = time.time()  # 处理中=有活动
            if last_pct < 82:
                on_log("视频处理/检查中，立即进入公开范围设置...")
                _emit_upload_progress(upload_state, 82, on_progress)
                last_pct = max(last_pct, int(upload_state.get("app_pct", 82)))
            break

        publish_btn = _find_enabled_upload_action_button(
            page, schedule_enabled=False, visibility=visibility
        )
        if publish_btn is not None:
            last_activity = time.time()
            break

        page.wait_for_timeout(2000)

    _emit_upload_progress(upload_state, 83, on_progress)
    on_log("上传完毕，准备点击预定..." if schedule_enabled else "上传完毕，准备点击发布/保存...")

    _action_label = "预定" if schedule_enabled else ("发布" if (visibility or "").strip().upper() == "PUBLIC" else "保存")
    on_log(f"点击{_action_label}...")
    _publish_clicked = False
    _publish_clicked_at = 0.0
    publish_btn = None
    _button_deadline = time.time() + 60
    while time.time() < _button_deadline and publish_btn is None:
        publish_btn = _find_enabled_upload_action_button(
            page, schedule_enabled=schedule_enabled, visibility=visibility
        )
        if publish_btn is None:
            page.wait_for_timeout(500)
    if publish_btn is None:
        on_log(f"⚠️ {_action_label}按钮在60秒内仍不可用")
        if schedule_enabled:
            # The video upload dialog can briefly replace the Schedule button
            # with a checking overlay.  Retrying the entire upload here can
            # create duplicate videos, so stop for a Studio review instead.
            on_log("⚠️ 已进入预定步骤但未确认按钮状态；为避免重复上传，不会重新上传此文件")
            return "SCHEDULE_UNCONFIRMED"
        return None
    try:
        if not _click_upload_action_button(page, publish_btn, _action_label):
            raise PWTimeout(f"{_action_label} click was not accepted")
        _publish_clicked = True
        _publish_clicked_at = time.time()
        page.wait_for_timeout(800)
    except PWTimeout:
        on_log(f"⚠️ {_action_label}按钮已显示可用，但点击未被页面接受")
        if schedule_enabled:
            on_log("⚠️ 已尝试预定；为避免重复上传，不会重新上传此文件，请到 Studio 检查")
            return "SCHEDULE_UNCONFIRMED"
        return None

    # 处理确认弹窗（"我们仍在检查你的视频" → 再次点击弹窗里的"发布"）
    confirm_result = None if schedule_enabled else _click_precheck_publish_dialog(page)
    if confirm_result and confirm_result.get("clicked"):
        if confirm_result.get("dismissed"):
            on_log("检测到检查确认弹窗，已点击发布并关闭弹窗")
        else:
            on_log("检测到检查确认弹窗，已尝试点击发布，等待 YouTube 响应...")
        _publish_clicked_at = time.time()
        page.wait_for_timeout(1500)
    elif confirm_result and confirm_result.get("visible"):
        on_log("检测到检查确认弹窗，等待弹窗里的发布按钮可用...")

    if schedule_enabled:
        schedule_notice = _click_schedule_checking_notice(page)
        if schedule_notice and schedule_notice.get("clicked"):
            on_log("检测到“我们仍在检查你的内容”，已点击“知道了”")
            page.wait_for_timeout(1200)

    video_id = None
    _dialog_closed = False  # 标记弹窗是否已成功关闭（弹窗关闭=发布/预约完成）

    # 处理"正在检查/正在处理"弹窗：完整流程可能会检查较久，持续重试弹窗里的发布按钮。
    # 背景：点"确认发布"后 YouTube 需要数秒才弹出此弹窗，且弹窗存在时 URL 不会跳转
    on_log("等待预约成功弹窗..." if schedule_enabled else "等待发布后弹窗或页面跳转...")
    _still_processing_deadline = time.time() + 600
    _precheck_wait_logged = False
    _last_precheck_click_log = 0.0
    _schedule_notice_wait_logged = False
    while time.time() < _still_processing_deadline:
        _dialog_visible = page.evaluate("""() => {
            const d = document.querySelector('ytcp-uploads-still-processing-dialog');
            return d && d.offsetParent !== null;
        }""")
        if _dialog_visible:
            on_log("检测到正在处理/上传弹窗，提取视频ID并关闭...")
            try:
                _href = page.evaluate("""() => {
                    const d = document.querySelector('ytcp-uploads-still-processing-dialog');
                    if (!d) return null;
                    const a = d.querySelector('a[href*="youtu.be"], a[href*="watch?v="]');
                    return a ? a.href : null;
                }""")
                if _href:
                    m = re.search(r"youtu\.be/([A-Za-z0-9_-]{11})|v=([A-Za-z0-9_-]{11})", _href)
                    if m:
                        video_id = m.group(1) or m.group(2)
                        on_log(f"  从弹窗取到 video_id: {video_id}")
            except Exception:
                pass
            closed = _close_still_processing_dialog(page)
            on_log(f"  关闭处理弹窗点击结果: {closed}")
            if closed and closed.get("dismissed"):
                page.wait_for_timeout(1500)
                on_log("  ✓ 已关闭正在处理/上传弹窗")
                _dialog_closed = True
                break
            on_log("  ⚠️ 正在处理/上传弹窗仍在，继续尝试关闭...")
            page.wait_for_timeout(2000)
            continue

        if schedule_enabled:
            schedule_notice = _click_schedule_checking_notice(page)
            if schedule_notice and schedule_notice.get("clicked"):
                on_log("检测到预约检查提示，已点击“知道了”，继续等待预约成功弹窗...")
                page.wait_for_timeout(1500)
                continue
            if schedule_notice and schedule_notice.get("visible"):
                if not _schedule_notice_wait_logged:
                    on_log("检测到预约检查提示，正在等待“知道了”按钮出现...")
                    _schedule_notice_wait_logged = True
                page.wait_for_timeout(1500)
                continue

        confirm_result = None if schedule_enabled else _click_precheck_publish_dialog(page)
        if confirm_result and confirm_result.get("clicked"):
            _publish_clicked_at = time.time()
            if confirm_result.get("dismissed"):
                on_log("检测到检查确认弹窗，已点击发布并关闭弹窗")
                page.wait_for_timeout(1500)
                continue
            now = time.time()
            if now - _last_precheck_click_log > 10:
                on_log("检测到检查确认弹窗，已尝试点击发布，继续等待弹窗关闭...")
                _last_precheck_click_log = now
            page.wait_for_timeout(2500)
            continue
        if confirm_result and confirm_result.get("visible"):
            if not _precheck_wait_logged:
                reason = confirm_result.get("reason") or "waiting"
                if reason == "button_not_found":
                    on_log("检查确认弹窗仍在，但暂未找到弹窗里的发布按钮，继续等待...")
                else:
                    on_log("检查确认弹窗仍在，继续等待 YouTube 检查或弹窗按钮出现...")
                _precheck_wait_logged = True
            page.wait_for_timeout(2000)
            continue

        # ── 检测"视频发布时间"分享弹窗（上传很快时直接跳过检查阶段会出现此弹窗）──
        _share_info = page.evaluate("""() => {
            const d = document.querySelector('ytcp-video-share-dialog');
            if (!d || d.offsetParent === null) return null;
            // 尝试从分享链接提取 video_id
            const a = d.querySelector('a[href*="youtu.be"], a[href*="youtube.com/shorts/"], a[href*="watch?v="]');
            const inputEl = d.querySelector('input[type="text"], .text-input');
            const href = a ? a.href : (inputEl ? inputEl.value : null);
            return { href: href };
        }""")
        if _share_info:
            on_log("检测到预约成功分享弹窗，提取视频ID并关闭..." if schedule_enabled else "检测到发布成功分享弹窗，提取视频ID并关闭...")
            _href = _share_info.get("href") or ""
            if _href:
                m = re.search(r"youtu\.be/([A-Za-z0-9_-]{11})|shorts/([A-Za-z0-9_-]{11})|v=([A-Za-z0-9_-]{11})", _href)
                if m:
                    video_id = m.group(1) or m.group(2) or m.group(3)
                    on_log(f"  从分享弹窗取到 video_id: {video_id}")
            # 关闭分享弹窗（点关闭按钮）
            closed = page.evaluate("""() => {
                const d = document.querySelector('ytcp-video-share-dialog');
                if (!d) return false;
                const btn = d.querySelector('button[aria-label="关闭"], button[aria-label="Close"], ytcp-button#close-button button, ytcp-button[id*="close"] button');
                if (btn) { btn.click(); return 'inner'; }
                const outer = d.querySelector('ytcp-button#close-button, ytcp-button[id*="close"]');
                if (outer) { outer.click(); return 'outer'; }
                return false;
            }""")
            on_log(f"  关闭分享弹窗: {closed}")
            page.wait_for_timeout(1500)
            on_log("  ✓ 已关闭分享弹窗，视频已预约" if schedule_enabled else "  ✓ 已关闭分享弹窗，视频已发布")
            _dialog_closed = True
            break

        _dialog_visible = page.evaluate("""() => {
            const d = document.querySelector('ytcp-uploads-still-processing-dialog');
            return d && d.offsetParent !== null;
        }""")
        if _dialog_visible:
            on_log("检测到上传中弹窗，提取视频ID并关闭...")
            # 提取 video_id
            try:
                _href = page.evaluate("""() => {
                    const d = document.querySelector('ytcp-uploads-still-processing-dialog');
                    if (!d) return null;
                    const a = d.querySelector('a[href*="youtu.be"], a[href*="watch?v="]');
                    return a ? a.href : null;
                }""")
                if _href:
                    m = re.search(r"youtu\.be/([A-Za-z0-9_-]{11})|v=([A-Za-z0-9_-]{11})", _href)
                    if m:
                        video_id = m.group(1) or m.group(2)
                        on_log(f"  从弹窗取到 video_id: {video_id}")
            except Exception:
                pass
            closed = _close_still_processing_dialog(page)
            on_log(f"  关闭弹窗点击结果: {closed}")
            if closed and closed.get("dismissed"):
                page.wait_for_timeout(1500)
                on_log("  ✓ 已关闭上传中弹窗")
                _dialog_closed = True
                break  # 继续走下面的方法取 video_id
            on_log("  ⚠️ 上传中弹窗仍在，继续尝试关闭...")
            page.wait_for_timeout(2000)
            continue
        # 检查 URL 是否已跳转（有时没有弹窗直接跳转）
        if re.search(r"studio\.youtube\.com/video/([A-Za-z0-9_-]{11})", page.url):
            break
        # 有些账号发布后不会弹分享/处理中弹窗，而是直接关闭上传窗口。
        # 只要发布按钮点击成功，上传窗口已关闭就视为发布完成，避免误判失败后刷新重传。
        if _publish_clicked and time.time() - _publish_clicked_at > 8:
            try:
                _upload_dialog_visible = page.evaluate("""() => {
                    const selectors = ['ytcp-uploads-dialog', 'ytcp-video-upload-dialog'];
                    return selectors.some(sel => {
                        const d = document.querySelector(sel);
                        return d && d.offsetParent !== null;
                    });
                }""")
                if not _upload_dialog_visible:
                    on_log("上传窗口已关闭，按预约成功处理" if schedule_enabled else "上传发布窗口已关闭，按发布成功处理")
                    _dialog_closed = True
                    break
            except Exception:
                pass
        page.wait_for_timeout(1000)

    if schedule_enabled and _publish_clicked and not video_id and not _dialog_closed:
        on_log("  ⚠️ 已点击预定，但在等待时间内没有确认到预约成功弹窗")
        return "SCHEDULE_UNCONFIRMED"

    # 方法1：等待 URL 跳转到 /video/VIDEO_ID（弹窗已关闭则跳过此等待）
    if not video_id and not _dialog_closed:
        try:
            page.wait_for_url(
                re.compile(r"studio\.youtube\.com/video/([A-Za-z0-9_-]{11})"),
                timeout=1_800_000
            )
            m = re.search(r"/video/([A-Za-z0-9_-]{11})", page.url)
            if m:
                video_id = m.group(1)
        except PWTimeout:
            pass

    # 方法2：页面上的"查看视频"链接
    if not video_id:
        try:
            link = page.locator('a[href*="youtube.com/watch?v="]').first
            href = link.get_attribute("href", timeout=8_000)
            m = re.search(r"v=([A-Za-z0-9_-]{11})", href)
            if m:
                video_id = m.group(1)
        except PWTimeout:
            pass

    # 方法3：当前 URL
    if not video_id:
        m = re.search(r"/video/([A-Za-z0-9_-]{11})", page.url)
        if m:
            video_id = m.group(1)

    # 弹窗已关闭说明发布成功，即使取不到 video_id 也不算失败
    if not video_id and _dialog_closed:
        video_id = "scheduled" if schedule_enabled else "published"
    if schedule_enabled and not video_id and _publish_clicked:
        # Do not let the outer retry loop upload a duplicate after Schedule was
        # clicked but Studio's success dialog could not be confirmed.
        video_id = "SCHEDULE_UNCONFIRMED"

    return video_id


# ─────────────────────────────────────────────────────────────
# 广告位注入
# ─────────────────────────────────────────────────────────────

def _run_ad_helper(page, video_id: str, interval: int, start: int,
                   on_log: Callable, timeout: int):
    from playwright.sync_api import TimeoutError as PWTimeout

    ad_url = f"https://studio.youtube.com/video/{video_id}/monetization/ads"
    on_log(f"打开广告位管理页: {ad_url}")
    page.goto(ad_url, timeout=timeout)
    page.wait_for_load_state("domcontentloaded", timeout=timeout)

    try:
        page.wait_for_selector(':text("中贴片广告位")', timeout=30_000)
    except PWTimeout:
        on_log("  ⚠️ 未找到中贴片广告位页面，请手动插入")
        return

    on_log(f"  广告位页面已就绪，开始自动插入（间隔={interval}s，起始={start}s）...")

    ad_script = r"""
(function(intervalSeconds, startTimeInput) {
    let isRunning = true;
    let currentStartTime = startTimeInput;
    let sameTimeCount = 0;
    let lastDetectedTime = null;

    function formatTime(totalSeconds) {
        const h = Math.floor(totalSeconds / 3600);
        const m = Math.floor((totalSeconds % 3600) / 60);
        const s = totalSeconds % 60;
        return `${h}:${String(m).padStart(2,'0')}:${String(s).padStart(2,'0')}:00`;
    }

    function findTimeInput() {
        const sels = [
            'input[type="text"][class*="ytcp-media-timestamp-input"]',
            'input.ytcp-media-timestamp-input',
            'input[maxlength="10"][type="text"]',
            'input[aria-label*="\u5c0f\u65f6"][aria-label*="\u5206\u949f"][aria-label*="\u79d2"]'
        ];
        for (const sel of sels) {
            const inputs = document.querySelectorAll(sel);
            for (let i = inputs.length - 1; i >= 0; i--) {
                if (inputs[i].offsetWidth > 0) return inputs[i];
            }
        }
        return null;
    }

    function getTimeFromPage(input) {
        try {
            const label = input.getAttribute('aria-label') || '';
            const m = label.match(/(\d+)\s*\u5c0f\u65f6\s*(\d+)\s*\u5206\u949f\s*(\d+)\s*\u79d2/);
            if (m) return +m[1]*3600 + +m[2]*60 + +m[3];
        } catch(e) {}
        return null;
    }

    function getTimeFromInput(input) {
        try {
            const parts = (input.value||'').trim().split(':');
            if (parts.length !== 4) return null;
            return +parts[0]*3600 + +parts[1]*60 + +parts[2];
        } catch(e) { return null; }
    }

    function setInputValue(input, value) {
        input.focus(); input.select();
        document.execCommand('insertText', false, value);
        ['input','change'].forEach(t => input.dispatchEvent(new Event(t, {bubbles:true})));
    }

    function clickInsertButton() {
        for (const btn of document.querySelectorAll('button,[role="button"],ytcp-button,div[class*="ytcpButtonShapeImpl"]')) {
            if ((btn.textContent||'').includes('\u63d2\u5165\u5e7f\u544a\u4f4d')) { btn.click(); return true; }
        }
        for (const btn of document.querySelectorAll('button,[role="button"]')) {
            const t = btn.textContent||'';
            if (t.includes('\u63d2\u5165') && !t.includes('\u4e86\u89e3\u8be6\u60c5')) { btn.click(); return true; }
        }
        return false;
    }

    function processInsert() {
        if (!isRunning) return;
        if (sameTimeCount >= 3) { window.__adHelperDone = true; return; }
        if (!document.body.innerText.includes('\u4e2d\u8d34\u7247\u5e7f\u544a\u4f4d')) {
            window.__adHelperDone = true; return;
        }
        const input = findTimeInput();
        if (!input) { setTimeout(processInsert, 200); return; }

        const pageTime = getTimeFromPage(input);
        if (pageTime !== null && (currentStartTime - intervalSeconds) > pageTime) {
            window.__adHelperDone = true; return;
        }

        const cur = getTimeFromInput(input);
        if (cur !== null) {
            if (lastDetectedTime === cur) sameTimeCount++;
            else { sameTimeCount = 0; lastDetectedTime = cur; }
            if (cur === currentStartTime) { currentStartTime += intervalSeconds; setTimeout(processInsert, 50); return; }
        }

        setInputValue(input, formatTime(currentStartTime));
        setTimeout(() => {
            if (getTimeFromInput(input) === currentStartTime) {
                clickInsertButton();
                currentStartTime += intervalSeconds;
                setTimeout(processInsert, 80);
            } else {
                setTimeout(processInsert, 150);
            }
        }, 40);
    }

    window.__adHelperDone = false;
    setTimeout(processInsert, 200);
})(%d, %d);
""" % (interval, start)

    page.evaluate(ad_script)
    on_log("  广告位脚本已注入，等待插入完成...")

    deadline = time.time() + 600
    while time.time() < deadline:
        if page.evaluate("() => !!window.__adHelperDone"):
            break
        page.wait_for_timeout(2000)

    on_log("  ✓ 广告位插入完成")

    try:
        save_btn = page.locator('ytcp-button:has-text("保存"), ytcp-button:has-text("Save")').first
        if save_btn.is_visible(timeout=5000):
            save_btn.click()
            page.wait_for_timeout(1500)
            on_log("  ✓ 广告位设置已保存")
    except Exception:
        on_log("  ⚠️ 未找到保存按钮，请手动保存")


# ─────────────────────────────────────────────────────────────
# 工具函数
# ─────────────────────────────────────────────────────────────

def _click_next(page, timeout: int):
    from playwright.sync_api import TimeoutError as PWTimeout
    try:
        page.locator(
            'ytcp-button#next-button:has-text("下一步"), '
            'ytcp-button#next-button:has-text("Next"), '
            'ytcp-button#next-button'
        ).first.click(timeout=timeout)
    except PWTimeout:
        page.click('ytcp-button:has-text("下一步"), ytcp-button:has-text("Next")',
                   timeout=timeout)


def _is_upload_visibility_page(page) -> bool:
    try:
        return bool(page.evaluate("""() => {
            return !!document.querySelector('ytcp-video-visibility-select') ||
                !!document.querySelector('tp-yt-paper-radio-button[name="PUBLIC"]');
        }"""))
    except Exception:
        return False


def _is_port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return True
    except OSError:
        return False


def _select_file_via_dialog(page, video_path: Path, on_log: Callable, timeout: int):
    """
    macOS/Linux 直接用 Playwright file chooser 设置本地路径。
    Windows 保留原生文件对话框注入逻辑，兼容原先的大文件流程。
    失败时返回 False，由上层决定是否重试整个上传流程。
    """
    video_path = Path(video_path).expanduser().resolve()
    if not video_path.exists():
        on_log(f"  ✗ 视频文件不存在: {video_path}")
        return False
    if os.name != "nt":
        return _select_file_with_playwright(page, video_path, on_log, timeout)
    import ctypes
    import ctypes.wintypes

    return _try_select_file_once(page, video_path, on_log, timeout)


def _close_stale_file_dialogs():
    """关闭所有残留的 #32770 文件选择对话框，防止重试时被旧对话框干扰。"""
    if os.name != "nt":
        return
    import ctypes
    import ctypes.wintypes
    WM_CLOSE = 0x0010
    user32 = ctypes.windll.user32
    WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM)
    found = []

    def _enum(hwnd, _):
        cls = ctypes.create_unicode_buffer(64)
        user32.GetClassNameW(hwnd, cls, 64)
        if cls.value == "#32770" and user32.IsWindowVisible(hwnd):
            found.append(hwnd)
        return True

    user32.EnumWindows(WNDENUMPROC(_enum), 0)
    for hwnd in found:
        user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)


def _select_file_with_playwright(page, video_path: Path, on_log: Callable, timeout: int) -> bool:
    from playwright.sync_api import TimeoutError as PWTimeout

    try:
        with page.expect_file_chooser(timeout=timeout) as chooser:
            page.click('ytcp-button#select-files-button, #select-files-button', timeout=timeout)
        chooser.value.set_files(str(video_path))
    except PWTimeout:
        try:
            file_input = page.locator('input[type="file"]').first
            file_input.set_input_files(str(video_path), timeout=10_000)
        except Exception as exc:
            on_log(f"  ✗ 无法设置上传文件: {exc}")
            return False
    except Exception as exc:
        if "Cannot transfer files larger than 50Mb" in str(exc):
            on_log("  检测到大视频，改用浏览器本机路径选择...")
            if not _set_local_file_via_cdp(page, video_path, on_log):
                return False
        else:
            on_log(f"  ✗ 文件选择失败: {exc}")
            return False

    try:
        page.wait_for_selector(
            'ytcp-video-metadata-editor #title-textarea',
            timeout=30_000,
        )
        on_log("  ✓ 文件已选择")
        return True
    except PWTimeout:
        on_log("  ✗ 文件选择后标题框未出现，将触发重试")
        return False


def _set_local_file_via_cdp(page, video_path: Path, on_log: Callable) -> bool:
    """Set a file path on the browser host without transferring file bytes.

    ``connect_over_cdp`` is marked remote by Playwright and ``set_files`` then
    tries to copy the whole file through its protocol, which has a 50 MB cap.
    The Chrome instance and this application are on the same Mac, so Chrome's
    native CDP command can safely receive the resolved local path directly.
    """
    session = None
    object_id = ""
    try:
        session = page.context.new_cdp_session(page)
        expression = r"""
(() => {
  const findFileInput = root => {
    if (!root || !root.querySelectorAll) return null;
    const direct = root.querySelector('input[type="file"]');
    if (direct) return direct;
    for (const element of root.querySelectorAll('*')) {
      if (element.shadowRoot) {
        const nested = findFileInput(element.shadowRoot);
        if (nested) return nested;
      }
    }
    return null;
  };
  return findFileInput(document);
})()
"""
        evaluated = session.send("Runtime.evaluate", {
            "expression": expression,
            "returnByValue": False,
            "awaitPromise": True,
        })
        result = evaluated.get("result") or {}
        object_id = str(result.get("objectId") or "")
        if not object_id or result.get("subtype") == "null":
            on_log("  ✗ 浏览器本机路径选择失败：未找到视频文件框")
            return False
        session.send("DOM.setFileInputFiles", {
            "objectId": object_id,
            "files": [str(Path(video_path).expanduser().resolve())],
        })
        on_log("  ✓ 大视频已通过本机路径选中")
        return True
    except Exception as exc:
        on_log(f"  ✗ 浏览器本机路径选择失败: {exc}")
        return False
    finally:
        if session is not None and object_id:
            try:
                session.send("Runtime.releaseObject", {"objectId": object_id})
            except Exception:
                pass


def _try_select_file_once(page, video_path: Path, on_log: Callable, timeout: int) -> bool:
    """尝试一次文件选择，成功返回 True"""
    import threading
    import ctypes
    import ctypes.wintypes
    import time as _time
    from playwright.sync_api import TimeoutError as PWTimeout

    file_str = str(video_path).replace("/", "\\")
    dialog_ok = threading.Event()
    dialog_found = [False]

    def _fill_dialog():
        user32 = ctypes.windll.user32
        WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM)
        WM_SETTEXT = 0x000C
        BM_CLICK = 0x00F5

        for _ in range(150):
            _time.sleep(0.1)
            hwnd_found = []

            def enum_cb(hwnd, _):
                cls = ctypes.create_unicode_buffer(64)
                user32.GetClassNameW(hwnd, cls, 64)
                if cls.value == "#32770" and user32.IsWindowVisible(hwnd):
                    hwnd_found.append(hwnd)
                return True

            user32.EnumWindows(WNDENUMPROC(enum_cb), 0)

            if hwnd_found:
                hwnd = hwnd_found[-1]
                _time.sleep(0.3)

                try:
                    user32.ShowWindow(hwnd, 9)
                    user32.SetForegroundWindow(hwnd)
                except Exception:
                    pass
                _time.sleep(0.3)

                # 主方法：WM_SETTEXT 直接写入 Edit 控件，不依赖全局键盘焦点
                edit_hwnd = None
                combo = user32.FindWindowExW(hwnd, None, "ComboBoxEx32", None)
                if combo:
                    combo2 = user32.FindWindowExW(combo, None, "ComboBox", None)
                    if combo2:
                        edit_hwnd = user32.FindWindowExW(combo2, None, "Edit", None)
                if not edit_hwnd:
                    edit_hwnd = user32.FindWindowExW(hwnd, None, "Edit", None)

                if edit_hwnd:
                    user32.SetFocus(edit_hwnd)
                    user32.SendMessageW(edit_hwnd, WM_SETTEXT, 0, file_str)
                    _time.sleep(0.2)
                    btn = user32.FindWindowExW(hwnd, None, "Button", None)
                    while btn:
                        buf = ctypes.create_unicode_buffer(64)
                        user32.GetWindowTextW(btn, buf, 64)
                        t_val = buf.value
                        if "Open" in t_val or "打开" in t_val or t_val.startswith("打"):
                            user32.SendMessageW(btn, BM_CLICK, 0, 0)
                            break
                        btn = user32.FindWindowExW(hwnd, btn, "Button", None)
                    else:
                        # 没找到按钮，用 Enter 键提交
                        VK_RETURN = 0x0D
                        user32.keybd_event(VK_RETURN, 0, 0, 0)
                        user32.keybd_event(VK_RETURN, 0, 2, 0)
                else:
                    # 极端 fallback：pyautogui（Edit 控件找不到时才用）
                    try:
                        import pyperclip
                        import pyautogui
                        pyperclip.copy(file_str)
                        _time.sleep(0.1)
                        pyautogui.hotkey("ctrl", "a")
                        _time.sleep(0.15)
                        pyautogui.hotkey("ctrl", "v")
                        _time.sleep(0.4)
                        pyautogui.press("enter")
                    except Exception:
                        pass

                dialog_found[0] = True
                dialog_ok.set()
                return

        dialog_ok.set()

    t = threading.Thread(target=_fill_dialog, daemon=True)
    t.start()

    _activate_chrome_window()
    _time.sleep(0.3)

    try:
        page.click('ytcp-button#select-files-button, #select-files-button', timeout=timeout)
    except PWTimeout:
        dialog_ok.set()
        on_log("  ✗ 未找到文件上传按钮")
        return False

    dialog_ok.wait(timeout=20)

    if not dialog_found[0]:
        on_log("  ✗ 文件选择对话框未出现（Chrome 在后台且无法激活，将触发重试）")
        return False

    try:
        page.wait_for_selector(
            'ytcp-video-metadata-editor #title-textarea',
            timeout=30_000
        )
        on_log("  ✓ 文件已选择")
        return True
    except PWTimeout:
        on_log("  ✗ 文件选择后标题框未出现，将触发重试")
        return False
