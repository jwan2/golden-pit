"""走势时机：价格是否已经在坑底企稳？

质量和坑位回答"值不值得买"，这里回答"现在是不是还在往下掉"。
它**不改变**黄金坑判定，只作为入场时机的参考。规则刻意简单、可解释：

  下跌中   收盘价 < MA20，MA20 仍在下行，且最近 10 个交易日内刚创一年新低
  筑底中   价格离一年低点不超过 12%，且（低点已在 10 个交易日之前，或 MA20 已走平：5 日变化在 ±1.5% 内）
  企稳反弹 收盘价 > MA20，MA20 拐头向上，且低点已在 10 个交易日之前（没有再创新低）
  方向不明 以上都不满足
"""
from __future__ import annotations

from typing import Optional

STATES = {
    "falling": "下跌中",
    "basing": "筑底中",
    "rebounding": "企稳反弹",
    "unclear": "方向不明",
    "unknown": "数据不足",
}
MIN_DAYS = 60
RECENT_LOW_DAYS = 10
BASE_BAND_PCT = 12.0
FLAT_SLOPE_PCT = 1.5


def _ma(xs: list[float], n: int, end: Optional[int] = None) -> Optional[float]:
    end = len(xs) if end is None else end
    if end < n:
        return None
    window = xs[end - n:end]
    return sum(window) / n


def rsi(xs: list[float], n: int = 14) -> Optional[float]:
    if len(xs) <= n:
        return None
    gains = losses = 0.0
    for a, b in zip(xs[-n - 1:-1], xs[-n:]):
        d = b - a
        gains += max(d, 0.0)
        losses += max(-d, 0.0)
    if losses == 0:
        return 100.0
    rs = (gains / n) / (losses / n)
    return 100 - 100 / (1 + rs)


def analyze_timing(closes: list[tuple[str, float]]) -> dict:
    if len(closes) < MIN_DAYS:
        return {"state": "unknown", "label": STATES["unknown"],
                "reason": f"价格历史不足 {MIN_DAYS} 个交易日", "metrics": {}}
    xs = [c for _, c in closes]
    last = xs[-1]
    low = min(xs)
    low_idx = len(xs) - 1 - xs[::-1].index(low)       # 最近一次触及低点
    days_since_low = len(xs) - 1 - low_idx
    above_low = (last / low - 1) * 100
    ma20, ma20_prev = _ma(xs, 20), _ma(xs, 20, len(xs) - 5)
    ma50 = _ma(xs, 50)
    slope = (ma20 / ma20_prev - 1) * 100 if ma20 and ma20_prev else 0.0
    r = rsi(xs)

    m = {"ma20": round(ma20, 2), "ma50": round(ma50, 2) if ma50 else None,
         "ma20_slope_5d_pct": round(slope, 2), "low_1y": round(low, 2), "low_date": closes[low_idx][0],
         "days_since_low": days_since_low, "above_low_pct": round(above_low, 1),
         "rsi14": round(r, 1) if r is not None else None}

    if last < ma20 and slope < 0 and days_since_low <= RECENT_LOW_DAYS:
        state = "falling"
        reason = f"{days_since_low} 个交易日前刚创一年新低，价格在 MA20 下方且均线仍在下行"
    elif last > ma20 and slope > 0 and days_since_low > RECENT_LOW_DAYS:
        state = "rebounding"
        reason = f"低点（{closes[low_idx][0]}）后 {days_since_low} 个交易日未再创新低，已站上拐头向上的 MA20，距低点 +{above_low:.1f}%"
    elif above_low <= BASE_BAND_PCT and (days_since_low > RECENT_LOW_DAYS or abs(slope) <= FLAT_SLOPE_PCT):
        state = "basing"
        reason = (f"价格贴近一年低点（+{above_low:.1f}%），MA20 5 日变化 {slope:+.1f}%，在底部横盘"
                  + (f"，且 {days_since_low} 个交易日未破底" if days_since_low > RECENT_LOW_DAYS else ""))
    else:
        state = "unclear"
        reason = f"距一年低点 +{above_low:.1f}%，MA20 5 日变化 {slope:+.1f}%，没有清晰的下跌或企稳特征"
    return {"state": state, "label": STATES[state], "reason": reason, "metrics": m}


def chart_series(closes: list[tuple[str, float]], max_points: int = 130) -> dict:
    """给前端画图用的降采样序列（保留首尾和最低点）。"""
    if not closes:
        return {"dates": [], "closes": []}
    step = max(1, len(closes) // max_points)
    idx = set(range(0, len(closes), step)) | {len(closes) - 1}
    low_i = min(range(len(closes)), key=lambda i: closes[i][1])
    idx.add(low_i)
    pts = [closes[i] for i in sorted(idx)]
    return {"dates": [d for d, _ in pts], "closes": [round(c, 2) for _, c in pts]}
