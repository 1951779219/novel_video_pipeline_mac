"""配置管理：所有可配置项均存 data/settings.json，UI 上可改。"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from app.utils.secrets import clean_api_key

ROOT = Path(__file__).parent.parent
DATA_DIR = ROOT / "data"
JOBS_DIR = DATA_DIR / "jobs"
SETTINGS_FILE = DATA_DIR / "settings.json"
PROFILES_DIR = DATA_DIR / "profiles"
DEFAULT_PROFILE_NAMES = ("配置1", "配置2")

DATA_DIR.mkdir(exist_ok=True)
JOBS_DIR.mkdir(exist_ok=True)
PROFILES_DIR.mkdir(exist_ok=True)


SETTINGS_SCHEMA_VERSION = 18
DEFAULT_YOUTUBE_TITLE_TEMPLATE = "💥【爆款短文】《{clean_title}》{intro} #小说 #推文"
DEFAULT_YOUTUBE_DESCRIPTION = (
    "原创内容，只只推文，每天更新高分又好看的#AI漫画#小说推文\n"
    "💖感谢您的订阅👉小伙伴想看完整版，欢迎催更"
)

API_KEY_FIELDS = {
    "ai_api_key",
    "llm_api_key",
    "tts_api_key",
    "image_api_key",
    "cover_api_key",
    "character_reference_api_key",
    "scene_reference_api_key",
}


DEFAULT_SETTINGS: dict[str, Any] = {
    "settings_schema_version": SETTINGS_SCHEMA_VERSION,
    "active_profile": "配置1",

    # 爬取
    "scraper_site": "qingtian",
    "scraper_max_chars": 0,
    "scraper_chapter_limit": 0,
    "source_base_url": "https://v1.gyks.cf",
    "source_platform": "番茄",
    "source_media": "小说",
    "source_hosts": (
        "https://v1.gyks.cf\n"
        "https://v2.gyks.cf\n"
        "https://v3.gyks.cf\n"
        "https://v4.gyks.cf\n"
        "https://v5.gyks.cf\n"
        "http://219.154.201.122:5006\n"
        "https://api.langge.cf"
    ),
    "source_delay": 0.15,
    "legado_source_ref": "",

    # 统一 AI 接口（OpenAI 兼容）：文本和生图默认共用这里的 Base URL / API Key
    "ai_api_enabled": True,
    "ai_api_base_url": "",
    "ai_api_key": "",
    "ai_api_text_model": "gpt-4o-mini",
    "ai_api_image_model": "gpt-image-1",

    # LLM（分镜）
    "llm_provider": "openai",  # openai / claude / gemini / deepseek / ollama / custom
    "llm_base_url": "https://api.openai.com/v1",
    "llm_api_key": "",
    "llm_model": "gpt-4o-mini",
    "llm_storyboard_prompt": (
        "You are a visual prompt director for long-form novel recap / tweet-style video images.\n"
        "Convert each novel paragraph into ONE finished English image-generation prompt. "
        "Use the supplied story visual theme lock and character consistency lock when present, while still making the scene "
        "match the current paragraph. The prompt must include: main event with an action verb, protagonist or relationship, "
        "conflict/tension, camera language, lighting, mood, palette, and one symbolic story detail. Avoid static landscape shots; "
        "make every image feel like a story moment from the same production. Use adult-looking characters, cinematic anime/editorial "
        "illustration style, and policy-safe symbolic drama. No readable text, UI, logos, watermarks, gore, nudity, sexual content, "
        "or explicit violence. Output ONLY the prompt, no quotes, no numbering."
    ),
    "llm_storyboard_user_template": (
        "Task: turn this novel excerpt into a single narrative storyboard image prompt for a recap video.\n"
        "Common visual prefix that must be preserved: {prefix}\n"
        "Style suffix: {style}\n"
        "Scene index: {index}\n\n"
        "Extract these visual decisions before writing the prompt: main event/action, main character, conflict hook, "
        "camera angle, lighting, mood, palette, and one strong symbolic detail. If a character/theme lock is supplied later "
        "in the pipeline, keep it visually compatible and do not contradict it.\n\n"
        "Novel excerpt:\n{text}\n\n"
        "Return one English image prompt only, <=90 words. No explanation, no labels, no JSON."
    ),
    "llm_image_prompt_prefix": (
        "Single finished cinematic anime/editorial illustration for a novel recap video, one clear hero subject, "
        "visible story action, strong visual hierarchy, expressive adult-looking characters, dramatic lighting, "
        "restrained 4-6 color palette, clean composition, no readable text."
    ),
    "llm_image_style_suffix": "anime style, detailed, cinematic lighting, masterpiece",
    "llm_negative_prompt": "lowres, bad anatomy, bad hands, text, error, missing fingers, jpeg artifacts, watermark, logo, words, blood, gore, corpse, severed limbs, nudity, sexual content, explicit violence, weapon close-up",

    # 视频生图解决方案1：把秒数作为图片数量预算，每个预算窗口只选择一个旁白高光点。
    "storyboard_highlight_enabled": True,
    "storyboard_highlight_context_max_chars": 18000,
    "storyboard_highlight_max_segments": 3,
    "storyboard_highlight_align_timeline": True,

    # Character / story visual consistency
    "character_analysis_enabled": True,
    "character_analysis_max_chars": 12000,
    "character_analysis_max_tokens": 1800,
    "character_analysis_max_characters_per_prompt": 4,
    "character_analysis_always_include_protagonists": True,
    "character_analysis_prompt": (
        "你是小说推文视频的剧情解析和人设导演。\n"
        "任务：阅读导入文章，自动区分主角、重要配角、路人，并为后续生图锁定固定人设。\n\n"
        "必须只输出 JSON，不要 Markdown，不要解释。\n"
        "如果原文没有明确外观，请合理补全一次，并在后续保持一致；不要写“可能”“推测”“未提及”。\n"
        "人物外观必须适合全年龄推文画面，避免血腥、裸露、色情、儿童危险、真实名人、商标水印。\n"
        "主角和重要配角必须有稳定的 hair / outfit / visual_prompt_en，后续每张图都会复用。\n\n"
        "输出格式：\n"
        "{\n"
        '  "plot_summary": "100字以内剧情梗概",\n'
        '  "story_conflict": "核心冲突",\n'
        '  "visual_theme": {\n'
        '    "genre": "wuxia|fantasy|anime|urban|historical|sci_fi|suspense|romance|other",\n'
        '    "theme_name_zh": "本次任务统一画面类型，例如都市悬疑、古风武侠、日漫校园、魔幻史诗",\n'
        '    "style_prompt_en": "English global style prompt for all character reference images and scene images",\n'
        '    "background_prompt_en": "English recurring world/background prompt for this story",\n'
        '    "negative_prompt_en": "English visual negative constraints"\n'
        "  },\n"
        '  "protagonists": ["主角姓名"],\n'
        '  "supporting_characters": ["重要配角姓名"],\n'
        '  "characters": [\n'
        "    {\n"
        '      "name": "人物姓名或身份名",\n'
        '      "trigger": "char_unique_ascii_trigger",\n'
        '      "aliases": ["别名或称呼"],\n'
        '      "importance": "protagonist|supporting|minor",\n'
        '      "gender": "male|female|unknown",\n'
        '      "age_group": "child|young_adult|adult|middle_aged|elderly|unknown",\n'
        '      "role_in_story": "人物在剧情中的功能",\n'
        '      "personality": "性格关键词",\n'
        '      "visual_profile_zh": "一句中文固定外观：性别，年龄段，发色发型，服装颜色和款式，气质",\n'
        '      "visual_prompt_en": "English stable visual prompt, including hair, outfit, age impression, and vibe",\n'
        '      "reference_prompt_en": "English prompt for generating a clean full-body character reference sheet on a simple themed background",\n'
        '      "lock_rules_zh": "后续生图必须保持不变的外观规则"\n'
        "    }\n"
        "  ],\n"
        '  "visual_rules": ["全片统一画风和连续性要求"]\n'
        "}"
    ),
    "character_reference_enabled": False,
    "character_reference_provider": "same_as_image",
    "character_reference_base_url": "",
    "character_reference_api_key": "",
    "character_reference_model": "",
    "character_reference_workflow": "",
    "character_reference_width": 768,
    "character_reference_height": 1024,
    "character_reference_max_count": 6,
    "character_reference_prompt_suffix": "",
    "scene_inject_visual_theme": True,
    "scene_inject_character_triggers": True,
    "scene_reference_enabled": False,
    "scene_reference_provider": "same_as_image",
    "scene_reference_base_url": "",
    "scene_reference_api_key": "",
    "scene_reference_model": "",
    "scene_reference_workflow": "",
    "scene_reference_max_images": 2,

    # Short marketing title
    "short_title_enabled": True,
    "short_title_min_chars": 20,
    "short_title_max_chars": 30,
    "short_title_prompt": (
        "请根据小说简介生成一个适合推文视频的短说明标题。"
        "要求 20 到 30 个中文字符，突出爽点/冲突/悬念，不要书名号，不要引号，不要换行，不要标签。"
    ),
    "ai_rewrite_enabled": False,
    "ai_rewrite_batch_chars": 3500,
    "ai_rewrite_prompt": (
        "你是小说推文视频的洗稿改写编辑。"
        "请把输入的小说正文改写成适合中文/日文推文长视频旁白的版本："
        "保留原剧情、人物关系、事件顺序、情绪转折和关键设定，不新增剧情，不改变结局；"
        "去掉生硬网页痕迹、重复废话、作者口癖和不适合朗读的表达；"
        "语言更顺、更有悬念和画面感，适合 TTS 朗读；"
        "保持段落分隔，不要输出标题、解释、编号、标签或 JSON，只输出改写后的正文。"
    ),
    # TTS
    "tts_provider": "edge",  # edge / voicevox / azure / openai / elevenlabs / custom
    "tts_voice": "ja-JP-NanamiNeural",
    "tts_rate": "+0%",
    "tts_volume": 1.0,
    "tts_model": "tts-1",
    "tts_api_key": "",
    "tts_base_url": "",  # voicevox: http://localhost:50021 ; openai: https://api.openai.com/v1 ; etc
    "tts_extra": {},  # 额外参数 (azure region 等)
    "tts_retries": 3,
    "tts_retry_until_success": True,
    "tts_fail_silence": False,
    "tts_emotion": "",
    "tts_segment_timeout_seconds": 180,
    "tts_stall_fallback_seconds": 240,
    "tts_heartbeat_seconds": 30,
    "tts_subprocess_isolation": True,
    "tts_waveform_validation": True,
    "tts_waveform_min_rms_db": -55.0,
    "tts_waveform_max_silence_ratio": 0.92,

    # Image
    "image_provider": "comfyui",  # comfyui / sdwebui / openai / replicate / aliyun / custom
    "image_base_url": "http://127.0.0.1:8188",
    "image_api_key": "",
    "image_model": "",
    "image_width": 1920,
    "image_height": 1080,
    "image_steps": 25,
    "image_cfg": 7.0,
    "image_api_timeout_seconds": 300,
    "image_workflow": "",  # comfyui workflow json 路径（可选）

    # Cover
    "cover_enabled": True,
    "cover_provider": "same_as_image",
    "cover_base_url": "",
    "cover_api_key": "",
    "cover_model": "",
    "cover_width": 1280,
    "cover_height": 720,
    "cover_title_size": 72,
    "cover_title_font": "Microsoft YaHei",
    "cover_title_color": "#FFFFFF",
    "cover_title_bg": "#050505",
    "cover_title_area_ratio": 0.24,
    "cover_prompt_template": (
        'Create ONE finished premium YouTube novel-recap cover, like a conceptual typography movie poster.\n'
        'Exact title text: "{title}"\n'
        'The exact title must be the main hero element: huge, readable, spelled exactly, not translated, not shortened, '
        'not replaced, and integrated into the artwork.\n'
        'Story excerpt: {excerpt}\n'
        'Custom direction: {custom}\n'
        'Composition: one strong hero visual; the character, object, or scene should interact with the title typography '
        'by overlapping it, emerging from it, being framed by it, casting shadow on it, or breaking through it. '
        'Use a restrained 4-6 color palette, high contrast, cinematic lighting, and clear thumbnail hierarchy. '
        'Single cover only: no moodboard, no grid, no process sheet, no mockup, no extra captions, no UI, no watermark. {style}'
    ),
    "cover_custom_prompt": (
        "viral mystery/romance/fantasy novel recap thumbnail, dramatic character close-up, symbolic story object, "
        "red-gold accent on dark cinematic background, high click-through tension"
    ),
    "cover_ai_analysis_prompt": (
        "You are a senior poster art director. Transform the fixed cover template, exact title, and story excerpt into "
        "one concise English image-generation prompt. Preserve the exact title text and make typography the visual hero. "
        "Silently interpret the title's mood, genre, symbolic associations, and psychological tension, then turn it into "
        "one strong visual metaphor. The output must describe a single finished cover, not a moodboard or design sheet. "
        "Do not request editor-added overlays or post-production text. No extra words, logos, captions, UI, watermarks, "
        "gore, nudity, sexual content, or explicit violence."
    ),

    # 图音配比
    "pacing_mode": "by_duration",  # by_duration / by_sentence / by_paragraph / fixed_count
    "pacing_seconds_per_image": 6,
    "pacing_sentences_per_image": 3,
    "pacing_fixed_count": 10,
    "ken_burns": True,
    "video_motion": "上下移动",
    "video_motion_curve": "ease",
    "video_motion_cycle_seconds": 0.0,
    "video_transition": "none",
    "video_transition_duration": 0.4,

    # 视频
    "video_width": 1920,
    "video_height": 1080,
    "video_fps": 30,
    "video_encoder": "libx264",
    "video_encoder_preset": "veryfast",
    "video_encoder_quality": 20,
    "video_subtitle": True,
    "video_external_subtitle": True,
    "video_subtitle_font": "Microsoft YaHei",
    "video_subtitle_size": 48,
    "video_subtitle_position": "下边",
    "video_subtitle_color": "#FFFFFF",
    "video_subtitle_outline_color": "#000000",
    "video_subtitle_back_color": "#000000",
    "video_subtitle_outline": 4.0,
    "video_subtitle_shadow": 1.0,
    "video_subtitle_margin_v": 60,
    "video_subtitle_margin_lr": 56,
    "video_subtitle_spacing": 0.0,
    "video_subtitle_bold": False,
    "video_subtitle_italic": False,
    "video_subtitle_chars_per_line": 24,
    "video_subtitle_max_lines": 2,
    "video_bgm_path": "",
    "video_bgm_volume": 0.15,
    "video_long_mode": True,
    "video_cleanup_temp": True,

    "max_concurrent_jobs": 1,
    "max_concurrent_external_api": 2,
    "max_concurrent_ffmpeg": 1,
    "max_concurrent_media_probe": 2,
    "max_parallel_tts": 2,
    "max_parallel_images": 2,
    "max_parallel_video_clips": 1,
    "pipeline_overlap_tts_images": True,
    "resource_wait_timeout": 0,
    "worker_detached": True,
    "hardware_autotune_enabled": True,
    "hardware_autotune_done": False,
    "hardware_autotune_at": "",
    "hardware_autotune_signature": "",
    "hardware_autotune_summary": "",
    "dependency_check_on_startup": True,
    "dependency_auto_install_python": True,
    "dependency_auto_install_ffmpeg": True,
    "dependency_auto_install_browser": True,
    "dependency_pip_index_url": "https://pypi.tuna.tsinghua.edu.cn/simple",
    "dependency_pip_extra_args": "",
    "dependency_pip_timeout_seconds": 1800,
    "dependency_ffmpeg_url": "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip",
    "dependency_last_report": "",
    "update_manifest_url": "https://github.com/1951779219/novel_video_pipeline_mac_release/releases/latest/download/latest.json",
    "update_check_on_startup": True,
    "feedback_issue_url": "https://github.com/1951779219/novel_video_pipeline_feedback/issues/new/choose",

    # 上传
    "upload_enabled": False,
    "youtube_upload_method": "browser",
    "youtube_visibility": "PRIVATE",
    "youtube_title_template": DEFAULT_YOUTUBE_TITLE_TEMPLATE,
    "youtube_title_max_chars": 100,
    "youtube_description": DEFAULT_YOUTUBE_DESCRIPTION,
    "youtube_tags": "",
    "browser_flow": "simple",
    "browser_upload_policy": "BTRA",
    "browser_ad_interval": 60,
    "browser_ad_start": 0,
    "browser_chrome_profile": "Default",
    "browser_upload_all_profiles": False,
    "browser_ad_suitability_template": "",
    "browser_auto_restart": True,
    "browser_stall_timeout_min": 10,
    "browser_op_speed": "normal",
    "upload_dependency_check_before_upload": True,
    "browser_profiles": json.dumps(
        [
            {
                "name": "无创收精简流程",
                "enabled": True,
                "flow": "simple",
                "upload_policy": "BTRA",
                "ad_interval": 60,
                "ad_start": 0,
                "visibility": "PUBLIC",
                "chrome_profile": "Default",
                "title_template": "",
                "description": "",
                "ad_suitability_template": "",
            },
            {
                "name": "完整创收流程",
                "enabled": True,
                "flow": "full",
                "upload_policy": "BTRA",
                "ad_interval": 60,
                "ad_start": 0,
                "visibility": "PUBLIC",
                "chrome_profile": "Default",
                "title_template": "",
                "description": "",
                "ad_suitability_template": "",
            },
        ],
        ensure_ascii=False,
    ),
    "browser_active_profile": "无创收精简流程",
}

if sys.platform == "darwin":
    DEFAULT_SETTINGS.update(
        {
            "cover_title_font": "PingFang SC",
            "video_subtitle_font": "Hiragino Mincho ProN",
            "dependency_ffmpeg_url": "",
        }
    )


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except Exception:
        return default


def _looks_like_legacy_title_template(value: Any) -> bool:
    text = str(value or "")
    return (
        not text.strip()
        or "#爽文" in text
        or "#一口气看完" in text
        or "#灏忚" in text
        or "{short_title}" in text
    )


def _looks_like_legacy_description(value: Any) -> bool:
    text = str(value or "")
    return (
        not text.strip()
        or "{short_title}" in text
        or "{intro}" in text
        or "#爽文" in text
        or "#灏忚" in text
        or "涔﹀悕" in text
    )


def _apply_compat_migrations(data: dict[str, Any], saved: dict[str, Any] | None = None) -> None:
    saved = saved or {}
    saved_version = _to_int(saved.get("settings_schema_version"), 0)
    if saved_version < SETTINGS_SCHEMA_VERSION:
        if _to_int(data.get("scraper_max_chars"), 0) == 8000:
            data["scraper_max_chars"] = 0
        if _looks_like_legacy_title_template(data.get("youtube_title_template")) or "{title}" in str(data.get("youtube_title_template") or ""):
            data["youtube_title_template"] = DEFAULT_YOUTUBE_TITLE_TEMPLATE
        if _looks_like_legacy_description(data.get("youtube_description")):
            data["youtube_description"] = DEFAULT_YOUTUBE_DESCRIPTION
        if _to_int(data.get("image_width"), 0) == 1080 and _to_int(data.get("image_height"), 0) == 1920:
            data["image_width"] = 1920
            data["image_height"] = 1080
        if _to_int(data.get("video_width"), 0) == 1080 and _to_int(data.get("video_height"), 0) == 1920:
            data["video_width"] = 1920
            data["video_height"] = 1080
        if "Leave a clean darker lower area" in str(data.get("cover_prompt_template") or ""):
            data["cover_prompt_template"] = DEFAULT_SETTINGS["cover_prompt_template"]
        if _to_int(data.get("max_concurrent_jobs"), 0) == 2:
            data["max_concurrent_jobs"] = 1
    if saved_version < 6:
        if _to_int(data.get("image_api_timeout_seconds"), 0) == 900:
            data["image_api_timeout_seconds"] = 300
    if saved_version < 7 and sys.platform == "darwin":
        for key in ("cover_title_font", "video_subtitle_font"):
            if str(data.get(key) or "").strip() in {"Microsoft YaHei", "Microsoft YaHei UI", "SimHei", "msyh.ttc"}:
                data[key] = "PingFang SC"
        if "gyan.dev/ffmpeg" in str(data.get("dependency_ffmpeg_url") or ""):
            data["dependency_ffmpeg_url"] = ""
    if saved_version < 8:
        cover_prompt = str(data.get("cover_prompt_template") or "")
        if (
            "Do not render any words" in cover_prompt
            or "title text inside the artwork" in cover_prompt
            or "title overlay" in cover_prompt
            or "editor-added title" in cover_prompt
        ):
            data["cover_prompt_template"] = DEFAULT_SETTINGS["cover_prompt_template"]
    if saved_version < 9:
        for legacy_key in ("ai_text_clean_enabled", "ai_text_clean_batch_chars", "ai_text_clean_prompt"):
            data.pop(legacy_key, None)
    if saved_version < 10:
        if not str(data.get("llm_image_prompt_prefix") or "").strip():
            data["llm_image_prompt_prefix"] = DEFAULT_SETTINGS["llm_image_prompt_prefix"]
        if not str(data.get("llm_storyboard_user_template") or "").strip():
            data["llm_storyboard_user_template"] = DEFAULT_SETTINGS["llm_storyboard_user_template"]
        if not str(data.get("cover_custom_prompt") or "").strip():
            data["cover_custom_prompt"] = DEFAULT_SETTINGS["cover_custom_prompt"]
        if not str(data.get("cover_ai_analysis_prompt") or "").strip():
            data["cover_ai_analysis_prompt"] = DEFAULT_SETTINGS["cover_ai_analysis_prompt"]
    if saved_version < 11:
        if "converting novel paragraphs into safe image-generation prompts" in str(data.get("llm_storyboard_prompt") or ""):
            data["llm_storyboard_prompt"] = DEFAULT_SETTINGS["llm_storyboard_prompt"]
        if "Common visual prefix:" in str(data.get("llm_storyboard_user_template") or ""):
            data["llm_storyboard_user_template"] = DEFAULT_SETTINGS["llm_storyboard_user_template"]
        if "Cinematic anime illustration for a novel recap scene" in str(data.get("llm_image_prompt_prefix") or ""):
            data["llm_image_prompt_prefix"] = DEFAULT_SETTINGS["llm_image_prompt_prefix"]
        if "Create a cinematic YouTube cover illustration" in str(data.get("cover_prompt_template") or ""):
            data["cover_prompt_template"] = DEFAULT_SETTINGS["cover_prompt_template"]
        if "dramatic composition, strong contrast" in str(data.get("cover_custom_prompt") or ""):
            data["cover_custom_prompt"] = DEFAULT_SETTINGS["cover_custom_prompt"]
        if "You write one concise English image-generation prompt" in str(data.get("cover_ai_analysis_prompt") or ""):
            data["cover_ai_analysis_prompt"] = DEFAULT_SETTINGS["cover_ai_analysis_prompt"]
    if saved_version < 12:
        data.setdefault("tts_stall_fallback_seconds", DEFAULT_SETTINGS["tts_stall_fallback_seconds"])
        data.setdefault("tts_subprocess_isolation", DEFAULT_SETTINGS["tts_subprocess_isolation"])
    if saved_version < 13:
        data["tts_fail_silence"] = False
        data.setdefault("tts_retry_until_success", DEFAULT_SETTINGS["tts_retry_until_success"])
        data.setdefault("tts_waveform_validation", DEFAULT_SETTINGS["tts_waveform_validation"])
        data.setdefault("tts_waveform_min_rms_db", DEFAULT_SETTINGS["tts_waveform_min_rms_db"])
        data.setdefault("tts_waveform_max_silence_ratio", DEFAULT_SETTINGS["tts_waveform_max_silence_ratio"])
    if saved_version < 14:
        if "character consistency lock" not in str(data.get("llm_storyboard_prompt") or ""):
            data["llm_storyboard_prompt"] = DEFAULT_SETTINGS["llm_storyboard_prompt"]
        if "character/theme lock" not in str(data.get("llm_storyboard_user_template") or ""):
            data["llm_storyboard_user_template"] = DEFAULT_SETTINGS["llm_storyboard_user_template"]
        for key in (
            "character_analysis_enabled",
            "character_analysis_max_chars",
            "character_analysis_max_tokens",
            "character_analysis_max_characters_per_prompt",
            "character_analysis_always_include_protagonists",
            "character_reference_enabled",
            "character_reference_provider",
            "character_reference_base_url",
            "character_reference_api_key",
            "character_reference_model",
            "character_reference_workflow",
            "character_reference_width",
            "character_reference_height",
            "character_reference_max_count",
            "character_reference_prompt_suffix",
            "scene_inject_visual_theme",
            "scene_inject_character_triggers",
            "scene_reference_enabled",
            "scene_reference_provider",
            "scene_reference_base_url",
            "scene_reference_api_key",
            "scene_reference_model",
            "scene_reference_workflow",
            "scene_reference_max_images",
        ):
            data.setdefault(key, DEFAULT_SETTINGS[key])
        if not str(data.get("character_analysis_prompt") or "").strip():
            data["character_analysis_prompt"] = DEFAULT_SETTINGS["character_analysis_prompt"]
    if saved_version < 15:
        data.setdefault("browser_chrome_profile", DEFAULT_SETTINGS["browser_chrome_profile"])
        data.setdefault("browser_upload_all_profiles", DEFAULT_SETTINGS["browser_upload_all_profiles"])
        data.setdefault("browser_ad_suitability_template", DEFAULT_SETTINGS["browser_ad_suitability_template"])
        try:
            profiles = json.loads(str(data.get("browser_profiles", "[]") or "[]"))
            if isinstance(profiles, list):
                changed = False
                for profile in profiles:
                    if not isinstance(profile, dict):
                        continue
                    if "enabled" not in profile:
                        profile["enabled"] = True
                        changed = True
                    if "chrome_profile" not in profile:
                        profile["chrome_profile"] = data.get("browser_chrome_profile", "Default") or "Default"
                        changed = True
                    if "ad_suitability_template" not in profile:
                        profile["ad_suitability_template"] = ""
                        changed = True
                if changed:
                    data["browser_profiles"] = json.dumps(profiles, ensure_ascii=False)
        except Exception:
            pass
    if saved_version < 16:
        for key in API_KEY_FIELDS:
            if key in data:
                data[key] = clean_api_key(data.get(key))
    if saved_version < 17:
        data.setdefault("ai_api_enabled", DEFAULT_SETTINGS["ai_api_enabled"])
        data.setdefault("ai_api_base_url", str(data.get("llm_base_url") or DEFAULT_SETTINGS["ai_api_base_url"]))
        data.setdefault("ai_api_key", clean_api_key(data.get("llm_api_key") or data.get("image_api_key") or ""))
        data.setdefault("ai_api_text_model", str(data.get("llm_model") or DEFAULT_SETTINGS["ai_api_text_model"]))
        data.setdefault("ai_api_image_model", str(data.get("image_model") or data.get("cover_model") or DEFAULT_SETTINGS["ai_api_image_model"]))
        for key in API_KEY_FIELDS:
            if key in data:
                data[key] = clean_api_key(data.get(key))
    if saved_version < 18:
        for key in (
            "storyboard_highlight_enabled",
            "storyboard_highlight_context_max_chars",
            "storyboard_highlight_max_segments",
            "storyboard_highlight_align_timeline",
        ):
            data.setdefault(key, DEFAULT_SETTINGS[key])
    data["settings_schema_version"] = SETTINGS_SCHEMA_VERSION


class Config:
    def __init__(self):
        self._data = dict(DEFAULT_SETTINGS)
        self.load()

    def load(self):
        if SETTINGS_FILE.exists():
            try:
                saved = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
                for k, v in saved.items():
                    self._data[k] = v
                _apply_compat_migrations(self._data, saved)
            except Exception:
                pass

    def save(self):
        SETTINGS_FILE.write_text(
            json.dumps(self._data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def list_profiles(self) -> list[str]:
        names = list(DEFAULT_PROFILE_NAMES)
        for path in sorted(PROFILES_DIR.glob("*.json")):
            if path.stem not in names:
                names.append(path.stem)
        return names

    def _profile_path(self, name: str) -> Path:
        cleaned = _safe_profile_name(name)
        return PROFILES_DIR / f"{cleaned}.json"

    def save_profile(self, name: str) -> str:
        cleaned = _safe_profile_name(name)
        self._data["active_profile"] = cleaned
        path = self._profile_path(cleaned)
        path.write_text(
            json.dumps(self._data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return cleaned

    def load_profile(self, name: str) -> str:
        cleaned = _safe_profile_name(name)
        path = self._profile_path(cleaned)
        data = dict(DEFAULT_SETTINGS)
        if path.exists():
            saved = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(saved, dict):
                data.update(saved)
                _apply_compat_migrations(data, saved)
        data["active_profile"] = cleaned
        self._data = data
        return cleaned

    def get(self, key: str, default=None):
        return self._data.get(key, default)

    def set(self, key: str, value: Any):
        self._data[key] = value

    def update(self, mapping: dict):
        self._data.update(mapping)

    def as_dict(self) -> dict:
        return dict(self._data)

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        if name in self._data:
            return self._data[name]
        raise AttributeError(name)


config = Config()


def _safe_profile_name(value: str) -> str:
    text = str(value or "").strip() or "配置1"
    for ch in '<>:"/\\|?*':
        text = text.replace(ch, "_")
    text = "".join("_" if ord(ch) < 32 else ch for ch in text)
    return text.strip(" .")[:80] or "配置1"
