"""自动字幕：本地 faster-whisper 或云端 Whisper 端点，输出标准 SRT。

设计原则：字幕是可选增强能力，任何环节失败都不能影响视频下载与分类主流程。
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import List, Optional

from ..settings import settings
from . import media

_models: dict = {}


def _srt_time(seconds: float) -> str:
    if seconds < 0:
        seconds = 0
    ms = int(round(seconds * 1000))
    h, ms = divmod(ms, 3600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def segments_to_srt(segments: List[dict]) -> str:
    lines: List[str] = []
    for i, seg in enumerate(segments, 1):
        text = (seg.get("text") or "").strip()
        if not text:
            continue
        start = _srt_time(float(seg.get("start", 0)))
        end = _srt_time(float(seg.get("end", 0)))
        lines.append(f"{i}\n{start} --> {end}\n{text}\n")
    return "\n".join(lines)


def _get_model():
    model_name = settings.get("subtitle", "model", default="base")
    if model_name in _models:
        return _models[model_name]
    import os

    # 国内网络直连 HuggingFace 经常超时，默认走镜像，用户可自行覆盖
    os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
    from faster_whisper import WhisperModel

    model_dir = settings.get("subtitle", "model_dir", default="data/models")
    p = Path(model_dir)
    if not p.is_absolute():
        p = Path(__file__).resolve().parent.parent.parent / p
    p.mkdir(parents=True, exist_ok=True)
    # CPU 用 int8 量化，内存占用最低
    model = WhisperModel(str(model_name), device="cpu", compute_type="int8", download_root=str(p))
    _models[model_name] = model
    return model


def _to_simplified(text: str) -> str:
    """Whisper 中文常输出繁体，统一转简体。缺失依赖时原样返回。"""
    if not text:
        return text
    try:
        import zhconv

        return zhconv.convert(text, "zh-cn")
    except Exception:
        return text


def _transcribe_local(audio_path: Path) -> List[dict]:
    model = _get_model()
    language = settings.get("subtitle", "language", default="zh") or None
    segments, _info = model.transcribe(
        str(audio_path),
        language=language,
        beam_size=1,
        vad_filter=True,
        condition_on_previous_text=False,
    )
    return [
        {"start": s.start, "end": s.end, "text": _to_simplified(s.text)}
        for s in segments
    ]


async def _transcribe_remote(audio_path: Path, endpoint: str, token: str) -> List[dict]:
    import httpx

    headers = {"Authorization": f"Bearer {token}"} if token else {}
    async with httpx.AsyncClient(timeout=600) as client:
        with open(audio_path, "rb") as f:
            resp = await client.post(
                endpoint,
                files={"file": (audio_path.name, f, "audio/wav")},
                headers=headers,
            )
        resp.raise_for_status()
        data = resp.json()
    if "segments" in data:
        return data["segments"]
    if "srt" in data:
        return _srt_to_segments(data["srt"])
    raise RuntimeError("云端返回格式不支持")


def _srt_to_segments(srt: str) -> List[dict]:
    """把 SRT 解析回 segments，方便统一处理。"""

    def to_sec(t: str) -> float:
        hh, mm, rest = t.split(":")
        ss, ms = rest.replace(".", ",").split(",")
        return int(hh) * 3600 + int(mm) * 60 + int(ss) + int(ms) / 1000

    segs: List[dict] = []
    for block in srt.strip().split("\n\n"):
        lines = [ln for ln in block.splitlines() if ln.strip()]
        if len(lines) < 3 or "-->" not in lines[1]:
            continue
        start_s, end_s = [x.strip() for x in lines[1].split("-->")]
        try:
            segs.append(
                {
                    "start": to_sec(start_s),
                    "end": to_sec(end_s),
                    "text": " ".join(lines[2:]).strip(),
                }
            )
        except Exception:
            continue
    return segs


def transcribe_audio(audio_path: Path) -> dict:
    """转写已有音频，供字幕与 60 秒跟看共用，不写任何字幕文件。"""
    endpoint = (settings.get("subtitle", "remote_endpoint", default="") or "").strip()
    token = settings.get("subtitle", "remote_token", default="") or ""
    engine = "local"
    try:
        if endpoint:
            engine = "remote"
            segments = asyncio.run(_transcribe_remote(audio_path, endpoint, token))
        else:
            segments = _transcribe_local(audio_path)
    except ImportError:
        return {
            "ok": False,
            "error": "本地字幕组件未安装，执行：pip install faster-whisper（或配置云端端点）",
        }
    except Exception as e:
        return {"ok": False, "error": f"字幕识别失败：{e}"}

    if not segments:
        return {"ok": False, "error": "未识别到语音内容"}
    return {
        "ok": True,
        "segments": segments,
        "text": "\n".join((s.get("text") or "").strip() for s in segments if (s.get("text") or "").strip()),
        "engine": engine,
    }


def generate(video_path: Path, work_dir: Path) -> dict:
    """为视频生成字幕，返回 {ok, srt_path, text, segments, engine, error}。"""
    if not settings.get("subtitle", "enabled", default=True):
        return {"ok": False, "error": "字幕功能已在配置中关闭"}

    work_dir.mkdir(parents=True, exist_ok=True)
    audio = work_dir / "audio.wav"
    try:
        media.extract_audio(video_path, audio)
    except Exception as e:
        return {"ok": False, "error": f"提取音频失败：{e}"}

    max_seconds = float(settings.get("subtitle", "max_seconds", default=600) or 0)
    duration = media.duration_of(video_path)
    if max_seconds and duration and duration > max_seconds:
        audio = media.trim_audio(audio, work_dir / "audio_trim.wav", max_seconds)

    transcribed = transcribe_audio(audio)
    if not transcribed.get("ok"):
        return transcribed
    segments = transcribed["segments"]
    srt = segments_to_srt(segments)
    srt_path = work_dir / "subtitle.srt"
    srt_path.write_text(srt, encoding="utf-8")
    text = "".join((s.get("text") or "").strip() for s in segments)
    return {
        "ok": True,
        "srt_path": str(srt_path),
        "text": text,
        "segments": segments,
        "engine": transcribed["engine"],
        "truncated": bool(max_seconds and duration and duration > max_seconds),
    }
