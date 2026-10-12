"""指标计算：RawStock + 阈值 → 11 项 Check（5 财务 + 3 管理层 + 3 估值）。

纯函数，不访问网络。核心约定：
- 缺失数据 → UNKNOWN（绝不当作通过，也绝不当作 0）
- 对该类公司没有意义的指标 → NA
- 每个 Check 带 value / display / rule / note，前端原样展示，用户能看到"为什么"
- 面向用户的文字一律是 i18n 消息（M），输出时再渲染成中文或英文
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Optional

from .config import Thresholds
from .i18n import M
from .models import FAIL, NA, PASS, UNKNOWN, Check, RawStock, num

FINANCIAL_SECTORS = {"Financial Services"}
STALE_STATEMENT_DAYS = 460  # 最新年报距今超过约 15 个月视为过期
DASH = "—"


# ---------------------------------------------------------------- 格式化（数字与语言无关）
def _x(v: float) -> str:
    return f"{v:.2f}x"


def _pct(v: float, sign: bool = False) -> str:
    return f"{v:+.1f}%" if sign else f"{v:.1f}%"


def _money(v: float, ccy: str = "USD") -> str:
    a = abs(v)
    s = "-" if v < 0 else ""
    sym = "$" if ccy == "USD" else f"{ccy} "
    for unit, div in (("T", 1e12), ("B", 1e9), ("M", 1e6)):
        if a >= div:
            return f"{s}{sym}{a / div:.2f}{unit}"
    return f"{s}{sym}{a:,.0f}"


def _judge(cond: bool) -> str:
    return PASS if cond else FAIL


def _check(key: str, group: str, status: str, value, display, rule, note="") -> Check:
    return Check(key, M(f"chk.{key}"), group, status, value, display, rule, note)


# ---------------------------------------------------------------- 取值辅助
def price_of(info: dict) -> Optional[float]:
    p = num(info.get("currentPrice")) or num(info.get("regularMarketPrice"))
    return p if p and p > 0 else None


def total_debt(raw: RawStock) -> Optional[float]:
    v = num(raw.info.get("totalDebt"))
    return v if v is not None else raw.balance.get("Total Debt")


def total_cash(raw: RawStock) -> Optional[float]:
    v = num(raw.info.get("totalCash"))
    if v is not None:
        return v
    return raw.balance.get("Cash Cash Equivalents And Short Term Investments", "Cash And Cash Equivalents")


def market_cap(raw: RawStock) -> Optional[float]:
    mc = num(raw.info.get("marketCap"))
    if mc and mc > 0:
        return mc
    sh, p = num(raw.info.get("sharesOutstanding")), price_of(raw.info)
    return sh * p if sh and p else None


def statement_currency(raw: RawStock) -> str:
    return raw.info.get("financialCurrency") or raw.info.get("currency") or "USD"


def currency_mismatch(raw: RawStock) -> Optional[dict]:
    """财报币种 ≠ 报价币种（常见于 ADR，如 SONY：财报日元、股价美元）。
    此时"市值 vs 财报金额"的比值没有意义，必须标为未知，而不是算出一个荒谬的数。"""
    q, f = raw.info.get("currency"), raw.info.get("financialCurrency")
    return M("n.currency_mismatch", fin=f, quote=q) if q and f and q != f else None


def is_financial(raw: RawStock) -> bool:
    return raw.info.get("sector") in FINANCIAL_SECTORS


def _as_date(s: str) -> date:
    return datetime.strptime(s[:10], "%Y-%m-%d").date()


# ---------------------------------------------------------------- 财务（5 项）
def check_interest_coverage(raw: RawStock, th: Thresholds) -> Check:
    rule = f"≥ {_x(th.interest_coverage_min)}"
    mk = lambda status, value, display, note="": _check(  # noqa: E731
        "interest_coverage", "financial", status, value, display, rule, note)
    ebit = raw.income.get("EBIT", "Operating Income")
    interest = raw.income.get("Interest Expense", "Interest Expense Non Operating")
    if ebit is None:
        return mk(UNKNOWN, None, DASH, M("n.no_ebit"))
    if interest is None:
        debt = total_debt(raw)
        if debt is not None and debt <= 0:
            return mk(PASS, None, M("disp.no_debt"), M("n.no_debt"))
        return mk(UNKNOWN, None, DASH, M("n.interest_undisclosed"))
    interest = abs(interest)
    if interest == 0:
        return mk(PASS, None, M("disp.no_interest"), M("n.zero_interest"))
    ratio = ebit / interest  # 注意：不取绝对值，EBIT 为负时覆盖倍数为负，必须不通过
    return mk(_judge(ratio >= th.interest_coverage_min), ratio, _x(ratio),
              M("n.ebit_negative") if ebit < 0 else "")


def check_current_ratio(raw: RawStock, th: Thresholds) -> Check:
    rule = f"≥ {th.current_ratio_min:.2f}"
    cr = num(raw.info.get("currentRatio"))
    note = ""
    if cr is None:
        ca, cl = raw.balance.get("Current Assets"), raw.balance.get("Current Liabilities")
        if ca is not None and cl:
            cr, note = ca / cl, M("n.from_balance")
    if cr is None:
        return _check("current_ratio", "financial", UNKNOWN, None, DASH, rule, M("n.no_current"))
    return _check("current_ratio", "financial", _judge(cr >= th.current_ratio_min), cr, f"{cr:.2f}", rule, note)


def check_net_debt(raw: RawStock, th: Thresholds) -> Check:
    rule = f"≤ {_x(th.net_debt_to_ebitda_max)}"
    mk = lambda status, value, display, note="": _check(  # noqa: E731
        "net_debt_ebitda", "financial", status, value, display, rule, note)
    debt, cash = total_debt(raw), total_cash(raw)
    ccy = statement_currency(raw)
    if debt is None or cash is None:
        return mk(UNKNOWN, None, DASH, M("n.no_debt_cash"))
    net = debt - cash
    if net <= 0:
        return mk(PASS, net / abs(debt) if debt else 0.0, M("disp.net_cash"),
                  M("n.net_cash", cash=_money(cash, ccy), debt=_money(debt, ccy)))
    ebitda = num(raw.info.get("ebitda"))
    if ebitda is None:
        ebitda = raw.income.get("EBITDA", "Normalized EBITDA")
    if ebitda is None:
        return mk(UNKNOWN, None, DASH, M("n.no_ebitda", net=_money(net, ccy)))
    if ebitda <= 0:
        return mk(FAIL, None, "EBITDA≤0", M("n.ebitda_nonpos", net=_money(net, ccy), ebitda=_money(ebitda, ccy)))
    ratio = net / ebitda
    return mk(_judge(ratio <= th.net_debt_to_ebitda_max), ratio, _x(ratio))


def check_fcf(raw: RawStock, th: Thresholds) -> Check:
    fcf = num(raw.info.get("freeCashflow"))
    note = ""
    if fcf is None:
        fcf = raw.cashflow.get("Free Cash Flow")
        note = M("n.fcf_annual")
    if fcf is None:
        cfo, capex = raw.cashflow.get("Operating Cash Flow"), raw.cashflow.get("Capital Expenditure")
        if cfo is not None and capex is not None:
            fcf, note = cfo + capex, M("n.fcf_derived")
    if fcf is None:
        return _check("fcf", "financial", UNKNOWN, None, DASH, "> 0", M("n.no_cashflow"))
    return _check("fcf", "financial", _judge(fcf > 0), fcf, _money(fcf, statement_currency(raw)), "> 0", note)


def check_altman(raw: RawStock, th: Thresholds) -> Check:
    rule = f"≥ {th.altman_z_min:.1f}"
    b, i = raw.balance, raw.income
    fields = {
        "total_assets": b.get("Total Assets"),
        "current_assets": b.get("Current Assets"),
        "current_liabilities": b.get("Current Liabilities"),
        "retained_earnings": b.get("Retained Earnings"),
        "total_liabilities": b.get("Total Liabilities Net Minority Interest"),
        "ebit": i.get("EBIT", "Operating Income"),
        "revenue": i.get("Total Revenue"),
        "market_cap": market_cap(raw),
    }
    mismatch = currency_mismatch(raw)
    if mismatch:
        return _check("altman_z", "financial", UNKNOWN, None, DASH, rule, mismatch)
    missing = [M(f"f.{k}") for k, v in fields.items() if v is None]
    if missing:
        return _check("altman_z", "financial", UNKNOWN, None, DASH, rule, M("n.missing_fields", fields=missing))
    ta, tl = fields["total_assets"], fields["total_liabilities"]
    if ta <= 0 or tl <= 0:
        return _check("altman_z", "financial", UNKNOWN, None, DASH, rule, M("n.bad_assets"))
    z = (1.2 * (fields["current_assets"] - fields["current_liabilities"]) / ta
         + 1.4 * fields["retained_earnings"] / ta
         + 3.3 * fields["ebit"] / ta
         + 0.6 * fields["market_cap"] / tl
         + 1.0 * fields["revenue"] / ta)
    return _check("altman_z", "financial", _judge(z >= th.altman_z_min), z, f"{z:.2f}", rule, M("n.altman_classic"))


# ---------------------------------------------------------------- 管理层（3 项）
def check_share_change(raw: RawStock, th: Thresholds) -> Check:
    rule = M("rule.share_reduction", v=_pct(th.shares_reduction_min_pct))
    mk = lambda status, value, display, note="": _check(  # noqa: E731
        "share_change", "management", status, value, display, rule, note)
    series = raw.balance.series("Ordinary Shares Number", "Share Issued")
    if len(series) < 2:
        return mk(UNKNOWN, None, DASH, M("n.shares_history"))
    (new_d, new_v), (old_d, old_v) = series[0], series[-1]
    years = (_as_date(new_d) - _as_date(old_d)).days / 365.25
    if years < 1.5 or old_v <= 0:
        return mk(UNKNOWN, None, DASH, M("n.shares_window"))
    # 只要最早报告期之后发生过拆/并股，历史股本是否复权就不可确定 → 宁可"未知"
    window_splits = [(d, r) for d, r in raw.splits if d > old_d]
    change = (new_v - old_v) / old_v * 100
    if window_splits:
        d, r = window_splits[-1]
        return mk(UNKNOWN, change, DASH, M("n.split", date=d, ratio=f"{r:g}", chg=_pct(change, True)))
    return mk(_judge(change <= -th.shares_reduction_min_pct), change,
              M("disp.share_change", chg=_pct(change, True), years=f"{years:.1f}"), f"{old_d[:4]}→{new_d[:4]}")


def check_buyback(raw: RawStock, th: Thresholds) -> Check:
    rule = M("rule.buyback", v=_pct(th.buyback_yield_min_pct))
    mk = lambda status, value, display, note="": _check(  # noqa: E731
        "buyback_yield", "management", status, value, display, rule, note)
    if raw.cashflow.empty:
        return mk(UNKNOWN, None, DASH, M("n.no_cf_statement"))
    mismatch = currency_mismatch(raw)
    if mismatch:
        return mk(UNKNOWN, None, DASH, mismatch)
    mc = market_cap(raw)
    if not mc:
        return mk(UNKNOWN, None, DASH, M("n.no_mcap"))
    rep = raw.cashflow.get("Repurchase Of Capital Stock")
    iss = raw.cashflow.get("Issuance Of Capital Stock")
    gross = abs(rep) if rep is not None and rep < 0 else 0.0
    issued = iss if iss is not None and iss > 0 else 0.0
    net = gross - issued
    y = net / mc * 100
    note = M("n.buyback_net") if issued else (M("n.no_buyback") if rep is None else "")
    return mk(_judge(y >= th.buyback_yield_min_pct), y, _pct(y, True), note)


def check_capex(raw: RawStock, th: Thresholds) -> Check:
    rule = M("rule.capex", v=_pct(th.capex_to_revenue_max_pct))
    capex, rev = raw.cashflow.get("Capital Expenditure"), raw.income.get("Total Revenue")
    if capex is None or not rev or rev <= 0:
        return _check("capex_revenue", "management", UNKNOWN, None, DASH, rule, M("n.no_capex"))
    r = abs(capex) / rev * 100
    return _check("capex_revenue", "management", _judge(r <= th.capex_to_revenue_max_pct), r, _pct(r), rule)


# ---------------------------------------------------------------- 估值（3 项）
def drop_from_high(raw: RawStock) -> Optional[float]:
    p, hi = price_of(raw.info), num(raw.info.get("fiftyTwoWeekHigh"))
    if not p or not hi or hi <= 0:
        return None
    return max(0.0, (hi - p) / hi * 100)


def check_drop(raw: RawStock, th: Thresholds) -> Check:
    rule = f"≥ {_pct(th.drop_from_high_min_pct)}"
    d = drop_from_high(raw)
    if d is None:
        return _check("drop", "valuation", UNKNOWN, None, DASH, rule, M("n.no_price_high"))
    return _check("drop", "valuation", _judge(d >= th.drop_from_high_min_pct), d, _pct(d), rule, M("n.required"))


def check_pb(raw: RawStock, th: Thresholds) -> Check:
    rule = f"0 < P/B ≤ {_x(th.pb_max)}"
    pb = num(raw.info.get("priceToBook"))
    if pb is None:
        return _check("pb", "valuation", UNKNOWN, None, DASH, rule, M("n.no_data"))
    if pb <= 0:
        return _check("pb", "valuation", FAIL, pb, _x(pb), rule, M("n.neg_book"))
    return _check("pb", "valuation", _judge(pb <= th.pb_max), pb, _x(pb), rule)


def check_forward_pe(raw: RawStock, th: Thresholds) -> Check:
    rule = f"0 < Forward P/E ≤ {th.forward_pe_max:.0f}"
    pe = num(raw.info.get("forwardPE"))
    if pe is None:
        return _check("forward_pe", "valuation", UNKNOWN, None, DASH, rule, M("n.no_data"))
    if pe <= 0:
        return _check("forward_pe", "valuation", FAIL, pe, f"{pe:.1f}", rule, M("n.expected_loss"))
    return _check("forward_pe", "valuation", _judge(pe <= th.forward_pe_max), pe, f"{pe:.1f}", rule)


# ---------------------------------------------------------------- 汇总
def dividend_yield_pct(raw: RawStock) -> Optional[float]:
    """不用 info['dividendYield']：它在不同 yfinance 版本里单位不一致（小数 vs 百分数）。"""
    p, rate = price_of(raw.info), num(raw.info.get("dividendRate"))
    if p and rate is not None:
        return round(rate / p * 100, 2)
    frac = num(raw.info.get("trailingAnnualDividendYield"))  # 始终是小数
    return round(frac * 100, 2) if frac is not None else None


def build_facts(raw: RawStock) -> dict:
    info = raw.info
    return {
        "name": info.get("longName") or info.get("shortName") or raw.ticker,
        "sector": info.get("sector") or "",
        "industry": info.get("industry") or "",
        "currency": info.get("currency") or "USD",
        "financial_currency": statement_currency(raw),
        "price": price_of(info),
        "market_cap": market_cap(raw),
        "high_52w": num(info.get("fiftyTwoWeekHigh")),
        "trailing_pe": num(info.get("trailingPE")),
        "dividend_yield_pct": dividend_yield_pct(raw),
        "is_financial": is_financial(raw),
        "statements_as_of": raw.balance.dates[0] if raw.balance.dates else None,
    }


def build_warnings(raw: RawStock, facts: dict) -> list[dict]:
    warnings = []
    if facts["statements_as_of"]:
        age = (datetime.now().date() - _as_date(facts["statements_as_of"])).days
        if age > STALE_STATEMENT_DAYS:
            warnings.append(M("w.stale", date=facts["statements_as_of"], months=age // 30))
    else:
        warnings.append(M("w.no_balance"))
    mismatch = currency_mismatch(raw)
    if mismatch:
        warnings.append(M("w.currency_mismatch", msg=mismatch))
    if facts["currency"] != "USD":
        warnings.append(M("w.non_usd", ccy=facts["currency"]))
    return warnings


def build_checks(raw: RawStock, th: Thresholds) -> list[Check]:
    if is_financial(raw):
        fin = [_check(k, "financial", NA, None, M("disp.na"), "", M("n.financial_na"))
               for k in ("interest_coverage", "current_ratio", "net_debt_ebitda", "fcf", "altman_z")]
    else:
        fin = [check_interest_coverage(raw, th), check_current_ratio(raw, th), check_net_debt(raw, th),
               check_fcf(raw, th), check_altman(raw, th)]
    mgmt = [check_share_change(raw, th), check_buyback(raw, th), check_capex(raw, th)]
    val = [check_drop(raw, th), check_pb(raw, th), check_forward_pe(raw, th)]
    return fin + mgmt + val
