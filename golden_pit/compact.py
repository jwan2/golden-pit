"""把完整结果压缩成给命令行 / AI 客户端看的精简结构（去掉图表数据等大字段）。"""
from __future__ import annotations


def compact(r: dict, detail: bool = True) -> dict:
    v = r["verdict"]
    out = {
        "ticker": r["ticker"], "name": r.get("name"), "industry": r.get("industry"),
        "verdict": v["label"], "tier": v["tier"],
        "price": r.get("price"), "currency": r.get("currency"),
        "drop_from_52w_high_pct": None if r.get("drop_from_high_pct") is None else round(r["drop_from_high_pct"], 1),
        "financial_pass": f"{v['fin_pass']}/{v['fin_total']}", "management_pass": f"{v['mgmt_pass']}/3",
        "unknown_checks": v["unknown_count"],
        "timing": (r.get("timing") or {}).get("label"),
    }
    if not detail:
        return out
    out.update({
        "reasons": v["reasons"],
        "timing_reason": (r.get("timing") or {}).get("reason"),
        "checks": [{k: c[k] for k in ("label", "status", "display", "rule", "note")} for c in r.get("checks", [])],
        "entry_plan": [{k: lv[k] for k in ("drop_pct", "price", "weight_pct", "reached")}
                       for lv in (r.get("plan") or {}).get("levels", [])],
        "warnings": r.get("warnings", []),
        "moat_view": (r.get("moat") or {}).get("summary"),
        "data_fetched_at": r.get("fetched_at"),
        "error": r.get("error"),
    })
    return out
