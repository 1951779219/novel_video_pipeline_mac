"""Grounded highlight selection for cost-controlled storyboard images."""
from __future__ import annotations

import json
import re
from typing import Any

from app.scrapers.base import Novel
from app.stages.stage2_clean import Segment


STORY_CONTEXT_SYSTEM_PROMPT = """
你是长篇旁白视频的故事背景编辑。请从采样文本中提取只用于画面判断的稳定背景，必须只输出 JSON。
不要创作原文没有的剧情，不要把不同年代、地点或人物合并成同一场景。
输出字段：story_summary（主线简述）、title_brief（供视频标题生成使用的事实简报，须涵盖主角处境、关键关系、主要事件、已明确的秘密或反转；限600字以内）、genre、era_and_world、primary_story_setting、
recurring_settings（数组）、main_characters（数组，每项含 name/aliases/role）、
relationship_context（数组）、continuity_rules（数组）。
""".strip()


HIGHLIGHT_SYSTEM_PROMPT = """
You select one grounded visual highlight from a long narration window and write its image prompt.
The global story context is background only. Never import people, actions, objects, or locations from the global
summary unless they are explicitly present in the selected numbered narration units. Prefer a complete, concrete,
high-impact action with identifiable subjects and location. Avoid incomplete transition phrases, abstract commentary,
historical exposition, cast lineups, and montages. Return JSON only with: segment_indexes (1-3 adjacent source indexes),
people (only visible named people or explicit identities), location, action, excluded_people, and image_prompt_en.
The image prompt must depict exactly one coherent moment, explicitly name its subjects/action/location, be <=110 words,
and contain no readable text, UI, logo, watermark, gore, nudity, sexual content, or explicit violence.
""".strip()


def parse_json_object(raw: str) -> dict[str, Any]:
    text = str(raw or "").strip()
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.I)
    text = re.sub(r"\s*```$", "", text)
    value: Any = None
    try:
        value = json.loads(text)
        # Some compatibility gateways JSON-encode the assistant text twice.
        if isinstance(value, str):
            value = json.loads(value)
    except Exception:
        # Decode the first complete JSON object instead of slicing from the
        # first opening brace to the last closing brace.  The latter fails on
        # otherwise-valid JSON followed by commentary or another object.
        decoder = json.JSONDecoder()
        for match in re.finditer(r"\{", text):
            try:
                candidate, _ = decoder.raw_decode(text[match.start():])
            except json.JSONDecodeError:
                continue
            if isinstance(candidate, dict):
                value = candidate
                break
        if value is None:
            raise ValueError("LLM did not return a JSON object")
    if not isinstance(value, dict):
        raise ValueError("LLM JSON response is not an object")
    return value


def sampled_story_input(
    novel: Novel,
    segments: list[Segment],
    max_chars: int,
    *,
    expand_to_limit: bool = False,
) -> str:
    """Return balanced title/context evidence without routinely sending the full cap.

    Long stories normally use compact beginning/middle/end samples.  A caller can
    explicitly expand a retry up to its configured cap when compact evidence did
    not yield a usable result.
    """
    limit = max(3000, int(max_chars or 10000))
    rows = [s.text.strip() for s in segments if s.text.strip()]
    if not rows:
        rows = [str(novel.full_text or "").strip()]
    header = f"标题：{novel.title}\n简介：{novel.description or ''}\n\n"
    budget = max(1000, limit - len(header))
    joined = "\n".join(rows)
    if len(joined) <= budget:
        return header + joined
    # Sample beginning/middle/end so a long article is not represented only by its opening.
    # A normal pass needs only enough evidence to establish the hook, turn and
    # outcome.  Do not consume the full context budget merely because it is
    # available; retries may opt into the full cap.
    part = max(300, budget // 3) if expand_to_limit else min(2000, max(500, budget // 3))
    middle = max(0, len(joined) // 2 - part // 2)
    return header + "[开头]\n" + joined[:part] + "\n[中段]\n" + joined[middle : middle + part] + "\n[结尾]\n" + joined[-part:]


def normalize_story_context(value: Any) -> dict[str, Any]:
    data = value if isinstance(value, dict) else {}
    list_keys = ("recurring_settings", "relationship_context", "continuity_rules")
    out: dict[str, Any] = {
        "story_summary": str(data.get("story_summary") or "").strip(),
        "title_brief": str(data.get("title_brief") or "").strip(),
        "genre": str(data.get("genre") or "").strip(),
        "era_and_world": str(data.get("era_and_world") or "").strip(),
        "primary_story_setting": str(data.get("primary_story_setting") or "").strip(),
    }
    for key in list_keys:
        values = data.get(key)
        out[key] = [str(x).strip() for x in values if str(x).strip()][:12] if isinstance(values, list) else []
    people = []
    for item in data.get("main_characters") or []:
        if not isinstance(item, dict) or not str(item.get("name") or "").strip():
            continue
        people.append({
            "name": str(item.get("name") or "").strip(),
            "aliases": [str(x).strip() for x in (item.get("aliases") or []) if str(x).strip()][:8],
            "role": str(item.get("role") or "").strip(),
        })
    out["main_characters"] = people[:30]
    return out


def highlight_request(segments: list[Segment], story_context: dict[str, Any], max_segments: int) -> str:
    compact_context = {k: v for k, v in story_context.items() if v}
    numbered = "\n".join(f"[{s.index}] {s.text}" for s in segments)
    return (
        "Global context (background constraints only):\n"
        + json.dumps(compact_context, ensure_ascii=False)
        + f"\n\nChoose one highlight using at most {max(1, min(3, int(max_segments or 3)))} adjacent units."
        + " Every selected index must come from the list below.\n\nNumbered narration units:\n"
        + numbered
    )


def normalize_highlight(value: Any, segments: list[Segment], max_segments: int = 3) -> dict[str, Any]:
    data = value if isinstance(value, dict) else {}
    allowed = {int(s.index): s for s in segments}
    raw_indexes = data.get("segment_indexes")
    if not isinstance(raw_indexes, list):
        raw_indexes = [data.get("segment_index")]
    indexes: list[int] = []
    for raw in raw_indexes:
        try:
            idx = int(raw)
        except Exception:
            continue
        if idx in allowed and idx not in indexes:
            indexes.append(idx)
    indexes = sorted(indexes)[: max(1, min(3, int(max_segments or 3)))]
    if indexes and any(b != a + 1 for a, b in zip(indexes, indexes[1:])):
        indexes = indexes[:1]
    if not indexes:
        return fallback_highlight(segments)
    selected_text = "".join(allowed[i].text for i in indexes)
    people = data.get("people")
    if not isinstance(people, list):
        people = []
    return {
        "segment_indexes": indexes,
        "highlight_text": selected_text,
        "people": [str(x).strip() for x in people if str(x).strip()][:6],
        "location": str(data.get("location") or "").strip(),
        "action": str(data.get("action") or "").strip(),
        "excluded_people": [str(x).strip() for x in (data.get("excluded_people") or []) if str(x).strip()][:8]
        if isinstance(data.get("excluded_people"), list) else [],
        "image_prompt_en": str(data.get("image_prompt_en") or "").strip(),
        "fallback_used": False,
    }


def fallback_highlight(segments: list[Segment]) -> dict[str, Any]:
    if not segments:
        return {"segment_indexes": [], "highlight_text": "", "people": [], "location": "", "action": "", "excluded_people": [], "image_prompt_en": "", "fallback_used": True}
    action_re = re.compile(r"(突然|冲|逃|倒|落|抓|推|打开|发现|出现|走进|站起|跪|喊|笑|哭|攻|救|変わ|現れ|落ち|倒れ|逃げ|叫|渡し|走|opened|found|entered|fell|ran|shouted)", re.I)
    quote_re = re.compile(r"[「『“\"]")
    exposition_re = re.compile(r"(つまり|一般には|と考えられ|によれば|という意味|历史|据说|换句话说|in other words|according to)", re.I)
    def score(seg: Segment) -> tuple[int, int]:
        text = seg.text
        value = 4 * len(action_re.findall(text)) + 2 * len(quote_re.findall(text)) - 3 * len(exposition_re.findall(text))
        value += 2 if 35 <= len(text) <= 180 else 0
        return value, -int(seg.index)
    chosen = max(segments, key=score)
    return {
        "segment_indexes": [int(chosen.index)],
        "highlight_text": chosen.text,
        "people": [], "location": "", "action": "", "excluded_people": [],
        "image_prompt_en": "", "fallback_used": True,
    }
