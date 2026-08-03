"""Seedance short-video canvas and tweet-hook bridge.

This module intentionally avoids a framework dependency. It runs a small
ThreadingHTTPServer so a Mac user can double-click one command file, paste copy,
set the Seedance API details, and produce a short hook video plus a manifest the
existing tweet/novel pipeline can consume.
"""
from __future__ import annotations

import argparse
import html
import json
import os
import re
import threading
import time
import traceback
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

import httpx
from PIL import Image, ImageDraw, ImageFont, ImageOps

from app.backends.image import ImageBackend
from app.config import DATA_DIR, ROOT
from app.utils.http import http_get, http_post


CANVAS_DIR = DATA_DIR / "seedance_canvas"
CANVAS_JOBS_DIR = CANVAS_DIR / "jobs"
SETTINGS_FILE = CANVAS_DIR / "settings.json"
TWEET_HOOK_INBOX = DATA_DIR / "tweet_hooks" / "inbox"

DEFAULT_BASE_URL = "https://ark.cn-beijing.volces.com/api/v3"
DEFAULT_MODEL = "doubao-seedance-2-0-fast-260128"

SUCCESS_STATUSES = {"succeeded", "success", "completed", "complete", "done"}
FAILED_STATUSES = {"failed", "error", "cancelled", "canceled", "rejected"}


DEFAULT_SETTINGS: dict[str, Any] = {
    "base_url": os.getenv("SEEDANCE_BASE_URL", DEFAULT_BASE_URL),
    "create_path": "/contents/generations/tasks",
    "retrieve_path": "/contents/generations/tasks/{task_id}",
    "payload_mode": "ark",
    "model": os.getenv("SEEDANCE_MODEL", DEFAULT_MODEL),
    "ratio": "9:16",
    "duration": 5,
    "resolution": "720p",
    "generate_audio": False,
    "watermark": True,
    "poll_interval": 5,
    "timeout_seconds": 900,
    "hook_style": "悬疑反转",
    "visual_style": "cinematic anime, high contrast, clean motion, dramatic lighting",
    "prompt_llm_provider": "openai",
    "prompt_llm_base_url": "",
    "prompt_llm_model": "",
    "prompt_llm_language": "English",
    "prompt_llm_temperature": 0.55,
    "prompt_llm_system": (
        "你是小说推文短视频的视觉提示词导演。"
        "把中文文章拆成可用于图片模型和视频模型的提示词。"
        "只输出 JSON，不要解释。"
    ),
    "image_provider": "custom",
    "image_base_url": "",
    "image_model": "",
    "image_width": 1024,
    "image_height": 1024,
    "image_timeout_seconds": 300,
    "character_count": 3,
    "grid_count": 9,
    "image_negative_prompt": (
        "lowres, bad anatomy, bad hands, text, watermark, logo, words, gore, blood, "
        "nudity, sexual content, explicit violence"
    ),
}


def _ensure_dirs() -> None:
    CANVAS_JOBS_DIR.mkdir(parents=True, exist_ok=True)
    TWEET_HOOK_INBOX.mkdir(parents=True, exist_ok=True)


def _utcish_now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _job_id() -> str:
    return "seedance_" + time.strftime("%Y%m%d_%H%M%S")


def _safe_slug(value: str, fallback: str = "hook") -> str:
    text = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", str(value or "").strip())
    text = re.sub(r"\s+", "_", text).strip("._")
    return (text[:60] or fallback)


def _redact(value: str) -> str:
    text = str(value or "")
    text = re.sub(r"(?i)(bearer\s+)[^\s\"']+", r"\1***", text)
    text = re.sub(r"sk-[A-Za-z0-9_-]{8,}", "sk-***", text)
    text = re.sub(r"(?i)(api[_-]?key[\"']?\s*[:=]\s*[\"']?)[^\"'\s,}]+", r"\1***", text)
    return text


def _load_settings() -> dict[str, Any]:
    data = dict(DEFAULT_SETTINGS)
    if SETTINGS_FILE.exists():
        try:
            saved = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            if isinstance(saved, dict):
                data.update({k: v for k, v in saved.items() if k != "api_key"})
        except Exception:
            pass
    return data


def _save_settings(values: dict[str, Any]) -> dict[str, Any]:
    current = _load_settings()
    allowed = set(DEFAULT_SETTINGS) | {"create_path", "retrieve_path", "payload_mode"}
    for key, value in values.items():
        if key in allowed:
            current[key] = value
    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS_FILE.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")
    return current


def _to_int(value: Any, default: int, minimum: int | None = None, maximum: int | None = None) -> int:
    try:
        number = int(float(value))
    except Exception:
        number = int(default)
    if minimum is not None:
        number = max(minimum, number)
    if maximum is not None:
        number = min(maximum, number)
    return number


def _clean_text(value: str, limit: int = 1800) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text[:limit].strip()


def _first_sentence(text: str, limit: int = 70) -> str:
    cleaned = _clean_text(text, 500)
    parts = re.split(r"(?<=[。！？!?；;])", cleaned)
    sentence = next((p.strip() for p in parts if p.strip()), cleaned)
    return sentence[:limit].strip(" ，,。.!！?？")


def _extract_conflict(text: str) -> str:
    cleaned = _clean_text(text, 900)
    patterns = [
        r"([^。！？!?]{4,40}(?:却|但|可是|然而|没想到|偏偏)[^。！？!?]{4,70})",
        r"([^。！？!?]{4,40}(?:发现|看见|听见|收到|醒来|重生|穿越)[^。！？!?]{4,70})",
        r"([^。！？!?]{4,80}(?:背叛|退婚|复仇|真相|秘密|死亡|失踪|系统)[^。！？!?]{0,50})",
    ]
    for pattern in patterns:
        match = re.search(pattern, cleaned)
        if match:
            return match.group(1).strip(" ，,。.!！?？")[:96]
    return _first_sentence(cleaned, 96)


def _pick_title(text: str, title: str = "") -> str:
    if title.strip():
        return title.strip()[:48]
    first_line = next((x.strip() for x in str(text or "").splitlines() if x.strip()), "")
    if len(first_line) <= 34:
        return first_line[:34] or "Seedance 推文钩子"
    return _first_sentence(text, 28) or "Seedance 推文钩子"


def _make_tweet_hook(script: str, title: str = "", style: str = "悬疑反转") -> dict[str, Any]:
    conflict = _extract_conflict(script)
    clean_title = _pick_title(script, title)
    if style == "强冲突":
        hook = f"他以为自己赢了，直到{conflict}"
    elif style == "情绪爆点":
        hook = f"最扎心的不是输，是{conflict}"
    elif style == "爽点反杀":
        hook = f"所有人都等他出丑，下一秒{conflict}"
    else:
        hook = f"如果这是真的，{conflict}"
    hook = re.sub(r"\s+", "", hook)[:42]
    thread = [
        hook,
        "这段开头先把矛盾压到最紧，视频里只放第一个反转点。",
        "真正的爽点在后面，适合接正文长推或小说推文完整流水线。",
    ]
    return {
        "title": clean_title,
        "hook": hook,
        "thread": thread,
        "cta": "先看这 5 秒，后面才是反杀开始。",
        "hashtags": ["#小说推文", "#AI视频", "#短视频钩子"],
        "style": style,
        "conflict": conflict,
    }


def _make_seedance_prompt(script: str, hook: dict[str, Any], settings: dict[str, Any]) -> str:
    duration = _to_int(settings.get("duration"), 5, 4, 15)
    ratio = str(settings.get("ratio") or "9:16")
    resolution = str(settings.get("resolution") or "720p")
    visual_style = str(settings.get("visual_style") or DEFAULT_SETTINGS["visual_style"]).strip()
    conflict = hook.get("conflict") or _extract_conflict(script)
    excerpt = _clean_text(script, 650)
    return (
        f"{duration}-second vertical {ratio} short-video opening hook for a Chinese novel tweet thread. "
        f"Visual style: {visual_style}. "
        "Use a cinematic three-beat micro story: "
        "0-2s, a tense close-up of an adult protagonist sensing something is wrong; "
        "2-4s, reveal the conflict through symbolic action and fast camera push-in; "
        f"final beat, hold on an unresolved cliffhanger based on this story conflict: {conflict}. "
        "Smooth motion, clear subject, expressive lighting, no readable text, no subtitles, no logos, no watermark-like typography. "
        f"Story context: {excerpt}. "
        f"Output target: {resolution}, social-media hook, dramatic but non-graphic."
    )


def build_plan(payload: dict[str, Any]) -> dict[str, Any]:
    script = str(payload.get("script") or payload.get("text") or "").strip()
    title = str(payload.get("title") or "").strip()
    settings = dict(_load_settings())
    settings.update({k: v for k, v in payload.items() if k in DEFAULT_SETTINGS})
    hook = _make_tweet_hook(script, title=title, style=str(settings.get("hook_style") or "悬疑反转"))
    prompt = str(payload.get("prompt") or "").strip() or _make_seedance_prompt(script, hook, settings)
    nodes = [
        {
            "id": "copy",
            "title": "文案",
            "kind": "input",
            "x": 40,
            "y": 60,
            "body": _clean_text(script, 260) or "等待输入文案",
        },
        {
            "id": "hook",
            "title": "推文钩子",
            "kind": "tweet",
            "x": 410,
            "y": 150,
            "body": hook["hook"],
        },
        {
            "id": "prompt",
            "title": "Seedance Prompt",
            "kind": "prompt",
            "x": 790,
            "y": 65,
            "body": prompt[:360],
        },
        {
            "id": "character",
            "title": "人设图",
            "kind": "image",
            "x": 1170,
            "y": 35,
            "body": "character references",
        },
        {
            "id": "grid",
            "title": "九宫图",
            "kind": "grid",
            "x": 1170,
            "y": 355,
            "body": "nine-panel visual beats",
        },
        {
            "id": "video",
            "title": "短视频",
            "kind": "output",
            "x": 1570,
            "y": 170,
            "body": "ready",
        },
    ]
    return {"hook": hook, "prompt": prompt, "nodes": nodes, "settings": settings}


def _json_from_text(text: str) -> dict[str, Any]:
    raw = str(text or "").strip()
    if not raw:
        raise ValueError("empty LLM response")
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.I).strip()
        raw = re.sub(r"\s*```$", "", raw).strip()
    if not raw.startswith("{"):
        start = raw.find("{")
        end = raw.rfind("}")
        if start >= 0 and end > start:
            raw = raw[start : end + 1]
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("LLM response JSON must be an object")
    return data


def _list_strings(value: Any, limit: int) -> list[str]:
    if isinstance(value, str):
        rows = [x.strip(" -0123456789.、") for x in value.splitlines()]
    elif isinstance(value, list):
        rows = []
        for item in value:
            if isinstance(item, dict):
                rows.append(str(item.get("prompt") or item.get("text") or item.get("description") or "").strip())
            else:
                rows.append(str(item or "").strip())
    else:
        rows = []
    return [x for x in rows if x][:limit]


def _fallback_prompt_bundle(script: str, title: str = "", language: str = "English") -> dict[str, Any]:
    hook = _make_tweet_hook(script, title=title)
    conflict = hook["conflict"]
    context = _clean_text(script, 700)
    suffix = (
        "cinematic anime, adult-looking characters, consistent character design, "
        "dramatic lighting, clean composition, no readable text, no watermark"
    )
    character_prompts = [
        f"Main protagonist character sheet, full body and close-up, determined expression, story conflict: {conflict}, {suffix}",
        f"Opposing character design, elegant but suspicious, subtle hostile body language, story context: {context[:180]}, {suffix}",
        f"Supporting character design, worried witness, modern fantasy novel mood, {suffix}",
    ]
    grid_prompts = []
    beats = [
        "public humiliation setup",
        "protagonist turns away",
        "hidden system awakens",
        "crowd freezes",
        "villain gives a subtle signal",
        "protagonist notices the real enemy",
        "memory flash of betrayal",
        "camera pushes into the protagonist's eyes",
        "cliffhanger freeze frame before the counterattack",
    ]
    for beat in beats:
        grid_prompts.append(f"{beat}, based on: {conflict}, vertical short-video storyboard frame, {suffix}")
    video_prompt = _make_seedance_prompt(script, hook, {**DEFAULT_SETTINGS, "prompt_llm_language": language})
    return {
        "title": hook["title"],
        "hook": hook["hook"],
        "video_prompt": video_prompt,
        "character_prompts": character_prompts,
        "grid_prompts": grid_prompts,
        "negative_prompt": DEFAULT_SETTINGS["image_negative_prompt"],
        "source": "fallback",
        "language": language,
    }


def _prompt_llm_payload(script: str, title: str, settings: dict[str, Any]) -> dict[str, Any]:
    language = str(settings.get("prompt_llm_language") or "English")
    system_prompt = str(settings.get("prompt_llm_system") or DEFAULT_SETTINGS["prompt_llm_system"])
    user_prompt = (
        f"标题：{title or '无'}\n"
        f"文章：{_clean_text(script, 6000)}\n\n"
        f"请用 {language} 输出 JSON，字段必须包含：\n"
        "title: string\n"
        "hook: string，中文推文钩子，不超过 42 个中文字符\n"
        "video_prompt: string，给 Seedance 视频模型的短视频提示词\n"
        "character_prompts: string[]，3 条人设图提示词，要求角色一致、成人、无文字\n"
        "grid_prompts: string[]，9 条九宫图分镜提示词，按剧情推进\n"
        "negative_prompt: string，图片反向提示词\n"
        "不要输出 markdown。"
    )
    return {
        "model": str(settings.get("prompt_llm_model") or ""),
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": float(settings.get("prompt_llm_temperature") or 0.55),
        "max_tokens": 1800,
    }


def build_prompt_bundle(payload: dict[str, Any]) -> dict[str, Any]:
    script = str(payload.get("script") or payload.get("text") or "").strip()
    title = str(payload.get("title") or "").strip()
    settings = dict(_load_settings())
    settings.update({k: v for k, v in payload.items() if k in DEFAULT_SETTINGS})
    language = str(settings.get("prompt_llm_language") or "English")
    base_url = str(payload.get("prompt_llm_base_url") or settings.get("prompt_llm_base_url") or "").strip().rstrip("/")
    api_key = str(payload.get("prompt_llm_api_key") or os.getenv("PROMPT_LLM_API_KEY") or "").strip()
    model = str(payload.get("prompt_llm_model") or settings.get("prompt_llm_model") or "").strip()
    if not script:
        raise ValueError("请先填写文章/文案")
    if not (base_url and model):
        return _fallback_prompt_bundle(script, title=title, language=language)

    provider = str(settings.get("prompt_llm_provider") or "openai")
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    try:
        if provider == "claude":
            response = http_post(
                _join_url(base_url, "/messages"),
                json={
                    "model": model,
                    "max_tokens": 1800,
                    "system": str(settings.get("prompt_llm_system") or DEFAULT_SETTINGS["prompt_llm_system"]),
                    "messages": [{"role": "user", "content": _prompt_llm_payload(script, title, {**settings, "prompt_llm_model": model})["messages"][1]["content"]}],
                },
                headers={
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                    "Content-Type": "application/json",
                },
                timeout=90,
            )
            response.raise_for_status()
            text = response.json()["content"][0]["text"]
        else:
            body = _prompt_llm_payload(script, title, {**settings, "prompt_llm_model": model})
            response = http_post(_join_url(base_url, "/chat/completions"), json=body, headers=headers, timeout=90)
            response.raise_for_status()
            text = response.json()["choices"][0]["message"]["content"]
        data = _json_from_text(text)
    except Exception as exc:
        fallback = _fallback_prompt_bundle(script, title=title, language=language)
        fallback["source"] = "fallback_after_llm_error"
        fallback["llm_error"] = _redact(str(exc))
        return fallback

    fallback = _fallback_prompt_bundle(script, title=title, language=language)
    character_prompts = _list_strings(data.get("character_prompts"), _to_int(settings.get("character_count"), 3, 1, 8))
    grid_prompts = _list_strings(data.get("grid_prompts"), 9)
    return {
        "title": str(data.get("title") or fallback["title"]).strip(),
        "hook": str(data.get("hook") or fallback["hook"]).strip(),
        "video_prompt": str(data.get("video_prompt") or fallback["video_prompt"]).strip(),
        "character_prompts": character_prompts or fallback["character_prompts"],
        "grid_prompts": (grid_prompts + fallback["grid_prompts"])[:9],
        "negative_prompt": str(data.get("negative_prompt") or fallback["negative_prompt"]).strip(),
        "source": "llm",
        "language": language,
    }


def _image_backend_from_request(request: dict[str, Any]) -> ImageBackend:
    settings = dict(_load_settings())
    settings.update({k: v for k, v in request.items() if k in DEFAULT_SETTINGS})
    provider = str(request.get("image_provider") or settings.get("image_provider") or "custom")
    api_key = str(request.get("image_api_key") or os.getenv("IMAGE_API_KEY") or "").strip()
    if provider not in {"placeholder", "custom", "openai", "openai_legacy", "sdwebui", "comfyui", "replicate", "aliyun"} and not api_key:
        raise RuntimeError("图片模型缺少 API Key；可在页面填写，或设置 IMAGE_API_KEY。")
    if provider in {"custom", "openai", "openai_legacy", "replicate", "aliyun"} and not api_key:
        raise RuntimeError("图片模型缺少 API Key；可在页面填写，或设置 IMAGE_API_KEY。")
    return ImageBackend(
        provider=provider,
        base_url=str(request.get("image_base_url") or settings.get("image_base_url") or ""),
        api_key=api_key,
        model=str(request.get("image_model") or settings.get("image_model") or ""),
        timeout_seconds=_to_int(settings.get("image_timeout_seconds"), 300, 60, 1800),
    )


def _media_url(job_id: str, path: Path) -> str:
    return f"/media/{job_id}/{path.name}"


def _compose_nine_grid(paths: list[Path], out_path: Path, cell: int = 512) -> Path:
    cols = rows = 3
    canvas = Image.new("RGB", (cols * cell, rows * cell), (245, 247, 250))
    for index, path in enumerate(paths[:9]):
        img = Image.open(path)
        img = ImageOps.fit(img.convert("RGB"), (cell, cell), method=Image.LANCZOS)
        x = index % cols * cell
        y = index // cols * cell
        canvas.paste(img, (x, y))
    draw = ImageDraw.Draw(canvas)
    try:
        font = ImageFont.truetype("PingFang.ttc", 22)
    except Exception:
        font = ImageFont.load_default()
    for i in range(1, 3):
        draw.line((i * cell, 0, i * cell, rows * cell), fill=(255, 255, 255), width=8)
        draw.line((0, i * cell, cols * cell, i * cell), fill=(255, 255, 255), width=8)
    for index in range(min(9, len(paths))):
        x = index % cols * cell + 16
        y = index // cols * cell + 14
        draw.rounded_rectangle((x - 7, y - 5, x + 40, y + 30), radius=8, fill=(0, 0, 0))
        draw.text((x, y), f"{index + 1}", fill=(255, 255, 255), font=font)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(out_path, "JPEG", quality=92)
    return out_path


def _join_url(base_url: str, path: str) -> str:
    base = str(base_url or DEFAULT_BASE_URL).rstrip("/")
    tail = str(path or "").strip()
    if not tail:
        return base
    if tail.startswith("http://") or tail.startswith("https://"):
        return tail
    return base + "/" + tail.lstrip("/")


def _nested_get(data: Any, dotted: str) -> Any:
    cur = data
    for key in dotted.split("."):
        if isinstance(cur, dict):
            cur = cur.get(key)
        else:
            cur = getattr(cur, key, None)
        if cur is None:
            return None
    return cur


def _first_value(data: Any, keys: list[str]) -> Any:
    for key in keys:
        value = _nested_get(data, key)
        if value not in (None, ""):
            return value
    return None


def _find_video_url(value: Any) -> str:
    if isinstance(value, str):
        text = value.strip()
        if text.startswith("http") and (
            ".mp4" in text.lower()
            or ".mov" in text.lower()
            or "video" in text.lower()
            or "tos" in text.lower()
        ):
            return text
        return ""
    if isinstance(value, list):
        for item in value:
            found = _find_video_url(item)
            if found:
                return found
    if isinstance(value, dict):
        priority = [
            "video_url",
            "videoUrl",
            "url",
            "download_url",
            "downloadUrl",
            "output_url",
            "outputUrl",
        ]
        for key in priority:
            if key in value:
                found = _find_video_url(value.get(key))
                if found:
                    return found
        for item in value.values():
            found = _find_video_url(item)
            if found:
                return found
    return ""


def _response_json(response: httpx.Response) -> dict[str, Any]:
    try:
        data = response.json()
        if isinstance(data, dict):
            return data
        return {"data": data}
    except Exception:
        return {"raw": response.text}


class SeedanceHttpClient:
    def __init__(self, settings: dict[str, Any], api_key: str):
        self.settings = settings
        self.api_key = api_key.strip()
        self.headers = {"Content-Type": "application/json"}
        if self.api_key:
            self.headers["Authorization"] = f"Bearer {self.api_key}"

    def create_task(self, prompt: str) -> tuple[str, dict[str, Any]]:
        url = _join_url(str(self.settings.get("base_url") or DEFAULT_BASE_URL), str(self.settings.get("create_path") or ""))
        payload = self._create_payload(prompt)
        response = http_post(url, json=payload, headers=self.headers, timeout=60)
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            detail = _redact(response.text[:1200])
            raise RuntimeError(f"Seedance create failed HTTP {response.status_code}: {detail}") from exc
        data = _response_json(response)
        task_id = str(
            _first_value(
                data,
                [
                    "id",
                    "task_id",
                    "taskId",
                    "data.id",
                    "data.task_id",
                    "data.taskId",
                    "result.id",
                    "result.task_id",
                ],
            )
            or ""
        )
        if not task_id:
            raise RuntimeError(f"Seedance create response has no task id: {_redact(json.dumps(data, ensure_ascii=False)[:900])}")
        return task_id, data

    def get_task(self, task_id: str) -> dict[str, Any]:
        retrieve_path = str(self.settings.get("retrieve_path") or "/contents/generations/tasks/{task_id}")
        path = retrieve_path.replace("{task_id}", task_id)
        url = _join_url(str(self.settings.get("base_url") or DEFAULT_BASE_URL), path)
        response = http_get(url, headers=self.headers, timeout=60)
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            detail = _redact(response.text[:1200])
            raise RuntimeError(f"Seedance retrieve failed HTTP {response.status_code}: {detail}") from exc
        return _response_json(response)

    def _create_payload(self, prompt: str) -> dict[str, Any]:
        mode = str(self.settings.get("payload_mode") or "ark")
        duration = _to_int(self.settings.get("duration"), 5, 4, 15)
        resolution = str(self.settings.get("resolution") or "720p")
        ratio = str(self.settings.get("ratio") or "9:16")
        model = str(self.settings.get("model") or DEFAULT_MODEL)
        if mode == "seedance_v2":
            return {
                "model": model,
                "prompt": prompt,
                "duration": duration,
                "resolution": resolution,
                "aspect_ratio": ratio,
            }
        if mode == "ark_text_flags":
            flagged_prompt = f"{prompt} --resolution {resolution} --duration {duration}"
            return {
                "model": model,
                "content": [{"type": "text", "text": flagged_prompt}],
            }
        return {
            "model": model,
            "content": [{"type": "text", "text": prompt}],
            "generate_audio": bool(self.settings.get("generate_audio", False)),
            "ratio": ratio,
            "duration": duration,
            "watermark": bool(self.settings.get("watermark", True)),
            "resolution": resolution,
        }


def _write_status(job_dir: Path, **updates: Any) -> dict[str, Any]:
    current: dict[str, Any] = {}
    path = job_dir / "status.json"
    if path.exists():
        try:
            current = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            current = {}
    current.update(updates)
    current["updated_at"] = _utcish_now()
    path.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")
    return current


def _load_status(job_id: str) -> dict[str, Any]:
    path = CANVAS_JOBS_DIR / job_id / "status.json"
    if not path.exists():
        return {"job_id": job_id, "stage": "missing", "progress": 0}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"job_id": job_id, "stage": "broken", "progress": 0, "error": str(exc)}


def _download_video(video_url: str, out_path: Path) -> None:
    response = http_get(video_url, timeout=300, follow_redirects=True)
    response.raise_for_status()
    out_path.write_bytes(response.content)


def _publish_tweet_manifest(job_dir: Path, manifest: dict[str, Any]) -> Path:
    TWEET_HOOK_INBOX.mkdir(parents=True, exist_ok=True)
    path = TWEET_HOOK_INBOX / f"{manifest['job_id']}.json"
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    (job_dir / "tweet_hook.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def run_seedance_job(job_id: str, request: dict[str, Any]) -> None:
    _ensure_dirs()
    job_dir = CANVAS_JOBS_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    script = str(request.get("script") or request.get("text") or "").strip()
    title = str(request.get("title") or "").strip()
    settings = dict(_load_settings())
    settings.update({k: v for k, v in request.items() if k in set(DEFAULT_SETTINGS) | {"create_path", "retrieve_path", "payload_mode"}})
    api_key = str(request.get("api_key") or os.getenv("SEEDANCE_API_KEY") or os.getenv("ARK_API_KEY") or "").strip()
    dry_run = bool(request.get("dry_run", False))
    plan = build_plan({"script": script, "title": title, **settings})
    prompt = str(request.get("prompt") or plan["prompt"])
    hook = plan["hook"]

    safe_request = dict(request)
    if "api_key" in safe_request:
        safe_request["api_key"] = "***" if safe_request["api_key"] else ""
    (job_dir / "request.json").write_text(json.dumps(safe_request, ensure_ascii=False, indent=2), encoding="utf-8")
    (job_dir / "script.txt").write_text(script, encoding="utf-8")
    (job_dir / "seedance_prompt.txt").write_text(prompt, encoding="utf-8")
    (job_dir / "plan.json").write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")

    _write_status(
        job_dir,
        job_id=job_id,
        stage="planning",
        progress=0.05,
        title=hook.get("title", title),
        hook=hook.get("hook", ""),
        job_path=str(job_dir),
        started_at=_utcish_now(),
    )
    try:
        if dry_run:
            manifest = _build_manifest(
                job_id=job_id,
                job_dir=job_dir,
                hook=hook,
                prompt=prompt,
                settings=settings,
                task_id="dry-run",
                video_url="",
                video_path="",
                stage="dry_run",
            )
            inbox_path = _publish_tweet_manifest(job_dir, manifest)
            _write_status(
                job_dir,
                stage="dry_run",
                progress=1.0,
                manifest=str(job_dir / "tweet_hook.json"),
                tweet_hook_inbox=str(inbox_path),
                finished_at=_utcish_now(),
            )
            return

        if not api_key:
            raise RuntimeError("缺少 Seedance API Key。可在页面填写，或设置 SEEDANCE_API_KEY / ARK_API_KEY。")

        client = SeedanceHttpClient(settings, api_key)
        _write_status(job_dir, stage="submitting", progress=0.12)
        task_id, create_response = client.create_task(prompt)
        (job_dir / "create_response.json").write_text(json.dumps(create_response, ensure_ascii=False, indent=2), encoding="utf-8")
        _write_status(job_dir, stage="polling", progress=0.20, task_id=task_id)

        deadline = time.monotonic() + _to_int(settings.get("timeout_seconds"), 900, 60, 7200)
        interval = _to_int(settings.get("poll_interval"), 5, 2, 60)
        last_response: dict[str, Any] = {}
        while time.monotonic() < deadline:
            last_response = client.get_task(task_id)
            (job_dir / "last_task_response.json").write_text(json.dumps(last_response, ensure_ascii=False, indent=2), encoding="utf-8")
            status = str(
                _first_value(
                    last_response,
                    ["status", "data.status", "result.status", "task.status", "state", "data.state", "result.state"],
                )
                or ""
            ).lower()
            progress = float(_first_value(last_response, ["progress", "data.progress", "result.progress"]) or 0)
            normalized_progress = 0.20 + min(0.65, max(0.0, progress / 100.0 * 0.65 if progress > 1 else progress * 0.65))
            _write_status(job_dir, stage=f"polling:{status or 'unknown'}", progress=normalized_progress, task_id=task_id)
            if status in SUCCESS_STATUSES:
                break
            if status in FAILED_STATUSES:
                raise RuntimeError(f"Seedance task failed: {_redact(json.dumps(last_response, ensure_ascii=False)[:1200])}")
            time.sleep(interval)
        else:
            raise TimeoutError(f"Seedance task timed out after {settings.get('timeout_seconds')} seconds")

        video_url = _find_video_url(last_response)
        if not video_url:
            raise RuntimeError(f"Seedance task succeeded but no video url was found: {_redact(json.dumps(last_response, ensure_ascii=False)[:1200])}")
        out_path = job_dir / f"{_safe_slug(hook.get('title') or title)}.mp4"
        _write_status(job_dir, stage="downloading", progress=0.90, task_id=task_id, video_url=video_url)
        _download_video(video_url, out_path)
        manifest = _build_manifest(
            job_id=job_id,
            job_dir=job_dir,
            hook=hook,
            prompt=prompt,
            settings=settings,
            task_id=task_id,
            video_url=video_url,
            video_path=str(out_path),
            stage="completed",
        )
        inbox_path = _publish_tweet_manifest(job_dir, manifest)
        (job_dir / "result.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        _write_status(
            job_dir,
            stage="completed",
            progress=1.0,
            task_id=task_id,
            video=str(out_path),
            media_url=f"/media/{job_id}/{out_path.name}",
            manifest=str(job_dir / "tweet_hook.json"),
            tweet_hook_inbox=str(inbox_path),
            finished_at=_utcish_now(),
        )
    except Exception as exc:
        (job_dir / "error.txt").write_text(f"{exc}\n\n{traceback.format_exc()}", encoding="utf-8")
        _write_status(job_dir, stage="failed", progress=0, error=str(exc), finished_at=_utcish_now())


def run_reference_images_job(job_id: str, request: dict[str, Any]) -> None:
    _ensure_dirs()
    job_dir = CANVAS_JOBS_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    mode = str(request.get("mode") or "all")
    dry_run = bool(request.get("dry_run", False))
    safe_request = dict(request)
    for key in ("api_key", "prompt_llm_api_key", "image_api_key"):
        if key in safe_request:
            safe_request[key] = "***" if safe_request[key] else ""
    (job_dir / "request.json").write_text(json.dumps(safe_request, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        _write_status(job_dir, job_id=job_id, stage="prompting", progress=0.05, job_path=str(job_dir), started_at=_utcish_now())
        prompt_bundle = request.get("prompt_bundle")
        if not isinstance(prompt_bundle, dict):
            prompt_bundle = build_prompt_bundle(request)
        (job_dir / "prompt_bundle.json").write_text(json.dumps(prompt_bundle, ensure_ascii=False, indent=2), encoding="utf-8")

        if dry_run:
            result = {
                "kind": "reference_images",
                "job_id": job_id,
                "stage": "dry_run",
                "prompt_bundle": prompt_bundle,
                "artifacts": {"job_dir": str(job_dir), "prompt_bundle": str(job_dir / "prompt_bundle.json")},
            }
            (job_dir / "reference_images.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            _write_status(job_dir, stage="dry_run", progress=1.0, result=str(job_dir / "reference_images.json"), finished_at=_utcish_now())
            return

        backend = _image_backend_from_request(request)
        width = _to_int(request.get("image_width") or _load_settings().get("image_width"), 1024, 256, 2048)
        height = _to_int(request.get("image_height") or _load_settings().get("image_height"), 1024, 256, 2048)
        negative = str(prompt_bundle.get("negative_prompt") or request.get("image_negative_prompt") or DEFAULT_SETTINGS["image_negative_prompt"])
        character_prompts = _list_strings(prompt_bundle.get("character_prompts"), _to_int(request.get("character_count") or _load_settings().get("character_count"), 3, 1, 8))
        grid_prompts = _list_strings(prompt_bundle.get("grid_prompts"), 9)
        character_images: list[Path] = []
        grid_images: list[Path] = []
        total = (len(character_prompts) if mode in {"all", "character", "characters"} else 0) + (len(grid_prompts[:9]) if mode in {"all", "grid", "nine_grid"} else 0)
        done = 0

        if mode in {"all", "character", "characters"}:
            for index, prompt in enumerate(character_prompts, start=1):
                out = job_dir / f"character_{index:02d}.png"
                _write_status(job_dir, stage=f"character:{index}", progress=0.10 + (done / max(1, total)) * 0.80)
                backend.generate(prompt, negative, out, width=width, height=height)
                character_images.append(out)
                done += 1

        if mode in {"all", "grid", "nine_grid"}:
            for index, prompt in enumerate(grid_prompts[:9], start=1):
                out = job_dir / f"grid_{index:02d}.png"
                _write_status(job_dir, stage=f"grid:{index}", progress=0.10 + (done / max(1, total)) * 0.80)
                backend.generate(prompt, negative, out, width=width, height=height)
                grid_images.append(out)
                done += 1

        nine_grid = None
        if grid_images:
            nine_grid = _compose_nine_grid(grid_images, job_dir / "nine_grid.jpg")
        result = {
            "kind": "reference_images",
            "version": 1,
            "job_id": job_id,
            "stage": "completed",
            "created_at": _utcish_now(),
            "prompt_bundle": prompt_bundle,
            "artifacts": {
                "job_dir": str(job_dir),
                "prompt_bundle": str(job_dir / "prompt_bundle.json"),
                "character_images": [str(p) for p in character_images],
                "grid_images": [str(p) for p in grid_images],
                "nine_grid": str(nine_grid or ""),
            },
        }
        (job_dir / "reference_images.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        character_media = [_media_url(job_id, p) for p in character_images]
        grid_media = [_media_url(job_id, p) for p in grid_images]
        _write_status(
            job_dir,
            stage="completed",
            progress=1.0,
            result=str(job_dir / "reference_images.json"),
            character_media=character_media,
            grid_media=grid_media,
            nine_grid_media=_media_url(job_id, nine_grid) if nine_grid else "",
            gallery=[*character_media, *grid_media],
            finished_at=_utcish_now(),
        )
    except Exception as exc:
        (job_dir / "error.txt").write_text(f"{exc}\n\n{traceback.format_exc()}", encoding="utf-8")
        _write_status(job_dir, stage="failed", progress=0, error=str(exc), finished_at=_utcish_now())


def _build_manifest(
    *,
    job_id: str,
    job_dir: Path,
    hook: dict[str, Any],
    prompt: str,
    settings: dict[str, Any],
    task_id: str,
    video_url: str,
    video_path: str,
    stage: str,
) -> dict[str, Any]:
    return {
        "kind": "tweet_hook_video",
        "version": 1,
        "job_id": job_id,
        "stage": stage,
        "created_at": _utcish_now(),
        "title": hook.get("title", ""),
        "hook": hook.get("hook", ""),
        "tweet_thread": hook.get("thread", []),
        "cta": hook.get("cta", ""),
        "hashtags": hook.get("hashtags", []),
        "seedance": {
            "task_id": task_id,
            "model": settings.get("model", ""),
            "base_url": settings.get("base_url", ""),
            "ratio": settings.get("ratio", ""),
            "duration": settings.get("duration", ""),
            "resolution": settings.get("resolution", ""),
            "prompt": prompt,
            "video_url": video_url,
        },
        "artifacts": {
            "job_dir": str(job_dir),
            "video": video_path,
            "prompt": str(job_dir / "seedance_prompt.txt"),
            "script": str(job_dir / "script.txt"),
            "manifest": str(job_dir / "tweet_hook.json"),
        },
        "pipeline_hint": {
            "entrypoint": "data/tweet_hooks/inbox",
            "contract": "Read this JSON, use artifacts.video as the short hook clip, then place hook/tweet_thread before the main tweet copy.",
        },
    }


def start_background_job(request: dict[str, Any], kind: str = "seedance") -> str:
    _ensure_dirs()
    base_id = _job_id() if kind == "seedance" else f"{kind}_{time.strftime('%Y%m%d_%H%M%S')}"
    job_id = base_id
    seq = 1
    while (CANVAS_JOBS_DIR / job_id).exists():
        seq += 1
        job_id = f"{base_id}_{seq:02d}"
    job_dir = CANVAS_JOBS_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    _write_status(job_dir, job_id=job_id, stage="queued", progress=0, job_path=str(job_dir), created_at=_utcish_now())
    target = run_reference_images_job if kind in {"refs", "reference"} else run_seedance_job
    thread = threading.Thread(target=target, args=(job_id, request), daemon=True, name=f"{kind}-{job_id}")
    thread.start()
    return job_id


def _list_recent_jobs(limit: int = 30) -> list[dict[str, Any]]:
    _ensure_dirs()
    rows = []
    for path in sorted([p for p in CANVAS_JOBS_DIR.iterdir() if p.is_dir()], reverse=True):
        rows.append(_load_status(path.name))
        if len(rows) >= limit:
            break
    return rows


class SeedanceCanvasHandler(BaseHTTPRequestHandler):
    server_version = "SeedanceCanvas/1.0"

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/":
            self._send_html(INDEX_HTML)
            return
        if path == "/api/settings":
            self._send_json({"settings": _load_settings(), "root": str(ROOT)})
            return
        if path == "/api/jobs":
            self._send_json({"jobs": _list_recent_jobs()})
            return
        if path.startswith("/api/jobs/"):
            job_id = _safe_slug(unquote(path.rsplit("/", 1)[-1]), "missing")
            self._send_json(_load_status(job_id))
            return
        if path.startswith("/media/"):
            self._send_media(path)
            return
        self.send_error(HTTPStatus.NOT_FOUND, "not found")

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        try:
            payload = self._read_json()
            if parsed.path == "/api/settings":
                self._send_json({"settings": _save_settings(payload)})
                return
            if parsed.path == "/api/plan":
                self._send_json(build_plan(payload))
                return
            if parsed.path == "/api/ai_prompts":
                self._send_json(build_prompt_bundle(payload))
                return
            if parsed.path == "/api/reference_images":
                job_id = start_background_job(payload, kind="refs")
                self._send_json(
                    {
                        "job_id": job_id,
                        "status_url": f"/api/jobs/{job_id}",
                        "job_dir": str(CANVAS_JOBS_DIR / job_id),
                    }
                )
                return
            if parsed.path in {"/api/generate", "/api/tweet_hook"}:
                job_id = start_background_job(payload)
                self._send_json(
                    {
                        "job_id": job_id,
                        "status_url": f"/api/jobs/{job_id}",
                        "job_dir": str(CANVAS_JOBS_DIR / job_id),
                        "tweet_hook_inbox": str(TWEET_HOOK_INBOX / f"{job_id}.json"),
                    }
                )
                return
            self.send_error(HTTPStatus.NOT_FOUND, "not found")
        except Exception as exc:
            self._send_json({"error": str(exc), "traceback": traceback.format_exc()}, status=500)

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"[seedance-canvas] {self.address_string()} - {fmt % args}")

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0") or 0)
        data = self.rfile.read(length) if length else b"{}"
        if not data:
            return {}
        parsed = json.loads(data.decode("utf-8"))
        if not isinstance(parsed, dict):
            raise ValueError("JSON body must be an object")
        return parsed

    def _send_json(self, data: Any, status: int = 200) -> None:
        body = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, source: str) -> None:
        body = source.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_media(self, path: str) -> None:
        parts = [p for p in path.split("/") if p]
        if len(parts) < 3:
            self.send_error(HTTPStatus.NOT_FOUND, "bad media path")
            return
        job_id = _safe_slug(parts[1], "missing")
        filename = unquote(parts[2])
        file_path = (CANVAS_JOBS_DIR / job_id / filename).resolve()
        root = (CANVAS_JOBS_DIR / job_id).resolve()
        if root not in file_path.parents and file_path != root:
            self.send_error(HTTPStatus.FORBIDDEN, "forbidden")
            return
        if not file_path.exists() or not file_path.is_file():
            self.send_error(HTTPStatus.NOT_FOUND, "missing media")
            return
        content_type = "video/mp4" if file_path.suffix.lower() == ".mp4" else "application/octet-stream"
        body = file_path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


INDEX_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Seedance 无限画布</title>
<style>
:root {
  color-scheme: light;
  --ink: #17202a;
  --muted: #627084;
  --line: #d8dee8;
  --panel: #f7f9fb;
  --panel-strong: #ffffff;
  --accent: #087f8c;
  --accent-ink: #ffffff;
  --warn: #9a5b00;
  --bad: #a53232;
  --ok: #176c44;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  color: var(--ink);
  background: #eef2f6;
  font-family: -apple-system, BlinkMacSystemFont, "PingFang SC", "Segoe UI", sans-serif;
  height: 100vh;
  overflow: hidden;
}
button, input, textarea, select {
  font: inherit;
}
.app {
  display: grid;
  grid-template-columns: minmax(330px, 390px) 1fr;
  height: 100vh;
}
.sidebar {
  background: var(--panel-strong);
  border-right: 1px solid var(--line);
  padding: 16px;
  overflow: auto;
}
.brand {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 14px;
}
.brand h1 {
  font-size: 20px;
  line-height: 1.15;
  margin: 0;
  letter-spacing: 0;
}
.pill {
  border: 1px solid var(--line);
  color: var(--muted);
  padding: 4px 8px;
  border-radius: 999px;
  font-size: 12px;
  white-space: nowrap;
}
.field {
  display: grid;
  gap: 6px;
  margin: 10px 0;
}
label {
  color: #334155;
  font-size: 13px;
  font-weight: 600;
}
textarea, input, select {
  width: 100%;
  border: 1px solid var(--line);
  border-radius: 8px;
  background: #fff;
  color: var(--ink);
  padding: 9px 10px;
  outline: none;
}
textarea:focus, input:focus, select:focus {
  border-color: var(--accent);
  box-shadow: 0 0 0 3px rgba(8, 127, 140, .13);
}
textarea {
  min-height: 180px;
  resize: vertical;
  line-height: 1.55;
}
#customPrompt {
  min-height: 118px;
}
.row {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 8px;
}
.row3 {
  display: grid;
  grid-template-columns: 1fr 1fr 1fr;
  gap: 8px;
}
.checks {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 8px;
  margin-top: 10px;
}
.check {
  display: flex;
  align-items: center;
  gap: 8px;
  border: 1px solid var(--line);
  border-radius: 8px;
  padding: 8px 10px;
  background: #fff;
  color: #334155;
  font-size: 13px;
}
.check input {
  width: auto;
}
.actions {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 8px;
  margin: 14px 0;
}
button {
  border: 1px solid var(--line);
  border-radius: 8px;
  background: #fff;
  color: var(--ink);
  padding: 10px 12px;
  cursor: pointer;
  min-height: 40px;
}
button.primary {
  background: var(--accent);
  color: var(--accent-ink);
  border-color: var(--accent);
  font-weight: 700;
}
button:disabled {
  opacity: .58;
  cursor: wait;
}
.status {
  border: 1px solid var(--line);
  border-radius: 8px;
  background: #fbfcfe;
  padding: 10px;
  min-height: 72px;
  color: var(--muted);
  white-space: pre-wrap;
  word-break: break-word;
}
.canvas-shell {
  position: relative;
  overflow: hidden;
  background:
    linear-gradient(#dce4ee 1px, transparent 1px),
    linear-gradient(90deg, #dce4ee 1px, transparent 1px),
    #edf2f7;
  background-size: 28px 28px;
}
.toolbar {
  position: absolute;
  top: 14px;
  left: 14px;
  right: 14px;
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 10px;
  z-index: 5;
  pointer-events: none;
}
.toolbar > * {
  pointer-events: auto;
}
.mini {
  display: flex;
  align-items: center;
  gap: 8px;
  border: 1px solid rgba(216, 222, 232, .95);
  border-radius: 8px;
  background: rgba(255, 255, 255, .9);
  padding: 8px 10px;
  color: var(--muted);
  backdrop-filter: blur(8px);
}
.stage {
  position: absolute;
  left: 0;
  top: 0;
  width: 2200px;
  height: 1400px;
  transform-origin: 0 0;
}
.node {
  position: absolute;
  width: 310px;
  min-height: 170px;
  border: 1px solid #cdd6e2;
  border-radius: 8px;
  background: rgba(255, 255, 255, .96);
  box-shadow: 0 16px 32px rgba(32, 45, 63, .13);
  overflow: hidden;
}
.node.dragging {
  box-shadow: 0 24px 44px rgba(32, 45, 63, .22);
}
.node header {
  display: flex;
  justify-content: space-between;
  gap: 10px;
  align-items: center;
  padding: 10px 12px;
  border-bottom: 1px solid var(--line);
  background: #f8fafc;
  font-weight: 700;
  cursor: grab;
  user-select: none;
  touch-action: none;
}
.node.dragging header {
  cursor: grabbing;
}
.node header span {
  color: var(--muted);
  font-size: 12px;
  font-weight: 600;
}
.node .body {
  padding: 12px;
  line-height: 1.55;
  color: #263241;
  white-space: pre-wrap;
  word-break: break-word;
}
.node.tweet {
  border-color: #d3bc70;
}
.node.tweet header {
  background: #fff8dc;
}
.node.prompt {
  border-color: #96c3ca;
}
.node.prompt header {
  background: #e9f7f8;
}
.node.output {
  width: 340px;
  min-height: 240px;
}
video, .node img {
  width: 100%;
  display: block;
  background: #111827;
}
video {
  aspect-ratio: 9 / 16;
  max-height: 520px;
}
.asset-grid {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 4px;
  padding: 8px;
}
.asset-grid img {
  aspect-ratio: 1 / 1;
  object-fit: cover;
  border-radius: 6px;
}
.node.image, .node.grid {
  min-height: 230px;
}
.node.image header {
  background: #eef7ed;
}
.node.grid header {
  background: #f3eefb;
}
.connector {
  position: absolute;
  height: 2px;
  background: #8aa0b6;
  transform-origin: 0 0;
  opacity: .8;
}
.jobs {
  margin-top: 14px;
  display: grid;
  gap: 8px;
}
.job {
  border: 1px solid var(--line);
  border-radius: 8px;
  padding: 8px 10px;
  background: #fff;
  color: var(--muted);
  font-size: 13px;
  word-break: break-word;
}
.job strong {
  color: var(--ink);
}
.ok { color: var(--ok); }
.bad { color: var(--bad); }
.warn { color: var(--warn); }
@media (max-width: 900px) {
  body { overflow: auto; }
  .app { grid-template-columns: 1fr; height: auto; min-height: 100vh; }
  .sidebar { border-right: none; border-bottom: 1px solid var(--line); }
  .canvas-shell { min-height: 720px; }
}
</style>
</head>
<body>
<div class="app">
  <aside class="sidebar">
    <div class="brand">
      <h1>Seedance 无限画布</h1>
      <div class="pill">Mac local</div>
    </div>
    <div class="field">
      <label for="title">标题</label>
      <input id="title" placeholder="可空">
    </div>
    <div class="field">
      <label for="script">文案</label>
      <textarea id="script" placeholder="粘贴小说推文开头、剧情梗概或短视频口播"></textarea>
    </div>
    <div class="row">
      <div class="field">
        <label for="hookStyle">钩子类型</label>
        <select id="hookStyle">
          <option>悬疑反转</option>
          <option>强冲突</option>
          <option>情绪爆点</option>
          <option>爽点反杀</option>
        </select>
      </div>
      <div class="field">
        <label for="payloadMode">API 模式</label>
        <select id="payloadMode">
          <option value="ark">Ark 标准</option>
          <option value="ark_text_flags">Ark 文本参数</option>
          <option value="seedance_v2">Seedance v2</option>
        </select>
      </div>
    </div>
    <div class="field">
      <label for="apiKey">API Key</label>
      <input id="apiKey" type="password" placeholder="留空则读取 SEEDANCE_API_KEY / ARK_API_KEY">
    </div>
    <div class="field">
      <label for="baseUrl">Base URL</label>
      <input id="baseUrl">
    </div>
    <div class="row">
      <div class="field">
        <label for="createPath">创建路径</label>
        <input id="createPath">
      </div>
      <div class="field">
        <label for="retrievePath">查询路径</label>
        <input id="retrievePath">
      </div>
    </div>
    <div class="field">
      <label for="model">模型</label>
      <input id="model">
    </div>
    <div class="row3">
      <div class="field">
        <label for="duration">秒数</label>
        <input id="duration" type="number" min="4" max="15">
      </div>
      <div class="field">
        <label for="ratio">比例</label>
        <select id="ratio">
          <option>9:16</option>
          <option>16:9</option>
          <option>1:1</option>
          <option>4:3</option>
          <option>3:4</option>
          <option>21:9</option>
        </select>
      </div>
      <div class="field">
        <label for="resolution">清晰度</label>
        <select id="resolution">
          <option>720p</option>
          <option>480p</option>
          <option>1080p</option>
          <option>4k</option>
        </select>
      </div>
    </div>
    <div class="field">
      <label for="visualStyle">视觉风格</label>
      <input id="visualStyle">
    </div>
    <div class="field">
      <label for="customPrompt">Seedance 提示词</label>
      <textarea id="customPrompt" placeholder="可空；点 AI 转提示词后会自动填入，也可手动改"></textarea>
    </div>
    <div class="row">
      <div class="field">
        <label for="promptLlmBaseUrl">语言 AI Base URL</label>
        <input id="promptLlmBaseUrl" placeholder="OpenAI 兼容 /v1">
      </div>
      <div class="field">
        <label for="promptLlmModel">语言 AI 模型</label>
        <input id="promptLlmModel" placeholder="如 gpt-4o-mini / qwen...">
      </div>
    </div>
    <div class="row">
      <div class="field">
        <label for="promptLlmApiKey">语言 AI Key</label>
        <input id="promptLlmApiKey" type="password" placeholder="可空，读 PROMPT_LLM_API_KEY">
      </div>
      <div class="field">
        <label for="promptLlmLanguage">提示词语言</label>
        <select id="promptLlmLanguage">
          <option>English</option>
          <option>中文</option>
          <option>日本語</option>
        </select>
      </div>
    </div>
    <div class="row">
      <div class="field">
        <label for="imageBaseUrl">图片 API Base URL</label>
        <input id="imageBaseUrl" placeholder="OpenAI 兼容 /v1">
      </div>
      <div class="field">
        <label for="imageModel">图片模型</label>
        <input id="imageModel" placeholder="如 gpt-image-2...">
      </div>
    </div>
    <div class="row3">
      <div class="field">
        <label for="imageApiKey">图片 API Key</label>
        <input id="imageApiKey" type="password" placeholder="可空，读 IMAGE_API_KEY">
      </div>
      <div class="field">
        <label for="imageProvider">图片 Provider</label>
        <select id="imageProvider">
          <option value="custom">中转站 OpenAI</option>
          <option value="openai">OpenAI</option>
          <option value="placeholder">占位图</option>
        </select>
      </div>
      <div class="field">
        <label for="characterCount">人设数量</label>
        <input id="characterCount" type="number" min="1" max="8">
      </div>
    </div>
    <div class="checks">
      <label class="check"><input id="generateAudio" type="checkbox">生成音频</label>
      <label class="check"><input id="watermark" type="checkbox">保留水印</label>
      <label class="check"><input id="dryRun" type="checkbox">只出方案</label>
      <label class="check"><input id="autosave" type="checkbox" checked>保存配置</label>
    </div>
    <div class="actions">
      <button id="promptBtn">AI 转提示词</button>
      <button id="characterBtn">生成人设图</button>
    </div>
    <div class="actions">
      <button id="gridBtn">生成九宫图</button>
      <button id="allRefsBtn">人设 + 九宫</button>
    </div>
    <div class="actions">
      <button id="planBtn">生成方案</button>
      <button id="generateBtn" class="primary">生成短视频</button>
    </div>
    <div id="status" class="status">ready</div>
    <div id="jobs" class="jobs"></div>
  </aside>
  <main id="canvasShell" class="canvas-shell">
    <div class="toolbar">
      <div class="mini"><strong id="zoomText">100%</strong><span id="stageText">canvas</span></div>
      <div class="mini"><span id="jobText">no job</span></div>
    </div>
    <div id="stage" class="stage"></div>
  </main>
</div>
<script>
const $ = (id) => document.getElementById(id);
const fields = [
  "baseUrl", "createPath", "retrievePath", "model", "duration", "ratio", "resolution", "visualStyle",
  "hookStyle", "payloadMode", "customPrompt", "promptLlmBaseUrl", "promptLlmModel", "promptLlmLanguage",
  "imageBaseUrl", "imageModel", "imageProvider", "characterCount"
];
const checks = ["generateAudio", "watermark"];
let pan = {x: 56, y: 72, z: 1};
let panDrag = null;
let nodeDrag = null;
let currentJob = "";
let pollTimer = null;
const stage = $("stage");
let nodePositions = {};
try {
  nodePositions = JSON.parse(localStorage.getItem("seedanceCanvasNodePositions") || "{}");
} catch (_) {
  nodePositions = {};
}

function setStatus(text, cls) {
  $("status").className = "status " + (cls || "");
  $("status").textContent = text;
}

async function api(path, body) {
  const res = await fetch(path, {
    method: body ? "POST" : "GET",
    headers: body ? {"Content-Type": "application/json"} : {},
    body: body ? JSON.stringify(body) : undefined
  });
  const data = await res.json();
  if (!res.ok || data.error) throw new Error(data.error || res.statusText);
  return data;
}

function payload() {
  return {
    title: $("title").value,
    script: $("script").value,
    api_key: $("apiKey").value,
    base_url: $("baseUrl").value,
    create_path: $("createPath").value,
    retrieve_path: $("retrievePath").value,
    payload_mode: $("payloadMode").value,
    model: $("model").value,
    prompt: $("customPrompt").value,
    duration: Number($("duration").value || 5),
    ratio: $("ratio").value,
    resolution: $("resolution").value,
    visual_style: $("visualStyle").value,
    hook_style: $("hookStyle").value,
    prompt_llm_base_url: $("promptLlmBaseUrl").value,
    prompt_llm_model: $("promptLlmModel").value,
    prompt_llm_api_key: $("promptLlmApiKey").value,
    prompt_llm_language: $("promptLlmLanguage").value,
    image_base_url: $("imageBaseUrl").value,
    image_model: $("imageModel").value,
    image_api_key: $("imageApiKey").value,
    image_provider: $("imageProvider").value,
    character_count: Number($("characterCount").value || 3),
    generate_audio: $("generateAudio").checked,
    watermark: $("watermark").checked,
    dry_run: $("dryRun").checked
  };
}

function applySettings(settings) {
  $("baseUrl").value = settings.base_url || "";
  $("createPath").value = settings.create_path || "";
  $("retrievePath").value = settings.retrieve_path || "";
  $("payloadMode").value = settings.payload_mode || "ark";
  $("model").value = settings.model || "";
  $("duration").value = settings.duration || 5;
  $("ratio").value = settings.ratio || "9:16";
  $("resolution").value = settings.resolution || "720p";
  $("visualStyle").value = settings.visual_style || "";
  $("hookStyle").value = settings.hook_style || "悬疑反转";
  $("promptLlmBaseUrl").value = settings.prompt_llm_base_url || "";
  $("promptLlmModel").value = settings.prompt_llm_model || "";
  $("promptLlmLanguage").value = settings.prompt_llm_language || "English";
  $("imageBaseUrl").value = settings.image_base_url || "";
  $("imageModel").value = settings.image_model || "";
  $("imageProvider").value = settings.image_provider || "custom";
  $("characterCount").value = settings.character_count || 3;
  $("generateAudio").checked = Boolean(settings.generate_audio);
  $("watermark").checked = settings.watermark !== false;
}

function updateTransform() {
  stage.style.transform = `translate(${pan.x}px, ${pan.y}px) scale(${pan.z})`;
  $("zoomText").textContent = Math.round(pan.z * 100) + "%";
}

function connector(x1, y1, x2, y2) {
  const dx = x2 - x1, dy = y2 - y1;
  const line = document.createElement("div");
  line.className = "connector";
  line.style.left = x1 + "px";
  line.style.top = y1 + "px";
  line.style.width = Math.sqrt(dx * dx + dy * dy) + "px";
  line.style.transform = `rotate(${Math.atan2(dy, dx)}rad)`;
  return line;
}

function nodeBox(id) {
  const escaped = window.CSS && CSS.escape ? CSS.escape(id) : id;
  const el = stage.querySelector(`[data-node-id="${escaped}"]`);
  if (!el) return null;
  return {
    x: Number.parseFloat(el.style.left || "0"),
    y: Number.parseFloat(el.style.top || "0"),
    w: el.offsetWidth || 310,
    h: el.offsetHeight || 170
  };
}

function redrawConnectors() {
  stage.querySelectorAll(".connector").forEach((el) => el.remove());
  const pairs = [["copy", "hook"], ["hook", "prompt"], ["prompt", "character"], ["prompt", "grid"], ["character", "video"], ["grid", "video"]];
  for (const [from, to] of pairs) {
    const a = nodeBox(from);
    const b = nodeBox(to);
    if (!a || !b) continue;
    stage.prepend(connector(a.x + a.w, a.y + a.h / 2, b.x, b.y + b.h / 2));
  }
}

function mediaGrid(urls) {
  const rows = (urls || []).filter(Boolean).slice(0, 9);
  if (!rows.length) return "";
  return `<div class="asset-grid">${rows.map((url) => `<img src="${escapeHtml(url)}" loading="lazy">`).join("")}</div>`;
}

function nodeMedia(node, status) {
  if (!status) return "";
  if (node.id === "character") return mediaGrid(status.character_media);
  if (node.id === "grid") {
    if (status.nine_grid_media) return `<img src="${escapeHtml(status.nine_grid_media)}" loading="lazy">`;
    return mediaGrid(status.grid_media);
  }
  if (node.kind === "output" && status.media_url) {
    return `<video controls playsinline src="${escapeHtml(status.media_url)}"></video>`;
  }
  return "";
}

function drawNodes(nodes, status) {
  stage.innerHTML = "";
  for (const node of nodes) {
    const el = document.createElement("section");
    el.className = "node " + (node.kind || "");
    el.dataset.nodeId = node.id;
    const saved = nodePositions[node.id] || {};
    const x = Number.isFinite(saved.x) ? saved.x : node.x;
    const y = Number.isFinite(saved.y) ? saved.y : node.y;
    el.style.left = x + "px";
    el.style.top = y + "px";
    const media = nodeMedia(node, status) || `<div class="body">${escapeHtml(node.body || "")}</div>`;
    el.innerHTML = `<header>${escapeHtml(node.title || "")}<span>${escapeHtml(node.kind || "")}</span></header>${media}`;
    stage.appendChild(el);
    wireNodeDrag(el, node.id);
  }
  redrawConnectors();
}

function escapeHtml(text) {
  return String(text).replace(/[&<>"']/g, (ch) => ({'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'}[ch]));
}

async function planOnly() {
  const data = await api("/api/plan", payload());
  drawNodes(data.nodes);
  setStatus(`钩子：${data.hook.hook}\n\nPrompt：${data.prompt.slice(0, 420)}`, "ok");
  if ($("autosave").checked) await saveSettings();
}

async function saveSettings() {
  const body = payload();
  delete body.api_key;
  delete body.prompt_llm_api_key;
  delete body.image_api_key;
  delete body.script;
  delete body.title;
  delete body.prompt;
  delete body.dry_run;
  await api("/api/settings", body);
}

async function aiToPrompts() {
  if (!$("script").value.trim()) {
    setStatus("请先填文案", "bad");
    return;
  }
  $("promptBtn").disabled = true;
  try {
    const data = await api("/api/ai_prompts", payload());
    $("customPrompt").value = data.video_prompt || "";
    await planOnly();
    setStatus(
      `提示词来源：${data.source || "unknown"}\n钩子：${data.hook || ""}\n人设提示词：${(data.character_prompts || []).length} 条\n九宫提示词：${(data.grid_prompts || []).length} 条`,
      data.llm_error ? "warn" : "ok"
    );
  } catch (err) {
    setStatus(String(err.message || err), "bad");
  } finally {
    $("promptBtn").disabled = false;
  }
}

function setRefButtons(disabled) {
  for (const id of ["characterBtn", "gridBtn", "allRefsBtn"]) {
    $(id).disabled = disabled;
  }
}

async function generateRefs(mode) {
  if (!$("script").value.trim()) {
    setStatus("请先填文案", "bad");
    return;
  }
  setRefButtons(true);
  try {
    if ($("autosave").checked) await saveSettings();
    const body = payload();
    body.mode = mode;
    const data = await api("/api/reference_images", body);
    currentJob = data.job_id;
    $("jobText").textContent = currentJob;
    setStatus(`图片任务已提交：${currentJob}\n${data.job_dir}`, "ok");
    startPolling();
  } catch (err) {
    setStatus(String(err.message || err), "bad");
    setRefButtons(false);
  }
}

async function generate() {
  if (!$("script").value.trim()) {
    setStatus("请先填文案", "bad");
    return;
  }
  $("generateBtn").disabled = true;
  try {
    await planOnly();
    const data = await api("/api/generate", payload());
    currentJob = data.job_id;
    $("jobText").textContent = currentJob;
    setStatus(`已提交：${currentJob}\n${data.job_dir}`, "ok");
    startPolling();
  } catch (err) {
    setStatus(String(err.message || err), "bad");
    $("generateBtn").disabled = false;
  }
}

function startPolling() {
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = setInterval(refreshJob, 2500);
  refreshJob();
}

async function refreshJob() {
  if (!currentJob) return;
  const st = await api(`/api/jobs/${currentJob}`);
  const line = `${st.stage || ""} ${Math.round((Number(st.progress || 0)) * 100)}%`;
  setStatus(`${line}\n${st.hook || ""}\n${st.error || st.video || st.manifest || ""}`, st.stage === "failed" ? "bad" : "ok");
  $("jobText").textContent = `${currentJob} · ${line}`;
  const plan = await api("/api/plan", payload());
  drawNodes(plan.nodes, st);
  await loadJobs();
  if (["completed", "failed", "dry_run"].includes(st.stage)) {
    clearInterval(pollTimer);
    pollTimer = null;
    $("generateBtn").disabled = false;
    setRefButtons(false);
  }
}

async function loadJobs() {
  const data = await api("/api/jobs");
  $("jobs").innerHTML = data.jobs.slice(0, 8).map((j) => (
    `<div class="job"><strong>${escapeHtml(j.job_id || "")}</strong><br>${escapeHtml(j.stage || "")} · ${Math.round((Number(j.progress || 0)) * 100)}%<br>${escapeHtml(j.hook || j.video || j.error || "")}</div>`
  )).join("");
}

function saveNodePositions() {
  localStorage.setItem("seedanceCanvasNodePositions", JSON.stringify(nodePositions));
}

function wireNodeDrag(el, id) {
  const header = el.querySelector("header");
  if (!header) return;
  const move = (ev) => {
    if (!nodeDrag || nodeDrag.id !== id) return;
    const x = nodeDrag.ox + (ev.clientX - nodeDrag.x) / pan.z;
    const y = nodeDrag.oy + (ev.clientY - nodeDrag.y) / pan.z;
    el.style.left = x + "px";
    el.style.top = y + "px";
    nodePositions[id] = {x, y};
    redrawConnectors();
  };
  const finish = () => {
    if (!nodeDrag || nodeDrag.id !== id) return;
    el.classList.remove("dragging");
    saveNodePositions();
    nodeDrag = null;
    window.removeEventListener("pointermove", move);
    window.removeEventListener("pointerup", finish);
    window.removeEventListener("pointercancel", finish);
    window.removeEventListener("mousemove", move);
    window.removeEventListener("mouseup", finish);
  };
  header.addEventListener("pointerdown", (ev) => {
    ev.preventDefault();
    ev.stopPropagation();
    nodeDrag = {
      id,
      el,
      x: ev.clientX,
      y: ev.clientY,
      ox: Number.parseFloat(el.style.left || "0"),
      oy: Number.parseFloat(el.style.top || "0")
    };
    el.classList.add("dragging");
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", finish);
    window.addEventListener("pointercancel", finish);
    window.addEventListener("mousemove", move);
    window.addEventListener("mouseup", finish);
  });
}

function wireCanvas() {
  const shell = $("canvasShell");
  shell.addEventListener("pointerdown", (ev) => {
    if (ev.target.closest(".node") || ev.target.closest(".toolbar")) return;
    panDrag = {x: ev.clientX, y: ev.clientY, ox: pan.x, oy: pan.y};
    shell.setPointerCapture(ev.pointerId);
  });
  shell.addEventListener("pointermove", (ev) => {
    if (!panDrag) return;
    pan.x = panDrag.ox + ev.clientX - panDrag.x;
    pan.y = panDrag.oy + ev.clientY - panDrag.y;
    updateTransform();
  });
  shell.addEventListener("pointerup", () => { panDrag = null; });
  shell.addEventListener("pointercancel", () => { panDrag = null; });
  shell.addEventListener("wheel", (ev) => {
    ev.preventDefault();
    const old = pan.z;
    const next = Math.max(.45, Math.min(1.8, old * (ev.deltaY > 0 ? .92 : 1.08)));
    const rect = shell.getBoundingClientRect();
    const cx = ev.clientX - rect.left;
    const cy = ev.clientY - rect.top;
    pan.x = cx - (cx - pan.x) * (next / old);
    pan.y = cy - (cy - pan.y) * (next / old);
    pan.z = next;
    updateTransform();
  }, {passive: false});
  updateTransform();
}

async function boot() {
  wireCanvas();
  const settings = await api("/api/settings");
  applySettings(settings.settings || {});
  await loadJobs();
  drawNodes((await api("/api/plan", {script: "", title: ""})).nodes);
  $("planBtn").addEventListener("click", () => planOnly().catch((err) => setStatus(String(err.message || err), "bad")));
  $("promptBtn").addEventListener("click", aiToPrompts);
  $("characterBtn").addEventListener("click", () => generateRefs("character"));
  $("gridBtn").addEventListener("click", () => generateRefs("grid"));
  $("allRefsBtn").addEventListener("click", () => generateRefs("all"));
  $("generateBtn").addEventListener("click", generate);
  for (const id of [...fields, ...checks, "title", "script"]) {
    const el = $(id);
    el.addEventListener("change", () => planOnly().catch(() => {}));
  }
}
boot().catch((err) => setStatus(String(err.message || err), "bad"));
</script>
</body>
</html>
"""


def serve(host: str, port: int) -> None:
    _ensure_dirs()
    server = ThreadingHTTPServer((host, port), SeedanceCanvasHandler)
    print(f"Seedance canvas running at http://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("Stopping Seedance canvas")
    finally:
        server.server_close()


def generate_once(args: argparse.Namespace) -> None:
    request = {
        "title": args.title or "",
        "script": args.script or Path(args.script_file).read_text(encoding="utf-8") if args.script_file else args.script or "",
        "api_key": args.api_key or "",
        "base_url": args.base_url or _load_settings().get("base_url"),
        "model": args.model or _load_settings().get("model"),
        "duration": args.duration,
        "ratio": args.ratio,
        "resolution": args.resolution,
        "dry_run": args.dry_run,
    }
    job_id = start_background_job(request)
    print(job_id)
    while True:
        status = _load_status(job_id)
        print(json.dumps(status, ensure_ascii=False))
        if status.get("stage") in {"completed", "failed", "dry_run"}:
            break
        time.sleep(2)


def main() -> None:
    parser = argparse.ArgumentParser(description="Seedance infinite canvas and tweet-hook bridge")
    sub = parser.add_subparsers(dest="cmd")
    serve_parser = sub.add_parser("serve", help="run local canvas server")
    serve_parser.add_argument("--host", default="127.0.0.1")
    serve_parser.add_argument("--port", type=int, default=7871)
    gen_parser = sub.add_parser("generate", help="generate one hook job from the terminal")
    gen_parser.add_argument("--title", default="")
    gen_parser.add_argument("--script", default="")
    gen_parser.add_argument("--script-file", default="")
    gen_parser.add_argument("--api-key", default="")
    gen_parser.add_argument("--base-url", default="")
    gen_parser.add_argument("--model", default="")
    gen_parser.add_argument("--duration", type=int, default=5)
    gen_parser.add_argument("--ratio", default="9:16")
    gen_parser.add_argument("--resolution", default="720p")
    gen_parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if args.cmd == "generate":
        generate_once(args)
    else:
        serve(args.host if getattr(args, "host", None) else "127.0.0.1", int(getattr(args, "port", 7871)))


if __name__ == "__main__":
    main()
