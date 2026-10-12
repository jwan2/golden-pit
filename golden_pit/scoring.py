"""判定逻辑：Check 列表 → 一个可解释的结论。

不再有"总分 100"。原因：旧版把质量 80 分 + 估值 20 分加在一起，
导致一只完全没跌的好公司也能拿到"强烈买入 · 核心黄金坑"。

现在分成两个独立问题：
  质量门槛  公司本身够不够健康？（财务 ≥4/5 且 管理层 ≥2/3）
  坑位条件  价格是不是真的在坑里？（回撤 ≥30% 且 P/B、Forward P/E 至少一项合格）
两者都满足才是"黄金坑"。
"""
from __future__ import annotations

from .config import Thresholds
from .models import FAIL, NA, PASS, UNKNOWN, Check

# 顺序即默认排序
TIERS = {
    "golden_pit":   {"label": "黄金坑",        "desc": "质量过关，且价格确实在坑里"},
    "quality_wait": {"label": "优质 · 待价",    "desc": "公司质量过关，但价格还没到坑位"},
    "value_trap":   {"label": "坑里 · 质量不足", "desc": "价格够便宜，但财务或管理层没过关（价值陷阱风险）"},
    "fail":         {"label": "未通过",        "desc": "质量和坑位都不满足"},
    "insufficient": {"label": "数据不足",       "desc": "关键数据缺失过多，不下结论"},
    "n_a":          {"label": "不适用",        "desc": "金融业等不适用本框架的公司"},
    "error":        {"label": "抓取失败",       "desc": "数据源没有返回有效数据"},
}
TIER_ORDER = list(TIERS)


def _by_group(checks: list[Check], group: str) -> list[Check]:
    return [c for c in checks if c.group == group]


def _count(checks: list[Check], status: str) -> int:
    return sum(1 for c in checks if c.status == status)


def _problem_lines(checks: list[Check]) -> list[str]:
    lines = []
    for c in checks:
        if c.status == FAIL:
            lines.append(f"✗ {c.label} {c.display}（要求 {c.rule}）" + (f" — {c.note}" if c.note else ""))
        elif c.status == UNKNOWN:
            lines.append(f"? {c.label} 无法判断 — {c.note}")
    return lines


def decide(checks: list[Check], facts: dict, th: Thresholds) -> dict:
    fin, mgmt, val = (_by_group(checks, g) for g in ("financial", "management", "valuation"))
    by_key = {c.key: c for c in checks}

    fin_total = len([c for c in fin if c.status != NA])
    fin_pass, mgmt_pass = _count(fin, PASS), _count(mgmt, PASS)
    unknown = _count(fin, UNKNOWN) + _count(mgmt, UNKNOWN)

    drop, pb, fpe = by_key["drop"], by_key["pb"], by_key["forward_pe"]
    anchors = int(pb.status == PASS) + int(fpe.status == PASS)
    pit_ok = drop.status == PASS and anchors >= th.pit_min_anchors
    quality_ok = fin_pass >= th.financial_min_pass and mgmt_pass >= th.management_min_pass

    if facts.get("is_financial"):
        tier = "n_a"
    elif unknown > th.max_unknown_checks or drop.status == UNKNOWN:
        tier = "insufficient"
    elif quality_ok and pit_ok:
        tier = "golden_pit"
    elif quality_ok:
        tier = "quality_wait"
    elif pit_ok:
        tier = "value_trap"
    else:
        tier = "fail"

    reasons: list[str] = []
    if tier == "n_a":
        reasons.append("金融业公司的利息保障、流动比率、净负债等指标没有可比意义，不纳入评判。")
    elif tier == "insufficient":
        reasons.append(f"财务+管理层 8 项中有 {unknown} 项数据缺失（上限 {th.max_unknown_checks}），不下结论。")
        reasons += _problem_lines(fin + mgmt)
        if drop.status == UNKNOWN:
            reasons.append("? 缺少价格或 52 周高点，无法判断是否在坑里")
    else:
        reasons.append(
            f"质量：财务 {fin_pass}/{fin_total}（要求 ≥{th.financial_min_pass}），"
            f"管理层 {mgmt_pass}/3（要求 ≥{th.management_min_pass}） → {'过关' if quality_ok else '未过关'}")
        reasons.append(
            f"坑位：回撤 {drop.display}（要求 ≥{th.drop_from_high_min_pct:.0f}%），"
            f"估值锚点 {anchors}/2 合格（要求 ≥{th.pit_min_anchors}） → {'在坑里' if pit_ok else '不在坑里'}")
        reasons += _problem_lines(fin + mgmt + [drop, pb, fpe] if not pit_ok else fin + mgmt)

    drop_val = drop.value if drop.value is not None else -1.0
    return {
        "tier": tier,
        "label": TIERS[tier]["label"],
        "quality_ok": quality_ok,
        "pit_ok": pit_ok,
        "fin_pass": fin_pass, "fin_total": fin_total,
        "mgmt_pass": mgmt_pass, "mgmt_total": 3,
        "anchors_pass": anchors,
        "unknown_count": unknown,
        "reasons": reasons,
        # 同一档内的排序：通过项越多越靠前，其次回撤越深越靠前
        "sort_key": [TIER_ORDER.index(tier), -(fin_pass + mgmt_pass), -round(drop_val, 2)],
    }
