"""国际化：词典完整性 + "英文输出里不能残留中文"。"""
import json
import pathlib
import re
import string

import pytest

from golden_pit import analyze, moat
from golden_pit.config import Thresholds
from golden_pit.i18n import CATALOG, M, T, normalize_lang, render
from golden_pit.timing import STATES
from tests.conftest import healthy_raw

CJK = re.compile(r"[　-〿一-鿿＀-￯]")
SRC = pathlib.Path(__file__).resolve().parent.parent / "golden_pit"


def fields(tpl):
    return {f for _, f, _, _ in string.Formatter().parse(tpl) if f}


def test_catalog_complete_and_placeholders_match():
    for key, (zh, en) in CATALOG.items():
        assert zh.strip() and en.strip(), key
        assert fields(zh) == fields(en), f"{key}: {fields(zh)} vs {fields(en)}"
        assert not CJK.search(en), f"{key} english text contains CJK: {en}"


def test_every_literal_message_key_in_code_exists():
    used = set()
    for p in SRC.rglob("*.py"):
        text = p.read_text(encoding="utf-8")
        used |= set(re.findall(r'\b(?:M|tr)\(\s*"([a-z0-9_]+\.[a-z0-9_.]+)"', text))
    missing = sorted(k for k in used if k not in CATALOG)
    assert not missing, missing
    assert len(used) > 100


def test_dynamic_key_families_exist():
    for k in ("interest_coverage", "current_ratio", "net_debt_ebitda", "fcf", "altman_z", "share_change",
              "buyback_yield", "capex_revenue", "drop", "pb", "forward_pe"):
        assert f"chk.{k}" in CATALOG
    for t in ("golden_pit", "quality_wait", "value_trap", "fail", "insufficient", "n_a", "error"):
        assert f"tier.{t}" in CATALOG and f"tier.{t}.desc" in CATALOG
    for s in STATES:
        assert f"tm.{s}" in CATALOG
    for fam, keys in (("ai.pit", ("cyclical", "structural", "event", "sentiment", "unclear")),
                      ("ai.agree", ("agree", "partial", "disagree")), ("ai.conf", ("low", "medium", "high"))):
        for k in keys:
            assert f"{fam}.{k}" in CATALOG


def test_render_semantics():
    assert render(M("tier.golden_pit"), "zh") == "黄金坑" and render(M("tier.golden_pit"), "en") == "Golden pit"
    nested = M("r.unknown", label=M("chk.pb"), note=M("n.no_data"))
    assert render(nested, "en") == "? Price / book can't be judged — No data"
    lst = M("n.missing_fields", fields=[M("f.revenue"), M("f.ebit")])
    assert render(lst, "zh") == "缺少：营收、EBIT" and render(lst, "en") == "Missing: revenue, EBIT"
    assert render(M("n.split", date="2025-01-01"), "en").count("{ratio}") == 1     # 缺参数不抛异常
    assert render({"$m": "no.such.key"}, "en") == "no.such.key"
    assert render(T("中", "en"), "en") == "en" and render([1, "x", None], "zh") == [1, "x", None]


def test_normalize_lang():
    assert normalize_lang("zh-CN") == "zh" and normalize_lang("zh_TW") == "zh"
    assert normalize_lang("en-US") == "en" and normalize_lang(None) == "en" and normalize_lang("fr") == "en"


def _variants():
    def mod(fn):
        r = healthy_raw("MCD")          # MCD 在知识库里，顺带检查护城河观点的英文
        fn(r)
        return r
    yield "golden", mod(lambda r: None)
    yield "insufficient", mod(lambda r: (r.balance.rows.clear(), r.cashflow.rows.clear(),
                                         [r.info.pop(k, None) for k in ("totalDebt", "totalCash", "ebitda", "freeCashflow", "currentRatio")]))
    yield "financial", mod(lambda r: r.info.update(sector="Financial Services"))
    yield "currency", mod(lambda r: r.info.update(financialCurrency="JPY", currency="EUR"))
    yield "stale+split", mod(lambda r: (setattr(r.balance, "dates", ["2019-12-31", "2018-12-31", "2017-12-31", "2016-12-31"]),
                                        setattr(r, "splits", [("2018-06-01", 2.0)])))
    yield "losses", mod(lambda r: (r.income.rows.update({"EBIT": [-1e9] * 4}), r.info.update(priceToBook=-3, forwardPE=-5)))


@pytest.mark.parametrize("name,raw", list(_variants()))
def test_english_output_has_no_chinese(name, raw):
    result = analyze.analyze_raw(raw, Thresholds())
    text = json.dumps(render(result, "en"), ensure_ascii=False)
    leftovers = CJK.findall(text)
    assert not leftovers, f"{name}: {''.join(leftovers)[:80]}"
    assert CJK.search(json.dumps(render(result, "zh"), ensure_ascii=False))


def test_error_and_moat_render_in_english():
    err = analyze.error_result("ZZZZ", M("err.no_quote", t="ZZZZ"))
    assert not CJK.search(json.dumps(render(err, "en"), ensure_ascii=False))
    for t in moat.curated_tickers():
        assert not CJK.search(json.dumps(render(moat.lookup(t), "en"), ensure_ascii=False)), t


def test_api_language_negotiation(tmp_path):
    from tests.test_api import make_engine
    from golden_pit.server import create_app
    s, engine = make_engine(tmp_path)
    app = create_app(s, engine=engine)
    engine.scanner.run_sync("custom", ["AAA", "BAD"], "t")
    c = app.test_client()
    en = c.get("/api/scan/latest?lang=en").get_data(as_text=True)
    zh = c.get("/api/scan/latest?lang=zh").get_data(as_text=True)
    assert not CJK.search(json.loads(en).__repr__()) and "Golden pit" in en
    assert "黄金坑" in json.loads(zh)["scan"]["results"][0]["verdict"]["label"]
    hdr = c.get("/api/config", headers={"Accept-Language": "zh-CN,zh;q=0.9"}).get_json()
    assert hdr["lang"] == "zh" and hdr["tiers"]["golden_pit"]["label"] == "黄金坑"
    assert c.get("/api/config").get_json()["lang"] == "en"
    st = c.get("/api/scan/status?lang=en").get_json()
    assert "golden pits" in st["message"]
