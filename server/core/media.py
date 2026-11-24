"""ffmpeg 封装：音频提取、字幕烧录、音轨替换、抽帧。

使用 imageio-ffmpeg 自带的静态 ffmpeg，用户无需单独安装。
所有命令都做失败兜底，字幕烧录失败会降级为软字幕，不影响主流程。
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path
from typing import Optional, Tuple

from ..settings import settings

_DUR_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+\.?\d*)")


def ffmpeg_path() -> str:
    configured = settings.get("media", "ffmpeg", default="") or ""
    if configured and Path(configured).exists():
        return configured
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        exe = shutil.which("ffmpeg")
        if exe:
            return exe
        raise RuntimeError("未找到 ffmpeg，请执行 pip install imageio-ffmpeg")


def _run(args: list, cwd: Optional[Path] = None, timeout: int = 1800) -> Tuple[int, str]:
    proc = subprocess.run(
        [ffmpeg_path(), "-y", "-hide_banner", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="ignore",
        cwd=str(cwd) if cwd else None,
        timeout=timeout,
    )
    return proc.returncode, (proc.stderr or "")


def duration_of(video: Path) -> float:
    try:
        _, err = _run(["-i", str(video)], timeout=60)
        m = _DUR_RE.search(err)
        if not m:
            return 0.0
        h, mi, s = m.groups()
        return int(h) * 3600 + int(mi) * 60 + float(s)
    except Exception:
        return 0.0


def has_audio(video: Path) -> bool:
    try:
        _, err = _run(["-i", str(video)], timeout=60)
        return "Audio:" in err
    except Exception:
        return False


def extract_audio(video: Path, out_wav: Path) -> Path:
    code, err = _run(
        ["-i", str(video), "-vn", "-ac", "1", "-ar", "16000", "-f", "wav", str(out_wav)]
    )
    if code != 0 or not out_wav.exists():
        raise RuntimeError(f"音频提取失败：{err[-400:]}")
    return out_wav


def trim_audio(src: Path, dst: Path, seconds: float) -> Path:
    code, _ = _run(["-i", str(src), "-t", str(seconds), str(dst)])
    return dst if code == 0 and dst.exists() else src


def screenshot(video: Path, out_jpg: Path, at: float = 1.0) -> Optional[Path]:
    code, _ = _run(["-ss", str(at), "-i", str(video), "-frames:v", "1", "-q:v", "3", str(out_jpg)])
    return out_jpg if code == 0 and out_jpg.exists() else None


def burn_subtitle(video: Path, srt: Path, out: Path, font_size: Optional[int] = None) -> dict:
    """把 SRT 烧进画面。失败时自动降级为软字幕轨道。"""
    size = int(font_size or settings.get("media", "subtitle_font_size", default=22))
    work = srt.parent
    video_in = video if video.parent == work else None

    # 硬字幕：在字幕文件所在目录执行，规避中文路径转义问题
    if video_in is not None:
        style = (
            f"FontName=Microsoft YaHei,FontSize={size},"
            "PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,"
            "BorderStyle=1,Outline=2,Shadow=0,MarginV=30"
        )
        vf = f"subtitles={srt.name}:force_style='{style}'"
        code, err = _run(["-i", video.name, "-vf", vf, "-c:a", "copy", str(out)], cwd=work)
        if code == 0 and out.exists():
            return {"ok": True, "mode": "hard", "path": str(out)}

    # 软字幕：把 SRT 作为内嵌字幕轨封装进 MP4
    code, err2 = _run(
        [
            "-i",
            str(video),
            "-i",
            str(srt),
            "-c",
            "copy",
            "-c:s",
            "mov_text",
            "-metadata:s:s:0",
            "language=chi",
            str(out),
        ]
    )
    if code == 0 and out.exists():
        return {"ok": True, "mode": "soft", "path": str(out)}
    return {"ok": False, "mode": "none", "error": (err2 or err)[-400:]}


def replace_audio(video: Path, audio: Path, out: Path) -> dict:
    """替换音轨并保留完整画面：配音不足补静音，过长则截到片尾。"""
    duration = duration_of(video)
    # apad 会生成无限静音，必须用明确的片长截断，不能只依赖 -shortest。
    limit = ["-af", "apad", "-t", f"{duration:.3f}"] if duration > 0 else ["-shortest"]
    code, err = _run(
        [
            "-i",
            str(video),
            "-i",
            str(audio),
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-c:v",
            "copy",
            *limit,
            "-c:a",
            "aac",
            str(out),
        ]
    )
    if code == 0 and out.exists():
        return {"ok": True, "path": str(out)}
    return {"ok": False, "error": err[-400:]}


def transcode_to_mp4(src: Path, out: Path) -> dict:
    code, err = _run(["-i", str(src), "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-c:a", "aac", str(out)])
    if code == 0 and out.exists():
        return {"ok": True, "path": str(out)}
    return {"ok": False, "error": err[-400:]}
