"""候选池：核心池（知识库）、Yahoo 实时筛选器、用户自定义。"""
from __future__ import annotations

import re

from . import moat
from .config import ScreenerConfig
from .i18n import M, MsgError

TICKER_RE = re.compile(r"^[A-Z]{1,5}(-[A-Z])?$")


def parse_tickers(raw: list[str] | str, limit: int = 60) -> list[str]:
    items = re.split(r"[\s,;，；]+", raw) if isinstance(raw, str) else raw
    out: list[str] = []
    for t in items:
        t = str(t).upper().strip()
        if TICKER_RE.match(t) and t not in out:
            out.append(t)
    return out[:limit]


def curated() -> tuple[list[str], dict]:
    tickers = moat.curated_tickers()
    return tickers, M("u.curated", n=len(tickers))


def live_screen(cfg: ScreenerConfig) -> tuple[list[str], dict]:
    """Yahoo 实时筛选。失败时抛异常，由调用方如实告知用户（不再静默回退）。"""
    import yfinance as yf

    exchanges = yf.EquityQuery("is-in", ["exchange", "NYQ", "NMS", "NGM", "ASE"])
    base = [
        exchanges,
        yf.EquityQuery("gt", ["intradaymarketcap", cfg.min_market_cap_billions * 1e9]),
        yf.EquityQuery("lt", ["fiftytwowkpercentchange", cfg.max_52w_change_pct]),
    ]
    if cfg.require_positive_fcf:
        base.append(yf.EquityQuery("gt", ["leveredfreecashflow.lasttwelvemonths", 0]))

    deep = [
        exchanges,
        yf.EquityQuery("gt", ["intradaymarketcap", cfg.min_market_cap_billions * 0.8e9]),
        yf.EquityQuery("lt", ["fiftytwowkpercentchange", min(cfg.max_52w_change_pct - 10, -25)]),
        yf.EquityQuery("lt", ["pricebookratio.quarterly", cfg.max_pb]),
    ]
    if cfg.require_positive_fcf:
        deep.append(yf.EquityQuery("gt", ["leveredfreecashflow.lasttwelvemonths", 0]))

    found: list[str] = []
    errors: list[dict] = []
    for label, operands, sort_field, asc in (
        (M("u.q.base"), base, "intradaymarketcap", False),
        (M("u.q.deep"), deep, "fiftytwowkpercentchange", True),
    ):
        try:
            res = yf.screen(yf.EquityQuery("and", operands), sortField=sort_field, sortAsc=asc,
                            size=min(max(cfg.size, 10), 250))
            for q in (res or {}).get("quotes", []):
                sym = q.get("symbol", "")
                if TICKER_RE.match(sym) and sym not in found:
                    found.append(sym)
        except Exception as e:  # noqa: BLE001
            errors.append(M("u.query_failed", label=label, error=type(e).__name__))
    if not found:
        raise MsgError(M("u.live_empty"))
    n = len(found[: cfg.size])
    note = M("u.live_partial", n=n, errors=errors) if errors else M("u.live", n=n)
    return found[: cfg.size], note
