"""大盘：美股三大指数、七巨头、A 股指数的走势、回撤，以及历史上"跌到这里之后"发生了什么。

全部是**历史统计**，不是预测，也不合成任何"抄底分数"——每个指标单独摆出来，由人自己判断。

口径
  距最高回撤     收盘价 / 此前最高收盘价 − 1。"历史最大回撤"就是这个序列的最低值。
  一轮下跌       从一个高点开始、跌幅达到 T 才算一轮；收复这个高点就结束。
                 A 股多年没收复 2007、2015 年的高点，所以另加一条：从低点反弹超过 U 也算这轮结束，
                 反弹到的位置作为下一轮的起点。美股和个股不设 U（只认收复高点），与常见的熊市统计口径一致。
  本轮回撤       当前收盘相对本轮起点高点的跌幅。美股、个股就等于距最高回撤。
  之后的收益     从"第一次跌到某一档"那天的收盘价算起，1 年 / 3 年后的价格涨跌（不含股息）。
"""
from __future__ import annotations

import bisect
import json
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from statistics import median
from typing import Callable, Optional
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .i18n import M


@dataclass(frozen=True)
class Spec:
    key: str                    # 网址里用的短名
    symbol: str                 # 数据源里的代码
    source: str                 # "yahoo" | "cn"
    group: str                  # "us" | "m7" | "cn"
    t: float                    # 一轮下跌的门槛（0.10 = 跌 10% 才算一轮）
    u: Optional[float]          # 从低点反弹多少算这轮结束（None = 只认收复高点）
    ladder: tuple[int, ...]     # 分档（%）


_US = dict(source="yahoo", group="us", t=0.10, u=None, ladder=(10, 15, 20, 25, 30, 40, 50))
_M7 = dict(source="yahoo", group="m7", t=0.20, u=None, ladder=(20, 30, 40, 50, 60, 70))
_CN = dict(source="cn", group="cn", t=0.20, u=0.30, ladder=(20, 30, 40, 50, 60))

SPECS: tuple[Spec, ...] = (
    Spec("spx", "^GSPC", **_US), Spec("ixic", "^IXIC", **_US), Spec("dji", "^DJI", **_US),
    *(Spec(t.lower(), t, **_M7) for t in ("AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "META", "TSLA")),
    Spec("sh", "sh000001", **_CN), Spec("cyb", "sz399006", **_CN), Spec("kc50", "sh000688", **_CN),
    Spec("hs300", "sh000300", **_CN),
)
BY_KEY = {s.key: s for s in SPECS}
GROUPS = ("us", "m7", "cn")
VIX = "^VIX"
TTL = 3600                      # 行情一小时刷新一次
DAILY_TAIL_DAYS = 3 * 365       # 图表：最近 3 年给日线，更早的给周线


# ================================================================ 纯计算
def _d(s: str) -> date:
    return date.fromisoformat(s[:10])


def _pct(x: Optional[float], nd: int = 1) -> Optional[float]:
    return None if x is None else round(x * 100, nd)


def ath_drawdown(closes: list[float]) -> list[float]:
    out, hi = [], float("-inf")
    for c in closes:
        hi = max(hi, c)
        out.append(c / hi - 1)
    return out


def max_drawdown(dates: list[str], closes: list[float], start: int = 0) -> Optional[dict]:
    """从 start 起算的最大回撤：高点、低点、收复日（没收复为 None）。"""
    if len(closes) - start < 2:
        return None
    hi_i, best = start, (0.0, start, start)
    for i in range(start, len(closes)):
        if closes[i] > closes[hi_i]:
            hi_i = i
        dd = closes[i] / closes[hi_i] - 1
        if dd < best[0]:
            best = (dd, hi_i, i)
    dd, p, tr = best
    if dd >= 0:
        return None
    rec = next((j for j in range(tr + 1, len(closes)) if closes[j] >= closes[p]), None)
    return {"depth": _pct(dd), "peak_date": dates[p], "peak": closes[p], "trough_date": dates[tr], "trough": closes[tr],
            "recover_date": dates[rec] if rec is not None else None}


def swings(closes: list[float], t: float, u: Optional[float]) -> tuple[list[float], list[dict]]:
    """把走势切成一轮一轮的下跌。返回（本轮回撤序列，各轮下跌）。每轮：peak/trough/end 是下标，
    end=None 表示还在进行中；how = "recovered"（收复高点）| "rebound"（从低点反弹超过 u）| None。"""
    n = len(closes)
    dd, eps = [0.0] * n, []
    peak = trough = 0

    def close(end, how):
        if closes[trough] / closes[peak] - 1 <= -t:
            eps.append({"peak": peak, "trough": trough, "end": end, "how": how})

    for i, c in enumerate(closes):
        if c >= closes[peak]:
            close(i, "recovered")
            peak = trough = i
        elif c < closes[trough]:
            trough = i
        elif u is not None and trough > peak and c >= closes[trough] * (1 + u):
            close(i, "rebound")
            peak = trough = i
        dd[i] = c / closes[peak] - 1
    if n:
        close(None, None)
    return dd, eps


def _after(dates_d: list[date], i: int, days: int) -> Optional[int]:
    """第 i 天之后 days 个自然日（含）的第一个交易日下标；数据不够则 None。"""
    j = bisect.bisect_left(dates_d, dates_d[i] + timedelta(days=days))
    return j if j < len(dates_d) else None


def _fwd(closes, dates_d, i, days) -> Optional[float]:
    j = _after(dates_d, i, days)
    return None if j is None else closes[j] / closes[i] - 1


def _summ(xs: list[float]) -> dict:
    xs = [x for x in xs if x is not None]
    if not xs:
        return {"n": 0, "median": None, "pos": None}
    return {"n": len(xs), "median": _pct(median(xs)), "pos": round(100 * sum(x > 0 for x in xs) / len(xs))}


def level_stats(closes, dates_d, eps, level: float, next_level: Optional[float] = None, detail: bool = False) -> dict:
    """所有"跌到 level（如 -0.2）"的轮次：第一次跌到这一档之后又跌了多少、最终见底在哪、1/3 年后涨跌。"""
    further, finals, f1, f3, stopped, reached, years = [], [], [], [], 0, [], []
    ongoing = False
    for e in eps:
        p, tr = closes[e["peak"]], closes[e["trough"]]
        if tr / p - 1 > level:
            continue
        k = next(i for i in range(e["peak"], e["trough"] + 1) if closes[i] / p - 1 <= level)
        reached.append(k)
        f1.append(_fwd(closes, dates_d, k, 365))
        f3.append(_fwd(closes, dates_d, k, 3 * 365))
        if e["end"] is None:
            ongoing = True
            continue                        # 还没走完的这一轮不进"最终跌多深"的统计
        further.append(tr / closes[k] - 1)
        finals.append(tr / p - 1)
        years.append(dates_d[e["peak"]].year)
        if next_level is not None and tr / p - 1 > next_level:
            stopped += 1
    done = len(finals)
    return {
        "level": _pct(level), "n": len(reached), "done": done, "ongoing": ongoing,
        "further_median": _pct(median(further)) if further else None,
        "further_worst": _pct(min(further)) if further else None,
        "final_median": _pct(median(finals)) if finals else None,
        "final_worst": _pct(min(finals)) if finals else None,
        "stopped_before_next": stopped if next_level is not None else None,
        "fwd1y": _summ(f1), "fwd3y": _summ(f3),
        **({"finals": sorted(({"year": y, "depth": _pct(f)} for y, f in zip(years, finals)), key=lambda x: x["depth"])}
           if detail else {}),
    }


def rank_below(series: list[Optional[float]], value: float) -> Optional[float]:
    """历史上有多少比例的交易日比 value 更低（越小越罕见）。"""
    xs = [x for x in series if x is not None]
    if not xs:
        return None
    return round(100 * sum(x < value for x in xs) / len(xs), 1)


def ma_gap(closes: list[float], n: int = 200) -> list[Optional[float]]:
    out, s = [], 0.0
    for i, c in enumerate(closes):
        s += c
        if i >= n:
            s -= closes[i - n]
        out.append(c / (s / n) - 1 if i >= n - 1 else None)
    return out


def _downsample(dates: list[str], keep: set[int]) -> list[int]:
    """最近 3 年每天一点，更早的每周最后一个交易日一点；关键高低点总是保留。"""
    if not dates:
        return []
    cut = _d(dates[-1]) - timedelta(days=DAILY_TAIL_DAYS)
    idx = []
    for i, s in enumerate(dates):
        d = _d(s)
        if d >= cut or i in keep or i == len(dates) - 1:
            idx.append(i)
            continue
        nxt = _d(dates[i + 1])
        if nxt.isocalendar()[:2] != d.isocalendar()[:2]:
            idx.append(i)
    return idx


def analyze(spec: Spec, rows: list[tuple[str, float]], today: Optional[date] = None) -> tuple[dict, dict]:
    """一个品种的全部统计。返回（摘要，图表序列）。"""
    rows = [(d[:10], float(c)) for d, c in rows if c and c > 0]
    if len(rows) < 30:
        raise ValueError("too little history")
    dates, closes = [d for d, _ in rows], [c for _, c in rows]
    dates_d = [_d(s) for s in dates]
    today = today or dates_d[-1]
    n = len(closes)

    ath = ath_drawdown(closes)
    ath_i = max(range(n), key=lambda i: (closes[i], i))
    dd, eps = swings(closes, spec.t, spec.u)
    cur = dd[-1]
    cur_ep = eps[-1] if eps and eps[-1]["end"] is None else None
    peak_i = next(i for i in range(n - 1, -1, -1) if dd[i] == 0.0)   # 本轮起点（最近一次本轮回撤为 0 的日子）
    low_i = min(range(peak_i, n), key=lambda i: closes[i])

    ten_y = bisect.bisect_left(dates_d, today - timedelta(days=3652))
    gaps = ma_gap(closes)
    lad = [level_stats(closes, dates_d, eps, -lv / 100, -spec.ladder[k + 1] / 100 if k + 1 < len(spec.ladder) else None)
           for k, lv in enumerate(spec.ladder)]
    here = level_stats(closes, dates_d, eps, cur, detail=True) if cur <= -spec.t / 2 else None

    summary = {
        "key": spec.key, "symbol": spec.symbol, "group": spec.group,
        "first_date": dates[0], "last_date": dates[-1], "last": closes[-1],
        "ath": closes[ath_i], "ath_date": dates[ath_i], "dd_ath": _pct(ath[-1]),
        "mdd": max_drawdown(dates, closes), "mdd_10y": max_drawdown(dates, closes, ten_y) if ten_y > 0 else None,
        "cur": {"dd": _pct(cur), "peak_date": dates[peak_i], "peak": closes[peak_i],
                "low_date": dates[low_i], "low": closes[low_i], "dd_low": _pct(closes[low_i] / closes[peak_i] - 1),
                "days": (dates_d[-1] - dates_d[peak_i]).days, "is_episode": cur_ep is not None},
        "pend": {
            "dd_rank": rank_below(dd, cur) if cur < 0 else None,
            "ma200_gap": _pct(gaps[-1]), "ma200_rank": rank_below(gaps, gaps[-1]) if gaps[-1] is not None else None,
        },
        "t": round(spec.t * 100), "u": round(spec.u * 100) if spec.u else None,
        "episodes": [{
            "peak_date": dates[e["peak"]], "peak": closes[e["peak"]],
            "trough_date": dates[e["trough"]], "trough": closes[e["trough"]],
            "depth": _pct(closes[e["trough"]] / closes[e["peak"]] - 1),
            "days_down": (dates_d[e["trough"]] - dates_d[e["peak"]]).days,
            "recover_date": _recover(dates, closes, e), "how": e["how"], "ongoing": e["end"] is None,
        } for e in reversed(eps)],
        "ladder": lad, "here": here,
    }
    keep = {i for e in eps for i in (e["peak"], e["trough"])} | {ath_i}
    idx = _downsample(dates, keep)
    series = {"key": spec.key, "d": [dates[i] for i in idx], "c": [round(closes[i], 2) for i in idx],
              "dd": [_pct(dd[i]) for i in idx]}
    return summary, series


def _recover(dates, closes, e) -> Optional[str]:
    p = closes[e["peak"]]
    j = next((j for j in range(e["trough"] + 1, len(closes)) if closes[j] >= p), None)
    return dates[j] if j is not None else None


def vix_stats(rows: list[tuple[str, float]]) -> Optional[dict]:
    xs = [c for _, c in rows if c]
    if len(xs) < 250:
        return None
    last = xs[-1]
    return {"last": round(last, 2), "date": rows[-1][0][:10], "first_date": rows[0][0][:10],
            "above_rank": round(100 * sum(x > last for x in xs) / len(xs), 1),   # 历史上有多少天比现在更恐慌
            "max": round(max(xs), 2), "max_date": rows[max(range(len(xs)), key=xs.__getitem__)][0][:10],
            "median": round(median(xs), 2)}


# ================================================================ 取数
def yahoo_closes(symbol: str) -> list[tuple[str, float]]:
    import yfinance as yf

    from .providers.yahoo import _closes
    rows = _closes(yf.Ticker(symbol).history(period="max", interval="1d", auto_adjust=False))
    if not rows:
        raise ValueError(f"no data for {symbol}")
    return rows


def _get_json(url: str):
    req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urlopen(req, timeout=30) as r:
        return json.load(r)


def tencent_closes(code: str) -> list[tuple[str, float]]:
    """腾讯行情日线（指数不需要复权）。每次最多 2000 行，所以按 7 年一段分页取。每行：日期、开、收、高、低、量。"""
    rows: dict[str, float] = {}
    for y in range(1990, date.today().year + 1, 7):
        url = (f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={code},day,"
               f"{y}-01-01,{y + 6}-12-31,2000,")
        d = (_get_json(url).get("data") or {})
        k = (d.get(code) or {}) if isinstance(d, dict) else {}
        for line in k.get("day") or k.get("qfqday") or []:
            rows[line[0]] = float(line[2])
    if not rows:
        raise ValueError(f"no data for {code}")
    return sorted(rows.items())


def eastmoney_closes(code: str) -> list[tuple[str, float]]:
    """东方财富日线（备用；对频繁请求会直接断开连接）。f51 = 日期，f53 = 收盘。"""
    secid = ("1." if code.startswith("sh") else "0.") + code[2:]
    q = urlencode({"secid": secid, "fields1": "f1,f2,f3", "fields2": "f51,f53", "klt": 101, "fqt": 0,
                   "beg": "19900101", "end": "20500101", "lmt": 100000})
    data = _get_json("https://push2his.eastmoney.com/api/qt/stock/kline/get?" + q).get("data") or {}
    rows = []
    for line in data.get("klines") or []:
        d, c = line.split(",")[:2]
        rows.append((d, float(c)))
    if not rows:
        raise ValueError(f"no data for {secid}")
    return rows


def cn_closes(code: str) -> list[tuple[str, float]]:
    try:
        return tencent_closes(code)
    except Exception:  # noqa: BLE001 — 主源失败就试备用源
        return eastmoney_closes(code)


FETCHERS: dict[str, Callable[[str], list[tuple[str, float]]]] = {"yahoo": yahoo_closes, "cn": cn_closes}


# ================================================================ 服务（磁盘缓存 + 后台刷新）
class Market:
    """GET 时有缓存就直接给（过期了在后台刷新）；第一次没有缓存时返回 None，前端稍后再取。"""

    def __init__(self, data_dir: Path, fetchers: Optional[dict] = None, ttl: int = TTL,
                 clock: Callable[[], float] = time.time):
        self.dir = Path(data_dir) / "market"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.fetchers = fetchers or FETCHERS
        self.ttl, self.clock = ttl, clock
        self._lock = threading.Lock()
        self._busy: set[str] = set()

    def _path(self, name: str) -> Path:
        return self.dir / f"{name}.json"

    def _read(self, name: str) -> Optional[dict]:
        try:
            return json.loads(self._path(name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def _write(self, name: str, obj: dict) -> None:
        tmp = self._path(name + ".tmp")
        tmp.write_text(json.dumps(obj, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        tmp.replace(self._path(name))

    def group(self, g: str) -> Optional[dict]:
        data = self._read(g)
        if data is None or self.clock() - data.get("ts", 0) > self.ttl:
            self.refresh_async(g)
        if data is not None:
            data["refreshing"] = g in self._busy
        return data

    def series(self, key: str) -> Optional[dict]:
        return self._read("s_" + key)

    def refresh_async(self, g: str) -> bool:
        with self._lock:
            if g in self._busy:
                return False
            self._busy.add(g)
        threading.Thread(target=self._refresh_guarded, args=(g,), daemon=True, name=f"market-{g}").start()
        return True

    def _refresh_guarded(self, g: str) -> None:
        try:
            self.refresh(g)
        finally:
            with self._lock:
                self._busy.discard(g)

    def refresh(self, g: str) -> dict:
        """逐个取数、计算、落盘。某个品种失败时沿用上一次的结果并标出错误，绝不编数字。"""
        old = {it["key"]: it for it in (self._read(g) or {}).get("items", [])}
        items = []
        for spec in (s for s in SPECS if s.group == g):
            try:
                summary, series = analyze(spec, self.fetchers[spec.source](spec.symbol))
                self._write("s_" + spec.key, series)
                items.append(summary)
            except Exception as e:  # noqa: BLE001 — 网络、限流、数据源改版都可能
                prev = old.get(spec.key)
                err = M("mk.err.fetch", e=str(e)[:160])
                if prev and prev.get("last") is not None:          # 沿用上次的数字，但标明是旧的
                    items.append({**prev, "error": err, "stale": True})
                else:
                    items.append({"key": spec.key, "symbol": spec.symbol, "group": g, "error": err})
        out = {"group": g, "ts": self.clock(),
               "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "items": items}
        if g == "us":
            try:
                out["vix"] = vix_stats(self.fetchers["yahoo"](VIX))
            except Exception:  # noqa: BLE001
                out["vix"] = (self._read(g) or {}).get("vix")
        self._write(g, out)
        return out

    def prewarm(self) -> None:
        for g in GROUPS:
            if self._read(g) is None:
                self.refresh_async(g)
