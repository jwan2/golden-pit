"""把取数、指标、判定、护城河观点串成一条完整的单股分析。"""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
from typing import Callable

from . import metrics, moat
from .plan import entry_plan
from .timing import analyze_timing, chart_series
from .config import Thresholds
from .i18n import M, msg_of
from .models import RawStock
from .providers.yahoo import FetchError, fetch_raw
from .scoring import decide


def analyze_raw(raw: RawStock, th: Thresholds) -> dict:
    facts = metrics.build_facts(raw)
    checks = metrics.build_checks(raw, th)
    verdict = decide(checks, facts, th)
    return {
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
        "fetched_at": raw.fetched_at,
        "fundamentals_fetched_at": raw.fundamentals_fetched_at,
        "error": None,
    }


def error_result(ticker: str, message) -> dict:
    return {
        "ticker": ticker, "name": ticker, "sector": "", "industry": "", "currency": "USD",
        "price": None, "market_cap": None, "drop_from_high_pct": None, "checks": [],
        "verdict": {"tier": "error", "label": M("tier.error"), "reasons": [message], "sort_key": [99, 0, 0],
                    "quality_ok": False, "pit_ok": False, "fin_pass": 0, "fin_total": 5, "mgmt_pass": 0,
                    "mgmt_total": 3, "anchors_pass": 0, "unknown_count": 0},
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
