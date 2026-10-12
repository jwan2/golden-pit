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
from .i18n import M
from .models import FAIL, NA, PASS, UNKNOWN, Check

# 顺序即默认排序。label/desc 是 i18n 消息
TIER_ORDER = ["golden_pit", "quality_wait", "value_trap", "fail", "insufficient", "n_a", "error"]
TIERS = {t: {"label": M(f"tier.{t}"), "desc": M(f"tier.{t}.desc")} for t in TIER_ORDER}


def _by_group(checks: list[Check], group: str) -> list[Check]:
    return [c for c in checks if c.group == group]


def _count(checks: list[Check], status: str) -> int:
    return sum(1 for c in checks if c.status == status)


def _problem_lines(checks: list[Check]) -> list[dict]:
    lines = []
    for c in checks:
        if c.status == FAIL:
            if c.note:
                lines.append(M("r.fail_note", label=c.label, display=c.display, rule=c.rule, note=c.note))
            else:
                lines.append(M("r.fail", label=c.label, display=c.display, rule=c.rule))
        elif c.status == UNKNOWN:
            lines.append(M("r.unknown", label=c.label, note=c.note))
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

    reasons: list[dict] = []
    if tier == "n_a":
        reasons.append(M("r.na_financial"))
    elif tier == "insufficient":
        reasons.append(M("r.insufficient", n=unknown, max=th.max_unknown_checks))
        reasons += _problem_lines(fin + mgmt)
        if drop.status == UNKNOWN:
            reasons.append(M("r.no_drop"))
    else:
        reasons.append(M("r.quality", fp=fin_pass, ft=fin_total, fmin=th.financial_min_pass, mp=mgmt_pass,
                         mmin=th.management_min_pass, res=M("r.quality_ok" if quality_ok else "r.quality_no")))
        reasons.append(M("r.pit", drop=drop.display, min=f"{th.drop_from_high_min_pct:.0f}", a=anchors,
                         amin=th.pit_min_anchors, res=M("r.pit_in" if pit_ok else "r.pit_out")))
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
