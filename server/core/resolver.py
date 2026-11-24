"""链接解析、平台识别与视频下载（去水印）。

核心引擎使用 yt-dlp：它是社区持续维护的纯 Python 解析库，
对抖音/快手/B站/TikTok/X 都有专门提取器，且抖音会优先取无水印源。
自行解析各平台接口的做法极易随平台改版失效，故不采用。
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Callable, Dict, List, Optional
from urllib.parse import urlparse

from ..settings import settings

# 分享文案里的链接提取（兼容中文标点收尾）
URL_RE = re.compile(r"https?://[^\s<>\"'，。；、）】」》]+")

PLATFORM_HOSTS: Dict[str, List[str]] = {
    "douyin": ["douyin.com", "iesdouyin.com", "amemv.com"],
    "toutiao": ["toutiao.com", "ixigua.com", "snssdk.com"],
    "kuaishou": ["kuaishou.com", "gifshow.com", "chenzhongtech.com", "kwai.com"],
    "bilibili": ["bilibili.com", "b23.tv", "bili2233.cn"],
    "tiktok": ["tiktok.com"],
    "x": ["twitter.com", "x.com", "t.co"],
}

PLATFORM_LABEL = {
    "douyin": "抖音",
    "toutiao": "今日头条",
    "kuaishou": "快手",
    "bilibili": "哔哩哔哩",
    "tiktok": "TikTok",
    "x": "X",
    "unknown": "未知平台",
}


def extract_urls(text: str) -> List[str]:
    """从分享文案中提取所有链接，去重保序。"""
    if not text:
        return []
    seen: set = set()
    out: List[str] = []
    for m in URL_RE.findall(text):
        u = m.rstrip("/.,;)")
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def detect_platform(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    for name, hosts in PLATFORM_HOSTS.items():
        if any(host == h or host.endswith("." + h) for h in hosts):
            return name
    return "unknown"


def _ydl_opts(extra: Optional[dict] = None) -> dict:
    dl = settings.get("download", default={}) or {}
    proxy = dl.get("proxy") or None
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "noprogress": True,
        "socket_timeout": int(dl.get("timeout", 30)),
        "retries": 2,
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        },
    }
    if proxy:
        opts["proxy"] = proxy
    cookie_file = dl.get("cookies_file") or ""
    if cookie_file:
        p = Path(cookie_file)
        if not p.is_absolute():
            p = Path(__file__).resolve().parent.parent.parent / p
        if p.exists():
            opts["cookiefile"] = str(p)
    if extra:
        opts.update(extra)
    return opts


def probe(url: str) -> dict:
    """只取元数据不下载，用于分类。"""
    from yt_dlp import YoutubeDL

    with YoutubeDL(_ydl_opts({"skip_download": True})) as ydl:
        info = ydl.extract_info(url, download=False)
    if info and info.get("entries"):
        info = info["entries"][0]
    if not info:
        raise RuntimeError("未能读取视频信息")
    return {
        "id": str(info.get("id", "")),
        "title": info.get("title") or "",
        "description": info.get("description") or "",
        "tags": [str(t) for t in (info.get("tags") or [])][:30],
        "uploader": info.get("uploader") or info.get("channel") or "",
        "duration": info.get("duration") or 0,
        "thumbnail": info.get("thumbnail") or "",
        "webpage_url": info.get("webpage_url") or url,
        "extractor": info.get("extractor_key") or info.get("extractor") or "",
    }


def download(
    url: str,
    out_dir: Path,
    on_progress: Optional[Callable[[float], None]] = None,
) -> Path:
    """下载视频到 out_dir，返回文件路径。优先无水印/最高画质。"""
    from yt_dlp import YoutubeDL

    out_dir.mkdir(parents=True, exist_ok=True)
    dl = settings.get("download", default={}) or {}
    max_height = int(dl.get("max_height", 1080) or 0)

    if max_height:
        fmt = (
            f"bestvideo[height<={max_height}]+bestaudio/"
            f"best[height<={max_height}]/best"
        )
    else:
        fmt = "bestvideo+bestaudio/best"

    def hook(d: dict) -> None:
        if on_progress and d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            got = d.get("downloaded_bytes") or 0
            if total:
                on_progress(round(got / total, 3))

    opts = _ydl_opts(
        {
            "format": fmt,
            "outtmpl": str(out_dir / "%(id)s.%(ext)s"),
            "merge_output_format": "mp4",
            "progress_hooks": [hook],
            "overwrites": True,
            "max_filesize": int(dl.get("max_size_mb", 800)) * 1024 * 1024,
        }
    )
    ffmpeg = _ffmpeg_path()
    if ffmpeg:
        # imageio-ffmpeg 的可执行文件带版本号，必须传完整路径，
        # 只传目录时 yt-dlp 只会寻找 ffmpeg.exe 并误判为未安装。
        opts["ffmpeg_location"] = ffmpeg

    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        if info and info.get("entries"):
            info = info["entries"][0]
        path = None
        if info:
            path = info.get("requested_downloads", [{}])[0].get("filepath")
        if not path:
            cands = sorted(out_dir.glob("*"), key=lambda p: p.stat().st_mtime, reverse=True)
            path = str(cands[0]) if cands else None
    if not path or not Path(path).exists():
        raise RuntimeError("下载失败：未生成文件")
    return Path(path)


def _ffmpeg_path() -> Optional[str]:
    configured = settings.get("media", "ffmpeg", default="") or ""
    if configured and Path(configured).exists():
        return configured
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


def friendly_error(exc: Exception) -> str:
    """把 yt-dlp 的英文异常转成中文可读提示。"""
    msg = str(exc)
    low = msg.lower()
    if "login" in low or "cookie" in low or "private" in low:
        return "该视频需要登录态，请在 config.yaml 配置 cookies_file 后重试"
    if "unsupported url" in low:
        return "暂不支持该链接，请确认是视频分享页而不是主页/直播"
    if "404" in low or "not found" in low:
        return "视频不存在或已被删除"
    if "timed out" in low or "timeout" in low:
        return "网络超时，请检查网络或配置代理"
    if "geo" in low and "restrict" in low:
        return "该视频有地区限制，需要配置代理"
    if "signature" in low or "403" in low:
        return "平台风控拦截，稍后重试或更新 yt-dlp（pip install -U yt-dlp）"
    return msg[:300]
