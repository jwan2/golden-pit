"""黄金坑之间的先后：A 优先看 / B 其次 / C 再看。

判定（是不是黄金坑）仍然只由质量门槛 × 坑位条件决定，这里**不改变**判定，只回答
"同样是黄金坑，先看哪只"。做法刻意不用加权总分，而是看 5 个"余量"条件各满足了没有：

  财务满分     财务 5 项全部通过（门槛只要求 4 项）
  管理层满分   管理层 3 项全部通过（门槛只要求 2 项）
  估值双锚     P/B 和前瞻 PE 都合格（门槛只要求 1 项）
  数据完整     没有"未知"项
  不在下跌中   走势不是"下跌中"，且走势数据足够

差 0–1 项 → A；差 2 项 → B；差 3 项及以上 → C。每只股票都列出差在哪，用户可以自己核对。
分档会随入选一起记进战绩，时间够长后可以检验 A 是否真的跑赢 C。
"""
from __future__ import annotations

from typing import Optional

from .i18n import M

GRADES = ("a", "b", "c")
CONDITIONS = ("fin_full", "mgmt_full", "two_anchors", "no_unknown", "not_falling")


def _grade(gaps: int) -> str:
    return "a" if gaps <= 1 else "b" if gaps == 2 else "c"


def rank(verdict: dict, timing: Optional[dict]) -> Optional[dict]:
    """只给黄金坑分档；其他档位返回 None。"""
    if verdict.get("tier") != "golden_pit":
        return None
    v = verdict
    state = (timing or {}).get("state", "unknown")
    ok = {
        "fin_full": v["fin_pass"] >= v["fin_total"],
        "mgmt_full": v["mgmt_pass"] >= v.get("mgmt_total", 3),
        "two_anchors": v["anchors_pass"] >= 2,
        "no_unknown": v["unknown_count"] == 0,
        "not_falling": state not in ("falling", "unknown"),
    }
    gap_msg = {
        "fin_full": M("rk.gap.fin", p=v["fin_pass"], t=v["fin_total"]),
        "mgmt_full": M("rk.gap.mgmt", p=v["mgmt_pass"], t=v.get("mgmt_total", 3)),
        "two_anchors": M("rk.gap.anchor"),
        "no_unknown": M("rk.gap.data", n=v["unknown_count"]),
        "not_falling": M("rk.gap.timing_unknown" if state == "unknown" else "rk.gap.falling"),
    }
    gaps = [k for k in CONDITIONS if not ok[k]]
    grade = _grade(len(gaps))
    return {
        "grade": grade,
        "label": M(f"rk.{grade}"),
        "gaps": len(gaps),
        "conditions": [{"key": k, "ok": ok[k], "label": M(f"rk.cond.{k}"), "gap": None if ok[k] else gap_msg[k]}
                       for k in CONDITIONS],
        "gap_labels": [gap_msg[k] for k in gaps],
    }


def apply(result: dict) -> dict:
    """在分析结果上补 verdict.rank，并让排序先按档位内的差距数排。"""
    v = result["verdict"]
    v["rank"] = rank(v, result.get("timing"))
    key = list(v.get("sort_key") or [99, 0, 0])
    v["sort_key"] = [key[0], v["rank"]["gaps"] if v["rank"] else 0, *key[1:]]
    return result


def ensure(scan: Optional[dict]) -> Optional[dict]:
    """分档上线前保存的扫描（latest.json / 历史）没有 rank：读取时按当时的数据补上，不写回文件。"""
    for r in (scan or {}).get("results", []):
        v = r.get("verdict") or {}
        if "rank" not in v:
            try:
                apply(r)
            except KeyError:        # 字段不全的老结果：不分档
                v["rank"] = None
    return scan
