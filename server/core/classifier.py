"""视频分类引擎：按今日头条频道体系，基于标题/话题/简介/作者的加权关键词匹配。

设计原则：
1. 规则全部来自 resources/categories.yaml，可随时增删，不改代码；
2. 中文用子串匹配（比分词更鲁棒），英文短词用词边界匹配（避免 AI 命中 MAIL）；
3. 同一关键词在多个字段出现只取最高字段权重，防止堆词刷分。
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import yaml

from ..settings import settings

_CATEGORY_FILE = Path(__file__).resolve().parent.parent / "resources" / "categories.yaml"
_ASCII_WORD = re.compile(r"^[A-Za-z0-9\s\-\.\+#]+$")


class Classifier:
    def __init__(self, path: Path = _CATEGORY_FILE):
        self.path = path
        self.ordered: List[str] = []
        self.meta: Dict[str, dict] = {}
        self.fallback = {"name": "其他", "emoji": "📌"}
        self._jieba = None
        self.load()

    def load(self) -> None:
        with open(self.path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        cats = data.get("categories") or {}
        self.ordered = list(cats.keys())
        self.meta = {}
        for name, cfg in cats.items():
            kws: Dict[str, float] = {}
            for kw, weight in (cfg.get("keywords") or {}).items():
                kws[str(kw)] = float(weight)
            self.meta[name] = {"emoji": cfg.get("emoji", "📄"), "keywords": kws}
        fb = data.get("fallback") or {}
        self.fallback = {
            "name": fb.get("name", "其他"),
            "emoji": fb.get("emoji", "📌"),
        }
        use_jieba = settings.get("classify", "use_jieba", default=True)
        if use_jieba and self._jieba is None:
            try:
                import jieba  # 首次加载会建词典，较慢，故懒加载

                jieba.setLogLevel(60)
                self._jieba = jieba
            except Exception:
                self._jieba = None

    # ---------- 匹配 ----------
    @staticmethod
    def _hit(keyword: str, text: str, tokens: Optional[set]) -> bool:
        if not text:
            return False
        if _ASCII_WORD.match(keyword):
            # 英文/数字关键词：词边界匹配，忽略大小写
            pattern = r"(?<![A-Za-z0-9])" + re.escape(keyword) + r"(?![A-Za-z0-9])"
            return re.search(pattern, text, re.IGNORECASE) is not None
        if keyword in text:
            return True
        # 分词兜底：处理「新建了个模型」这类被断开的组合词
        if tokens and keyword in tokens:
            return True
        return False

    def classify(
        self,
        title: str = "",
        tags: Optional[List[str]] = None,
        description: str = "",
        author: str = "",
        transcript: Optional[str] = None,
        ocr_text: Optional[str] = None,
        visual_tags: Optional[List[str]] = None,
    ) -> dict:
        weights = settings.get("classify", "weight", default={}) or {}
        watch_weights = settings.get("watch", "weight", default={}) or {}
        multimodal = transcript is not None or ocr_text is not None or visual_tags is not None

        def dedupe_lines(value: Optional[str]) -> str:
            if not value:
                return ""
            return "\n".join(dict.fromkeys(line.strip() for line in value.splitlines() if line.strip()))

        transcript_text = dedupe_lines(transcript)
        ocr = dedupe_lines(ocr_text)
        visuals = list(dict.fromkeys(str(v).strip() for v in (visual_tags or []) if str(v).strip()))
        tag_text = " ".join(tags or [])
        fields: List[Tuple[str, str, float]] = [
            ("title", title or "", float(weights.get("title", 1.0))),
            ("tags", tag_text, float(weights.get("tags", 1.4))),
            ("description", description or "", float(weights.get("description", 0.6))),
            ("author", author or "", float(weights.get("author", 0.4))),
        ]
        if multimodal:
            fields.extend(
                [
                    ("transcript", transcript_text, float(watch_weights.get("transcript", 1.3))),
                    ("ocr_text", ocr, float(watch_weights.get("ocr_text", 1.1))),
                    ("visual_tags", " ".join(visuals), float(watch_weights.get("visual_tags", 0.8))),
                ]
            )

        tokens: Optional[set] = None
        if self._jieba is not None:
            try:
                tokens = set(self._jieba.lcut(title or ""))
            except Exception:
                tokens = None

        best: Dict[str, dict] = {}
        for cat in self.ordered:
            total = 0.0
            matched: List[str] = []
            source_matches: Dict[str, List[str]] = {}
            for kw, base_weight in self.meta[cat]["keywords"].items():
                best_field = 0.0
                hit_sources: List[str] = []
                for source, text, fw in fields:
                    if text and self._hit(kw, text, tokens):
                        best_field = max(best_field, fw)
                        hit_sources.append(source)
                if best_field > 0:
                    total += base_weight * best_field
                    matched.append(kw)
                    if multimodal:
                        for source in hit_sources:
                            source_matches.setdefault(source, []).append(kw)
            if total > 0:
                best[cat] = {"score": round(total, 2), "matched": matched, "sources": source_matches}

        min_score = float(settings.get("classify", "min_score", default=3))

        def finish(result: dict, selected: Optional[dict] = None) -> dict:
            if multimodal:
                result["evidence"] = {
                    "source_hits": (selected or {}).get("sources", {}),
                    "input_chars": {
                        "title": len(title or ""),
                        "description": len(description or ""),
                        "transcript": len(transcript_text),
                        "ocr_text": len(ocr),
                    },
                    "visual_tags": visuals,
                }
                result["degradations"] = [
                    label
                    for missing, label in [
                        (not transcript_text, "未提供语音转写"),
                        (not ocr, "未识别到画面文字"),
                        (not visuals, "未提供图像标签"),
                    ]
                    if missing
                ]
            return result

        if not best:
            return finish(
                {
                    "category": self.fallback["name"],
                    "emoji": self.fallback["emoji"],
                    "score": 0.0,
                    "confidence": 0.0,
                    "matched": [],
                    "ranking": [],
                }
            )

        # 同分时按 yaml 顺序优先
        ranked = sorted(
            best.items(),
            key=lambda kv: (-kv[1]["score"], self.ordered.index(kv[0])),
        )
        top_name, top = ranked[0]
        if top["score"] < min_score:
            return finish(
                {
                    "category": self.fallback["name"],
                    "emoji": self.fallback["emoji"],
                    "score": top["score"],
                    "confidence": round(min(1.0, top["score"] / (min_score * 2)), 2),
                    "matched": top["matched"],
                    "ranking": [
                        {"category": k, "score": v["score"]} for k, v in ranked[:5]
                    ],
                },
                top,
            )

        second_score = ranked[1][1]["score"] if len(ranked) > 1 else 0.0
        confidence = top["score"] / (top["score"] + second_score) if second_score else 1.0
        # 分数越高越可信，避免单薄命中被判成 100%
        confidence = round(min(0.99, confidence * min(1.0, 0.55 + top["score"] / 20)), 2)

        return finish(
            {
                "category": top_name,
                "emoji": self.meta[top_name]["emoji"],
                "score": top["score"],
                "confidence": confidence,
                "matched": top["matched"][:10],
                "ranking": [{"category": k, "score": v["score"]} for k, v in ranked[:5]],
            },
            top,
        )

    @property
    def all_categories(self) -> List[str]:
        return self.ordered


classifier = Classifier()
