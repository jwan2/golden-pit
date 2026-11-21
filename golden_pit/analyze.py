"""把取数、指标、判定、护城河观点串成一条完整的单股分析。"""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
from typing import Callable

from . import metrics, moat, rank
from .plan import entry_plan
from .timing import analyze_timing, chart_series
from .config import Thresholds
from .i18n import M, msg_of
from .models import RawStock
from .providers.yahoo import FetchError, fetch_raw
from .scoring import decide


# 现价与最近一个收盘价相差超过这个倍数，就认为两者不是同一口径（典型是拆股当天：
# Yahoo 的日线已经复权，现价、市值、52 周高点却还是新旧口径混用）
PRICE_MISMATCH_X = 1.8


def analyze_raw(raw: RawStock, th: Thresholds) -> dict:
    facts = metrics.build_facts(raw)
    last_close = raw.closes[-1][1] if raw.closes else None
    if facts.get("price") and last_close and not 1 / PRICE_MISMATCH_X <= facts["price"] / last_close <= PRICE_MISMATCH_X:
        # 拿混合口径的数据下结论会出大错（2026-11-20 SNDK：P/B 47.97、回撤 10.8%，被误判为掉出黄金坑）
        return error_result(raw.ticker, M("err.price_mismatch", t=raw.ticker, p=facts["price"], c=last_close,
                                          x=f"{max(facts['price'], last_close) / min(facts['price'], last_close):.1f}"))
    checks = metrics.build_checks(raw, th)
    verdict = decide(checks, facts, th)
    return rank.apply({
        "ticker": raw.ticker,
        **facts,
        "drop_from_high_pct": metrics.drop_from_high(raw),
        "checks": [asdict(c) for c in checks],
        "verdict": verdict,
        "moat": moat.lookup(raw.ticker),
        "plan": entry_plan(facts["price"], facts["high_52w"], th),
        "timing": analyze_timing(raw.closes),
        "chart": chart_series(raw.closes),
        "warnings": metrics.build_warnings(raw, facts),
        "splits": [list(s) for s in raw.splits],       # 战绩追踪按它给拆股前记下的价格复权
        "fetched_at": raw.fetched_at,
        "fundamentals_fetched_at": raw.fundamentals_fetched_at,
        "error": None,
    })


def error_result(ticker: str, message) -> dict:
    return {
        "ticker": ticker, "name": ticker, "sector": "", "industry": "", "currency": "USD",
        "price": None, "market_cap": None, "drop_from_high_pct": None, "checks": [],
        "verdict": {"tier": "error", "label": M("tier.error"), "reasons": [message], "sort_key": [99, 0, 0, 0],
                    "quality_ok": False, "pit_ok": False, "fin_pass": 0, "fin_total": 5, "mgmt_pass": 0,
                    "mgmt_total": 3, "anchors_pass": 0, "unknown_count": 0, "rank": None},
        "moat": moat.lookup(ticker), "plan": None, "warnings": [],
        "timing": {"state": "unknown", "label": M("tm.unknown"), "reason": "", "metrics": {}},
        "chart": {"dates": [], "closes": []}, "fundamentals_fetched_at": "",
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "error": message,
    }


def analyze_ticker(ticker: str, th: Thresholds, fetch: Callable[[str], RawStock] = fetch_raw) -> dict:
    ticker = ticker.upper().strip()
    try:
        return analyze_raw(fetch(ticker), th)
    except FetchError as e:
        return error_result(ticker, msg_of(e))
    except Exception as e:  # noqa: BLE001 — 单只股票的 bug 不能拖垮整次扫描，但必须如实报错
        return error_result(ticker, M("err.analyze", t=ticker, e=f"{type(e).__name__}: {e}"))
