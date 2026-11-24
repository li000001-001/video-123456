"""HTTP 接口层。"""
from __future__ import annotations

import json
import shutil
import threading
from pathlib import Path
from typing import List, Optional

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .core import classifier, dubbing, pipeline, resolver
from .core.store import store
from .settings import settings

STATIC_DIR = Path(__file__).resolve().parent / "static"

app = FastAPI(title="视频分类助手", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.get("server", "cors", default="*")],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 媒体文件直链
app.mount("/media", StaticFiles(directory=str(settings.storage_root)), name="media")


@app.middleware("http")
async def access_guard(request: Request, call_next):
    """可选的访问口令保护，部署到公网时启用，防止被他人白嫖算力。"""
    key = settings.get("server", "access_key", default="") or ""
    if not key or request.url.path == "/api/health":
        return await call_next(request)
    provided = request.query_params.get("k") or request.cookies.get("vca_key") or ""
    if provided != key:
        return JSONResponse({"detail": "需要访问口令，请在网址后加 ?k=你的口令"}, status_code=401)
    resp = await call_next(request)
    if request.query_params.get("k") == key:
        resp.set_cookie("vca_key", key, max_age=86400 * 365, httponly=True, samesite="lax")
    return resp


# ---------------- 数据模型 ----------------
class ParseIn(BaseModel):
    text: str


class TaskIn(BaseModel):
    text: str = ""
    url: str = ""
    download: bool = True
    subtitle: bool = True
    burn: bool = True
    dubbing: bool = False
    dubbing_text: str = ""
    dubbing_voice: str = ""
    dubbing_mode: str = "replace"


class ClassifyIn(BaseModel):
    title: str = ""
    tags: List[str] = []
    description: str = ""
    author: str = ""


class BridgeIn(BaseModel):
    title: str = ""
    description: str = ""
    author: str = ""
    package: str = ""
    token: str = ""


class WatchMeta(BaseModel):
    title: str = ""
    package: str = ""
    duration: int = 60
    frames: int = 0
    ocr_text: List[str] = []
    visual_tags: List[str] = []
    degradations: List[str] = []
    audio_mode: str = "unavailable"


# ---------------- 基础 ----------------
@app.get("/api/health")
def health():
    import platform

    return {
        "ok": True,
        "version": app.version,
        "python": platform.python_version(),
        "storage": str(settings.storage_root),
        "platforms": settings.get("download", "platforms", default=[]),
    }


@app.get("/api/categories")
def categories():
    return {
        "categories": classifier.classifier.all_categories,
        "fallback": classifier.classifier.fallback,
    }


@app.post("/api/reload")
def reload_rules():
    """修改 config.yaml 或 categories.yaml 后无需重启服务。"""
    settings.reload()
    classifier.classifier.load()
    return {"ok": True, "categories": len(classifier.classifier.all_categories)}


@app.get("/api/voices")
def voices():
    return {"voices": dubbing.VOICES, "default": settings.get("dubbing", "voice")}


@app.get("/api/config")
def public_config():
    """给前端用的脱敏配置。"""
    return {
        "subtitle_local_ready": _module_exists("faster_whisper"),
        "max_seconds": settings.get("subtitle", "max_seconds"),
        "burn_default": settings.get("media", "burn_subtitle"),
        "watch": {
            "duration_seconds": settings.get("watch", "duration_seconds", default=60),
            "sample_interval_seconds": settings.get("watch", "sample_interval_seconds", default=5),
            "audio_max_mb": settings.get("watch", "audio_max_mb", default=4),
        },
        "platforms": [
            {"id": k, "label": v} for k, v in resolver.PLATFORM_LABEL.items() if k != "unknown"
        ],
    }


def _module_exists(name: str) -> bool:
    import importlib.util

    return importlib.util.find_spec(name) is not None


# ---------------- 解析与任务 ----------------
@app.post("/api/parse")
def parse(body: ParseIn):
    urls = resolver.extract_urls(body.text)
    if not urls:
        return {"urls": [], "hint": "没有识别到链接，请粘贴完整的分享内容"}
    out = []
    for u in urls:
        p = resolver.detect_platform(u)
        out.append({"url": u, "platform": p, "label": resolver.PLATFORM_LABEL.get(p, "未知平台")})
    return {"urls": out}


@app.post("/api/classify")
def classify(body: ClassifyIn):
    return classifier.classifier.classify(
        title=body.title,
        tags=body.tags,
        description=body.description,
        author=body.author,
    )


@app.post("/api/bridge/report")
def bridge_report(body: BridgeIn):
    """原生端「无障碍自动识别」上报当前视频文字，仅分类不下载。"""
    token = settings.get("bridge", "token", default="") or ""
    if token and body.token != token:
        raise HTTPException(401, "令牌不匹配")
    result = classifier.classifier.classify(
        title=body.title, description=body.description, author=body.author
    )
    result["package"] = body.package
    return result


@app.post("/api/bridge/watch")
def bridge_watch(
    metadata: str = Form(...),
    token: str = Form(""),
    audio: Optional[UploadFile] = File(None),
):
    """接收 60 秒跟看元数据与可选内录 WAV，原始画面不上传。"""
    bridge_token = settings.get("bridge", "token", default="") or ""
    if bridge_token and token != bridge_token:
        raise HTTPException(401, "令牌不匹配")
    if len(metadata) > 200_000:
        raise HTTPException(413, "跟看元数据过大")
    try:
        meta = WatchMeta.model_validate(json.loads(metadata))
    except Exception:
        raise HTTPException(400, "跟看元数据格式错误")

    max_duration = max(1, int(settings.get("watch", "duration_seconds", default=60) or 60))
    if meta.duration < 1 or meta.duration > max_duration:
        raise HTTPException(400, f"跟看时长需在 1 到 {max_duration} 秒之间")
    raw = meta.model_dump()
    raw["title"] = meta.title.strip()[:1500]
    raw["package"] = meta.package.strip()[:200]
    raw["frames"] = max(0, min(meta.frames, 120))
    raw["ocr_text"] = list(
        dict.fromkeys(x.strip()[:2000] for x in meta.ocr_text if x.strip())
    )[:32]
    raw["visual_tags"] = list(
        dict.fromkeys(x.strip()[:80] for x in meta.visual_tags if x.strip())
    )[:100]
    raw["degradations"] = list(
        dict.fromkeys(x.strip()[:200] for x in meta.degradations if x.strip())
    )[:12]
    raw["audio_mode"] = "internal" if audio and meta.audio_mode == "internal" else "unavailable"

    task = store.create("", "watch", "60秒跟看", {"duration": meta.duration})
    tid = task["id"]
    work = pipeline.work_dir_of(tid)
    dest = None
    if audio:
        max_bytes = int(float(settings.get("watch", "audio_max_mb", default=4) or 4) * 1024 * 1024)
        dest = work / "watch.wav"
        try:
            total = 0
            with open(dest, "wb") as f:
                while True:
                    chunk = audio.file.read(1024 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > max_bytes:
                        raise HTTPException(413, "内录音频超过大小上限")
                    f.write(chunk)
            with open(dest, "rb") as f:
                header = f.read(12)
            if total < 44 or len(header) != 12 or header[:4] != b"RIFF" or header[8:12] != b"WAVE":
                raise HTTPException(400, "仅支持有效的 16 kHz WAV 内录音频")
        except Exception:
            store.delete(tid)
            shutil.rmtree(work, ignore_errors=True)
            raise

    store.update(tid, persist=True, title=raw["title"] or "60秒跟看", stage="排队中")
    pipeline.submit_watch(tid, raw, dest)
    return store.get(tid)


@app.post("/api/tasks")
def create_task(body: TaskIn):
    raw = (body.url or "").strip()
    if not raw:
        urls = resolver.extract_urls(body.text)
        if not urls:
            raise HTTPException(400, "没有识别到视频链接")
        raw = urls[0]
    platform = resolver.detect_platform(raw)
    allowed = settings.get("download", "platforms", default=[])
    if platform not in allowed and platform != "unknown":
        raise HTTPException(400, f"平台 {platform} 未启用")
    opts = body.model_dump()
    opts.pop("text", None)
    opts.pop("url", None)
    task = store.create(raw, platform, resolver.PLATFORM_LABEL.get(platform, "未知平台"), opts)
    pipeline.submit(task["id"], raw, opts)
    return task


@app.get("/api/tasks")
def list_tasks(limit: int = 100):
    return {"tasks": store.list(limit)}


@app.get("/api/tasks/{tid}")
def get_task(tid: str):
    t = store.get(tid)
    if not t:
        raise HTTPException(404, "任务不存在")
    return t


@app.delete("/api/tasks/{tid}")
def delete_task(tid: str):
    if not store.delete(tid):
        raise HTTPException(404, "任务不存在")
    work = settings.storage_root / "work" / tid
    if work.exists():
        shutil.rmtree(work, ignore_errors=True)
    return {"ok": True}


@app.get("/api/tasks/{tid}/file/{kind}")
def download_file(tid: str, kind: str):
    t = store.get(tid)
    if not t:
        raise HTTPException(404, "任务不存在")
    exts_map = {
        "video": [".mp4", ".webm", ".mkv", ".flv", ".mov"],
        "subtitled": [".mp4"],
        "dubbed": [".mp4"],
        "srt": [".srt"],
        "dubbing": [".mp3"],
    }
    if kind not in exts_map:
        raise HTTPException(400, "不支持的文件类型")
    work = settings.storage_root / "work" / tid
    exts = exts_map[kind]
    base = {"video": "video", "subtitled": "subtitled", "dubbed": "dubbed", "srt": "subtitle", "dubbing": "dubbing"}[kind]
    target = None
    for e in exts:
        cand = work / (base + e)
        if cand.exists():
            target = cand
            break
    if not target:
        # 原片名字带平台 id，兜底扫描
        cands = [
            p
            for p in work.glob("*")
            if p.suffix.lower() in exts and p.name not in {"subtitled.mp4", "dubbed.mp4"}
        ]
        if kind == "video" and cands:
            target = sorted(cands, key=lambda p: p.stat().st_size, reverse=True)[0]
    if not target or not target.exists():
        raise HTTPException(404, "文件不存在")
    return FileResponse(str(target), filename=f"{t.get('category', 'video')}_{target.name}")


@app.post("/api/tasks/upload")
def upload_local(
    file: UploadFile = File(...),
    subtitle: bool = Form(True),
    burn: bool = Form(True),
    dubbing: bool = Form(False),
    dubbing_text: str = Form(""),
    dubbing_voice: str = Form(""),
    dubbing_mode: str = Form("replace"),
):
    tid_task = store.create("", "local", "本地视频", {})
    tid = tid_task["id"]
    work = pipeline.work_dir_of(tid)
    suffix = Path(file.filename or "video.mp4").suffix or ".mp4"
    dest = work / f"video{suffix}"
    with open(dest, "wb") as f:
        shutil.copyfileobj(file.file, f)
    store.update(tid, persist=True, title=file.filename or "本地视频", stage="排队中")
    pipeline.submit_local(
        tid,
        dest,
        {
            "download": False,
            "subtitle": subtitle,
            "burn": burn,
            "dubbing": dubbing,
            "dubbing_text": dubbing_text,
            "dubbing_voice": dubbing_voice,
            "dubbing_mode": dubbing_mode,
        },
    )
    return store.get(tid)


# ---------------- 静态站点（PWA） ----------------
@app.get("/share")
def share_page():
    """系统分享菜单跳到此处，由前端读取参数并自动填入。"""
    return FileResponse(str(STATIC_DIR / "index.html"))


@app.on_event("startup")
def _startup():
    def loop():
        import time

        while True:
            try:
                store.cleanup_old_files()
            except Exception:
                pass
            time.sleep(3600)

    threading.Thread(target=loop, daemon=True).start()


if STATIC_DIR.exists():
    app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
