"""自动配音：edge-tts（微软免费语音合成通道），零成本、中文音色自然。"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Optional

from ..settings import settings

# 常用中文音色，供前端下拉展示
VOICES = [
    {"id": "zh-CN-XiaoxiaoNeural", "name": "晓晓 · 女声 · 温柔"},
    {"id": "zh-CN-XiaoyiNeural", "name": "晓伊 · 女声 · 活泼"},
    {"id": "zh-CN-YunxiNeural", "name": "云希 · 男声 · 阳光"},
    {"id": "zh-CN-YunjianNeural", "name": "云健 · 男声 · 解说"},
    {"id": "zh-CN-YunyangNeural", "name": "云扬 · 男声 · 新闻"},
    {"id": "zh-CN-liaoning-XiaobeiNeural", "name": "晓北 · 东北话"},
    {"id": "zh-CN-shaanxi-XiaoniNeural", "name": "晓妮 · 陕西话"},
]


async def _synth(text: str, voice: str, rate: str, volume: str, out: Path) -> None:
    import edge_tts

    communicate = edge_tts.Communicate(text, voice, rate=rate, volume=volume)
    await communicate.save(str(out))


def speak(
    text: str,
    work_dir: Path,
    voice: Optional[str] = None,
    rate: Optional[str] = None,
    volume: Optional[str] = None,
) -> dict:
    """把文本合成 mp3，返回 {ok, path, error}。"""
    if not settings.get("dubbing", "enabled", default=True):
        return {"ok": False, "error": "配音功能已在配置中关闭"}
    text = (text or "").strip()
    if not text:
        return {"ok": False, "error": "配音文本为空"}
    if len(text) > 5000:
        text = text[:5000]

    cfg_voice = settings.get("dubbing", "voice", default="zh-CN-XiaoxiaoNeural")
    cfg_rate = settings.get("dubbing", "rate", default="+0%")
    cfg_volume = settings.get("dubbing", "volume", default="+0%")

    work_dir.mkdir(parents=True, exist_ok=True)
    out = work_dir / "dubbing.mp3"
    try:
        asyncio.run(
            _synth(
                text,
                voice or cfg_voice,
                rate or cfg_rate,
                volume or cfg_volume,
                out,
            )
        )
    except Exception as e:
        return {"ok": False, "error": f"配音合成失败：{e}"}
    if not out.exists() or out.stat().st_size == 0:
        return {"ok": False, "error": "配音合成失败：输出为空"}
    return {"ok": True, "path": str(out)}
