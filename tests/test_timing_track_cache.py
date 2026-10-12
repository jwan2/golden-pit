"""走势时机、战绩追踪、磁盘缓存。"""
import math
import os
import time
from datetime import date, timedelta

import pytest

from golden_pit import timing
from golden_pit.models import Statement
from golden_pit.providers.cache import CachedProvider
from golden_pit.providers.yahoo import FetchError
from golden_pit.track import Tracker


def series(values):
    d0 = date(2025, 1, 1)
    return [((d0 + timedelta(days=i)).isoformat(), float(v)) for i, v in enumerate(values)]


# ---------------- 走势
def test_timing_needs_history():
    assert timing.analyze_timing(series([10] * 30))["state"] == "unknown"


def test_timing_falling_when_making_new_lows():
    xs = [100 - i * 0.3 for i in range(200)]                 # 一路下跌，今天就是最低
    t = timing.analyze_timing(series(xs))
    assert t["state"] == "falling" and t["metrics"]["days_since_low"] == 0


def test_timing_rebounding_after_bottom():
    xs = [100 - i * 0.4 for i in range(150)] + [40 + i * 0.25 for i in range(60)]   # 跌到 40 后回升
    t = timing.analyze_timing(series(xs))
    assert t["state"] == "rebounding" and t["metrics"]["days_since_low"] >= 10


def test_timing_basing_near_flat_bottom():
    xs = [100 - i * 0.5 for i in range(120)] + [40 + (1 if i % 2 else 0) for i in range(60)]
    t = timing.analyze_timing(series(xs))
    assert t["state"] == "basing" and t["metrics"]["above_low_pct"] <= 12


def test_rsi_bounds():
    assert timing.rsi([float(i) for i in range(30)]) == 100.0
    r = timing.rsi([10 + math.sin(i) for i in range(60)])
    assert 0 <= r <= 100


def test_chart_series_downsamples_and_keeps_low_and_last():
    xs = [100 - i * 0.1 for i in range(100)] + [50] + [60 + i * 0.1 for i in range(150)]
    c = timing.chart_series(series(xs), max_points=50)
    assert len(c["closes"]) <= 60 and 50.0 in c["closes"] and c["closes"][-1] == round(xs[-1], 2)


# ---------------- 战绩追踪
def scan(prices_tiers, sid):
    return {"scan_id": str(sid), "finished_at": f"2026-01-0{sid}T00:00:00+00:00",
            "results": [{"ticker": t, "name": t, "price": p, "verdict": {"tier": tier}}
                        for t, (p, tier) in prices_tiers.items()]}


def test_tracker_open_exit_and_reentry(tmp_path):
    tr = Tracker(tmp_path)
    bench = {"SPY": 100.0}
    px = lambda t: bench.get(t)  # noqa: E731
    tr.update(scan({"A": (10, "golden_pit"), "B": (20, "fail")}, 1), px)
    bench["SPY"] = 105.0
    tr.update(scan({"A": (12, "quality_wait")}, 2), px)           # A 掉出：+20%，基准 +5%
    tr.update(scan({"A": (11, "golden_pit")}, 3), px)             # 再次入选 → 第二条
    rep = Tracker.report(tr.load())
    a = sorted([e for e in rep["entries"] if e["ticker"] == "A"], key=lambda e: e["flagged_at"])
    assert len(a) == 2 and a[0]["status"] == "exited" and a[1]["status"] == "open"
    assert a[0]["return_during_pct"] == 20.0 and a[0]["excess_during_pct"] == 15.0
    assert a[0]["return_since_pct"] == 10.0                        # 现价 11 相对 10
    assert all(e["ticker"] != "B" for e in rep["entries"])         # 从未入选的不追踪


def test_tracker_insufficient_or_error_is_not_an_exit(tmp_path):
    tr = Tracker(tmp_path)
    px = lambda t: 100.0  # noqa: E731
    tr.update(scan({"A": (10, "golden_pit")}, 1), px)
    tr.update(scan({"A": (9, "insufficient")}, 2), px)
    tr.update(scan({"A": (None, "error")}, 3), px)
    assert tr.load()["entries"][0]["status"] == "open"


def test_tracker_missing_benchmark_gives_none_excess(tmp_path):
    tr = Tracker(tmp_path)
    tr.update(scan({"A": (10, "golden_pit")}, 1), lambda t: None)
    rep = Tracker.report(tr.load())
    assert rep["entries"][0]["excess_since_pct"] is None and rep["summary"]["excess_since"]["n"] == 0


# ---------------- 缓存
def fake_quote_factory(calls, fail=False):
    def f(t):
        calls.append(("q", t))
        if fail:
            raise FetchError("down")
        return {"info": {"currentPrice": 10.0}, "closes": [("2026-01-01", 10.0)],
                "fetched_at": "2026-01-01T00:00:00+00:00"}
    return f


def fake_fund_factory(calls, empty=False):
    def f(t):
        calls.append(("f", t))
        b = Statement() if empty else Statement(["2025-12-31"], {"Total Assets": [1.0]})
        return {"balance": b, "income": Statement(), "cashflow": Statement(), "splits": [("2024-01-01", 2.0)],
                "fetched_at": "2026-01-01T00:00:00+00:00"}
    return f


def test_cache_hits_and_roundtrip(tmp_path):
    calls = []
    cp = CachedProvider(tmp_path, fetch_quote=fake_quote_factory(calls), fetch_fundamentals=fake_fund_factory(calls))
    r1 = cp.raw("AAA")
    r2 = cp.raw("AAA")
    assert calls == [("q", "AAA"), ("f", "AAA")]                   # 第二次全部命中缓存
    assert r2.balance.get("Total Assets") == 1.0 and r2.splits == [("2024-01-01", 2.0)]
    assert r2.closes == [("2026-01-01", 10.0)] and r1.fetched_at == r2.fetched_at


def test_quote_expires_and_failure_never_falls_back_to_stale(tmp_path):
    calls = []
    cp = CachedProvider(tmp_path, quote_ttl=60, fetch_quote=fake_quote_factory(calls),
                        fetch_fundamentals=fake_fund_factory(calls))
    cp.quote("AAA")
    p = tmp_path / "quote" / "AAA.json"
    old = time.time() - 3600
    os.utime(p, (old, old))                                         # 让缓存过期
    cp._fetch_quote = fake_quote_factory(calls, fail=True)
    with pytest.raises(FetchError):
        cp.quote("AAA")
    assert cp.price("AAA") is None                                  # 价格接口：失败返回 None


def test_empty_fundamentals_not_cached(tmp_path):
    calls = []
    cp = CachedProvider(tmp_path, fetch_quote=fake_quote_factory(calls),
                        fetch_fundamentals=fake_fund_factory(calls, empty=True))
    cp.fundamentals("AAA")
    cp.fundamentals("AAA")
    assert calls.count(("f", "AAA")) == 2


def test_cache_rejects_bad_ticker(tmp_path):
    cp = CachedProvider(tmp_path, fetch_quote=fake_quote_factory([]), fetch_fundamentals=fake_fund_factory([]))
    with pytest.raises(FetchError):
        cp.quote("../../x")


def test_stats_ignore_entries_younger_than_a_week(tmp_path):
    tr = Tracker(tmp_path)
    tr.update(scan({"A": (10, "golden_pit")}, 1), lambda t: 100.0)
    data = tr.load()
    assert Tracker.report(data)["summary"]["return_since"]["n"] == 0          # 当天：不计入
    data["entries"][0].update(last_price=11.0, last_at="2026-01-20T00:00:00+00:00")
    st = Tracker.report(data)["summary"]["return_since"]
    assert st["n"] == 1 and st["mean"] == 10.0 and st["win_rate"] == 100.0


# ---------------- S&P 500 候选池 / 时限 / 历史瘦身
def test_sp500_bundled_list():
    from golden_pit import universe
    tickers, note = universe.sp500()
    assert len(tickers) >= 400 and "BRK-B" in tickers and "AAPL" in tickers
    assert len(set(tickers)) == len(tickers)
    from golden_pit.i18n import render
    assert "S&P 500" in render(note, "en")


def test_sp500_data_dir_override(tmp_path):
    import json as _j
    from golden_pit import universe
    (tmp_path / universe.SP500_FILE).write_text(_j.dumps({"as_of": "2030-01-01", "tickers": [{"t": "AAA"}, {"t": "bad!"}]}))
    tickers, _ = universe.sp500(tmp_path)
    assert tickers == ["AAA"]


def test_deadline_scales_with_universe():
    from golden_pit.scanner import SCAN_DEADLINE_SECONDS, deadline_for
    assert deadline_for(10) == SCAN_DEADLINE_SECONDS and deadline_for(503) > SCAN_DEADLINE_SECONDS


def test_history_archive_has_no_charts(tmp_path):
    import json as _j
    from golden_pit.store import Store
    st = Store(tmp_path)
    st.save_scan({"scan_id": "20260101_000000", "finished_at": "x",
                  "results": [{"ticker": "A", "chart": {"closes": [1, 2]}, "verdict": {"tier": "fail"}}]})
    assert "chart" in st.latest()["results"][0]
    hist = _j.loads((tmp_path / "history" / "scan_20260101_000000.json").read_text())
    assert "chart" not in hist["results"][0] and hist["results"][0]["ticker"] == "A"


# ---------------- 限流：节流、识别、补扫
def test_throttle_spaces_out_network_fetches(tmp_path):
    calls = []
    cp = CachedProvider(tmp_path, fetch_quote=fake_quote_factory(calls), fetch_fundamentals=fake_fund_factory(calls),
                        min_interval=0.05)
    t0 = time.monotonic()
    for t in ("AAA", "BBB", "CCC"):
        cp.quote(t)
    assert time.monotonic() - t0 >= 0.09            # 3 次真实抓取 → 至少 2 个间隔
    t1 = time.monotonic()
    cp.quote("AAA")                                  # 缓存命中不节流
    assert time.monotonic() - t1 < 0.04


def test_rate_limit_detection():
    from golden_pit.providers.yahoo import is_rate_limit

    class YFRateLimitError(Exception):
        pass
    assert is_rate_limit(YFRateLimitError("Too Many Requests"))
    assert is_rate_limit(RuntimeError("429 Too Many Requests")) and not is_rate_limit(ValueError("x"))


def test_scanner_retries_transient_fetch_failures(tmp_path):
    from golden_pit.config import ScreenerConfig, Thresholds
    from golden_pit.i18n import M
    from golden_pit.scanner import Scanner
    from golden_pit.store import Store
    from tests.conftest import healthy_raw
    attempts = {}

    def flaky(t):
        attempts[t] = attempts.get(t, 0) + 1
        if t == "FLAKY" and attempts[t] == 1:
            raise FetchError(M("err.fetch", t=t, e="YFRateLimitError: Too Many Requests"))
        if t == "DEAD":
            raise FetchError(M("err.no_quote", t=t))
        return healthy_raw(t)
    sc = Scanner(Store(tmp_path), Thresholds(), ScreenerConfig(), workers=2, fetch=flaky)
    sc.retry_cooldown = 0
    scan = sc.run_sync("custom", ["AAA", "FLAKY", "DEAD"], "t")
    tiers = {r["ticker"]: r["verdict"]["tier"] for r in scan["results"]}
    assert tiers == {"AAA": "golden_pit", "FLAKY": "golden_pit", "DEAD": "error"}
    assert attempts == {"AAA": 1, "FLAKY": 2, "DEAD": 1}      # 无效代码不重试



def test_cache_dir_deleted_while_running_still_works(tmp_path):
    """真实事故：服务运行中删除缓存目录 → 之前每只股票都 FileNotFoundError。"""
    import shutil
    calls = []
    cp = CachedProvider(tmp_path / "c", fetch_quote=fake_quote_factory(calls), fetch_fundamentals=fake_fund_factory(calls),
                        min_interval=0)
    shutil.rmtree(tmp_path / "c")
    r = cp.raw("AAA")
    assert r.info["currentPrice"] == 10.0 and (tmp_path / "c" / "quote" / "AAA.json").exists()


def test_cache_write_failure_is_not_fatal(tmp_path, monkeypatch):
    import tempfile as _tf
    calls = []
    cp = CachedProvider(tmp_path, fetch_quote=fake_quote_factory(calls), fetch_fundamentals=fake_fund_factory(calls),
                        min_interval=0)

    def boom(*a, **k):
        raise PermissionError("read-only")
    monkeypatch.setattr(_tf, "mkstemp", boom)
    assert cp.raw("AAA").info["currentPrice"] == 10.0


def test_mostly_failed_scan_does_not_replace_previous(tmp_path):
    from golden_pit.config import ScreenerConfig, Thresholds
    from golden_pit.i18n import M, MsgError, render
    from golden_pit.scanner import Scanner
    from golden_pit.store import Store
    from tests.conftest import healthy_raw
    st = Store(tmp_path)
    good = Scanner(st, Thresholds(), ScreenerConfig(), workers=2, fetch=healthy_raw)
    first = good.run_sync("custom", ["AAA", "BBB", "CCC", "DDD", "EEE"], "t")

    def broken(t):
        raise FetchError(M("err.analyze", t=t, e="FileNotFoundError"))
    bad = Scanner(st, Thresholds(), ScreenerConfig(), workers=2, fetch=broken)
    bad.retry_cooldown = 0
    with pytest.raises(MsgError) as ei:
        bad.run_sync("custom", ["AAA", "BBB", "CCC", "DDD", "EEE"], "t")
    assert "0/5" in render(ei.value.msg, "en")
    assert st.latest()["scan_id"] == first["scan_id"]               # 上一次好结果保持不变
    assert len(st.list_history()) == 1



def test_combined_universe_merges_and_survives_live_failure():
    from golden_pit import universe
    from golden_pit.config import ScreenerConfig
    from golden_pit.i18n import render
    base, _ = universe.sp500()
    tickers, note = universe.combined(ScreenerConfig(), live=lambda cfg: (["AAPL", "ZZZQ", "BRBR"], {}))
    assert tickers[: len(base)] == base and tickers[len(base):] == ["ZZZQ", "BRBR"]
    assert "2 outside" in render(note, "en")

    def boom(cfg):
        raise RuntimeError("yahoo down")
    tickers, note = universe.combined(ScreenerConfig(), live=boom)
    assert tickers == base and "failed" in render(note, "en")
