"""全局配置加载：所有可调参数来自 config.yaml，代码中不硬编码。"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.yaml"


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


_DEFAULTS: Dict[str, Any] = {
    "server": {"host": "0.0.0.0", "port": 8000, "cors": "*"},
    "storage": {"root": "data", "keep_hours": 72},
    "download": {
        "platforms": ["douyin", "toutiao", "kuaishou", "bilibili", "tiktok", "x"],
        "timeout": 30,
        "max_size_mb": 800,
        "proxy": "",
        "prefer_no_watermark": True,
        "max_height": 1080,
    },
    "classify": {
        "min_score": 3,
        "use_jieba": True,
        "weight": {"title": 1.0, "tags": 1.4, "description": 0.6, "author": 0.4},
    },
    "watch": {
        "duration_seconds": 60,
        "sample_interval_seconds": 5,
        "audio_max_mb": 4,
        "keep_audio": False,
        "weight": {"transcript": 1.3, "ocr_text": 1.1, "visual_tags": 0.8},
    },
    "subtitle": {
        "enabled": True,
        "remote_endpoint": "",
        "remote_token": "",
        "model": "base",
        "language": "zh",
        "model_dir": "data/models",
        "max_seconds": 600,
    },
    "dubbing": {
        "enabled": True,
        "voice": "zh-CN-XiaoxiaoNeural",
        "rate": "+0%",
        "volume": "+0%",
    },
    "media": {"ffmpeg": "", "burn_subtitle": True, "subtitle_font_size": 22},
    "bridge": {"token": ""},
}


class Settings:
    def __init__(self, path: Path = CONFIG_PATH):
        self.path = path
        self.data = self._load()

    def _load(self) -> Dict[str, Any]:
        merged = dict(_DEFAULTS)
        if self.path.exists():
            with open(self.path, "r", encoding="utf-8") as f:
                user_cfg = yaml.safe_load(f) or {}
            merged = _deep_merge(merged, user_cfg)
        # 环境变量覆盖端口，方便云平台部署
        if os.getenv("PORT"):
            merged["server"]["port"] = int(os.environ["PORT"])
        return merged

    def get(self, *keys: str, default: Any = None) -> Any:
        cur: Any = self.data
        for k in keys:
            if not isinstance(cur, dict) or k not in cur:
                return default
            cur = cur[k]
        return cur

    def reload(self) -> None:
        self.data = self._load()

    @property
    def storage_root(self) -> Path:
        p = Path(self.get("storage", "root", default="data"))
        if not p.is_absolute():
            p = ROOT / p
        p.mkdir(parents=True, exist_ok=True)
        return p


settings = Settings()
