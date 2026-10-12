"""指标层：重点验证旧版踩过的坑不会再出现。"""
import math

from golden_pit import metrics
from golden_pit.config import Thresholds
from golden_pit.models import FAIL, NA, PASS, UNKNOWN

TH = Thresholds()


def by_key(raw, th=TH):
    return {c.key: c for c in metrics.build_checks(raw, th)}


def test_healthy_company_passes_everything(raw):
    checks = by_key(raw)
    assert {k: c.status for k, c in checks.items()} == {k: PASS for k in checks}


# ---- 利息保障
def test_negative_ebit_does_not_pass_interest_coverage(mutate):
    """旧版对覆盖倍数取绝对值，亏损公司反而会通过。"""
    r = mutate(lambda r: r.income.rows.update({"EBIT": [-2e9] * 4}))
    c = by_key(r)["interest_coverage"]
    assert c.status == FAIL and c.value < 0


def test_missing_interest_with_debt_is_unknown_not_pass(mutate):
    """旧版把"没披露利息"当成"无债务"并直接给 999 倍和 1 分。"""
    r = mutate(lambda r: r.income.rows.pop("Interest Expense"))
    assert by_key(r)["interest_coverage"].status == UNKNOWN


def test_missing_interest_but_zero_debt_passes(mutate):
    def f(r):
        r.income.rows.pop("Interest Expense")
        r.info["totalDebt"] = 0
    assert by_key(mutate(f))["interest_coverage"].status == PASS


# ---- 净负债
def test_missing_debt_and_cash_is_unknown_not_net_cash(mutate):
    """旧版 totalDebt/totalCash 缺失都默认为 0，然后 0<=0 判定"零净负债"通过。"""
    def f(r):
        r.info.pop("totalDebt"); r.info.pop("totalCash")
    assert by_key(mutate(f))["net_debt_ebitda"].status == UNKNOWN


def test_net_debt_with_negative_ebitda_fails(mutate):
    r = mutate(lambda r: r.info.update({"ebitda": -1e9}))
    assert by_key(r)["net_debt_ebitda"].status == FAIL


def test_net_cash_passes_without_ebitda(mutate):
    def f(r):
        r.info.update({"totalDebt": 1e9, "totalCash": 3e9}); r.info.pop("ebitda")
    assert by_key(mutate(f))["net_debt_ebitda"].status == PASS


# ---- Altman Z
def test_altman_missing_inputs_is_unknown_and_names_them(mutate):
    r = mutate(lambda r: r.balance.rows.pop("Retained Earnings"))
    c = by_key(r)["altman_z"]
    assert c.status == UNKNOWN and "留存收益" in c.note


def test_altman_value(raw):
    z = by_key(raw)["altman_z"].value
    assert math.isclose(z, 2.4944, abs_tol=0.01)


# ---- 股本变化
def test_share_reduction_threshold_zero_change_fails(mutate):
    r = mutate(lambda r: r.balance.rows.update({"Ordinary Shares Number": [500e6] * 4}))
    assert by_key(r)["share_change"].status == FAIL  # 旧版阈值 0%，"没变化"也算通过


def test_share_change_with_split_in_window_is_unknown(mutate):
    r = mutate(lambda r: setattr(r, "splits", [(f"{r.balance.dates[1]}", 10.0)]))
    c = by_key(r)["share_change"]
    assert c.status == UNKNOWN and "拆" in c.note


def test_share_change_needs_enough_history(mutate):
    def f(r):
        r.balance.dates = r.balance.dates[:1]
        r.balance.rows = {k: v[:1] for k, v in r.balance.rows.items()}
    assert by_key(mutate(f))["share_change"].status == UNKNOWN


# ---- 回购
def test_buyback_net_of_issuance(mutate):
    r = mutate(lambda r: r.cashflow.rows.update({"Issuance Of Capital Stock": [1.4e9] * 4}))
    c = by_key(r)["buyback_yield"]  # 净回购 0.1B / 50B = 0.2% < 1%
    assert c.status == FAIL and c.value < 1.0


def test_buyback_missing_statement_is_unknown(mutate):
    from golden_pit.models import Statement
    r = mutate(lambda r: setattr(r, "cashflow", Statement()))
    assert by_key(r)["buyback_yield"].status == UNKNOWN


# ---- 估值
def test_negative_pb_never_passes(mutate):
    r = mutate(lambda r: r.info.update({"priceToBook": -161.0}))
    c = by_key(r)["pb"]
    assert c.status == FAIL and "净资产为负" in c.note


def test_forward_pe_infinity_is_unknown(mutate):
    r = mutate(lambda r: r.info.update({"forwardPE": float("inf")}))
    assert by_key(r)["forward_pe"].status == UNKNOWN


def test_nan_everywhere_does_not_crash(mutate):
    def f(r):
        for k in ("priceToBook", "forwardPE", "currentRatio", "ebitda", "freeCashflow"):
            r.info[k] = float("nan")
    checks = by_key(mutate(f))
    assert checks["pb"].status == UNKNOWN and checks["current_ratio"].status in (UNKNOWN, PASS)


def test_drop_never_negative_when_price_above_high(mutate):
    r = mutate(lambda r: r.info.update({"fiftyTwoWeekHigh": 90.0}))
    assert metrics.drop_from_high(r) == 0.0


# ---- 股息率
def test_dividend_yield_ignores_ambiguous_field(mutate):
    """旧版把 yfinance 返回的 0.67（已是百分数）当小数乘 100，SONY 显示 67%。"""
    def f(r):
        r.info["dividendYield"] = 0.67
        r.info["dividendRate"] = 0.67
        r.info["currentPrice"] = 100.0
    assert metrics.dividend_yield_pct(mutate(f)) == 0.67


def test_dividend_yield_fallback_to_trailing_fraction(mutate):
    def f(r):
        r.info.pop("dividendRate"); r.info["trailingAnnualDividendYield"] = 0.0326
    assert metrics.dividend_yield_pct(mutate(f)) == 3.26


# ---- 金融业
def test_financial_sector_marks_financial_checks_na(mutate):
    r = mutate(lambda r: r.info.update({"sector": "Financial Services"}))
    checks = by_key(r)
    assert all(checks[k].status == NA for k in ("interest_coverage", "current_ratio", "net_debt_ebitda", "fcf", "altman_z"))


# ---- 过期财报
def test_stale_statements_warn(mutate):
    def f(r):
        r.balance.dates = ["2020-12-31", "2019-12-31", "2018-12-31", "2017-12-31"]
    r = mutate(f)
    w = metrics.build_warnings(r, metrics.build_facts(r))
    assert any("过期" in x for x in w)


# ---- 币种
def test_currency_mismatch_makes_cap_ratios_unknown(mutate):
    """旧版 SONY：日元回购额 ÷ 美元市值 = 368% 的"回购收益率"。"""
    r = mutate(lambda r: r.info.update({"currency": "USD", "financialCurrency": "JPY"}))
    checks = by_key(r)
    assert checks["buyback_yield"].status == UNKNOWN and "JPY" in checks["buyback_yield"].note
    assert checks["altman_z"].status == UNKNOWN
    assert checks["interest_coverage"].status == PASS            # 纯财报内部比值不受影响
    w = metrics.build_warnings(r, metrics.build_facts(r))
    assert any("JPY" in x for x in w)


def test_money_shows_statement_currency(mutate):
    r = mutate(lambda r: r.info.update({"currency": "USD", "financialCurrency": "JPY", "totalCash": 9e12, "totalDebt": 1e12}))
    assert "JPY" in by_key(r)["net_debt_ebitda"].note
