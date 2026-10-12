"""定性护城河知识库（第一维"终极颠覆"、第二维"产能复制"）。

这是人写的行业观点，只对知识库里明确列出的代码生效，并且：
- 不再用关键词去匹配行业（旧版会把 T-Mobile 当成"标准必要专利"公司）
- 不计入量化判定，仅作为"观点"展示，由使用者自行判断
- 中英双语（字段名带 _en 的为英文）
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from .i18n import M, T

KB_PATH = Path(__file__).parent / "data" / "moat_kb.json"


@lru_cache(maxsize=1)
def _load() -> tuple[list[dict], dict[str, dict]]:
    industries = json.loads(KB_PATH.read_text(encoding="utf-8"))
    index = {t: ind for ind in industries for t in ind["tickers"]}
    return industries, index


def lookup(ticker: str) -> dict | None:
    ind = _load()[1].get(ticker.upper())
    if not ind:
        return None
    bi = lambda k: T(ind[k], ind.get(f"{k}_en") or ind[k])  # noqa: E731
    return {
        "industry": bi("industry"),
        "summary": bi("moat_reason"),
        "disruption_risk": bi("disruption_risk"),
        "capacity_barrier": bi("capacity_barrier"),
        "disruption_view": bi("disruption_view"),
        "capacity_view": bi("capacity_view"),
        "source": M("moat.source"),
    }


def curated_tickers() -> list[str]:
    return list(_load()[1])
