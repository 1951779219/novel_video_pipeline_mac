"""TTS backend：统一接口 synth(text, out_path) -> seconds
支持：
  - edge       (microsoft edge-tts，免费、零配置)
  - voicevox   (本地，HTTP API http://127.0.0.1:50021)
  - openai     (OpenAI TTS，需 API key)
  - azure      (Azure Cognitive Services TTS)
  - elevenlabs (ElevenLabs)
  - custom     (自定义 OpenAI 兼容 audio/speech 端点)
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import wave
import contextlib
import re
from pathlib import Path

import httpx
from app.utils.http import http_post
from app.utils.secrets import clean_api_key


class TTSBackend:
    def __init__(self, provider: str, voice: str, rate: str = "+0%",
                 api_key: str = "", base_url: str = "", extra: dict | None = None,
                 volume: float | str = 1.0):
        self.provider = provider
        self.voice = voice
        self.rate = rate
        self.api_key = clean_api_key(api_key)
        self.base_url = base_url.rstrip("/") if base_url else ""
        self.extra = extra or {}
        self.volume = _volume_multiplier(volume)
        self.timeout_seconds = _timeout_seconds(self.extra.get("timeout_seconds"), 180.0)

    def synth(self, text: str, out_path: Path) -> float:
        """合成音频写入 out_path（mp3 或 wav），返回时长（秒）。"""
        out_path.parent.mkdir(parents=True, exist_ok=True)
        method = getattr(self, f"_synth_{self.provider}", None)
        if method is None:
            raise ValueError(f"未知 TTS provider: {self.provider}")
        method(text, out_path)
        if self.provider != "silent":
            self._apply_volume(out_path)
        return _audio_duration(out_path)

    def _apply_volume(self, out_path: Path):
        if abs(float(self.volume) - 1.0) < 0.001:
            return
        import subprocess
        from app.utils.ffmpeg import ffmpeg_path

        tmp = out_path.with_name(out_path.stem + ".volume.tmp" + out_path.suffix)
        tmp.unlink(missing_ok=True)
        suffix = out_path.suffix.lower()
        audio_codec = ["-c:a", "libmp3lame", "-b:a", "192k"] if suffix == ".mp3" else ["-c:a", "pcm_s16le"]
        try:
            subprocess.run(
                [
                    ffmpeg_path(),
                    "-y",
                    "-i",
                    str(out_path),
                    "-filter:a",
                    f"volume={float(self.volume):.4f}",
                    *audio_codec,
                    str(tmp),
                ],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            tmp.replace(out_path)
        finally:
            tmp.unlink(missing_ok=True)

    # ── edge-tts（免费） ─────────────────────────────────
    def _synth_edge(self, text: str, out_path: Path):
        script = (
            "import asyncio, pathlib, sys\n"
            "import edge_tts\n"
            "text = pathlib.Path(sys.argv[1]).read_text(encoding='utf-8')\n"
            "out = sys.argv[2]\n"
            "voice = sys.argv[3]\n"
            "rate = sys.argv[4]\n"
            "async def main():\n"
            "    await edge_tts.Communicate(text, voice=voice, rate=rate).save(out)\n"
            "asyncio.run(main())\n"
        )
        text_file = None
        try:
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".txt", delete=False) as handle:
                handle.write(str(text or ""))
                text_file = handle.name
            env = os.environ.copy()
            env["PYTHONUTF8"] = "1"
            env["PYTHONIOENCODING"] = "utf-8"
            kwargs = {}
            if os.name == "nt":
                kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            subprocess.run(
                [sys.executable, "-B", "-c", script, text_file, str(out_path), self.voice, self.rate],
                check=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="ignore",
                timeout=self.timeout_seconds,
                env=env,
                **kwargs,
            )
        except subprocess.TimeoutExpired as exc:
            raise TimeoutError(f"edge TTS timed out after {self.timeout_seconds:.0f}s") from exc
        finally:
            if text_file:
                try:
                    Path(text_file).unlink(missing_ok=True)
                except OSError:
                    pass

    # ── voicevox（本地） ─────────────────────────────────
    def _synth_voicevox(self, text: str, out_path: Path):
        base = self.base_url or "http://127.0.0.1:50021"
        # The GUI uses short, memorable numeric codes for the small curated
        # VOICEVOX list.  Convert those to VOICEVOX's immutable speaker IDs
        # before calling its local API.  A raw VOICEVOX speaker ID still works.
        code = re.match(r"\s*(\d+)", str(self.voice))
        selected_id = int(code.group(1)) if code else 1
        speaker = VOICEVOX_FREQUENT_VOICE_IDS.get(selected_id, selected_id)
        # 1) audio_query
        q = http_post(f"{base}/audio_query", params={"text": text, "speaker": speaker}, timeout=min(60.0, self.timeout_seconds))
        q.raise_for_status()
        query = q.json()
        speed = _bounded_extra_float(self.extra, "voicevox_speed_scale", 0.90, 0.50, 2.00)
        intonation = _bounded_extra_float(self.extra, "voicevox_intonation_scale", 0.85, 0.00, 2.00)
        pause_scale = _bounded_extra_float(self.extra, "voicevox_pause_scale", 1.25, 0.50, 2.00)
        query["speedScale"] = speed
        query["intonationScale"] = intonation
        query["prePhonemeLength"] = float(query.get("prePhonemeLength") or 0.1) * pause_scale
        query["postPhonemeLength"] = float(query.get("postPhonemeLength") or 0.1) * pause_scale
        for phrase in query.get("accent_phrases") or []:
            pause_mora = phrase.get("pause_mora") if isinstance(phrase, dict) else None
            if isinstance(pause_mora, dict) and isinstance(pause_mora.get("vowel_length"), (int, float)):
                pause_mora["vowel_length"] *= pause_scale
        # 2) synthesis
        s = http_post(
            f"{base}/synthesis",
            params={"speaker": speaker},
            headers={"Content-Type": "application/json"},
            json=query,
            timeout=self.timeout_seconds,
        )
        s.raise_for_status()
        out_path.write_bytes(s.content)

    # ── OpenAI TTS ───────────────────────────────────────
    def _synth_openai(self, text: str, out_path: Path):
        base = self.base_url or "https://api.openai.com/v1"
        url = f"{base}/audio/speech"
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        payload = {
            "model": self.extra.get("model", "tts-1"),
            "input": text,
            "voice": self.voice or "alloy",
            "response_format": "mp3",
        }
        instructions = self.extra.get("instructions") or self.extra.get("emotion") or ""
        if instructions:
            payload["instructions"] = str(instructions)
        r = http_post(url, json=payload, headers=headers, timeout=self.timeout_seconds)
        r.raise_for_status()
        out_path.write_bytes(r.content)

    # ── Azure ────────────────────────────────────────────
    def _synth_azure(self, text: str, out_path: Path):
        region = self.extra.get("region", "eastus")
        url = f"https://{region}.tts.speech.microsoft.com/cognitiveservices/v1"
        ssml = (
            f'<speak version="1.0" xml:lang="ja-JP">'
            f'<voice name="{self.voice}"><prosody rate="{self.rate}">'
            f'{_xml_escape(text)}</prosody></voice></speak>'
        )
        headers = {
            "Ocp-Apim-Subscription-Key": self.api_key,
            "Content-Type": "application/ssml+xml",
            "X-Microsoft-OutputFormat": "audio-24khz-48kbitrate-mono-mp3",
        }
        r = http_post(url, content=ssml.encode("utf-8"), headers=headers, timeout=self.timeout_seconds)
        r.raise_for_status()
        out_path.write_bytes(r.content)

    # ── ElevenLabs ───────────────────────────────────────
    def _synth_elevenlabs(self, text: str, out_path: Path):
        base = self.base_url or "https://api.elevenlabs.io/v1"
        url = f"{base}/text-to-speech/{self.voice}"
        headers = {"xi-api-key": self.api_key, "Content-Type": "application/json"}
        payload = {"text": text, "model_id": self.extra.get("model", "eleven_multilingual_v2")}
        r = http_post(url, json=payload, headers=headers, timeout=self.timeout_seconds)
        r.raise_for_status()
        out_path.write_bytes(r.content)

    # ── 自定义（OpenAI 兼容 /audio/speech） ──────────────
    def _synth_custom(self, text: str, out_path: Path):
        self._synth_openai(text, out_path)

    def _synth_silent(self, text: str, out_path: Path):
        """Generate silent audio for dry-runs and recovery placeholders."""
        import subprocess
        from app.utils.ffmpeg import ffmpeg_path

        compact = "".join(str(text or "").split())
        duration = max(1.2, min(45.0, len(compact) / 4.2))
        subprocess.run(
            [
                ffmpeg_path(),
                "-y",
                "-f",
                "lavfi",
                "-i",
                "anullsrc=r=24000:cl=mono",
                "-t",
                f"{duration:.3f}",
                "-c:a",
                "libmp3lame",
                "-b:a",
                "64k",
                str(out_path),
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )


# ─────────────────────────────────────────────────────────
# 工具
# ─────────────────────────────────────────────────────────

def _bounded_extra_float(extra: dict, key: str, default: float, minimum: float, maximum: float) -> float:
    try:
        value = float(extra.get(key, default))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))

def _volume_multiplier(value) -> float:
    text = str(value if value is not None else "1.0").strip()
    if not text:
        return 1.0
    try:
        if text.endswith("%"):
            raw = float(text[:-1])
            multiplier = 1.0 + raw / 100.0 if text.startswith(("+", "-")) else raw / 100.0
        else:
            multiplier = float(text)
    except Exception:
        multiplier = 1.0
    return max(0.0, min(5.0, multiplier))


def _timeout_seconds(value, default: float = 180.0) -> float:
    try:
        seconds = float(value if value not in (None, "") else default)
    except Exception:
        seconds = default
    return max(30.0, min(1800.0, seconds))


def _xml_escape(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;")
             .replace(">", "&gt;").replace('"', "&quot;"))


def _audio_duration(path: Path) -> float:
    """用 ffprobe 读时长（秒）。"""
    import subprocess
    from app.concurrency import media_probe_slot
    from app.utils.ffmpeg import ffmpeg_path, ffprobe_path
    try:
        with media_probe_slot(action="ffprobe"):
            r = subprocess.run(
                [ffprobe_path(), "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
                capture_output=True, text=True, timeout=20,
            )
        if r.returncode == 0 and r.stdout.strip():
            return float(r.stdout.strip())
    except Exception:
        pass
    try:
        with media_probe_slot(action="ffmpeg duration"):
            r = subprocess.run(
                [ffmpeg_path(), "-hide_banner", "-i", str(path)],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="ignore",
                timeout=20,
            )
        match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", r.stderr or "")
        if not match:
            return 0.0
        hours, minutes, seconds = match.groups()
        return int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    except Exception:
        return 0.0


# ─────────────────────────────────────────────────────────
# 内置 voice 列表（UI 下拉用，非完整）
# ─────────────────────────────────────────────────────────
EDGE_MULTI_VOICES = [
    "zh-CN-XiaoxiaoNeural",
    "zh-CN-YunxiNeural",
    "zh-CN-XiaoyiNeural",
    "zh-CN-YunjianNeural",
    "zh-CN-liaoning-XiaobeiNeural",
    "zh-CN-shaanxi-XiaoniNeural",
    "zh-HK-HiuMaanNeural",
    "zh-TW-HsiaoChenNeural",
    "ja-JP-NanamiNeural",
    "ja-JP-KeitaNeural",
    "ja-JP-AoiNeural",
    "ja-JP-DaichiNeural",
    "en-US-AriaNeural",
    "en-US-GuyNeural",
    "en-GB-SoniaNeural",
    "ko-KR-SunHiNeural",
    "ko-KR-InJoonNeural",
    "es-ES-ElviraNeural",
    "fr-FR-DeniseNeural",
    "de-DE-KatjaNeural",
]

EDGE_JA_VOICES = EDGE_MULTI_VOICES


def edge_voice_choices(available_voices, current_voice: str = "") -> list[str]:
    """Return curated voices first, followed by every voice discovered online.

    Curated entries are deliberately retained even when Microsoft removes them
    so the GUI can render an old saved choice as unavailable instead of silently
    hiding why a profile stopped working.
    """
    available = {
        str(voice or "").strip()
        for voice in (available_voices or [])
        if str(voice or "").strip()
    }
    current = str(current_voice or "").strip()
    choices = list(dict.fromkeys(EDGE_MULTI_VOICES))
    if current and current not in choices:
        choices.append(current)
    choices.extend(sorted(available.difference(choices), key=str.casefold))
    return choices


def preferred_available_edge_voice(current_voice: str, available_voices) -> str:
    """Choose a stable replacement, preferring the saved voice's locale."""
    available = {
        str(voice or "").strip()
        for voice in (available_voices or [])
        if str(voice or "").strip()
    }
    current = str(current_voice or "").strip()
    if current in available:
        return current

    locale = "-".join(current.split("-")[:2]) if current else ""
    same_locale = [voice for voice in EDGE_MULTI_VOICES if voice in available and voice.startswith(f"{locale}-")]
    if same_locale:
        return same_locale[0]
    curated = [voice for voice in EDGE_MULTI_VOICES if voice in available]
    if curated:
        return curated[0]
    return sorted(available, key=str.casefold)[0] if available else ""


def discover_edge_voices() -> list[str]:
    """Fetch the Edge endpoint's current voice catalog.

    Call this from a worker thread because it performs network I/O.
    """
    import edge_tts

    async def _list() -> list[str]:
        voices = await edge_tts.list_voices()
        return sorted(
            {
                str(item.get("ShortName") or "").strip()
                for item in voices
                if str(item.get("ShortName") or "").strip()
            },
            key=str.casefold,
        )

    return asyncio.run(_list())

# GUI codes: first digit = gender (1 female / 2 male), second = character,
# third = style.  Values are translated to VOICEVOX's own speaker IDs above.
VOICEVOX_FREQUENT_VOICE_IDS = {
    111: 2, 112: 0, 113: 6, 114: 4, 115: 36, 116: 37,  # 四国めたん
    121: 8,  # 春日部つむぎ
    131: 10,  # 雨晴はう
    211: 11, 212: 39, 213: 40, 214: 41,  # 玄野武宏
}

VOICEVOX_FREQUENT_VOICES = [
    "111｜女｜四国めたん｜普通",
    "112｜女｜四国めたん｜甘甜",
    "113｜女｜四国めたん｜傲娇",
    "114｜女｜四国めたん｜性感",
    "115｜女｜四国めたん｜耳语",
    "116｜女｜四国めたん｜低语",
    "121｜女｜春日部つむぎ｜普通",
    "131｜女｜雨晴はう｜普通",
    "211｜男｜玄野武宏｜普通",
    "212｜男｜玄野武宏｜喜悦",
    "213｜男｜玄野武宏｜傲娇",
    "214｜男｜玄野武宏｜悲伤",
]
