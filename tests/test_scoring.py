"""判定层：重点验证"没跌的股票不可能是黄金坑"。"""
from golden_pit import analyze
from golden_pit.config import Thresholds
from golden_pit.scanner import diff_scans
from golden_pit.scoring import TIER_ORDER

TH = Thresholds()


def tier(raw, th=TH):
    return analyze.analyze_raw(raw, th)["verdict"]["tier"]


def test_healthy_in_a_pit_is_golden_pit(raw):
    assert tier(raw) == "golden_pit"


def test_perfect_quality_but_no_drawdown_is_not_a_pit(mutate):
    """旧版 bug：质量 80 分就够"强烈买入 · 核心黄金坑"，哪怕股价就在高点。"""
    r = mutate(lambda r: r.info.update({"fiftyTwoWeekHigh": 105.0}))   # 回撤 4.8%
    assert tier(r) == "quality_wait"


def test_drop_just_below_threshold_is_not_pit(mutate):
    r = mutate(lambda r: r.info.update({"fiftyTwoWeekHigh": 140.0}))   # 回撤 28.6% < 30%
    assert tier(r) == "quality_wait"


def test_expensive_valuation_blocks_pit_even_with_big_drop(mutate):
    """旧版 RACE：P/B 16.7、Forward P/E 31.7、回撤 21% 仍得 80 分。"""
    r = mutate(lambda r: r.info.update({"priceToBook": 16.7, "forwardPE": 31.65}))
    assert tier(r) == "quality_wait"


def test_one_valuation_anchor_is_enough_by_default(mutate):
    r = mutate(lambda r: r.info.update({"priceToBook": 16.7}))  # Forward P/E 12 仍合格
    assert tier(r) == "golden_pit"


def test_two_anchors_required_when_configured(mutate):
    r = mutate(lambda r: r.info.update({"priceToBook": 16.7}))
    assert tier(r, Thresholds(pit_min_anchors=2)) == "quality_wait"


def test_cheap_but_weak_company_is_value_trap(mutate):
    def f(r):
        r.income.rows.update({"EBIT": [-1e9] * 4})
        r.info.update({"freeCashflow": -1e8, "currentRatio": 0.6})
    assert tier(mutate(f)) == "value_trap"


def test_weak_and_not_cheap_fails(mutate):
    def f(r):
        r.income.rows.update({"EBIT": [-1e9] * 4})
        r.info.update({"freeCashflow": -1e8, "currentRatio": 0.6, "fiftyTwoWeekHigh": 105.0})
    assert tier(mutate(f)) == "fail"


def test_too_many_unknowns_is_insufficient_not_passed(mutate):
    def f(r):
        for k in ("totalDebt", "totalCash", "ebitda", "freeCashflow", "currentRatio"):
            r.info.pop(k, None)
        r.balance.rows.clear(); r.cashflow.rows.clear()
    v = analyze.analyze_raw(mutate(f), TH)["verdict"]
    assert v["tier"] == "insufficient" and v["unknown_count"] > TH.max_unknown_checks


def test_unknown_drop_is_insufficient(mutate):
    r = mutate(lambda r: r.info.pop("fiftyTwoWeekHigh"))
    assert tier(r) == "insufficient"


def test_financial_company_is_not_applicable(mutate):
    r = mutate(lambda r: r.info.update({"sector": "Financial Services"}))
    assert tier(r) == "n_a"


def test_verdict_explains_itself(mutate):
    r = mutate(lambda r: r.info.update({"fiftyTwoWeekHigh": 105.0}))
    reasons = analyze.analyze_raw(r, TH)["verdict"]["reasons"]
    assert any("不在坑里" in x for x in reasons)


def test_sort_key_orders_tiers_then_quality_then_drawdown(mutate):
    a = analyze.analyze_raw(mutate(lambda r: None), TH)["verdict"]["sort_key"]                         # golden
    b = analyze.analyze_raw(mutate(lambda r: r.info.update({"fiftyTwoWeekHigh": 105.0})), TH)["verdict"]["sort_key"]
    assert a < b and a[0] == TIER_ORDER.index("golden_pit")


def test_error_result_has_error_tier():
    r = analyze.analyze_ticker("ZZZZ", TH, fetch=lambda t: (_ for _ in ()).throw(RuntimeError("boom")))
    assert r["verdict"]["tier"] == "error" and "boom" in r["error"]


def test_diff_scans():
    mk = lambda pairs: {"finished_at": "t", "results": [{"ticker": t, "verdict": {"tier": x}} for t, x in pairs]}  # noqa: E731
    d = diff_scans(mk([("A", "golden_pit"), ("B", "fail")]), mk([("A", "quality_wait"), ("B", "golden_pit"), ("C", "fail")]))
    assert d["entered_golden_pit"] == ["B"]
    assert d["left_golden_pit"] == [{"ticker": "A", "now": "quality_wait"}]
    assert d["new_tickers"] == ["C"]
    assert diff_scans(None, mk([]))["has_previous"] is False
