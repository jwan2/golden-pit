"""指标计算：RawStock + 阈值 → 11 项 Check（5 财务 + 3 管理层 + 3 估值）。

纯函数，不访问网络。核心约定：
- 缺失数据 → UNKNOWN（绝不当作通过，也绝不当作 0）
- 对该类公司没有意义的指标 → NA
- 每个 Check 带 value / display / rule / note，前端原样展示，用户能看到"为什么"
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Optional

from .config import Thresholds
from .models import FAIL, NA, PASS, UNKNOWN, Check, RawStock, num

FINANCIAL_SECTORS = {"Financial Services"}
STALE_STATEMENT_DAYS = 460  # 最新年报距今超过约 15 个月视为过期


# ---------------------------------------------------------------- 格式化
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


def currency_mismatch(raw: RawStock) -> Optional[str]:
    """财报币种 ≠ 报价币种（常见于 ADR，如 SONY：财报日元、股价美元）。
    此时"市值 vs 财报金额"的比值没有意义，必须标为未知，而不是算出一个荒谬的数。"""
    q, f = raw.info.get("currency"), raw.info.get("financialCurrency")
    return f"财报币种 {f} ≠ 报价币种 {q}，市值与财报金额不可直接相比" if q and f and q != f else None


def is_financial(raw: RawStock) -> bool:
    return raw.info.get("sector") in FINANCIAL_SECTORS


def _as_date(s: str) -> date:
    return datetime.strptime(s[:10], "%Y-%m-%d").date()


# ---------------------------------------------------------------- 财务（5 项）
def check_interest_coverage(raw: RawStock, th: Thresholds) -> Check:
    rule = f"≥ {_x(th.interest_coverage_min)}"
    mk = lambda status, value, display, note="": Check(  # noqa: E731
        "interest_coverage", "利息保障倍数", "financial", status, value, display, rule, note)
    ebit = raw.income.get("EBIT", "Operating Income")
    interest = raw.income.get("Interest Expense", "Interest Expense Non Operating")
    if ebit is None:
        return mk(UNKNOWN, None, "—", "缺少 EBIT")
    if interest is None:
        debt = total_debt(raw)
        if debt is not None and debt <= 0:
            return mk(PASS, None, "无债务", "无有息债务")
        return mk(UNKNOWN, None, "—", "数据源未披露利息支出，不能当作无债务")
    interest = abs(interest)
    if interest == 0:
        return mk(PASS, None, "无利息", "利息支出为 0")
    ratio = ebit / interest  # 注意：不取绝对值，EBIT 为负时覆盖倍数为负，必须不通过
    return mk(_judge(ratio >= th.interest_coverage_min), ratio, _x(ratio),
              "EBIT 为负" if ebit < 0 else "")


def check_current_ratio(raw: RawStock, th: Thresholds) -> Check:
    rule = f"≥ {th.current_ratio_min:.2f}"
    cr = num(raw.info.get("currentRatio"))
    note = ""
    if cr is None:
        ca, cl = raw.balance.get("Current Assets"), raw.balance.get("Current Liabilities")
        if ca is not None and cl:
            cr, note = ca / cl, "由资产负债表计算"
    if cr is None:
        return Check("current_ratio", "流动比率", "financial", UNKNOWN, None, "—", rule, "缺少流动资产/负债")
    return Check("current_ratio", "流动比率", "financial", _judge(cr >= th.current_ratio_min), cr, f"{cr:.2f}", rule, note)


def check_net_debt(raw: RawStock, th: Thresholds) -> Check:
    rule = f"≤ {_x(th.net_debt_to_ebitda_max)}"
    mk = lambda status, value, display, note="": Check(  # noqa: E731
        "net_debt_ebitda", "净负债 / EBITDA", "financial", status, value, display, rule, note)
    debt, cash = total_debt(raw), total_cash(raw)
    ccy = statement_currency(raw)
    if debt is None or cash is None:
        return mk(UNKNOWN, None, "—", "缺少总债务或现金数据")
    net = debt - cash
    if net <= 0:
        return mk(PASS, net / abs(debt) if debt else 0.0, "净现金", f"现金 {_money(cash, ccy)} ≥ 债务 {_money(debt, ccy)}")
    ebitda = num(raw.info.get("ebitda"))
    if ebitda is None:
        ebitda = raw.income.get("EBITDA", "Normalized EBITDA")
    if ebitda is None:
        return mk(UNKNOWN, None, "—", f"净负债 {_money(net, ccy)}，但缺少 EBITDA")
    if ebitda <= 0:
        return mk(FAIL, None, "EBITDA≤0", f"净负债 {_money(net, ccy)} 而 EBITDA 为 {_money(ebitda, ccy)}")
    ratio = net / ebitda
    return mk(_judge(ratio <= th.net_debt_to_ebitda_max), ratio, _x(ratio))


def check_fcf(raw: RawStock, th: Thresholds) -> Check:
    fcf = num(raw.info.get("freeCashflow"))
    note = ""
    if fcf is None:
        fcf = raw.cashflow.get("Free Cash Flow")
        note = "取自最新年报"
    if fcf is None:
        cfo, capex = raw.cashflow.get("Operating Cash Flow"), raw.cashflow.get("Capital Expenditure")
        if cfo is not None and capex is not None:
            fcf, note = cfo + capex, "经营现金流 + 资本开支（年报）"
    if fcf is None:
        return Check("fcf", "自由现金流", "financial", UNKNOWN, None, "—", "> 0", "缺少现金流数据")
    return Check("fcf", "自由现金流", "financial", _judge(fcf > 0), fcf, _money(fcf, statement_currency(raw)), "> 0", note)


def check_altman(raw: RawStock, th: Thresholds) -> Check:
    rule = f"≥ {th.altman_z_min:.1f}"
    b, i = raw.balance, raw.income
    fields = {
        "总资产": b.get("Total Assets"),
        "流动资产": b.get("Current Assets"),
        "流动负债": b.get("Current Liabilities"),
        "留存收益": b.get("Retained Earnings"),
        "总负债": b.get("Total Liabilities Net Minority Interest"),
        "EBIT": i.get("EBIT", "Operating Income"),
        "营收": i.get("Total Revenue"),
        "市值": market_cap(raw),
    }
    mismatch = currency_mismatch(raw)
    if mismatch:
        return Check("altman_z", "Altman Z-Score", "financial", UNKNOWN, None, "—", rule, mismatch)
    missing = [k for k, v in fields.items() if v is None]
    if missing:
        return Check("altman_z", "Altman Z-Score", "financial", UNKNOWN, None, "—", rule,
                     "缺少：" + "、".join(missing))
    ta, tl = fields["总资产"], fields["总负债"]
    if ta <= 0 or tl <= 0:
        return Check("altman_z", "Altman Z-Score", "financial", UNKNOWN, None, "—", rule, "总资产/总负债无效")
    z = (1.2 * (fields["流动资产"] - fields["流动负债"]) / ta
         + 1.4 * fields["留存收益"] / ta
         + 3.3 * fields["EBIT"] / ta
         + 0.6 * fields["市值"] / tl
         + 1.0 * fields["营收"] / ta)
    return Check("altman_z", "Altman Z-Score", "financial", _judge(z >= th.altman_z_min), z, f"{z:.2f}", rule,
                 "经典制造业版本；轻资产公司留存收益偏低会被低估")


# ---------------------------------------------------------------- 管理层（3 项）
def check_share_change(raw: RawStock, th: Thresholds) -> Check:
    rule = f"净减少 ≥ {_pct(th.shares_reduction_min_pct)}"
    mk = lambda status, value, display, note="": Check(  # noqa: E731
        "share_change", "总股本变化", "management", status, value, display, rule, note)
    series = raw.balance.series("Ordinary Shares Number", "Share Issued")
    if len(series) < 2:
        return mk(UNKNOWN, None, "—", "历史股本数据不足（需要至少 2 个年报期）")
    (new_d, new_v), (old_d, old_v) = series[0], series[-1]
    years = (_as_date(new_d) - _as_date(old_d)).days / 365.25
    if years < 1.5 or old_v <= 0:
        return mk(UNKNOWN, None, "—", "可比期间不足 1.5 年")
    # 只要最早报告期之后发生过拆/并股，历史股本是否复权就不可确定 → 宁可"未知"
    window_splits = [(d, r) for d, r in raw.splits if d > old_d]
    change = (new_v - old_v) / old_v * 100
    if window_splits:
        d, r = window_splits[-1]
        return mk(UNKNOWN, change, "—",
                  f"窗口内发生拆/并股（{d}，×{r:g}），股本未必复权，无法可靠判断（未复权变化 {_pct(change, True)}）")
    return mk(_judge(change <= -th.shares_reduction_min_pct), change,
              f"{_pct(change, True)}（{years:.1f}年）", f"{old_d[:4]}→{new_d[:4]}")


def check_buyback(raw: RawStock, th: Thresholds) -> Check:
    rule = f"净回购 ≥ {_pct(th.buyback_yield_min_pct)} 市值"
    mk = lambda status, value, display, note="": Check(  # noqa: E731
        "buyback_yield", "净回购收益率", "management", status, value, display, rule, note)
    if raw.cashflow.empty:
        return mk(UNKNOWN, None, "—", "缺少现金流量表")
    mismatch = currency_mismatch(raw)
    if mismatch:
        return mk(UNKNOWN, None, "—", mismatch)
    mc = market_cap(raw)
    if not mc:
        return mk(UNKNOWN, None, "—", "缺少市值")
    rep = raw.cashflow.get("Repurchase Of Capital Stock")
    iss = raw.cashflow.get("Issuance Of Capital Stock")
    gross = abs(rep) if rep is not None and rep < 0 else 0.0
    issued = iss if iss is not None and iss > 0 else 0.0
    net = gross - issued
    y = net / mc * 100
    note = "回购减去发行（含股权激励发行）" if issued else ("现金流表未披露回购" if rep is None else "")
    return mk(_judge(y >= th.buyback_yield_min_pct), y, _pct(y, True), note)


def check_capex(raw: RawStock, th: Thresholds) -> Check:
    rule = f"≤ {_pct(th.capex_to_revenue_max_pct)} 营收"
    capex, rev = raw.cashflow.get("Capital Expenditure"), raw.income.get("Total Revenue")
    if capex is None or not rev or rev <= 0:
        return Check("capex_revenue", "资本开支 / 营收", "management", UNKNOWN, None, "—", rule, "缺少资本开支或营收")
    r = abs(capex) / rev * 100
    return Check("capex_revenue", "资本开支 / 营收", "management", _judge(r <= th.capex_to_revenue_max_pct),
                 r, _pct(r), rule)


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
        return Check("drop", "距 52 周高点回撤", "valuation", UNKNOWN, None, "—", rule, "缺少价格或 52 周高点")
    return Check("drop", "距 52 周高点回撤", "valuation", _judge(d >= th.drop_from_high_min_pct), d, _pct(d), rule,
                 "必需条件")


def check_pb(raw: RawStock, th: Thresholds) -> Check:
    rule = f"0 < P/B ≤ {_x(th.pb_max)}"
    pb = num(raw.info.get("priceToBook"))
    if pb is None:
        return Check("pb", "市净率 P/B", "valuation", UNKNOWN, None, "—", rule, "缺少数据")
    if pb <= 0:
        return Check("pb", "市净率 P/B", "valuation", FAIL, pb, _x(pb), rule, "净资产为负，P/B 无意义（常见于大额回购）")
    return Check("pb", "市净率 P/B", "valuation", _judge(pb <= th.pb_max), pb, _x(pb), rule)


def check_forward_pe(raw: RawStock, th: Thresholds) -> Check:
    rule = f"0 < Forward P/E ≤ {th.forward_pe_max:.0f}"
    pe = num(raw.info.get("forwardPE"))
    if pe is None:
        return Check("forward_pe", "前瞻市盈率", "valuation", UNKNOWN, None, "—", rule, "缺少数据")
    if pe <= 0:
        return Check("forward_pe", "前瞻市盈率", "valuation", FAIL, pe, f"{pe:.1f}", rule, "预期亏损")
    return Check("forward_pe", "前瞻市盈率", "valuation", _judge(pe <= th.forward_pe_max), pe, f"{pe:.1f}", rule)


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


def build_warnings(raw: RawStock, facts: dict) -> list[str]:
    warnings = []
    if facts["statements_as_of"]:
        age = (datetime.now().date() - _as_date(facts["statements_as_of"])).days
        if age > STALE_STATEMENT_DAYS:
            warnings.append(f"最新年报是 {facts['statements_as_of']}，已过期 {age // 30} 个月，财务指标可能不反映现状")
    else:
        warnings.append("没有取到任何资产负债表")
    mismatch = currency_mismatch(raw)
    if mismatch:
        warnings.append(mismatch + "；Altman Z、回购收益率已标为未知，其余金额以财报币种显示")
    if facts["currency"] != "USD":
        warnings.append(f"报价币种为 {facts['currency']}，非美元标的")
    return warnings


def build_checks(raw: RawStock, th: Thresholds) -> list[Check]:
    if is_financial(raw):
        fin = [Check(k, lbl, "financial", NA, None, "不适用", "", "金融业的杠杆/流动性指标没有可比意义")
               for k, lbl in (("interest_coverage", "利息保障倍数"), ("current_ratio", "流动比率"),
                              ("net_debt_ebitda", "净负债 / EBITDA"), ("fcf", "自由现金流"),
                              ("altman_z", "Altman Z-Score"))]
    else:
        fin = [check_interest_coverage(raw, th), check_current_ratio(raw, th), check_net_debt(raw, th),
               check_fcf(raw, th), check_altman(raw, th)]
    mgmt = [check_share_change(raw, th), check_buyback(raw, th), check_capex(raw, th)]
    val = [check_drop(raw, th), check_pb(raw, th), check_forward_pe(raw, th)]
    return fin + mgmt + val
