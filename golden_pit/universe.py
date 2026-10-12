"""候选池：核心池（知识库）、Yahoo 实时筛选器、用户自定义。"""
from __future__ import annotations

import re

from . import moat
from .config import ScreenerConfig

TICKER_RE = re.compile(r"^[A-Z]{1,5}(-[A-Z])?$")


def parse_tickers(raw: list[str] | str, limit: int = 60) -> list[str]:
    items = re.split(r"[\s,;，；]+", raw) if isinstance(raw, str) else raw
    out: list[str] = []
    for t in items:
        t = str(t).upper().strip()
        if TICKER_RE.match(t) and t not in out:
            out.append(t)
    return out[:limit]


def curated() -> tuple[list[str], str]:
    tickers = moat.curated_tickers()
    return tickers, f"核心池：知识库中的 {len(tickers)} 只标的"


def live_screen(cfg: ScreenerConfig) -> tuple[list[str], str]:
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
    errors: list[str] = []
    for label, operands, sort_field, asc in (
        ("大中盘回撤", base, "intradaymarketcap", False),
        ("深度回撤+低P/B", deep, "fiftytwowkpercentchange", True),
    ):
        try:
            res = yf.screen(yf.EquityQuery("and", operands), sortField=sort_field, sortAsc=asc,
                            size=min(max(cfg.size, 10), 250))
            for q in (res or {}).get("quotes", []):
                sym = q.get("symbol", "")
                if TICKER_RE.match(sym) and sym not in found:
                    found.append(sym)
        except Exception as e:  # noqa: BLE001
            errors.append(f"{label}查询失败：{type(e).__name__}")
    if not found:
        raise RuntimeError("Yahoo 实时筛选器没有返回任何标的" + (f"（{'；'.join(errors)}）" if errors else ""))
    note = f"Yahoo 实时筛选：{len(found[:cfg.size])} 只"
    if errors:
        note += f"（部分查询失败：{'；'.join(errors)}）"
    return found[: cfg.size], note
