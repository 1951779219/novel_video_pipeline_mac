"""YouTube 浏览器上传：直接复用 drama_pipeline_release 的 stage5_upload_browser.py。
用 importlib 按文件路径加载，避免与本工程 app 包同名冲突。

要求：Chrome 已通过 --remote-debugging-port=9222 启动且登录 YouTube Studio。
macOS 可运行 scripts/start_chrome_debug_macos.command 启动调试 Chrome。
"""
import importlib.util
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Callable, Optional

_DRAMA_CANDIDATES = [
    Path(os.environ["NOVEL_UPLOAD_BROWSER_SCRIPT"]) if os.environ.get("NOVEL_UPLOAD_BROWSER_SCRIPT") else None,
    Path(__file__).resolve().parent / "vendor" / "stage5_upload_browser.py",
    Path(r"F:\Manao\drama_pipeline_app\app\stages\stage5_upload_browser.py"),
    Path(r"F:\Manao\drama_pipeline_release\app\stages\stage5_upload_browser.py"),
]

_module = None

def _load():
    global _module
    if _module is not None:
        return _module
    drama_file = next((p for p in _DRAMA_CANDIDATES if p and p.exists()), None)
    if drama_file is None:
        raise RuntimeError("找不到浏览器上传脚本: " + " / ".join(str(p) for p in _DRAMA_CANDIDATES))
    vendor_file = Path(__file__).resolve().parent / "vendor" / "stage5_upload_browser.py"
    module_name = "app.vendor.stage5_upload_browser" if drama_file.resolve() == vendor_file.resolve() else "app.vendor.stage5_upload_browser_external"
    spec = importlib.util.spec_from_file_location(module_name, drama_file)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    _module = mod
    return mod


@dataclass
class _Job:
    cancelled: bool = False
    mode: str = "full"

    def is_cancelled(self) -> bool:
        return self.cancelled


def upload_to_youtube(video_path: Path, title: str, description: str = "",
                       visibility: str = "PRIVATE",
                       cover_path: Optional[Path] = None,
                       flow: str = "simple",
                       upload_policy: str = "BTRA",
                       ad_interval: int = 60,
                       ad_start: int = 0,
                       chrome_profile: str = "Default",
                       ad_suitability_template: str = "",
                       browser_profiles: str = "[]",
                       browser_active_profile: str = "",
                       upload_all_profiles: bool = False,
                       force_profile_launch: bool = False,
                       auto_restart: bool = True,
                       stall_timeout_min: int = 10,
                       op_speed: str = "normal",
                       schedule_enabled: bool = False,
                       scheduled_at: str = "",
                       schedule_timezone: str = "Asia/Tokyo",
                       job: Optional[_Job] = None,
                       on_log: Callable = print,
                       on_progress: Callable = lambda x: None) -> Optional[str]:
    """visibility: PUBLIC / UNLISTED / PRIVATE"""
    mod = _load()

    fake_config = SimpleNamespace(
        browser_profiles=browser_profiles,
        browser_active_profile=browser_active_profile,
        browser_upload_policy=upload_policy,
        browser_ad_interval=int(ad_interval),
        browser_ad_start=int(ad_start),
        browser_visibility=visibility,
        browser_auto_restart=bool(auto_restart),
        browser_stall_timeout_min=int(stall_timeout_min),
        browser_op_speed=op_speed,
        browser_chrome_profile=chrome_profile or "Default",
        browser_upload_all_profiles=bool(upload_all_profiles),
        browser_ad_suitability_template=ad_suitability_template or "",
        youtube_description=description,
        youtube_schedule_enabled=bool(schedule_enabled),
        youtube_scheduled_at=str(scheduled_at or ""),
        youtube_schedule_timezone=str(schedule_timezone or "Asia/Tokyo"),
    )
    profile = {
        "name": browser_active_profile or "novel",
        "enabled": True,
        "chrome_profile": chrome_profile or "Default",
        "flow": flow,
        "upload_policy": upload_policy,
        "ad_interval": int(ad_interval), "ad_start": int(ad_start),
        "visibility": visibility,
        "description": description,
        "ad_suitability_template": ad_suitability_template or "",
        "schedule_enabled": bool(schedule_enabled),
        "scheduled_at": str(scheduled_at or ""),
        "schedule_timezone": str(schedule_timezone or "Asia/Tokyo"),
    }
    try:
        return mod.upload_via_browser(
            job or _Job(), fake_config, video_path, title, cover_path,
            on_log=on_log, on_progress=on_progress, profile=profile,
            force_profile_launch=bool(force_profile_launch),
        )
    except TypeError:
        orig = mod._get_active_profile
        mod._get_active_profile = lambda c: profile
        try:
            return mod.upload_via_browser(
                job or _Job(), fake_config, video_path, title, cover_path,
                on_log=on_log, on_progress=on_progress,
            )
        finally:
            mod._get_active_profile = orig
