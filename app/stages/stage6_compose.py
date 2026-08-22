"""Video composition helpers based on ffmpeg."""
from __future__ import annotations

import html
import hashlib
import os
import re
import subprocess
import sys
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from functools import lru_cache
from pathlib import Path
from typing import Callable

from app.config import config
from app.concurrency import ffmpeg_slot
from app.utils.ffmpeg import ffmpeg_path


MOTION_RANDOM_POOL = ["vertical_pan", "horizontal_pan", "ken_burns", "pan_up", "pan_down", "pan_left", "pan_right"]
TRANSITION_RANDOM_POOL = ["none", "fade", "fadeblack", "fadewhite"]


def _parallel_limit(config_key: str, default: int, total: int, hard_cap: int = 8) -> int:
    try:
        raw = int(config.get(config_key, default) or default)
    except Exception:
        raw = default
    return max(1, min(int(total or 1), hard_cap, raw))


def _normalize_motion(value: str) -> str:
    text = str(value or "none").strip().lower()
    if "随机" in text or "random" in text:
        return "random"
    if "向上" in text or "pan_up" in text:
        return "pan_up"
    if "向下" in text or "pan_down" in text:
        return "pan_down"
    if "向左" in text or "pan_left" in text:
        return "pan_left"
    if "向右" in text or "pan_right" in text:
        return "pan_right"
    if "上下" in text or "vertical" in text or "up" in text:
        return "vertical_pan"
    if "左右" in text or "horizontal" in text or "left" in text:
        return "horizontal_pan"
    if "缩放" in text or "ken" in text or "zoom" in text:
        return "ken_burns"
    if "静态" in text or text in {"", "none", "static"}:
        return "none"
    return text


def _normalize_transition(value: str) -> str:
    text = str(value or "none").strip().lower()
    if "随机" in text or "random" in text:
        return "random"
    if "白" in text or "white" in text:
        return "fadewhite"
    if "黑" in text or "black" in text:
        return "fadeblack"
    if "淡" in text or "fade" in text:
        return "fade"
    return "none" if text in {"", "none", "no"} else text


def _pick(pool: list[str], index: int, salt: str) -> str:
    digest = hashlib.sha1(f"{salt}:{index}".encode("utf-8")).hexdigest()
    return pool[int(digest[:8], 16) % len(pool)]


def _ease_progress_expr(
    frames: int,
    reverse: bool = False,
    curve: str = "ease",
    cycle_seconds: float = 0.0,
    fps: int = 30,
    frame_var: str = "n",
) -> str:
    cycle_frames = int(float(cycle_seconds or 0) * max(1, int(fps or 30)))
    if cycle_frames > 0 and frames > cycle_frames:
        t = f"(mod({frame_var},{cycle_frames})/{cycle_frames})"
        expr = f"(0.5-0.5*cos(2*PI*{t}))"
    else:
        denom = max(1, frames - 1)
        t = f"({frame_var}/{denom})"
        if "linear" in str(curve or "").lower() or "线性" in str(curve or ""):
            expr = t
        else:
            expr = f"(0.5-0.5*cos(PI*{t}))"
    return f"(1-{expr})" if reverse else expr


def _video_encoder_args(encoder: str, preset: str = "veryfast", quality: int | float = 20) -> list[str]:
    enc = str(encoder or "libx264").strip().lower()
    quality_number = max(1, min(51, int(float(quality or 20))))
    quality_value = str(quality_number)
    if enc in {"h264_nvenc", "nvenc", "nvidia"}:
        nv_preset = str(preset or "p4")
        if nv_preset in {"veryfast", "fast", "medium", "slow"}:
            nv_preset = "p4"
        return ["-c:v", "h264_nvenc", "-preset", nv_preset, "-cq", quality_value, "-pix_fmt", "yuv420p"]
    if enc in {"h264_qsv", "qsv", "intel"}:
        return ["-c:v", "h264_qsv", "-global_quality", quality_value, "-pix_fmt", "yuv420p"]
    if enc in {"h264_amf", "amf", "amd"}:
        return ["-c:v", "h264_amf", "-quality", "speed", "-qp_i", quality_value, "-qp_p", quality_value, "-pix_fmt", "yuv420p"]
    if enc in {"h264_videotoolbox", "videotoolbox", "apple"}:
        # The GUI exposes CRF-style quality (lower is better). VideoToolbox's
        # q:v scale has the opposite direction (higher is better).  Passing 20
        # through unchanged produced ~0.5 Mbps 1080p output on Apple Silicon.
        # Convert the 1..51 UI scale linearly to VideoToolbox's 100..1 scale.
        videotoolbox_quality = round(100 - ((quality_number - 1) * 99 / 50))
        return ["-c:v", "h264_videotoolbox", "-q:v", str(videotoolbox_quality), "-pix_fmt", "yuv420p"]
    x264_preset = str(preset or "veryfast").strip().lower()
    if x264_preset not in {"ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow", "slower", "veryslow"}:
        x264_preset = "veryfast"
    return ["-c:v", "libx264", "-preset", x264_preset, "-crf", quality_value, "-pix_fmt", "yuv420p"]


def _is_hardware_encoder(encoder: str) -> bool:
    enc = str(encoder or "").strip().lower()
    return enc in {
        "h264_nvenc",
        "nvenc",
        "nvidia",
        "h264_qsv",
        "qsv",
        "intel",
        "h264_amf",
        "amf",
        "amd",
        "h264_videotoolbox",
        "videotoolbox",
        "apple",
    }


def _run_encoder_command(
    cmd_factory: Callable[[list[str]], list[str]],
    out_path: Path,
    on_log: Callable[[str], None],
    encoder: str,
    preset: str,
    quality: int | float,
) -> str:
    try:
        _run(cmd_factory(_video_encoder_args(encoder, preset, quality)), on_log)
        return encoder
    except RuntimeError:
        if not _is_hardware_encoder(encoder):
            raise
        on_log("  hardware encoder failed, retrying with libx264 for compatibility")
        out_path.unlink(missing_ok=True)
        _run(cmd_factory(_video_encoder_args("libx264", "veryfast", quality)), on_log)
        return "libx264"


def _run(cmd: list[str], on_log: Callable[[str], None]):
    on_log("  $ " + " ".join(str(c) for c in cmd[:8]) + (" ..." if len(cmd) > 8 else ""))
    with ffmpeg_slot(action=Path(str(cmd[0])).name if cmd else "ffmpeg"):
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="ignore",
        )
        tail: deque[str] = deque(maxlen=40)
        last_progress = 0.0
        assert proc.stderr is not None
        for line in proc.stderr:
            line = line.strip()
            if not line:
                continue
            tail.append(line)
            now = time.time()
            if "time=" in line and now - last_progress > 20:
                on_log("  ffmpeg " + line[-180:])
                last_progress = now
        rc = proc.wait()
        if rc != 0:
            on_log("  ffmpeg error:\n" + "\n".join(tail))
            raise RuntimeError(f"ffmpeg failed: {rc}")


@lru_cache(maxsize=8)
def _ffmpeg_has_filter(executable: str, filter_name: str) -> bool:
    try:
        proc = subprocess.run(
            [executable, "-hide_banner", "-filters"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="ignore",
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    pattern = re.compile(rf"^\s*[TSC\.]+\s+{re.escape(filter_name)}\s", re.MULTILINE)
    return proc.returncode == 0 and bool(pattern.search(proc.stdout or ""))


def _subtitle_mode(subtitle_ass: Path | None, on_log: Callable[[str], None]) -> tuple[bool, Path | None]:
    if not subtitle_ass or not subtitle_ass.exists():
        return False, None
    if _ffmpeg_has_filter(ffmpeg_path(), "ass"):
        return True, None
    subtitle_srt = subtitle_ass.with_suffix(".srt")
    if subtitle_srt.exists():
        on_log("  WARN ffmpeg has no libass filter; embedding subtitle.srt as a selectable MP4 subtitle track")
        return False, subtitle_srt
    on_log("  WARN ffmpeg has no libass filter and subtitle.srt is missing; composing without subtitles")
    return False, None


def _concat_file_path(path: Path) -> str:
    # ffmpeg concat demuxer accepts forward slashes on Windows.
    escaped = path.resolve().as_posix().replace("'", r"'\''")
    return f"file '{escaped}'"


def concat_audios(audios: list[Path], out_path: Path, on_log: Callable[[str], None]):
    if not audios:
        raise ValueError("没有可拼接的音频")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    list_file = out_path.with_suffix(".concat.txt")
    tmp_path = out_path.with_name(out_path.stem + ".tmp" + out_path.suffix)
    tmp_path.unlink(missing_ok=True)
    try:
        list_file.write_text("\n".join(_concat_file_path(p) for p in audios), encoding="utf-8")
        _run(
            [
                ffmpeg_path(),
                "-y",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(list_file),
                "-c:a",
                "libmp3lame",
                "-b:a",
                "192k",
                str(tmp_path),
            ],
            on_log,
        )
        tmp_path.replace(out_path)
    finally:
        list_file.unlink(missing_ok=True)
        tmp_path.unlink(missing_ok=True)


def build_blurred_portrait_short(
    source_video: Path,
    out_path: Path,
    on_log: Callable[[str], None],
    *,
    duration: float = 58.0,
    width: int = 1080,
    height: int = 1920,
    fps: int = 30,
    blur_sigma: float = 28.0,
    video_encoder: str = "libx264",
    video_encoder_preset: str = "veryfast",
    video_encoder_quality: int | float = 20,
) -> None:
    """Turn the start of a landscape master into a portrait Short.

    The foreground always keeps its original aspect ratio. A separately scaled
    copy fills the portrait canvas behind it and is blurred/darkened, so no
    source pixels are stretched and no people are cropped from the foreground.
    """
    source_video = Path(source_video)
    if not source_video.exists():
        raise FileNotFoundError(f"source video not found: {source_video}")
    width = max(2, int(width) // 2 * 2)
    height = max(2, int(height) // 2 * 2)
    fps = max(1, int(fps))
    duration = max(0.1, min(60.0, float(duration or 58.0)))
    blur_sigma = max(0.0, min(100.0, float(blur_sigma or 0.0)))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = out_path.with_name(out_path.stem + ".tmp" + out_path.suffix)
    tmp_path.unlink(missing_ok=True)
    filter_complex = (
        f"[0:v]split=2[short_bg][short_fg];"
        f"[short_bg]scale={width}:{height}:force_original_aspect_ratio=increase,"
        f"crop={width}:{height},gblur=sigma={blur_sigma:.2f},eq=brightness=-0.12[short_bgv];"
        f"[short_fg]scale={width}:{height}:force_original_aspect_ratio=decrease[short_fgv];"
        f"[short_bgv][short_fgv]overlay=(W-w)/2:(H-h)/2,setsar=1,fps={fps}[v]"
    )

    def make_cmd(encoder_args: list[str]) -> list[str]:
        return [
            ffmpeg_path(), "-y", "-i", str(source_video),
            "-filter_complex", filter_complex,
            "-map", "[v]", "-map", "0:a:0?",
            "-t", f"{duration:.3f}",
            *encoder_args,
            "-c:a", "aac", "-b:a", "192k",
            "-shortest", str(tmp_path),
        ]

    try:
        _run_encoder_command(
            make_cmd,
            tmp_path,
            on_log,
            video_encoder,
            video_encoder_preset,
            video_encoder_quality,
        )
        if not tmp_path.exists() or tmp_path.stat().st_size < 1000:
            raise RuntimeError("portrait Short encoder returned no valid output")
        tmp_path.replace(out_path)
    finally:
        tmp_path.unlink(missing_ok=True)


def build_video(
    images: list[Path],
    image_durations: list[float],
    audio_path: Path,
    out_path: Path,
    on_log: Callable[[str], None],
    width: int = 1080,
    height: int = 1920,
    fps: int = 30,
    ken_burns: bool = True,
    subtitle_ass: Path | None = None,
    bgm_path: Path | None = None,
    bgm_volume: float = 0.15,
    long_mode: bool = False,
    cleanup_temp: bool = True,
    motion: str = "none",
    motion_curve: str = "ease",
    motion_cycle_seconds: float = 18.0,
    transition: str = "none",
    transition_duration: float = 0.4,
    video_encoder: str = "libx264",
    video_encoder_preset: str = "veryfast",
    video_encoder_quality: int | float = 20,
):
    if len(images) != len(image_durations):
        raise ValueError("images 和 image_durations 数量不一致")
    if not images:
        raise ValueError("没有可合成的视频图片")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    motion = _normalize_motion(motion)
    motion_overscan = 1.22
    zoom_amount = 0.10
    # An explicit static selection must always bypass per-image motion.  The
    # Ken Burns checkbox controls the default effect, not a user's explicit
    # "静态" choice.
    if motion in {"", "none", "static"}:
        _build_video_from_image_list(
            images=images,
            image_durations=image_durations,
            audio_path=audio_path,
            out_path=out_path,
            on_log=on_log,
            width=width,
            height=height,
            fps=fps,
            subtitle_ass=subtitle_ass,
            bgm_path=bgm_path,
            bgm_volume=bgm_volume,
            video_encoder=video_encoder,
            video_encoder_preset=video_encoder_preset,
            video_encoder_quality=video_encoder_quality,
        )
        return
    _build_video_from_clips(
        images=images,
        image_durations=image_durations,
        audio_path=audio_path,
        out_path=out_path,
        on_log=on_log,
        width=width,
        height=height,
        fps=fps,
        subtitle_ass=subtitle_ass,
        bgm_path=bgm_path,
        bgm_volume=bgm_volume,
        cleanup_temp=cleanup_temp,
        motion=motion,
        motion_curve=motion_curve,
        motion_cycle_seconds=motion_cycle_seconds,
        transition=transition,
        transition_duration=transition_duration,
        video_encoder=video_encoder,
        video_encoder_preset=video_encoder_preset,
        video_encoder_quality=video_encoder_quality,
        motion_overscan=motion_overscan,
        zoom_amount=zoom_amount,
    )


def _build_video_from_image_list(
    images: list[Path],
    image_durations: list[float],
    audio_path: Path,
    out_path: Path,
    on_log: Callable[[str], None],
    width: int,
    height: int,
    fps: int,
    subtitle_ass: Path | None,
    bgm_path: Path | None,
    bgm_volume: float,
    video_encoder: str,
    video_encoder_preset: str,
    video_encoder_quality: int | float,
):
    list_file: Path | None = None
    if len(images) == 1:
        # A concat demuxer with one still image can stall when finalizing a
        # long MP4 on macOS.  Loop the image directly instead.
        cmd = [
            # Decode the still once per second; the fps filter below duplicates
            # frames for the target video rate without repeatedly decoding PNG.
            ffmpeg_path(), "-y", "-loop", "1", "-framerate", "1",
            "-i", str(images[0]), "-i", str(audio_path),
        ]
    else:
        list_file = out_path.parent / "images.concat.txt"
        lines: list[str] = []
        for img, dur in zip(images, image_durations):
            lines.append(_concat_file_path(img))
            lines.append(f"duration {max(0.05, float(dur)):.3f}")
        lines.append(_concat_file_path(images[-1]))
        list_file.write_text("\n".join(lines), encoding="utf-8")
        cmd = [
            ffmpeg_path(), "-y", "-f", "concat", "-safe", "0",
            "-i", str(list_file), "-i", str(audio_path),
        ]
    filter_complex: list[str] = []
    hard_subtitle, soft_subtitle = _subtitle_mode(subtitle_ass, on_log)
    video_filter = (
        f"[0:v]scale={width}:{height}:force_original_aspect_ratio=increase,"
        f"crop={width}:{height},fps={fps},setsar=1"
    )
    if hard_subtitle and subtitle_ass:
        video_filter += "," + _ass_filter(subtitle_ass)
    video_filter += "[v]"
    filter_complex.append(video_filter)
    audio_map = "1:a"
    next_input_index = 2
    if bgm_path and Path(bgm_path).exists():
        cmd.extend(["-stream_loop", "-1", "-i", str(bgm_path)])
        filter_complex.append(
            f"[{next_input_index}:a]volume={bgm_volume}[bgm];[1:a][bgm]amix=inputs=2:duration=first:dropout_transition=2[a]"
        )
        audio_map = "[a]"
        next_input_index += 1
    soft_subtitle_index: int | None = None
    if soft_subtitle:
        soft_subtitle_index = next_input_index
        cmd.extend(["-i", str(soft_subtitle)])

    def make_cmd(encoder_args: list[str]) -> list[str]:
        final_cmd = [
            *cmd,
            "-filter_complex",
            ";".join(filter_complex),
            "-map",
            "[v]",
            "-map",
            audio_map,
            *encoder_args,
            "-c:a",
            "aac",
            "-b:a",
            "192k",
        ]
        if soft_subtitle_index is not None:
            final_cmd.extend(
                [
                    "-map",
                    f"{soft_subtitle_index}:s:0",
                    "-c:s",
                    "mov_text",
                    "-metadata:s:s:0",
                    "language=und",
                    "-disposition:s:0",
                    "default",
                ]
            )
        final_cmd.extend(["-t", f"{sum(float(duration) for duration in image_durations):.3f}", "-shortest", str(out_path)])
        return final_cmd

    try:
        _run_encoder_command(make_cmd, out_path, on_log, video_encoder, video_encoder_preset, video_encoder_quality)
    finally:
        if list_file:
            list_file.unlink(missing_ok=True)


def _build_video_from_clips(
    images: list[Path],
    image_durations: list[float],
    audio_path: Path,
    out_path: Path,
    on_log: Callable[[str], None],
    width: int,
    height: int,
    fps: int,
    subtitle_ass: Path | None,
    bgm_path: Path | None,
    bgm_volume: float,
    cleanup_temp: bool,
    motion: str,
    motion_curve: str,
    motion_cycle_seconds: float,
    transition: str,
    transition_duration: float,
    video_encoder: str,
    video_encoder_preset: str,
    video_encoder_quality: int | float,
    motion_overscan: float,
    zoom_amount: float,
):
    tmp_dir = out_path.parent / "_clips"
    tmp_dir.mkdir(exist_ok=True)
    clips: list[Path | None] = [None] * len(images)
    render_jobs: list[tuple[int, Path, float, Path, str, str]] = []
    for i, (img, dur) in enumerate(zip(images, image_durations)):
        clip_motion = _normalize_motion(motion)
        if clip_motion == "random":
            clip_motion = _pick(MOTION_RANDOM_POOL, i, "motion")
        clip_transition = _normalize_transition(transition)
        if clip_transition == "random":
            clip_transition = _pick(TRANSITION_RANDOM_POOL, i, "transition")
        try:
            sig = img.stat()
            img_sig = f"{sig.st_size}:{sig.st_mtime:.3f}"
        except OSError:
            img_sig = str(img)
        clip_hash = hashlib.sha1(
            f"{img}:{img_sig}:{dur:.3f}:{width}:{height}:{fps}:{clip_motion}:{motion_curve}:{motion_cycle_seconds}:{clip_transition}:{transition_duration}".encode("utf-8")
        ).hexdigest()[:10]
        clip = tmp_dir / f"clip_{i:05d}_{clip_hash}.mp4"
        clips[i] = clip
        if not clip.exists() or clip.stat().st_size < 1000:
            render_jobs.append((i, img, dur, clip, clip_motion, clip_transition))

    def render_clip(job: tuple[int, Path, float, Path, str, str]) -> str:
        i, img, dur, clip, clip_motion, clip_transition = job
        frames = max(1, int(dur * fps))
        if clip_motion in {"vertical_pan", "pan_up", "pan_down"}:
            scale_h = int(height * motion_overscan)
            reverse = (i % 2 == 1) if clip_motion == "vertical_pan" else clip_motion == "pan_down"
            progress = _ease_progress_expr(
                frames,
                reverse=reverse,
                curve=motion_curve,
                cycle_seconds=motion_cycle_seconds,
                fps=fps,
            )
            y_expr = f"'(ih-oh)*{progress}'"
            vf = (
                f"scale={width}:{scale_h}:force_original_aspect_ratio=increase,"
                f"crop={width}:{height}:x='(iw-ow)/2':y={y_expr},"
                f"fps={fps},setsar=1"
            )
        elif clip_motion in {"horizontal_pan", "pan_left", "pan_right"}:
            scale_w = int(width * motion_overscan)
            reverse = (i % 2 == 1) if clip_motion == "horizontal_pan" else clip_motion == "pan_right"
            progress = _ease_progress_expr(
                frames,
                reverse=reverse,
                curve=motion_curve,
                cycle_seconds=motion_cycle_seconds,
                fps=fps,
            )
            x_expr = f"'(iw-ow)*{progress}'"
            vf = (
                f"scale={scale_w}:{height}:force_original_aspect_ratio=increase,"
                f"crop={width}:{height}:x={x_expr}:y='(ih-oh)/2',"
                f"fps={fps},setsar=1"
            )
        else:
            progress = _ease_progress_expr(
                frames,
                reverse=False,
                curve=motion_curve,
                cycle_seconds=motion_cycle_seconds,
                fps=fps,
                frame_var="on",
            )
            vf = (
                f"scale={width * 2}:-1,"
                f"zoompan=z='1+{zoom_amount:.2f}*{progress}':d={frames}:"
                f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s={width}x{height}:fps={fps},"
                f"setsar=1"
            )
        if clip_transition in {"fade", "fadeblack", "fadewhite"}:
            fade_d = max(0.05, min(float(transition_duration or 0.4), max(0.1, float(dur) / 3)))
            fade_out = max(0.0, float(dur) - fade_d)
            color = "white" if clip_transition == "fadewhite" else "black"
            vf += (
                f",fade=t=in:st=0:d={fade_d:.3f}:color={color},"
                f"fade=t=out:st={fade_out:.3f}:d={fade_d:.3f}:color={color}"
            )

        def make_clip_cmd(encoder_args: list[str]) -> list[str]:
            return [
                ffmpeg_path(),
                "-y",
                "-loop",
                "1",
                "-i",
                str(img),
                "-t",
                f"{dur:.3f}",
                "-r",
                str(fps),
                "-vf",
                vf,
                *encoder_args,
                str(clip),
            ]

        return _run_encoder_command(
            make_clip_cmd,
            clip,
            on_log,
            video_encoder,
            video_encoder_preset,
            video_encoder_quality,
        )

    used_encoders: set[str] = set()
    if render_jobs:
        clip_workers = _parallel_limit("max_parallel_video_clips", 1, len(render_jobs), hard_cap=8)
        on_log(f"  render {len(render_jobs)} dynamic clips parallel={clip_workers}")
        if clip_workers == 1:
            for job in render_jobs:
                used_encoders.add(render_clip(job))
        else:
            with ThreadPoolExecutor(max_workers=clip_workers, thread_name_prefix="ffmpeg-clip") as pool:
                futures = [pool.submit(render_clip, job) for job in render_jobs]
                for future in as_completed(futures):
                    used_encoders.add(future.result())

    final_clips = [c for c in clips if c is not None]
    if len(final_clips) != len(images):
        raise RuntimeError("not all video clips were prepared")
    effective_encoder = "libx264" if "libx264" in used_encoders and _is_hardware_encoder(video_encoder) else video_encoder

    list_file = tmp_dir / "concat.txt"
    list_file.write_text("\n".join(_concat_file_path(c) for c in final_clips), encoding="utf-8")
    silent_video = tmp_dir / "silent.mp4"
    _run([ffmpeg_path(), "-y", "-f", "concat", "-safe", "0", "-i", str(list_file), "-c", "copy", str(silent_video)], on_log)

    cmd = [ffmpeg_path(), "-y", "-i", str(silent_video), "-i", str(audio_path)]
    filter_complex: list[str] = []
    audio_map = "1:a"
    next_input_index = 2
    if bgm_path and Path(bgm_path).exists():
        cmd.extend(["-stream_loop", "-1", "-i", str(bgm_path)])
        filter_complex.append(
            f"[{next_input_index}:a]volume={bgm_volume}[bgm];[1:a][bgm]amix=inputs=2:duration=first:dropout_transition=2[a]"
        )
        audio_map = "[a]"
        next_input_index += 1
    hard_subtitle, soft_subtitle = _subtitle_mode(subtitle_ass, on_log)
    soft_subtitle_index: int | None = None
    if soft_subtitle:
        soft_subtitle_index = next_input_index
        cmd.extend(["-i", str(soft_subtitle)])
    if hard_subtitle and subtitle_ass:
        filter_complex.insert(0, f"[0:v]{_ass_filter(subtitle_ass)}[v]")
        cmd.extend(["-filter_complex", ";".join(filter_complex), "-map", "[v]", "-map", audio_map])

        def make_final_cmd(encoder_args: list[str]) -> list[str]:
            return [
                *cmd,
                *encoder_args,
                "-c:a",
                "aac",
                "-b:a",
                "192k",
                "-shortest",
                "-movflags",
                "+faststart",
                str(out_path),
            ]

        _run_encoder_command(make_final_cmd, out_path, on_log, effective_encoder, video_encoder_preset, video_encoder_quality)
    elif filter_complex:
        cmd.extend(["-filter_complex", ";".join(filter_complex), "-map", "0:v", "-map", audio_map])
        video_args = ["-c:v", "copy"]
        cmd.extend([*video_args, "-c:a", "aac", "-b:a", "192k"])
        _append_soft_subtitle_args(cmd, soft_subtitle_index)
        cmd.extend(["-shortest", str(out_path)])
        _run(cmd, on_log)
    else:
        cmd.extend(["-c:v", "copy", "-map", "0:v", "-map", audio_map])
        video_args = []
        cmd.extend([*video_args, "-c:a", "aac", "-b:a", "192k"])
        _append_soft_subtitle_args(cmd, soft_subtitle_index)
        cmd.extend(["-shortest", str(out_path)])
        _run(cmd, on_log)

    if cleanup_temp:
        for c in final_clips:
            c.unlink(missing_ok=True)
        list_file.unlink(missing_ok=True)
        silent_video.unlink(missing_ok=True)
        try:
            tmp_dir.rmdir()
        except OSError:
            pass


def _ass_filter(path: Path) -> str:
    ass_path = path.resolve().as_posix().replace(":", r"\:").replace("'", r"\'")
    fonts_dir = _default_fonts_dir()
    if fonts_dir.exists():
        fonts = fonts_dir.resolve().as_posix().replace(":", r"\:").replace("'", r"\'")
        return f"ass=filename='{ass_path}':fontsdir='{fonts}'"
    return f"ass=filename='{ass_path}'"


def _append_soft_subtitle_args(cmd: list[str], subtitle_index: int | None) -> None:
    if subtitle_index is None:
        return
    cmd.extend(
        [
            "-map",
            f"{subtitle_index}:s:0",
            "-c:s",
            "mov_text",
            "-metadata:s:s:0",
            "language=und",
            "-disposition:s:0",
            "default",
        ]
    )


def _default_fonts_dir() -> Path:
    if sys.platform == "darwin":
        for path in (
            Path("/System/Library/Fonts"),
            Path("/Library/Fonts"),
            Path.home() / "Library" / "Fonts",
        ):
            if path.exists():
                return path
    return Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"


def build_ass(
    segments_with_times: list[tuple[float, float, str]],
    out_path: Path,
    font: str = "Microsoft YaHei",
    size: int = 48,
    video_w: int = 1080,
    video_h: int = 1920,
    position: str = "下边",
    primary_color: str = "#FFFFFF",
    outline_color: str = "#000000",
    back_color: str = "#000000",
    outline: float = 4.0,
    shadow: float = 1.0,
    margin_v: int = 60,
    margin_lr: int = 56,
    spacing: float = 0.0,
    bold: bool = False,
    italic: bool = False,
    chars_per_line: int | None = None,
    max_lines: int = 2,
):
    alignment = _ass_alignment(position)
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {video_w}
PlayResY: {video_h}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{font},{size},{_ass_color(primary_color)},&H000000FF,{_ass_color(outline_color)},{_ass_color(back_color, 128)},{-1 if bold else 0},{-1 if italic else 0},0,0,100,100,{float(spacing):.2f},0,1,{float(outline):.2f},{float(shadow):.2f},{alignment},{int(margin_lr)},{int(margin_lr)},{int(margin_v)},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    if chars_per_line is None or int(chars_per_line) <= 0:
        chars_per_line = max(12, int(video_w / max(28, size)))
    chars_per_line = max(6, int(chars_per_line))
    max_lines = max(1, int(max_lines or 1))
    lines = [header]
    for start, end, text in _split_subtitle_events(segments_with_times, chars_per_line, max_lines):
        safe = _ass_escape(_wrap_text(text, chars_per_line, max_lines=max_lines))
        lines.append(f"Dialogue: 0,{_fmt_ass_time(start)},{_fmt_ass_time(end)},Default,,0,0,0,,{safe}")
    out_path.write_text("\n".join(lines), encoding="utf-8")


def build_srt(
    segments_with_times: list[tuple[float, float, str]],
    out_path: Path,
    chars_per_line: int | None = None,
    max_lines: int = 2,
):
    if chars_per_line is None or int(chars_per_line) <= 0:
        chars_per_line = 24
    chars_per_line = max(6, int(chars_per_line))
    max_lines = max(1, int(max_lines or 1))
    chunks: list[str] = []
    events = _split_subtitle_events(segments_with_times, chars_per_line, max_lines)
    for i, (start, end, text) in enumerate(events, start=1):
        clean = re.sub(r"\s+", " ", html.unescape(text)).strip()
        chunks.append(f"{i}\n{_fmt_srt_time(start)} --> {_fmt_srt_time(end)}\n{clean}\n")
    out_path.write_text("\n".join(chunks), encoding="utf-8")


def _fmt_ass_time(t: float) -> str:
    h = int(t // 3600)
    m = int((t % 3600) // 60)
    s = t % 60
    return f"{h}:{m:02d}:{s:05.2f}"


def _fmt_srt_time(t: float) -> str:
    h = int(t // 3600)
    m = int((t % 3600) // 60)
    s = int(t % 60)
    ms = int(round((t - int(t)) * 1000))
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _ass_alignment(position: str) -> int:
    text = str(position or "下边").strip().lower()
    if "上" in text or "top" in text:
        return 8
    if "中" in text or "middle" in text or "center" in text:
        return 5
    return 2


def _ass_color(value: str, alpha: int = 0) -> str:
    text = str(value or "").strip().lstrip("#")
    if len(text) == 3:
        text = "".join(ch * 2 for ch in text)
    if len(text) != 6:
        text = "FFFFFF"
    try:
        r, g, b = int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16)
    except ValueError:
        r, g, b = 255, 255, 255
    alpha = max(0, min(255, int(alpha)))
    return f"&H{alpha:02X}{b:02X}{g:02X}{r:02X}"


def _split_subtitle_events(
    segments_with_times: list[tuple[float, float, str]],
    chars_per_line: int,
    max_lines: int,
) -> list[tuple[float, float, str]]:
    events: list[tuple[float, float, str]] = []
    for start, end, text in segments_with_times:
        clean = _normalize_subtitle_text(text)
        if not clean:
            continue
        parts = _split_text_to_fit(clean, chars_per_line, max_lines)
        if len(parts) <= 1:
            events.append((start, end, clean))
            continue
        duration = max(0.05, float(end) - float(start))
        weights = [max(1, len(re.sub(r"\s+", "", part))) for part in parts]
        total = max(1, sum(weights))
        cursor = float(start)
        acc = 0
        for idx, (part, weight) in enumerate(zip(parts, weights)):
            acc += weight
            part_end = float(end) if idx == len(parts) - 1 else float(start) + duration * acc / total
            if part_end <= cursor:
                part_end = cursor + 0.01
            events.append((cursor, part_end, part))
            cursor = part_end
    return events


def _split_text_to_fit(text: str, chars_per_line: int, max_lines: int) -> list[str]:
    chars_per_line = max(6, int(chars_per_line))
    max_lines = max(1, int(max_lines or 1))
    parts: list[str] = []
    current = ""
    punctuation = "，。！？；、,.!?; "
    for ch in text:
        candidate = current + ch
        if current and len(_wrap_lines(candidate, chars_per_line)) > max_lines:
            parts.append(current.strip())
            current = ch
        else:
            current = candidate
        if current and len(_wrap_lines(current, chars_per_line)) >= max_lines and ch in punctuation:
            parts.append(current.strip())
            current = ""
    if current.strip():
        parts.append(current.strip())
    return [p for p in parts if p]


def _normalize_subtitle_text(text: str) -> str:
    value = html.unescape(text or "")
    value = re.sub(r"[，,]+", "，", value)
    value = re.sub(r"[。\.]+", "。", value)
    value = re.sub(r"[！？!?；;：:、]+", " ", value)
    value = re.sub(r"[^\w\s\u4e00-\u9fff，。]", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def _wrap_lines(text: str, chars_per_line: int) -> list[str]:
    text = re.sub(r"\s+", " ", html.unescape(text or "")).strip()
    if not text:
        return []
    chunks: list[str] = []
    current = ""
    for ch in text:
        current += ch
        if len(current) >= chars_per_line and ch in "，。！？；、,.!?; ":
            chunks.append(current.strip())
            current = ""
        elif len(current) >= chars_per_line + 6:
            chunks.append(current.strip())
            current = ""
    if current.strip():
        chunks.append(current.strip())
    return chunks


def _wrap_text(text: str, chars_per_line: int, max_lines: int = 3) -> str:
    chunks = _wrap_lines(text, chars_per_line)
    return r"\N".join(chunks[:max(1, int(max_lines or 1))])


def _ass_escape(text: str) -> str:
    marker = "\u0000ASS_NEWLINE\u0000"
    text = text.replace(r"\N", marker)
    text = text.replace("\\", r"\\").replace("{", r"\{").replace("}", r"\}")
    return text.replace(marker, r"\N")
