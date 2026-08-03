"""Story and character analysis for consistent novel-video images."""
from __future__ import annotations

import json
import re
from typing import Any

from app.backends.llm import LLMBackend
from app.scrapers.base import Novel
from app.stages.stage2_clean import Segment


DEFAULT_CHARACTER_ANALYSIS_PROMPT = """
你是小说推文视频的剧情解析和人设导演。
任务：阅读导入文章，自动区分主角、重要配角、路人，并为后续生图锁定固定人设。

必须只输出 JSON，不要 Markdown，不要解释。
如果原文没有明确外观，请合理补全一次，并在后续保持一致；不要写“可能”“推测”“未提及”。
人物外观必须适合全年龄推文画面，避免血腥、裸露、色情、儿童危险、真实名人、商标水印。
主角和重要配角必须有稳定的 hair / outfit / visual_prompt_en，后续每张图都会复用。

输出格式：
{
  "plot_summary": "100字以内剧情梗概",
  "story_conflict": "核心冲突",
  "visual_theme": {
    "genre": "wuxia|fantasy|anime|urban|historical|sci_fi|suspense|romance|other",
    "theme_name_zh": "本次任务统一画面类型，例如都市悬疑、古风武侠、日漫校园、魔幻史诗",
    "style_prompt_en": "English global style prompt for all character reference images and scene images",
    "background_prompt_en": "English recurring world/background prompt for this story",
    "negative_prompt_en": "English visual negative constraints"
  },
  "protagonists": ["主角姓名"],
  "supporting_characters": ["重要配角姓名"],
  "relationships": [
    {
      "from": "人物姓名",
      "to": "另一人物姓名",
      "relation": "亲属、同伴、敌对、爱慕、上下级等简短关系",
      "record_status": "auto"
    }
  ],
  "characters": [
    {
      "name": "人物姓名或身份名",
      "trigger": "char_unique_ascii_trigger",
      "aliases": ["别名或称呼"],
      "importance": "protagonist|supporting|minor",
      "gender": "male|female|unknown",
      "age_group": "child|young_adult|adult|middle_aged|elderly|unknown",
      "role_in_story": "人物在剧情中的功能",
      "personality": "性格关键词",
      "visual_profile_zh": "一句中文固定外观：性别，年龄段，发色发型，服装颜色和款式，气质",
      "visual_prompt_en": "English stable visual prompt, including hair, outfit, age impression, and vibe",
      "reference_prompt_en": "English prompt for generating a clean full-body character reference sheet on a simple themed background",
      "lock_rules_zh": "后续生图必须保持不变的外观规则"
    }
  ],
  "visual_rules": ["全片统一画风和连续性要求"]
}
""".strip()


def can_call_analysis_llm(provider: str, api_key: str) -> bool:
    provider = str(provider or "").lower()
    return bool(api_key) or provider in {"ollama", "custom"}


def analyze_characters(
    novel: Novel,
    segments: list[Segment],
    llm: LLMBackend,
    *,
    max_chars: int = 12000,
    max_tokens: int = 1800,
    system_prompt: str = DEFAULT_CHARACTER_ANALYSIS_PROMPT,
) -> dict[str, Any]:
    user_text = _analysis_input(novel, segments, max_chars=max_chars)
    raw = llm.complete(system_prompt, user_text, max_tokens=max_tokens, temperature=0.15)
    data = _parse_json_object(raw)
    return normalize_analysis(data, raw=raw)


def normalize_analysis(data: Any, *, raw: str = "") -> dict[str, Any]:
    if not isinstance(data, dict):
        data = {}
    characters = data.get("characters")
    if not isinstance(characters, list):
        characters = []

    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in characters:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        aliases = item.get("aliases")
        if not isinstance(aliases, list):
            aliases = []
        aliases = [
            str(x).strip()
            for x in aliases
            if _usable_alias(str(x).strip(), name)
        ]
        importance = str(item.get("importance") or "minor").strip().lower()
        if importance not in {"protagonist", "supporting", "minor"}:
            importance = "minor"
        normalized.append(
            {
                "name": name,
                "trigger": _safe_trigger(str(item.get("trigger") or ""), name, len(normalized)),
                "aliases": aliases[:8],
                "importance": importance,
                "gender": str(item.get("gender") or "unknown").strip() or "unknown",
                "age_group": str(item.get("age_group") or "unknown").strip() or "unknown",
                "role_in_story": str(item.get("role_in_story") or "").strip(),
                "personality": str(item.get("personality") or "").strip(),
                "visual_profile_zh": str(item.get("visual_profile_zh") or "").strip(),
                "visual_prompt_en": str(item.get("visual_prompt_en") or "").strip(),
                "reference_prompt_en": str(item.get("reference_prompt_en") or "").strip(),
                "lock_rules_zh": str(item.get("lock_rules_zh") or "").strip(),
            }
        )

    protagonists = _names_from(data.get("protagonists")) or [
        c["name"] for c in normalized if c.get("importance") == "protagonist"
    ]
    supporting = _names_from(data.get("supporting_characters")) or [
        c["name"] for c in normalized if c.get("importance") == "supporting"
    ]
    visual_rules = data.get("visual_rules")
    if not isinstance(visual_rules, list):
        visual_rules = []

    relationships = []
    for item in data.get("relationships") or data.get("character_relationships") or []:
        if not isinstance(item, dict):
            continue
        source = str(item.get("from") or item.get("source") or "").strip()
        target = str(item.get("to") or item.get("target") or "").strip()
        relation = str(item.get("relation") or item.get("type") or "").strip()
        if source and target and relation:
            relationships.append({
                "from": source,
                "to": target,
                "relation": relation,
                "record_status": "confirmed"
                if str(item.get("record_status") or "").lower() == "confirmed"
                else "auto",
            })

    return {
        "enabled": True,
        "plot_summary": str(data.get("plot_summary") or "").strip(),
        "story_conflict": str(data.get("story_conflict") or "").strip(),
        "visual_theme": _normalize_visual_theme(data.get("visual_theme")),
        "protagonists": protagonists[:6],
        "supporting_characters": supporting[:12],
        "relationships": relationships[:80],
        "characters": normalized[:40],
        "visual_rules": [str(x).strip() for x in visual_rules if str(x).strip()][:12],
        "raw_response": raw[:6000],
    }


def character_context_for_text(
    analysis: dict[str, Any] | None,
    text: str,
    *,
    max_characters: int = 4,
    always_include_protagonists: bool = True,
) -> str:
    if not analysis or not analysis.get("enabled"):
        return ""
    characters = analysis.get("characters")
    if not isinstance(characters, list):
        return ""

    matched: list[dict[str, Any]] = []
    haystack = str(text or "")
    protagonist_names = set(_names_from(analysis.get("protagonists")))
    for item in characters:
        if not isinstance(item, dict):
            continue
        names = [str(item.get("name") or "").strip()]
        aliases = item.get("aliases")
        if isinstance(aliases, list):
            names.extend(str(x).strip() for x in aliases)
        names = [n for n in names if n]
        if any(n and n in haystack for n in names):
            matched.append(item)

    if always_include_protagonists:
        for item in characters:
            if not isinstance(item, dict):
                continue
            if str(item.get("name") or "").strip() in protagonist_names and item not in matched:
                matched.append(item)
            if len(matched) >= max_characters:
                break

    if not matched:
        return ""

    parts = []
    for item in matched[:max_characters]:
        name = str(item.get("name") or "").strip()
        trigger = str(item.get("trigger") or "").strip()
        visual = str(item.get("visual_prompt_en") or item.get("visual_profile_zh") or "").strip()
        role = str(item.get("importance") or "").strip()
        if visual:
            token = f", trigger token {trigger}" if trigger else ""
            parts.append(f"{name} ({role}{token}): {visual}")
    if not parts:
        return ""

    rules = analysis.get("visual_rules")
    rule_text = ""
    if isinstance(rules, list) and rules:
        rule_text = " Overall continuity: " + "; ".join(str(x).strip() for x in rules[:3] if str(x).strip())
    theme_text = visual_theme_context(analysis)
    prefix = f"{theme_text}. " if theme_text else ""
    return (
        prefix
        + "Character consistency lock: "
        + " | ".join(parts)
        + ". Keep these characters' face, hair, age impression, clothing colors, and outfit style consistent in this image."
        + rule_text
    )


def visual_theme_context(analysis: dict[str, Any] | None) -> str:
    if not analysis or not analysis.get("enabled"):
        return ""
    theme = analysis.get("visual_theme")
    if not isinstance(theme, dict):
        return ""
    parts = [
        str(theme.get("style_prompt_en") or "").strip(),
        str(theme.get("background_prompt_en") or "").strip(),
    ]
    parts = [x for x in parts if x]
    if not parts:
        return ""
    return "Story visual theme lock: " + "; ".join(parts)


def character_reference_prompt(character: dict[str, Any], analysis: dict[str, Any] | None = None) -> str:
    name = str(character.get("name") or "character").strip()
    trigger = str(character.get("trigger") or "").strip()
    visual = str(
        character.get("reference_prompt_en")
        or character.get("visual_prompt_en")
        or character.get("visual_profile_zh")
        or ""
    ).strip()
    theme = visual_theme_context(analysis)
    theme = f"{theme}. " if theme else ""
    trigger_text = f"Use trigger token {trigger} for this character. " if trigger else ""
    return (
        f"{theme}{trigger_text}Clean full-body character reference sheet for {name}: {visual}. "
        "single character, front view, neutral pose, clear face, consistent hair, consistent outfit, "
        "simple story-themed background, high detail, no text, no watermark, no logo"
    )


def _analysis_input(novel: Novel, segments: list[Segment], *, max_chars: int) -> str:
    header = [
        f"标题：{novel.title}",
        f"作者：{novel.author or ''}",
        f"简介：{novel.description or ''}",
        "",
        "正文片段：",
    ]
    body = "\n".join(seg.text for seg in segments if seg.text.strip())
    if not body.strip():
        body = novel.full_text or ""
    body = body[: max(1000, int(max_chars or 12000))]
    return "\n".join(header) + body


def _parse_json_object(raw: str) -> Any:
    text = str(raw or "").strip()
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.I)
    text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except Exception:
        pass
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        return json.loads(text[start : end + 1])
    raise ValueError("LLM did not return a JSON object")


def _names_from(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(x).strip() for x in value if str(x).strip()]
    if isinstance(value, str) and value.strip():
        return [x.strip() for x in re.split(r"[,，、\s]+", value) if x.strip()]
    return []


def _normalize_visual_theme(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {
        "genre": str(value.get("genre") or "other").strip() or "other",
        "theme_name_zh": str(value.get("theme_name_zh") or "").strip(),
        "style_prompt_en": str(value.get("style_prompt_en") or "").strip(),
        "background_prompt_en": str(value.get("background_prompt_en") or "").strip(),
        "negative_prompt_en": str(value.get("negative_prompt_en") or "").strip(),
    }


def _safe_trigger(value: str, name: str, index: int) -> str:
    text = re.sub(r"[^A-Za-z0-9_]+", "_", str(value or "")).strip("_").lower()
    if not text:
        # Names can be Chinese; keep the trigger deterministic and ASCII for prompt/workflow routing.
        text = f"char_{index + 1:02d}"
    if not text.startswith("char_"):
        text = f"char_{text}"
    return text[:48]


def _usable_alias(alias: str, name: str) -> bool:
    if not alias or alias == name:
        return False
    if alias in {"他", "她", "它", "我", "你", "TA", "ta", "男主", "女主"}:
        return False
    return len(alias) >= 2
