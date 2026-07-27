"""Desktop production GUI for the novel video pipeline."""
from __future__ import annotations

import re
import json
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
import unicodedata
import webbrowser
from datetime import datetime, timedelta
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, simpledialog, ttk
from tkinter import font as tkfont
from zoneinfo import ZoneInfo

try:
    from app.dependency_manager import ensure_dependencies, report_to_lines

    _DEPENDENCY_STARTUP_REPORT = ensure_dependencies(scope="core")
except Exception as exc:
    ensure_dependencies = None  # type: ignore[assignment]
    report_to_lines = None  # type: ignore[assignment]
    _DEPENDENCY_STARTUP_REPORT = {
        "ok": False,
        "summary": f"核心依赖检测失败：{exc}",
        "logs": [f"[依赖检测] 核心依赖检测失败：{exc}"],
    }

from app import pipeline_runner as pr
from app import script_publish_scheduler as publish_scheduler
from app.api_probe import probe_image, probe_llm, probe_tts
from app.autotune import run_startup_autotune
from app.config import DATA_DIR, config
from app.config import API_KEY_FIELDS
from app.version import VERSION
from app.backends.tts import EDGE_MULTI_VOICES, VOICEVOX_FREQUENT_VOICES
from app.utils.secrets import clean_api_key, redact_secret_text


IMPORT_DIR = DATA_DIR / "imports"
UI_FONT = "PingFang SC" if sys.platform == "darwin" else "Microsoft YaHei UI"
MONO_FONT = "Menlo" if sys.platform == "darwin" else "Consolas"
UI_FONT_SIZE = 12
UI_SMALL_FONT_SIZE = 11
UI_HEADING_FONT_SIZE = 13

_DICTIONARY_SUFFIX_RE = re.compile(
    r"(?:[\s_.\-—－]*"
    r"(?:tts[\s_.\-—－]*)?"
    r"(?:读音词典|讀音詞典|发音词典|發音詞典|注音词典|注音詞典|词典|詞典|"
    r"読み方辞書|読み辞書|発音辞書|ふりがな辞書|辞書|"
    r"pronunciation(?:[\s_.\-—－]*dictionary)?|dictionary)"
    r")+$",
    re.I,
)
_TEXT_SUFFIX_RE = re.compile(
    r"(?:[\s_.\-—－]*"
    r"(?:最终文本|最終文本|最終テキスト|完成文本|完成稿|最终稿|最終稿|正文文本|正文|原文|本文|朗读文本|朗讀文本|"
    r"日语文本|日語文本|日本語テキスト|日语|日語|日本語|text)"
    r")+$",
    re.I,
)


def _normalized_batch_pair_key(path: str | Path) -> str:
    """Normalize body/dictionary filename suffixes to the same work key."""
    value = unicodedata.normalize("NFKC", Path(path).stem).strip()
    previous = None
    while value and value != previous:
        previous = value
        value = _DICTIONARY_SUFFIX_RE.sub("", value).strip()
        value = _TEXT_SUFFIX_RE.sub("", value).strip()
    return re.sub(r"[\W_]+", "", value, flags=re.UNICODE).casefold()


def _is_pronunciation_dictionary_filename(path: str | Path) -> bool:
    stem = unicodedata.normalize("NFKC", Path(path).stem).strip()
    return bool(_DICTIONARY_SUFFIX_RE.search(stem))


def _match_batch_texts_and_dictionaries(paths: list[str | Path]) -> dict:
    unique_paths = list(dict.fromkeys(Path(path).expanduser() for path in paths))
    dictionaries = [path for path in unique_paths if _is_pronunciation_dictionary_filename(path)]
    texts = [path for path in unique_paths if path not in dictionaries]
    dictionaries_by_key: dict[str, list[Path]] = {}
    for path in dictionaries:
        dictionaries_by_key.setdefault(_normalized_batch_pair_key(path), []).append(path)

    pairs: list[tuple[Path, Path]] = []
    unmatched_texts: list[Path] = []
    ambiguous_texts: list[tuple[Path, list[Path]]] = []
    used_dictionaries: set[Path] = set()
    for text_path in texts:
        candidates = dictionaries_by_key.get(_normalized_batch_pair_key(text_path), [])
        if len(candidates) == 1:
            pairs.append((text_path, candidates[0]))
            used_dictionaries.add(candidates[0])
        elif len(candidates) > 1:
            ambiguous_texts.append((text_path, candidates))
        else:
            unmatched_texts.append(text_path)
    return {
        "pairs": pairs,
        "texts": texts,
        "dictionaries": dictionaries,
        "unmatched_texts": unmatched_texts,
        "ambiguous_texts": ambiguous_texts,
        "unused_dictionaries": [path for path in dictionaries if path not in used_dictionaries],
    }


def _match_import_files(paths: list[str | Path], *, pair_within_parent: bool = False) -> dict:
    """Classify one multi-file import and pair body, MP3, and dictionary by name."""
    unique_paths = list(dict.fromkeys(Path(path).expanduser() for path in paths))
    txt_paths = [path for path in unique_paths if path.suffix.lower() == ".txt"]
    audio_paths = [path for path in unique_paths if path.suffix.lower() == ".mp3"]
    unsupported = [path for path in unique_paths if path not in txt_paths and path not in audio_paths]
    dictionary_paths = [path for path in txt_paths if _is_pronunciation_dictionary_filename(path)]
    text_paths = [path for path in txt_paths if path not in dictionary_paths]

    def by_key(items: list[Path]) -> dict[object, list[Path]]:
        result: dict[object, list[Path]] = {}
        for item in items:
            key: object = _normalized_batch_pair_key(item)
            if pair_within_parent:
                key = (str(item.parent.resolve(strict=False)), key)
            result.setdefault(key, []).append(item)
        return result

    audio_by_key = by_key(audio_paths)
    dictionary_by_key = by_key(dictionary_paths)
    pairs = []
    used_audio: set[Path] = set()
    used_dictionary: set[Path] = set()
    ambiguous: list[tuple[Path, str, list[Path]]] = []
    for text_path in text_paths:
        key: object = _normalized_batch_pair_key(text_path)
        if pair_within_parent:
            key = (str(text_path.parent.resolve(strict=False)), key)
        audio_candidates = audio_by_key.get(key, [])
        dictionary_candidates = dictionary_by_key.get(key, [])
        if len(audio_candidates) > 1:
            ambiguous.append((text_path, "MP3", audio_candidates))
            continue
        if len(dictionary_candidates) > 1:
            ambiguous.append((text_path, "词典", dictionary_candidates))
            continue
        audio_path = audio_candidates[0] if audio_candidates else None
        dictionary_path = dictionary_candidates[0] if dictionary_candidates else None
        if audio_path:
            used_audio.add(audio_path)
        if dictionary_path:
            used_dictionary.add(dictionary_path)
        pairs.append((text_path, audio_path, dictionary_path))
    return {
        "pairs": pairs,
        "ambiguous": ambiguous,
        "unused_audio": [path for path in audio_paths if path not in used_audio],
        "unused_dictionaries": [path for path in dictionary_paths if path not in used_dictionary],
        "unsupported": unsupported,
    }


def _choose_import_files_and_folders() -> list[Path]:
    """Open one macOS panel that accepts files, folders, or a mixture of both."""
    if sys.platform == "darwin":
        script = r'''
ObjC.import('AppKit');
(function () {
    var app = $.NSApplication.sharedApplication;
    app.setActivationPolicy($.NSApplicationActivationPolicyAccessory);
    app.activateIgnoringOtherApps(true);
    var panel = $.NSOpenPanel.openPanel;
    panel.canChooseFiles = true;
    panel.canChooseDirectories = true;
    panel.allowsMultipleSelection = true;
    panel.resolvesAliases = true;
    panel.level = $.NSNormalWindowLevel;
    panel.message = '选择小说 TXT、同名 MP3、读音词典或包含它们的文件夹';
    panel.prompt = '导入';
    panel.center;
    app.activateIgnoringOtherApps(true);
    var response = panel.runModal;
    if (response != $.NSModalResponseOK) return '';
    var urls = panel.URLs;
    var paths = [];
    for (var i = 0; i < urls.count; i++) {
        paths.push(ObjC.unwrap(urls.objectAtIndex(i).path));
    }
    return paths.join('\n');
})()
'''
        try:
            result = subprocess.run(
                ["osascript", "-l", "JavaScript", "-e", script],
                capture_output=True,
                text=True,
                check=False,
            )
        except OSError:
            result = None
        if result is not None:
            if result.returncode == 0:
                return [Path(line).expanduser() for line in result.stdout.splitlines() if line.strip()]
            error = str(result.stderr or "")
            if "User canceled" in error or "(-128)" in error:
                return []

    selected = filedialog.askopenfilenames(
        title="选择正文 TXT、同名 MP3 或读音词典",
        filetypes=[("Supported files", ("*.txt", "*.mp3")), ("All files", "*.*")],
    )
    return [Path(path).expanduser() for path in selected]


def _choose_import_folders() -> list[Path]:
    """Choose one or more folders on macOS, with a single-folder fallback elsewhere."""
    if sys.platform == "darwin":
        script = [
            'set chosenFolders to choose folder with prompt "选择一个或多个包含小说 TXT 的文件夹" with multiple selections allowed',
            "set chosenPaths to {}",
            "repeat with chosenFolder in chosenFolders",
            "set end of chosenPaths to POSIX path of chosenFolder",
            "end repeat",
            "set AppleScript's text item delimiters to linefeed",
            "return chosenPaths as text",
        ]
        command = ["osascript"]
        for line in script:
            command.extend(["-e", line])
        try:
            result = subprocess.run(command, capture_output=True, text=True, check=False)
        except OSError:
            result = None
        if result is not None:
            if result.returncode == 0:
                return [Path(line).expanduser() for line in result.stdout.splitlines() if line.strip()]
            error = str(result.stderr or "")
            if "(-128)" in error or "User canceled" in error or "用户已取消" in error:
                return []

    selected = filedialog.askdirectory(title="选择包含小说 TXT 的文件夹")
    return [Path(selected).expanduser()] if selected else []


def _remove_nested_import_folders(folders: list[Path]) -> tuple[list[Path], int]:
    """Avoid importing the same files twice when both a parent and its child are selected."""
    unique: list[Path] = []
    for folder in folders:
        try:
            normalized = folder.resolve()
        except OSError:
            normalized = folder.absolute()
        if normalized not in unique:
            unique.append(normalized)

    retained = [
        folder for folder in unique
        if not any(folder != other and folder.is_relative_to(other) for other in unique)
    ]
    return retained, len(unique) - len(retained)


class _TextValue:
    def __init__(self, widget: tk.Text):
        self.widget = widget

    def get(self) -> str:
        return self.widget.get("1.0", tk.END).strip()

    def set(self, value: str):
        self.widget.delete("1.0", tk.END)
        self.widget.insert("1.0", str(value or ""))


def _safe_name(value: str, fallback: str = "article") -> str:
    text = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", str(value or "").strip())
    text = re.sub(r"\s+", " ", text).strip(" .")
    return (text[:80].rstrip(" .") or fallback)


def _save_import_text(title: str, text: str) -> Path:
    IMPORT_DIR.mkdir(parents=True, exist_ok=True)
    name = _safe_name(title or next((x.strip() for x in text.splitlines() if x.strip()), ""), "article")
    path = IMPORT_DIR / f"{time.strftime('%Y%m%d_%H%M%S')}_{name}.txt"
    path.write_text(text.strip(), encoding="utf-8")
    return path


def _upload_profile_names() -> list[str]:
    return [profile["name"] for profile in _parse_upload_profiles()]


def _profile_bool(value, default: bool = True) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    text = str(value).strip().lower()
    if text in {"0", "false", "no", "off", "disabled", "关闭", "停用"}:
        return False
    if text in {"1", "true", "yes", "on", "enabled", "开启", "启用"}:
        return True
    return default


def _profile_int(value, default: int) -> int:
    try:
        return int(float(value))
    except Exception:
        return default


def _normalize_profile_flow(value: str) -> str:
    text = str(value or "").strip().lower()
    if "full" in text or "完整" in text or "创收" in text:
        return "full"
    return "simple"


def _normalize_upload_profile(profile: dict | None, index: int = 0) -> dict:
    profile = profile if isinstance(profile, dict) else {}
    visibility = str(profile.get("visibility") or config.get("youtube_visibility", "PRIVATE") or "PRIVATE").strip().upper()
    if visibility not in {"PRIVATE", "UNLISTED", "PUBLIC"}:
        visibility = "PRIVATE"
    publish_mode = str(profile.get("publish_mode") or config.get("youtube_publish_mode", "immediate") or "immediate").strip().lower()
    if publish_mode not in {"immediate", "youtube", "script"}:
        publish_mode = "youtube" if _profile_bool(profile.get("youtube_schedule_enabled"), False) else "immediate"
    return {
        "name": str(profile.get("name") or f"上传频道{index + 1}").strip() or f"上传频道{index + 1}",
        "enabled": _profile_bool(profile.get("enabled"), True),
        "chrome_profile": str(profile.get("chrome_profile") or config.get("browser_chrome_profile", "Default") or "Default").strip() or "Default",
        "flow": _normalize_profile_flow(str(profile.get("flow") or config.get("browser_flow", "simple"))),
        "visibility": visibility,
        "upload_policy": str(profile.get("upload_policy") or config.get("browser_upload_policy", "BTRA") or "BTRA").strip() or "BTRA",
        "ad_interval": _profile_int(profile.get("ad_interval") or config.get("browser_ad_interval", 60), 60),
        "ad_start": _profile_int(profile.get("ad_start") or config.get("browser_ad_start", 0), 0),
        "title_template": str(profile.get("title_template") or ""),
        "description": str(profile.get("description") or ""),
        "ad_suitability_template": profile.get("ad_suitability_template") or "",
        "publish_mode": publish_mode,
        "youtube_schedule_date": str(profile.get("youtube_schedule_date") or config.get("youtube_schedule_date", "") or ""),
        "youtube_schedule_time": str(profile.get("youtube_schedule_time") or config.get("youtube_schedule_time", "18:00") or "18:00"),
        "youtube_schedule_timezone": str(profile.get("youtube_schedule_timezone") or config.get("youtube_schedule_timezone", "Asia/Tokyo") or "Asia/Tokyo"),
        "script_schedule_first_date": str(profile.get("script_schedule_first_date") or config.get("script_schedule_first_date", "") or ""),
        "script_schedule_time": str(profile.get("script_schedule_time") or config.get("script_schedule_time", "18:00") or "18:00"),
        "script_schedule_interval_hours": _profile_int(profile.get("script_schedule_interval_hours") or config.get("script_schedule_interval_hours", 24), 24),
        "script_schedule_timezone": str(profile.get("script_schedule_timezone") or config.get("script_schedule_timezone", "Asia/Tokyo") or "Asia/Tokyo"),
        "script_schedule_missed_action": str(profile.get("script_schedule_missed_action") or config.get("script_schedule_missed_action", "next_slot") or "next_slot"),
        "script_schedule_unfinished_action": str(profile.get("script_schedule_unfinished_action") or config.get("script_schedule_unfinished_action", "next_slot") or "next_slot"),
        "script_manual_queue": _profile_bool(profile.get("script_manual_queue"), False),
    }


def _parse_upload_profiles(raw: str | None = None) -> list[dict]:
    try:
        profiles = json.loads(str(raw if raw is not None else config.get("browser_profiles", "[]") or "[]"))
    except Exception:
        profiles = []
    rows = [_normalize_upload_profile(p, idx) for idx, p in enumerate(profiles) if isinstance(p, dict)]
    if rows:
        return rows
    return [
        _normalize_upload_profile({"name": "无创收精简流程", "flow": "simple", "visibility": "PUBLIC"}, 0),
        _normalize_upload_profile({"name": "完整创收流程", "flow": "full", "visibility": "PUBLIC"}, 1),
    ]


def _upload_ad_template_to_text(value) -> str:
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, indent=2)
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        return json.dumps(json.loads(text), ensure_ascii=False, indent=2)
    except Exception:
        return text


def _upload_ad_template_from_text(text: str):
    text = str(text or "").strip()
    if not text:
        return ""
    try:
        return json.loads(text)
    except Exception:
        return text


UPLOAD_VISIBILITY_LABELS = {
    "PRIVATE": "PRIVATE 私享",
    "UNLISTED": "UNLISTED 不公开链接",
    "PUBLIC": "PUBLIC 公开",
}
UPLOAD_VISIBILITY_VALUES = {label: value for value, label in UPLOAD_VISIBILITY_LABELS.items()}


def _split_numeric_date(value: str, fallback: datetime | None = None) -> tuple[str, str, str]:
    fallback = fallback or (datetime.now() + timedelta(days=1))
    match = re.fullmatch(r"\s*(\d{4})\D+(\d{1,2})\D+(\d{1,2})\D*\s*", str(value or ""))
    if not match:
        return str(fallback.year), str(fallback.month), str(fallback.day)
    return match.group(1), str(int(match.group(2))), str(int(match.group(3)))


def _split_numeric_time(value: str, fallback_hour: int = 18) -> tuple[str, str]:
    match = re.fullmatch(r"\s*(\d{1,2})(?:\D+(\d{1,2}))?\s*", str(value or ""))
    if not match:
        return str(fallback_hour), "0"
    return str(int(match.group(1))), str(int(match.group(2) or 0))


def _future_numeric_date_parts(value: str) -> tuple[str, str, str]:
    fallback = datetime.now() + timedelta(days=1)
    year, month, day = _split_numeric_date(value, fallback)
    try:
        if datetime(int(year), int(month), int(day)).date() < datetime.now().date():
            return str(fallback.year), str(fallback.month), str(fallback.day)
    except ValueError:
        return str(fallback.year), str(fallback.month), str(fallback.day)
    return year, month, day


def _datetime_from_numeric_parts(year: str, month: str, day: str, hour: str, minute: str) -> datetime:
    values = (year, month, day, hour, minute)
    if not all(str(value).strip().isdigit() for value in values):
        raise ValueError("年、月、日、时、分都只能填写数字。")
    try:
        return datetime(*(int(value) for value in values))
    except ValueError as exc:
        raise ValueError("日期或时间不存在，请检查年、月、日、时、分。") from exc


class PipelineGUI:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title(f"小说推文视频生产台 v{VERSION}")
        self.root.geometry("1320x820")
        self.root.minsize(1050, 680)
        self._selected_job = ""
        self._search_refs: dict[str, str] = {}
        self._search_seq = 0
        self._search_result_queue: queue.Queue = queue.Queue()
        self._dependency_check_running = False
        self._script_upload_running = False
        self._image_failure_prompted: set[str] = set()
        self._pronunciation_resolution_cache: dict[str, dict[str, str]] = {}
        self._build_style()
        self._build_ui()
        self._poll_jobs()
        self._poll_log()
        self.root.after(350, self._emit_dependency_startup_logs)
        self.root.after(1600, self._maybe_start_dependency_check)
        self.root.after(700, self._maybe_start_hardware_autotune)
        self.root.after(2400, self._maybe_check_update_on_startup)
        self.root.after(3500, self._poll_script_publish_queue)

    def _build_style(self):
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except Exception:
            pass
        style.configure(".", font=(UI_FONT, UI_FONT_SIZE))
        style.configure("Treeview", rowheight=34, font=(UI_FONT, UI_FONT_SIZE))
        style.configure("Treeview.Heading", font=(UI_FONT, UI_FONT_SIZE, "bold"))
        style.configure("Primary.TButton", font=(UI_FONT, UI_FONT_SIZE, "bold"))

    def _build_ui(self):
        outer = ttk.PanedWindow(self.root, orient=tk.HORIZONTAL)
        outer.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)

        left = ttk.Frame(outer)
        right = ttk.Frame(outer)
        outer.add(left, weight=3)
        outer.add(right, weight=2)

        left_pane = ttk.PanedWindow(left, orient=tk.VERTICAL)
        self._left_pane = left_pane
        left_pane.pack(fill=tk.BOTH, expand=True)
        source_area = ttk.Frame(left_pane)
        queue_area = ttk.Frame(left_pane)
        left_pane.add(source_area, weight=1)
        left_pane.add(queue_area, weight=1)

        self._build_source_panel(source_area)
        self._build_queue_panel(queue_area)
        self._build_right_panel(right)

        def set_default_left_split():
            left_pane.update_idletasks()
            height = left_pane.winfo_height()
            if height > 100:
                left_pane.sashpos(0, height // 2)

        self.root.after_idle(set_default_left_split)

    def _build_source_panel(self, parent):
        notebook = ttk.Notebook(parent)
        self._source_notebook = notebook
        notebook.pack(fill=tk.X, pady=(0, 8))

        library_tab = ttk.Frame(notebook, padding=8)
        file_tab = ttk.Frame(notebook, padding=8)
        category_tab = ttk.Frame(notebook, padding=8)
        notebook.add(library_tab, text="书库导入")
        notebook.add(file_tab, text="文件导入")
        notebook.add(category_tab, text="任务分类")
        self._source_library_tab = library_tab
        self._source_file_tab = file_tab
        self._source_category_tab = category_tab
        self._task_category_active = False
        self._task_category_paths: dict[str, str] = {}

        search_row = ttk.Frame(library_tab)
        search_row.pack(fill=tk.X, pady=(0, 6))
        ttk.Label(search_row, text="来源").pack(side=tk.LEFT)
        self.source_mode_var = tk.StringVar(value="fanqie")
        ttk.Radiobutton(search_row, text="番茄小说", variable=self.source_mode_var, value="fanqie").pack(side=tk.LEFT, padx=(6, 0))
        ttk.Radiobutton(search_row, text="日本文库", variable=self.source_mode_var, value="japanese").pack(side=tk.LEFT, padx=(4, 8))
        ttk.Label(search_row, text="小说名").pack(side=tk.LEFT)
        self.search_var = tk.StringVar()
        search_entry = ttk.Entry(search_row, textvariable=self.search_var, width=28)
        search_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=6)
        search_entry.bind("<Return>", lambda _event: self._search_books())
        self.search_button = ttk.Button(search_row, text="搜索", command=self._search_books)
        self.search_button.pack(side=tk.LEFT)

        result_cols = ("title", "author", "source", "type", "paid", "latest", "ref")
        result_box = ttk.Frame(library_tab)
        result_box.pack(fill=tk.BOTH, expand=True)
        result_box.columnconfigure(0, weight=1)
        result_box.rowconfigure(0, weight=1)
        result_vscroll = ttk.Scrollbar(result_box, orient=tk.VERTICAL)
        result_hscroll = ttk.Scrollbar(result_box, orient=tk.HORIZONTAL)
        self.search_tree = ttk.Treeview(
            result_box,
            columns=result_cols,
            show="headings",
            height=6,
            selectmode="extended",
            xscrollcommand=result_hscroll.set,
            yscrollcommand=result_vscroll.set,
        )
        result_vscroll.configure(command=self.search_tree.yview)
        result_hscroll.configure(command=self.search_tree.xview)
        for col, text, width in [
            ("title", "书名", 180),
            ("author", "作者", 90),
            ("source", "来源", 70),
            ("type", "类型", 150),
            ("paid", "付费", 90),
            ("latest", "最新章节", 180),
            ("ref", "引用", 260),
        ]:
            self.search_tree.heading(col, text=text)
            self.search_tree.column(col, width=width, anchor=tk.W)
        self.search_tree.grid(row=0, column=0, sticky="nsew")
        result_vscroll.grid(row=0, column=1, sticky="ns")
        result_hscroll.grid(row=1, column=0, sticky="ew")

        library_actions = ttk.Frame(library_tab)
        library_actions.pack(fill=tk.X, pady=(6, 0))
        ttk.Label(
            library_actions,
            text="可按住 Command 多选搜索结果",
            foreground="#666",
        ).pack(side=tk.LEFT)
        ttk.Button(
            library_actions,
            text="加入选中书籍",
            command=self._add_selected_search,
            style="Primary.TButton",
        ).pack(side=tk.RIGHT)

        import_row = ttk.Frame(file_tab)
        import_row.pack(fill=tk.X)
        import_button = ttk.Button(
            import_row,
            text="导入文件 / 文件夹…",
            style="Primary.TButton",
            command=self._import_files_or_folders,
        )
        import_button.pack(side=tk.LEFT)
        ttk.Label(
            import_row,
            text="自动识别正文 TXT、同名 MP3 和同名读音词典",
            foreground="#666",
        ).pack(side=tk.LEFT, padx=(10, 0))

        self.pronunciation_dictionary_var = tk.StringVar()
        dictionary_status = ttk.Frame(file_tab)
        dictionary_status.pack(fill=tk.X, pady=(6, 0))
        ttk.Label(dictionary_status, text="当前读音词典").pack(side=tk.LEFT)
        ttk.Entry(
            dictionary_status,
            textvariable=self.pronunciation_dictionary_var,
            state="readonly",
        ).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(6, 0))

        ttk.Separator(file_tab, orient=tk.HORIZONTAL).pack(fill=tk.X, pady=8)
        manual_row = ttk.Frame(file_tab)
        manual_row.pack(fill=tk.X)
        ttk.Label(manual_row, text="标题").pack(side=tk.LEFT)
        self.manual_title = tk.StringVar()
        ttk.Entry(manual_row, textvariable=self.manual_title, width=28).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=6)
        ttk.Button(
            manual_row,
            text="加入粘贴文本",
            command=self._add_manual_text,
        ).pack(side=tk.LEFT)

        ttk.Label(file_tab, text="粘贴正文").pack(anchor=tk.W, pady=(6, 0))
        self.manual_text = scrolledtext.ScrolledText(file_tab, height=4, wrap=tk.WORD, font=(UI_FONT, UI_FONT_SIZE))
        self.manual_text.pack(fill=tk.X, pady=(2, 0))

        category_source_row = ttk.Frame(category_tab)
        category_source_row.pack(fill=tk.X)
        ttk.Label(category_source_row, text="文本来源目录").pack(side=tk.LEFT)
        self.task_category_root_var = tk.StringVar(value=str(config.get("task_category_root", "") or ""))
        ttk.Entry(
            category_source_row,
            textvariable=self.task_category_root_var,
            state="readonly",
        ).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=6)
        ttk.Button(
            category_source_row,
            text="选择目录…",
            command=self._choose_task_category_root,
        ).pack(side=tk.LEFT)
        ttk.Button(
            category_source_row,
            text="全部来源",
            command=self._clear_task_category_root,
        ).pack(side=tk.LEFT, padx=(6, 0))

        category_controls = ttk.Frame(category_tab)
        category_controls.pack(fill=tk.X, pady=(6, 0))
        ttk.Label(category_controls, text="排序").pack(side=tk.LEFT)
        saved_category_sort = str(config.get("task_category_sort", "名称自然排序") or "名称自然排序")
        if saved_category_sort not in {"名称自然排序", "添加日期", "文本修改日期", "任务更新时间", "制作阶段"}:
            saved_category_sort = "名称自然排序"
        self.task_category_sort_var = tk.StringVar(value=saved_category_sort)
        sort_combo = ttk.Combobox(
            category_controls,
            textvariable=self.task_category_sort_var,
            values=["名称自然排序", "添加日期", "文本修改日期", "任务更新时间", "制作阶段"],
            state="readonly",
            width=14,
        )
        sort_combo.pack(side=tk.LEFT, padx=(6, 0))
        saved_category_direction = str(config.get("task_category_direction", "升序") or "升序")
        if saved_category_direction not in {"升序", "降序"}:
            saved_category_direction = "升序"
        self.task_category_direction_var = tk.StringVar(value=saved_category_direction)
        direction_combo = ttk.Combobox(
            category_controls,
            textvariable=self.task_category_direction_var,
            values=["升序", "降序"],
            state="readonly",
            width=7,
        )
        direction_combo.pack(side=tk.LEFT, padx=6)
        ttk.Button(
            category_controls,
            text="选择当前显示的全部任务",
            command=self._select_all_visible_jobs,
        ).pack(side=tk.RIGHT)
        sort_combo.bind("<<ComboboxSelected>>", self._on_task_category_sort_changed)
        direction_combo.bind("<<ComboboxSelected>>", self._on_task_category_sort_changed)

        category_tree_box = ttk.Frame(category_tab)
        category_tree_box.pack(fill=tk.BOTH, expand=True, pady=(6, 0))
        category_tree_box.columnconfigure(0, weight=1)
        category_tree_box.rowconfigure(0, weight=1)
        category_scroll = ttk.Scrollbar(category_tree_box, orient=tk.VERTICAL)
        self.task_category_tree = ttk.Treeview(
            category_tree_box,
            columns=("count",),
            show="tree headings",
            height=6,
            selectmode="browse",
            yscrollcommand=category_scroll.set,
        )
        category_scroll.configure(command=self.task_category_tree.yview)
        self.task_category_tree.heading("#0", text="来源目录分类")
        self.task_category_tree.heading("count", text="任务数")
        self.task_category_tree.column("#0", width=340, minwidth=120, stretch=True)
        self.task_category_tree.column("count", width=70, minwidth=55, stretch=False, anchor=tk.CENTER)
        self.task_category_tree.grid(row=0, column=0, sticky="nsew")
        category_scroll.grid(row=0, column=1, sticky="ns")
        self.task_category_tree.bind("<<TreeviewSelect>>", self._on_task_category_selected)

        def resize_source_notebook(_event=None):
            selected = notebook.select()
            if not selected:
                return
            left_pane = getattr(self, "_left_pane", None)
            previous_sash = None
            if left_pane is not None and left_pane.winfo_height() > 100:
                try:
                    previous_sash = left_pane.sashpos(0)
                except tk.TclError:
                    pass
            page = notebook.nametowidget(selected)
            page.update_idletasks()
            notebook.configure(height=page.winfo_reqheight())
            if previous_sash is not None:
                self.root.after_idle(lambda: left_pane.sashpos(0, previous_sash))
            category_active = page is category_tab
            if category_active != self._task_category_active:
                self._task_category_active = category_active
                if hasattr(self, "job_tree"):
                    self.job_tree.selection_remove(self.job_tree.selection())
                    self._selected_job = ""
                    if category_active:
                        self._rebuild_task_category_tree()
                    self._refresh_jobs()

        notebook.bind("<<NotebookTabChanged>>", resize_source_notebook)
        self.root.after_idle(resize_source_notebook)

    def _build_queue_panel(self, parent):
        box = ttk.LabelFrame(parent, text="任务队列", padding=8)
        box.pack(fill=tk.BOTH, expand=True)

        queue_pane = ttk.PanedWindow(box, orient=tk.VERTICAL)
        queue_pane.pack(fill=tk.BOTH, expand=True)

        toolbar_box = ttk.Frame(queue_pane, padding=4)
        ttk.Button(toolbar_box, text="启动全部待处理", command=self._start_all_pending, style="Primary.TButton").pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(toolbar_box, text="清空已结束任务", command=self._delete_finished_jobs).pack(side=tk.LEFT)
        ttk.Button(toolbar_box, text="停止一切任务", command=self._stop_all_jobs).pack(side=tk.LEFT, padx=(6, 0))

        cols = ("job_id", "stage", "progress", "worker", "audio", "dictionary", "title", "video", "youtube")
        tree_box = ttk.Frame(queue_pane)
        queue_pane.add(toolbar_box, weight=0)
        queue_pane.add(tree_box, weight=1)
        tree_box.columnconfigure(0, weight=1)
        tree_box.rowconfigure(0, weight=1)
        tree_vscroll = ttk.Scrollbar(tree_box, orient=tk.VERTICAL)
        tree_hscroll = ttk.Scrollbar(tree_box, orient=tk.HORIZONTAL)
        self.job_tree = ttk.Treeview(
            tree_box,
            columns=cols,
            show="headings",
            selectmode="extended",
            xscrollcommand=tree_hscroll.set,
            yscrollcommand=tree_vscroll.set,
        )
        tree_vscroll.configure(command=self.job_tree.yview)
        tree_hscroll.configure(command=self.job_tree.xview)
        headings = {
            "job_id": "任务ID",
            "stage": "制作阶段",
            "progress": "进度",
            "worker": "Worker",
            "audio": "音频来源",
            "dictionary": "读音词典",
            "title": "标题",
            "video": "视频",
            "youtube": "YouTube",
        }
        widths = {
            "job_id": 95,
            "stage": 88,
            "progress": 56,
            "worker": 72,
            "audio": 74,
            "dictionary": 82,
            "title": 130,
            "video": 140,
            "youtube": 140,
        }
        min_widths = {
            "job_id": 58,
            "stage": 52,
            "progress": 44,
            "worker": 50,
            "audio": 52,
            "dictionary": 58,
            "title": 60,
            "video": 60,
            "youtube": 60,
        }
        for col in cols:
            self.job_tree.heading(col, text=headings[col])
            self.job_tree.column(
                col,
                width=widths[col],
                minwidth=min_widths[col],
                stretch=False,
                anchor=tk.W,
            )
        self.job_tree.grid(row=0, column=0, sticky="nsew")
        tree_vscroll.grid(row=0, column=1, sticky="ns")
        tree_hscroll.grid(row=1, column=0, sticky="ew")
        self.job_tree.bind("<<TreeviewSelect>>", self._on_job_select)
        self.job_tree.bind("<Button-3>", self._show_job_context_menu)
        self.job_tree.bind("<Button-2>", self._show_job_context_menu)
        self.job_tree.bind("<Control-Button-1>", self._show_job_context_menu)
        self.job_tree.bind(
            "<ButtonRelease-1>",
            lambda _event: self.root.after_idle(self._refresh_job_table_display),
            add="+",
        )
        self.job_context_menu = tk.Menu(self.root, tearoff=False)
        for label, command in (
            ("套用当前方案到选中任务", self._apply_profile_to_selected_jobs),
            ("启动选中", self._start_selected),
            ("继续选中任务", self._continue_selected),
            ("加入排队序列", self._enqueue_selected_jobs),
            ("停止选中", self._stop_selected_jobs),
            ("清除图片缓存", self._clear_selected_media_cache),
        ):
            self.job_context_menu.add_command(label=label, command=command)
        self.job_schedule_menu = tk.Menu(self.job_context_menu, tearoff=False)
        self.job_schedule_menu.add_command(label="油管内定时", command=self._schedule_selected_jobs_on_youtube)
        self.job_schedule_menu.add_command(label="脚本内定时", command=self._schedule_selected_jobs_in_script)
        self.job_context_menu.add_cascade(label="定时任务", menu=self.job_schedule_menu)
        # A cascade opens to the right on hover, keeping the everyday actions
        # compact while grouping every recovery/retry path in one Adobe-like menu.
        self.job_retry_menu = tk.Menu(self.job_context_menu, tearoff=False)
        for label, command in (
            ("全部重试（重新生图）", self._retry_all),
            ("重做 TTS", self._redo_tts),
            ("指定段配音修复", self._retry_tts_segments),
            ("重试（生图除外）", self._retry_from_clean_reuse_images),
            ("仅重试合成", self._compose_selected),
        ):
            self.job_retry_menu.add_command(label=label, command=command)
        self.job_context_menu.add_cascade(label="重试", menu=self.job_retry_menu)
        self.job_context_menu.add_separator()
        self.job_context_menu.add_command(
            label="附加读音词典…",
            command=self._choose_pronunciation_dictionary,
        )
        self.job_context_menu.add_command(label="打开任务文件夹", command=self._open_selected_output_dir)
        self.job_context_menu.add_command(label="删除选中任务", command=self._delete_selected_jobs)
        self.job_context_menu.add_separator()
        self.job_context_menu.add_command(label="刷新", command=self._refresh_jobs)
        self._job_tree_hscroll = tree_hscroll
        self._job_tree_vscroll = tree_vscroll

    def _build_right_panel(self, parent):
        notebook = ttk.Notebook(parent)
        self._right_notebook = notebook
        notebook.pack(fill=tk.BOTH, expand=True)

        log_tab = ttk.Frame(notebook, padding=6)
        cfg_tab = ttk.Frame(notebook, padding=8)
        notebook.add(log_tab, text="实时日志")
        notebook.add(cfg_tab, text="流水线配置")
        try:
            from app.update_tab import UpdateTab

            self.update_tab = UpdateTab(notebook, config)
            notebook.add(self.update_tab.frame, text="软件更新")
        except Exception as exc:
            self.update_tab = None
            self._append_probe_log(f"[软件更新] 更新页加载失败：{exc}")

        self.log_text = scrolledtext.ScrolledText(log_tab, wrap=tk.WORD, font=(MONO_FONT, UI_SMALL_FONT_SIZE))
        self.log_text.pack(fill=tk.BOTH, expand=True)

        self._build_config_panel(cfg_tab)

    def _build_config_panel(self, parent):
        self.vars: dict[str, tk.Variable] = {}
        self.combo_widgets: dict[str, ttk.Combobox] = {}
        self.entry_widgets: dict[str, ttk.Entry] = {}
        self._config_sections: dict[str, dict] = {}
        self._model_mirrors: dict[str, dict] = {}
        self._secret_widgets: dict[str, dict] = {}

        footer = ttk.Frame(parent)
        footer.pack(side=tk.BOTTOM, fill=tk.X, pady=(6, 0))
        ttk.Button(footer, text="保存到当前方案", style="Primary.TButton", command=self._save_profile).pack(side=tk.RIGHT)
        ttk.Button(footer, text="套用此方案到选中任务", command=self._apply_profile_to_selected_jobs).pack(side=tk.RIGHT, padx=(0, 6))

        scroll_area = ttk.Frame(parent)
        scroll_area.pack(side=tk.TOP, fill=tk.BOTH, expand=True)
        canvas = tk.Canvas(scroll_area, highlightthickness=0)
        scroll = ttk.Scrollbar(scroll_area, orient=tk.VERTICAL, command=canvas.yview)
        body = ttk.Frame(canvas)
        # The section heading that has just scrolled past the top is repeated
        # here.  Keeping this widget outside the canvas makes it genuinely
        # fixed while the settings body scrolls.
        sticky_header = ttk.Frame(scroll_area, relief=tk.RIDGE, borderwidth=1, padding=(9, 5))
        sticky_title = ttk.Label(
            sticky_header,
            font=(UI_FONT, UI_HEADING_FONT_SIZE, "bold"),
            cursor="hand2",
        )
        sticky_title.pack(side=tk.LEFT, fill=tk.X, expand=True)
        sticky_arrow = ttk.Label(sticky_header, width=3, anchor=tk.CENTER, cursor="hand2")
        sticky_arrow.pack(side=tk.RIGHT)
        sticky_state: dict[str, dict | None] = {"section": None}
        body.bind("<Configure>", lambda _event: canvas.configure(scrollregion=canvas.bbox("all")))
        body_window = canvas.create_window((0, 0), window=body, anchor="nw")

        def update_sticky_header():
            """Show the current settings section title at the top while scrolling."""
            canvas.update_idletasks()
            viewport_top = canvas.canvasy(0)
            current = None
            for state in self._config_sections.values():
                header = state.get("header")
                container = state.get("container")
                header_y = (
                    container.winfo_y() + header.winfo_y()
                    if header is not None and container is not None
                    else None
                )
                if header_y is not None and header_y < viewport_top:
                    current = state
                else:
                    break
            if current is None:
                sticky_header.place_forget()
                sticky_state["section"] = None
                return
            sticky_state["section"] = current
            sticky_title.configure(text=current["title"])
            sticky_arrow.configure(text="▼" if current["expanded"] else "▶")
            sticky_header.place(x=0, y=0, width=canvas.winfo_width())

        def on_canvas_configure(event):
            canvas.itemconfigure(body_window, width=event.width)
            if sticky_state["section"] is not None:
                sticky_header.place_configure(width=event.width)
            canvas.after_idle(update_sticky_header)

        def on_canvas_scroll(first, last):
            scroll.set(first, last)
            canvas.after_idle(update_sticky_header)

        canvas.bind("<Configure>", on_canvas_configure)
        canvas.configure(yscrollcommand=on_canvas_scroll)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)

        def scroll_config(event, units: int | None = None):
            if units is None:
                delta = int(getattr(event, "delta", 0) or 0)
                if not delta:
                    return None
                units = -delta if sys.platform == "darwin" else -int(delta / 120)
                if units == 0:
                    units = -1 if delta > 0 else 1
            canvas.yview_scroll(units, "units")
            canvas.after_idle(update_sticky_header)
            return "break"

        section_parent = body

        def section(title: str, *, expanded: bool = False):
            nonlocal section_parent
            container = ttk.Frame(body)
            container.pack(fill=tk.X, pady=(0, 6))

            header = ttk.Frame(container, relief=tk.RIDGE, borderwidth=1, padding=(9, 5))
            header.pack(fill=tk.X)
            title_label = ttk.Label(
                header,
                text=title,
                font=(UI_FONT, UI_HEADING_FONT_SIZE, "bold"),
                cursor="hand2",
            )
            title_label.pack(side=tk.LEFT, fill=tk.X, expand=True)
            arrow = ttk.Label(
                header,
                text="▼" if expanded else "▶",
                width=3,
                anchor=tk.CENTER,
                cursor="hand2",
            )
            arrow.pack(side=tk.RIGHT)

            content = ttk.Frame(container, padding=(8, 4, 4, 4))
            state = {
                "title": title,
                "expanded": expanded,
                "content": content,
                "arrow": arrow,
                "header": header,
                "container": container,
            }
            self._config_sections[title] = state

            def toggle(_event=None):
                state["expanded"] = not state["expanded"]
                if state["expanded"]:
                    content.pack(fill=tk.X)
                    arrow.configure(text="▼")
                else:
                    content.pack_forget()
                    arrow.configure(text="▶")
                if sticky_state["section"] is state:
                    sticky_arrow.configure(text="▼" if state["expanded"] else "▶")
                canvas.after_idle(lambda: canvas.configure(scrollregion=canvas.bbox("all")))
                canvas.after_idle(update_sticky_header)
                return "break"

            header.bind("<Button-1>", toggle)
            title_label.bind("<Button-1>", toggle)
            arrow.bind("<Button-1>", toggle)
            # The fixed copy uses exactly the same toggle handler as the
            # original header, so its behaviour stays consistent.
            def toggle_sticky(_event=None):
                current = sticky_state["section"]
                if current is None:
                    return "break"
                current["expanded"] = not current["expanded"]
                if current["expanded"]:
                    current["content"].pack(fill=tk.X)
                    current["arrow"].configure(text="▼")
                else:
                    current["content"].pack_forget()
                    current["arrow"].configure(text="▶")
                sticky_arrow.configure(text="▼" if current["expanded"] else "▶")
                canvas.after_idle(lambda: canvas.configure(scrollregion=canvas.bbox("all")))
                canvas.after_idle(update_sticky_header)
                return "break"

            sticky_header.bind("<Button-1>", toggle_sticky)
            sticky_title.bind("<Button-1>", toggle_sticky)
            sticky_arrow.bind("<Button-1>", toggle_sticky)
            if expanded:
                content.pack(fill=tk.X)
            section_parent = content
            return content

        def row(label: str, key: str, default="", width=30):
            line = ttk.Frame(section_parent)
            line.pack(fill=tk.X, pady=3)
            ttk.Label(line, text=label, width=18).pack(side=tk.LEFT)
            var = tk.StringVar(value=str(config.get(key, default)))
            widget = ttk.Entry(line, textvariable=var, width=width)
            widget.pack(side=tk.LEFT, fill=tk.X, expand=True)
            self.vars[key] = var
            self.entry_widgets[key] = widget
            return var

        def directory_row(label: str, key: str):
            line = ttk.Frame(section_parent)
            line.pack(fill=tk.X, pady=3)
            ttk.Label(line, text=label, width=18).pack(side=tk.LEFT)
            var = tk.StringVar(value=str(config.get(key, "") or ""))
            ttk.Entry(line, textvariable=var).pack(side=tk.LEFT, fill=tk.X, expand=True)

            def choose_directory():
                selected = filedialog.askdirectory(
                    title="选择成品视频保存目录",
                    initialdir=var.get().strip() or str(Path.home()),
                )
                if selected:
                    var.set(selected)

            ttk.Button(line, text="选择…", command=choose_directory).pack(side=tk.LEFT, padx=(6, 0))
            self.vars[key] = var
            return var

        def secret_row(label: str, key: str, default=""):
            line = ttk.Frame(section_parent)
            line.pack(fill=tk.X, pady=3)
            ttk.Label(line, text=label, width=18).pack(side=tk.LEFT)
            var = tk.StringVar(value=str(config.get(key, default)))
            entry = ttk.Entry(line, textvariable=var, show="●")
            entry.pack(side=tk.LEFT, fill=tk.X, expand=True)

            def toggle_visibility():
                hidden = bool(entry.cget("show"))
                entry.configure(show="" if hidden else "●")
                eye_button.configure(text="🙈" if hidden else "👁")

            def restore_copy_button():
                try:
                    copy_button.configure(text="复制")
                except tk.TclError:
                    pass

            def copy_secret():
                value = clean_api_key(var.get())
                if not value:
                    copy_button.configure(text="无内容")
                    copy_button.after(1200, restore_copy_button)
                    return
                self.root.clipboard_clear()
                self.root.clipboard_append(value)
                copy_button.configure(text="已复制")
                copy_button.after(1200, restore_copy_button)

            eye_button = ttk.Button(line, text="👁", width=3, command=toggle_visibility)
            eye_button.pack(side=tk.LEFT, padx=(5, 0))
            copy_button = ttk.Button(line, text="复制", width=5, command=copy_secret)
            copy_button.pack(side=tk.LEFT, padx=(5, 0))
            self.vars[key] = var
            self._secret_widgets[key] = {
                "var": var,
                "entry": entry,
                "eye_button": eye_button,
                "copy_button": copy_button,
            }
            return var

        def textrow(label: str, key: str, default="", height=3):
            line = ttk.Frame(section_parent)
            line.pack(fill=tk.X, pady=3)
            ttk.Label(line, text=label, width=18).pack(side=tk.LEFT, anchor=tk.N)
            widget = tk.Text(line, height=height, wrap=tk.WORD, font=(UI_FONT, UI_SMALL_FONT_SIZE))
            widget.insert("1.0", str(config.get(key, default)))
            widget.pack(side=tk.LEFT, fill=tk.X, expand=True)
            self.vars[key] = _TextValue(widget)
            return widget

        def combo(label: str, key: str, values: list[str], default=""):
            line = ttk.Frame(section_parent)
            line.pack(fill=tk.X, pady=3)
            ttk.Label(line, text=label, width=18).pack(side=tk.LEFT)
            var = tk.StringVar(value=str(config.get(key, default or (values[0] if values else ""))))
            widget = ttk.Combobox(line, textvariable=var, values=values, state="readonly")
            widget.pack(side=tk.LEFT, fill=tk.X, expand=True)
            self.vars[key] = var
            self.combo_widgets[key] = widget

        def model_combo(label: str, key: str, default="", values: list[str] | None = None):
            line = ttk.Frame(section_parent)
            line.pack(fill=tk.X, pady=3)
            ttk.Label(line, text=label, width=18).pack(side=tk.LEFT)
            var = tk.StringVar(value=str(config.get(key, default)))
            widget = ttk.Combobox(line, textvariable=var, values=values or [], state="normal")
            widget.pack(side=tk.LEFT, fill=tk.X, expand=True)
            self.vars[key] = var
            self.combo_widgets[key] = widget
            return var

        def api_model_combo(
            label: str,
            key: str,
            section_title: str,
            default="",
            values: list[str] | None = None,
        ):
            var = model_combo(label, key, default, values)
            self._model_mirrors[key] = {
                "api_var": var,
                "section_var": None,
                "section_title": section_title,
                "label": label,
            }
            return var

        def section_model_combo(label: str, key: str, default="", values: list[str] | None = None):
            line = ttk.Frame(section_parent)
            line.pack(fill=tk.X, pady=3)
            ttk.Label(line, text=label, width=18).pack(side=tk.LEFT)
            var = tk.StringVar(value=str(config.get(key, default)))
            widget = ttk.Combobox(line, textvariable=var, values=values or [], state="normal")
            widget.pack(side=tk.LEFT, fill=tk.X, expand=True)
            mirror = self._model_mirrors.get(key)
            if mirror is None:
                raise KeyError(f"API model field must be created before section mirror: {key}")
            mirror["section_var"] = var
            mirror["section_widget"] = widget
            return var

        def check(label: str, key: str):
            line = ttk.Frame(section_parent)
            line.pack(fill=tk.X, pady=3)
            ttk.Label(line, text=label, width=18).pack(side=tk.LEFT)
            var = tk.StringVar(value="开启" if bool(config.get(key, False)) else "关闭")
            ttk.Combobox(line, textvariable=var, values=["开启", "关闭"], state="readonly", width=12).pack(side=tk.LEFT)
            self.vars[key] = var

        def synced_check(label: str, key: str):
            """Render the same setting in multiple sections with one shared value."""
            line = ttk.Frame(section_parent)
            line.pack(fill=tk.X, pady=3)
            ttk.Label(line, text=label, width=18).pack(side=tk.LEFT)
            var = self.vars.get(key)
            if var is None:
                var = tk.StringVar(value="开启" if bool(config.get(key, False)) else "关闭")
                self.vars[key] = var
            ttk.Combobox(
                line, textvariable=var, values=["开启", "关闭"], state="readonly", width=12
            ).pack(side=tk.LEFT)

        def buttons(*items: tuple[str, callable]):
            line = ttk.Frame(section_parent)
            line.pack(fill=tk.X, pady=(5, 3))
            ttk.Label(line, text="", width=18).pack(side=tk.LEFT)
            for text, command in items:
                ttk.Button(line, text=text, command=command).pack(side=tk.LEFT, padx=(0, 6))

        def subheading(title: str, note: str = ""):
            line = ttk.Frame(section_parent)
            line.pack(fill=tk.X, pady=(10, 3))
            ttk.Label(line, text=title, font=(UI_FONT, UI_FONT_SIZE, "bold")).pack(anchor=tk.W)
            if note:
                ttk.Label(line, text=note, foreground="#666", wraplength=520).pack(anchor=tk.W, pady=(2, 0))
            ttk.Separator(section_parent).pack(fill=tk.X, pady=(0, 3))

        profile_box = ttk.LabelFrame(body, text="配置方案", padding=6)
        profile_box.pack(fill=tk.X, pady=(0, 8))
        profile_line = ttk.Frame(profile_box)
        profile_line.pack(fill=tk.X)
        ttk.Label(profile_line, text="当前方案", width=18).pack(side=tk.LEFT)
        self.profile_var = tk.StringVar(value=str(config.get("active_profile", "配置1")))
        self.profile_combo = ttk.Combobox(
            profile_line,
            textvariable=self.profile_var,
            values=config.list_profiles(),
            state="normal",
            width=20,
        )
        self.profile_combo.pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(profile_line, text="新增", width=6, command=self._create_profile).pack(side=tk.LEFT, padx=(6, 0))
        ttk.Button(profile_line, text="删除", width=6, command=self._delete_profile).pack(side=tk.LEFT, padx=(6, 0))
        # Selecting a profile from the drop-down must load its saved values.
        # Without this binding, only the displayed name changes while all form
        # fields (and the configuration used for a run) remain from the prior profile.
        self.profile_combo.bind("<<ComboboxSelected>>", self._on_profile_selected)

        section("系统与依赖")
        check("启动时检测依赖", "dependency_check_on_startup")
        check("自动安装 Python 包", "dependency_auto_install_python")
        check("自动下载 FFmpeg", "dependency_auto_install_ffmpeg")
        check("自动安装 Chrome", "dependency_auto_install_browser")
        check("启动时检查更新", "update_check_on_startup")
        row("反馈入口 URL", "feedback_issue_url", "", width=48)
        row("PIP 镜像", "dependency_pip_index_url", "", width=48)
        row("PIP 额外参数", "dependency_pip_extra_args", "", width=48)
        row("PIP 超时秒数", "dependency_pip_timeout_seconds", "1800")
        row("FFmpeg 下载地址", "dependency_ffmpeg_url", "", width=48)
        row("更新清单 URL", "update_manifest_url", "", width=48)
        row("依赖检测摘要", "dependency_last_report", "", width=48)
        buttons(("检测/安装依赖", self._manual_dependency_check), ("提交反馈", self._open_feedback_url))

        section("API 设置与 Key")
        ttk.Label(
            section_parent,
            text="集中管理所有接口、Key、Provider 和模型。各业务分组仍保留模型输入框；保存时若两处填写不同，会弹窗让你选择最终使用哪一个。",
            foreground="#666",
            wraplength=520,
        ).pack(anchor=tk.W, pady=(0, 4))

        subheading("统一 API", "开启后，文本分析和全部图片生成优先使用这套 OpenAI 兼容接口。")
        check("统一 API 接管文本和全部图片", "ai_api_enabled")
        row("统一 Base URL/中转", "ai_api_base_url", "")
        secret_row("统一 API Key", "ai_api_key", "")
        api_model_combo(
            "统一文本模型",
            "ai_api_text_model",
            "图片与提示词",
            "gpt-4o-mini",
            ["gpt-4o-mini", "gpt-4o", "deepseek-chat"],
        )
        api_model_combo(
            "统一图片模型",
            "ai_api_image_model",
            "图片与提示词",
            "gpt-image-2",
            ["gpt-image-1", "gpt-image-2", "dall-e-3"],
        )
        row("统一图片 API 请求宽", "ai_api_image_width", "1792")
        row("统一图片 API 请求高", "ai_api_image_height", "1008")
        buttons(
            ("检测统一文本 API", lambda: self._probe_api("unified_llm")),
            ("检测统一图片 API", lambda: self._probe_api("unified_image")),
        )

        subheading("生词词典", "默认沿用统一文本 API 和 Key；开启独立接口后，以下设置优先，留空项仍会继承统一设置。")
        check("使用独立词典 API", "pronunciation_dictionary_dedicated_api_enabled")
        row("词典 Base URL/中转", "pronunciation_dictionary_base_url", "")
        secret_row("词典 API Key(可空=统一)", "pronunciation_dictionary_api_key", "")
        api_model_combo(
            "词典文本模型",
            "pronunciation_dictionary_model",
            "生词词典",
            "gpt-4o-mini",
            ["gpt-4o-mini", "gpt-4o", "deepseek-chat"],
        )
        buttons(("检测生词词典 API", lambda: self._probe_api("pronunciation_dictionary")))

        subheading("TTS")
        combo("TTS Provider", "tts_provider", ["edge", "voicevox", "openai", "azure", "elevenlabs", "custom"], "edge")
        row("TTS Base URL/中转", "tts_base_url", "")
        secret_row("TTS API Key", "tts_api_key", "")
        api_model_combo(
            "TTS 模型名",
            "tts_model",
            "TTS",
            "tts-1",
            ["tts-1", "tts-1-hd", "gpt-4o-mini-tts"],
        )
        buttons(("检测 TTS API / 识别模型", lambda: self._probe_api("tts")))

        subheading("图片与提示词")
        row("分镜 LLM Provider", "llm_provider", "openai")
        row("分镜 Base URL/中转(可空=统一)", "llm_base_url", "")
        secret_row("分镜 API Key(可空=统一)", "llm_api_key", "")
        api_model_combo(
            "分镜模型",
            "llm_model",
            "图片与提示词",
            "gpt-4o-mini",
            ["gpt-4o-mini", "gpt-4o", "deepseek-chat"],
        )
        combo("图片 Provider", "image_provider", ["placeholder", "comfyui", "sdwebui", "openai", "replicate", "aliyun", "custom"], "placeholder")
        row("图片 Base URL/中转(可空=统一)", "image_base_url", "")
        secret_row("图片 API Key(可空=统一)", "image_api_key", "")
        api_model_combo("图片模型名", "image_model", "图片与提示词", "", ["gpt-image-1", "gpt-image-2", "dall-e-3"])
        row("图片 API 超时秒", "image_api_timeout_seconds", "300")
        combo("人设图 Provider", "character_reference_provider", ["same_as_image", "placeholder", "comfyui", "sdwebui", "openai", "replicate", "aliyun", "custom"], "same_as_image")
        row("人设图 Base URL/中转(可空=统一)", "character_reference_base_url", "")
        secret_row("人设图 API Key(可空=统一)", "character_reference_api_key", "")
        api_model_combo(
            "人设图模型名",
            "character_reference_model",
            "图片与提示词",
            "",
            ["gpt-image-1", "dall-e-3", "flux", "sdxl"],
        )
        combo("剧情参考 Provider", "scene_reference_provider", ["same_as_image", "comfyui", "sdwebui", "openai", "custom"], "same_as_image")
        row("剧情参考 Base URL/中转(可空=统一)", "scene_reference_base_url", "")
        secret_row("剧情参考 API Key(可空=统一)", "scene_reference_api_key", "")
        api_model_combo(
            "剧情参考模型名",
            "scene_reference_model",
            "图片与提示词",
            "",
            ["gpt-image-1", "dall-e-3", "flux", "sdxl"],
        )
        buttons(
            ("检测分镜 LLM", lambda: self._probe_api("llm")),
            ("检测图片 API", lambda: self._probe_api("image")),
            ("检测人设图 API", lambda: self._probe_api("character_reference")),
            ("检测剧情参考 API", lambda: self._probe_api("scene_reference")),
        )

        subheading("封面")
        combo("封面 Provider", "cover_provider", ["same_as_image", "placeholder", "comfyui", "sdwebui", "openai", "replicate", "aliyun", "custom"], "same_as_image")
        row("封面 Base URL/中转(可空=统一)", "cover_base_url", "")
        secret_row("封面 API Key(可空=统一)", "cover_api_key", "")
        api_model_combo("封面模型名", "cover_model", "封面", "", ["gpt-image-1", "dall-e-3"])
        buttons(("检测封面 API", lambda: self._probe_api("cover")))

        subheading("批量与上传")
        row("外部 API 并发", "max_concurrent_external_api", "2")

        section("TTS")
        combo("常用音色", "tts_voice", EDGE_MULTI_VOICES, config.tts_voice)
        self.combo_widgets["tts_provider"].bind(
            "<<ComboboxSelected>>", lambda _event: self._refresh_tts_voice_options()
        )
        self._refresh_tts_voice_options()
        row("语速", "tts_rate", "+0%")
        row("音量倍率", "tts_volume", "1.0")
        section_model_combo("TTS 模型名", "tts_model", "tts-1", ["tts-1", "tts-1-hd", "gpt-4o-mini-tts"])
        row("情感/指令", "tts_emotion", "")
        row("TTS 重试次数", "tts_retries", "3")
        row("TTS 单段超时秒", "tts_segment_timeout_seconds", "180")
        row("TTS 卡段提示秒", "tts_stall_fallback_seconds", "240")
        row("TTS 心跳日志秒", "tts_heartbeat_seconds", "30")
        check("TTS 持续重试直到成功", "tts_retry_until_success")
        check("TTS 波形校验", "tts_waveform_validation")
        row("TTS 最低 RMS dB", "tts_waveform_min_rms_db", "-55")
        row("TTS 最大静音比例", "tts_waveform_max_silence_ratio", "0.92")
        check("TTS 子进程隔离", "tts_subprocess_isolation")
        tuning_box = ttk.Frame(section_parent, relief=tk.GROOVE, borderwidth=1, padding=(6, 4))
        tuning_box.pack(fill=tk.X, pady=(8, 2))
        tuning_header = ttk.Frame(tuning_box)
        tuning_header.pack(fill=tk.X)
        tuning_title = ttk.Label(
            tuning_header,
            text="▶  VOICEVOX 高级调音（仅 VOICEVOX 生效）",
            font=(UI_FONT, UI_SMALL_FONT_SIZE, "bold"),
            cursor="hand2",
        )
        tuning_title.pack(side=tk.LEFT, fill=tk.X, expand=True)
        tuning_content = ttk.Frame(tuning_box, padding=(8, 5, 0, 0))

        def toggle_voicevox_tuning(_event=None):
            if tuning_content.winfo_manager():
                tuning_content.pack_forget()
                tuning_title.configure(text="▶  VOICEVOX 高级调音（仅 VOICEVOX 生效）")
            else:
                tuning_content.pack(fill=tk.X)
                tuning_title.configure(text="▼  VOICEVOX 高级调音（仅 VOICEVOX 生效）")
            canvas.after_idle(lambda: canvas.configure(scrollregion=canvas.bbox("all")))
            return "break"

        tuning_header.bind("<Button-1>", toggle_voicevox_tuning)
        tuning_title.bind("<Button-1>", toggle_voicevox_tuning)
        ttk.Label(
            tuning_content,
            text="默认值更舒缓、停顿更自然。语速越低越慢；抑扬 1.0 为标准；停顿越高越长。",
            foreground="#666",
            wraplength=520,
        ).pack(anchor=tk.W, pady=(0, 4))

        def tuning_row(label: str, key: str, default: str):
            line = ttk.Frame(tuning_content)
            line.pack(fill=tk.X, pady=3)
            ttk.Label(line, text=label, width=18).pack(side=tk.LEFT)
            var = tk.StringVar(value=str(config.get(key, default)))
            ttk.Entry(line, textvariable=var, width=30).pack(side=tk.LEFT, fill=tk.X, expand=True)
            self.vars[key] = var

        tuning_row("语速倍率", "voicevox_speed_scale", "0.90")
        tuning_row("抑扬倍率", "voicevox_intonation_scale", "0.85")
        tuning_row("间隔长度倍率", "voicevox_pause_scale", "1.25")

        section("洗稿")
        check("AI 洗稿改写", "ai_rewrite_enabled")
        check("保留章节标题并朗读（第X话）", "narrate_chapter_headings")
        check("洗稿后朗读净化（清除装饰符号）", "tts_clean_rewritten_text")
        row("洗稿批大小", "ai_rewrite_batch_chars", "3500")
        textrow("洗稿提示词", "ai_rewrite_prompt", str(config.get("ai_rewrite_prompt", "")), height=5)

        section("生词词典")
        ttk.Label(
            section_parent,
            text="仅在开启 AI 洗稿后运行：从洗稿并净化后的日文中筛选容易读错的汉字词，生成假名词典供 Edge TTS 使用。",
            foreground="#666",
            wraplength=520,
        ).pack(anchor=tk.W, pady=(0, 4))
        check("生成生词读音词典", "tts_auto_pronunciation_enabled")
        synced_check("使用独立词典 API", "pronunciation_dictionary_dedicated_api_enabled")
        section_model_combo(
            "使用的文本模型",
            "pronunciation_dictionary_model",
            "gpt-4o-mini",
            ["gpt-4o-mini", "gpt-4o", "deepseek-chat"],
        )
        row("最多候选生词", "tts_auto_pronunciation_max_terms", "40")
        textrow(
            "制作词典提示词",
            "pronunciation_dictionary_prompt",
            str(config.get("pronunciation_dictionary_prompt", "")),
            height=6,
        )

        section("图片与提示词")
        subheading("模型输入", "这些模型也可以在“API 设置与 Key”中填写；保存时会检查两处是否一致。")
        section_model_combo("统一文本模型", "ai_api_text_model", "gpt-4o-mini", ["gpt-4o-mini", "gpt-4o", "deepseek-chat"])
        section_model_combo("统一图片模型", "ai_api_image_model", "gpt-image-2", ["gpt-image-1", "gpt-image-2", "dall-e-3"])
        section_model_combo("分镜模型", "llm_model", "gpt-4o-mini", ["gpt-4o-mini", "gpt-4o", "deepseek-chat"])
        section_model_combo("图片模型名", "image_model", "", ["gpt-image-1", "gpt-image-2", "dall-e-3"])
        row("分镜图宽", "image_width", "1792")
        row("分镜图高", "image_height", "1008")
        section_model_combo("人设图模型名", "character_reference_model", "", ["gpt-image-1", "dall-e-3", "flux", "sdxl"])
        section_model_combo("剧情参考模型名", "scene_reference_model", "", ["gpt-image-1", "dall-e-3", "flux", "sdxl"])
        check("AI 人设分析", "character_analysis_enabled")
        row("人设分析最大字数", "character_analysis_max_chars", "12000")
        row("人设分析输出 tokens", "character_analysis_max_tokens", "1800")
        row("每图最多注入人物数", "character_analysis_max_characters_per_prompt", "4")
        check("每图固定带主角", "character_analysis_always_include_protagonists")
        textrow("人设分析提示词", "character_analysis_prompt", str(config.get("character_analysis_prompt", "")), height=5)
        check("剧情图注入视觉主题", "scene_inject_visual_theme")
        check("剧情图注入人物锁", "scene_inject_character_triggers")
        check("生成人设参考图", "character_reference_enabled")
        row("人设图 workflow(仅 ComfyUI)", "character_reference_workflow", "")
        row("人设图宽", "character_reference_width", "768")
        row("人设图高", "character_reference_height", "1024")
        row("最多人设图", "character_reference_max_count", "6")
        row("人设图提示词后缀", "character_reference_prompt_suffix", "")
        check("剧情图使用人设参考图", "scene_reference_enabled")
        row("剧情参考 workflow(仅 ComfyUI)", "scene_reference_workflow", "")
        row("剧情参考最大图数", "scene_reference_max_images", "2")
        textrow("视频图通用前缀", "llm_image_prompt_prefix", str(config.get("llm_image_prompt_prefix", "")), height=3)
        row("视频图风格后缀", "llm_image_style_suffix", "")
        textrow("视频图 AI 分析系统提示词", "llm_storyboard_prompt", str(config.get("llm_storyboard_prompt", "")), height=4)
        textrow("视频图 AI 分析用户模板", "llm_storyboard_user_template", str(config.get("llm_storyboard_user_template", "")), height=4)
        check("智能选择旁白高光点（不增加图片数）", "storyboard_highlight_enabled")
        check("按高光旁白调整换图时间", "storyboard_highlight_align_timeline")
        row("全篇背景分析最大字数", "storyboard_highlight_context_max_chars", "18000")
        row("每图高光最多连续段数", "storyboard_highlight_max_segments", "3")

        section("封面")
        check("自动生成封面", "cover_enabled")
        synced_check("检测系列动画", "series_animation_enabled")
        section_model_combo("封面模型名", "cover_model", "", ["gpt-image-1", "gpt-image-2", "dall-e-3"])
        row("封面宽", "cover_width", "1792")
        row("封面高", "cover_height", "1008")
        textrow("封面固定模板", "cover_prompt_template", str(config.get("cover_prompt_template", "")), height=5)
        textrow("封面自定义提示词", "cover_custom_prompt", str(config.get("cover_custom_prompt", "")), height=3)
        textrow("封面 AI 分析提示词", "cover_ai_analysis_prompt", str(config.get("cover_ai_analysis_prompt", "")), height=4)
        textrow("封面完整方法（可编辑）", "cover_poster_method_prompt", str(config.get("cover_poster_method_prompt", "")), height=5)
        ttk.Button(
            section_parent,
            text="执行选中任务",
            command=self._regenerate_selected_covers,
            style="Primary.TButton",
        ).pack(anchor=tk.E, pady=(5, 14))

        section("画面节奏 / 视频")
        self._pacing_mode_labels = {
            "按时长": "by_duration",
            "按句子": "by_sentence",
            "按段落": "by_paragraph",
            "固定数量": "fixed_count",
        }
        self._pacing_mode_values_to_labels = {value: label for label, value in self._pacing_mode_labels.items()}
        pacing_line = ttk.Frame(section_parent)
        pacing_line.pack(fill=tk.X, pady=3)
        ttk.Label(pacing_line, text="配图模式", width=18).pack(side=tk.LEFT)
        saved_pacing_mode = str(config.get("pacing_mode", "by_duration"))
        pacing_var = tk.StringVar(value=self._pacing_mode_values_to_labels.get(saved_pacing_mode, "按时长"))
        pacing_widget = ttk.Combobox(
            pacing_line,
            textvariable=pacing_var,
            values=list(self._pacing_mode_labels),
            state="readonly",
        )
        pacing_widget.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.vars["pacing_mode"] = pacing_var
        self.combo_widgets["pacing_mode"] = pacing_widget
        row("每张图秒数", "pacing_seconds_per_image", "6")
        row("固定图片数", "pacing_fixed_count", "10")
        pacing_widget.bind("<<ComboboxSelected>>", lambda _event: self._refresh_pacing_mode_controls())
        self._refresh_pacing_mode_controls()
        row("视频宽", "video_width", "1920")
        row("视频高", "video_height", "1080")
        row("FPS", "video_fps", "30")
        combo("视频编码器", "video_encoder", ["libx264", "h264_videotoolbox", "h264_nvenc", "h264_qsv", "h264_amf"], "libx264")
        row("编码预设", "video_encoder_preset", "veryfast")
        row("编码质量", "video_encoder_quality", "20")
        combo("图片动作", "video_motion", ["随机", "上下移动", "左右移动", "向上推进", "向下推进", "向左推进", "向右推进", "轻微缩放", "静态"], "上下移动")
        combo("移动曲线", "video_motion_curve", ["ease", "linear"], "ease")
        row("移动周期秒(0=整段)", "video_motion_cycle_seconds", "0")
        combo("转场", "video_transition", ["随机", "none", "fade", "fadeblack", "fadewhite"], "none")
        row("转场秒数", "video_transition_duration", "0.4")
        check("长视频稳定模式", "video_long_mode")
        check("Ken Burns 移动特效", "ken_burns")
        combo("字幕位置", "video_subtitle_position", ["下边", "中间", "上边"], "下边")
        row("字幕字体", "video_subtitle_font", "Hiragino Mincho ProN")
        row("字幕字号", "video_subtitle_size", "48")
        row("字幕颜色", "video_subtitle_color", "#FFFFFF")
        row("描边颜色", "video_subtitle_outline_color", "#000000")
        row("描边宽度", "video_subtitle_outline", "4")
        row("阴影深度", "video_subtitle_shadow", "1")
        row("字幕边距", "video_subtitle_margin_v", "60")
        row("每行字数", "video_subtitle_chars_per_line", "24")
        row("最多行数", "video_subtitle_max_lines", "2")
        check("字幕粗体", "video_subtitle_bold")
        check("字幕斜体", "video_subtitle_italic")
        buttons(("预览字幕样式", self._preview_subtitle))
        check("内嵌字幕（强制）", "video_subtitle")
        check("导出 SRT", "video_external_subtitle")

        section("输出目录")
        directory_row("成品视频保存到", "output_directory")
        check("本地 TXT 导入的成品输出回原文件夹", "output_to_input_directory")
        ttk.Label(
            section_parent,
            text="可选一个统一的成品目录；或勾选“输出回原文件夹”，在原 TXT 同级生成 MP4、cover 和标题.txt。后者优先，适合分段小说。",
            foreground="#666",
            wraplength=520,
        ).pack(anchor=tk.W, pady=(0, 4))

        section("批量与上传")
        synced_check("检测系列动画", "series_animation_enabled")
        check("自动生成标题/概梗候选", "short_title_enabled")
        row("候选标题最少字", "marketing_title_min_chars", "40")
        row("候选标题最多字", "marketing_title_max_chars", "70")
        row("标题接口重试次数", "marketing_candidates_retry_attempts", "5")
        row("标题接口重试等待秒", "marketing_candidates_retry_delay_seconds", "60")
        textrow("标题/概梗提示词（可编辑）", "marketing_candidates_prompt", str(config.get("marketing_candidates_prompt", "")), height=7)
        check("首次启动自动检测", "hardware_autotune_enabled")
        row("硬件检测摘要", "hardware_autotune_summary", "", width=48)
        buttons(("检测电脑并自动配置并发", self._manual_hardware_autotune))
        row("同时任务数", "max_concurrent_jobs", "1")
        row("FFmpeg 并发", "max_concurrent_ffmpeg", "1")
        row("TTS 并行数", "max_parallel_tts", "2")
        row("图片并行数", "max_parallel_images", "2")
        row("视频片段并行数", "max_parallel_video_clips", "1")
        check("TTS 与图片并行", "pipeline_overlap_tts_images")
        check("生成后自动上传 YouTube", "upload_enabled")
        combo("上传方案", "browser_active_profile", _upload_profile_names(), str(config.get("browser_active_profile", "无创收精简流程")))
        check("上传全部启用方案", "browser_upload_all_profiles")
        combo("上传流程", "browser_flow", ["simple", "full"], "simple")
        combo("公开范围", "youtube_visibility", ["PRIVATE", "UNLISTED", "PUBLIC"], "PRIVATE")
        row("Chrome 账号资料", "browser_chrome_profile", "Default")
        buttons(("编辑频道上传方案", self._edit_upload_profiles_dialog))
        textrow("高级: 方案 JSON", "browser_profiles", str(config.get("browser_profiles", "[]")), height=5)
        row("上传标题模板", "youtube_title_template", "{candidate_title}")
        row("上传标题字数", "youtube_title_max_chars", "100")
        textrow("说明模板", "youtube_description", "", height=4)
        row("模板 Tags（仅 {tags} 使用）", "youtube_tags", "")
        row("上传政策", "browser_upload_policy", "BTRA")
        row("广告间隔秒", "browser_ad_interval", "60")
        row("广告起始秒", "browser_ad_start", "0")
        textrow("广告分级模板(JSON)", "browser_ad_suitability_template", str(config.get("browser_ad_suitability_template", "")), height=3)

        ttk.Label(
            section_parent,
            text="新任务上传时会从3个候选标题中随机选择，并先按上传标题模板生成；若仍有字数空间，再补入模板中没有的内容标签。脚本绝不会自动加【朗读・小説】。{tags} 只引用“模板 Tags”。simple=无创收精简流程，full=完整创收/广告/分级流程。",
            foreground="#666",
            wraplength=520,
        ).pack(anchor=tk.W, pady=(4, 14))
        ttk.Button(
            section_parent,
            text="执行选中任务",
            command=self._upload_selected_completed_jobs,
            style="Primary.TButton",
        ).pack(anchor=tk.E, pady=(0, 14))

        def bind_config_mousewheel(widget):
            # Bind before the widget class handler so nested Text/Combo widgets
            # scroll the whole configuration page instead of swallowing the wheel.
            widget.bind("<MouseWheel>", scroll_config, add="+")
            widget.bind("<Button-4>", lambda event: scroll_config(event, -1), add="+")
            widget.bind("<Button-5>", lambda event: scroll_config(event, 1), add="+")
            for child in widget.winfo_children():
                bind_config_mousewheel(child)

        bind_config_mousewheel(canvas)
        bind_config_mousewheel(scroll)

        def scroll_config_under_pointer(event):
            """Catch trackpad/mouse scrolling anywhere over the config pane.

            Some Tk controls consume wheel events before their child bindings
            see them.  Checking the pointer against the canvas gives the whole
            visible settings area one predictable scrolling behaviour, without
            affecting the task list or other tabs.
            """
            x = event.x_root
            y = event.y_root
            left = canvas.winfo_rootx()
            top = canvas.winfo_rooty()
            if left <= x < left + canvas.winfo_width() and top <= y < top + canvas.winfo_height():
                return scroll_config(event)
            return None

        # This is a fallback for controls that do not pass wheel events to
        # their parent (notably some macOS Tk widgets).  Existing per-widget
        # bindings continue to handle the normal path first.
        self.root.bind_all("<MouseWheel>", scroll_config_under_pointer, add="+")
        self.root.bind_all("<Button-4>", scroll_config_under_pointer, add="+")
        self.root.bind_all("<Button-5>", scroll_config_under_pointer, add="+")
        self._config_canvas = canvas

    def _resolve_model_mirror_conflicts(self):
        """Choose one value when the central API model and section copy differ."""
        for key, mirror in getattr(self, "_model_mirrors", {}).items():
            api_var = mirror.get("api_var")
            section_var = mirror.get("section_var")
            if api_var is None or section_var is None:
                continue
            api_value = str(api_var.get() or "").strip()
            section_value = str(section_var.get() or "").strip()
            if api_value == section_value:
                api_var.set(api_value)
                section_var.set(section_value)
                continue

            # An empty field is treated as "not filled" and inherits the value
            # from the other location. Only two actual, different model names
            # require an explicit decision.
            if not api_value:
                chosen = section_value
            elif not section_value:
                chosen = api_value
            else:
                section_title = str(mirror.get("section_title") or "原配置分组")
                label = str(mirror.get("label") or key)
                use_api_value = messagebox.askyesno(
                    "模型设置不一致",
                    f"检测到“{label}”在两处填写得不一样：\n\n"
                    f"API 设置与 Key：{api_value}\n"
                    f"{section_title}：{section_value}\n\n"
                    f"选择“是”使用 API 设置中的模型；\n"
                    f"选择“否”使用 {section_title} 中的模型。",
                    icon="warning",
                    parent=self.root,
                )
                chosen = api_value if use_api_value else section_value
            api_var.set(chosen)
            section_var.set(chosen)

    def _config_key_sets(self):
        int_keys = {
            "pacing_fixed_count", "video_width", "video_height", "video_fps",
            "video_encoder_quality", "ai_rewrite_batch_chars",
            "ai_api_image_width", "ai_api_image_height", "image_width", "image_height",
            "character_analysis_max_chars", "character_analysis_max_tokens",
            "character_analysis_max_characters_per_prompt",
            "storyboard_highlight_context_max_chars", "storyboard_highlight_max_segments",
            "character_reference_width", "character_reference_height", "character_reference_max_count",
            "scene_reference_max_images",
            "image_api_timeout_seconds", "tts_segment_timeout_seconds", "tts_stall_fallback_seconds",
            "tts_heartbeat_seconds", "tts_retries", "tts_auto_pronunciation_max_terms",
            "max_concurrent_jobs", "max_concurrent_external_api", "max_concurrent_ffmpeg",
            "max_parallel_tts", "max_parallel_images", "max_parallel_video_clips",
            "browser_ad_interval", "browser_ad_start", "dependency_pip_timeout_seconds",
            "cover_width", "cover_height", "cover_title_size",
            "video_subtitle_size", "video_subtitle_margin_v", "video_subtitle_margin_lr",
            "video_subtitle_chars_per_line", "video_subtitle_max_lines",
            "short_title_min_chars", "short_title_max_chars",
            "marketing_title_min_chars", "marketing_title_max_chars", "youtube_title_max_chars",
            "script_schedule_interval_hours",
        }
        float_keys = {
            "tts_volume", "tts_waveform_min_rms_db", "tts_waveform_max_silence_ratio",
            "voicevox_speed_scale", "voicevox_intonation_scale", "voicevox_pause_scale",
            "pacing_seconds_per_image", "video_motion_cycle_seconds",
            "video_transition_duration", "cover_title_area_ratio",
            "video_subtitle_outline", "video_subtitle_shadow", "video_subtitle_spacing",
        }
        bool_keys = {
            "video_long_mode", "ken_burns", "video_subtitle", "video_external_subtitle",
            "upload_enabled", "cover_enabled", "video_subtitle_bold", "video_subtitle_italic",
            "short_title_enabled", "pipeline_overlap_tts_images", "ai_rewrite_enabled",
            "narrate_chapter_headings",
            "character_analysis_enabled", "character_analysis_always_include_protagonists",
            "character_reference_enabled", "scene_inject_visual_theme", "scene_inject_character_triggers",
            "storyboard_highlight_enabled", "storyboard_highlight_align_timeline",
            "scene_reference_enabled", "ai_api_enabled",
            "tts_retry_until_success", "tts_waveform_validation", "tts_subprocess_isolation",
            "tts_clean_rewritten_text", "tts_auto_pronunciation_enabled",
            "pronunciation_dictionary_dedicated_api_enabled",
            "hardware_autotune_enabled",
            "dependency_check_on_startup", "dependency_auto_install_python",
            "dependency_auto_install_ffmpeg", "dependency_auto_install_browser",
            "update_check_on_startup", "browser_upload_all_profiles",
            "youtube_schedule_enabled",
            "series_animation_enabled",
        }
        return int_keys, float_keys, bool_keys

    def _apply_config_form(self, save_profile: bool = True) -> str:
        self._resolve_model_mirror_conflicts()
        int_keys, float_keys, bool_keys = self._config_key_sets()
        for key, var in self.vars.items():
            value = var.get()
            if key == "pacing_mode":
                value = getattr(self, "_pacing_mode_labels", {}).get(str(value), str(value))
            if key in bool_keys:
                value = str(value) == "开启"
            elif key in int_keys:
                try:
                    value = int(float(value))
                except Exception:
                    value = int(config.get(key, 0) or 0)
            elif key in float_keys:
                try:
                    value = float(value)
                except Exception:
                    value = float(config.get(key, 0) or 0)
            elif key in API_KEY_FIELDS:
                value = clean_api_key(value)
            config.set(key, value)
        config.set("video_subtitle", True)
        if "video_subtitle" in self.vars:
            self.vars["video_subtitle"].set("开启")
        profile_var = getattr(self, "profile_var", None)
        profile_name = str(profile_var.get() if profile_var is not None else "配置1").strip() or "配置1"
        config.set("active_profile", profile_name)
        if save_profile:
            profile_name = config.save_profile(profile_name)
            if hasattr(self, "profile_var"):
                self.profile_var.set(profile_name)
            if hasattr(self, "profile_combo"):
                self.profile_combo.configure(values=config.list_profiles())
        config.save()
        return profile_name

    def _save_profile(self):
        profile_name = self._apply_config_form(save_profile=True)
        messagebox.showinfo("已保存", f"已保存到配置方案：{profile_name}")

    def _create_profile(self):
        suggested = config.suggest_profile_name()
        raw_name = simpledialog.askstring(
            "新增配置",
            "请输入新配置名称。\n新配置会复制当前界面中的设置：",
            initialvalue=suggested,
            parent=self.root,
        )
        if raw_name is None:
            return
        profile_name = str(raw_name).strip()
        if not profile_name:
            messagebox.showwarning("名称无效", "配置名称不能为空。")
            return
        invalid_chars = sorted(set(profile_name) & set('<>:"/\\|?*'))
        if invalid_chars or any(ord(ch) < 32 for ch in profile_name):
            messagebox.showwarning("名称无效", "配置名称不能包含以下字符：< > : \" / \\ | ? *")
            return
        if profile_name.strip(" .") != profile_name or len(profile_name) > 80:
            messagebox.showwarning("名称无效", "配置名称不能以空格或句点结尾，且最多为 80 个字符。")
            return
        if profile_name in config.list_profiles():
            messagebox.showwarning("名称已存在", f"配置方案「{profile_name}」已经存在，请换一个名称。")
            return

        try:
            self._apply_config_form(save_profile=False)
            config.set("active_profile", profile_name)
            created = config.save_profile(profile_name)
            config.save()
            self.profile_var.set(created)
            self.profile_combo.configure(values=config.list_profiles())
            messagebox.showinfo("新增成功", f"已复制当前设置并创建配置方案：{created}")
        except Exception as exc:
            messagebox.showerror("新增失败", str(exc))

    def _delete_profile(self):
        profile_name = str(self.profile_var.get()).strip() or "配置1"
        profiles = config.list_profiles()
        if profile_name not in profiles:
            messagebox.showwarning("无法删除", "当前输入的配置尚未保存，请先保存或从列表中选择一个配置。")
            return
        if len(profiles) <= 1:
            messagebox.showwarning("无法删除", "至少需要保留一个配置方案。")
            return
        if not messagebox.askyesno(
            "删除配置",
            f"确定删除配置方案「{profile_name}」吗？\n\n此操作无法撤销。",
            parent=self.root,
        ):
            return
        try:
            deleted, loaded = config.delete_profile(profile_name)
            self._refresh_config_form()
            self.profile_var.set(loaded)
            self.profile_combo.configure(values=config.list_profiles())
            messagebox.showinfo("已删除", f"已删除「{deleted}」，当前已切换到「{loaded}」。")
        except Exception as exc:
            messagebox.showerror("删除失败", str(exc))

    def _apply_profile_to_selected_jobs(self):
        ids = self._selected_job_ids()
        if not ids:
            messagebox.showwarning("没有选择任务", "请先在任务列表多选任务，再套用方案。")
            return
        profile_name = str(self.profile_var.get()).strip() or "配置1"
        try:
            applied_profile, count = pr.apply_profile_to_jobs(ids, profile_name)
        except Exception as exc:
            messagebox.showerror("套用方案失败", str(exc))
            return
        self._refresh_jobs()
        messagebox.showinfo(
            "已套用方案",
            f"已将「{applied_profile}」固定到 {count} 个任务。\n\n"
            "这些任务之后自动排队启动时，会继续使用各自固定的方案。",
        )

    def _on_profile_selected(self, _event=None):
        """Apply the profile immediately when it is chosen from the drop-down."""
        self._load_profile(show_confirmation=False)

    def _load_profile(self, *, show_confirmation: bool = True):
        profile_var = getattr(self, "profile_var", None)
        profile_name = str(profile_var.get() if profile_var is not None else "配置1").strip() or "配置1"
        try:
            loaded = config.load_profile(profile_name)
            config.save()
            self._refresh_config_form()
            if hasattr(self, "profile_var"):
                self.profile_var.set(loaded)
            if hasattr(self, "profile_combo"):
                self.profile_combo.configure(values=config.list_profiles())
            if show_confirmation:
                messagebox.showinfo("已加载", f"已切换到配置方案：{loaded}")
        except Exception as exc:
            messagebox.showerror("加载失败", str(exc))

    def _refresh_config_form(self):
        _, _, bool_keys = self._config_key_sets()
        for key, var in self.vars.items():
            value = config.get(key, "")
            if key in bool_keys:
                var.set("开启" if bool(value) else "关闭")
            elif key == "pacing_mode":
                var.set(getattr(self, "_pacing_mode_values_to_labels", {}).get(str(value), "按时长"))
            else:
                var.set(str(value))
        self._refresh_pacing_mode_controls()
        for key, mirror in getattr(self, "_model_mirrors", {}).items():
            section_var = mirror.get("section_var")
            if section_var is not None:
                section_var.set(str(config.get(key, "")))

    def _refresh_pacing_mode_controls(self):
        """Grey out image-count fields that do not apply to the selected mode."""
        mode_label = str(self.vars.get("pacing_mode").get() if self.vars.get("pacing_mode") else "")
        mode = getattr(self, "_pacing_mode_labels", {}).get(mode_label, mode_label)
        seconds = getattr(self, "entry_widgets", {}).get("pacing_seconds_per_image")
        fixed_count = getattr(self, "entry_widgets", {}).get("pacing_fixed_count")
        if seconds is not None:
            seconds.configure(state="normal" if mode == "by_duration" else "disabled")
        if fixed_count is not None:
            fixed_count.configure(state="normal" if mode == "fixed_count" else "disabled")

    def _ui_value(self, key: str, default: str = "") -> str:
        var = self.vars.get(key)
        return str(var.get() if var is not None else config.get(key, default) or "").strip()

    def _ui_enabled(self, key: str, default: bool = False) -> bool:
        var = self.vars.get(key)
        if var is not None:
            return str(var.get()) == "开启"
        return bool(config.get(key, default))

    def _ui_unified_api_enabled(self) -> bool:
        return self._ui_enabled("ai_api_enabled", True) and bool(
            self._ui_value("ai_api_base_url") or self._ui_value("ai_api_key")
        )

    def _effective_llm_probe_args(self, *, force_unified: bool = False) -> dict:
        provider = self._ui_value("llm_provider", "openai").lower()
        base_url = self._ui_value("llm_base_url")
        api_key = self._ui_value("llm_api_key")
        model = self._ui_value("llm_model")
        if force_unified and provider not in {"openai", "custom", "deepseek", "gemini"}:
            provider = "openai"
        if (force_unified or self._ui_unified_api_enabled()) and provider in {"openai", "custom", "deepseek", "gemini"}:
            base_url = self._ui_value("ai_api_base_url") or base_url
            api_key = self._ui_value("ai_api_key") or api_key
            model = self._ui_value("ai_api_text_model") or model
        return {"provider": provider, "base_url": base_url, "api_key": api_key, "model": model}

    def _effective_pronunciation_dictionary_probe_args(self) -> dict:
        args = self._effective_llm_probe_args()
        if not self._ui_enabled("pronunciation_dictionary_dedicated_api_enabled", False):
            return args
        return {
            "provider": args["provider"],
            "base_url": self._ui_value("pronunciation_dictionary_base_url") or args["base_url"],
            "api_key": self._ui_value("pronunciation_dictionary_api_key") or args["api_key"],
            "model": self._ui_value("pronunciation_dictionary_model") or args["model"],
        }

    def _effective_image_probe_args(self, kind: str = "image", *, force_unified: bool = False) -> dict:
        if kind == "cover":
            provider = self._ui_value("cover_provider", "same_as_image")
            base_url = self._ui_value("cover_base_url")
            api_key = self._ui_value("cover_api_key")
            model = self._ui_value("cover_model")
        elif kind == "character_reference":
            provider = self._ui_value("character_reference_provider", "same_as_image")
            base_url = self._ui_value("character_reference_base_url")
            api_key = self._ui_value("character_reference_api_key")
            model = self._ui_value("character_reference_model")
        elif kind == "scene_reference":
            provider = self._ui_value("scene_reference_provider", "same_as_image")
            base_url = self._ui_value("scene_reference_base_url")
            api_key = self._ui_value("scene_reference_api_key")
            model = self._ui_value("scene_reference_model")
        else:
            provider = self._ui_value("image_provider", "placeholder")
            base_url = self._ui_value("image_base_url")
            api_key = self._ui_value("image_api_key")
            model = self._ui_value("image_model")

        if provider in {"", "same_as_image", "同配图", "同图片"}:
            provider = self._ui_value("image_provider", "placeholder")
            base_url = base_url or self._ui_value("image_base_url")
            api_key = api_key or self._ui_value("image_api_key")
            model = model or self._ui_value("image_model")

        if force_unified or self._ui_unified_api_enabled():
            # Keep probes on the exact same route as real generation: when the
            # unified switch is on, scene, cover and reference images all use
            # the OpenAI-compatible unified endpoint.
            provider = "openai"
            base_url = self._ui_value("ai_api_base_url") or base_url
            api_key = self._ui_value("ai_api_key") or api_key
            model = self._ui_value("ai_api_image_model") or model
        return {"provider": provider, "base_url": base_url, "api_key": api_key, "model": model}

    def _emit_dependency_startup_logs(self):
        lines = []
        try:
            if report_to_lines is not None:
                lines = report_to_lines(_DEPENDENCY_STARTUP_REPORT)
            else:
                lines = list(_DEPENDENCY_STARTUP_REPORT.get("logs") or [])
        except Exception:
            lines = list(_DEPENDENCY_STARTUP_REPORT.get("logs") or [])
        for line in lines[-12:]:
            self._append_probe_log(str(line))
        self._refresh_config_form()

    def _maybe_start_dependency_check(self):
        if not bool(config.get("dependency_check_on_startup", True)):
            return
        self._run_dependency_check(silent=True)

    def _manual_dependency_check(self):
        try:
            self._apply_config_form(save_profile=False)
        except Exception:
            pass
        self._run_dependency_check(silent=False)

    def _open_feedback_url(self):
        try:
            self._apply_config_form(save_profile=False)
        except Exception:
            pass
        url = str(config.get("feedback_issue_url", "") or "").strip()
        if not url:
            messagebox.showwarning("未配置反馈入口", "请先填写反馈入口 URL。")
            return
        try:
            webbrowser.open(url)
        except Exception as exc:
            messagebox.showerror("无法打开反馈入口", str(exc))

    def _run_dependency_check(self, *, silent: bool):
        if ensure_dependencies is None:
            messagebox.showerror("依赖检测不可用", str(_DEPENDENCY_STARTUP_REPORT.get("summary", "依赖检测模块加载失败")))
            return
        if self._dependency_check_running:
            if not silent:
                messagebox.showinfo("依赖检测", "依赖检测正在运行，请稍等。")
            return
        self._dependency_check_running = True
        self._append_probe_log("[依赖检测] 正在检测 Python 包 / FFmpeg / Chrome / 上传脚本...")

        def worker():
            try:
                report = ensure_dependencies(scope="full")
            except Exception as exc:
                self.root.after(0, lambda: self._show_dependency_check_error(exc, silent))
                return
            self.root.after(0, lambda: self._show_dependency_check_result(report, silent))

        threading.Thread(target=worker, daemon=True).start()

    def _show_dependency_check_error(self, exc: Exception, silent: bool):
        self._dependency_check_running = False
        self._append_probe_log(f"[依赖检测] 失败：{exc}")
        if not silent:
            messagebox.showerror("依赖检测失败", str(exc))

    def _show_dependency_check_result(self, report: dict, silent: bool):
        self._dependency_check_running = False
        self._refresh_config_form()
        lines = report_to_lines(report) if report_to_lines is not None else list(report.get("logs") or [])
        for line in lines[-18:]:
            self._append_probe_log(str(line))
        summary = str(report.get("summary") or "依赖检测完成")
        if not silent:
            if bool(report.get("ok")):
                messagebox.showinfo("依赖检测完成", summary)
            else:
                messagebox.showwarning("依赖仍有异常", summary)

    def _maybe_start_hardware_autotune(self):
        if not bool(config.get("hardware_autotune_enabled", True)):
            return
        self._run_hardware_autotune(force=False, silent=True)

    def _maybe_check_update_on_startup(self):
        if not bool(config.get("update_check_on_startup", True)):
            return
        if not str(config.get("update_manifest_url", "") or "").strip():
            return

        def worker():
            try:
                from app.updater import check_for_update

                info = check_for_update(config)
            except Exception as exc:
                self.root.after(0, lambda: self._append_probe_log(f"[软件更新] 检查失败：{exc}"))
                return
            if info:
                self.root.after(0, lambda: self._notify_update_available(info))

        threading.Thread(target=worker, daemon=True).start()

    def _notify_update_available(self, info):
        self._append_probe_log(f"[软件更新] 发现新版本 v{info.version}")
        tab = getattr(self, "update_tab", None)
        if tab is not None:
            tab.check_done(info)
            nb = getattr(self, "_right_notebook", None)
            if nb is not None:
                nb.select(tab.frame)
        messagebox.showinfo("发现新版本", f"发现新版本 v{info.version}，可在「软件更新」页下载。")

    def _manual_hardware_autotune(self):
        try:
            self._apply_config_form(save_profile=False)
        except Exception:
            pass
        self._run_hardware_autotune(force=True, silent=False)

    def _run_hardware_autotune(self, *, force: bool, silent: bool):
        if not silent or not bool(config.get("hardware_autotune_done", False)):
            self._append_probe_log("[硬件检测] 正在扫描 CPU / 内存 / 显卡 / FFmpeg 硬编能力...")

        def worker():
            try:
                changed, result = run_startup_autotune(force=force)
            except Exception as exc:
                self.root.after(0, lambda: self._show_hardware_autotune_error(exc, silent))
                return
            self.root.after(0, lambda: self._show_hardware_autotune_result(changed, result, silent))

        threading.Thread(target=worker, daemon=True).start()

    def _show_hardware_autotune_error(self, exc: Exception, silent: bool):
        text = f"[硬件检测] 失败：{exc}"
        self._append_probe_log(text)
        if not silent:
            messagebox.showerror("硬件检测失败", str(exc))

    def _show_hardware_autotune_result(self, changed: bool, result: dict, silent: bool):
        if silent and str(result.get("status") or "") in {"disabled", "skipped"}:
            return
        self._refresh_config_form()
        if hasattr(self, "profile_combo"):
            self.profile_combo.configure(values=config.list_profiles())
        message = str(result.get("message") or result.get("status") or "硬件检测完成")
        suffix = "已更新配置。" if changed else "配置已是推荐值。"
        self._append_probe_log(f"[硬件检测] {message} {suffix}")
        if not silent:
            messagebox.showinfo("硬件检测完成", f"{message}\n\n{suffix}")

    def _probe_api(self, kind: str):
        title = {
            "tts": "TTS API",
            "llm": "分镜 LLM",
            "unified_llm": "统一文本 API",
            "unified_image": "统一图片 API",
            "pronunciation_dictionary": "生词词典 API",
            "image": "图片 API",
            "cover": "封面 API",
            "character_reference": "人设图 API",
            "scene_reference": "剧情参考 API",
        }.get(kind, "API")
        self._append_probe_log(f"[API检测] {title} 检测中...")

        def worker():
            try:
                if kind == "tts":
                    result = probe_tts(
                        provider=self._ui_value("tts_provider", "edge"),
                        base_url=self._ui_value("tts_base_url"),
                        api_key=self._ui_value("tts_api_key"),
                        voice=self._ui_value("tts_voice"),
                        model=self._ui_value("tts_model"),
                    )
                elif kind == "llm":
                    args = self._effective_llm_probe_args()
                    result = probe_llm(
                        provider=args["provider"],
                        base_url=args["base_url"],
                        api_key=args["api_key"],
                        model=args["model"],
                    )
                elif kind == "unified_llm":
                    args = self._effective_llm_probe_args(force_unified=True)
                    result = probe_llm(
                        provider=args["provider"],
                        base_url=args["base_url"],
                        api_key=args["api_key"],
                        model=args["model"],
                    )
                elif kind == "pronunciation_dictionary":
                    args = self._effective_pronunciation_dictionary_probe_args()
                    result = probe_llm(
                        provider=args["provider"],
                        base_url=args["base_url"],
                        api_key=args["api_key"],
                        model=args["model"],
                    )
                elif kind == "unified_image":
                    args = self._effective_image_probe_args("image", force_unified=True)
                    result = probe_image(
                        provider=args["provider"],
                        base_url=args["base_url"],
                        api_key=args["api_key"],
                        model=args["model"],
                        name="统一图片 API",
                    )
                elif kind == "image":
                    args = self._effective_image_probe_args("image")
                    result = probe_image(
                        provider=args["provider"],
                        base_url=args["base_url"],
                        api_key=args["api_key"],
                        model=args["model"],
                        name="图片 API",
                    )
                elif kind == "cover":
                    args = self._effective_image_probe_args("cover")
                    result = probe_image(
                        provider=args["provider"],
                        base_url=args["base_url"],
                        api_key=args["api_key"],
                        model=args["model"],
                        name="封面 API",
                    )
                elif kind == "character_reference":
                    args = self._effective_image_probe_args("character_reference")
                    result = probe_image(
                        provider=args["provider"],
                        base_url=args["base_url"],
                        api_key=args["api_key"],
                        model=args["model"],
                        name="人设图 API",
                    )
                elif kind == "scene_reference":
                    args = self._effective_image_probe_args("scene_reference")
                    result = probe_image(
                        provider=args["provider"],
                        base_url=args["base_url"],
                        api_key=args["api_key"],
                        model=args["model"],
                        name="剧情参考 API",
                    )
                else:
                    raise ValueError(f"unknown probe kind: {kind}")
            except Exception as exc:
                self.root.after(0, lambda exc=exc: messagebox.showerror("API 检测失败", redact_secret_text(exc)))
                return
            self.root.after(0, lambda: self._show_probe_result(kind, result))

        threading.Thread(target=worker, daemon=True).start()

    def _append_probe_log(self, text: str):
        try:
            self.log_text.insert(tk.END, redact_secret_text(text).rstrip() + "\n")
            self.log_text.see(tk.END)
        except Exception:
            pass

    def _refresh_tts_voice_options(self):
        """Keep the voice picker meaningful for the currently selected TTS."""
        provider = self._ui_value("tts_provider").strip().lower()
        widget = self.combo_widgets.get("tts_voice")
        var = self.vars.get("tts_voice")
        if widget is None or var is None:
            return
        choices = VOICEVOX_FREQUENT_VOICES if provider == "voicevox" else EDGE_MULTI_VOICES
        widget.configure(values=choices)
        if self._ui_value("tts_voice") not in choices:
            var.set(choices[0])

    def _show_probe_result(self, kind: str, result):
        text = result.to_text()
        target_key = {
            "llm": "llm_model",
            "unified_llm": "ai_api_text_model",
            "unified_image": "ai_api_image_model",
            "pronunciation_dictionary": "pronunciation_dictionary_model",
            "image": "image_model",
            "cover": "cover_model",
            "character_reference": "character_reference_model",
            "scene_reference": "scene_reference_model",
        }.get(kind)
        if kind == "tts" and self._ui_value("tts_provider") in {"openai", "custom"}:
            target_key = "tts_model"
        elif kind == "tts" and self._ui_value("tts_provider") in {"edge", "elevenlabs"}:
            target_key = "tts_voice"
        if target_key and result.models:
            widget = self.combo_widgets.get(target_key)
            if widget is not None:
                widget.configure(values=result.models)
            text += f"\n\n已识别 {len(result.models)} 个候选，已更新 {target_key} 下拉列表。"
        current_value = self._ui_value(target_key) if target_key else ""
        should_fill = (
            target_key
            and result.suggested_model
            and (
                not current_value
                or (bool(result.models) and current_value not in result.models)
            )
        )
        if should_fill:
            var = self.vars.get(target_key)
            if var is not None:
                var.set(result.suggested_model)
                text += f"\n\n已自动填入 {target_key}: {result.suggested_model}"
        self._append_probe_log(text)
        self._show_probe_result_window("API 检测通过" if result.ok else "API 检测未通过", text, ok=result.ok)

    def _show_probe_result_window(self, title: str, text: str, *, ok: bool):
        win = tk.Toplevel(self.root)
        win.title(title)
        win.geometry("760x560")
        win.minsize(520, 340)
        try:
            win.transient(self.root)
        except Exception:
            pass

        outer = ttk.Frame(win, padding=10)
        outer.pack(fill=tk.BOTH, expand=True)
        status = "状态：通过" if ok else "状态：未通过/需检查"
        ttk.Label(
            outer,
            text=f"{status}。模型列表已放在下面，可滚动查看；按钮固定在窗口底部。",
            foreground="#26733f" if ok else "#9a5b00",
            wraplength=700,
        ).pack(anchor=tk.W, pady=(0, 8))

        box = scrolledtext.ScrolledText(outer, wrap=tk.WORD, font=(MONO_FONT, UI_SMALL_FONT_SIZE), height=24)
        box.pack(fill=tk.BOTH, expand=True)
        box.insert("1.0", redact_secret_text(text))
        box.configure(state=tk.DISABLED)

        btns = ttk.Frame(outer)
        btns.pack(fill=tk.X, pady=(8, 0))

        def copy_text():
            try:
                self.root.clipboard_clear()
                self.root.clipboard_append(redact_secret_text(text))
            except Exception:
                pass

        ttk.Button(btns, text="复制结果", command=copy_text).pack(side=tk.LEFT)
        ttk.Button(btns, text="关闭", style="Primary.TButton", command=win.destroy).pack(side=tk.RIGHT)
        try:
            win.bind("<Escape>", lambda _event: win.destroy())
            win.focus_set()
        except Exception:
            pass

    def _edit_upload_profiles_dialog(self):
        raw_profiles = self._ui_value("browser_profiles", str(config.get("browser_profiles", "[]") or "[]"))
        profiles = _parse_upload_profiles(raw_profiles)
        current = {"idx": -1}
        busy = {"selecting": False}

        win = tk.Toplevel(self.root)
        win.title("频道上传方案")
        win.geometry("980x620")
        win.minsize(820, 500)
        try:
            win.transient(self.root)
        except Exception:
            pass

        root = ttk.Frame(win, padding=10)
        root.pack(fill=tk.BOTH, expand=True)

        top = ttk.Frame(root)
        top.pack(fill=tk.X, pady=(0, 8))
        multi_var = tk.StringVar(value=self._ui_value("browser_upload_all_profiles", "关闭"))
        ttk.Label(top, text="上传模式").pack(side=tk.LEFT)
        ttk.Combobox(top, textvariable=multi_var, values=["关闭", "开启"], state="readonly", width=8).pack(side=tk.LEFT, padx=(6, 16))
        ttk.Label(top, text="关闭时只上传当前频道；开启时按左侧顺序上传全部启用频道。", foreground="#666").pack(side=tk.LEFT)

        pane = ttk.PanedWindow(root, orient=tk.HORIZONTAL)
        pane.pack(fill=tk.BOTH, expand=True)

        left = ttk.Frame(pane)
        right = ttk.Frame(pane)
        pane.add(left, weight=1)
        pane.add(right, weight=3)

        ttk.Label(left, text="频道列表", font=(UI_FONT, UI_HEADING_FONT_SIZE, "bold")).pack(anchor=tk.W)
        list_wrap = ttk.Frame(left)
        list_wrap.pack(fill=tk.BOTH, expand=True, pady=(4, 6))
        list_scroll = ttk.Scrollbar(list_wrap, orient=tk.VERTICAL)
        profile_list = tk.Listbox(list_wrap, height=16, exportselection=False, yscrollcommand=list_scroll.set)
        list_scroll.configure(command=profile_list.yview)
        list_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        profile_list.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        left_btns = ttk.Frame(left)
        left_btns.pack(fill=tk.X)

        active_row = ttk.Frame(left)
        active_row.pack(fill=tk.X, pady=(8, 0))
        ttk.Label(active_row, text="当前频道").pack(side=tk.LEFT)
        active_var = tk.StringVar(value=self._ui_value("browser_active_profile", profiles[0]["name"] if profiles else ""))
        active_combo = ttk.Combobox(active_row, textvariable=active_var, state="readonly", width=18)
        active_combo.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(6, 0))

        action_box = ttk.LabelFrame(left, text="频道操作", padding=6)
        action_box.pack(fill=tk.X, pady=(8, 0))
        status = ttk.Label(left, text="", foreground="#555", wraplength=260)
        status.pack(fill=tk.X, pady=(8, 0))

        settings_tabs = ttk.Notebook(right)
        settings_tabs.pack(fill=tk.BOTH, expand=True)
        form = ttk.Frame(settings_tabs, padding=(0, 2, 0, 0))
        schedule_form = ttk.Frame(settings_tabs, padding=10)
        script_schedule_form = ttk.Frame(settings_tabs, padding=10)
        settings_tabs.add(form, text="上传方案")
        settings_tabs.add(schedule_form, text="油管内定时")
        settings_tabs.add(script_schedule_form, text="脚本内定时")

        def row(label: str):
            line = ttk.Frame(form)
            line.pack(fill=tk.X, pady=3)
            ttk.Label(line, text=label, width=16, anchor=tk.E).pack(side=tk.LEFT)
            return line

        name_var = tk.StringVar()
        enabled_var = tk.StringVar(value="开启")
        mode_var = tk.StringVar(value="immediate")
        direct_enabled_var = tk.StringVar(value="开启")
        line = row("直接发布")
        direct_enabled_widget = ttk.Combobox(
            line, textvariable=direct_enabled_var, values=["关闭", "开启"], state="readonly", width=8
        )
        direct_enabled_widget.pack(side=tk.LEFT, padx=(6, 6))
        ttk.Label(line, text="制作完成后立即上传；与两种定时方式互斥", foreground="#777").pack(side=tk.LEFT)

        line = row("频道名称")
        ttk.Entry(line, textvariable=name_var, width=28).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(6, 6))
        ttk.Combobox(line, textvariable=enabled_var, values=["开启", "关闭"], state="readonly", width=8).pack(side=tk.LEFT)

        chrome_var = tk.StringVar()
        line = row("Chrome 资料")
        ttk.Entry(line, textvariable=chrome_var, width=28).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(6, 6))
        ttk.Label(line, text="每个频道用独立资料名保存登录态", foreground="#777").pack(side=tk.LEFT)

        flow_var = tk.StringVar(value="simple")
        line = row("上传流程")
        ttk.Radiobutton(line, text="精简：跳过创收/广告/分级", variable=flow_var, value="simple").pack(side=tk.LEFT, padx=(6, 12))
        ttk.Radiobutton(line, text="完整：政策/创收/广告/分级", variable=flow_var, value="full").pack(side=tk.LEFT)

        visibility_var = tk.StringVar(value=UPLOAD_VISIBILITY_LABELS["PRIVATE"])
        line = row("公开范围")
        visibility_widget = ttk.Combobox(
            line,
            textvariable=visibility_var,
            values=list(UPLOAD_VISIBILITY_VALUES.keys()),
            state="readonly",
            width=22,
        )
        visibility_widget.pack(side=tk.LEFT, padx=(6, 0))

        title_var = tk.StringVar()
        line = row("标题模板")
        ttk.Entry(line, textvariable=title_var).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(6, 0))

        series_title_var = tk.StringVar()
        line = row("系列共通标题")
        ttk.Entry(line, textvariable=series_title_var).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(6, 0))
        ttk.Label(line, text="可留空；例如《某某小说》", foreground="#777").pack(side=tk.LEFT, padx=(6, 0))

        series_format_var = tk.StringVar(value="第{episode}话")
        line = row("分集格式")
        ttk.Entry(line, textvariable=series_format_var, width=22).pack(side=tk.LEFT, padx=(6, 0))
        ttk.Label(line, text="支持 {episode}、{total}；例如 上篇 / 第{episode}集", foreground="#777").pack(side=tk.LEFT, padx=(6, 0))

        series_episode_var = tk.StringVar(value="")
        series_total_var = tk.StringVar(value="")
        line = row("当前集 / 总集")
        ttk.Entry(line, textvariable=series_episode_var, width=8).pack(side=tk.LEFT, padx=(6, 4))
        ttk.Label(line, text="/", foreground="#777").pack(side=tk.LEFT)
        ttk.Entry(line, textvariable=series_total_var, width=8).pack(side=tk.LEFT, padx=(4, 0))

        line = row("说明模板")
        desc_text = tk.Text(line, height=4, wrap=tk.WORD, font=(UI_FONT, UI_SMALL_FONT_SIZE))
        desc_text.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(6, 0))

        policy_var = tk.StringVar(value="BTRA")
        line = row("上传政策")
        policy_entry = ttk.Combobox(line, textvariable=policy_var, values=["BTRA"], state="normal", width=24)
        policy_entry.pack(side=tk.LEFT, padx=(6, 6))
        policy_tip = ttk.Label(line, text="仅完整流程使用，例如 BTRA", foreground="#777")
        policy_tip.pack(side=tk.LEFT)

        ad_interval_var = tk.StringVar(value="60")
        ad_start_var = tk.StringVar(value="0")
        line = row("广告位")
        ad_interval_entry = ttk.Entry(line, textvariable=ad_interval_var, width=8)
        ad_interval_entry.pack(side=tk.LEFT, padx=(6, 4))
        ttk.Label(line, text="秒间隔，起始").pack(side=tk.LEFT)
        ad_start_entry = ttk.Entry(line, textvariable=ad_start_var, width=8)
        ad_start_entry.pack(side=tk.LEFT, padx=(4, 4))
        ttk.Label(line, text="秒，仅完整流程使用", foreground="#777").pack(side=tk.LEFT)

        line = row("广告分级模板")
        ad_text = tk.Text(line, height=7, wrap=tk.NONE, font=(MONO_FONT, UI_SMALL_FONT_SIZE))
        ad_text.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(6, 0))

        hint = ttk.Label(
            form,
            text="标题模板可用：{candidate_title}（随机候选）、{short_title}、{clean_title}、{title}、{intro}、{author}、{tags}、{job_id}。模板文字优先，剩余字数才补内容标签；脚本绝不会自动加【朗読・小説】。",
            foreground="#666",
            wraplength=640,
        )
        hint.pack(anchor=tk.W, pady=(5, 0), padx=(120, 0))

        ttk.Label(schedule_form, text="油管内定时（测试）", font=(UI_FONT, UI_HEADING_FONT_SIZE, "bold")).pack(anchor=tk.W)
        ttk.Label(
            schedule_form,
            text="测试阶段仅支持一个已完成视频。开启后会在 YouTube Studio 中填写日期、时间并点击“预定”。",
            foreground="#666",
            wraplength=620,
        ).pack(anchor=tk.W, pady=(4, 12))

        schedule_enabled_var = tk.StringVar(value="关闭")
        default_schedule_date = str(config.get("youtube_schedule_date", "") or "").strip()
        if not default_schedule_date:
            default_schedule_date = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
        schedule_year, schedule_month, schedule_day = _future_numeric_date_parts(default_schedule_date)
        schedule_hour, schedule_minute = _split_numeric_time(str(config.get("youtube_schedule_time", "18:00") or "18:00"))
        schedule_year_var = tk.StringVar(value=schedule_year)
        schedule_month_var = tk.StringVar(value=schedule_month)
        schedule_day_var = tk.StringVar(value=schedule_day)
        schedule_hour_var = tk.StringVar(value=schedule_hour)
        schedule_minute_var = tk.StringVar(value=schedule_minute)
        schedule_timezone_var = tk.StringVar(value=str(config.get("youtube_schedule_timezone", "Asia/Tokyo") or "Asia/Tokyo"))

        schedule_rows = []

        def numeric_part_entry(parent, variable: tk.StringVar, width: int, max_digits: int):
            validate = (win.register(lambda value, limit=max_digits: (not value) or (value.isdigit() and len(value) <= limit)), "%P")
            widget = ttk.Entry(parent, textvariable=variable, width=width, validate="key", validatecommand=validate)
            widget.pack(side=tk.LEFT, padx=(8, 2))
            return widget

        def schedule_row(label: str):
            line = ttk.Frame(schedule_form)
            line.pack(fill=tk.X, pady=4)
            ttk.Label(line, text=label, width=12, anchor=tk.E).pack(side=tk.LEFT)
            schedule_rows.append(line)
            return line

        line = schedule_row("启用定时")
        schedule_enabled_widget = ttk.Combobox(
            line, textvariable=schedule_enabled_var, values=["关闭", "开启"], state="readonly", width=8
        )
        schedule_enabled_widget.pack(side=tk.LEFT, padx=(8, 0))

        line = schedule_row("发布日期")
        schedule_year_widget = numeric_part_entry(line, schedule_year_var, 6, 4)
        ttk.Label(line, text="年").pack(side=tk.LEFT)
        schedule_month_widget = numeric_part_entry(line, schedule_month_var, 4, 2)
        ttk.Label(line, text="月").pack(side=tk.LEFT)
        schedule_day_widget = numeric_part_entry(line, schedule_day_var, 4, 2)
        ttk.Label(line, text="日").pack(side=tk.LEFT)

        line = schedule_row("发布时间")
        schedule_hour_widget = numeric_part_entry(line, schedule_hour_var, 4, 2)
        ttk.Label(line, text="时").pack(side=tk.LEFT)
        schedule_minute_widget = numeric_part_entry(line, schedule_minute_var, 4, 2)
        ttk.Label(line, text="分").pack(side=tk.LEFT)

        line = schedule_row("时区")
        schedule_timezone_widget = ttk.Entry(line, textvariable=schedule_timezone_var, width=20)
        schedule_timezone_widget.pack(side=tk.LEFT, padx=(8, 0))

        schedule_test_btn = ttk.Button(schedule_form, text="测试定时上传选中视频")
        schedule_test_btn.pack(anchor=tk.W, pady=(14, 0))
        ttk.Label(
            schedule_form,
            text="定时测试会忽略上传方案中的公开范围，改用“安排时间”。请先在任务列表只选一个已完成视频。",
            foreground="#9a5b00",
            wraplength=620,
        ).pack(anchor=tk.W, pady=(6, 0))

        ttk.Label(script_schedule_form, text="脚本内定时", font=(UI_FONT, UI_HEADING_FONT_SIZE, "bold")).pack(anchor=tk.W)
        ttk.Label(
            script_schedule_form,
            text="视频制作完成后进入本地待发布队列，到点才由脚本打开对应频道并上传。软件必须保持运行，Mac 不能深度睡眠。",
            foreground="#666",
            wraplength=620,
        ).pack(anchor=tk.W, pady=(4, 10))

        script_enabled_var = tk.StringVar(value="关闭")
        default_script_date = str(config.get("script_schedule_first_date", "") or "").strip()
        if not default_script_date:
            default_script_date = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
        script_year, script_month, script_day = _split_numeric_date(default_script_date)
        script_hour, script_minute = _split_numeric_time(str(config.get("script_schedule_time", "18:00") or "18:00"))
        script_year_var = tk.StringVar(value=script_year)
        script_month_var = tk.StringVar(value=script_month)
        script_day_var = tk.StringVar(value=script_day)
        script_hour_var = tk.StringVar(value=script_hour)
        script_minute_var = tk.StringVar(value=script_minute)
        script_interval_var = tk.StringVar(value=str(config.get("script_schedule_interval_hours", 24) or 24))
        script_timezone_var = tk.StringVar(value=str(config.get("script_schedule_timezone", "Asia/Tokyo") or "Asia/Tokyo"))
        script_missed_var = tk.StringVar(value="顺延到下一个时间")
        script_unfinished_var = tk.StringVar(value="顺延到下一个时间")

        def script_row(label: str):
            line = ttk.Frame(script_schedule_form)
            line.pack(fill=tk.X, pady=3)
            ttk.Label(line, text=label, width=14, anchor=tk.E).pack(side=tk.LEFT)
            return line

        line = script_row("启用定时")
        script_enabled_widget = ttk.Combobox(
            line, textvariable=script_enabled_var, values=["关闭", "开启"], state="readonly", width=8
        )
        script_enabled_widget.pack(side=tk.LEFT, padx=(8, 0))
        line = script_row("首次发布日期")
        script_year_widget = numeric_part_entry(line, script_year_var, 6, 4)
        ttk.Label(line, text="年").pack(side=tk.LEFT)
        script_month_widget = numeric_part_entry(line, script_month_var, 4, 2)
        ttk.Label(line, text="月").pack(side=tk.LEFT)
        script_day_widget = numeric_part_entry(line, script_day_var, 4, 2)
        ttk.Label(line, text="日").pack(side=tk.LEFT)
        line = script_row("每天发布时间")
        script_hour_widget = numeric_part_entry(line, script_hour_var, 4, 2)
        ttk.Label(line, text="时").pack(side=tk.LEFT)
        script_minute_widget = numeric_part_entry(line, script_minute_var, 4, 2)
        ttk.Label(line, text="分").pack(side=tk.LEFT)
        line = script_row("发布间隔")
        script_interval_widget = ttk.Entry(line, textvariable=script_interval_var, width=8)
        script_interval_widget.pack(side=tk.LEFT, padx=(8, 6))
        ttk.Label(line, text="小时（推荐 24）", foreground="#777").pack(side=tk.LEFT)
        line = script_row("时区")
        script_timezone_widget = ttk.Entry(line, textvariable=script_timezone_var, width=20)
        script_timezone_widget.pack(side=tk.LEFT, padx=(8, 0))
        line = script_row("错过发布时间")
        script_missed_widget = ttk.Combobox(
            line, textvariable=script_missed_var, values=["顺延到下一个时间", "立即补发"], state="readonly", width=18
        )
        script_missed_widget.pack(side=tk.LEFT, padx=(8, 0))
        line = script_row("视频尚未完成")
        script_unfinished_widget = ttk.Combobox(
            line, textvariable=script_unfinished_var, values=["顺延到下一个时间", "完成后立即补发"], state="readonly", width=18
        )
        script_unfinished_widget.pack(side=tk.LEFT, padx=(8, 0))

        queue_box = ttk.LabelFrame(script_schedule_form, text="待发布队列（同系列连续、集数升序）", padding=5)
        queue_box.pack(fill=tk.BOTH, expand=True, pady=(8, 0))
        queue_tree = ttk.Treeview(
            queue_box,
            columns=("order", "title", "series", "episode", "time", "state"),
            show="headings",
            height=7,
        )
        for column, label, width in (
            ("order", "顺序", 45), ("title", "视频", 150), ("series", "系列", 110),
            ("episode", "集数", 50), ("time", "计划时间", 130), ("state", "状态", 80),
        ):
            queue_tree.heading(column, text=label)
            queue_tree.column(column, width=width, minwidth=width, stretch=column in {"title", "series"})
        queue_tree.pack(fill=tk.BOTH, expand=True)
        queue_btns = ttk.Frame(script_schedule_form)
        queue_btns.pack(fill=tk.X, pady=(5, 0))
        rebuild_queue_btn = ttk.Button(queue_btns, text="重新识别并计算队列")
        rebuild_queue_btn.pack(side=tk.LEFT)
        queue_up_btn = ttk.Button(queue_btns, text="上移")
        queue_up_btn.pack(side=tk.LEFT, padx=(5, 0))
        queue_down_btn = ttk.Button(queue_btns, text="下移")
        queue_down_btn.pack(side=tk.LEFT, padx=(5, 0))
        queue_edit_btn = ttk.Button(queue_btns, text="修改系列/集数")
        queue_edit_btn.pack(side=tk.LEFT, padx=(5, 0))
        ttk.Label(queue_btns, text="没有“第1话/第2期”等标记的文件按独立故事处理", foreground="#777").pack(side=tk.LEFT, padx=(8, 0))

        form_btns = ttk.Frame(right)
        form_btns.pack(fill=tk.X, pady=(8, 0))

        def set_status(text: str, color: str = "#555"):
            status.configure(text=text, foreground=color)

        def profile_line(profile: dict) -> str:
            enabled = "启用" if profile.get("enabled", True) else "停用"
            flow = "精简" if profile.get("flow") == "simple" else "完整"
            mode = {"immediate": "直接", "youtube": "油管定时", "script": "脚本定时"}.get(
                str(profile.get("publish_mode") or "immediate"), "直接"
            )
            return f"{enabled} | {mode} | {flow} | {profile.get('chrome_profile', 'Default')} | {profile.get('name', '')}"

        def refresh_flow_ui(*_args):
            full = flow_var.get() == "full"
            state = tk.NORMAL if full else tk.DISABLED
            for widget in (policy_entry, ad_interval_entry, ad_start_entry):
                widget.configure(state=state)
            ad_text.configure(state=state)
            policy_tip.configure(foreground="#777" if full else "#aaa")

        flow_var.trace_add("write", refresh_flow_ui)

        mode_switch = {"busy": False}

        def refresh_schedule_ui(*_args):
            youtube_enabled = mode_var.get() == "youtube"
            script_enabled = mode_var.get() == "script"
            mode_switch["busy"] = True
            direct_enabled_var.set("开启" if mode_var.get() == "immediate" else "关闭")
            schedule_enabled_var.set("开启" if youtube_enabled else "关闭")
            script_enabled_var.set("开启" if script_enabled else "关闭")
            mode_switch["busy"] = False
            state = tk.NORMAL if youtube_enabled else tk.DISABLED
            for widget in (
                schedule_year_widget, schedule_month_widget, schedule_day_widget,
                schedule_hour_widget, schedule_minute_widget,
                schedule_timezone_widget, schedule_test_btn,
            ):
                widget.configure(state=state)
            script_state = tk.NORMAL if script_enabled else tk.DISABLED
            for widget in (
                script_year_widget, script_month_widget, script_day_widget,
                script_hour_widget, script_minute_widget,
                script_interval_widget, script_timezone_widget,
                script_missed_widget, script_unfinished_widget, rebuild_queue_btn,
                queue_up_btn, queue_down_btn, queue_edit_btn,
            ):
                widget.configure(state=script_state)
            visibility_widget.configure(state="disabled" if youtube_enabled else "readonly")

        def request_mode(target: str, variable: tk.StringVar):
            if mode_switch["busy"]:
                return
            current_mode = mode_var.get()
            requested_on = variable.get() == "开启"
            if requested_on and current_mode != target:
                labels = {"immediate": "直接发布", "youtube": "油管内定时", "script": "脚本内定时"}
                if not messagebox.askyesno(
                    "切换发布方式",
                    f"当前频道正在使用“{labels.get(current_mode, current_mode)}”。\n\n"
                    f"确定切换至“{labels[target]}”吗？切换后其他冲突设置会被锁定。",
                    parent=win,
                ):
                    refresh_schedule_ui()
                    return
                mode_var.set(target)
                if target in {"youtube", "script"}:
                    multi_var.set("关闭")
                    active_var.set(name_var.get().strip())
            elif not requested_on and current_mode == target:
                if target == "immediate":
                    refresh_schedule_ui()
                    return
                if not messagebox.askyesno(
                    "关闭定时发布",
                    "关闭当前定时方式后，将切换为“直接发布”。确定吗？",
                    parent=win,
                ):
                    refresh_schedule_ui()
                    return
                mode_var.set("immediate")
            refresh_schedule_ui()

        direct_enabled_widget.bind("<<ComboboxSelected>>", lambda _event: request_mode("immediate", direct_enabled_var))
        schedule_enabled_widget.bind("<<ComboboxSelected>>", lambda _event: request_mode("youtube", schedule_enabled_var))
        script_enabled_widget.bind("<<ComboboxSelected>>", lambda _event: request_mode("script", script_enabled_var))

        def rebuild(select_idx: int | None = None):
            busy["selecting"] = True
            profile_list.delete(0, tk.END)
            names = []
            for profile in profiles:
                profile_list.insert(tk.END, profile_line(profile))
                names.append(str(profile.get("name") or ""))
            active_combo.configure(values=names)
            if active_var.get() not in names and names:
                active_var.set(names[0])
            if select_idx is not None and profiles:
                select_idx = max(0, min(select_idx, len(profiles) - 1))
                profile_list.selection_clear(0, tk.END)
                profile_list.selection_set(select_idx)
                profile_list.see(select_idx)
            busy["selecting"] = False

        def fill(idx: int):
            if idx < 0 or idx >= len(profiles):
                return
            profile = profiles[idx]
            current["idx"] = idx
            name_var.set(str(profile.get("name") or ""))
            enabled_var.set("开启" if profile.get("enabled", True) else "关闭")
            chrome_var.set(str(profile.get("chrome_profile") or "Default"))
            flow_var.set(_normalize_profile_flow(str(profile.get("flow") or "simple")))
            visibility = str(profile.get("visibility") or "PRIVATE").upper()
            visibility_var.set(UPLOAD_VISIBILITY_LABELS.get(visibility, UPLOAD_VISIBILITY_LABELS["PRIVATE"]))
            title_var.set(str(profile.get("title_template") or ""))
            series_title_var.set(str(profile.get("series_title") or ""))
            series_format_var.set(str(profile.get("series_format") or "第{episode}话"))
            series_episode_var.set(str(profile.get("series_episode") or ""))
            series_total_var.set(str(profile.get("series_total") or ""))
            desc_text.delete("1.0", tk.END)
            desc_text.insert("1.0", str(profile.get("description") or ""))
            policy_var.set(str(profile.get("upload_policy") or "BTRA"))
            ad_interval_var.set(str(profile.get("ad_interval") or 60))
            ad_start_var.set(str(profile.get("ad_start") or 0))
            ad_text.configure(state=tk.NORMAL)
            ad_text.delete("1.0", tk.END)
            ad_text.insert("1.0", _upload_ad_template_to_text(profile.get("ad_suitability_template") or ""))
            mode_var.set(str(profile.get("publish_mode") or "immediate"))
            year, month, day = _future_numeric_date_parts(str(profile.get("youtube_schedule_date") or ""))
            hour, minute = _split_numeric_time(str(profile.get("youtube_schedule_time") or "18:00"))
            schedule_year_var.set(year)
            schedule_month_var.set(month)
            schedule_day_var.set(day)
            schedule_hour_var.set(hour)
            schedule_minute_var.set(minute)
            schedule_timezone_var.set(str(profile.get("youtube_schedule_timezone") or "Asia/Tokyo"))
            year, month, day = _split_numeric_date(
                str(profile.get("script_schedule_first_date") or ""),
                datetime.now() + timedelta(days=1),
            )
            hour, minute = _split_numeric_time(str(profile.get("script_schedule_time") or "18:00"))
            script_year_var.set(year)
            script_month_var.set(month)
            script_day_var.set(day)
            script_hour_var.set(hour)
            script_minute_var.set(minute)
            script_interval_var.set(str(profile.get("script_schedule_interval_hours") or 24))
            script_timezone_var.set(str(profile.get("script_schedule_timezone") or "Asia/Tokyo"))
            script_missed_var.set("立即补发" if profile.get("script_schedule_missed_action") == "immediate" else "顺延到下一个时间")
            script_unfinished_var.set("完成后立即补发" if profile.get("script_schedule_unfinished_action") == "immediate" else "顺延到下一个时间")
            refresh_flow_ui()
            refresh_schedule_ui()
            refresh_queue_preview(str(profile.get("name") or ""))

        def flush(idx: int) -> bool:
            if idx < 0 or idx >= len(profiles):
                return True
            try:
                youtube_scheduled = _datetime_from_numeric_parts(
                    schedule_year_var.get(), schedule_month_var.get(), schedule_day_var.get(),
                    schedule_hour_var.get(), schedule_minute_var.get(),
                )
                script_scheduled = _datetime_from_numeric_parts(
                    script_year_var.get(), script_month_var.get(), script_day_var.get(),
                    script_hour_var.get(), script_minute_var.get(),
                )
                profile = _normalize_upload_profile(
                    {
                        "name": name_var.get(),
                        "enabled": enabled_var.get() == "开启",
                        "chrome_profile": chrome_var.get(),
                        "flow": flow_var.get(),
                        "visibility": UPLOAD_VISIBILITY_VALUES.get(visibility_var.get(), "PRIVATE"),
                        "upload_policy": policy_var.get(),
                        "ad_interval": ad_interval_var.get(),
                        "ad_start": ad_start_var.get(),
                        "title_template": title_var.get(),
                        "series_title": series_title_var.get().strip(),
                        "series_format": series_format_var.get().strip() or "第{episode}话",
                        "series_episode": series_episode_var.get().strip(),
                        "series_total": series_total_var.get().strip(),
                        "description": desc_text.get("1.0", tk.END).strip(),
                        "ad_suitability_template": _upload_ad_template_from_text(ad_text.get("1.0", tk.END)),
                        "publish_mode": mode_var.get(),
                        "youtube_schedule_date": youtube_scheduled.strftime("%Y-%m-%d"),
                        "youtube_schedule_time": youtube_scheduled.strftime("%H:%M"),
                        "youtube_schedule_timezone": schedule_timezone_var.get().strip(),
                        "script_schedule_first_date": script_scheduled.strftime("%Y-%m-%d"),
                        "script_schedule_time": script_scheduled.strftime("%H:%M"),
                        "script_schedule_interval_hours": script_interval_var.get().strip(),
                        "script_schedule_timezone": script_timezone_var.get().strip(),
                        "script_schedule_missed_action": "immediate" if script_missed_var.get() == "立即补发" else "next_slot",
                        "script_schedule_unfinished_action": "immediate" if script_unfinished_var.get() == "完成后立即补发" else "next_slot",
                        "script_manual_queue": bool(profiles[idx].get("script_manual_queue", False)),
                    },
                    idx,
                )
            except Exception as exc:
                set_status(f"保存当前频道失败：{exc}", "red")
                return False
            profiles[idx] = profile
            return True

        def on_select(_event=None):
            if busy["selecting"]:
                return
            selection = profile_list.curselection()
            if not selection:
                return
            if not flush(current["idx"]):
                return
            idx = int(selection[0])
            fill(idx)
            rebuild(idx)

        def add_profile():
            if not flush(current["idx"]):
                return
            profiles.append(
                _normalize_upload_profile(
                    {
                        "name": f"新频道{len(profiles) + 1}",
                        "enabled": True,
                        "chrome_profile": f"Account-{len(profiles) + 1}",
                        "flow": "simple",
                        "visibility": "PRIVATE",
                        "upload_policy": "BTRA",
                        "ad_interval": 60,
                        "ad_start": 0,
                    },
                    len(profiles),
                )
            )
            idx = len(profiles) - 1
            rebuild(idx)
            fill(idx)

        def delete_profile():
            selection = profile_list.curselection()
            if not selection:
                return
            if len(profiles) <= 1:
                set_status("至少保留一个频道方案。", "red")
                return
            idx = int(selection[0])
            profiles.pop(idx)
            idx = min(idx, len(profiles) - 1)
            rebuild(idx)
            fill(idx)

        def move_profile(delta: int):
            selection = profile_list.curselection()
            if not selection:
                return
            if not flush(current["idx"]):
                return
            idx = int(selection[0])
            new_idx = idx + delta
            if new_idx < 0 or new_idx >= len(profiles):
                return
            profiles[idx], profiles[new_idx] = profiles[new_idx], profiles[idx]
            rebuild(new_idx)
            fill(new_idx)

        def refresh_queue_preview(profile_name: str = ""):
            queue_tree.delete(*queue_tree.get_children())
            profile_name = profile_name or name_var.get().strip()
            state_labels = {
                "pending": "等待", "retry": "待重试", "uploading": "上传中",
                "published": "已发布", "failed": "失败", "needs_review": "需检查",
            }
            for position, item in enumerate(publish_scheduler.load_items(profile_name), start=1):
                episode = int(item.get("episode") or 0)
                scheduled = str(item.get("scheduled_at") or "").replace("T", " ")
                job_id = str(item.get("job_id") or "")
                queue_tree.insert(
                    "", tk.END, iid=job_id,
                    values=(
                        position,
                        str(item.get("display_title") or item.get("job_id") or ""),
                        str(item.get("series") or ""),
                        episode if episode else "单篇",
                        scheduled,
                        state_labels.get(str(item.get("state") or "pending"), str(item.get("state") or "")),
                    ),
                )

        def rebuild_script_queue(show_message: bool = True) -> bool:
            if not flush(current["idx"]):
                return False
            profile = profiles[current["idx"]]
            try:
                first_at = datetime.strptime(
                    f"{str(profile.get('script_schedule_first_date') or '').strip()} "
                    f"{str(profile.get('script_schedule_time') or '').strip()}",
                    "%Y-%m-%d %H:%M",
                )
                interval = max(1, int(profile.get("script_schedule_interval_hours") or 24))
                zone = ZoneInfo(str(profile.get("script_schedule_timezone") or "Asia/Tokyo"))
            except (TypeError, ValueError):
                messagebox.showerror("脚本定时格式不正确", "首次日期请填写 YYYY-MM-DD，时间填写 HH:MM，间隔填写整数小时。", parent=win)
                return False
            except Exception:
                messagebox.showerror("脚本定时时区不正确", "请填写有效时区，例如 Asia/Tokyo。", parent=win)
                return False
            schedule_now = datetime.now(zone).replace(tzinfo=None)
            if profile.get("script_schedule_missed_action") == "next_slot":
                step = timedelta(hours=interval)
                while first_at <= schedule_now:
                    first_at += step
            elif first_at <= schedule_now:
                first_at = schedule_now.replace(second=0, microsecond=0)
            statuses = [(row["job_id"], pr.load_status(row["job_id"])) for row in pr.list_jobs(limit=10000)]
            profile["script_manual_queue"] = False
            rows = publish_scheduler.rebuild_queue(str(profile.get("name") or ""), statuses, first_at, interval)
            refresh_queue_preview(str(profile.get("name") or ""))
            if show_message:
                messagebox.showinfo("队列已更新", f"已识别并安排 {len(rows)} 个尚未发布的任务。", parent=win)
            return True

        def move_queue_item(delta: int):
            selection = queue_tree.selection()
            if not selection:
                return
            profile_name = name_var.get().strip()
            if publish_scheduler.move_item(profile_name, str(selection[0]), delta):
                refresh_queue_preview(profile_name)
                queue_tree.selection_set(str(selection[0]))

        def edit_queue_item():
            selection = queue_tree.selection()
            if not selection:
                return
            job_id = str(selection[0])
            profile_name = name_var.get().strip()
            item = next((row for row in publish_scheduler.load_items(profile_name) if str(row.get("job_id") or "") == job_id), None)
            if not item:
                return
            series = simpledialog.askstring("修改系列", "请输入系列名称：", initialvalue=str(item.get("series") or ""), parent=win)
            if series is None:
                return
            episode = simpledialog.askinteger(
                "修改集数", "请输入集数；独立单篇填写 0：", initialvalue=int(item.get("episode") or 0), minvalue=0, parent=win
            )
            if episode is None:
                return
            publish_scheduler.update_item(profile_name, job_id, series=series.strip() or str(item.get("display_title") or job_id), episode=episode)
            refresh_queue_preview(profile_name)

        def write_profiles_to_form(select_idx: int | None = None):
            if not flush(current["idx"]):
                return False
            if select_idx is None:
                selection = profile_list.curselection()
                select_idx = int(selection[0]) if selection else current["idx"]
            select_idx = max(0, min(select_idx or 0, len(profiles) - 1))
            selected = profiles[select_idx]
            names = [str(p.get("name") or "") for p in profiles]
            active_name = str(active_var.get() or "").strip()
            if active_name not in names:
                active_name = str(selected.get("name") or "")
                active_var.set(active_name)
            active_profile = next((p for p in profiles if str(p.get("name") or "") == active_name), selected)
            profiles_json = json.dumps(profiles, ensure_ascii=False)
            if "browser_profiles" in self.vars:
                self.vars["browser_profiles"].set(profiles_json)
            if "browser_active_profile" in self.vars:
                self.vars["browser_active_profile"].set(active_name)
            if "browser_chrome_profile" in self.vars:
                self.vars["browser_chrome_profile"].set(str(active_profile.get("chrome_profile") or "Default"))
            if "browser_flow" in self.vars:
                self.vars["browser_flow"].set(str(active_profile.get("flow") or "simple"))
            if "youtube_visibility" in self.vars:
                self.vars["youtube_visibility"].set(str(active_profile.get("visibility") or "PRIVATE"))
            if "browser_upload_policy" in self.vars:
                self.vars["browser_upload_policy"].set(str(active_profile.get("upload_policy") or "BTRA"))
            if "browser_ad_interval" in self.vars:
                self.vars["browser_ad_interval"].set(str(active_profile.get("ad_interval") or 60))
            if "browser_ad_start" in self.vars:
                self.vars["browser_ad_start"].set(str(active_profile.get("ad_start") or 0))
            if "youtube_title_template" in self.vars and str(active_profile.get("title_template") or "").strip():
                self.vars["youtube_title_template"].set(str(active_profile.get("title_template") or ""))
            if "youtube_description" in self.vars and str(active_profile.get("description") or "").strip():
                self.vars["youtube_description"].set(str(active_profile.get("description") or ""))
            if "browser_ad_suitability_template" in self.vars and active_profile.get("ad_suitability_template"):
                self.vars["browser_ad_suitability_template"].set(_upload_ad_template_to_text(active_profile.get("ad_suitability_template")))
            if "browser_upload_all_profiles" in self.vars:
                self.vars["browser_upload_all_profiles"].set(multi_var.get())
            def set_form_value(key: str, value: str):
                if key in self.vars:
                    self.vars[key].set(value)
                else:
                    self.vars[key] = tk.StringVar(value=value)

            publish_mode = str(active_profile.get("publish_mode") or "immediate")
            set_form_value("youtube_publish_mode", publish_mode)
            set_form_value("youtube_schedule_enabled", "开启" if publish_mode == "youtube" else "关闭")
            set_form_value("youtube_schedule_date", str(active_profile.get("youtube_schedule_date") or ""))
            set_form_value("youtube_schedule_time", str(active_profile.get("youtube_schedule_time") or "18:00"))
            set_form_value("youtube_schedule_timezone", str(active_profile.get("youtube_schedule_timezone") or "Asia/Tokyo"))
            set_form_value("script_schedule_first_date", str(active_profile.get("script_schedule_first_date") or ""))
            set_form_value("script_schedule_time", str(active_profile.get("script_schedule_time") or "18:00"))
            set_form_value("script_schedule_interval_hours", str(active_profile.get("script_schedule_interval_hours") or 24))
            set_form_value("script_schedule_timezone", str(active_profile.get("script_schedule_timezone") or "Asia/Tokyo"))
            set_form_value("script_schedule_missed_action", str(active_profile.get("script_schedule_missed_action") or "next_slot"))
            set_form_value("script_schedule_unfinished_action", str(active_profile.get("script_schedule_unfinished_action") or "next_slot"))
            if publish_mode in {"youtube", "script"} and "upload_enabled" in self.vars:
                self.vars["upload_enabled"].set("开启")
            widget = self.combo_widgets.get("browser_active_profile")
            if widget is not None:
                widget.configure(values=names)
            return True

        def start_schedule_test():
            if 0 <= current["idx"] < len(profiles):
                active_var.set(str(profiles[current["idx"]].get("name") or ""))
                multi_var.set("关闭")
            if not write_profiles_to_form():
                return
            self._upload_selected_completed_jobs(schedule_test=True)

        def save_and_close():
            if not write_profiles_to_form():
                return
            selected_profile = profiles[current["idx"]]
            if selected_profile.get("publish_mode") == "script" and not rebuild_script_queue(show_message=False):
                return
            try:
                self._apply_config_form(save_profile=False)
                config.save()
            except Exception as exc:
                set_status(f"写入配置失败：{exc}", "red")
                return
            set_status("已保存频道上传方案。", "green")
            win.destroy()

        def use_selected_only():
            selection = profile_list.curselection()
            if selection:
                active_var.set(str(profiles[int(selection[0])].get("name") or ""))
            multi_var.set("关闭")
            if write_profiles_to_form():
                try:
                    self._apply_config_form(save_profile=False)
                    config.save()
                except Exception:
                    pass
                set_status(f"当前只上传：{active_var.get()}", "green")

        def enable_all_profiles():
            multi_var.set("开启")
            if write_profiles_to_form():
                try:
                    self._apply_config_form(save_profile=False)
                    config.save()
                except Exception:
                    pass
                count = sum(1 for p in profiles if p.get("enabled", True))
                set_status(f"已开启顺序上传：{count} 个启用频道。", "green")

        def open_selected_profile():
            selection = profile_list.curselection()
            idx = int(selection[0]) if selection else current["idx"]
            if 0 <= idx < len(profiles):
                active_var.set(str(profiles[idx].get("name") or ""))
            if not write_profiles_to_form(idx):
                return
            profile = profiles[idx]
            chrome_profile = str(profile.get("chrome_profile") or "Default")
            set_status(f"正在打开 Chrome 资料：{chrome_profile}", "#9a5b00")

            def worker():
                try:
                    from app.vendor.stage5_upload_browser import _restart_chrome

                    def log(line):
                        self.root.after(0, lambda line=line: set_status(str(line), "#555"))

                    ok = _restart_chrome(config, log, chrome_profile)
                    final = f"已打开：{chrome_profile}，请确认 YouTube 登录。" if ok else f"打开失败：{chrome_profile}"
                    self.root.after(0, lambda: set_status(final, "green" if ok else "red"))
                except Exception as exc:
                    self.root.after(0, lambda exc=exc: set_status(f"打开失败：{exc}", "red"))

            threading.Thread(target=worker, daemon=True).start()

        ttk.Button(left_btns, text="新增频道", command=add_profile).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(left_btns, text="删除", command=delete_profile).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(left_btns, text="上移", command=lambda: move_profile(-1)).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(left_btns, text="下移", command=lambda: move_profile(1)).pack(side=tk.LEFT)
        ttk.Button(action_box, text="打开此频道 Chrome 登录", command=open_selected_profile).pack(fill=tk.X, pady=(0, 4))
        ttk.Button(action_box, text="设为当前单频道上传", command=use_selected_only).pack(fill=tk.X, pady=(0, 4))
        ttk.Button(action_box, text="顺序上传全部启用频道", command=enable_all_profiles).pack(fill=tk.X)
        schedule_test_btn.configure(command=start_schedule_test)
        rebuild_queue_btn.configure(command=rebuild_script_queue)
        queue_up_btn.configure(command=lambda: move_queue_item(-1))
        queue_down_btn.configure(command=lambda: move_queue_item(1))
        queue_edit_btn.configure(command=edit_queue_item)
        ttk.Button(form_btns, text="保存频道方案", style="Primary.TButton", command=save_and_close).pack(side=tk.RIGHT)
        ttk.Button(form_btns, text="关闭", command=win.destroy).pack(side=tk.RIGHT, padx=(0, 6))

        profile_list.bind("<<ListboxSelect>>", on_select)
        rebuild(0)
        fill(0)
        refresh_schedule_ui()
        try:
            win.bind("<Escape>", lambda _event: win.destroy())
            win.focus_set()
        except Exception:
            pass

    def _preview_subtitle(self):
        try:
            from PIL import Image, ImageDraw, ImageFont, ImageTk
        except Exception as exc:
            messagebox.showerror("无法预览", f"Pillow/ImageTk 不可用: {exc}")
            return

        width, height = 720, 405
        img = Image.new("RGB", (width, height), (28, 34, 44))
        draw = ImageDraw.Draw(img)
        for y in range(height):
            t = y / max(1, height - 1)
            color = (int(28 + 34 * t), int(34 + 25 * t), int(44 + 40 * t))
            draw.line((0, y, width, y), fill=color)
        draw.rectangle((0, int(height * 0.68), width, height), fill=(18, 18, 22))

        sample = "蒙头睡了十三个小时 醒来一看"
        font_size = max(16, int(int(float(self._ui_value("video_subtitle_size", "48"))) * width / max(1, int(config.get("video_width", 1080) or 1080))))
        font = self._preview_font(self._ui_value("video_subtitle_font", "Hiragino Mincho ProN"), font_size)
        max_chars = max(8, int(float(self._ui_value("video_subtitle_chars_per_line", "24"))))
        lines = self._wrap_preview_text(sample, max_chars)[: max(1, int(float(self._ui_value("video_subtitle_max_lines", "2"))))]
        text = "\n".join(lines)

        bbox = draw.multiline_textbbox((0, 0), text, font=font, spacing=8)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        margin = max(16, int(int(float(self._ui_value("video_subtitle_margin_v", "60"))) * height / max(1, int(config.get("video_height", 1920) or 1920))))
        pos = self._ui_value("video_subtitle_position", "下边")
        if "上" in pos:
            y = margin
        elif "中" in pos:
            y = (height - th) / 2
        else:
            y = height - margin - th
        x = (width - tw) / 2
        fill = self._rgb(self._ui_value("video_subtitle_color", "#FFFFFF"), (255, 255, 255))
        outline_fill = self._rgb(self._ui_value("video_subtitle_outline_color", "#000000"), (0, 0, 0))
        stroke = max(0, int(float(self._ui_value("video_subtitle_outline", "4")) * width / max(1, int(config.get("video_width", 1080) or 1080))))
        shadow = max(0, int(float(self._ui_value("video_subtitle_shadow", "1")) * width / max(1, int(config.get("video_width", 1080) or 1080))))
        if shadow:
            draw.multiline_text((x + shadow, y + shadow), text, font=font, fill=(0, 0, 0), spacing=8, align="center")
        draw.multiline_text((x, y), text, font=font, fill=fill, spacing=8, align="center", stroke_width=stroke, stroke_fill=outline_fill)

        win = tk.Toplevel(self.root)
        win.title("字幕样式预览")
        photo = ImageTk.PhotoImage(img)
        label = ttk.Label(win, image=photo)
        label.image = photo
        label.pack(padx=10, pady=10)

    def _preview_font(self, font_name: str, size: int):
        from PIL import ImageFont

        fonts_dir = Path(r"C:\Windows\Fonts")
        candidates: list[str | Path] = [font_name]
        if sys.platform == "darwin":
            mac_fonts = Path("/System/Library/Fonts")
            candidates.extend(sorted(mac_fonts.glob("*明朝*ProN*.ttc")))
            candidates.extend(
                [
                    mac_fonts / "ヒラギノ明朝 ProN.ttc",
                    mac_fonts / "PingFang.ttc",
                    Path("/Library/Fonts") / font_name,
                    Path.home() / "Library" / "Fonts" / font_name,
                ]
            )
        candidates.extend([
            fonts_dir / "msyhbd.ttc",
            fonts_dir / "msyh.ttc",
            fonts_dir / "simhei.ttf",
            fonts_dir / "NotoSansSC-VF.ttf",
            "msyhbd.ttc",
            "msyh.ttc",
            "simhei.ttf",
        ])
        for candidate in candidates:
            try:
                return ImageFont.truetype(str(candidate), size)
            except Exception:
                continue
        return ImageFont.load_default()

    @staticmethod
    def _rgb(value: str, fallback: tuple[int, int, int]) -> tuple[int, int, int]:
        text = str(value or "").strip().lstrip("#")
        if len(text) == 3:
            text = "".join(ch * 2 for ch in text)
        if len(text) != 6:
            return fallback
        try:
            return int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16)
        except ValueError:
            return fallback

    @staticmethod
    def _wrap_preview_text(text: str, chars_per_line: int) -> list[str]:
        rows: list[str] = []
        current = ""
        for ch in text:
            current += ch
            if len(current) >= chars_per_line:
                rows.append(current)
                current = ""
        if current:
            rows.append(current)
        return rows or [text]

    def _search_books(self):
        keyword = self.search_var.get().strip()
        if not keyword:
            return
        self._search_seq += 1
        seq = self._search_seq
        self.search_tree.delete(*self.search_tree.get_children())
        self.search_tree.insert("", tk.END, values=("搜索中...", "", "", "", "", "", ""))
        mode = self.source_mode_var.get()
        self.search_button.configure(state=tk.DISABLED)

        def worker():
            try:
                items = pr.search_books(keyword, site="source_catalog", source=mode, media="小说", limit=20, enrich_latest=False)
                rows = [
                    (x.title, x.author, x.source, x.content_type, x.paid_label, x.latest_chapter, x.ref)
                    for x in items
                ]
                if not rows:
                    rows = [("没有搜索结果或站点超时", "", "", "", "", "", "")]
            except Exception as exc:
                rows = [(f"搜索失败: {exc}", "", "", "", "", "", "")]
            self._search_result_queue.put((seq, rows))

        threading.Thread(target=worker, daemon=True).start()
        self.root.after(100, self._poll_search_results)

    def _poll_search_results(self):
        handled = False
        while True:
            try:
                seq, rows = self._search_result_queue.get_nowait()
            except queue.Empty:
                break
            handled = True
            self._finish_search(seq, rows)
        if not handled and str(self.search_button.cget("state")) == tk.DISABLED:
            self.root.after(100, self._poll_search_results)

    def _finish_search(self, seq: int, rows):
        if seq != self._search_seq:
            return
        self._render_search(rows)
        self.search_button.configure(state=tk.NORMAL)

    def _render_search(self, rows):
        self.search_tree.delete(*self.search_tree.get_children())
        self._search_refs.clear()
        for row in rows:
            iid = self.search_tree.insert("", tk.END, values=row)
            self._search_refs[iid] = row[-1]

    def _add_selected_search(self):
        for iid in self.search_tree.selection():
            ref = self._search_refs.get(iid, "")
            values = self.search_tree.item(iid, "values")
            if ref:
                self._create_queued_job(str(values[0] or "book"), ref)

    def _add_manual_text(self):
        text = self.manual_text.get("1.0", tk.END).strip()
        if not text:
            messagebox.showwarning("没有文本", "请先粘贴文章正文。")
            return
        title = self.manual_title.get().strip() or next((x.strip() for x in text.splitlines() if x.strip()), "article")
        path = _save_import_text(title, text)
        self._create_queued_job(title, str(path))
        self.manual_text.delete("1.0", tk.END)

    def _choose_imported_mp3(self) -> str:
        path = filedialog.askopenfilename(
            title="选择已生成的完整配音 MP3",
            filetypes=[("MP3 audio", "*.mp3"), ("All files", "*.*")],
        )
        if not path:
            return ""
        try:
            pr.inspect_imported_audio(path)
        except Exception as exc:
            messagebox.showerror("MP3 无效", str(exc))
            return ""
        return path

    def _add_manual_text_with_audio(self):
        text = self.manual_text.get("1.0", tk.END).strip()
        if not text:
            messagebox.showwarning("没有文本", "请先粘贴用来生成这份 MP3 的原文。")
            return
        audio_path = self._choose_imported_mp3()
        if not audio_path:
            return
        title = self.manual_title.get().strip() or next((x.strip() for x in text.splitlines() if x.strip()), "article")
        path = _save_import_text(title, text)
        job_id = self._create_queued_job(title, str(path), imported_audio_path=audio_path)
        if job_id:
            self.manual_text.delete("1.0", tk.END)
            messagebox.showinfo(
                "正文 + MP3 已加入",
                "任务已设为跳过 TTS。\n字幕时间会按原文长度估算，视频总时长与 MP3 一致。",
            )

    def _import_files_or_folders(self):
        """Choose files/folders once, then recursively classify everything."""
        try:
            selected = _choose_import_files_and_folders()
        finally:
            if sys.platform == "darwin":
                self.root.after(100, self.root.focus_force)
        if not selected:
            return
        explicit_files = [path for path in selected if path.is_file()]
        folders, _nested_count = _remove_nested_import_folders(
            [path for path in selected if path.is_dir()]
        )
        discovered = list(explicit_files)
        for folder in folders:
            discovered.extend(
                path for path in folder.rglob("*")
                if path.is_file()
                and path.suffix.lower() in {".txt", ".mp3"}
                and path.name != ".novel_video_series_bible.json"
            )
        discovered = list(dict.fromkeys(path.resolve(strict=False) for path in discovered))
        if not discovered:
            messagebox.showwarning(
                "没有可导入文件",
                "所选内容中没有找到正文 TXT、MP3 或读音词典。",
            )
            return
        self._import_files(discovered, pair_within_parent=True)

    def _import_files(
        self,
        selected_paths: list[str | Path] | tuple[str | Path, ...] | None = None,
        *,
        pair_within_parent: bool = False,
    ):
        """Import all related files in one selection, pairing them by filename."""
        selected = selected_paths
        if selected is None:
            selected = filedialog.askopenfilenames(
                title="选择正文 TXT、可选同名 MP3 和可选同名读音词典",
                filetypes=[("Supported files", ("*.txt", "*.mp3")), ("All files", "*.*")],
            )
        if not selected:
            return
        matched = _match_import_files(list(selected), pair_within_parent=pair_within_parent)
        if not matched["pairs"]:
            messagebox.showwarning("没有正文", "请选择至少一个正文 TXT。词典 TXT 需要与正文一起选择。")
            return

        invalid_audio: list[tuple[Path, str]] = []
        invalid_dictionary: list[tuple[Path, str]] = []
        created = 0
        audio_attached = 0
        dictionary_attached = 0
        for text_path, audio_path, dictionary_path in matched["pairs"]:
            if audio_path:
                try:
                    pr.inspect_imported_audio(audio_path)
                except Exception as exc:
                    invalid_audio.append((audio_path, str(exc)))
                    audio_path = None
            if dictionary_path:
                try:
                    if self._inspect_pronunciation_dictionary_with_choices(dictionary_path) is None:
                        dictionary_path = None
                except Exception as exc:
                    invalid_dictionary.append((dictionary_path, str(exc)))
                    dictionary_path = None
            job_id = self._create_queued_job(
                text_path.stem,
                str(text_path),
                dictionary_path=str(dictionary_path) if dictionary_path else "",
                imported_audio_path=str(audio_path) if audio_path else None,
            )
            if job_id:
                created += 1
                audio_attached += int(audio_path is not None)
                dictionary_attached += int(dictionary_path is not None)

        report = [f"已创建 {created} 个任务，其中 {audio_attached} 个自动跳过 TTS，{dictionary_attached} 个自动附加读音词典。"]
        if matched["ambiguous"]:
            report.append("以下文件名匹配到多个候选文件，未导入：\n" + "\n".join(
                f"  - {text.name} 的 {kind}：{', '.join(path.name for path in candidates)}"
                for text, kind, candidates in matched["ambiguous"]
            ))
        if matched["unused_audio"]:
            report.append("未找到同名正文的 MP3（未使用）：\n" + "\n".join(f"  - {path.name}" for path in matched["unused_audio"]))
        if matched["unused_dictionaries"]:
            report.append("未找到同名正文的词典（未使用）：\n" + "\n".join(f"  - {path.name}" for path in matched["unused_dictionaries"]))
        if invalid_audio:
            report.append("无效 MP3，已作为普通 TTS 任务导入：\n" + "\n".join(f"  - {path.name}: {error}" for path, error in invalid_audio))
        if invalid_dictionary:
            report.append("无效词典，未附加：\n" + "\n".join(f"  - {path.name}: {error}" for path, error in invalid_dictionary))
        if matched["unsupported"]:
            report.append("不支持的文件：\n" + "\n".join(f"  - {path.name}" for path in matched["unsupported"]))
        report.append("配对规则：同一作品名的正文、MP3、词典自动组合；例如《作品.txt》《作品.mp3》《作品_读音词典.txt》。")
        if len(report) == 2:
            messagebox.showinfo("文件导入完成", "\n\n".join(report))
        else:
            messagebox.showwarning("文件导入完成", "\n\n".join(report))

    def _import_folder(self):
        """Recursively import selected novel folders after an explicit confirmation."""
        selected, nested_count = _remove_nested_import_folders(_choose_import_folders())
        if not selected:
            return

        imports: list[tuple[Path, dict]] = []
        total_files = 0
        total_tasks = 0
        for root in selected:
            files = [
                str(path) for path in root.rglob("*")
                if path.is_file()
                and path.suffix.lower() in {".txt", ".mp3"}
                and path.name != ".novel_video_series_bible.json"
            ]
            matched = _match_import_files(files)
            imports.append((root, matched))
            total_files += len(files)
            total_tasks += len(matched["pairs"])

        if not total_files or not total_tasks:
            messagebox.showwarning(
                "没有可导入正文",
                f"所选 {len(selected)} 个文件夹及其子文件夹中没有可导入的正文 TXT。",
            )
            return

        folder_preview = "\n".join(f"  • {root.name or str(root)}" for root in selected[:8])
        if len(selected) > 8:
            folder_preview += f"\n  • ……另有 {len(selected) - 8} 个文件夹"
        nested_note = (
            f"\n\n为避免重复导入，已忽略 {nested_count} 个同时选中的下级文件夹。"
            if nested_count else ""
        )
        confirmed = messagebox.askyesno(
            "确认文件夹导入",
            f"确认导入 {len(selected)} 个文件夹吗？\n\n"
            f"扫描到 {total_files} 个 TXT/MP3 文件，将创建 {total_tasks} 个正文任务。\n\n"
            f"所选文件夹：\n{folder_preview}{nested_note}",
            icon=messagebox.WARNING,
            default=messagebox.NO,
        )
        if not confirmed:
            return

        created = 0
        for _root, matched in imports:
            for text_path, audio_path, dictionary_path in matched["pairs"]:
                job_id = self._create_queued_job(
                    text_path.stem,
                    str(text_path),
                    dictionary_path=str(dictionary_path) if dictionary_path else "",
                    imported_audio_path=str(audio_path) if audio_path else None,
                )
                created += int(bool(job_id))
        messagebox.showinfo(
            "文件夹导入完成",
            f"已从 {len(selected)} 个文件夹导入 {created} 个正文任务。\n"
            "同一小说文件夹内的 _01、_02… 会自动共享洗稿人名和设定。",
        )

    def _import_text_audio_pair(self):
        selected = filedialog.askopenfilenames(
            title="同时选中 1 份原文 TXT 和 1 份完整配音 MP3",
            filetypes=[("TXT and MP3", ("*.txt", "*.mp3")), ("All files", "*.*")],
        )
        if not selected:
            return
        text_files = [Path(path) for path in selected if Path(path).suffix.lower() == ".txt"]
        audio_files = [Path(path) for path in selected if Path(path).suffix.lower() == ".mp3"]
        unsupported = [Path(path).name for path in selected if Path(path).suffix.lower() not in {".txt", ".mp3"}]
        if len(text_files) != 1 or len(audio_files) != 1 or unsupported:
            details = []
            if len(text_files) != 1:
                details.append(f"TXT 已选 {len(text_files)} 份")
            if len(audio_files) != 1:
                details.append(f"MP3 已选 {len(audio_files)} 份")
            if unsupported:
                details.append("不支持的文件：" + "、".join(unsupported))
            messagebox.showwarning(
                "请选择一对文件",
                "请在同一个窗口中同时选中 1 份 TXT 和 1 份 MP3。\n"
                + "\n".join(details),
            )
            return
        text_path = text_files[0]
        audio_path = audio_files[0]
        try:
            pr.inspect_imported_audio(audio_path)
        except Exception as exc:
            messagebox.showerror("MP3 无效", str(exc))
            return
        title = Path(text_path).stem
        job_id = self._create_queued_job(title, str(text_path), imported_audio_path=str(audio_path))
        if job_id:
            messagebox.showinfo(
                "正文 + MP3 已加入",
                f"已导入：\n正文：{Path(text_path).name}\n音频：{Path(audio_path).name}\n\n"
                "该任务会跳过 TTS，直接继续画面、字幕、封面和视频流程。",
            )

    def _import_txt_files(self):
        files = filedialog.askopenfilenames(
            title="选择正文和读音词典 TXT（可混合多选）",
            filetypes=[("Text files", "*.txt"), ("All files", "*.*")],
        )
        if not files:
            return
        initial = _match_batch_texts_and_dictionaries(list(files))
        if not initial["dictionaries"]:
            for text_path in initial["texts"]:
                self._create_queued_job(text_path.stem, str(text_path))
            return

        valid_dictionaries: list[Path] = []
        invalid_dictionaries: list[tuple[Path, str]] = []
        for dictionary_path in initial["dictionaries"]:
            try:
                if self._inspect_pronunciation_dictionary_with_choices(dictionary_path) is not None:
                    valid_dictionaries.append(dictionary_path)
            except Exception as exc:
                invalid_dictionaries.append((dictionary_path, str(exc)))

        matched = _match_batch_texts_and_dictionaries([*initial["texts"], *valid_dictionaries])
        created_with_dictionary = 0
        for text_path, dictionary_path in matched["pairs"]:
            self._create_queued_job(
                text_path.stem,
                str(text_path),
                dictionary_path=str(dictionary_path),
            )
            created_with_dictionary += 1

        texts_without_dictionary = [
            *matched["unmatched_texts"],
            *(text_path for text_path, _candidates in matched["ambiguous_texts"]),
        ]
        for text_path in texts_without_dictionary:
            # An explicit empty value prevents a previously selected global
            # dictionary from being attached to the wrong batch item.
            self._create_queued_job(text_path.stem, str(text_path), dictionary_path="")

        report = [
            f"成功创建 {created_with_dictionary + len(texts_without_dictionary)} 个正文任务："
            f"{created_with_dictionary} 个已自动附加词典，"
            f"{len(texts_without_dictionary)} 个按普通方式导入。"
        ]
        if matched["unmatched_texts"]:
            names = "\n".join(f"  - {path.name}" for path in matched["unmatched_texts"])
            report.append(f"未找到同名词典，已按普通正文导入：\n{names}")
        if matched["ambiguous_texts"]:
            rows = []
            for text_path, candidates in matched["ambiguous_texts"]:
                rows.append(f"  - {text_path.name} → {', '.join(path.name for path in candidates)}")
            report.append("找到多个同名词典，无法安全选择；正文已按普通方式导入：\n" + "\n".join(rows))
        if matched["unused_dictionaries"]:
            names = "\n".join(f"  - {path.name}" for path in matched["unused_dictionaries"])
            report.append(f"没有对应正文、未使用的词典：\n{names}")
        if invalid_dictionaries:
            names = "\n".join(f"  - {path.name}: {error}" for path, error in invalid_dictionaries)
            report.append(f"格式无效的词典：\n{names}")
        report.append("推荐命名：作品名_正文.txt + 作品名_读音词典.txt")
        message = "\n\n".join(report)
        if created_with_dictionary + len(texts_without_dictionary) and not any(
            (matched["unmatched_texts"], matched["ambiguous_texts"], matched["unused_dictionaries"], invalid_dictionaries)
        ):
            messagebox.showinfo("批量配对完成", message)
        else:
            messagebox.showwarning("批量配对结果", message)

    def _choose_pronunciation_dictionary(self) -> str:
        path = filedialog.askopenfilename(
            title="选择日语读音词典",
            filetypes=[("Text files", "*.txt"), ("All files", "*.*")],
        )
        if not path:
            return ""
        try:
            resolved = self._inspect_pronunciation_dictionary_with_choices(path)
            if resolved is None:
                return ""
            info, _choices = resolved
        except Exception as exc:
            messagebox.showerror("读音词典无效", str(exc))
            return ""
        self.pronunciation_dictionary_var.set(str(info["path"]))
        selected_ids = self._selected_job_ids()
        if selected_ids:
            self._attach_pronunciation_dictionary_to_jobs(selected_ids, str(info["path"]))
        else:
            messagebox.showinfo("读音词典已选择", f"已识别 {info['entries']} 条读音。之后新建的任务会自动附加此词典。")
        return str(info["path"])

    def _choose_pronunciation_conflicts(self, conflicts: list[dict]) -> dict[str, str] | None:
        """Show all duplicate dictionary readings and return the user's choices."""
        win = tk.Toplevel(self.root)
        win.title("选择本任务使用的读音")
        win.geometry("620x520")
        win.minsize(500, 360)
        win.transient(self.root)
        win.grab_set()

        outer = ttk.Frame(win, padding=14)
        outer.pack(fill=tk.BOTH, expand=True)
        ttk.Label(
            outer,
            text="词典里以下词语有多个读音。请为本任务各选一个，不会修改原始 TXT。",
            wraplength=570,
        ).pack(anchor=tk.W, pady=(0, 10))

        body = ttk.Frame(outer)
        body.pack(fill=tk.BOTH, expand=True)
        canvas = tk.Canvas(body, highlightthickness=0)
        scrollbar = ttk.Scrollbar(body, orient=tk.VERTICAL, command=canvas.yview)
        rows = ttk.Frame(canvas)
        rows.bind("<Configure>", lambda _e: canvas.configure(scrollregion=canvas.bbox("all")))
        window_id = canvas.create_window((0, 0), window=rows, anchor="nw")
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(window_id, width=e.width))
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        variables: dict[str, tk.StringVar] = {}
        for item in conflicts:
            written = str(item["written"])
            readings = [str(value) for value in item["readings"]]
            box = ttk.LabelFrame(rows, text=f"{written}（词典第 {', '.join(map(str, item.get('lines', [])))} 行）", padding=8)
            box.pack(fill=tk.X, pady=(0, 8))
            variable = tk.StringVar(value=readings[0])
            variables[written] = variable
            for reading in readings:
                ttk.Radiobutton(box, text=reading, value=reading, variable=variable).pack(anchor=tk.W, pady=2)

        result: dict[str, str] | None = None

        def confirm():
            nonlocal result
            result = {written: variable.get() for written, variable in variables.items()}
            win.destroy()

        buttons = ttk.Frame(outer)
        buttons.pack(fill=tk.X, pady=(10, 0))
        ttk.Button(buttons, text="取消", command=win.destroy).pack(side=tk.RIGHT)
        ttk.Button(buttons, text="使用所选读音", style="Primary.TButton", command=confirm).pack(side=tk.RIGHT, padx=(0, 8))
        win.protocol("WM_DELETE_WINDOW", win.destroy)
        win.bind("<Escape>", lambda _event: win.destroy())
        win.bind("<Return>", lambda _event: confirm())
        win.wait_visibility()
        win.focus_force()
        self.root.wait_window(win)
        return result

    def _inspect_pronunciation_dictionary_with_choices(self, path: str | Path):
        cache_key = str(Path(path).expanduser().resolve())
        choices = dict(self._pronunciation_resolution_cache.get(cache_key, {}))
        try:
            info = pr.inspect_pronunciation_dictionary(path, conflict_choices=choices or None)
        except pr.PronunciationDictionaryConflictError as exc:
            selected = self._choose_pronunciation_conflicts(exc.conflicts)
            if selected is None:
                return None
            choices.update(selected)
            info = pr.inspect_pronunciation_dictionary(path, conflict_choices=choices)
        self._pronunciation_resolution_cache[cache_key] = choices
        return info, choices

    def _attach_pronunciation_dictionary_with_choices(self, job_id: str, path: str):
        resolved = self._inspect_pronunciation_dictionary_with_choices(path)
        if resolved is None:
            return None
        _info, choices = resolved
        return pr.attach_pronunciation_dictionary(job_id, path, conflict_choices=choices or None)

    def _clear_pronunciation_dictionary(self):
        self.pronunciation_dictionary_var.set("")

    def _attach_pronunciation_dictionary_to_selected(self):
        ids = self._selected_job_ids()
        if not ids:
            messagebox.showwarning("没有选择任务", "请先在任务队列中选择任务。")
            return
        path = self.pronunciation_dictionary_var.get().strip()
        if not path:
            self._choose_pronunciation_dictionary()
            return
        self._attach_pronunciation_dictionary_to_jobs(ids, path)

    def _attach_pronunciation_dictionary_to_jobs(self, ids: list[str], path: str):
        errors = []
        attached = 0
        reset_segments = 0
        for job_id in ids:
            try:
                result = self._attach_pronunciation_dictionary_with_choices(job_id, path)
                if result is None:
                    break
                attached += 1
                reset_segments += int(result.get("reset_segments", 0))
            except Exception as exc:
                errors.append(f"{job_id}: {exc}")
        self._refresh_jobs()
        if errors:
            messagebox.showerror("附加读音词典失败", "\n".join(errors))
        if attached:
            messagebox.showinfo(
                "读音词典已附加",
                f"已附加到 {attached} 个任务；已重置 {reset_segments} 个旧 TTS 段。启动任务后会按新读音重新配音。",
            )

    def _create_queued_job(
        self,
        title: str,
        input_text: str,
        dictionary_path: str | None = None,
        imported_audio_path: str | None = None,
    ) -> str:
        source = Path(str(input_text)).expanduser()
        if source.is_file():
            try:
                source_key = str(source.resolve())
            except OSError:
                source_key = str(source.absolute())
            duplicates = []
            for row in pr.list_jobs(limit=10000):
                status = pr.load_status(str(row.get("job_id") or ""), include_worker=True)
                previous = str(status.get("source_path") or status.get("input") or "").strip()
                if not previous:
                    continue
                try:
                    previous_key = str(Path(previous).expanduser().resolve())
                except OSError:
                    previous_key = previous
                if previous_key == source_key:
                    duplicates.append(status)
            if duplicates:
                running = [item for item in duplicates if item.get("worker_alive")]
                if running:
                    messagebox.showwarning(
                        "任务正在制作",
                        f"“{source.name}”已经有正在制作的任务，不能覆盖。\n"
                        "请等待它结束，或先在任务队列中停止该任务后再重新导入。",
                    )
                    return ""
                replace = messagebox.askyesno(
                    "发现重复导入",
                    f"“{source.name}”已经导入过 {len(duplicates)} 次。\n\n"
                    "选择“是”：删除旧任务并重新制作。\n"
                    "选择“否”：跳过这次导入。",
                    icon=messagebox.WARNING,
                )
                if not replace:
                    return ""
                for item in duplicates:
                    try:
                        pr.delete_job(str(item.get("job_id") or ""))
                    except Exception as exc:
                        messagebox.showerror("覆盖失败", f"无法删除旧任务：\n{exc}")
                        return ""
        output_basename = pr.safe_job_name(title, fallback="article")
        job_id = pr.new_named_job_id(output_basename)
        job_dir = pr.job_dir_for(job_id)
        pr.write_status(
            job_dir,
            job_id=job_id,
            title=title,
            input=input_text,
            output_basename=output_basename,
            source_path=input_text if Path(str(input_text)).is_file() else "",
            source_directory=str(Path(input_text).parent) if Path(str(input_text)).is_file() else "",
            stage="queued",
            progress=0.0,
        )
        pr.append_log(job_dir, f"queued: {title}")
        pr.register_imported_series_job(job_id, input_text)
        selected_dictionary = (
            self.pronunciation_dictionary_var.get().strip()
            if dictionary_path is None
            else str(dictionary_path).strip()
        )
        if imported_audio_path:
            try:
                pr.attach_imported_audio(job_id, imported_audio_path)
            except Exception as exc:
                try:
                    pr.delete_job(job_id)
                except Exception:
                    pass
                messagebox.showerror("导入 MP3 失败", f"任务未创建：\n{exc}")
                self._refresh_jobs()
                return ""
        if selected_dictionary:
            try:
                self._attach_pronunciation_dictionary_with_choices(job_id, selected_dictionary)
            except Exception as exc:
                messagebox.showerror("附加读音词典失败", f"任务已加入队列，但词典附加失败：\n{exc}")
        self._refresh_jobs()
        return job_id

    def _selected_job_ids(self) -> list[str]:
        # Tree row IDs always retain the full job ID even though the first
        # visible column is abbreviated for readability.
        return [str(iid) for iid in self.job_tree.selection()]

    def _show_job_context_menu(self, event):
        """Select the clicked row (without losing a multi-selection) and show actions."""
        row_id = self.job_tree.identify_row(event.y)
        if not row_id:
            return "break"
        if row_id not in self.job_tree.selection():
            self.job_tree.selection_set(row_id)
        self.job_tree.focus(row_id)
        self.job_context_menu.tk_popup(event.x_root, event.y_root)
        return "break"

    def _require_single_selected_job(self, action: str) -> str | None:
        ids = self._selected_job_ids()
        if not ids:
            messagebox.showwarning("没有选择任务", f"请先选择一个任务再{action}。")
            return None
        if len(ids) > 1:
            messagebox.showwarning("只能选择一个任务", f"{action}一次只能处理一个任务，请只选择一个任务。")
            return None
        return ids[0]

    def _start_selected(self):
        ids = self._selected_job_ids()
        for job_id in ids:
            self._start_job(job_id, selected_job_ids=ids)

    def _continue_selected(self):
        """Resume selected jobs from their saved artifacts and settings snapshot."""
        for job_id in self._selected_job_ids():
            status = pr.load_status(job_id)
            if status.get("worker_alive"):
                messagebox.showwarning("任务正在运行", f"{job_id} 正在运行，无需继续启动。")
                continue
            input_text = str(status.get("input") or "")
            if not input_text:
                messagebox.showwarning("缺少输入", f"{job_id} 没有 input")
                continue
            try:
                pr.start_worker(input_text, job_id=job_id, resume=True)
            except Exception as exc:
                messagebox.showerror("继续任务失败", str(exc))
        self._refresh_jobs()

    def _ask_batch_schedule_datetimes(self, title: str, ids: list[str], mode: str) -> tuple[list[datetime], str, str, list[str]] | None:
        now = datetime.now()
        initial = now + timedelta(days=1)
        if mode == "script":
            configured_date = str(config.get("script_schedule_first_date", "") or "")
            configured_time = str(config.get("script_schedule_time", "18:00") or "18:00")
            year, month, day = _split_numeric_date(configured_date, initial)
        else:
            configured_date = str(config.get("youtube_schedule_date", "") or "")
            configured_time = str(config.get("youtube_schedule_time", "18:00") or "18:00")
            year, month, day = _future_numeric_date_parts(configured_date)
        hour, minute = _split_numeric_time(configured_time)
        try:
            base_time = _datetime_from_numeric_parts(year, month, day, hour, minute)
            if base_time <= now + timedelta(minutes=5):
                base_time = initial.replace(hour=int(hour), minute=int(minute), second=0, microsecond=0)
        except ValueError:
            base_time = initial.replace(hour=18, minute=0, second=0, microsecond=0)

        configuration_names = []
        for configuration_name in config.list_profiles():
            try:
                config.profile_settings(configuration_name)
            except Exception:
                continue
            configuration_names.append(configuration_name)
        if not configuration_names:
            messagebox.showerror("没有可用配置", "请先保存至少一个配置方案，再创建定时任务。")
            return None
        active_configuration = str(config.get("active_profile", "") or "").strip()
        selected_configuration = active_configuration if active_configuration in configuration_names else configuration_names[0]
        channel_profiles: list[dict] = []

        def load_channels(configuration_name: str) -> list[dict]:
            try:
                _cleaned, settings = config.profile_settings(configuration_name)
            except Exception:
                return []
            return [
                item for item in _parse_upload_profiles(str(settings.get("browser_profiles", "[]") or "[]"))
                if bool(item.get("enabled", True))
            ]

        channel_profiles = load_channels(selected_configuration)
        if not channel_profiles:
            messagebox.showerror("没有可用频道", "所选配置中没有启用的频道方案，请先在“编辑频道上传方案”中启用频道。")
            return None
        profile_names = [str(item.get("name") or "").strip() for item in channel_profiles]
        _cleaned, selected_settings = config.profile_settings(selected_configuration)
        active_name = str(selected_settings.get("browser_active_profile", "") or "").strip()
        selected_profile_name = active_name if active_name in profile_names else profile_names[0]
        script_items = {
            str(item.get("job_id") or ""): item
            for item in publish_scheduler.load_items(selected_profile_name)
        } if mode == "script" else {}

        win = tk.Toplevel(self.root)
        win.title(title)
        win.geometry("940x520")
        win.minsize(760, 360)
        try:
            win.transient(self.root)
            win.grab_set()
        except Exception:
            pass
        outer = ttk.Frame(win, padding=12)
        outer.pack(fill=tk.BOTH, expand=True)
        ttk.Label(
            outer,
            text="左侧是选中的任务，右侧可分别修改年、月、日、时、分。所有输入框只接受数字。",
            foreground="#555",
        ).pack(anchor=tk.W, pady=(0, 8))

        channel_bar = ttk.Frame(outer)
        channel_bar.pack(fill=tk.X, pady=(0, 8))
        ttk.Label(channel_bar, text="使用配置：").pack(side=tk.LEFT)
        configuration_var = tk.StringVar(value=selected_configuration)
        configuration_picker = ttk.Combobox(
            channel_bar, textvariable=configuration_var, values=configuration_names,
            state="readonly", width=20,
        )
        configuration_picker.pack(side=tk.LEFT, padx=(4, 14))
        ttk.Label(channel_bar, text="频道方案：").pack(side=tk.LEFT)
        channel_var = tk.StringVar(value=selected_profile_name)
        channel_picker = ttk.Combobox(
            channel_bar, textvariable=channel_var, values=profile_names,
            state="readonly", width=28,
        )
        channel_picker.pack(side=tk.LEFT, padx=(4, 14))
        channel_detail_var = tk.StringVar()
        ttk.Label(channel_bar, textvariable=channel_detail_var, foreground="#555").pack(side=tk.LEFT)

        def refresh_channel_detail(*_args):
            chosen = next(
                (item for item in channel_profiles if str(item.get("name") or "") == channel_var.get()),
                None,
            )
            if chosen is None:
                channel_detail_var.set("未选择频道")
                return
            channel_detail_var.set(f"频道账号：{str(chosen.get('chrome_profile') or 'Default')}")

        def refresh_configuration_channels(*_args):
            nonlocal channel_profiles
            channel_profiles = load_channels(configuration_var.get().strip())
            names = [str(item.get("name") or "").strip() for item in channel_profiles]
            channel_picker.configure(values=names)
            if not names:
                channel_var.set("")
                channel_detail_var.set("此配置没有启用的频道方案")
                return
            try:
                _cleaned, settings = config.profile_settings(configuration_var.get().strip())
            except Exception:
                settings = {}
            preferred = str(settings.get("browser_active_profile", "") or "").strip()
            channel_var.set(preferred if preferred in names else names[0])

        channel_var.trace_add("write", refresh_channel_detail)
        configuration_var.trace_add("write", refresh_configuration_channels)
        refresh_channel_detail()

        header = ttk.Frame(outer)
        header.pack(fill=tk.X, padx=(2, 18))
        ttk.Label(header, text="文件名", width=46, anchor=tk.W, font=(UI_FONT, UI_SMALL_FONT_SIZE, "bold")).pack(side=tk.LEFT)
        ttk.Label(header, text="定时时间", font=(UI_FONT, UI_SMALL_FONT_SIZE, "bold")).pack(side=tk.LEFT)

        list_wrap = ttk.Frame(outer)
        list_wrap.pack(fill=tk.BOTH, expand=True)
        canvas = tk.Canvas(list_wrap, highlightthickness=0)
        scrollbar = ttk.Scrollbar(list_wrap, orient=tk.VERTICAL, command=canvas.yview)
        rows_frame = ttk.Frame(canvas)
        rows_window = canvas.create_window((0, 0), window=rows_frame, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        rows_frame.bind("<Configure>", lambda _event: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda event: canvas.itemconfigure(rows_window, width=event.width))

        row_vars: list[dict[str, object]] = []
        drag_state: dict[str, object] = {}

        def numeric_entry(parent, variable: tk.StringVar, width: int, max_digits: int):
            validate = (win.register(lambda value, limit=max_digits: (not value) or (value.isdigit() and len(value) <= limit)), "%P")
            entry = ttk.Entry(parent, textvariable=variable, width=width, validate="key", validatecommand=validate)
            entry.pack(side=tk.LEFT, padx=(4, 1))
            return entry

        for index, job_id in enumerate(ids):
            status = pr.load_status(job_id)
            source = str(status.get("source_path") or status.get("input") or "").strip()
            display_name = Path(source).name if source and Path(source).suffix else str(status.get("title") or job_id)
            scheduled = base_time + timedelta(days=index)
            if mode == "script" and job_id in script_items:
                try:
                    scheduled = datetime.strptime(str(script_items[job_id].get("scheduled_at") or ""), "%Y-%m-%dT%H:%M")
                except ValueError:
                    pass
            else:
                saved_at = str(status.get("publish_scheduled_at") or "")
                if saved_at:
                    try:
                        scheduled = datetime.strptime(saved_at, "%Y-%m-%dT%H:%M")
                    except ValueError:
                        pass
            values = {
                "year": tk.StringVar(value=str(scheduled.year)),
                "month": tk.StringVar(value=str(scheduled.month)),
                "day": tk.StringVar(value=str(scheduled.day)),
                "hour": tk.StringVar(value=str(scheduled.hour)),
                "minute": tk.StringVar(value=str(scheduled.minute)),
            }
            line = ttk.Frame(rows_frame)
            line.pack(fill=tk.X, pady=3)
            row = {"job_id": str(job_id), "name": display_name, "values": values, "line": line}
            row_vars.append(row)
            ttk.Label(line, text=display_name, width=46, anchor=tk.W).pack(side=tk.LEFT)
            numeric_entry(line, values["year"], 6, 4)
            ttk.Label(line, text="年").pack(side=tk.LEFT)
            numeric_entry(line, values["month"], 4, 2)
            ttk.Label(line, text="月").pack(side=tk.LEFT)
            numeric_entry(line, values["day"], 4, 2)
            ttk.Label(line, text="日").pack(side=tk.LEFT)
            numeric_entry(line, values["hour"], 4, 2)
            ttk.Label(line, text="时").pack(side=tk.LEFT)
            numeric_entry(line, values["minute"], 4, 2)
            ttk.Label(line, text="分").pack(side=tk.LEFT)

            # Rows can be reordered without changing the selected task set.
            def begin_drag(event, current=row):
                drag_state["row"] = current
                drag_state["y"] = event.y_root

            def finish_drag(event, current=row):
                if drag_state.get("row") is not current:
                    return
                try:
                    old_index = row_vars.index(current)
                except ValueError:
                    return
                target_y = event.y_root
                new_index = old_index
                for index, candidate in enumerate(row_vars):
                    candidate_line = candidate["line"]
                    if candidate_line is current["line"]:
                        continue
                    midpoint = candidate_line.winfo_rooty() + max(1, candidate_line.winfo_height()) // 2
                    if target_y < midpoint:
                        new_index = index
                        break
                    new_index = index
                if new_index != old_index:
                    row_vars.pop(old_index)
                    row_vars.insert(new_index, current)
                    for ordered in row_vars:
                        ordered["line"].pack_forget()
                        ordered["line"].pack(fill=tk.X, pady=3)
                drag_state.clear()

            def delete_row(current=row):
                if current not in row_vars:
                    return
                if not messagebox.askyesno("移除定时任务", f"确定从本次定时列表中移除“{current['name']}”吗？", parent=win):
                    return
                row_vars.remove(current)
                current["line"].destroy()

            row_menu = tk.Menu(win, tearoff=False)
            row_menu.add_command(label="删除这个定时任务", command=lambda current=row: delete_row(current))

            def show_row_menu(event, menu=row_menu):
                menu.tk_popup(event.x_root, event.y_root)
                return "break"

            # Do not bind the drag handlers to Entry widgets.  A normal click
            # while editing a date/time field must not be interpreted as a row
            # reorder, otherwise releasing the mouse can move the row and make
            # the value being edited appear to jump between fields.
            for widget in (line, *line.winfo_children()):
                widget.bind("<Button-3>", show_row_menu, add="+")
                widget.bind("<Button-2>", show_row_menu, add="+")
                widget.bind("<Control-Button-1>", show_row_menu, add="+")
                if not isinstance(widget, (tk.Entry, ttk.Entry)):
                    widget.bind("<ButtonPress-1>", begin_drag, add="+")
                    widget.bind("<ButtonRelease-1>", finish_drag, add="+")

        actions = ttk.Frame(outer)
        actions.pack(fill=tk.X, pady=(10, 0))

        def sync_part(key: str):
            if not row_vars:
                return
            value = row_vars[0]["values"][key].get()
            for row in row_vars[1:]:
                row["values"][key].set(value)

        def arrange_24_hours():
            if not row_vars:
                return
            first = row_vars[0]["values"]
            try:
                first_time = _datetime_from_numeric_parts(
                    first["year"].get(), first["month"].get(), first["day"].get(),
                    first["hour"].get(), first["minute"].get(),
                )
            except ValueError as exc:
                messagebox.showerror("第一个视频时间不正确", str(exc), parent=win)
                return
            for index, row in enumerate(row_vars):
                values = row["values"]
                scheduled = first_time + timedelta(hours=24 * index)
                values["year"].set(str(scheduled.year))
                values["month"].set(str(scheduled.month))
                values["day"].set(str(scheduled.day))
                values["hour"].set(str(scheduled.hour))
                values["minute"].set(str(scheduled.minute))

        ttk.Button(actions, text="同步所有年份", command=lambda: sync_part("year")).pack(side=tk.LEFT)
        ttk.Button(actions, text="同步所有月份", command=lambda: sync_part("month")).pack(side=tk.LEFT, padx=(6, 0))
        ttk.Button(actions, text="从第一个视频开始每24小时排列", command=arrange_24_hours).pack(side=tk.LEFT, padx=(6, 0))

        result: dict[str, object] = {}

        def accept():
            chosen_configuration = configuration_var.get().strip()
            if chosen_configuration not in configuration_names:
                messagebox.showerror("请选择配置", "请选择要使用的配置方案。", parent=win)
                return
            chosen_profile = channel_var.get().strip()
            channel_names = [str(item.get("name") or "").strip() for item in channel_profiles]
            if chosen_profile not in channel_names:
                messagebox.showerror("请选择频道", "请选择要使用的频道方案。", parent=win)
                return
            scheduled_rows = []
            for index, row in enumerate(row_vars):
                values = row["values"]
                try:
                    scheduled = _datetime_from_numeric_parts(
                        values["year"].get(), values["month"].get(), values["day"].get(),
                        values["hour"].get(), values["minute"].get(),
                    )
                except ValueError as exc:
                    messagebox.showerror("定时时间不正确", f"第 {index + 1} 行：{exc}", parent=win)
                    return
                if scheduled <= datetime.now() + timedelta(minutes=5):
                    messagebox.showerror("定时时间太近", f"第 {index + 1} 行必须至少晚于当前时间 5 分钟。", parent=win)
                    return
                scheduled_rows.append(scheduled)
            result["values"] = scheduled_rows
            result["job_ids"] = [str(row["job_id"]) for row in row_vars]
            result["configuration_name"] = chosen_configuration
            result["profile_name"] = chosen_profile
            win.destroy()

        ttk.Button(actions, text="确定", style="Primary.TButton", command=accept).pack(side=tk.RIGHT)
        ttk.Button(actions, text="取消", command=win.destroy).pack(side=tk.RIGHT, padx=(0, 6))
        win.bind("<Escape>", lambda _event: win.destroy())
        self.root.wait_window(win)
        values = result.get("values")
        configuration_name = result.get("configuration_name")
        profile_name = result.get("profile_name")
        if not isinstance(values, list) or not isinstance(configuration_name, str) or not isinstance(profile_name, str):
            return None
        ordered_ids = result.get("job_ids")
        if not isinstance(ordered_ids, list):
            return None
        return values, configuration_name, profile_name, [str(job_id) for job_id in ordered_ids]

    def _switch_active_channel_schedule_mode(self, mode: str, scheduled: datetime,
                                             profile_name_override: str = "") -> str | None:
        if mode not in {"youtube", "script"}:
            raise ValueError(f"unsupported schedule mode: {mode}")
        profiles = _parse_upload_profiles(str(config.get("browser_profiles", "[]") or "[]"))
        active_name = str(config.get("browser_active_profile", "") or "").strip()
        selected_name = str(profile_name_override or active_name).strip()
        profile = next((item for item in profiles if str(item.get("name") or "") == selected_name), None)
        if profile is None:
            messagebox.showerror("没有频道方案", "请先在“编辑频道上传方案”中建立并选择频道。")
            return None
        current_mode = str(profile.get("publish_mode") or "immediate")
        target_label = "油管内定时" if mode == "youtube" else "脚本内定时"
        if current_mode != mode:
            labels = {"immediate": "直接发布", "script": "脚本内定时", "youtube": "油管内定时"}
            if not messagebox.askyesno(
                "切换发布方式",
                f"当前频道正在使用“{labels.get(current_mode, current_mode)}”。\n\n"
                f"确定切换至“{target_label}”并锁定其他冲突设置吗？",
            ):
                return None
        profile["publish_mode"] = mode
        if mode == "youtube":
            profile["youtube_schedule_date"] = scheduled.strftime("%Y-%m-%d")
            profile["youtube_schedule_time"] = scheduled.strftime("%H:%M")
            profile["youtube_schedule_timezone"] = str(profile.get("youtube_schedule_timezone") or "Asia/Tokyo")
        else:
            profile["script_schedule_first_date"] = scheduled.strftime("%Y-%m-%d")
            profile["script_schedule_time"] = scheduled.strftime("%H:%M")
            profile["script_schedule_interval_hours"] = 24
            profile["script_schedule_timezone"] = str(profile.get("script_schedule_timezone") or "Asia/Tokyo")
            profile["script_manual_queue"] = True
        encoded = json.dumps(profiles, ensure_ascii=False)
        config.set("browser_profiles", encoded)
        config.set("browser_active_profile", str(profile.get("name") or selected_name))
        config.set("browser_upload_all_profiles", False)
        config.set("youtube_publish_mode", mode)
        config.set("youtube_schedule_enabled", mode == "youtube")
        if mode == "youtube":
            config.set("youtube_schedule_date", scheduled.strftime("%Y-%m-%d"))
            config.set("youtube_schedule_time", scheduled.strftime("%H:%M"))
            config.set("youtube_schedule_timezone", str(profile.get("youtube_schedule_timezone") or "Asia/Tokyo"))
        else:
            config.set("script_schedule_first_date", scheduled.strftime("%Y-%m-%d"))
            config.set("script_schedule_time", scheduled.strftime("%H:%M"))
            config.set("script_schedule_interval_hours", 24)
            config.set("script_schedule_timezone", str(profile.get("script_schedule_timezone") or "Asia/Tokyo"))
        config.set("upload_enabled", True)
        config.save()
        for key, value in (
            ("browser_profiles", encoded),
            ("browser_active_profile", str(profile.get("name") or selected_name)),
            ("browser_upload_all_profiles", "关闭"),
            ("youtube_publish_mode", mode),
            ("youtube_schedule_enabled", "开启" if mode == "youtube" else "关闭"),
            ("upload_enabled", "开启"),
        ):
            if key in getattr(self, "vars", {}):
                self.vars[key].set(value)
        mode_values = (
            (
                ("youtube_schedule_date", scheduled.strftime("%Y-%m-%d")),
                ("youtube_schedule_time", scheduled.strftime("%H:%M")),
            )
            if mode == "youtube"
            else (
                ("script_schedule_first_date", scheduled.strftime("%Y-%m-%d")),
                ("script_schedule_time", scheduled.strftime("%H:%M")),
                ("script_schedule_interval_hours", "24"),
            )
        )
        for key, value in mode_values:
            if key in getattr(self, "vars", {}):
                self.vars[key].set(value)
        return str(profile.get("name") or selected_name)

    def _confirm_reschedule_jobs(self, ids: list[str]) -> bool:
        queue_by_job = {
            str(item.get("job_id") or ""): item
            for item in publish_scheduler.load_items()
            if str(item.get("state") or "pending") not in {"published"}
        }
        reminders = []
        for job_id in ids:
            status = pr.load_status(job_id)
            mode = str(status.get("publish_schedule_mode") or "")
            scheduled_at = str(status.get("publish_scheduled_at") or "")
            youtube_url = str(status.get("youtube_url") or "").strip()
            if job_id in queue_by_job:
                item = queue_by_job[job_id]
                reminders.append(
                    f"{job_id}：脚本内定时 {str(item.get('scheduled_at') or '').replace('T', ' ')}"
                )
            elif mode and scheduled_at:
                reminders.append(f"{job_id}：{'油管内定时' if mode == 'youtube' else '脚本内定时'} {scheduled_at.replace('T', ' ')}")
            elif youtube_url:
                reminders.append(f"{job_id}：已经上传 {youtube_url}")
            upload_result = pr.job_dir_for(job_id) / "upload_result.json"
            if upload_result.exists():
                try:
                    payload = json.loads(upload_result.read_text(encoding="utf-8"))
                except Exception:
                    payload = {}
                if isinstance(payload, dict) and str(payload.get("publish_mode") or "") == "scheduled":
                    result_time = str(payload.get("scheduled_at") or scheduled_at).replace("T", " ")
                    if not any(line.startswith(f"{job_id}：") for line in reminders):
                        reminders.append(f"{job_id}：油管内定时 {result_time}")
        if not reminders:
            return True
        return messagebox.askyesno(
            "检测到定时发布记录",
            "以下任务已经上传或设置过定时：\n\n"
            + "\n".join(reminders)
            + "\n\n请注意是否会造成重复上传。\n\n确定重新发布定时任务吗？",
            icon=messagebox.WARNING,
        )

    def _schedule_selected_jobs_on_youtube(self):
        ids = self._selected_job_ids()
        if not ids:
            messagebox.showwarning("没有选择任务", "请先选择一个或多个已经完成的视频。")
            return
        ids.sort(key=lambda job_id: self.job_tree.index(job_id) if self.job_tree.exists(job_id) else 10**9)
        invalid = []
        for job_id in ids:
            status = pr.load_status(job_id)
            video = Path(str(status.get("video") or pr.video_output_path(pr.job_dir_for(job_id))))
            if status.get("worker_alive") or not video.exists() or video.stat().st_size < 100:
                invalid.append(job_id)
        if invalid:
            messagebox.showwarning("视频尚未完成", "以下任务还没有可上传的成片：\n" + "\n".join(invalid))
            return
        if not self._confirm_reschedule_jobs(ids):
            return
        schedule_choice = self._ask_batch_schedule_datetimes("油管内定时任务", ids, "youtube")
        if not schedule_choice:
            return
        scheduled_rows, selected_configuration_name, selected_profile_name, ids = schedule_choice
        try:
            _cleaned, selected_settings = config.profile_settings(selected_configuration_name)
        except Exception as exc:
            messagebox.showerror("读取配置失败", str(exc))
            return
        profiles = _parse_upload_profiles(str(selected_settings.get("browser_profiles", "[]") or "[]"))
        active_profile = next(
            (item for item in profiles if str(item.get("name") or "") == selected_profile_name),
            None,
        )
        if active_profile is None:
            messagebox.showerror("没有频道方案", "请先在“编辑频道上传方案”中建立并选择频道。")
            return
        profile_name = str(active_profile.get("name") or selected_profile_name)
        channel_account = str(active_profile.get("chrome_profile") or "Default")
        timezone_text = str(active_profile.get("youtube_schedule_timezone") or "Asia/Tokyo")
        preview = "\n".join(
            f"{index + 1}. {job_id} → {scheduled_rows[index].strftime('%Y-%m-%d %H:%M')}"
            for index, job_id in enumerate(ids)
        )
        if not messagebox.askyesno(
            "确认油管内定时发布",
            f"使用配置：{selected_configuration_name}\n频道方案：{profile_name}\n"
            f"频道账号：{channel_account}\n时区：{timezone_text}\n\n"
            f"{preview}\n\n确认使用这个配置和频道发布定时任务吗？",
        ):
            return
        try:
            pr.apply_profile_to_jobs(ids, selected_configuration_name)
            config.load_profile(selected_configuration_name)
            config.save()
            self._refresh_config_form()
            if hasattr(self, "profile_var"):
                self.profile_var.set(selected_configuration_name)
        except Exception as exc:
            messagebox.showerror("套用配置失败", str(exc))
            return
        profile_name = self._switch_active_channel_schedule_mode(
            "youtube", scheduled_rows[0], selected_profile_name
        )
        if not profile_name:
            return
        publish_scheduler.remove_jobs(ids)
        for job_id, target in zip(ids, scheduled_rows):
            pr.write_status(
                pr.job_dir_for(job_id),
                publish_schedule_mode="youtube",
                publish_scheduled_at=target.strftime("%Y-%m-%dT%H:%M"),
                publish_schedule_profile=profile_name,
                publish_config_profile=selected_configuration_name,
                publish_schedule_state="uploading",
            )

        def worker():
            errors = []
            for job_id, target in zip(ids, scheduled_rows):
                try:
                    url = pr.upload_completed_job(
                        job_id,
                        on_log=lambda message, jid=job_id: pr.append_log(pr.job_dir_for(jid), message),
                        profile_name_override=profile_name,
                        schedule_enabled_override=True,
                        scheduled_at_override=target.strftime("%Y-%m-%dT%H:%M"),
                        schedule_timezone_override=timezone_text,
                    )
                    pr.write_status(
                        pr.job_dir_for(job_id),
                        publish_schedule_state="scheduled",
                        publish_schedule_url=url,
                    )
                except Exception as exc:
                    pr.write_status(
                        pr.job_dir_for(job_id),
                        publish_schedule_state="failed",
                        publish_schedule_error=str(exc),
                    )
                    errors.append(f"{job_id}: {exc}")
            self.root.after(0, lambda: self._finish_manual_upload(ids, errors, scheduled=True))

        threading.Thread(target=worker, daemon=True).start()

    def _schedule_selected_jobs_in_script(self):
        ids = self._selected_job_ids()
        if not ids:
            messagebox.showwarning("没有选择任务", "请先选择一个或多个任务。")
            return
        ids.sort(key=lambda job_id: self.job_tree.index(job_id) if self.job_tree.exists(job_id) else 10**9)
        if not self._confirm_reschedule_jobs(ids):
            return
        schedule_choice = self._ask_batch_schedule_datetimes("脚本内定时任务", ids, "script")
        if not schedule_choice:
            return
        scheduled_rows, selected_configuration_name, selected_profile_name, ids = schedule_choice
        try:
            _cleaned, selected_settings = config.profile_settings(selected_configuration_name)
        except Exception as exc:
            messagebox.showerror("读取配置失败", str(exc))
            return
        profiles = _parse_upload_profiles(str(selected_settings.get("browser_profiles", "[]") or "[]"))
        active_profile = next(
            (item for item in profiles if str(item.get("name") or "") == selected_profile_name),
            None,
        )
        if active_profile is None:
            messagebox.showerror("没有频道方案", "请先在“编辑频道上传方案”中建立并选择频道。")
            return
        profile_name = str(active_profile.get("name") or selected_profile_name)
        channel_account = str(active_profile.get("chrome_profile") or "Default")
        timezone_text = str(active_profile.get("script_schedule_timezone") or "Asia/Tokyo")
        preview = "\n".join(
            f"{index + 1}. {job_id} → {scheduled_rows[index].strftime('%Y-%m-%d %H:%M')}"
            for index, job_id in enumerate(ids)
        )
        if not messagebox.askyesno(
            "确认脚本内定时",
            f"使用配置：{selected_configuration_name}\n频道方案：{profile_name}\n"
            f"频道账号：{channel_account}\n时区：{timezone_text}\n\n"
            f"{preview}\n\n到达各自时间后，脚本才会上传对应视频。"
            "确认使用这个配置和频道创建定时任务吗？",
        ):
            return
        try:
            pr.apply_profile_to_jobs(ids, selected_configuration_name)
            config.load_profile(selected_configuration_name)
            config.save()
            self._refresh_config_form()
            if hasattr(self, "profile_var"):
                self.profile_var.set(selected_configuration_name)
        except Exception as exc:
            messagebox.showerror("套用配置失败", str(exc))
            return
        profile_name = self._switch_active_channel_schedule_mode(
            "script", scheduled_rows[0], selected_profile_name
        )
        if not profile_name:
            return
        statuses = {job_id: pr.load_status(job_id) for job_id in ids}
        publish_scheduler.set_manual_schedule(
            profile_name,
            list(zip(ids, scheduled_rows)),
            statuses,
        )
        for job_id, target in zip(ids, scheduled_rows):
            pr.write_status(
                pr.job_dir_for(job_id),
                publish_schedule_mode="script",
                publish_scheduled_at=target.strftime("%Y-%m-%dT%H:%M"),
                publish_schedule_profile=profile_name,
                publish_config_profile=selected_configuration_name,
                publish_schedule_state="pending",
                publish_schedule_error="",
            )
        self._refresh_jobs()
        messagebox.showinfo(
            "脚本内定时已保存",
            f"已为 {len(ids)} 个任务保存独立发布时间。软件需要保持运行，Mac 不能进入深度睡眠。",
        )

    def _upload_selected_completed_jobs(self, schedule_test: bool = False):
        ids = self._selected_job_ids()
        if not ids:
            messagebox.showwarning("没有选择任务", "请先在任务队列中选择已完成的视频。")
            return
        invalid = [job_id for job_id in ids if pr.load_status(job_id).get("worker_alive")]
        if invalid:
            messagebox.showwarning("任务仍在制作", "请等待选中的任务全部制作完成后再上传。")
            return
        try:
            self._apply_config_form(save_profile=False)
        except Exception as exc:
            messagebox.showerror("上传失败", f"无法保存上传设置：\n{exc}")
            return

        publish_mode = str(config.get("youtube_publish_mode", "immediate") or "immediate")
        if publish_mode == "script" and not schedule_test:
            profiles = _parse_upload_profiles(str(config.get("browser_profiles", "[]") or "[]"))
            active_name = str(config.get("browser_active_profile", "") or "").strip()
            profile = next((item for item in profiles if str(item.get("name") or "") == active_name), None)
            if profile is None:
                messagebox.showerror("脚本内定时", "找不到当前频道上传方案，请先保存频道方案。")
                return
            try:
                first_at = datetime.strptime(
                    f"{profile.get('script_schedule_first_date', '')} {profile.get('script_schedule_time', '')}",
                    "%Y-%m-%d %H:%M",
                )
                interval = max(1, int(profile.get("script_schedule_interval_hours") or 24))
            except (TypeError, ValueError):
                messagebox.showerror("脚本内定时", "请先在“脚本内定时”页填写正确的首次日期、时间和发布间隔。")
                return
            statuses = [(row["job_id"], pr.load_status(row["job_id"])) for row in pr.list_jobs(limit=10000)]
            publish_scheduler.ensure_jobs(active_name, statuses, first_at, interval)
            messagebox.showinfo("已交给脚本内定时", "选中的视频不会立即上传，将按照“脚本内定时”的待发布队列执行。")
            return

        schedule_enabled = publish_mode == "youtube"
        if schedule_test and not schedule_enabled:
            messagebox.showwarning("尚未开启定时发布", "请先在“编辑频道上传方案”的“油管内定时”页签中选择开启。")
            return
        if schedule_enabled:
            if len(ids) != 1:
                messagebox.showwarning("测试阶段仅支持一个视频", "请只选择一个已完成视频进行定时发布测试。")
                return
            date_text = str(config.get("youtube_schedule_date", "") or "").strip()
            time_text = str(config.get("youtube_schedule_time", "") or "").strip()
            try:
                scheduled_local = datetime.strptime(f"{date_text} {time_text}", "%Y-%m-%d %H:%M")
            except ValueError:
                messagebox.showerror("定时时间格式不正确", "发布日期请填写 YYYY-MM-DD，发布时间请填写 HH:MM。")
                return
            if scheduled_local <= datetime.now() + timedelta(minutes=5):
                messagebox.showerror("定时时间太近", "测试时间必须至少晚于当前时间 5 分钟。")
                return
            timezone_text = str(config.get("youtube_schedule_timezone", "Asia/Tokyo") or "Asia/Tokyo")
            confirm_text = (
                f"将上传选中的视频并预约公开：\n\n"
                f"{date_text} {time_text}\n时区：{timezone_text}\n\n"
                "不会重新制作视频。继续吗？"
            )
            confirm_title = "测试定时发布"
        else:
            confirm_text = f"将单独上传选中的 {len(ids)} 个已完成视频，不会重新制作视频。继续吗？"
            confirm_title = "执行选中任务"
        if not messagebox.askyesno(confirm_title, confirm_text):
            return

        def worker():
            errors = []
            for job_id in ids:
                try:
                    pr.upload_completed_job(job_id, on_log=lambda message, jid=job_id: pr.append_log(pr.job_dir_for(jid), message))
                except Exception as exc:
                    errors.append(f"{job_id}: {exc}")
            self.root.after(0, lambda: self._finish_manual_upload(ids, errors, scheduled=schedule_enabled))

        threading.Thread(target=worker, daemon=True).start()

    def _regenerate_selected_covers(self):
        ids = self._selected_job_ids()
        if not ids:
            messagebox.showwarning("没有选择任务", "请先在任务队列中选择要重新生成封面的任务。")
            return
        running = [job_id for job_id in ids if pr.load_status(job_id).get("worker_alive")]
        if running:
            messagebox.showwarning("任务正在运行", "请先停止选中的运行中任务，再重新生成封面。")
            return
        if not messagebox.askyesno(
            "重新生成封面",
            f"将使用当前封面设置，重新生成选中的 {len(ids)} 个任务封面。\n\n"
            "原封面会被替换；配音、剧情图、字幕和成片不会重新生成。继续吗？",
        ):
            return
        try:
            self._apply_config_form(save_profile=False)
        except Exception as exc:
            messagebox.showerror("重新生成封面失败", f"无法保存当前封面设置：\n{exc}")
            return

        def worker():
            errors = []
            completed = []
            for job_id in ids:
                try:
                    pr.regenerate_job_cover(
                        job_id,
                        on_log=lambda message, jid=job_id: pr.append_log(pr.job_dir_for(jid), message),
                    )
                    completed.append(job_id)
                except Exception as exc:
                    errors.append(f"{job_id}: {redact_secret_text(exc)}")
            self.root.after(0, lambda: self._finish_cover_regeneration(completed, errors))

        threading.Thread(target=worker, daemon=True).start()

    def _finish_cover_regeneration(self, completed: list[str], errors: list[str]):
        self._refresh_jobs()
        if errors:
            summary = f"已完成 {len(completed)} 个，失败 {len(errors)} 个。\n\n" + "\n".join(errors)
            messagebox.showerror("部分封面生成失败", summary)
        else:
            messagebox.showinfo("封面生成完成", f"已重新生成 {len(completed)} 个选中任务的封面。")

    def _finish_manual_upload(self, ids: list[str], errors: list[str], *, scheduled: bool = False):
        self._refresh_jobs()
        if errors:
            title = "定时发布需要检查" if scheduled else "部分上传失败"
            messagebox.showerror(title, "\n".join(errors))
        else:
            if scheduled:
                messagebox.showinfo("预约完成", "视频已经上传，并完成 YouTube 定时发布预约。")
            else:
                messagebox.showinfo("上传完成", f"已上传 {len(ids)} 个选中任务。")

    def _enqueue_selected_jobs(self):
        """Put selected idle jobs into the durable auto-start queue.

        Unlike "启动选中", this never launches a worker immediately.  A
        finishing worker picks queued jobs up when capacity becomes available.
        """
        ids = self._selected_job_ids()
        if not ids:
            messagebox.showwarning("没有选择任务", "请先选择要加入排队序列的任务。")
            return
        queued = []
        skipped = []
        for job_id in ids:
            status = pr.load_status(job_id)
            if status.get("worker_alive"):
                skipped.append(job_id)
                continue
            if not str(status.get("input") or "").strip():
                skipped.append(job_id)
                continue
            pr.clear_worker_pid(job_id)
            pr.write_status(
                pr.job_dir_for(job_id),
                job_id=job_id,
                stage="queued",
                progress=0.0,
                worker_pid=None,
                error="",
                queued_at=time.strftime("%Y-%m-%d %H:%M:%S"),
            )
            pr.append_log(pr.job_dir_for(job_id), "queued by '加入排队序列'")
            queued.append(job_id)
        self._refresh_jobs()
        if queued:
            messagebox.showinfo(
                "已加入排队序列",
                f"已加入 {len(queued)} 个任务。当前制作任务结束并有空位后，会自动启动。"
                + (f"\n\n未加入 {len(skipped)} 个任务（正在运行或缺少输入）。" if skipped else ""),
            )
        elif skipped:
            messagebox.showwarning("未加入排队序列", "所选任务正在运行，或缺少输入内容。")

    def _compose_selected(self):
        for job_id in self._selected_job_ids():
            status = pr.load_status(job_id)
            if status.get("worker_alive"):
                messagebox.showwarning("任务正在运行", f"{job_id} 正在运行，请先停止后再仅重试合成。")
                continue
            fallback = pr.image_fallback_info(job_id)
            tts = pr.tts_completion_info(job_id)
            input_text = status.get("input", "")
            if not tts.get("complete"):
                if not fallback.get("needs_fallback"):
                    messagebox.showwarning("配音尚未完成", "仅重试合成需要完整配音。请用“启动选中”补完 TTS 后再合成。")
                    continue
                answer = messagebox.askyesnocancel(
                    "配音未完成：补完后继续合成",
                    f"配音尚未完成；已保存 {fallback.get('available_images', 0)}/{fallback.get('total_images', 0)} 张图片。\n\n"
                    "选择“是”：先补完 TTS，再循环使用已有图片完成合成。\n"
                    "选择“否”：先补完 TTS，后续固定使用最后一张已有图片。\n"
                    "选择“取消”：不继续。\n\n"
                    "补 TTS 时不会重新调用图片 API。",
                )
                if answer is None:
                    continue
                try:
                    pr.set_image_fallback_selection(job_id, "cycle" if answer else "hold_last")
                    pr.start_worker(input_text, job_id=job_id, resume=True)
                except Exception as exc:
                    messagebox.showerror("补完 TTS 并继续合成失败", str(exc))
                continue
            if fallback.get("needs_fallback") and not self._choose_image_fallback(job_id, fallback):
                continue
            try:
                pr.start_worker(input_text, job_id=job_id, resume=True, compose_only=True)
            except Exception as exc:
                messagebox.showerror("仅重试合成失败", str(exc))
        self._refresh_jobs()

    def _start_all_pending(self):
        try:
            # Save once before dispatching the batch.  The workers then retain
            # this configuration in their normal per-job snapshots.
            self._apply_config_form(save_profile=False)
            queued, started = pr.queue_all_pending_jobs()
        except Exception as exc:
            messagebox.showerror("启动全部待处理失败", str(exc))
            return
        self._refresh_jobs()
        if queued:
            waiting = len(queued) - len(started)
            messagebox.showinfo(
                "已加入任务队列",
                f"已安排 {len(queued)} 个待处理任务。"
                + (f"其中 {len(started)} 个已启动，剩余 {waiting} 个会在前面的任务结束后自动继续。" if waiting else "已全部启动。"),
            )
        else:
            messagebox.showinfo("没有待处理任务", "没有可启动的待处理任务。")

    def _start_job(self, job_id: str, selected_job_ids: list[str] | None = None):
        st = pr.load_status(job_id)
        if st.get("worker_alive"):
            return
        info = pr.series_start_choice_info(job_id, selected_job_ids or [job_id])
        if info:
            answer = messagebox.askyesnocancel(
                "检测到系列动画",
                f"检测到《{info['series_title']}》共有 {info['total']} 个分段任务，"
                f"当前只启动第 {info['episode']} 个。\n\n"
                "选择“是”：按系列方式制作。如果系列名尚未建立，就由当前这一集生成并保存共享小说名；"
                "随后立即制作本集标题和封面，不等待其他分段。\n\n"
                "选择“否”：本次按单条视频制作，立即生成普通标题和封面。\n\n"
                "选择“取消”：不启动这个任务。",
            )
            if answer is None:
                return
            pr.set_job_series_animation_mode(job_id, "series" if answer else "single")
            st = pr.load_status(job_id)
        try:
            # Persist the form currently on screen before the worker creates
            # its per-job snapshot.  This makes the selected profile and its
            # displayed fields the exact settings used by this task.
            self._apply_config_form(save_profile=False)
        except Exception as exc:
            messagebox.showerror("启动失败", f"无法应用当前配置方案：\n{exc}")
            return
        dictionary_path = self.pronunciation_dictionary_var.get().strip()
        if dictionary_path:
            try:
                resolved = self._inspect_pronunciation_dictionary_with_choices(dictionary_path)
                if resolved is None:
                    return
                dictionary_info, choices = resolved
                attached_hash = str(st.get("pronunciation_dictionary_hash") or "")
                internal_dictionary = pr.job_dir_for(job_id) / pr.TTS_PRONUNCIATION_DICTIONARY
                if attached_hash != str(dictionary_info["hash"]) or not internal_dictionary.exists():
                    pr.attach_pronunciation_dictionary(
                        job_id,
                        dictionary_path,
                        conflict_choices=choices or None,
                    )
                    st = pr.load_status(job_id)
            except Exception as exc:
                messagebox.showerror("启动失败", f"读音词典自动附加失败：\n{exc}")
                self._refresh_jobs()
                return
        input_text = st.get("input", "")
        if not input_text:
            messagebox.showwarning("缺少输入", f"{job_id} 没有 input")
            return
        try:
            pr.start_worker(input_text, job_id=job_id, resume=True)
        except Exception as exc:
            messagebox.showerror("启动失败", str(exc))
        self._refresh_jobs()

    def _clear_selected_media_cache(self):
        ids = self._selected_job_ids()
        if not ids:
            return
        if not messagebox.askyesno("确认清除缓存", "会删除选中任务的图片、封面和最终视频，下次启动会重新调用生图 API 和合成视频。继续吗？"):
            return
        errors = []
        for job_id in ids:
            try:
                pr.clear_job_media_cache(job_id)
            except Exception as exc:
                errors.append(f"{job_id}: {exc}")
        self._refresh_jobs()
        if errors:
            messagebox.showerror("清除失败", "\n".join(errors))

    def _retry_all(self):
        ids = self._selected_job_ids()
        if not ids:
            messagebox.showwarning("没有选择任务", "请先选择要全部重试的任务。")
            return
        message = (
            "将从头重新执行选中的任务，包括文本处理、分镜、全部配图、封面、配音、字幕和视频合成。\n\n"
            "旧图片、旧配音和旧成片会被删除，并重新调用相关 AI 接口；原始输入文件、手动读音词典和导入的 MP3 会保留。\n"
            "本次会使用界面当前显示的配置。继续吗？"
        )
        if not messagebox.askyesno("全部重试（重新生图）", message):
            return
        if not self._stop_jobs_for_tts_operation(ids, "全部重试"):
            return
        errors = []
        for job_id in ids:
            try:
                # Save/apply the visible profile before removing the old
                # per-job snapshot. _start_job then freezes these settings.
                self._apply_config_form(save_profile=False)
                pr.reset_full_job(job_id)
                self._start_job(job_id)
            except Exception as exc:
                errors.append(f"{job_id}: {exc}")
        self._refresh_jobs()
        if errors:
            messagebox.showerror("全部重试失败", "\n".join(errors))
        else:
            messagebox.showinfo("已开始全部重试", f"已从头启动 {len(ids)} 个任务，将重新生成全部图片和成片。")

    def _retry_from_clean_reuse_images(self):
        ids = self._selected_job_ids()
        if not ids:
            return
        message = (
            "会从洗稿、朗读净化和切片重新开始，并重做全部配音、字幕和成片。\n"
            "已有图片会被复用，不会调用任何生图 API；新音频会按现有图片数量平均分配画面时长。继续吗？"
        )
        if not messagebox.askyesno("重试（生图除外）", message):
            return
        if not self._stop_jobs_for_tts_operation(ids, "重试（生图除外）"):
            return
        errors = []
        for job_id in ids:
            try:
                pr.reset_from_clean_reuse_images(job_id)
                self._start_job(job_id)
            except Exception as exc:
                errors.append(f"{job_id}: {exc}")
        self._refresh_jobs()
        if errors:
            messagebox.showerror("重试失败", "\n".join(errors))

    def _stop_jobs_for_tts_operation(self, ids: list[str], action: str) -> bool:
        running = [job_id for job_id in ids if pr.load_status(job_id).get("worker_alive")]
        if not running:
            return True
        if not messagebox.askyesno("需要先停止任务", f"{action} 需要先停止 {len(running)} 个正在运行的任务。已生成的中间文件会保留。继续吗？"):
            return False
        errors = []
        for job_id in running:
            try:
                pr.stop_job(job_id)
            except Exception as exc:
                errors.append(f"{job_id}: {exc}")
        if errors:
            self._refresh_jobs()
            self._update_log()
            messagebox.showerror("停止失败", "\n".join(errors))
            return False
        return True

    def _parse_tts_indices(self, raw: str) -> list[int] | None:
        text = str(raw or "").strip()
        if not text:
            return None
        values: list[int] = []
        for token in re.split(r"[\s,，、]+", text):
            token = token.strip().lower()
            if not token:
                continue
            token = token[3:] if token.startswith("seg") else token
            try:
                index = int(token)
            except Exception as exc:
                raise ValueError(f"无法识别段编号: {token}") from exc
            if index < 0:
                raise ValueError(f"段编号不能小于 0: {index}")
            values.append(index)
        return sorted(set(values))

    def _retry_tts_segments(self):
        ids = self._selected_job_ids()
        if not ids:
            return
        raw = simpledialog.askstring("重试TTS段", "输入段编号，如 0 或 0,3,7；留空则重试失败/卡住/波形异常段。", parent=self.root)
        if raw is None:
            return
        try:
            indices = self._parse_tts_indices(raw)
        except Exception as exc:
            messagebox.showerror("段编号错误", str(exc))
            return
        if indices is not None and len(ids) > 1:
            messagebox.showwarning("只能选择一个任务", "指定段编号重试时，请只选择一个任务。")
            return
        if not self._stop_jobs_for_tts_operation(ids, "重试 TTS 段"):
            return
        errors = []
        total = 0
        for job_id in ids:
            try:
                result = pr.reset_tts_segments(job_id, indices=indices)
                total += int(result.get("changed") or 0)
            except Exception as exc:
                errors.append(f"{job_id}: {exc}")
        self._refresh_jobs()
        self._update_log()
        if errors:
            messagebox.showerror("TTS 重试处理失败", "\n".join(errors))
        else:
            for job_id in ids:
                self._start_job(job_id)
            messagebox.showinfo("已重置 TTS 段", f"已重置 {total} 个 TTS 段，并已重新启动任务继续合成真实音频。")

    def _choose_tts_redo_mode(self) -> str | None:
        """Offer the two deliberately different full-TTS actions."""
        dialog = tk.Toplevel(self.root)
        dialog.title("重做 TTS")
        dialog.transient(self.root)
        dialog.resizable(False, False)
        dialog.grab_set()
        choice: list[str | None] = [None]
        body = ttk.Frame(dialog, padding=18)
        body.grid(sticky="nsew")
        ttk.Label(body, text="请选择配音处理方式", font=(UI_FONT, UI_HEADING_FONT_SIZE, "bold")).grid(row=0, column=0, columnspan=2, sticky="w")
        ttk.Label(
            body,
            text="全段修复：只补未完成、失败或异常的段。\n重做 TTS：重做全部配音；保留现有图片和封面，只重新合成视频。",
            justify=tk.LEFT,
            wraplength=420,
        ).grid(row=1, column=0, columnspan=2, pady=(10, 16), sticky="w")

        def pick(value: str | None):
            choice[0] = value
            dialog.destroy()

        ttk.Button(body, text="全段修复", command=lambda: pick("repair")).grid(row=2, column=0, padx=(0, 8), sticky="ew")
        ttk.Button(body, text="重做 TTS", command=lambda: pick("redo")).grid(row=2, column=1, sticky="ew")
        ttk.Button(body, text="取消", command=lambda: pick(None)).grid(row=3, column=0, columnspan=2, pady=(10, 0))
        dialog.protocol("WM_DELETE_WINDOW", lambda: pick(None))
        self.root.wait_window(dialog)
        return choice[0]

    def _redo_tts(self):
        ids = self._selected_job_ids()
        if not ids:
            return
        mode = self._choose_tts_redo_mode()
        if mode is None:
            return
        if mode == "repair":
            self._retry_stalled_tts_segments()
            return
        if not self._stop_jobs_for_tts_operation(ids, "重做 TTS"):
            return
        errors = []
        total = 0
        for job_id in ids:
            try:
                result = pr.redo_all_tts_reuse_images(job_id)
                total += int(result.get("changed") or 0)
            except Exception as exc:
                errors.append(f"{job_id}: {exc}")
        self._refresh_jobs()
        self._update_log()
        if errors:
            messagebox.showerror("重做 TTS 失败", "\n".join(errors))
            return
        for job_id in ids:
            self._start_job(job_id)
        messagebox.showinfo(
            "已开始重做 TTS",
            f"已重置 {total} 个配音段。现有图片与封面将被保留，不会调用生图接口；完成后会自动重新合成视频。",
        )

    def _retry_stalled_tts_segments(self):
        ids = self._selected_job_ids()
        if not ids:
            return
        if not messagebox.askyesno("TTS 重试推进", "会重置未完成/失败/波形异常的 TTS 段，清掉受音频时长影响的合成缓存，并重新启动任务继续重试。继续吗？"):
            return
        if not self._stop_jobs_for_tts_operation(ids, "TTS 重试推进"):
            return
        errors = []
        total = 0
        for job_id in ids:
            try:
                result = pr.reset_tts_unfinished_segments(job_id)
                total += int(result.get("changed") or 0)
            except Exception as exc:
                errors.append(f"{job_id}: {exc}")
        self._refresh_jobs()
        self._update_log()
        if errors:
            messagebox.showerror("TTS 重试推进失败", "\n".join(errors))
        else:
            for job_id in ids:
                self._start_job(job_id)
            messagebox.showinfo("已处理 TTS 卡段", f"已重置 {total} 个 TTS 段，并已重新启动任务继续生成真实音频。")

    def _stop_selected_jobs(self):
        ids = self._selected_job_ids()
        if not ids:
            return
        active_stages = {"queued", "worker_starting", "starting", "scrape", "clean", "tts", "images_prefetch", "pacing", "images", "image_decision", "cover", "compose", "upload", "stopping"}
        running = [
            job_id for job_id in ids
            if (status := pr.load_status(job_id)).get("worker_alive") or status.get("stage") in active_stages
        ]
        if not running:
            messagebox.showinfo("无需停止", "选中的任务没有正在运行的 worker。")
            return
        if not messagebox.askyesno("确认停止任务", f"将强制停止 {len(running)} 个正在运行的任务。已生成的中间文件会保留，可之后续跑。继续吗？"):
            return
        errors = []
        for job_id in running:
            try:
                pr.stop_job(job_id)
            except Exception as exc:
                errors.append(f"{job_id}: {exc}")
        self._refresh_jobs()
        self._update_log()
        if errors:
            messagebox.showerror("停止失败", "\n".join(errors))

    def _stop_all_jobs(self):
        """Stop every active worker, including jobs currently in the upload stage."""
        active_stages = {
            "queued", "worker_starting", "starting", "scrape", "clean", "tts",
            "images_prefetch", "pacing", "images", "image_decision", "cover",
            "compose", "upload", "stopping",
        }
        rows = pr.list_jobs(limit=10000)
        running = []
        for row in rows:
            job_id = str(row.get("job_id") or "")
            if not job_id:
                continue
            status = pr.load_status(job_id)
            if status.get("worker_alive") or status.get("stage") in active_stages:
                running.append(job_id)
        if not running and not self._script_upload_running:
            messagebox.showinfo("无需停止", "当前没有正在运行或上传的任务。")
            return
        upload_count = sum(1 for job_id in running if pr.load_status(job_id).get("stage") == "upload")
        detail = f"将停止全部 {len(running)} 个活动任务"
        if upload_count:
            detail += f"，其中 {upload_count} 个正在上传"
        detail += "。已生成的中间文件会保留，可之后续跑。继续吗？"
        if not messagebox.askyesno("确认停止一切任务", detail):
            return
        errors = []
        for job_id in running:
            try:
                pr.stop_job(job_id)
            except Exception as exc:
                errors.append(f"{job_id}: {exc}")
        self._refresh_jobs()
        self._update_log()
        if errors:
            messagebox.showerror("部分任务停止失败", "\n".join(errors))
        else:
            messagebox.showinfo("已停止", f"已请求停止 {len(running)} 个活动任务。")

    def _delete_selected_jobs(self):
        ids = self._selected_job_ids()
        if not ids:
            return
        running = [job_id for job_id in ids if pr.load_status(job_id).get("worker_alive")]
        if running:
            message = f"将删除 {len(ids)} 个任务目录，其中 {len(running)} 个正在运行，会先强制停止再删除。此操作不可恢复。继续吗？"
        else:
            message = f"将删除 {len(ids)} 个任务目录。此操作不可恢复。继续吗？"
        if not messagebox.askyesno("确认删除任务", message):
            return
        errors = []
        for job_id in ids:
            try:
                pr.delete_job(job_id, stop_running=True)
            except Exception as exc:
                errors.append(f"{job_id}: {exc}")
        self._selected_job = ""
        self._refresh_jobs()
        if errors:
            messagebox.showerror("删除失败", "\n".join(errors))

    def _delete_finished_jobs(self):
        rows = pr.list_jobs(limit=1000)
        ids = [row["job_id"] for row in rows if row.get("stage") in {"completed", "failed", "missing", "broken"} and not row.get("worker_alive")]
        if not ids:
            messagebox.showinfo("无需清理", "没有已结束任务可清理。")
            return
        if not messagebox.askyesno("确认清空", f"将删除 {len(ids)} 个已结束/失败任务目录。继续吗？"):
            return
        errors = []
        for job_id in ids:
            try:
                pr.delete_job(job_id)
            except Exception as exc:
                errors.append(f"{job_id}: {exc}")
        self._selected_job = ""
        self._refresh_jobs()
        if errors:
            messagebox.showerror("清空失败", "\n".join(errors))

    @staticmethod
    def _natural_task_key(value: str):
        return tuple(
            (0, int(part)) if part.isdigit() else (1, part.casefold())
            for part in re.split(r"(\d+)", unicodedata.normalize("NFKC", str(value or "")))
        )

    @staticmethod
    def _saved_task_source(status: dict) -> tuple[Path | None, Path | None]:
        source_text = str(status.get("source_path") or "").strip()
        directory_text = str(status.get("source_directory") or "").strip()
        source = Path(source_text).expanduser() if source_text else None
        directory = Path(directory_text).expanduser() if directory_text else (source.parent if source else None)
        return source, directory

    @staticmethod
    def _path_is_within(path: Path | None, root: Path | None) -> bool:
        if path is None or root is None:
            return False
        try:
            path.resolve(strict=False).relative_to(root.resolve(strict=False))
            return True
        except (OSError, ValueError):
            return False

    def _choose_task_category_root(self):
        selected = filedialog.askdirectory(
            title="选择任务文本来源的分类根目录",
            initialdir=self.task_category_root_var.get().strip() or str(Path.home()),
        )
        if not selected:
            return
        self.task_category_root_var.set(str(Path(selected).expanduser()))
        config.set("task_category_root", self.task_category_root_var.get().strip())
        config.set("task_category_selected_path", "")
        config.save()
        self.job_tree.selection_remove(self.job_tree.selection())
        self._selected_job = ""
        self._rebuild_task_category_tree()
        self._refresh_jobs()

    def _clear_task_category_root(self):
        self.task_category_root_var.set("")
        config.set("task_category_root", "")
        config.set("task_category_selected_path", "")
        config.save()
        self.job_tree.selection_remove(self.job_tree.selection())
        self._selected_job = ""
        self._rebuild_task_category_tree()
        self._refresh_jobs()

    def _on_task_category_sort_changed(self, _event=None):
        if not getattr(self, "_task_category_active", False):
            return
        config.set("task_category_sort", self.task_category_sort_var.get())
        config.set("task_category_direction", self.task_category_direction_var.get())
        config.save()
        self.job_tree.selection_remove(self.job_tree.selection())
        self._selected_job = ""
        self._refresh_jobs()

    def _on_task_category_selected(self, _event=None):
        if not getattr(self, "_task_category_active", False):
            return
        selection = self.task_category_tree.selection()
        selected_path = (
            self._task_category_paths.get(str(selection[0]), "")
            if selection else ""
        )
        config.set("task_category_root", self.task_category_root_var.get().strip())
        config.set("task_category_selected_path", selected_path)
        config.set("task_category_sort", self.task_category_sort_var.get())
        config.set("task_category_direction", self.task_category_direction_var.get())
        config.save()
        self.job_tree.selection_remove(self.job_tree.selection())
        self._selected_job = ""
        self._refresh_jobs()

    def _selected_task_category_path(self) -> Path | None:
        selection = self.task_category_tree.selection()
        if selection:
            value = self._task_category_paths.get(str(selection[0]), "")
            if value:
                return Path(value).expanduser()
        root = self.task_category_root_var.get().strip()
        return Path(root).expanduser() if root else None

    def _rebuild_task_category_tree(self):
        tree = self.task_category_tree
        previous_path = str(config.get("task_category_selected_path", "") or "")
        selection = tree.selection()
        if selection:
            previous_path = self._task_category_paths.get(str(selection[0]), "")
        tree.delete(*tree.get_children())
        self._task_category_paths.clear()

        rows = pr.list_jobs(limit=10000)
        root_text = self.task_category_root_var.get().strip()
        if not root_text:
            local_count = 0
            for row in rows:
                _source, directory = self._saved_task_source(pr.load_status(row["job_id"], include_worker=False))
                local_count += int(directory is not None)
            all_iid = tree.insert("", tk.END, text="全部来源", values=(len(rows),), open=True)
            self._task_category_paths[all_iid] = ""
            local_iid = tree.insert(all_iid, tk.END, text="本地文件任务", values=(local_count,))
            unclassified_iid = tree.insert(
                all_iid,
                tk.END,
                text="书库 / 粘贴 / 未分类",
                values=(len(rows) - local_count,),
            )
            self._task_category_paths[local_iid] = "__local__"
            self._task_category_paths[unclassified_iid] = "__unclassified__"
            selected_iid = {
                "__local__": local_iid,
                "__unclassified__": unclassified_iid,
            }.get(previous_path, all_iid)
            tree.selection_set(selected_iid)
            tree.see(selected_iid)
            return

        root = Path(root_text).expanduser().resolve(strict=False)
        counts: dict[Path, int] = {root: 0}
        children: dict[Path, set[Path]] = {}
        for row in rows:
            _source, directory = self._saved_task_source(pr.load_status(row["job_id"], include_worker=False))
            if not self._path_is_within(directory, root):
                continue
            directory = directory.resolve(strict=False)
            counts[root] = counts.get(root, 0) + 1
            relative_parts = directory.relative_to(root).parts
            parent = root
            for part in relative_parts:
                child = parent / part
                children.setdefault(parent, set()).add(child)
                counts[child] = counts.get(child, 0) + 1
                parent = child

        root_label = root.name or str(root)
        root_iid = tree.insert("", tk.END, text=root_label, values=(counts.get(root, 0),), open=True)
        self._task_category_paths[root_iid] = str(root)

        def insert_children(parent_iid: str, parent_path: Path):
            ordered = sorted(children.get(parent_path, set()), key=lambda path: self._natural_task_key(path.name))
            for child_path in ordered:
                label = child_path.name
                if not child_path.exists():
                    label += "（来源缺失）"
                iid = tree.insert(parent_iid, tk.END, text=label, values=(counts.get(child_path, 0),))
                self._task_category_paths[iid] = str(child_path)
                insert_children(iid, child_path)

        insert_children(root_iid, root)
        selected_iid = next(
            (iid for iid, path in self._task_category_paths.items() if path == previous_path),
            root_iid,
        )
        tree.selection_set(selected_iid)
        tree.see(selected_iid)

    def _select_all_visible_jobs(self):
        rows = self.job_tree.get_children()
        if not rows:
            messagebox.showinfo("没有可选任务", "当前目录和排序条件下没有任务。")
            return
        self.job_tree.selection_set(rows)
        self.job_tree.focus(rows[0])
        self.job_tree.see(rows[0])
        self._on_job_select()

    def _classified_job_rows(self) -> list[tuple[dict, dict]]:
        rows_with_status = [
            (row, pr.load_status(row["job_id"]))
            for row in pr.list_jobs(limit=10000)
        ]
        tree_selection = self.task_category_tree.selection()
        special_filter = (
            self._task_category_paths.get(str(tree_selection[0]), "")
            if tree_selection else ""
        )
        if special_filter == "__local__":
            rows_with_status = [
                (row, status)
                for row, status in rows_with_status
                if self._saved_task_source(status)[1] is not None
            ]
        elif special_filter == "__unclassified__":
            rows_with_status = [
                (row, status)
                for row, status in rows_with_status
                if self._saved_task_source(status)[1] is None
            ]
        else:
            filter_path = self._selected_task_category_path()
            if filter_path is not None:
                rows_with_status = [
                    (row, status)
                    for row, status in rows_with_status
                    if self._path_is_within(self._saved_task_source(status)[1], filter_path)
                ]

        sort_name = self.task_category_sort_var.get()
        reverse = self.task_category_direction_var.get() == "降序"

        def added_time(job_id: str) -> float:
            try:
                stat = pr.job_dir_for(job_id).stat()
                return float(getattr(stat, "st_birthtime", stat.st_ctime))
            except OSError:
                return 0.0

        def source_mtime(status: dict) -> float:
            source, _directory = self._saved_task_source(status)
            try:
                return float(source.stat().st_mtime) if source else 0.0
            except OSError:
                return 0.0

        def sort_key(item: tuple[dict, dict]):
            row, status = item
            if sort_name == "添加日期":
                return (added_time(row["job_id"]), self._natural_task_key(row["job_id"]))
            if sort_name == "文本修改日期":
                return (source_mtime(status), self._natural_task_key(row["job_id"]))
            if sort_name == "任务更新时间":
                return (str(status.get("updated_at") or ""), self._natural_task_key(row["job_id"]))
            if sort_name == "制作阶段":
                return (
                    str(row.get("stage_display") or row.get("stage") or ""),
                    self._natural_task_key(row.get("title") or row["job_id"]),
                )
            return self._natural_task_key(row.get("title") or row["job_id"])

        rows_with_status.sort(key=sort_key, reverse=reverse)
        return rows_with_status

    def _refresh_jobs(self):
        # Polling used to delete and recreate every row every 2.5 seconds.
        # That silently cleared the selection just as an operator clicked a
        # task action.  Preserve both the selected rows and viewport across a
        # data refresh so task actions always operate on the visible choice.
        selected_ids = list(self.job_tree.selection())
        focused_id = self.job_tree.focus()
        yview = self.job_tree.yview()
        xview = self.job_tree.xview()
        self.job_tree.delete(*self.job_tree.get_children())
        self._job_table_full_values: dict[str, dict[str, str]] = {}
        inserted_ids: set[str] = set()
        rows_with_status = (
            self._classified_job_rows()
            if getattr(self, "_task_category_active", False)
            else [(row, pr.load_status(row["job_id"])) for row in pr.list_jobs(limit=500)]
        )
        for row, st in rows_with_status:
            worker = f"PID {st.get('worker_pid')}" if st.get("worker_alive") else ""
            full_values = {
                "title": str(row.get("title", "") or ""),
                "video": str(row.get("video", "") or ""),
                "youtube": str(st.get("youtube_url", "") or ""),
            }
            self._job_table_full_values[str(row["job_id"])] = full_values
            values = (
                self._display_job_id(row["job_id"]),
                row.get("stage_display", row.get("stage", "")),
                f"{float(row.get('progress') or 0) * 100:.0f}%",
                worker,
                "导入 MP3" if st.get("audio_mode") == "imported" else "TTS",
                f"已附加 {int(st.get('pronunciation_dictionary_entries') or 0)}条" if st.get("pronunciation_dictionary") else "未附加",
                self._display_job_column("title", full_values["title"]),
                self._display_job_column("video", full_values["video"]),
                self._display_job_column("youtube", full_values["youtube"]),
            )
            self.job_tree.insert("", tk.END, iid=row["job_id"], values=values)
            inserted_ids.add(str(row["job_id"]))
        retained = [job_id for job_id in selected_ids if job_id in inserted_ids]
        if retained:
            self.job_tree.selection_set(retained)
            self.job_tree.focus(focused_id if focused_id in retained else retained[0])
            self.job_tree.see(retained[0])
        elif self._selected_job and self._selected_job not in inserted_ids:
            self._selected_job = ""
        if yview:
            self.job_tree.yview_moveto(yview[0])
        if xview:
            self.job_tree.xview_moveto(xview[0])

    def _job_tree_font(self):
        font = getattr(self, "_job_id_font", None)
        if font is None:
            font_spec = ttk.Style(self.root).lookup("Treeview", "font") or (UI_FONT, UI_FONT_SIZE)
            font = tkfont.Font(root=self.root, font=font_spec)
            self._job_id_font = font
        return font

    def _display_job_column(self, column: str, value: str) -> str:
        """Fit long table content to a resizable column with a visible ellipsis."""
        text = str(value or "")
        if not text:
            return ""
        try:
            available = max(24, int(self.job_tree.column(column, "width")) - 14)
            font = self._job_tree_font()
        except Exception:
            return text
        if font.measure(text) <= available:
            return text

        if column == "title":
            visible = ""
            for char in text:
                candidate = f"{visible}{char}…"
                if font.measure(candidate) > available:
                    break
                visible += char
            return f"{visible}…" if visible else "…"

        head = ""
        tail = ""
        # Paths and URLs are usually distinguished by their filename/video ID,
        # so reserve useful space for the end before revealing more of the start.
        for tail_length in range(1, min(12, len(text)) + 1):
            next_tail = text[-tail_length:]
            if font.measure(f"…{next_tail}") > available:
                break
            tail = next_tail
        while len(head) + len(tail) < len(text):
            next_head = text[:len(head) + 1]
            if font.measure(f"{next_head}…{tail}") > available:
                break
            head = next_head
        return f"{head}…{tail}"

    def _display_job_id(self, job_id: str) -> str:
        """Fit the visible ID to the current column while retaining its suffix."""
        text = str(job_id or "")
        try:
            available = max(24, int(self.job_tree.column("job_id", "width")) - 14)
            font = self._job_tree_font()
        except Exception:
            return text
        if font.measure(text) <= available:
            return text

        suffix_length = min(6, max(1, len(text) - 2))
        suffix = text[-suffix_length:]
        head_length = 1
        candidate = f"{text[:head_length]}…{suffix}"
        while suffix_length > 1 and font.measure(candidate) > available:
            suffix_length -= 1
            suffix = text[-suffix_length:]
            candidate = f"{text[:head_length]}…{suffix}"
        while head_length < len(text) - suffix_length:
            next_candidate = f"{text[:head_length + 1]}…{suffix}"
            if font.measure(next_candidate) > available:
                break
            head_length += 1
            candidate = next_candidate
        return candidate

    def _refresh_job_table_display(self):
        """Reveal or hide long cell content after the operator resizes columns."""
        for iid in self.job_tree.get_children():
            values = list(self.job_tree.item(iid, "values"))
            if values:
                values[0] = self._display_job_id(str(iid))
                full_values = getattr(self, "_job_table_full_values", {}).get(str(iid), {})
                values[6] = self._display_job_column("title", full_values.get("title", values[6]))
                values[7] = self._display_job_column("video", full_values.get("video", values[7]))
                values[8] = self._display_job_column("youtube", full_values.get("youtube", values[8]))
                self.job_tree.item(iid, values=values)

    def _poll_jobs(self):
        self._refresh_jobs()
        self._prompt_image_failure_choices()
        self.root.after(2500, self._poll_jobs)

    def _poll_script_publish_queue(self):
        try:
            self._run_script_publish_queue_once()
        except Exception as exc:
            self._append_probe_log(f"[脚本内定时] 检查队列失败：{exc}")
        finally:
            self.root.after(15000, self._poll_script_publish_queue)

    def _run_script_publish_queue_once(self):
        if self._script_upload_running or not bool(config.get("upload_enabled", False)):
            return
        profiles = _parse_upload_profiles(str(config.get("browser_profiles", "[]") or "[]"))
        active_name = str(config.get("browser_active_profile", "") or "").strip()
        profile = next((item for item in profiles if str(item.get("name") or "") == active_name), profiles[0] if profiles else None)
        if not profile or str(profile.get("publish_mode") or "immediate") != "script":
            return
        profile_name = str(profile.get("name") or "").strip()
        try:
            first_at = datetime.strptime(
                f"{str(profile.get('script_schedule_first_date') or '').strip()} "
                f"{str(profile.get('script_schedule_time') or '').strip()}",
                "%Y-%m-%d %H:%M",
            )
            interval = max(1, int(profile.get("script_schedule_interval_hours") or 24))
            zone = ZoneInfo(str(profile.get("script_schedule_timezone") or "Asia/Tokyo"))
        except (TypeError, ValueError):
            return
        statuses = [(row["job_id"], pr.load_status(row["job_id"])) for row in pr.list_jobs(limit=10000)]
        if not bool(profile.get("script_manual_queue", False)):
            publish_scheduler.ensure_jobs(profile_name, statuses, first_at, interval)
        for queued in publish_scheduler.load_items(profile_name):
            if str(queued.get("state") or "") != "uploading":
                continue
            interrupted_job = str(queued.get("job_id") or "")
            interrupted_status = pr.load_status(interrupted_job)
            known_url = str(interrupted_status.get("youtube_url") or "").strip()
            publish_scheduler.update_item(
                profile_name,
                interrupted_job,
                state="published" if known_url else "needs_review",
                youtube_url=known_url,
                last_error="" if known_url else "软件上次退出时此视频正在上传；为避免重复上传，请到 YouTube Studio 检查。",
            )
        schedule_now = datetime.now(zone).replace(tzinfo=None)
        item = publish_scheduler.due_item(profile_name, now=schedule_now)
        if not item:
            return
        scheduled_text = str(item.get("scheduled_at") or "")
        scheduled_at = datetime.strptime(scheduled_text, "%Y-%m-%dT%H:%M")
        job_id = str(item.get("job_id") or "")
        status = pr.load_status(job_id)
        video = Path(str(status.get("video") or pr.video_output_path(pr.job_dir_for(job_id))))
        ready = not status.get("worker_alive") and video.exists() and video.stat().st_size >= 100
        if not ready:
            if profile.get("script_schedule_unfinished_action") == "next_slot":
                publish_scheduler.postpone_pending(profile_name, scheduled_text, interval)
                pr.append_log(pr.job_dir_for(job_id), f"脚本内定时：视频尚未完成，队列顺延 {interval} 小时")
            return
        if profile.get("script_schedule_missed_action") == "next_slot" and schedule_now > scheduled_at + timedelta(minutes=10):
            publish_scheduler.postpone_pending(profile_name, scheduled_text, interval)
            self._append_probe_log(f"[脚本内定时] 已错过 {scheduled_text.replace('T', ' ')}，整个待发布队列顺延 {interval} 小时。")
            return
        publish_scheduler.update_item(profile_name, job_id, state="uploading", last_error="")
        pr.write_status(
            pr.job_dir_for(job_id),
            publish_schedule_mode="script",
            publish_schedule_state="uploading",
            publish_scheduled_at=scheduled_text,
            publish_schedule_profile=profile_name,
        )
        self._script_upload_running = True
        pr.append_log(pr.job_dir_for(job_id), f"脚本内定时：到达 {scheduled_text.replace('T', ' ')}，开始自动上传")

        def worker():
            try:
                url = pr.upload_completed_job(
                    job_id,
                    on_log=lambda message: pr.append_log(pr.job_dir_for(job_id), message),
                    profile_name_override=profile_name,
                    schedule_enabled_override=False,
                )
                publish_scheduler.update_item(profile_name, job_id, state="published", youtube_url=url, last_error="")
                pr.write_status(
                    pr.job_dir_for(job_id),
                    publish_schedule_state="published",
                    publish_schedule_url=url,
                    publish_schedule_error="",
                )
                self.root.after(0, lambda: self._append_probe_log(f"[脚本内定时] 已发布：{job_id} {url}"))
            except Exception as exc:
                publish_scheduler.update_item(
                    profile_name,
                    job_id,
                    state="retry",
                    scheduled_at=(datetime.now(zone).replace(tzinfo=None) + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M"),
                    last_error=str(exc),
                )
                pr.write_status(
                    pr.job_dir_for(job_id),
                    publish_schedule_state="retry",
                    publish_schedule_error=str(exc),
                )
                self.root.after(0, lambda exc=exc: self._append_probe_log(f"[脚本内定时] 上传失败，1 小时后重试：{exc}"))
            finally:
                self._script_upload_running = False

        threading.Thread(target=worker, daemon=True).start()

    def _prompt_image_failure_choices(self):
        """Show a single, explicit choice for workers paused after image failure."""
        for row in pr.list_jobs(limit=500):
            job_id = row["job_id"]
            status = pr.load_status(job_id)
            failure = status.get("image_failure")
            if status.get("stage") != "image_decision" or not isinstance(failure, dict):
                self._image_failure_prompted.discard(job_id)
                continue
            if job_id in self._image_failure_prompted:
                continue
            self._image_failure_prompted.add(job_id)
            try:
                self._choose_image_failure_action(job_id, failure)
            except Exception as exc:
                self._image_failure_prompted.discard(job_id)
                messagebox.showerror("无法继续合成", f"无法保存图片处理选择：\n{exc}")

    def _choose_image_failure_action(self, job_id: str, failure: dict) -> None:
        available = int(failure.get("available_images") or 0)
        total = int(failure.get("total_images") or 0)
        error = str(failure.get("error") or "图片生成失败")
        answer = messagebox.askyesnocancel(
            "图片生成失败：请选择处理方式",
            f"已成功保存 {available}/{total} 张图片，但后续图片生成失败。\n\n"
            "选择“是”：循环使用已成功生成的图片，继续完成视频；后续不再调用图片 API。\n"
            "选择“否”：跳过此任务，自动开始队列中的下一个任务；本任务保留，之后可手动处理。\n"
            "选择“取消”：保持暂停。\n\n"
            f"失败原因：{error}",
        )
        if answer is True:
            pr.set_image_fallback_selection(job_id, "cycle")
            return
        if answer is False:
            path = pr.job_dir_for(job_id) / pr.IMAGE_FAILURE_DECISION_FILE
            payload = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
            if not isinstance(payload, dict):
                payload = {}
            payload["decision"] = "skip"
            payload["decided_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def _choose_image_fallback(self, job_id: str, info: dict, *, title: str = "图片不完整：仅重试合成", error: str = "") -> bool:
        available = int(info.get("available_images") or 0)
        total = int(info.get("total_images") or 0)
        if available < 1:
            return False
        message = (
            f"已成功保存 {available}/{total} 张图片。\n"
            "可直接使用这些图片完成后续视频，且不会再调用图片 API。\n\n"
            "选择“是”：循环使用已有图片。\n"
            "选择“否”：从缺失位置起固定使用最后一张已生成图片。\n"
            "选择“取消”：不继续。"
        )
        if error:
            message += f"\n\n失败原因：{error}"
        answer = messagebox.askyesnocancel(title, message)
        if answer is None:
            return False
        pr.set_image_fallback_selection(job_id, "cycle" if answer else "hold_last")
        return True

    def _on_job_select(self, _event=None):
        ids = self._selected_job_ids()
        if ids:
            self._selected_job = ids[0]
            self._update_log()

    def _poll_log(self):
        self._update_log()
        self.root.after(1500, self._poll_log)

    def _update_log(self):
        if not self._selected_job:
            self.log_text.configure(state=tk.NORMAL)
            self.log_text.delete("1.0", tk.END)
            self.log_text.insert(tk.END, "请在左侧选择一个任务以查看实时日志")
            self.log_text.configure(state=tk.DISABLED)
            return
        text = pr.tail_log(self._selected_job, lines=500)
        self.log_text.configure(state=tk.NORMAL)
        self.log_text.delete("1.0", tk.END)
        self.log_text.insert(tk.END, text)
        self.log_text.see(tk.END)
        self.log_text.configure(state=tk.NORMAL)

    def _open_selected_output_dir(self):
        """Open this task's own job folder, where its completed MP4 is kept."""
        job_id = self._require_single_selected_job("打开任务文件夹")
        if job_id is None:
            return
        job_dir = pr.job_dir_for(job_id)
        path = job_dir
        try:
            if sys.platform == "darwin":
                subprocess.Popen(["open", str(path)])
            elif sys.platform == "win32":
                __import__("os").startfile(str(path))
            else:
                subprocess.Popen(["xdg-open", str(path)])
        except Exception as exc:
            messagebox.showerror("打开任务文件夹失败", str(exc))


def main():
    root = tk.Tk()
    PipelineGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
