"""扫描编排：并发抓取、进度、与上一次扫描的差异、持久化。"""
from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from concurrent.futures import TimeoutError as FuturesTimeout  # 3.10 中与内置 TimeoutError 不同
from datetime import datetime, timezone
from typing import Callable, Optional

from . import SCHEMA_VERSION, universe
from .analyze import analyze_ticker, error_result
from .config import ScreenerConfig, Thresholds
from .i18n import M, MsgError, msg_of
from .models import RawStock
from .providers.yahoo import fetch_raw
from .scoring import TIER_ORDER
from .store import Store
from .track import Tracker

SCAN_DEADLINE_SECONDS = 15 * 60
RETRY_COOLDOWN_SECONDS = 20     # 补扫前先冷却，让 Yahoo 的限流窗口过去
RETRY_MAX = 200


def _is_retryable(r: dict) -> bool:
    """只补扫"抓取失败 / 超时"这类暂时性错误；代码无效（没有行情）不重试。"""
    err = r.get("error")
    return isinstance(err, dict) and err.get("$m") in ("err.fetch", "err.timeout")


def deadline_for(n: int) -> int:
    """扫描总时限：至少 15 分钟；大股票池（如 S&P 500 首次抓取）按每只 2.5 秒放宽。"""
    return max(SCAN_DEADLINE_SECONDS, int(n * 2.5))


def summarize(results: list[dict]) -> dict:
    counts = {t: 0 for t in TIER_ORDER}
    for r in results:
        counts[r["verdict"]["tier"]] += 1
    return counts


def diff_scans(prev: Optional[dict], cur: dict) -> dict:
    """与上一次扫描比较：谁新晋黄金坑、谁掉出、谁换了档位。"""
    if not prev:
        return {"has_previous": False}
    old = {r["ticker"]: r["verdict"]["tier"] for r in prev.get("results", [])}
    new = {r["ticker"]: r["verdict"]["tier"] for r in cur["results"]}
    entered = sorted(t for t, tier in new.items() if tier == "golden_pit" and old.get(t) != "golden_pit")
    left = sorted(t for t, tier in old.items() if tier == "golden_pit" and new.get(t) != "golden_pit")
    moved = sorted(t for t in new if t in old and new[t] != old[t])
    return {
        "has_previous": True,
        "previous_finished_at": prev.get("finished_at"),
        "entered_golden_pit": entered,
        "left_golden_pit": [{"ticker": t, "now": new.get(t, "removed")} for t in left],
        "tier_changes": [{"ticker": t, "from": old[t], "to": new[t]} for t in moved],
        "new_tickers": sorted(set(new) - set(old)),
        "removed_tickers": sorted(set(old) - set(new)),
    }


class Scanner:
    def __init__(self, store: Store, thresholds: Thresholds, screener: ScreenerConfig, workers: int = 4,
                 fetch: Callable[[str], RawStock] = fetch_raw,
                 on_complete: Optional[Callable[[dict], None]] = None,
                 price: Optional[Callable[[str], Optional[float]]] = None,
                 tracker: Optional["Tracker"] = None):
        self.store, self.th, self.screener, self.workers, self.fetch = store, thresholds, screener, workers, fetch
        self.on_complete, self.price, self.tracker = on_complete, price, tracker
        self.retry_cooldown = RETRY_COOLDOWN_SECONDS
        self._lock = threading.Lock()
        self.state = self._idle_state()

    @staticmethod
    def _idle_state() -> dict:
        return {"running": False, "stage": "idle", "message": "", "done": 0, "total": 0, "current": "",
                "started_at": None, "error": None}

    def status(self) -> dict:
        with self._lock:
            s = dict(self.state)
        if s["running"] and s["started_at"]:
            s["elapsed"] = round(time.time() - s["started_at"])
        return s

    def _set(self, **kw) -> None:
        with self._lock:
            self.state.update(kw)

    def start(self, mode: str = "live", tickers: Optional[list[str]] = None, trigger: str = "manual") -> bool:
        """后台启动一次扫描。已有扫描在跑时返回 False。"""
        with self._lock:
            if self.state["running"]:
                return False
            self.state = {**self._idle_state(), "running": True, "stage": "universe",
                          "message": M("s.universe"), "started_at": time.time()}
        threading.Thread(target=self._run, args=(mode, tickers or [], trigger), daemon=True).start()
        return True

    def _run(self, mode: str, custom: list[str], trigger: str) -> None:
        try:
            self.run_sync(mode, custom, trigger)
        except Exception as e:  # noqa: BLE001
            self._set(running=False, stage="error", message=M("s.failed"), error=msg_of(e))

    def run_sync(self, mode: str, custom: list[str], trigger: str) -> dict:
        started = datetime.now(timezone.utc)
        if mode == "custom":
            tickers, note = universe.parse_tickers(custom), M("u.custom")
            if not tickers:
                raise MsgError(M("u.no_tickers"))
        elif mode == "curated":
            tickers, note = universe.curated()
        elif mode == "sp500":
            tickers, note = universe.sp500(self.store.dir)
        else:
            tickers, note = universe.live_screen(self.screener)
        self._set(stage="analyzing", total=len(tickers), done=0, message=M("s.analyzing", n=len(tickers)))

        results: dict[str, dict] = {}
        limit = deadline_for(len(tickers))
        pool = ThreadPoolExecutor(max_workers=self.workers)
        try:
            futures = {pool.submit(analyze_ticker, t, self.th, self.fetch): t for t in tickers}
            try:
                for fut in as_completed(futures, timeout=limit):
                    t = futures[fut]
                    results[t] = fut.result()
                    self._set(done=len(results), current=t)
            except FuturesTimeout:
                pass
        finally:
            pool.shutdown(wait=False, cancel_futures=True)
        for t in tickers:  # 超时未完成的如实标记，不丢弃
            results.setdefault(t, error_result(t, M("err.timeout", t=t, m=limit // 60)))

        # 补扫：主扫描里因限流/网络失败的，冷却后单线程再试一次（成功的数据已缓存，不会重复抓）
        retry = [t for t in tickers if _is_retryable(results[t])][:RETRY_MAX]
        if retry:
            self._set(stage="retrying", done=0, total=len(retry), message=M("s.retrying", n=len(retry)))
            time.sleep(self.retry_cooldown)
            for i, t in enumerate(retry, 1):
                r = analyze_ticker(t, self.th, self.fetch)
                if not r.get("error"):
                    results[t] = r
                self._set(done=i, current=t)

        ordered = sorted(results.values(), key=lambda r: r["verdict"]["sort_key"] + [r["ticker"]])
        finished = datetime.now(timezone.utc)
        scan = {
            "schema_version": SCHEMA_VERSION,
            "scan_id": finished.strftime("%Y%m%d_%H%M%S"),
            "started_at": started.isoformat(timespec="seconds"),
            "finished_at": finished.isoformat(timespec="seconds"),
            "trigger": trigger,
            "universe": {"mode": mode, "note": note, "count": len(tickers)},
            "thresholds": self.th.to_dict(),
            "summary": summarize(ordered),
            "results": ordered,
        }
        scan["changes"] = diff_scans(self.store.latest(), scan)
        self.store.save_scan(scan)
        if self.tracker and self.price:
            self._set(stage="tracking", message=M("s.tracking"))
            try:
                self.tracker.update(scan, self.price)
            except Exception as e:  # noqa: BLE001 — 追踪失败不影响扫描结果，但要留痕
                scan.setdefault("notes", []).append(M("s.track_failed", e=str(e)))
        if self.on_complete:
            try:
                self.on_complete(scan)
            except Exception:  # noqa: BLE001 — 后处理（如自动 AI 研判）失败不能影响扫描结果
                pass
        failed, golden = scan["summary"]["error"], scan["summary"]["golden_pit"]
        msg = M("s.done_failed", n=len(tickers), g=golden, f=failed) if failed else M("s.done", n=len(tickers), g=golden)
        self._set(running=False, stage="done", done=len(tickers), current="", message=msg)
        return scan
