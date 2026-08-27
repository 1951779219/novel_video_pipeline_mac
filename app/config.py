"""配置管理：所有可配置项均存 data/settings.json，UI 上可改。"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any

from app.utils.secrets import clean_api_key

ROOT = Path(__file__).parent.parent
DATA_DIR = ROOT / "data"
JOBS_DIR = DATA_DIR / "jobs"
# A pipeline worker may be given a per-job settings snapshot.  The GUI keeps
# using data/settings.json, while each launched job can remain pinned to the
# profile that was selected at the moment it started.
SETTINGS_FILE = Path(os.environ.get("NOVEL_VIDEO_CONFIG_FILE") or (DATA_DIR / "settings.json"))
PROFILES_DIR = DATA_DIR / "profiles"
DEFAULT_PROFILE_NAMES = ("配置1", "配置2")

DATA_DIR.mkdir(exist_ok=True)
JOBS_DIR.mkdir(exist_ok=True)
PROFILES_DIR.mkdir(exist_ok=True)


SETTINGS_SCHEMA_VERSION = 47
DEFAULT_YOUTUBE_TITLE_TEMPLATE = "{candidate_title}"
DEFAULT_YOUTUBE_DESCRIPTION = ""

API_KEY_FIELDS = {
    "ai_api_key",
    "llm_api_key",
    "tts_api_key",
    "image_api_key",
    "cover_api_key",
    "character_reference_api_key",
    "scene_reference_api_key",
    "pronunciation_dictionary_api_key",
}
API_KEY_ENV_PREFIX = "NOVEL_VIDEO_SECRET_"

# Profiles are meant to be shareable production recipes.  Keep connection
# details outside them so changing a visual/TTS profile never exposes or
# clears the operator's already configured API endpoint and credentials.
PROFILE_CONNECTION_FIELDS = API_KEY_FIELDS | {
    "source_base_url",
    "source_hosts",
    "ai_api_base_url",
    "llm_base_url",
    "tts_base_url",
    "image_base_url",
    "cover_base_url",
    "character_reference_base_url",
    "scene_reference_base_url",
    "pronunciation_dictionary_base_url",
}
PROFILE_LOCAL_FIELDS = PROFILE_CONNECTION_FIELDS | {
    "deleted_profiles",
    "task_category_root",
    "task_category_selected_path",
    "task_category_sort",
    "task_category_direction",
    # The editorial cover system is shared by every production profile.
    # Profiles keep only their genre/medium direction in cover_custom_prompt.
    "cover_prompt_template",
    "cover_ai_analysis_prompt",
    "cover_poster_method_prompt",
    # Table layout is an operator preference, not part of a production profile.
    "job_table_column_widths",
}


def api_key_env_name(key: str) -> str:
    """Return the process-only environment variable for a sensitive setting."""
    return f"{API_KEY_ENV_PREFIX}{str(key).upper()}"


DEFAULT_SETTINGS: dict[str, Any] = {
    "settings_schema_version": SETTINGS_SCHEMA_VERSION,
    "task_category_root": "",
    "task_category_selected_path": "",
    "task_category_sort": "名称自然排序",
    "task_category_direction": "升序",
    # The task table writes the widths here after the operator drags a header
    # separator.  Keep this as data (instead of Tk geometry) so it survives a
    # restart as well as profile switching.
    "job_table_column_widths": {},
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
    "ai_api_text_model": "gemini-3.5-flash",
    "ai_api_image_model": "gpt-image-2",
    "ai_api_image_width": 1792,
    "ai_api_image_height": 1008,

    # LLM（分镜）
    "llm_provider": "openai",  # openai / claude / gemini / deepseek / ollama / custom
    "llm_base_url": "https://api.openai.com/v1",
    "llm_api_key": "",
    "llm_model": "gemini-3.5-flash",
    "llm_storyboard_prompt": (
        "You are a visual prompt director for long-form novel recap / tweet-style video images.\n"
        "Convert each novel paragraph into ONE finished English image-generation prompt. "
        "Use the supplied story visual theme lock and character consistency lock when present, while still making the scene "
        "match the current paragraph. The prompt must include: main event with an action verb, protagonist or relationship, "
        "conflict/tension, camera language, lighting, mood, palette, and one symbolic story detail. Avoid static landscape shots; "
        "make every image feel like a story moment from the same production. Use adult-looking characters, cinematic anime/editorial "
        "illustration style, and policy-safe symbolic drama. Render absolutely no writing of any kind: no letters, words, Japanese, "
        "Chinese, numbers, signage, book pages, speech bubbles, captions, typography, UI, logos, or watermarks; leave all props blank. "
        "No gore, nudity, sexual content, "
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
        "restrained 4-6 color palette, cinematic 16:9 composition, "
        "clean composition. Absolutely no visible writing or glyphs anywhere: no letters, words, Japanese, Chinese, numbers, "
        "signage, book pages, speech bubbles, captions, typography, UI, logos, or watermarks; all documents, signs, screens and "
        "magical effects must be blank or abstract."
    ),
    "llm_image_style_suffix": "anime style, detailed, cinematic lighting, masterpiece",
    # Keep this compatible with SD/ComfyUI backends without putting policy
    # trigger words in a reusable setting.  OpenAI-compatible backends do not
    # receive this list because their API has no negative-prompt field.
    "llm_negative_prompt": "low resolution, anatomy errors, malformed hands, text, letters, words, writing, Japanese characters, Chinese characters, numbers, typography, signage, book pages, speech bubbles, captions, watermark, logo, UI overlay, duplicate limbs",

    # 视频生图解决方案1：把秒数作为图片数量预算，每个预算窗口只选择一个旁白高光点。
    "storyboard_highlight_enabled": True,
    "storyboard_highlight_context_max_chars": 10000,
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
        '  "relationships": [\n'
        "    {\n"
        '      "from": "人物姓名",\n'
        '      "to": "另一人物姓名",\n'
        '      "relation": "亲属、同伴、敌对、爱慕、上下级等简短关系",\n'
        '      "record_status": "auto"\n'
        "    }\n"
        "  ],\n"
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
    # One grounded marketing pass produces every publishing candidate used by
    # the cover and upload stages.  Keep the legacy short-title settings above
    # for old profiles and task snapshots, but new jobs use this structured
    # bundle instead of asking for one disposable title.
    "marketing_title_min_chars": 40,
    "marketing_title_max_chars": 70,
    "marketing_candidates_max_tokens": 1600,
    # An OpenAI-compatible relay can occasionally return a non-JSON answer
    # while otherwise being healthy.  Keep unattended jobs alive long enough
    # for that transient route/model condition to clear.
    "marketing_candidates_retry_attempts": 5,
    "marketing_candidates_retry_delay_seconds": 60,
    "marketing_candidates_prompt": (
        "あなたは日本向けYouTube小説朗読・物語動画の編集者です。入力は作品の冒頭・中盤・終盤から抽出した"
        "事実ベースの資料です。全文を細かく要約せず、誰が誰に何をしたか、主要事件、人物関係、異常事態、"
        "終盤で明らかになる真相または逆転を内部で把握してください。原文にない設定・人物・能力・事件・結末は"
        "絶対に追加しないでください。\n\n"
        "内部で男性向け・女性向け・中性向け、および恋愛、復讐、ざまぁ、修羅場、職場、学園、異世界、"
        "ファンタジー、ラブコメ、ダーク、歴史などの題材を判定し、判定結果自体は出力しないでください。\n\n"
        "通常の小説では、漫画広告・WEB小説風で、一瞬で内容が分かり、事件性と関係性が具体的で、続きを"
        "見たくなる自然な日本語タイトルを作ってください。女性向けでは後悔、溺愛、執着、ざまぁ、今さら感、"
        "男性向けでは逆転、覚醒、追放、美少女、同居、甘やかし、距離感などを、原文に根拠がある場合だけ使えます。"
        "題材に自然なら【修羅場】【ざまぁ】【衝撃】【復讐】【禁断】【沼注意】、（笑）、❤、……、www、"
        "〜なんだが等も使用できます。露骨な性的表現、未成年の性的描写、抽象的な短文、ポエム、汎用テンプレは"
        "禁止です。\n\n"
        "三国志・歴史題材では同じ構成を使いながら、主要人物または勢力、計略、兵力差、危機、決断、同盟、"
        "裏切り、戦局や政治の逆転を具体的に書いてください。❤、（笑）、www、溺愛、修羅場などの恋愛向け表現や、"
        "根拠のない大げさな煽りは使用しないでください。史実と演義を入力以上に混同しないでください。\n\n"
        "完成した動画タイトルを必ず3案作り、各40〜70文字程度、3案は構成と切り口を変えてください。"
        "概要欄用あらすじを必ず2案作り、各80〜160文字程度、事件・関係性・主人公の状況を入れ、結末を全部"
        "書かず、2案は切り口を変えてください。内容に一致するタグを10〜15個作ってください。"
        "【朗読・小説】、#日本語・#日语・#Japaneseなど言語名だけのタグは付けず、#赤陽の勧めるノベルと制作方法に関するタグも使用しないでください。\n\n"
        "必ず次のJSONオブジェクトだけを出力し、Markdown、説明、作品タイトル、判定結果、推理過程を付けないでください。\n"
        "{\"titles\":[\"タイトル1\",\"タイトル2\",\"タイトル3\"],"
        "\"synopses\":[\"あらすじ1\",\"あらすじ2\"],"
        "\"tags\":[\"#タグ1\",\"#タグ2\",\"#タグ3\"]}"
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
    # Make the rewritten script safe to display and narrate before it is split.
    "tts_clean_rewritten_text": True,
    # Off is the unattended-safe default.  When enabled, only deterministic
    # high-risk Japanese token candidates are sent to the text LLM for readings.
    "tts_auto_pronunciation_enabled": False,
    "tts_auto_pronunciation_max_terms": 40,
    "pronunciation_dictionary_dedicated_api_enabled": False,
    "pronunciation_dictionary_base_url": "",
    "pronunciation_dictionary_api_key": "",
    "pronunciation_dictionary_model": "gemini-3.5-flash",
    "pronunciation_dictionary_prompt": (
        "你是日语 TTS 生词词典助手。只为用户给出的候选词提供准确的日语假名读音。"
        "只输出严格的“原词=假名读音”，每行一条；不得输出解释、标题或 Markdown，"
        "不得添加候选列表以外的词，不确定时直接省略。"
        "读音只能使用平假名、片假名、中点或长音符。"
    ),
    # TTS
    "tts_provider": "edge",  # edge / voicevox / azure / openai / elevenlabs / custom
    "tts_voice": "ja-JP-NanamiNeural",
    "tts_rate": "+0%",
    "tts_volume": 1.0,
    "voicevox_speed_scale": 0.90,
    "voicevox_intonation_scale": 0.85,
    "voicevox_pause_scale": 1.25,
    "tts_model": "tts-1",
    "tts_api_key": "",
    "tts_base_url": "",  # voicevox: http://localhost:50021 ; openai: https://api.openai.com/v1 ; etc
    "tts_extra": {},  # 额外参数 (azure region 等)
    # Unattended jobs must eventually yield the queue to the next task.
    "pipeline_failure_retry_limit": 5,
    "pipeline_skip_after_failures": True,
    "tts_retries": 5,
    "tts_retry_until_success": False,
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
    "image_width": 1792,
    "image_height": 1008,
    "image_steps": 25,
    "image_cfg": 7.0,
    "image_api_timeout_seconds": 300,
    "image_retry_attempts": 5,
    "image_workflow": "",  # comfyui workflow json 路径（可选）

    # Cover
    "cover_enabled": True,
    "series_animation_enabled": True,
    "cover_provider": "same_as_image",
    "cover_base_url": "",
    "cover_api_key": "",
    "cover_model": "",
    "cover_width": 1792,
    "cover_height": 1008,
    "cover_title_size": 72,
    "cover_title_font": "Microsoft YaHei",
    "cover_title_color": "#FFFFFF",
    "cover_title_bg": "#050505",
    "cover_title_area_ratio": 0.24,
    "cover_prompt_template": (
        "Design one finished horizontal 16:9 Japanese LINE Manga editorial advertisement.\n"
        "Story input: {excerpt}\n"
        "Generated title candidate: {title}\n"
        "Series identifier when supplied: {series_badge}\n"
        "Prioritize commercial editorial design over illustration. Build the composition around 3-5 exact Japanese editorial "
        "text blocks with a clear hierarchy: one huge selling point, one secondary headline, one or two sticker/badge blocks, "
        "and optionally one short supporting line. Count a supplied series identifier inside the 3-5 blocks. Use bold, layered, "
        "compact, rhythmic typography with strong size contrast "
        "and thumbnail readability. The supporting story image communicates one decisive event through character action, "
        "reaction, and a meaningful prop or setting.\n"
        "{custom}"
    ),
    "cover_custom_prompt": (
        "Japanese LINE Manga editorial advertisement, commercial editorial design, typography-led composition, bold layered "
        "compact Japanese type, strong size contrast, bright editorial color blocks, stickers and badges, clean manga rendering "
        "as supporting story imagery, highly readable at thumbnail size"
    ),
    "cover_ai_analysis_prompt": (
        "Think like a Japanese manga editor maximizing click-through rate. Choose the strongest supported event or reversal and "
        "turn it into one story-specific advertising concept, composition, and visual storytelling device. Prioritize commercial "
        "editorial design over illustration. Write exact natural Japanese copy for 3-5 clearly differentiated editorial text "
        "blocks: one huge selling point, one secondary headline, one or two stickers/badges, and optionally one short supporting "
        "line. Count a supplied series identifier as one of the 3-5 blocks. Create rhythm through strong size contrast, layering, "
        "compact grouping, and placement. Make the composition and "
        "storytelling concept specific to this story. Return one complete image-generation prompt only."
    ),
    "cover_prompt_max_tokens": 420,
    "cover_policy_prompt_versions": 3,
    "cover_transient_retries": 3,
    "cover_poster_method_prompt": (
        "Japanese LINE Manga editorial-ad method:\n"
        "Return one 180-280 word English image prompt for one finished horizontal 16:9 commercial manga advertisement. Choose "
        "one story-supported high-CTR event and one distinctive advertising concept. Let typography and graphic layout lead; use "
        "character action, reaction, a meaningful prop, and the setting as supporting evidence for the selling point.\n"
        "Specify 3-5 exact natural Japanese editorial text blocks: one huge selling point, one secondary headline, one or two "
        "sticker/badge blocks, and optionally one short supporting line. Count a supplied series identifier as one of these blocks. "
        "For every block give the exact text, position, relative "
        "scale, color, outline or panel shape, angle, and overlap relationship. Make the hierarchy bold, layered, compact, rhythmic, "
        "and readable at thumbnail size. Create rhythm through extreme size contrast and intentional grouping.\n"
        "Use clean story-appropriate rendering, bold editorial color fields, cutout framing, borders, stickers, arrows, bursts, or "
        "cropped panels when they serve the chosen concept. Give every cover its own advertising concept, composition, and storytelling. "
        "Return one finished prompt with the explicitly planned Japanese text blocks."
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

    # Short vertical video. Disabled by default so upgrading an existing
    # installation never creates unexpected text/image API calls.
    "short_video_enabled": False,
    "short_video_mode": "reuse_main",  # reuse_main / independent
    "short_video_duration_seconds": 58,
    "short_video_width": 1080,
    "short_video_height": 1920,
    "short_video_blur_sigma": 28.0,
    "short_video_script_min_seconds": 45,
    "short_video_script_max_seconds": 58,
    "short_video_prebuild_script_enabled": False,
    "short_video_script_max_chars": 350,
    "short_video_image_count": 6,
    "short_video_image_width": 1024,
    "short_video_image_height": 1792,
    "short_video_image_prompt_mode": "reuse_main",  # reuse_main / rewrite
    "short_video_script_prompt": (
        "入力は、同じ小説の冒頭・中盤・終盤から抽出した事実資料です。資料の順番どおりに朗読・要約せず、"
        "三段構成のあらすじや原文の機械的な継ぎ合わせにもしないでください。まず内部で、作品全体から視聴者を最も"
        "引きつける核心――主人公の境遇、重要な人物関係、最大の対立、異常事態、秘密、逆転、結末直前の謎――を"
        "見極め、それらの精髄を一本のShort予告ナレーションへ再構成してください。最初の一文で最も強い対立、"
        "異常な事実、衝撃的な結果のいずれかを直ちに提示し、その後は人物と危機を短く明確に伝えながら緊張を高め、"
        "真相、決断、逆転、結末が明らかになる直前で止め、続きを強く見たくさせてください。出力は、そのまま音声化"
        "できる自然な日本語の一段落だけにしてください。中国語・英語・他言語を混ぜず、タイトル、説明、タグ、"
        "番号、絵コンテ、冒頭・中盤・終盤という資料ラベル、『この物語は』のような前置きを出力しないでください。"
        "原作の事実を厳守し、存在しない人物、能力、事件、設定、結末を追加しないでください。"
    ),
    "short_video_image_prompt": (
        "根据Short旁白拆分竖屏分镜。每张图必须描述人物身份、外貌、服装、动作、情绪、场景、镜头景别与光线；"
        "保持人物设定连续。主体放在画面中央安全区域，避免关键信息贴近顶部、底部和右侧按钮区。"
        "只输出严格JSON对象：{\"prompts\":[\"prompt 1\",\"prompt 2\"]}，不要Markdown和解释。"
    ),
    "short_video_portrait_suffix": (
        "portrait 9:16 composition, vertical canvas, full-height environment, "
        "subject inside the center safe area, no letterbox, no landscape canvas, no text"
    ),
    "short_video_subtitle_size": 58,
    "short_video_subtitle_margin_v": 300,
    "short_video_subtitle_chars_per_line": 13,
    "short_video_subtitle_max_lines": 2,
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
    # First-stage YouTube scheduled-publish test.  This is deliberately
    # applied only by the GUI's manual upload path until batch allocation is
    # implemented and verified against the live Studio UI.
    "youtube_schedule_enabled": False,
    "youtube_schedule_date": "",
    "youtube_schedule_time": "18:00",
    "youtube_schedule_timezone": "Asia/Tokyo",
    "youtube_publish_mode": "immediate",
    "script_schedule_first_date": "",
    "script_schedule_time": "18:00",
    "script_schedule_interval_hours": 24,
    "script_schedule_timezone": "Asia/Tokyo",
    "script_schedule_missed_action": "next_slot",
    "script_schedule_unfinished_action": "next_slot",
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


def _apply_compat_migrations(data: dict[str, Any], saved: dict[str, Any] | None = None) -> None:
    saved = saved or {}
    saved_version = _to_int(saved.get("settings_schema_version"), 0)
    if saved_version < SETTINGS_SCHEMA_VERSION:
        if _to_int(data.get("scraper_max_chars"), 0) == 8000:
            data["scraper_max_chars"] = 0
        if _looks_like_legacy_title_template(data.get("youtube_title_template")) or "{title}" in str(data.get("youtube_title_template") or ""):
            data["youtube_title_template"] = DEFAULT_YOUTUBE_TITLE_TEMPLATE
        # An empty upload description is an intentional operator choice.
        # Never restore bundled wording merely because this field is blank.
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
    if saved_version < 25:
        # GPT Image 2 accepts this native 16:9 size directly. Keep the API
        # request, scene files and cover files on the same canvas.
        data["ai_api_image_width"] = DEFAULT_SETTINGS["ai_api_image_width"]
        data["ai_api_image_height"] = DEFAULT_SETTINGS["ai_api_image_height"]
        data["image_width"] = DEFAULT_SETTINGS["image_width"]
        data["image_height"] = DEFAULT_SETTINGS["image_height"]
        data["cover_width"] = DEFAULT_SETTINGS["cover_width"]
        data["cover_height"] = DEFAULT_SETTINGS["cover_height"]
    if saved_version < 26:
        # Remove framing workarounds that were added when GPT Image responses
        # were incorrectly treated as permanently fixed at 3:2. This also
        # cleans per-job settings snapshots when an older job is resumed.
        data.pop("video_safe_framing", None)
        prefix = str(data.get("llm_image_prompt_prefix") or "")
        prefix = prefix.replace("cinematic medium-wide 16:9 composition", "cinematic 16:9 composition")
        prefix = prefix.replace(", complete heads and faces inside the frame with generous safe margins", "")
        prefix = prefix.replace(", complete heads and faces inside the frame, safe edge margins", "")
        data["llm_image_prompt_prefix"] = prefix

        negative_parts = []
        framing_negatives = {
            "cropped face", "cropped head", "cut-off head", "face outside frame",
            "forehead outside frame", "portrait crop", "partial person at image boundary",
            "person touching image edge", "oversized foreground face", "extreme close-up",
        }
        for part in str(data.get("llm_negative_prompt") or "").split(","):
            item = part.strip()
            if item and item.lower() not in framing_negatives:
                negative_parts.append(item)
        data["llm_negative_prompt"] = ", ".join(negative_parts)

        for key in ("cover_ai_analysis_prompt", "cover_poster_method_prompt"):
            text = str(data.get(key) or "")
            text = text.replace(" Keep all people, action, weapons, horses, banners, and text inside the final 16:9 safe area.", "")
            text = text.replace(" Keep all people, action, and text inside the final 16:9 safe area.", "")
            text = text.replace("a clean 16:9 safe composition", "a clean composition")
            data[key] = text
    if saved_version < 19:
        # Only replace the known old bundled list; leave deliberate custom
        # negative prompts untouched for local SD/ComfyUI users.
        old_negative = "lowres, bad anatomy, bad hands, text, error, missing fingers, jpeg artifacts, watermark, logo, words, blood, gore, corpse, severed limbs, nudity, sexual content, explicit violence, weapon close-up"
        if str(data.get("llm_negative_prompt") or "").strip() == old_negative:
            data["llm_negative_prompt"] = DEFAULT_SETTINGS["llm_negative_prompt"]
    if saved_version < 20:
        for key in (
            "tts_clean_rewritten_text",
            "tts_auto_pronunciation_enabled",
            "tts_auto_pronunciation_max_terms",
        ):
            data.setdefault(key, DEFAULT_SETTINGS[key])
    if saved_version < 23:
        for key in (
            "marketing_title_min_chars",
            "marketing_title_max_chars",
            "marketing_candidates_max_tokens",
            "marketing_candidates_prompt",
            "cover_prompt_max_tokens",
            "cover_policy_prompt_versions",
            "cover_transient_retries",
            "cover_poster_method_prompt",
        ):
            data.setdefault(key, DEFAULT_SETTINGS[key])
        if "conceptual typography movie poster" in str(data.get("cover_prompt_template") or ""):
            data["cover_prompt_template"] = DEFAULT_SETTINGS["cover_prompt_template"]
        if "senior poster art director" in str(data.get("cover_ai_analysis_prompt") or "").lower():
            data["cover_ai_analysis_prompt"] = DEFAULT_SETTINGS["cover_ai_analysis_prompt"]
        if "dramatic character close-up" in str(data.get("cover_custom_prompt") or ""):
            data["cover_custom_prompt"] = DEFAULT_SETTINGS["cover_custom_prompt"]
    if saved_version < 34:
        for key in (
            "pronunciation_dictionary_dedicated_api_enabled",
            "pronunciation_dictionary_base_url",
            "pronunciation_dictionary_api_key",
            "pronunciation_dictionary_model",
            "pronunciation_dictionary_prompt",
        ):
            data.setdefault(key, DEFAULT_SETTINGS[key])
    if saved_version < 28:
        for key in (
            "youtube_schedule_enabled",
            "youtube_schedule_date",
            "youtube_schedule_time",
            "youtube_schedule_timezone",
        ):
            data.setdefault(key, DEFAULT_SETTINGS[key])
    if saved_version < 29:
        legacy_publish_mode = "youtube" if bool(data.get("youtube_schedule_enabled", False)) else "immediate"
        if "youtube_publish_mode" not in saved:
            data["youtube_publish_mode"] = legacy_publish_mode
        for key in (
            "script_schedule_first_date",
            "script_schedule_time",
            "script_schedule_interval_hours",
            "script_schedule_timezone",
            "script_schedule_missed_action",
            "script_schedule_unfinished_action",
        ):
            data.setdefault(key, DEFAULT_SETTINGS[key])
        try:
            profiles = json.loads(str(data.get("browser_profiles", "[]") or "[]"))
            if isinstance(profiles, list):
                for profile in profiles:
                    if not isinstance(profile, dict):
                        continue
                    profile.setdefault("publish_mode", legacy_publish_mode)
                    profile.setdefault("youtube_schedule_date", data.get("youtube_schedule_date", ""))
                    profile.setdefault("youtube_schedule_time", data.get("youtube_schedule_time", "18:00"))
                    profile.setdefault("youtube_schedule_timezone", data.get("youtube_schedule_timezone", "Asia/Tokyo"))
                    for key in (
                        "script_schedule_first_date", "script_schedule_time", "script_schedule_interval_hours",
                        "script_schedule_timezone", "script_schedule_missed_action", "script_schedule_unfinished_action",
                    ):
                        profile.setdefault(key, data.get(key, DEFAULT_SETTINGS[key]))
                data["browser_profiles"] = json.dumps(profiles, ensure_ascii=False)
        except Exception:
            pass
    if saved_version < 30:
        # Upload text is now entirely template-driven. Preserve deliberate
        # wording and literal tags, but point known bundled templates at the
        # randomly selected publishing candidate instead of the source title.
        title_template = str(data.get("youtube_title_template") or "")
        if title_template == "💥【爆款短文】《{clean_title}》{intro} #小说 #推文":
            data["youtube_title_template"] = DEFAULT_YOUTUBE_TITLE_TEMPLATE
        elif "【三国志完全解説】{clean_title}" in title_template:
            data["youtube_title_template"] = title_template.replace(
                "【三国志完全解説】{clean_title}",
                "【三国志完全解説】{candidate_title}",
            )
    if saved_version < 32:
        # Replace only the previous bundled cover prompts. Operator-written
        # templates stay intact; bundled defaults use a compact fixed layout.
        if str(data.get("cover_prompt_template") or "").startswith("Create ONE finished premium"):
            data["cover_prompt_template"] = DEFAULT_SETTINGS["cover_prompt_template"]
        if str(data.get("cover_custom_prompt") or "").startswith("viral Japanese isekai light novel recap thumbnail"):
            data["cover_custom_prompt"] = DEFAULT_SETTINGS["cover_custom_prompt"]
        if str(data.get("cover_ai_analysis_prompt") or "").startswith("You are the in-house art director for premium Japanese"):
            data["cover_ai_analysis_prompt"] = DEFAULT_SETTINGS["cover_ai_analysis_prompt"]
        if str(data.get("cover_poster_method_prompt") or "").startswith("Fixed poster-production method for every cover:"):
            data["cover_poster_method_prompt"] = DEFAULT_SETTINGS["cover_poster_method_prompt"]
        if int(data.get("cover_prompt_max_tokens", 1400) or 1400) == 1400:
            data["cover_prompt_max_tokens"] = DEFAULT_SETTINGS["cover_prompt_max_tokens"]
    if saved_version < 33:
        data.setdefault("series_animation_enabled", DEFAULT_SETTINGS["series_animation_enabled"])
    if saved_version < 36:
        for key in (
            "marketing_candidates_retry_attempts",
            "marketing_candidates_retry_delay_seconds",
        ):
            data.setdefault(key, DEFAULT_SETTINGS[key])
    if saved_version < 39:
        data.pop("acceleration_mode_enabled", None)
    if saved_version < 40:
        data.pop("acceleration_mode_enabled", None)
    if saved_version < 42:
        data["ai_rewrite_prompt"] = DEFAULT_SETTINGS["ai_rewrite_prompt"]
        for key in (
            "narrate_chapter_headings",
            "ai_rewrite_replace_entities",
            "ai_rewrite_entity_max_chars",
            "ai_rewrite_min_length_ratio",
            "ai_rewrite_language_check_enabled",
            "ai_rewrite_japanese_kana_min_ratio",
            "ai_rewrite_stop_on_failure",
            "ai_rewrite_api_timeout_seconds",
            "ai_rewrite_retry_attempts",
            "ai_rewrite_entity_prompt",
            "ai_rewrite_user_template",
        ):
            data.pop(key, None)
    if saved_version < 43:
        # Delivery-folder export has been retired.  Remove legacy profile
        # values so they cannot create an unexpected extra copy again.
        data.pop("output_directory", None)
        data.pop("output_to_input_directory", None)
    if saved_version < 44:
        for key in (
            "short_video_enabled", "short_video_mode", "short_video_duration_seconds",
            "short_video_width", "short_video_height", "short_video_blur_sigma",
            "short_video_script_min_seconds", "short_video_script_max_seconds",
            "short_video_image_count", "short_video_image_width", "short_video_image_height",
            "short_video_image_prompt_mode", "short_video_script_prompt",
            "short_video_image_prompt", "short_video_portrait_suffix",
            "short_video_subtitle_size", "short_video_subtitle_margin_v",
            "short_video_subtitle_chars_per_line", "short_video_subtitle_max_lines",
        ):
            data.setdefault(key, DEFAULT_SETTINGS[key])
    if saved_version < 45:
        for key in (
            "short_video_prebuild_script_enabled",
            "short_video_script_max_chars",
        ):
            data.setdefault(key, DEFAULT_SETTINGS[key])
    if saved_version < 46:
        old_short_prompt = str(data.get("short_video_script_prompt") or "")
        if old_short_prompt.startswith("请从小说全文中选取一个冲突最强、能够独立理解的情节"):
            data["short_video_script_prompt"] = DEFAULT_SETTINGS["short_video_script_prompt"]
    if saved_version < 47:
        old_short_prompt = str(data.get("short_video_script_prompt") or "")
        if old_short_prompt.startswith("输入是同一部小说从开头、中段和结尾提取的事实资料"):
            data["short_video_script_prompt"] = DEFAULT_SETTINGS["short_video_script_prompt"]
    # Cover prompts describe aspect ratio only. Older profiles hard-coded a
    # pixel size even when the GUI's actual cover dimensions were different.
    for key in ("cover_prompt_template", "cover_ai_analysis_prompt", "cover_poster_method_prompt"):
        if key in data:
            data[key] = re.sub(r"(?i)\b1280\s*[x×✖]\s*720\b", "16:9", str(data.get(key) or ""))
    marketing_prompt = str(data.get("marketing_candidates_prompt") or "").replace(
        "異世界なら#異世界、異世界以外の一般小説なら#赤陽の勧めるノベルを含めてください。",
        "異世界なら#異世界を含めてください。#赤陽の勧めるノベルは絶対に使用しないでください。",
    )
    marketing_prompt = marketing_prompt.replace(
        "タグは10〜15個、先頭表示は必ず【朗読・小説】とし、異世界なら#異世界を含めてください。#赤陽の勧めるノベルは絶対に使用しないでください。制作方法のタグは不要です。",
        "内容に一致するタグを10〜15個作ってください。【朗読・小説】は付けず、#赤陽の勧めるノベルと制作方法に関するタグも使用しないでください。",
    )
    marketing_prompt = marketing_prompt.replace(
        "タグは生成しないでください。",
        "内容に一致するタグを10〜15個作ってください。【朗読・小説】は付けず、#赤陽の勧めるノベルと制作方法に関するタグも使用しないでください。",
    )
    language_tag_rule = "#日本語・#日语・#Japaneseなど言語名だけのタグは付けないでください。"
    if language_tag_rule not in marketing_prompt:
        marketing_prompt = marketing_prompt.replace(
            "#赤陽の勧めるノベルと制作方法に関するタグも使用しないでください。",
            f"{language_tag_rule}#赤陽の勧めるノベルと制作方法に関するタグも使用しないでください。",
        )
    if '"tags"' not in marketing_prompt and '"synopses"' in marketing_prompt:
        marketing_prompt = marketing_prompt.replace(
            '"synopses":["あらすじ1","あらすじ2"]}',
            '"synopses":["あらすじ1","あらすじ2"],"tags":["#タグ1","#タグ2","#タグ3"]}',
        )
    data["marketing_candidates_prompt"] = marketing_prompt
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
        # Worker jobs receive their non-sensitive settings from a JSON snapshot.
        # API keys deliberately travel only through the child-process environment
        # so no task JSON, log, or release package stores a real credential.
        for key in API_KEY_FIELDS:
            value = os.environ.get(api_key_env_name(key))
            if value is not None:
                self._data[key] = clean_api_key(value)

    def save(self):
        SETTINGS_FILE.write_text(
            json.dumps(self._data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def list_profiles(self) -> list[str]:
        deleted = {
            _safe_profile_name(name)
            for name in self._data.get("deleted_profiles", [])
            if str(name or "").strip()
        }
        names = [name for name in DEFAULT_PROFILE_NAMES if name not in deleted]
        for path in sorted(PROFILES_DIR.glob("*.json")):
            if path.stem not in deleted and path.stem not in names:
                names.append(path.stem)
        return names

    def suggest_profile_name(self) -> str:
        """Return the first unused friendly name for a newly copied profile."""
        existing = set(self.list_profiles())
        index = 1
        while f"新配置{index}" in existing:
            index += 1
        return f"新配置{index}"

    def _profile_path(self, name: str) -> Path:
        cleaned = _safe_profile_name(name)
        return PROFILES_DIR / f"{cleaned}.json"

    def save_profile(self, name: str) -> str:
        cleaned = _safe_profile_name(name)
        deleted = {
            _safe_profile_name(item)
            for item in self._data.get("deleted_profiles", [])
            if str(item or "").strip()
        }
        deleted.discard(cleaned)
        self._data["deleted_profiles"] = sorted(deleted)
        self._data["active_profile"] = cleaned
        path = self._profile_path(cleaned)
        profile_data = {
            key: value for key, value in self._data.items()
            if key not in PROFILE_LOCAL_FIELDS
        }
        path.write_text(
            json.dumps(profile_data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return cleaned

    def delete_profile(self, name: str) -> tuple[str, str]:
        """Delete one visible profile and load a neighboring remaining profile."""
        cleaned = _safe_profile_name(name)
        names = self.list_profiles()
        if cleaned not in names:
            raise FileNotFoundError(f"配置方案不存在：{cleaned}")
        if len(names) <= 1:
            raise ValueError("至少需要保留一个配置方案，当前方案不能删除。")

        current_index = names.index(cleaned)
        remaining = [item for item in names if item != cleaned]
        next_name = remaining[min(current_index, len(remaining) - 1)]
        path = self._profile_path(cleaned)
        if path.exists():
            path.unlink()

        deleted = {
            _safe_profile_name(item)
            for item in self._data.get("deleted_profiles", [])
            if str(item or "").strip()
        }
        if cleaned in DEFAULT_PROFILE_NAMES:
            deleted.add(cleaned)

        self.load_profile(next_name)
        self._data["deleted_profiles"] = sorted(deleted)
        self._data["active_profile"] = next_name
        self.save()
        return cleaned, next_name

    def profile_settings(self, name: str) -> tuple[str, dict]:
        """Return a saved profile without changing the GUI's active profile."""
        cleaned = _safe_profile_name(name)
        path = self._profile_path(cleaned)
        if not path.exists():
            raise FileNotFoundError(f"配置方案不存在：{cleaned}")
        try:
            saved = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise ValueError(f"配置方案无法读取：{cleaned}（{exc}）") from exc
        if not isinstance(saved, dict):
            raise ValueError(f"配置方案格式无效：{cleaned}")
        data = dict(DEFAULT_SETTINGS)
        # A compact/shareable profile intentionally omits API URLs and keys.
        # In that case inherit the live connection settings, rather than
        # reverting to defaults and making the profile appear unusable.
        for key in PROFILE_LOCAL_FIELDS:
            if key in self._data:
                data[key] = self._data[key]
        # Older profile files may still contain copies of the shared cover
        # system. Ignore those stale copies so switching profiles cannot bring
        # back an incompatible title hierarchy or composition method.
        data.update({key: value for key, value in saved.items() if key not in PROFILE_LOCAL_FIELDS})
        _apply_compat_migrations(data, saved)
        data["active_profile"] = cleaned
        return cleaned, data

    def load_profile(self, name: str) -> str:
        cleaned = _safe_profile_name(name)
        path = self._profile_path(cleaned)
        if path.exists():
            _, data = self.profile_settings(cleaned)
        else:
            data = dict(DEFAULT_SETTINGS)
            for key in PROFILE_LOCAL_FIELDS:
                if key in self._data:
                    data[key] = self._data[key]
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
