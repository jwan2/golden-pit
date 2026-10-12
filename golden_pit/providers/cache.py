"""磁盘缓存：让重复扫描更快、能扫更大的股票池，同时不牺牲数据新鲜度。

规则：
- 行情（价格、估值快照、一年走势）默认 1 小时过期；抓取失败时**绝不**回退到旧行情。
- 财报默认 7 天过期（年报一年才变一次）；只缓存非空结果，避免把"抓取失败"缓存 7 天。
- 每条数据都保留真实的抓取时间，前端照实展示。
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Callable, Optional

from ..models import RawStock, Statement, num
from . import yahoo

SAFE_NAME = re.compile(r"^[A-Z]{1,5}(-[A-Z])?$")


def _dump_statement(s: Statement) -> dict:
    return {"dates": s.dates, "rows": s.rows}


def _load_statement(d: dict) -> Statement:
    return Statement(dates=list(d.get("dates", [])), rows={k: list(v) for k, v in d.get("rows", {}).items()})


class CachedProvider:
    def __init__(self, cache_dir: Path, quote_ttl: float = 3600, fundamentals_ttl: float = 7 * 86400,
                 fetch_quote: Callable[[str], dict] = yahoo.fetch_quote,
                 fetch_fundamentals: Callable[[str], dict] = yahoo.fetch_fundamentals):
        self.dir = Path(cache_dir)
        (self.dir / "quote").mkdir(parents=True, exist_ok=True)
        (self.dir / "fundamentals").mkdir(parents=True, exist_ok=True)
        self.quote_ttl, self.fund_ttl = quote_ttl, fundamentals_ttl
        self._fetch_quote, self._fetch_fund = fetch_quote, fetch_fundamentals

    # ---------------------------------------------------------------- 底层读写
    def _path(self, kind: str, ticker: str) -> Path:
        if not SAFE_NAME.match(ticker):
            raise yahoo.FetchError(f"无效的股票代码：{ticker}")
        return self.dir / kind / f"{ticker}.json"

    def _read(self, kind: str, ticker: str, ttl: float) -> Optional[dict]:
        p = self._path(kind, ticker)
        try:
            if time.time() - p.stat().st_mtime > ttl:
                return None
            return json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def _write(self, kind: str, ticker: str, obj: dict) -> None:
        p = self._path(kind, ticker)
        fd, tmp = tempfile.mkstemp(dir=p.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(obj, f, ensure_ascii=False, default=str)
            os.replace(tmp, p)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    # ---------------------------------------------------------------- 对外接口
    def quote(self, ticker: str, refresh: bool = False) -> dict:
        ticker = ticker.upper().strip()
        cached = None if refresh else self._read("quote", ticker, self.quote_ttl)
        if cached:
            cached["closes"] = [tuple(x) for x in cached.get("closes", [])]
            return cached
        q = self._fetch_quote(ticker)          # 失败直接抛出，不回退旧数据
        self._write("quote", ticker, q)
        return q

    def fundamentals(self, ticker: str, refresh: bool = False) -> dict:
        ticker = ticker.upper().strip()
        cached = None if refresh else self._read("fundamentals", ticker, self.fund_ttl)
        if cached:
            return {"balance": _load_statement(cached["balance"]), "income": _load_statement(cached["income"]),
                    "cashflow": _load_statement(cached["cashflow"]),
                    "splits": [tuple(x) for x in cached.get("splits", [])], "fetched_at": cached["fetched_at"]}
        f = self._fetch_fund(ticker)
        if not f["balance"].empty:              # 只缓存有效结果
            self._write("fundamentals", ticker, {
                "balance": _dump_statement(f["balance"]), "income": _dump_statement(f["income"]),
                "cashflow": _dump_statement(f["cashflow"]), "splits": f["splits"], "fetched_at": f["fetched_at"]})
        return f

    def raw(self, ticker: str, refresh: bool = False) -> RawStock:
        ticker = ticker.upper().strip()
        return yahoo.assemble(ticker, self.quote(ticker, refresh), self.fundamentals(ticker, refresh))

    __call__ = raw

    def price(self, ticker: str) -> Optional[float]:
        """只要现价（用于战绩追踪、基准）。失败返回 None，由调用方决定如何展示。"""
        try:
            info = self.quote(ticker)["info"]
        except yahoo.FetchError:
            return None
        return num(info.get("currentPrice")) or num(info.get("regularMarketPrice"))
