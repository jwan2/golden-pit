"""Yahoo Finance 数据层。只负责取数并转成 RawStock，不做任何判断。

拆成两部分，便于分别缓存：
  quote         info（价格、估值、负债等快照）+ 近一年日收盘价   —— 变化快
  fundamentals  三张年报 + 拆股记录                              —— 变化慢
"""
from __future__ import annotations

import random
import time
from datetime import datetime, timezone
from typing import Optional

from ..i18n import M, MsgError
from ..models import RawStock, Statement, num


class FetchError(MsgError):
    """取数失败（网络、限流、代码无效等）。.msg 会按语言展示给用户。"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _statement(df) -> Statement:
    """yfinance 的财报 DataFrame（列 = 报告期，新→旧）→ Statement。"""
    if df is None or getattr(df, "empty", True):
        return Statement()
    dates = [c.strftime("%Y-%m-%d") if hasattr(c, "strftime") else str(c) for c in df.columns]
    rows = {str(label): [num(v) for v in df.loc[label].tolist()] for label in df.index}
    return Statement(dates=dates, rows=rows)


def _splits(series) -> list[tuple[str, float]]:
    if series is None or getattr(series, "empty", True):
        return []
    out = []
    for idx, ratio in series.items():
        r = num(ratio)
        if r and r != 1.0:
            out.append((idx.strftime("%Y-%m-%d") if hasattr(idx, "strftime") else str(idx), r))
    return out


def _closes(df) -> list[tuple[str, float]]:
    if df is None or getattr(df, "empty", True) or "Close" not in df:
        return []
    out = []
    for idx, v in df["Close"].items():
        c = num(v)
        if c is not None and c > 0:
            out.append((idx.strftime("%Y-%m-%d") if hasattr(idx, "strftime") else str(idx)[:10], round(c, 4)))
    return out


def _safe(fn):
    try:
        return fn()
    except Exception:  # noqa: BLE001 — 单张报表失败不应让整只股票失败，缺失会体现为"未知"
        return None


def _retry(fn, ticker: str, retries: int = 2):
    last: Optional[Exception] = None
    for attempt in range(retries + 1):
        try:
            return fn()
        except FetchError:
            raise
        except Exception as e:  # noqa: BLE001 — yfinance 会抛各种异常类型
            last = e
            if attempt < retries:
                time.sleep(1.5 * (attempt + 1) + random.random())
    raise FetchError(M("err.fetch", t=ticker, e=f"{type(last).__name__}: {last}"))


def fetch_quote(ticker: str) -> dict:
    import yfinance as yf  # 延迟导入：测试和纯逻辑模块不需要它

    def go():
        t = yf.Ticker(ticker)
        info = t.info or {}
        price = num(info.get("currentPrice")) or num(info.get("regularMarketPrice"))
        if not price:
            raise FetchError(M("err.no_quote", t=ticker))
        hist = _safe(lambda: t.history(period="1y", interval="1d", auto_adjust=False))
        return {"info": info, "closes": _closes(hist), "fetched_at": now_iso()}
    return _retry(go, ticker)


def fetch_fundamentals(ticker: str) -> dict:
    import yfinance as yf

    def go():
        t = yf.Ticker(ticker)
        return {
            "balance": _statement(_safe(lambda: t.balance_sheet)),
            "income": _statement(_safe(lambda: t.income_stmt)),
            "cashflow": _statement(_safe(lambda: t.cashflow)),
            "splits": _splits(_safe(lambda: t.splits)),
            "fetched_at": now_iso(),
        }
    return _retry(go, ticker)


def assemble(ticker: str, quote: dict, fund: dict) -> RawStock:
    return RawStock(
        ticker=ticker, info=quote["info"],
        balance=fund["balance"], income=fund["income"], cashflow=fund["cashflow"],
        splits=fund["splits"], fetched_at=quote["fetched_at"],
        closes=quote.get("closes", []), fundamentals_fetched_at=fund["fetched_at"],
    )


def fetch_raw(ticker: str) -> RawStock:
    ticker = ticker.upper().strip()
    return assemble(ticker, fetch_quote(ticker), fetch_fundamentals(ticker))
