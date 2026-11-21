"""大盘：回撤、分轮、分档统计都是纯计算，用合成走势验证；服务层用假取数函数，不碰网络。"""
from datetime import date, timedelta

import pytest

from golden_pit import market
from golden_pit.market import Market, Spec, analyze, level_stats, max_drawdown, swings

US = Spec("x", "X", "yahoo", "us", 0.10, None, (10, 20, 30))
CN = Spec("y", "Y", "cn", "cn", 0.20, 0.30, (20, 30))


def path(*legs, start=100.0):
    """legs = [(目标价, 天数), …] → 线性连接的日收盘价。"""
    out, p = [start], start
    for target, n in legs:
        step = (target - p) / n
        for _ in range(n):
            p += step
            out.append(round(p, 6))
    return out


def rows_of(closes, first=date(2000, 1, 3)):
    return [((first + timedelta(days=i)).isoformat(), c) for i, c in enumerate(closes)]


def test_max_drawdown_and_recovery():
    c = path((120, 10), (60, 10), (130, 30))            # 120 → 60 = -50%，之后收复
    d = [r[0] for r in rows_of(c)]
    m = max_drawdown(d, c)
    assert m["depth"] == -50.0 and m["peak"] == 120 and m["trough"] == 60
    assert m["recover_date"] is not None
    assert max_drawdown(d, path((150, 10))) is None      # 一路涨：没有回撤


def test_swings_need_threshold_and_end_on_recovery():
    c = path((100.0, 1), (95, 5), (101, 5), (80, 10), (110, 20), (90, 10))
    dd, eps = swings(c, 0.10, None)
    assert [round(c[e["trough"]] / c[e["peak"]] - 1, 3) for e in eps] == [-0.208, -0.182]   # -5% 那次不算一轮
    assert eps[0]["how"] == "recovered" and eps[1]["end"] is None                          # 最后一轮还在进行
    assert dd[-1] == pytest.approx(90 / 110 - 1)


def test_cn_rebound_ends_a_swing_without_regaining_the_peak():
    # 100 → 40（-60%）→ 反弹到 60（+50%，超过 30%）→ 再跌到 45：应是两轮，第二轮从反弹后的高点算
    c = path((40, 20), (60, 20), (45, 20))
    dd, eps = swings(c, 0.20, 0.30)
    assert len(eps) == 2 and eps[0]["how"] == "rebound"
    assert c[eps[1]["peak"]] == pytest.approx(60) and dd[-1] == pytest.approx(45 / 60 - 1)
    _, us_eps = swings(c, 0.20, None)                     # 不设 U：一直算同一轮
    assert len(us_eps) == 1


def test_level_stats_further_drop_and_forward_returns():
    # 两轮 -20% 以上：第一轮最终 -40%，第二轮在 -25% 见底
    c = path((100, 1), (60, 100), (100, 100), (75, 100), (130, 400))
    rows = rows_of(c)
    dates_d = [date.fromisoformat(d) for d, _ in rows]
    _, eps = swings(c, 0.10, None)
    s = level_stats(c, dates_d, eps, -0.20, -0.30, detail=True)
    assert s["n"] == 2 and s["done"] == 2 and not s["ongoing"]
    assert s["final_worst"] == -40.0 and s["further_worst"] == pytest.approx(-25, abs=0.5)      # 从 80 跌到 60
    assert s["stopped_before_next"] == 1                                    # 第二轮在 -30% 之前见底
    assert [f["depth"] for f in s["finals"]] == [-40.0, -25.0]
    assert s["fwd1y"]["n"] == 2 and s["fwd3y"]["n"] == 0                    # 数据不够 3 年就不算


def test_analyze_reports_current_state_and_never_invents_ladder_rows():
    c = path((200, 400), (150, 60))                                         # 现在距最高 -25%
    summary, series = analyze(US, rows_of(c))
    assert summary["dd_ath"] == -25.0 and summary["cur"]["dd"] == -25.0 and summary["cur"]["is_episode"]
    assert summary["episodes"][0]["ongoing"] and summary["episodes"][0]["recover_date"] is None
    assert summary["here"]["done"] == 0 and summary["here"]["final_median"] is None   # 没有完整的先例 → 空，不编
    assert summary["pend"]["ma200_gap"] < 0
    assert series["d"][-1] == rows_of(c)[-1][0] and len(series["d"]) == len(series["c"]) == len(series["dd"])


def test_analyze_rejects_too_little_history():
    with pytest.raises(ValueError):
        analyze(US, rows_of([100.0] * 10))


def test_downsample_keeps_recent_days_and_key_points():
    c = path((300, 3000), (200, 1000))
    rows = rows_of(c, first=date(2010, 1, 1))
    _, series = analyze(US, rows)
    assert len(series["d"]) < len(rows) / 2                                  # 早年按周
    assert max(series["c"]) == pytest.approx(300, abs=0.01)                  # 最高点没被采样丢掉


def test_vix_stats():
    v = market.vix_stats(rows_of([15.0] * 300 + [40.0]))
    assert v["last"] == 40 and v["above_rank"] == 0 and v["max"] == 40


# ---------------------------------------------------------------- 服务层
def fake_fetchers(fail=()):
    up = rows_of(path((200, 400), (170, 30)))

    def f(sym):
        if sym in fail:
            raise ValueError("boom")
        return rows_of([20.0] * 300) if sym == market.VIX else up
    return {"yahoo": f, "cn": f}


def test_market_refresh_writes_group_and_series(tmp_path):
    m = Market(tmp_path, fetchers=fake_fetchers())
    out = m.refresh("us")
    assert [it["key"] for it in out["items"]] == ["spx", "ixic", "dji"] and out["vix"]["last"] == 20
    assert m.series("spx")["key"] == "spx"
    assert m.group("us")["items"][0]["dd_ath"] == -15.0


def test_failed_fetch_keeps_old_numbers_marked_stale_or_reports_error(tmp_path):
    m = Market(tmp_path, fetchers=fake_fetchers())
    m.refresh("m7")
    m.fetchers = fake_fetchers(fail={"AAPL"})
    items = {it["key"]: it for it in m.refresh("m7")["items"]}
    assert items["aapl"]["stale"] and items["aapl"]["last"] is not None and items["aapl"]["error"]
    m2 = Market(tmp_path / "fresh", fetchers=fake_fetchers(fail={"sh000001"}))
    sh = next(it for it in m2.refresh("cn")["items"] if it["key"] == "sh")
    assert sh["error"] and "last" not in sh                                  # 没有旧数据就只报错


def test_group_is_none_before_first_refresh_and_triggers_one(tmp_path):
    calls = []
    m = Market(tmp_path, fetchers=fake_fetchers())
    m.refresh_async = lambda g: calls.append(g) or True
    assert m.group("cn") is None and calls == ["cn"]


def test_market_api(tmp_path):
    from golden_pit.server import create_app
    from tests.test_api import make_engine

    s, engine = make_engine(tmp_path)
    m = Market(tmp_path, fetchers=fake_fetchers())
    m.refresh("us")
    app = create_app(s, engine=engine, market=m)
    c = app.test_client()
    body = c.get("/api/market/us?lang=en").get_json()
    assert body["items"][0]["key"] == "spx"
    assert c.get("/api/market/series/spx").get_json()["key"] == "spx"
    assert c.get("/api/market/nope").status_code == 404
    assert c.get("/api/market/series/nope").status_code == 404
