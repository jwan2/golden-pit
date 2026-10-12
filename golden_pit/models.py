"""核心数据结构。"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Optional

# 指标状态：
#   pass    通过
#   fail    未通过
#   unknown 数据缺失/不可靠，无法判断（永远不得分、也不免检）
#   na      该指标对此类公司不适用
PASS, FAIL, UNKNOWN, NA = "pass", "fail", "unknown", "na"


def num(x: Any) -> Optional[float]:
    """把任意值转成有限浮点数；NaN / inf / 非数字 → None。"""
    if x is None or isinstance(x, bool):
        return None
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


@dataclass
class Statement:
    """一张财报：dates 从新到旧；rows[label][i] 对应 dates[i]。"""
    dates: list[str] = field(default_factory=list)
    rows: dict[str, list[Optional[float]]] = field(default_factory=dict)

    def get(self, *labels: str, idx: int = 0) -> Optional[float]:
        """按候选字段名依次取第 idx 期的值，返回第一个有效值。"""
        for label in labels:
            series = self.rows.get(label)
            if series and idx < len(series):
                v = num(series[idx])
                if v is not None:
                    return v
        return None

    def series(self, *labels: str) -> list[tuple[str, float]]:
        """返回 [(date, value)]（从新到旧，仅有效值），取第一个有数据的字段。"""
        for label in labels:
            s = self.rows.get(label)
            if not s:
                continue
            out = [(d, num(v)) for d, v in zip(self.dates, s)]
            out = [(d, v) for d, v in out if v is not None]
            if out:
                return out
        return []

    @property
    def empty(self) -> bool:
        return not self.dates


@dataclass
class RawStock:
    """从数据源取回的原始数据（与评分逻辑完全解耦，便于离线测试）。"""
    ticker: str
    info: dict
    balance: Statement
    income: Statement
    cashflow: Statement
    splits: list[tuple[str, float]]       # [(date, ratio)]
    fetched_at: str                       # 行情（info / 价格走势）抓取时间
    closes: list[tuple[str, float]] = field(default_factory=list)   # 近一年日收盘价，旧→新
    fundamentals_fetched_at: str = ""     # 财报抓取时间（可能来自缓存，最多 7 天）


@dataclass
class Check:
    key: str
    label: str
    group: str                   # financial | management | valuation
    status: str                  # PASS / FAIL / UNKNOWN / NA
    value: Optional[float]
    display: str                 # 给人看的值，如 "3.4x"、"−6.5%"
    rule: str                    # 给人看的规则，如 "≥ 2.0x"
    note: str = ""
