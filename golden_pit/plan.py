"""买入计划参考：把"黄金坑"拆成分批价位。

这是**纪律性模板**，不是建议：
- 以 52 周高点为锚，在 -30% / -40% / -50% 三个回撤位设置分批价位
- 价格已经到达的价位标记为 reached
- 仓位金额的换算在前端完成（用户自己输入总资金和单只上限）
- 质量一旦不过关就停止加仓：这条写进"失效条件"
"""
from __future__ import annotations

from typing import Optional

from .config import Thresholds

DEFAULT_TRANCHES = ((0.0, 40), (10.0, 30), (20.0, 30))   # (相对入场线再多跌几个点, 仓位占比%)


def entry_plan(price: Optional[float], high: Optional[float], th: Thresholds) -> Optional[dict]:
    if not price or not high or high <= 0:
        return None
    base = th.drop_from_high_min_pct
    levels = []
    for extra, weight in DEFAULT_TRANCHES:
        drop = base + extra
        level = round(high * (1 - drop / 100), 2)
        levels.append({"drop_pct": drop, "price": level, "weight_pct": weight, "reached": price <= level,
                       "away_pct": round((price / level - 1) * 100, 1) if price > level else 0.0})
    return {
        "anchor_high": round(high, 2),
        "levels": levels,
        "invalidation": [
            "下一次扫描质量门槛不再过关（财务或管理层项目转为未通过）→ 暂停加仓，重新评估",
            "出现无法解释的新增“未知”项或数据警告 → 先核实数据再行动",
            "坑的成因从周期性/情绪性变为结构性衰退 → 这不再是黄金坑",
        ],
    }
