"""Image backend：统一接口 generate(prompt, negative, out_path, w, h)
支持：
  - openai     (GPT Image 2)
  - sdwebui    (AUTOMATIC1111 /sdapi/v1/txt2img)
  - comfyui    (本地 ComfyUI，需要预设 workflow.json)
  - replicate  (Replicate API)
  - aliyun     (阿里云通义万相 wanx-v1)
  - placeholder (无任何后端时生成纯色占位图，方便先跑通流程)
  - custom     (自定义 OpenAI 兼容 /images/generations)
"""
from __future__ import annotations

import base64
import io
import json
import mimetypes
import re
import time
import uuid
from pathlib import Path
from urllib.parse import urlencode, urlparse

import httpx
from PIL import Image, ImageDraw, ImageFont, ImageOps
from app.utils.http import http_get, http_post
from app.utils.secrets import clean_api_key, redact_secret_text


def _valid_gpt_image_2_size(width: int, height: int) -> bool:
    width = int(width or 0)
    height = int(height or 0)
    if width <= 0 or height <= 0 or width % 16 or height % 16:
        return False
    long_edge, short_edge = max(width, height), min(width, height)
    pixels = width * height
    return long_edge <= 3840 and long_edge / short_edge <= 3 and 655_360 <= pixels <= 8_294_400


def _openai_image_size(
    model: str,
    width: int,
    height: int,
    request_width: int = 0,
    request_height: int = 0,
) -> str:
    text = str(model or "").lower()
    if "gpt-image-2" in text:
        if _valid_gpt_image_2_size(request_width, request_height):
            return f"{int(request_width)}x{int(request_height)}"
        if _valid_gpt_image_2_size(width, height):
            return f"{int(width)}x{int(height)}"
    if width == height:
        return "1024x1024"
    if "gpt-image" in text or "gemini" in text:
        return "1024x1536" if height >= width else "1536x1024"
    return "1024x1792" if height >= width else "1792x1024"


def _redact_http_detail(text: str, limit: int = 700) -> str:
    detail = " ".join(str(text or "").split())
    detail = redact_secret_text(detail)
    if len(detail) > limit:
        detail = detail[:limit].rstrip() + "..."
    return detail or "-"


def _http_error_detail(exc: httpx.HTTPStatusError) -> str:
    response = exc.response
    status = response.status_code if response is not None else "?"
    try:
        body = response.text if response is not None else ""
    except Exception:
        body = ""
    return f"HTTP {status}: {_redact_http_detail(body)}"


def _image_model_hint(model: str) -> str:
    text = str(model or "").strip().lower()
    if text and text != "gpt-image-2":
        return " Hint: this build is configured for gpt-image-2; use API probe to confirm a custom relay supports another image model."
    return ""


def _fit_generated_image(img: Image.Image, width: int, height: int) -> Image.Image:
    """Normalize a provider response to the configured output canvas.

    With a compliant provider this is only a resize (or a no-op). If a provider
    ignores the requested dimensions, fill the canvas instead of adding bars.
    """
    source = ImageOps.exif_transpose(img).convert("RGB")
    target = (max(1, int(width)), max(1, int(height)))
    return ImageOps.fit(source, target, method=Image.LANCZOS, centering=(0.5, 0.5))


class ImageBackend:
    def __init__(self, provider: str, base_url: str = "", api_key: str = "",
                 model: str = "", steps: int = 25, cfg: float = 7.0,
                 workflow_path: str = "", timeout_seconds: int | float = 300,
                 preserve_source: bool = False, request_width: int = 0,
                 request_height: int = 0):
        self.provider = str(provider or "placeholder").lower()
        self.base_url = base_url.rstrip("/") if base_url else ""
        self.api_key = clean_api_key(api_key)
        self.model = model
        self.steps = steps
        self.cfg = cfg
        self.workflow_path = workflow_path
        self.timeout_seconds = max(60.0, float(timeout_seconds or 300))
        self.preserve_source = bool(preserve_source)
        self.request_width = int(request_width or 0)
        self.request_height = int(request_height or 0)

    def generate(self, prompt: str, negative: str, out_path: Path,
                 width: int = 1080, height: int = 1920) -> Path:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        method = getattr(self, f"_gen_{self.provider}", None)
        if method is None:
            raise ValueError(f"未知 image provider: {self.provider}")
        method(prompt, negative, out_path, width, height)
        return out_path

    def generate_with_references(
        self,
        prompt: str,
        negative: str,
        reference_paths: list[Path],
        out_path: Path,
        width: int = 1080,
        height: int = 1920,
    ) -> Path:
        """Generate an image with optional reference images."""
        refs = [Path(p) for p in reference_paths if p and Path(p).exists()]
        if refs and self.provider in {"openai", "custom"}:
            out_path.parent.mkdir(parents=True, exist_ok=True)
            self._gen_openai_edit(prompt, negative, refs, out_path, width, height)
            return out_path
        if refs and self.provider == "sdwebui":
            out_path.parent.mkdir(parents=True, exist_ok=True)
            self._gen_sdwebui_img2img(prompt, negative, refs[0], out_path, width, height)
            return out_path
        return self.generate(prompt, negative, out_path, width=width, height=height)

    # ── placeholder ─────────────────────────────────────
    def _gen_placeholder(self, prompt, negative, out_path, w, h):
        # 生成带 prompt 文本的纯色占位图（方便快速跑通）
        from random import randint
        c = (randint(60, 180), randint(60, 180), randint(60, 180))
        img = Image.new("RGB", (w, h), c)
        d = ImageDraw.Draw(img)
        try:
            font = ImageFont.truetype("msyh.ttc", 36)
        except Exception:
            font = ImageFont.load_default()
        # 简单换行
        text = prompt[:200]
        lines = [text[i:i+24] for i in range(0, len(text), 24)]
        y = h // 3
        for line in lines[:10]:
            d.text((40, y), line, fill=(255, 255, 255), font=font)
            y += 50
        img.save(out_path, "PNG")

    # ── SD WebUI (AUTOMATIC1111) ────────────────────────
    def _gen_sdwebui(self, prompt, negative, out_path, w, h):
        url = f"{self.base_url or 'http://127.0.0.1:7860'}/sdapi/v1/txt2img"
        payload = {
            "prompt": prompt,
            "negative_prompt": negative,
            "width": w, "height": h,
            "steps": self.steps,
            "cfg_scale": self.cfg,
            "sampler_name": "DPM++ 2M Karras",
        }
        r = http_post(url, json=payload, timeout=self.timeout_seconds)
        r.raise_for_status()
        data = r.json()
        b64 = data["images"][0]
        img_bytes = base64.b64decode(b64.split(",", 1)[-1])
        out_path.write_bytes(img_bytes)

    def _gen_sdwebui_img2img(self, prompt, negative, reference_path, out_path, w, h):
        url = f"{self.base_url or 'http://127.0.0.1:7860'}/sdapi/v1/img2img"
        with Image.open(reference_path) as img:
            buf = io.BytesIO()
            _fit_generated_image(img, w, h).save(buf, "PNG")
        payload = {
            "prompt": prompt,
            "negative_prompt": negative,
            "init_images": [base64.b64encode(buf.getvalue()).decode("ascii")],
            "width": w,
            "height": h,
            "steps": self.steps,
            "cfg_scale": self.cfg,
            "denoising_strength": 0.55,
            "sampler_name": "DPM++ 2M Karras",
        }
        r = http_post(url, json=payload, timeout=self.timeout_seconds)
        r.raise_for_status()
        data = r.json()
        b64 = data["images"][0]
        out_path.write_bytes(base64.b64decode(b64.split(",", 1)[-1]))

    # ── ComfyUI ─────────────────────────────────────────
    def _gen_comfyui(self, prompt, negative, out_path, w, h):
        """
        需要 workflow_path 指向一个 ComfyUI workflow JSON（API 格式）。
        会替换其中 KSampler 的 seed、CLIPTextEncode 节点的 text、EmptyLatentImage 的尺寸。
        """
        if not self.workflow_path or not Path(self.workflow_path).exists():
            raise RuntimeError("ComfyUI 需要在配置中指定 image_workflow（API 格式 JSON 路径）")
        base = self.base_url or "http://127.0.0.1:8188"
        wf = json.loads(Path(self.workflow_path).read_text(encoding="utf-8"))
        # 简单替换：找到第一个 EmptyLatentImage 改尺寸；找正负 prompt CLIPTextEncode
        pos_node, neg_node = None, None
        for nid, node in wf.items():
            ct = node.get("class_type")
            if ct == "EmptyLatentImage":
                node["inputs"]["width"] = w
                node["inputs"]["height"] = h
            elif ct == "CLIPTextEncode":
                # 第一个当 positive，第二个当 negative
                if pos_node is None:
                    pos_node = nid
                elif neg_node is None:
                    neg_node = nid
            elif ct == "KSampler":
                import random
                node["inputs"]["seed"] = random.randint(1, 2**31 - 1)
                node["inputs"]["steps"] = self.steps
                node["inputs"]["cfg"] = self.cfg
        if pos_node:
            wf[pos_node]["inputs"]["text"] = prompt
        if neg_node:
            wf[neg_node]["inputs"]["text"] = negative

        client_id = str(uuid.uuid4())
        r = http_post(f"{base}/prompt", json={"prompt": wf, "client_id": client_id}, timeout=min(120.0, self.timeout_seconds))
        r.raise_for_status()
        prompt_id = r.json()["prompt_id"]

        # 轮询 history
        deadline = time.time() + self.timeout_seconds
        while time.time() < deadline:
            time.sleep(1.5)
            h_resp = http_get(f"{base}/history/{prompt_id}", timeout=min(30.0, self.timeout_seconds))
            if h_resp.status_code != 200:
                continue
            hist = h_resp.json()
            if prompt_id not in hist:
                continue
            outputs = hist[prompt_id].get("outputs", {})
            for node_id, out in outputs.items():
                images = out.get("images", [])
                if images:
                    img_meta = images[0]
                    params = urlencode({
                        "filename": img_meta["filename"],
                        "subfolder": img_meta.get("subfolder", ""),
                        "type": img_meta.get("type", "output"),
                    })
                    img_resp = http_get(f"{base}/view?{params}", timeout=min(120.0, self.timeout_seconds))
                    img_resp.raise_for_status()
                    out_path.write_bytes(img_resp.content)
                    return
        raise TimeoutError("ComfyUI 出图超时")

    # ── OpenAI 兼容 /images/generations ─────────────────
    def _gen_openai(self, prompt, negative, out_path, w, h):
        base = self.base_url or "https://api.openai.com/v1"
        url = f"{base}/images/generations"
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        model = self.model or "gpt-image-2"
        # The OpenAI image API has no separate negative-prompt field.  Putting
        # an SD/ComfyUI negative list into the natural-language prompt made
        # policy-sensitive words (for example gore or nudity) part of every
        # request, including the pipeline's deliberately safe retry.
        # Keep negative prompts for backends that support them, but never send
        # them through this OpenAI-compatible request shape.
        request_prompt = str(prompt or "")
        payload = {
            "model": model,
            "prompt": request_prompt,
            "size": _openai_image_size(model, w, h, self.request_width, self.request_height),
            "n": 1,
        }
        img = self._post_openai_image(url, headers, payload)
        if not self.preserve_source:
            img = _fit_generated_image(img, w, h)
        else:
            img = ImageOps.exif_transpose(img).convert("RGB")
        img.save(out_path, "PNG")

    def _gen_openai_edit(self, prompt, negative, reference_paths, out_path, w, h):
        base = self.base_url or "https://api.openai.com/v1"
        url = f"{base}/images/edits"
        headers = {}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        model = self.model or "gpt-image-2"
        # See _gen_openai: an OpenAI-compatible edit request does not support
        # a negative prompt, so do not turn restricted-word exclusions into
        # positive prompt text.
        edit_prompt = str(prompt or "")
        data = {
            "model": model,
            "prompt": edit_prompt,
            "size": _openai_image_size(model, w, h, self.request_width, self.request_height),
            "n": "1",
        }
        files = []
        handles = []
        try:
            for path in reference_paths:
                handle = Path(path).open("rb")
                handles.append(handle)
                mime = mimetypes.guess_type(str(path))[0] or "image/png"
                files.append(("image", (Path(path).name, handle, mime)))
            img = self._post_openai_image_multipart(url, headers, data, files)
            if not self.preserve_source:
                img = _fit_generated_image(img, w, h)
            else:
                img = ImageOps.exif_transpose(img).convert("RGB")
            img.save(out_path, "PNG")
        finally:
            for handle in handles:
                try:
                    handle.close()
                except Exception:
                    pass

    def _post_openai_image(self, url: str, headers: dict, payload: dict) -> Image.Image:
        try:
            r = http_post(url, json=payload, headers=headers, timeout=self.timeout_seconds)
            r.raise_for_status()
        except httpx.HTTPStatusError as exc:
            # Older OpenAI-compatible relays often require response_format.
            first_error = _http_error_detail(exc)
            retry_payload = dict(payload)
            retry_payload["response_format"] = "b64_json"
            try:
                r = http_post(url, json=retry_payload, headers=headers, timeout=self.timeout_seconds)
                r.raise_for_status()
            except httpx.HTTPStatusError as retry_exc:
                retry_error = _http_error_detail(retry_exc)
                hint = _image_model_hint(str(payload.get("model") or ""))
                raise RuntimeError(
                    f"OpenAI image request failed after retry. first={first_error}; retry={retry_error}.{hint}"
                ) from retry_exc
        data = r.json()
        return self._openai_response_image(data)

    def _post_openai_image_multipart(self, url: str, headers: dict, data: dict, files: list) -> Image.Image:
        try:
            r = http_post(url, data=data, files=files, headers=headers, timeout=self.timeout_seconds)
            r.raise_for_status()
        except httpx.HTTPStatusError as exc:
            first_error = _http_error_detail(exc)
            hint = _image_model_hint(str(data.get("model") or ""))
            raise RuntimeError(f"OpenAI image edit request failed. first={first_error}.{hint}") from exc
        return self._openai_response_image(r.json())

    def _openai_response_image(self, data: dict) -> Image.Image:
        row = (data.get("data") or [{}])[0]
        b64_value = row.get("b64_json") or row.get("image") or row.get("content")
        if b64_value:
            if isinstance(b64_value, str) and "," in b64_value and b64_value.startswith("data:"):
                b64_value = b64_value.split(",", 1)[1]
            return Image.open(io.BytesIO(base64.b64decode(str(b64_value))))
        url_value = row.get("url")
        if url_value:
            img_bytes = http_get(url_value, timeout=min(120.0, self.timeout_seconds)).content
            return Image.open(io.BytesIO(img_bytes))
        raise RuntimeError(f"OpenAI image response has no b64_json/url: {str(data)[:500]}")

    def _gen_openai_legacy(self, prompt, negative, out_path, w, h):
        base = self.base_url or "https://api.openai.com/v1"
        url = f"{base}/images/generations"
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        payload = {
            "model": self.model or "gpt-image-2",
            "prompt": prompt,
            "size": _openai_image_size(self.model or "gpt-image-2", w, h, self.request_width, self.request_height),
            "n": 1,
            "response_format": "b64_json",
        }
        r = http_post(url, json=payload, headers=headers, timeout=self.timeout_seconds)
        r.raise_for_status()
        data = r.json()["data"][0]
        if "b64_json" in data:
            img_bytes = base64.b64decode(data["b64_json"])
            img = Image.open(io.BytesIO(img_bytes))
        else:
            img_bytes = http_get(data["url"], timeout=min(120.0, self.timeout_seconds)).content
            img = Image.open(io.BytesIO(img_bytes))
        # Scene images fill the configured canvas. Covers may preserve the
        # provider's complete source so typography can be adapted without
        # cutting the top or bottom off.
        if not self.preserve_source:
            img = _fit_generated_image(img, w, h)
        else:
            img = ImageOps.exif_transpose(img).convert("RGB")
        img.save(out_path, "PNG")

    def _gen_custom(self, prompt, negative, out_path, w, h):
        self._gen_openai(prompt, negative, out_path, w, h)

    # ── Replicate ───────────────────────────────────────
    def _gen_replicate(self, prompt, negative, out_path, w, h):
        url = "https://api.replicate.com/v1/predictions"
        headers = {"Authorization": f"Token {self.api_key}", "Content-Type": "application/json"}
        payload = {
            "version": self.model,  # 例如 stability-ai/sdxl 的 version id
            "input": {
                "prompt": prompt, "negative_prompt": negative,
                "width": w, "height": h, "num_inference_steps": self.steps,
                "guidance_scale": self.cfg,
            },
        }
        r = http_post(url, json=payload, headers=headers, timeout=min(120.0, self.timeout_seconds))
        r.raise_for_status()
        pid = r.json()["id"]
        deadline = time.time() + self.timeout_seconds
        while time.time() < deadline:
            time.sleep(2)
            r2 = http_get(f"{url}/{pid}", headers=headers, timeout=20.0).json()
            if r2["status"] == "succeeded":
                out = r2["output"]
                img_url = out[0] if isinstance(out, list) else out
                img_bytes = http_get(img_url, timeout=min(120.0, self.timeout_seconds)).content
                out_path.write_bytes(img_bytes)
                return
            if r2["status"] in ("failed", "canceled"):
                raise RuntimeError(f"Replicate 失败: {r2.get('error')}")
        raise TimeoutError("Replicate 超时")

    # ── 阿里云 通义万相 ─────────────────────────────────
    def _gen_aliyun(self, prompt, negative, out_path, w, h):
        url = "https://dashscope.aliyuncs.com/api/v1/services/aigc/text2image/image-synthesis"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "X-DashScope-Async": "enable",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.model or "wanx-v1",
            "input": {"prompt": prompt, "negative_prompt": negative},
            "parameters": {"size": f"{w}*{h}", "n": 1},
        }
        r = http_post(url, json=payload, headers=headers, timeout=min(120.0, self.timeout_seconds))
        r.raise_for_status()
        task_id = r.json()["output"]["task_id"]
        # poll
        deadline = time.time() + self.timeout_seconds
        while time.time() < deadline:
            time.sleep(3)
            q = http_get(
                f"https://dashscope.aliyuncs.com/api/v1/tasks/{task_id}",
                headers={"Authorization": f"Bearer {self.api_key}"}, timeout=20.0,
            ).json()
            status = q["output"]["task_status"]
            if status == "SUCCEEDED":
                img_url = q["output"]["results"][0]["url"]
                out_path.write_bytes(http_get(img_url, timeout=min(120.0, self.timeout_seconds)).content)
                return
            if status in ("FAILED", "CANCELED"):
                raise RuntimeError(f"阿里云出图失败: {q}")
        raise TimeoutError("阿里云出图超时")
