"""战绩追踪（前向验证，不是回测）。

为什么不做回测：免费数据源拿不到"历史时点"的财报（只有最近约 4 年年报，且无法确认当时是否已公布），
用今天的财报去回测过去，会产生严重的前视偏差，结果好看但不可信。

所以改成前向追踪：每只股票第一次被判为"黄金坑"时，记下当时价格和基准（SPY）价格；
之后每次扫描更新现价。时间越长、样本越多，结论越可信。

两个口径：
  入选至今   从入选那天持有到现在（不管后来是否掉出黄金坑）
  入选期间   从入选到它掉出黄金坑那天（仍在坑里的算到现在）
都是**价格收益**，不含股息；超额收益 = 个股收益 − 同期 SPY 收益。
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from typing import Callable, Optional

from .i18n import M

BENCHMARK = "SPY"
MIN_DAYS_FOR_STATS = 7   # 追踪不足 7 天的记录只展示、不计入统计（当天收益恒为 0，会污染胜率）


def _ret(a: Optional[float], b: Optional[float]) -> Optional[float]:
    return round((b / a - 1) * 100, 2) if a and b else None


class Tracker:
    def __init__(self, data_dir: Path):
        self.path = Path(data_dir) / "track.json"
        self._lock = threading.Lock()

    def load(self) -> dict:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"benchmark": BENCHMARK, "entries": []}

    def _save(self, data: dict) -> None:
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            os.replace(tmp, self.path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    def update(self, scan: dict, price: Callable[[str], Optional[float]]) -> dict:
        """扫描完成后调用。price(ticker) 返回现价或 None。"""
        with self._lock:
            data = self.load()
            now = scan.get("finished_at") or datetime.now(timezone.utc).isoformat(timespec="seconds")
            bench = price(BENCHMARK)
            by_ticker = {r["ticker"]: r for r in scan["results"]}
            open_by_ticker = {e["ticker"]: e for e in data["entries"] if e["status"] == "open"}

            # 1. 已追踪的：更新现价；本次扫描里明确不再是黄金坑的 → 记为离场
            for e in data["entries"]:
                r = by_ticker.get(e["ticker"])
                p = r["price"] if r and r.get("price") else price(e["ticker"])
                if p:
                    e["last_price"], e["last_at"] = p, now
                if bench:
                    e["bench_last"] = bench
                if e["status"] == "open" and r and r["verdict"]["tier"] not in ("golden_pit", "error", "insufficient"):
                # 抓取失败 / 数据不足 不算"掉出"：那只是这次看不清，不是结论变了
                    e.update(status="exited", exited_at=now, exit_price=r.get("price"), bench_exit=bench,
                             exit_tier=r["verdict"]["tier"])

            # 2. 新晋黄金坑 → 开新记录（同一只股票掉出后再次入选，会开第二条）
            for r in scan["results"]:
                if r["verdict"]["tier"] == "golden_pit" and r["ticker"] not in open_by_ticker and r.get("price"):
                    data["entries"].append({
                        "id": f"{r['ticker']}-{scan.get('scan_id', now)}", "ticker": r["ticker"], "name": r.get("name", ""),
                        "status": "open", "flagged_at": now, "flag_price": r["price"], "bench_flag": bench,
                        "last_price": r["price"], "last_at": now, "bench_last": bench,
                    })
            data["updated_at"] = now
            self._save(data)
            return data

    @staticmethod
    def report(data: dict) -> dict:
        rows = []
        for e in data.get("entries", []):
            since = _ret(e["flag_price"], e.get("last_price"))
            bench_since = _ret(e.get("bench_flag"), e.get("bench_last"))
            end_price = e.get("exit_price") if e["status"] == "exited" else e.get("last_price")
            end_bench = e.get("bench_exit") if e["status"] == "exited" else e.get("bench_last")
            during = _ret(e["flag_price"], end_price)
            bench_during = _ret(e.get("bench_flag"), end_bench)
            rows.append({**e,
                         "return_since_pct": since, "bench_since_pct": bench_since,
                         "excess_since_pct": None if since is None or bench_since is None else round(since - bench_since, 2),
                         "return_during_pct": during,
                         "excess_during_pct": None if during is None or bench_during is None else round(during - bench_during, 2),
                         "days": _days(e["flagged_at"], e.get("last_at"))})
        rows.sort(key=lambda r: r["flagged_at"], reverse=True)

        def stats(key: str) -> dict:
            vals = [r[key] for r in rows if r[key] is not None and (r["days"] or 0) >= MIN_DAYS_FOR_STATS]
            if not vals:
                return {"n": 0}
            return {"n": len(vals), "mean": round(sum(vals) / len(vals), 2), "median": round(median(vals), 2),
                    "win_rate": round(sum(v > 0 for v in vals) / len(vals) * 100, 1)}

        mature = [r for r in rows if r["days"] is not None and r["days"] >= 90]
        return {
            "benchmark": data.get("benchmark", BENCHMARK), "updated_at": data.get("updated_at"),
            "entries": rows,
            "summary": {
                "total": len(rows), "open": sum(r["status"] == "open" for r in rows),
                "return_since": stats("return_since_pct"), "excess_since": stats("excess_since_pct"),
                "excess_during": stats("excess_during_pct"),
                "mature_90d": len(mature),
                "min_days_for_stats": MIN_DAYS_FOR_STATS,
            },
            "caveat": M("track.caveat", days=MIN_DAYS_FOR_STATS),
        }


def _days(a: Optional[str], b: Optional[str]) -> Optional[int]:
    try:
        return (datetime.fromisoformat(b) - datetime.fromisoformat(a)).days
    except (TypeError, ValueError):
        return None
