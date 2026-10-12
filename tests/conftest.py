"""测试夹具：构造一只"各项都健康"的合成公司，再逐项破坏它来验证规则。"""
import copy
import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from golden_pit.models import RawStock, Statement  # noqa: E402


def _dates(n=4):
    y = date.today().year - 1
    return [f"{y - i}-12-31" for i in range(n)]


def healthy_raw(ticker="TEST") -> RawStock:
    d = _dates()
    return RawStock(
        ticker=ticker,
        info={
            "currentPrice": 100.0, "marketCap": 50e9, "fiftyTwoWeekHigh": 160.0,   # 回撤 37.5%
            "priceToBook": 1.5, "forwardPE": 12.0, "currentRatio": 1.8,
            "totalDebt": 5e9, "totalCash": 2e9, "ebitda": 6e9, "freeCashflow": 3e9,
            "sector": "Industrials", "industry": "Machinery", "longName": "Test Corp",
            "dividendRate": 2.0, "currency": "USD",
        },
        balance=Statement(d, {
            "Total Assets": [60e9] * 4, "Current Assets": [20e9] * 4, "Current Liabilities": [10e9] * 4,
            "Retained Earnings": [15e9] * 4, "Total Liabilities Net Minority Interest": [30e9] * 4,
            "Ordinary Shares Number": [470e6, 490e6, 500e6, 520e6],
        }),
        income=Statement(d, {"EBIT": [5e9] * 4, "Interest Expense": [0.5e9] * 4, "Total Revenue": [40e9] * 4}),
        cashflow=Statement(d, {"Repurchase Of Capital Stock": [-1.5e9] * 4, "Capital Expenditure": [-2e9] * 4}),
        splits=[],
        fetched_at="2026-01-01T00:00:00+00:00",
    )


@pytest.fixture
def raw():
    return healthy_raw()


@pytest.fixture
def mutate():
    """mutate(raw_fn)：深拷贝健康样本并应用修改。"""
    def _make(fn):
        r = copy.deepcopy(healthy_raw())
        fn(r)
        return r
    return _make
