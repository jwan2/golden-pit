"""候选池：核心池（知识库）、Yahoo 实时筛选器、用户自定义。"""
from __future__ import annotations

import datetime
import io
import json
import re
from pathlib import Path
from typing import Optional

from . import moat
from .config import ScreenerConfig
from .i18n import M, MsgError

TICKER_RE = re.compile(r"^[A-Z]{1,5}(-[A-Z])?$")
SP500_FILE = "sp500.json"
SP500_SOURCE = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
MODES = ("live", "curated", "sp500", "custom")


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


def _sp500_path(data_dir: Optional[Path]) -> Path:
    """数据目录里有更新过的名单就用它，否则用随包附带的名单。"""
    if data_dir and (Path(data_dir) / SP500_FILE).exists():
        return Path(data_dir) / SP500_FILE
    return Path(__file__).parent / "data" / SP500_FILE


def sp500(data_dir: Optional[Path] = None) -> tuple[list[str], dict]:
    d = json.loads(_sp500_path(data_dir).read_text(encoding="utf-8"))
    tickers = [x["t"] for x in d["tickers"] if TICKER_RE.match(x["t"])]
    return tickers, M("u.sp500", n=len(tickers), date=d.get("as_of", "?"))


def update_sp500(data_dir: Path) -> tuple[int, str]:
    """从维基百科拉取最新成分股，写入数据目录（不改动随包名单）。失败时抛异常，不写半成品。"""
    import pandas as pd
    import requests

    html = requests.get(SP500_SOURCE, timeout=30,
                        headers={"User-Agent": "golden-pit (https://github.com/jwan2/golden-pit)"}).text
    df = pd.read_html(io.StringIO(html), attrs={"id": "constituents"})[0]
    rows = []
    for _, r in df.iterrows():
        t = str(r["Symbol"]).strip().upper().replace(".", "-")
        if TICKER_RE.match(t):
            rows.append({"t": t, "sector": str(r.get("GICS Sector", "")).strip()})
    if len(rows) < 400:
        raise MsgError(M("u.sp500_bad", n=len(rows)))
    as_of = datetime.date.today().isoformat()
    path = Path(data_dir) / SP500_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"index": "S&P 500", "as_of": as_of, "source": SP500_SOURCE, "tickers": rows}),
                   encoding="utf-8")
    tmp.replace(path)
    return len(rows), as_of


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
