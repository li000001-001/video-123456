"""任务编排：解析 → 分类 → 下载 → 字幕 → 配音 → 合成。

单核机器上所有任务串行执行，避免并发把系统拖垮。
任何可选环节（字幕/配音）失败都不影响已完成的部分，结果照常返回。
"""
from __future__ import annotations

import queue
import threading
import traceback
from pathlib import Path
from typing import Optional

from ..settings import settings
from . import classifier, dubbing, media, resolver, subtitle
from .store import store

DEFAULT_OPTIONS = {
    "download": True,
    "subtitle": True,
    "burn": True,
    "dubbing": False,
    "dubbing_text": "",
    "dubbing_voice": "",
    "dubbing_mode": "replace",  # replace=替换原声  mix=与原声混合
}

_QUEUE: "queue.Queue[tuple]" = queue.Queue()
_WORKER_STARTED = False
_WORKER_LOCK = threading.Lock()


def work_dir_of(tid: str) -> Path:
    d = settings.storage_root / "work" / tid
    d.mkdir(parents=True, exist_ok=True)
    return d


def _set(tid: str, persist: bool = False, **kw) -> None:
    store.update(tid, persist=persist, **kw)


def _media_url(tid: str, filename: str) -> str:
    return f"/media/work/{tid}/{filename}"


def submit(tid: str, url: str, options: Optional[dict] = None) -> None:
    opts = _merge_opts(options)
    _QUEUE.put((tid, url, opts))
    _ensure_worker()


def submit_local(tid: str, path: Path, options: Optional[dict] = None) -> None:
    opts = _merge_opts(options)
    _QUEUE.put((tid, {"__local__": str(path)}, opts))
    _ensure_worker()


def submit_watch(tid: str, metadata: dict, audio: Optional[Path] = None) -> None:
    """跟看任务复用同一串行队列，避免 Whisper 与下载任务争抢单核资源。"""
    target = {"__watch__": metadata, "__audio__": str(audio) if audio else ""}
    _QUEUE.put((tid, target, {}))
    _ensure_worker()


def _merge_opts(options: Optional[dict]) -> dict:
    opts = dict(DEFAULT_OPTIONS)
    if options:
        opts.update({k: v for k, v in options.items() if v is not None})
    return opts


def _ensure_worker() -> None:
    global _WORKER_STARTED
    with _WORKER_LOCK:
        if _WORKER_STARTED:
            return
        t = threading.Thread(target=_worker_loop, name="task-worker", daemon=True)
        t.start()
        _WORKER_STARTED = True


def _worker_loop() -> None:
    while True:
        tid, target, opts = _QUEUE.get()
        try:
            if isinstance(target, dict) and "__watch__" in target:
                audio = Path(target["__audio__"]) if target.get("__audio__") else None
                run_watch(tid, target["__watch__"], audio)
            elif isinstance(target, dict) and "__local__" in target:
                run_local(tid, Path(target["__local__"]), opts)
            else:
                run_task(tid, target, opts)
        except Exception as e:
            _set(
                tid,
                persist=True,
                status="failed",
                stage="失败",
                error=resolver.friendly_error(e) if "yt" in type(e).__module__ else str(e)[:300],
            )
            store.add_note(tid, traceback.format_exc()[-1200:])
        finally:
            _QUEUE.task_done()


def run_watch(tid: str, metadata: dict, audio: Optional[Path] = None) -> None:
    """融合端侧文字、OCR、图像标签与可选内录语音，不触碰视频下载主路径。"""
    work_dir_of(tid)
    _set(tid, persist=True, status="running", stage="整理跟看证据", progress=10)
    degradations = [str(x)[:200] for x in (metadata.get("degradations") or []) if str(x).strip()]
    transcript = ""
    engine = ""
    files = {}
    try:
        if audio and audio.exists():
            _set(tid, stage="识别跟看语音", progress=25)
            transcribed = subtitle.transcribe_audio(audio)
            if transcribed.get("ok"):
                transcript = transcribed.get("text", "")
                engine = transcribed.get("engine", "")
            else:
                degradations.append(f"语音转写未生成：{transcribed.get('error', '未知原因')}")
        else:
            degradations.append("无内录音频，仅使用画面与文字")
        _set(tid, stage="融合多模态分类", progress=65)

        ocr_samples = [str(x).strip() for x in (metadata.get("ocr_text") or []) if str(x).strip()]
        visuals = [str(x).strip() for x in (metadata.get("visual_tags") or []) if str(x).strip()]
        result = classifier.classifier.classify(
            title=metadata.get("title", ""),
            description="",
            author="",
            transcript=transcript,
            ocr_text="\n".join(ocr_samples),
            visual_tags=visuals,
        )
        degradations.extend(result.get("degradations") or [])
        degradations = list(dict.fromkeys(degradations))
        evidence = result.get("evidence") or {}
        evidence.update(
            {
                "frames": int(metadata.get("frames") or 0),
                "ocr_samples": len(ocr_samples),
                "audio_mode": metadata.get("audio_mode", "unavailable"),
                "transcript_engine": engine,
            }
        )
        if audio and settings.get("watch", "keep_audio", default=False):
            files["watch_audio"] = _media_url(tid, audio.name)
        for reason in degradations:
            store.add_note(tid, reason)
        _set(
            tid,
            persist=True,
            status="done",
            stage="已完成",
            progress=100,
            title=metadata.get("title", "") or "60秒跟看",
            duration=int(metadata.get("duration") or 0),
            category=result["category"],
            emoji=result["emoji"],
            confidence=result["confidence"],
            score=result["score"],
            matched=result["matched"],
            ranking=result["ranking"],
            subtitle_text=transcript,
            evidence=evidence,
            degradations=degradations,
            files=files,
        )
    finally:
        if audio and not settings.get("watch", "keep_audio", default=False):
            try:
                audio.unlink(missing_ok=True)
            except Exception:
                pass


def run_local(tid: str, video: Path, opts: dict) -> None:
    """本地视频：跳过解析与下载，直接字幕 / 配音。"""
    work = work_dir_of(tid)
    _set(tid, persist=True, status="running", stage="处理本地视频", progress=10)
    files = {"video": _media_url(tid, video.name)}

    subtitle_text = ""
    srt_path: Optional[Path] = None
    if opts.get("subtitle"):
        _set(tid, stage="识别字幕中", progress=30)
        sub = subtitle.generate(video, work)
        if sub.get("ok"):
            srt_path = Path(sub["srt_path"])
            subtitle_text = sub.get("text", "")
            files["srt"] = _media_url(tid, srt_path.name)
            _set(tid, persist=True, subtitle_text=subtitle_text, files=dict(files))
        else:
            store.add_note(tid, f"字幕未生成：{sub.get('error', '未知原因')}")
        _set(tid, progress=70)

    current = video
    if srt_path and opts.get("burn"):
        _set(tid, stage="烧录字幕中", progress=78)
        out = work / "subtitled.mp4"
        res = media.burn_subtitle(video, srt_path, out)
        if res.get("ok"):
            files["subtitled"] = _media_url(tid, out.name)
            current = out
        else:
            store.add_note(tid, "字幕烧录失败，已保留原片")
        _set(tid, persist=True, files=dict(files))

    if opts.get("dubbing"):
        _set(tid, stage="合成配音中", progress=90)
        text = (opts.get("dubbing_text") or subtitle_text or "").strip()
        sp = dubbing.speak(text, work, voice=opts.get("dubbing_voice") or None)
        if sp.get("ok"):
            files["dubbing"] = _media_url(tid, "dubbing.mp3")
            audio_path = Path(sp["path"])
            out = work / "dubbed.mp4"
            if opts.get("dubbing_mode") == "mix" and media.has_audio(current):
                mix = _mix_audio(current, audio_path, out)
            else:
                mix = media.replace_audio(current, audio_path, out)
            if mix.get("ok"):
                files["dubbed"] = _media_url(tid, out.name)
            else:
                store.add_note(tid, f"配音合成失败：{mix.get('error', '')}")
        else:
            store.add_note(tid, sp.get("error", "配音失败"))

    _set(tid, persist=True, status="done", stage="已完成", progress=100, files=dict(files))


def run_task(tid: str, url: str, opts: dict) -> None:
    work = work_dir_of(tid)
    _set(tid, persist=True, status="running", stage="解析链接", progress=5)

    # ---------- 1. 读取元数据 ----------
    try:
        info = resolver.probe(url)
    except Exception as e:
        _set(tid, persist=True, status="failed", stage="解析失败", error=resolver.friendly_error(e))
        return

    result = classifier.classifier.classify(
        title=info.get("title", ""),
        tags=info.get("tags", []),
        description=info.get("description", ""),
        author=info.get("uploader", ""),
    )

    files = {}
    # 封面
    if info.get("thumbnail"):
        files["thumbnail"] = info["thumbnail"]

    _set(
        tid,
        persist=True,
        stage="已识别分类",
        progress=15,
        title=info.get("title", ""),
        author=info.get("uploader", ""),
        duration=info.get("duration", 0),
        category=result["category"],
        emoji=result["emoji"],
        confidence=result["confidence"],
        score=result["score"],
        matched=result["matched"],
        ranking=result["ranking"],
        files=files,
    )

    if not opts.get("download"):
        _set(tid, persist=True, status="done", stage="完成（仅分类）", progress=100)
        return

    # ---------- 2. 下载原片（去水印） ----------
    _set(tid, stage="下载中", progress=20)

    def on_progress(p: float) -> None:
        _set(tid, progress=round(20 + p * 35, 1))

    try:
        video = resolver.download(url, work, on_progress=on_progress)
    except Exception as e:
        _set(tid, persist=True, status="failed", stage="下载失败", error=resolver.friendly_error(e))
        return

    files["video"] = _media_url(tid, video.name)
    _set(tid, persist=True, progress=55, files=dict(files))

    # ---------- 3. 字幕 ----------
    subtitle_text = ""
    srt_path: Optional[Path] = None
    if opts.get("subtitle"):
        _set(tid, stage="识别字幕中")
        sub = subtitle.generate(video, work)
        if sub.get("ok"):
            srt_path = Path(sub["srt_path"])
            subtitle_text = sub.get("text", "")
            files["srt"] = _media_url(tid, srt_path.name)
            _set(tid, persist=True, subtitle_text=subtitle_text, files=dict(files))
            if sub.get("truncated"):
                store.add_note(tid, "视频较长，字幕仅识别了前段内容")
        else:
            store.add_note(tid, f"字幕未生成：{sub.get('error', '未知原因')}")
        _set(tid, progress=70)

    # ---------- 4. 烧录字幕 ----------
    current = video
    if srt_path and opts.get("burn"):
        _set(tid, stage="烧录字幕中", progress=75)
        out = work / "subtitled.mp4"
        res = media.burn_subtitle(video, srt_path, out)
        if res.get("ok"):
            files["subtitled"] = _media_url(tid, out.name)
            current = out
            if res.get("mode") == "soft":
                store.add_note(tid, "本机 ffmpeg 不支持硬字幕，已改为内嵌软字幕")
        else:
            store.add_note(tid, "字幕烧录失败，已保留原片")
        _set(tid, persist=True, progress=85, files=dict(files))

    # ---------- 5. 配音 ----------
    if opts.get("dubbing"):
        _set(tid, stage="合成配音中", progress=88)
        text = (opts.get("dubbing_text") or subtitle_text or "").strip()
        voice = opts.get("dubbing_voice") or None
        sp = dubbing.speak(text, work, voice=voice)
        if sp.get("ok"):
            files["dubbing"] = _media_url(tid, "dubbing.mp3")
            audio_path = Path(sp["path"])
            out = work / "dubbed.mp4"
            if opts.get("dubbing_mode") == "mix" and media.has_audio(current):
                mix = _mix_audio(current, audio_path, out)
            else:
                mix = media.replace_audio(current, audio_path, out)
            if mix.get("ok"):
                files["dubbed"] = _media_url(tid, out.name)
                current = out
            else:
                store.add_note(tid, f"配音合成失败：{mix.get('error', '')}")
        else:
            store.add_note(tid, sp.get("error", "配音失败"))
        _set(tid, persist=True, files=dict(files))

    _set(
        tid,
        persist=True,
        status="done",
        stage="已完成",
        progress=100,
        files=dict(files),
    )


def _mix_audio(video: Path, audio: Path, out: Path) -> dict:
    """原声压低 + 配音叠加。"""
    import subprocess

    ff = media.ffmpeg_path()
    cmd = [
        ff,
        "-y",
        "-hide_banner",
        "-i",
        str(video),
        "-i",
        str(audio),
        "-filter_complex",
        "[0:a]volume=0.25[a0];[1:a]volume=1.0[a1];[a0][a1]amix=inputs=2:duration=first:dropout_transition=2",
        "-c:v",
        "copy",
        "-c:a",
        "aac",
        str(out),
    ]
    p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="ignore")
    if p.returncode == 0 and out.exists():
        return {"ok": True, "path": str(out)}
    # 混合失败则退回替换
    return media.replace_audio(video, audio, out)
