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
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, simpledialog, ttk

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


def _match_import_files(paths: list[str | Path]) -> dict:
    """Classify one multi-file import and pair body, MP3, and dictionary by name."""
    unique_paths = list(dict.fromkeys(Path(path).expanduser() for path in paths))
    txt_paths = [path for path in unique_paths if path.suffix.lower() == ".txt"]
    audio_paths = [path for path in unique_paths if path.suffix.lower() == ".mp3"]
    unsupported = [path for path in unique_paths if path not in txt_paths and path not in audio_paths]
    dictionary_paths = [path for path in txt_paths if _is_pronunciation_dictionary_filename(path)]
    text_paths = [path for path in txt_paths if path not in dictionary_paths]

    def by_key(items: list[Path]) -> dict[str, list[Path]]:
        result: dict[str, list[Path]] = {}
        for item in items:
            result.setdefault(_normalized_batch_pair_key(item), []).append(item)
        return result

    audio_by_key = by_key(audio_paths)
    dictionary_by_key = by_key(dictionary_paths)
    pairs = []
    used_audio: set[Path] = set()
    used_dictionary: set[Path] = set()
    ambiguous: list[tuple[Path, str, list[Path]]] = []
    for text_path in text_paths:
        key = _normalized_batch_pair_key(text_path)
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
        self._image_failure_prompted: set[str] = set()
        self._build_style()
        self._build_ui()
        self._poll_jobs()
        self._poll_log()
        self.root.after(350, self._emit_dependency_startup_logs)
        self.root.after(1600, self._maybe_start_dependency_check)
        self.root.after(700, self._maybe_start_hardware_autotune)
        self.root.after(2400, self._maybe_check_update_on_startup)

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

        self._build_source_panel(left)
        self._build_queue_panel(left)
        self._build_right_panel(right)

    def _build_source_panel(self, parent):
        box = ttk.LabelFrame(parent, text="输入 / 导入 / 小说搜索", padding=8)
        box.pack(fill=tk.X, pady=(0, 8))

        search_row = ttk.Frame(box)
        search_row.pack(fill=tk.X, pady=(0, 6))
        ttk.Label(search_row, text="来源").pack(side=tk.LEFT)
        self.source_mode_var = tk.StringVar(value="fanqie")
        ttk.Radiobutton(search_row, text="番茄小说", variable=self.source_mode_var, value="fanqie").pack(side=tk.LEFT, padx=(6, 0))
        ttk.Radiobutton(search_row, text="日本文库", variable=self.source_mode_var, value="japanese").pack(side=tk.LEFT, padx=(4, 8))
        ttk.Label(search_row, text="小说名").pack(side=tk.LEFT)
        self.search_var = tk.StringVar()
        ttk.Entry(search_row, textvariable=self.search_var, width=28).pack(side=tk.LEFT, padx=6)
        self.search_button = ttk.Button(search_row, text="搜索", command=self._search_books)
        self.search_button.pack(side=tk.LEFT)
        ttk.Button(search_row, text="加入选中搜索结果", command=self._add_selected_search).pack(side=tk.LEFT, padx=6)

        result_cols = ("title", "author", "source", "type", "paid", "latest", "ref")
        self.search_tree = ttk.Treeview(box, columns=result_cols, show="headings", height=4)
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
        self.search_tree.pack(fill=tk.X, pady=(0, 8))

        manual_row = ttk.Frame(box)
        manual_row.pack(fill=tk.X)
        ttk.Label(manual_row, text="标题").pack(side=tk.LEFT)
        self.manual_title = tk.StringVar()
        ttk.Entry(manual_row, textvariable=self.manual_title, width=28).pack(side=tk.LEFT, padx=6)
        ttk.Button(manual_row, text="加入粘贴文本", command=self._add_manual_text).pack(side=tk.LEFT)

        import_row = ttk.Frame(box)
        import_row.pack(fill=tk.X, pady=(6, 0))
        ttk.Label(import_row, text="文件导入").pack(side=tk.LEFT)
        ttk.Button(import_row, text="选择文件导入…", command=self._import_files).pack(side=tk.LEFT, padx=6)
        ttk.Label(import_row, text="可同时选择正文 TXT、同名 MP3、同名读音词典；自动配对", foreground="#666").pack(side=tk.LEFT)

        dictionary_row = ttk.Frame(box)
        dictionary_row.pack(fill=tk.X, pady=(6, 0))
        ttk.Label(dictionary_row, text="日语读音词典").pack(side=tk.LEFT)
        self.pronunciation_dictionary_var = tk.StringVar()
        ttk.Entry(
            dictionary_row,
            textvariable=self.pronunciation_dictionary_var,
            width=52,
            state="readonly",
        ).pack(side=tk.LEFT, padx=6)
        ttk.Button(dictionary_row, text="选择词典 TXT", command=self._choose_pronunciation_dictionary).pack(side=tk.LEFT)
        ttk.Button(dictionary_row, text="清除选择", command=self._clear_pronunciation_dictionary).pack(side=tk.LEFT, padx=6)
        ttk.Button(
            dictionary_row,
            text="附加到选中任务",
            command=self._attach_pronunciation_dictionary_to_selected,
        ).pack(side=tk.LEFT)

        ttk.Label(box, text="粘贴正文 可上下滚动").pack(anchor=tk.W, pady=(6, 0))
        self.manual_text = scrolledtext.ScrolledText(box, height=3, wrap=tk.WORD, font=(UI_FONT, UI_FONT_SIZE))
        self.manual_text.pack(fill=tk.X, pady=(2, 0))

    def _build_queue_panel(self, parent):
        box = ttk.LabelFrame(parent, text="任务队列", padding=8)
        box.pack(fill=tk.BOTH, expand=True)

        queue_pane = ttk.PanedWindow(box, orient=tk.VERTICAL)
        queue_pane.pack(fill=tk.BOTH, expand=True)

        toolbar_box = ttk.Frame(queue_pane, padding=4)
        ttk.Button(toolbar_box, text="启动全部待处理", command=self._start_all_pending, style="Primary.TButton").pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(toolbar_box, text="清空已结束任务", command=self._delete_finished_jobs).pack(side=tk.LEFT)

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
            "job_id": 170,
            "stage": 150,
            "progress": 62,
            "worker": 88,
            "audio": 88,
            "dictionary": 90,
            "title": 150,
            "video": 190,
            "youtube": 170,
        }
        for col in cols:
            self.job_tree.heading(col, text=headings[col])
            self.job_tree.column(col, width=widths[col], minwidth=widths[col], stretch=False, anchor=tk.W)
        self.job_tree.grid(row=0, column=0, sticky="nsew")
        tree_vscroll.grid(row=0, column=1, sticky="ns")
        tree_hscroll.grid(row=1, column=0, sticky="ew")
        self.job_tree.bind("<<TreeviewSelect>>", self._on_job_select)
        self.job_tree.bind("<Button-3>", self._show_job_context_menu)
        self.job_tree.bind("<Button-2>", self._show_job_context_menu)
        self.job_tree.bind("<Control-Button-1>", self._show_job_context_menu)
        self.job_context_menu = tk.Menu(self.root, tearoff=False)
        for label, command in (
            ("启动选中", self._start_selected),
            ("仅重试合成", self._compose_selected),
            ("停止选中", self._stop_selected_jobs),
            ("清除图片缓存", self._clear_selected_media_cache),
            ("指定段配音修复", self._retry_tts_segments),
            ("全段配音修复", self._retry_stalled_tts_segments),
        ):
            self.job_context_menu.add_command(label=label, command=command)
        self.job_context_menu.add_separator()
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
        self._config_sections: dict[str, dict] = {}
        self._model_mirrors: dict[str, dict] = {}
        self._secret_widgets: dict[str, dict] = {}

        footer = ttk.Frame(parent)
        footer.pack(side=tk.BOTTOM, fill=tk.X, pady=(6, 0))
        ttk.Button(footer, text="保存到当前方案", style="Primary.TButton", command=self._save_profile).pack(side=tk.RIGHT)

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
            ttk.Entry(line, textvariable=var, width=width).pack(side=tk.LEFT, fill=tk.X, expand=True)
            self.vars[key] = var
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
            "gpt-image-1",
            ["gpt-image-1", "gpt-image-2", "dall-e-3"],
        )
        buttons(
            ("检测统一文本 API", lambda: self._probe_api("unified_llm")),
            ("检测统一图片 API", lambda: self._probe_api("unified_image")),
        )

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
        api_model_combo("图片模型名", "image_model", "图片与提示词", "", ["gpt-image-1", "dall-e-3"])
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
        check("高风险新词自动注音（Edge，调用文本 API）", "tts_auto_pronunciation_enabled")
        row("自动注音最多候选词", "tts_auto_pronunciation_max_terms", "40")

        section("洗稿")
        check("AI 洗稿改写", "ai_rewrite_enabled")
        check("保留章节标题并朗读（第X话）", "narrate_chapter_headings")
        check("洗稿后朗读净化（清除装饰符号）", "tts_clean_rewritten_text")
        row("洗稿批大小", "ai_rewrite_batch_chars", "3500")
        textrow("洗稿提示词", "ai_rewrite_prompt", str(config.get("ai_rewrite_prompt", "")), height=5)

        section("图片与提示词")
        subheading("模型输入", "这些模型也可以在“API 设置与 Key”中填写；保存时会检查两处是否一致。")
        section_model_combo("统一文本模型", "ai_api_text_model", "gpt-4o-mini", ["gpt-4o-mini", "gpt-4o", "deepseek-chat"])
        section_model_combo("统一图片模型", "ai_api_image_model", "gpt-image-1", ["gpt-image-1", "gpt-image-2", "dall-e-3"])
        section_model_combo("分镜模型", "llm_model", "gpt-4o-mini", ["gpt-4o-mini", "gpt-4o", "deepseek-chat"])
        section_model_combo("图片模型名", "image_model", "", ["gpt-image-1", "dall-e-3"])
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
        section_model_combo("封面模型名", "cover_model", "", ["gpt-image-1", "dall-e-3"])
        row("封面宽", "cover_width", "1280")
        row("封面高", "cover_height", "720")
        textrow("封面固定模板", "cover_prompt_template", str(config.get("cover_prompt_template", "")), height=5)
        textrow("封面自定义提示词", "cover_custom_prompt", str(config.get("cover_custom_prompt", "")), height=3)
        textrow("封面 AI 分析提示词", "cover_ai_analysis_prompt", str(config.get("cover_ai_analysis_prompt", "")), height=4)

        section("画面节奏 / 视频")
        combo("配图模式", "pacing_mode", ["by_duration", "by_sentence", "by_paragraph", "fixed_count"], "by_duration")
        row("每张图秒数", "pacing_seconds_per_image", "6")
        row("固定图片数", "pacing_fixed_count", "10")
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
        check("保护人物构图（保留移动、减小裁切，推荐）", "video_safe_framing")
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
        ttk.Label(
            section_parent,
            text="选择后，每个任务完成时都会按任务名创建文件夹，内含成品 MP4、封面和标题.txt；不填则只保存在任务目录。关闭自动上传也会正常导出。",
            foreground="#666",
            wraplength=520,
        ).pack(anchor=tk.W, pady=(0, 4))

        section("批量与上传")
        check("自动短说明标题", "short_title_enabled")
        row("短标题最少字", "short_title_min_chars", "20")
        row("短标题最多字", "short_title_max_chars", "30")
        textrow("短标题提示词", "short_title_prompt", str(config.get("short_title_prompt", "")), height=3)
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
        row("上传标题模板", "youtube_title_template", "💥【爆款短文】《{clean_title}》{intro} #小说 #推文")
        row("上传标题字数", "youtube_title_max_chars", "100")
        textrow("说明模板", "youtube_description", "", height=4)
        row("固定 Tags", "youtube_tags", "")
        row("上传政策", "browser_upload_policy", "BTRA")
        row("广告间隔秒", "browser_ad_interval", "60")
        row("广告起始秒", "browser_ad_start", "0")
        textrow("广告分级模板(JSON)", "browser_ad_suitability_template", str(config.get("browser_ad_suitability_template", "")), height=3)

        ttk.Label(
            section_parent,
            text="上传标题可用：{clean_title}=去作者名书名，{title}=原标题，{short_title}=短说明标题。simple=无创收精简流程，full=完整创收/广告/分级流程。",
            foreground="#666",
            wraplength=520,
        ).pack(anchor=tk.W, pady=(4, 14))

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
            "short_title_min_chars", "short_title_max_chars", "youtube_title_max_chars",
        }
        float_keys = {
            "tts_volume", "tts_waveform_min_rms_db", "tts_waveform_max_silence_ratio",
            "pacing_seconds_per_image", "video_motion_cycle_seconds",
            "video_transition_duration", "cover_title_area_ratio",
            "video_subtitle_outline", "video_subtitle_shadow", "video_subtitle_spacing",
        }
        bool_keys = {
            "video_long_mode", "video_safe_framing", "ken_burns", "video_subtitle", "video_external_subtitle",
            "upload_enabled", "cover_enabled", "video_subtitle_bold", "video_subtitle_italic",
            "short_title_enabled", "pipeline_overlap_tts_images", "ai_rewrite_enabled",
            "narrate_chapter_headings",
            "character_analysis_enabled", "character_analysis_always_include_protagonists",
            "character_reference_enabled", "scene_inject_visual_theme", "scene_inject_character_triggers",
            "storyboard_highlight_enabled", "storyboard_highlight_align_timeline",
            "scene_reference_enabled", "ai_api_enabled",
            "tts_retry_until_success", "tts_waveform_validation", "tts_subprocess_isolation",
            "tts_clean_rewritten_text", "tts_auto_pronunciation_enabled",
            "hardware_autotune_enabled",
            "dependency_check_on_startup", "dependency_auto_install_python",
            "dependency_auto_install_ffmpeg", "dependency_auto_install_browser",
            "update_check_on_startup", "browser_upload_all_profiles",
        }
        return int_keys, float_keys, bool_keys

    def _apply_config_form(self, save_profile: bool = True) -> str:
        self._resolve_model_mirror_conflicts()
        int_keys, float_keys, bool_keys = self._config_key_sets()
        for key, var in self.vars.items():
            value = var.get()
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
            else:
                var.set(str(value))
        for key, mirror in getattr(self, "_model_mirrors", {}).items():
            section_var = mirror.get("section_var")
            if section_var is not None:
                section_var.set(str(config.get(key, "")))

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

        form = ttk.Frame(right)
        form.pack(fill=tk.BOTH, expand=True)

        def row(label: str):
            line = ttk.Frame(form)
            line.pack(fill=tk.X, pady=3)
            ttk.Label(line, text=label, width=16, anchor=tk.E).pack(side=tk.LEFT)
            return line

        name_var = tk.StringVar()
        enabled_var = tk.StringVar(value="开启")
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
        ttk.Combobox(
            line,
            textvariable=visibility_var,
            values=list(UPLOAD_VISIBILITY_VALUES.keys()),
            state="readonly",
            width=22,
        ).pack(side=tk.LEFT, padx=(6, 0))

        title_var = tk.StringVar()
        line = row("标题模板")
        ttk.Entry(line, textvariable=title_var).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(6, 0))

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
            text="标题模板可用：{clean_title}、{title}、{short_title}、{intro}、{author}、{tags}、{job_id}。广告分级模板可填 JSON；留空则走全局模板。",
            foreground="#666",
            wraplength=640,
        )
        hint.pack(anchor=tk.W, pady=(5, 0), padx=(120, 0))

        form_btns = ttk.Frame(right)
        form_btns.pack(fill=tk.X, pady=(8, 0))

        def set_status(text: str, color: str = "#555"):
            status.configure(text=text, foreground=color)

        def profile_line(profile: dict) -> str:
            enabled = "启用" if profile.get("enabled", True) else "停用"
            flow = "精简" if profile.get("flow") == "simple" else "完整"
            return f"{enabled} | {flow} | {profile.get('chrome_profile', 'Default')} | {profile.get('name', '')}"

        def refresh_flow_ui(*_args):
            full = flow_var.get() == "full"
            state = tk.NORMAL if full else tk.DISABLED
            for widget in (policy_entry, ad_interval_entry, ad_start_entry):
                widget.configure(state=state)
            ad_text.configure(state=state)
            policy_tip.configure(foreground="#777" if full else "#aaa")

        flow_var.trace_add("write", refresh_flow_ui)

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
            desc_text.delete("1.0", tk.END)
            desc_text.insert("1.0", str(profile.get("description") or ""))
            policy_var.set(str(profile.get("upload_policy") or "BTRA"))
            ad_interval_var.set(str(profile.get("ad_interval") or 60))
            ad_start_var.set(str(profile.get("ad_start") or 0))
            ad_text.configure(state=tk.NORMAL)
            ad_text.delete("1.0", tk.END)
            ad_text.insert("1.0", _upload_ad_template_to_text(profile.get("ad_suitability_template") or ""))
            refresh_flow_ui()

        def flush(idx: int) -> bool:
            if idx < 0 or idx >= len(profiles):
                return True
            try:
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
                        "description": desc_text.get("1.0", tk.END).strip(),
                        "ad_suitability_template": _upload_ad_template_from_text(ad_text.get("1.0", tk.END)),
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
            widget = self.combo_widgets.get("browser_active_profile")
            if widget is not None:
                widget.configure(values=names)
            return True

        def save_and_close():
            if not write_profiles_to_form():
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
        ttk.Button(form_btns, text="保存频道方案", style="Primary.TButton", command=save_and_close).pack(side=tk.RIGHT)
        ttk.Button(form_btns, text="关闭", command=win.destroy).pack(side=tk.RIGHT, padx=(0, 6))

        profile_list.bind("<<ListboxSelect>>", on_select)
        rebuild(0)
        fill(0)
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

    def _import_files(self):
        """Import all related files in one selection, pairing them by filename."""
        selected = filedialog.askopenfilenames(
            title="选择正文 TXT、可选同名 MP3 和可选同名读音词典",
            filetypes=[("Supported files", ("*.txt", "*.mp3")), ("All files", "*.*")],
        )
        if not selected:
            return
        matched = _match_import_files(list(selected))
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
                    pr.inspect_pronunciation_dictionary(dictionary_path)
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
                pr.inspect_pronunciation_dictionary(dictionary_path)
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
            info = pr.inspect_pronunciation_dictionary(path)
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
                result = pr.attach_pronunciation_dictionary(job_id, path)
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
        output_basename = pr.safe_job_name(title, fallback="article")
        job_id = pr.new_named_job_id(output_basename)
        job_dir = pr.job_dir_for(job_id)
        pr.write_status(
            job_dir,
            job_id=job_id,
            title=title,
            input=input_text,
            output_basename=output_basename,
            stage="queued",
            progress=0.0,
        )
        pr.append_log(job_dir, f"queued: {title}")
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
                pr.attach_pronunciation_dictionary(job_id, selected_dictionary)
            except Exception as exc:
                messagebox.showerror("附加读音词典失败", f"任务已加入队列，但词典附加失败：\n{exc}")
        self._refresh_jobs()
        return job_id

    def _selected_job_ids(self) -> list[str]:
        ids = []
        for iid in self.job_tree.selection():
            values = self.job_tree.item(iid, "values")
            if values:
                ids.append(str(values[0]))
        return ids

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
        for job_id in self._selected_job_ids():
            self._start_job(job_id)

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

    def _start_job(self, job_id: str):
        st = pr.load_status(job_id)
        if st.get("worker_alive"):
            return
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
                dictionary_info = pr.inspect_pronunciation_dictionary(dictionary_path)
                attached_hash = str(st.get("pronunciation_dictionary_hash") or "")
                internal_dictionary = pr.job_dir_for(job_id) / pr.TTS_PRONUNCIATION_DICTIONARY
                if attached_hash != str(dictionary_info["hash"]) or not internal_dictionary.exists():
                    pr.attach_pronunciation_dictionary(job_id, dictionary_path)
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
        inserted_ids: set[str] = set()
        for row in pr.list_jobs(limit=500):
            st = pr.load_status(row["job_id"])
            worker = f"PID {st.get('worker_pid')}" if st.get("worker_alive") else ""
            values = (
                row["job_id"],
                row.get("stage_display", row.get("stage", "")),
                f"{float(row.get('progress') or 0) * 100:.0f}%",
                worker,
                "导入 MP3" if st.get("audio_mode") == "imported" else "TTS",
                f"已附加 {int(st.get('pronunciation_dictionary_entries') or 0)}条" if st.get("pronunciation_dictionary") else "未附加",
                row.get("title", ""),
                row.get("video", ""),
                st.get("youtube_url", ""),
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

    def _poll_jobs(self):
        self._refresh_jobs()
        self._prompt_image_failure_choices()
        self.root.after(2500, self._poll_jobs)

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
